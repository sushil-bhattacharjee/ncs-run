# -*- mode: python; python-indent: 4 -*-
import _ncs
import ncs.maapi as maapi
import ncs.maagic as maagic
from ncs.dp import Action
from _ncs import maapi as _maapi
from phased_provisioning.pdp_task_map import WorkItemDeque, WorkItem, TaskMap
from phased_provisioning.pdp_task_data import TaskData
from phased_provisioning.namespaces.ciscoPhasedProvisioning_ns import ns as pdp_ns
from phased_provisioning.constants import Constants
from phased_provisioning.util import get_kp_service_id
from phased_provisioning.ha_util import validate_ha_mode
from phased_provisioning.maapi_util import MaapiUtil
from phased_provisioning.actions_util import (ProcessPolicyUpdate, start_batch_process,
                                              can_task_be_paused, task_cleanup, set_action_output)


class PDPTaskRunHandler(Action):
    """
    Action handler for PDP Tasks
    """

    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        task_name = get_kp_service_id(kp)
        try:
            validate_ha_mode()
            mu = MaapiUtil()
            task_data = TaskData(task_name, self.log, mu)

            status, reason = task_data.is_task_suspended(True)
            if status:
                raise Exception(f"Task is in suspended state, because {reason}")

            # Create a WorkItemQueue for this task
            queue = WorkItemDeque()
            task_map = TaskMap.getInstance()
            error_budget = 0
            schedule = None
            time = None

            if (not task_map.key_exists_in_map(task_name)):
                task_map.put(task_name, queue)
            else:
                queue = TaskMap.getInstance().get(task_name)

            # Get policy info and schedule accordingly
            with mu.trans_read_running() as th:
                task = maagic.get_node(th, kp)
                policy_id = task.policy
                policy = maagic.get_node(th, Constants.POLICY_PATH.format(policy_id))
                schedule = pdp_ns.cisco_pdp_immediately_
                if str(policy.schedule.schedule_type) == pdp_ns.cisco_pdp_schedule_when_:
                    schedule = pdp_ns.cisco_pdp_future_
                    time = policy.schedule.future.time
                error_budget = policy.error_budget
                task_data.create_task_status(error_budget)

                work_items = self._filter_nodes(th, task, input)
                # Add WorkItems to the WorkItemQueue in pendingQ phase
                queue.enqueue_all(work_items, mu)

            self.log.info(f"{task_name} - PSP Task schedule {schedule}")
            if schedule == pdp_ns.cisco_pdp_future_:
                task_data.setup_scheduler(time)
            elif schedule == pdp_ns.cisco_pdp_immediately_:
                start_batch_process(mu, task_name, self.log, False)

            set_action_output(output, True, "Task successfully processed.")
        except Exception as exp:
            self.log.exception(f"{task_name} - Error while processing task: {exp}")
            set_action_output(output, False, f"Task processing failed: {str(exp)}")

    def _filter_nodes(self, th, task, input):
        filtered_nodes = []
        if task.target:
            target_node = task.target
            nodes = self._get_target(th, target_node)
            # If there is no filter set then all the list elements under the target node
            # are candidates for the phased provisioning
            filtered_nodes = nodes
            if task.filter:
                self.log.debug(f"{task.name} - Filtering nodes")
                filtered_nodes_iter = nodes.filter(xpath_expr=task.filter)
                filtered_nodes = list(filtered_nodes_iter)
            if len(filtered_nodes) == 0:
                raise Exception("Task filter/target did not result in any nodes")
        else:
            for target_node in input.target_nodes.as_list():
                filtered_nodes.append(self._get_target(th, target_node))

        return [WorkItem(task.name, node._path) for node in filtered_nodes]

    def _get_target(self, th, target_node):
        if th.exists(target_node):
            return maagic.get_node(th, target_node)


class PDPTaskExecutionHandler(Action):
    """
    Action handler for PDP Tasks
    """
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        task_name = get_kp_service_id(kp)
        try:
            validate_ha_mode()
            start_batch_process(MaapiUtil(), task_name, self.log, next_run=True)
        except Exception as exp:
            self.log.exception(f"{task_name} - Error while executing task: {exp}")


class PDPTaskStatusPurgeHandler(Action):
    """
    Action handler for PDP Task Status Cleanup
    """
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output, trans):
        task_name = get_kp_service_id(kp)
        self.log.info(f"{task_name} - PURGE ACTION CALLED...")
        try:
            validate_ha_mode()
            root = maagic.get_root(trans)
            if not (task_name in root.phased_provisioning.task):
                task_cleanup(task_name)
                set_action_output(output, True,
                                  f"{task_name} task-status operational data successfully purged.")
            else:
                raise Exception(
                    f"ERROR: {task_name} configuration present in CDB."
                    " WARNING: Invoke purge when delete task config failed to delete task-status.")
        except Exception as exp:
            self.log.exception(f"{task_name} - Error while purging: {exp}")
            set_action_output(output, False, str(exp))


class PDPTaskChangeHandler(Action):
    """
    Action handler for PDP Tasks Change from kicker
    """
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output, trans):
        _ncs.dp.action_set_timeout(uinfo, 750)
        task_name = get_kp_service_id(input.path)
        try:
            validate_ha_mode()
            self.log.info(f"{task_name} - KICKER INVOKED ON TASK CHANGE")
            root = maagic.get_root(trans)
            if not (task_name in root.phased_provisioning.task):
                task_cleanup(task_name)
        except Exception as exp:
            self.log.exception(f"{task_name} - Error while handling task change: {exp}")


class PolicyChangeHandler(Action):
    """
    Action handler for PDP policy Change from kicker
    """
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output, trans):
        _ncs.dp.action_set_timeout(uinfo, 750)
        policy_name = get_kp_service_id(input.path)
        try:
            validate_ha_mode()
            self.log.info(f"{policy_name} - KICKER INVOKED ON POLICY CHANGE")
            policy_update = ProcessPolicyUpdate(self.log)
            with maapi.Maapi() as m:
                with m.attach(input.tid) as th:
                    th.diff_iterate(policy_update.iterate, 0)

            changes = policy_update.changes
            if len(changes) > 0:
                tasks = []
                # filter task which use the policy and in init or in-progress state
                # init task has to be started if policy schedule change from future to immediately
                query = Constants.POLICY_UPDATE_QUERY.format(policy_name)
                mu = MaapiUtil()
                with mu.trans_read_running() as rth:
                    qh = _maapi.query_start(rth.maapi.msock, rth.th, query, '/', 0, 1,
                                            _ncs.QUERY_STRING, ["name"], [])
                    res = _maapi.query_result(rth.maapi.msock, qh)
                    for r in res:
                        tasks.extend(r)
                    _maapi.query_stop(rth.maapi.msock, qh)
                    for task in tasks:
                        self.log.info(f"{policy_name} - Impacted tasks: {task}")
                        queue = TaskMap.getInstance().get(task)
                        # mark the task's policy updated so that batch, window and error-budget
                        # can be updated for the next batch execution
                        queue.policy_updated = True

                        task_data = TaskData(task, self.log, mu)
                        if 'schedule-immediate' in changes:
                            task_data.delete_scheduler()
                            # Start batch process because schedule changed to immediate
                            # Required for task is in init state
                            start_batch_process(mu, task, self.log, False)
                        elif 'schedule-time' in changes:
                            task_data.setup_scheduler(changes['schedule-time'])
                        if 'error-budget' in changes:
                            task_data.adjust_error_budget(changes['error-budget'])
        except Exception as exp:
            self.log.exception(f"{policy_name} - Error while handling policy change: {exp}")


class PDPBriefExecutionHandler(Action):
    """
    Action handler for PDP Brief
    """
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        try:
            self.log.info("BRIEF ACTION CALLED...")
            validate_ha_mode()
            with MaapiUtil().trans_read_oper() as th:
                task = maagic.get_node(th, kp)
                output.state = task.state
                output.reason = task.reason
                output.current_error_budget = task.current_error_budget
                output.allocated_error_budget = task.allocated_error_budget
                output.last_runtime = task.last_runtime
                output.next_runtime = task.next_runtime
                output.schedule_task_id = task.schedule_task_id
                output.pending_nodes_count = len(task.pending_nodes)
                output.in_progress_nodes_count = len(task.in_progress_nodes)
                output.completed_nodes_count = len(task.completed_nodes)
                output.failed_nodes_count = len(task.failed_nodes)
        except Exception as exp:
            self.log.exception(f"Error in brief action: {exp}")


class PDPTaskResumeHandler(Action):
    """
    Action handler for resuming PDP Tasks
    """

    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        task_name = get_kp_service_id(kp)
        try:
            validate_ha_mode()
            error_budget = None
            schedule = pdp_ns.cisco_pdp_immediately_
            time = None
            mu = MaapiUtil()
            task_data = TaskData(task_name, self.log, mu)
            if task_data.is_task_suspended():
                with mu.trans_read_running() as th:
                    task = maagic.get_node(th, kp)
                    policy_id = task.policy
                    policy = maagic.get_node(th, Constants.POLICY_PATH.format(policy_id))
                    if input.reset_error_budget:
                        error_budget = str(policy.error_budget)
                    if str(policy.schedule.schedule_type) == pdp_ns.cisco_pdp_schedule_when_:
                        schedule = pdp_ns.cisco_pdp_future_
                        time = policy.schedule.future.time
                # Reset/update error budget
                if schedule == pdp_ns.cisco_pdp_future_:
                    task_data.update_task_status(pdp_ns.cisco_pdp_pdp_init_, clear_reason=True,
                                                 error_budget=error_budget)
                    task_data.setup_scheduler(time)
                else:
                    start_batch_process(mu, task_name, self.log, False, error_budget=error_budget)

                set_action_output(output, True, "Task resumed successfully.")
            else:
                set_action_output(output, False, "Task is not in suspended state.")
        except Exception as exp:
            self.log.exception(f"{task_name} - Error while resuming the task: {exp}")
            set_action_output(output, False, f"Task resume failed {str(exp)}")


class PDPTaskPauseHandler(Action):
    """
    Action handler for pausing PDP Tasks
    """
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        task_name = get_kp_service_id(kp)
        try:
            validate_ha_mode()
            mu = MaapiUtil()
            if can_task_be_paused(task_name, mu):
                task_data = TaskData(task_name, self.log, mu)
                task_data.load(load_task=False)
                msg = "Task is {}paused by user."
                if task_data.schedule != pdp_ns.cisco_pdp_immediately_:
                    task_data.suspend_scheduler()
                    msg = f"{msg[:-1]} to suspend future schedule."
                status, reason = task_data.is_task_suspended(True)
                if status:
                    msg = f"{reason} {msg.format('also ')}"
                else:
                    msg = msg.format('')
                task_data.update_task_status(pdp_ns.cisco_pdp_pdp_suspended_, msg)
                self.log.info(f"{task_name} - Task is paused.")
                set_action_output(output, True, "Task paused successfully.")
            else:
                set_action_output(output, False, "Task is already paused or in completed state.")
        except Exception as exp:
            self.log.exception(f"{task_name} - Error while pausing the task: {exp}")
            set_action_output(output, False, f"Task pause failed {str(exp)}")


class PDPTaskRetryHandler(Action):
    """
    Action handler for retrying failed nodes of PDP Tasks
    """
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        task_name = get_kp_service_id(kp)
        try:
            validate_ha_mode()
            mu = MaapiUtil()
            with mu.trans_read_oper() as th:
                if th.exists(Constants.STATUS_PATH.format(task_name)):
                    task_status = maagic.get_node(th, Constants.STATUS_PATH.format(task_name))
                    if input.failed_nodes:
                        failed_nodes = input.failed_nodes.as_list()
                    else:
                        failed_nodes = [str(node.name) for node in task_status.failed_nodes]
                    self.log.info(f"{task_name} - failed-nodes to be retried: {failed_nodes}")

                    if len(failed_nodes) > 0:
                        task_map = TaskMap.getInstance()
                        if task_map.key_exists_in_map(task_name):
                            queue = task_map.get(task_name)
                        else:
                            queue = WorkItemDeque()
                            task_map.put(task_name, queue)

                        for item in failed_nodes:
                            work_item = WorkItem(task_name, item)
                            # for suspended task enque at the end of queue
                            if task_status.state in [pdp_ns.cisco_pdp_pdp_suspended_,
                                                     pdp_ns.cisco_pdp_completed_]:
                                queue.enqueue(work_item, mu)
                            else:
                                queue.enqueue_first(work_item, mu)

                        # completed task is marked suspended to be resumed by user
                        if pdp_ns.cisco_pdp_completed_ == task_status.state:
                            TaskData(task_name, self.log, mu).update_task_status(
                                pdp_ns.cisco_pdp_pdp_suspended_,
                                reason=("Failed nodes are moved back to pending."
                                        " Resume task to retry."))
                        set_action_output(output, True,
                                          "Task failed nodes added successfully to pending.")
                    else:
                        set_action_output(output, False,
                                          "Task has no failed nodes to be added to pending.")
                else:
                    set_action_output(output, False, "Task has not run yet.")
        except Exception as exp:
            self.log.exception(f"{task_name} - Error while re-trying the failures: {exp}")
            set_action_output(output, False, f"Task retry failed {str(exp)}")
