# -*- mode: python; python-indent: 4 -*-
from threading import Lock
from ncs import maagic
from ncs.template import Template, Variables
from phased_provisioning.namespaces.ciscoPhasedProvisioning_ns import ns as pdp_ns
from phased_provisioning.constants import Constants
from phased_provisioning.util import get_current_time, get_window_time, validate_window
from phased_provisioning.ha_util import is_ha_primary, validate_ha_mode
from phased_provisioning.maapi_util import MaapiUtil


class PDPException(Exception):
    pass


class PDPActionException(PDPException):
    pass


class PDPTestException(PDPException):
    pass


class TaskData:
    """The data store for phased provisioning task.
    Provides APIs for task-status oper data and tailf:scheduler.
    """
    def __init__(self, name, log, mu: MaapiUtil = None) -> None:
        self.name = name
        self.log = log
        self.lock = Lock()
        self.mu = MaapiUtil() if mu is None else mu
        self.log_prefix = f"{self.name} -"

    def load(self, load_task: bool = True, load_policy: bool = True) -> None:
        """Load the task data into memory and initialize the running error budget.
        """
        with self.mu.trans_read_running() as th:
            task = maagic.get_node(th, Constants.TASK_PATH.format(self.name))
            if load_task:
                self._load_task(task)
            if load_policy:
                self._load_policy(th, task.policy)

    def _load_task(self, task_node):
        self.action_name = task_node.action.action_name
        if task_node.action.variable is not None:
            self.variable = self._cache_variables(task_node.action.variable)
        self.test_type = None
        if task_node.self_test.action_name:
            self.test_type = pdp_ns.cisco_pdp_action_name_
            self.self_test_action = task_node.self_test.action_name
        elif task_node.self_test.test_expr:
            self.test_type = pdp_ns.cisco_pdp_test_expr_
            self.test_expr = task_node.self_test.test_expr
        if task_node.self_test.variable is not None:
            self.self_test_variable = self._cache_variables(task_node.self_test.variable)

    def _load_policy(self, th, policy_name):
        self.policy = policy_name
        policy = maagic.get_node(th, Constants.POLICY_PATH.format(self.policy))
        self.schedule = pdp_ns.cisco_pdp_immediately_
        self.time = None
        self.window_time = None
        self.window_time_unit = None
        if str(policy.schedule.schedule_type) == pdp_ns.cisco_pdp_schedule_when_:
            self.schedule = pdp_ns.cisco_pdp_future_
            self.time = policy.schedule.future.time
            self.window_time_unit = policy.schedule.future.window.unit
            self.window_time = policy.schedule.future.window.window_time
        self.batch = policy.batch.size
        self.error_budget = policy.error_budget
        self.error_budget_left = self.error_budget
        # Read and set error budget
        task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
        if task_status.current_error_budget:
            self.error_budget_left = task_status.current_error_budget
        self.avg_exec_time = float(task_status.avg_node_exec_time)

    def process_policy_updates(self):
        stop_next_batch = False
        is_reset_window = False
        with self.mu.trans_read_running() as th:
            task = maagic.get_node(th, Constants.TASK_PATH.format(self.name))
            policy = maagic.get_node(th, Constants.POLICY_PATH.format(task.policy))
            schedule = pdp_ns.cisco_pdp_immediately_
            if str(policy.schedule.schedule_type) == pdp_ns.cisco_pdp_schedule_when_:
                schedule = pdp_ns.cisco_pdp_future_
                time = policy.schedule.future.time
                window_time_unit = policy.schedule.future.window.unit
                window_time = policy.schedule.future.window.window_time
                if window_time != self.window_time or window_time_unit != self.window_time_unit:
                    self.window_time = window_time
                    self.window_time_unit = window_time_unit
                    is_reset_window = True
                if self._process_schedule_time(time) != self._process_schedule_time(self.time):
                    stop_next_batch = True
            elif self.schedule == pdp_ns.cisco_pdp_future_:
                self.window_time = None
                self.window_time_unit = None
                is_reset_window = True
            self.schedule = schedule
            self.batch = policy.batch.size
            self.error_budget = policy.error_budget

        return stop_next_batch, is_reset_window

    def _cache_variables(self, args: dict) -> dict:
        """Cache variable dictionary and record if user configured value
            or expr for variables
            Args: a dictionary, either it is task variable or
            task action variable
        """
        res_d = {}
        if args:
            for var in args:
                value = None
                if var.value:
                    value = (Constants.VAR_VAL, var.value)
                elif var.expr:
                    value = (Constants.VAR_EXPR, var.expr)
                res_d[var.name] = value
        return res_d

    def _process_schedule_time(self, time):
        """process schedule time, because
        * * * * * == */1 */1 */1 */1 */1
        """
        if time:
            processed_time = ""
            for comp in time.split(" "):
                if comp.endswith("/1"):
                    comp = comp[:-2]
                processed_time += comp + " "
            return processed_time[:-1]
        return time

    def is_overdraft(self) -> bool:
        """Check if we have over consumed the error budget.
        """
        return self.error_budget != pdp_ns.cisco_pdp_ignore_failures_ and \
            self.error_budget >= 0 and self.error_budget_left < 0

    def create_task_status(self, error_budget):
        with self.mu.trans_write_oper() as th:
            task_status = th.safe_create(Constants.STATUS_PATH.format(self.name))
            root = maagic.get_root(th)
            task_status_list = root.phased_provisioning.task_status
            if self.name in task_status_list:
                task_status = task_status_list[self.name]
            else:
                task_status = task_status_list.create(self.name)
            if task_status.state is None or task_status.state == pdp_ns.cisco_pdp_completed_:
                task_status.state = pdp_ns.cisco_pdp_pdp_init_
            if task_status.state != pdp_ns.cisco_pdp_pdp_in_progress_:
                task_status.allocated_error_budget = error_budget
                task_status.current_error_budget = error_budget
            th.apply()

    def update_task_status(self, status: str, reason: str = None, clear_reason: bool = False,
                           last_run: bool = False, next_run: bool = False,
                           error_budget: str = None) -> None:
        """Update the task level status, reason, last_runtime or clear reason.
        """
        with self.mu.trans_write_oper() as th:
            task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
            task_status.state = status
            log_str = f"{self.log_prefix} Updating task status: {status}"
            if clear_reason:
                del task_status.reason
                log_str += ", clearing reason"
            elif reason:
                task_status.reason = reason
                log_str += f", reason: {reason}"
            if last_run:
                runtime = str(get_current_time().isoformat())
                task_status.last_runtime = runtime
                log_str += f", last-runtime: {runtime}"
            if next_run:
                runtime = self.update_next_schedule(th)
                task_status.next_runtime = runtime
                log_str += f", next-runtime: {runtime}"
            if error_budget is not None:
                task_status.allocated_error_budget = error_budget
                task_status.current_error_budget = error_budget
                log_str += f", error-budget: {error_budget}"
            self.log.info(log_str)
            th.apply()

    def adjust_error_budget(self, new_eb):
        with self.mu.trans_write_oper() as th:
            task_data = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
            old_eb = task_data.allocated_error_budget
            self.log.debug(f"{self.log_prefix} Task error-budget updated {old_eb} -> {new_eb}")
            eb_left = new_eb
            if pdp_ns.cisco_pdp_ignore_failures_ not in [old_eb, new_eb]:
                eb_left = (task_data.current_error_budget + (int(new_eb) - int(old_eb)))
            task_data.current_error_budget = eb_left
            task_data.allocated_error_budget = new_eb
            th.apply()

    def is_task_suspended(self, return_reason=False):
        status = False
        reason = None
        with self.mu.trans_read_oper() as th:
            if th.exists(status_path := Constants.STATUS_PATH.format(self.name)):
                task_status = maagic.get_node(th, status_path)
                if task_status.state == pdp_ns.cisco_pdp_pdp_suspended_:
                    status = True
                    if return_reason:
                        reason = task_status.reason
                        self.log.debug(f"{self.log_prefix} Task paused because, {reason}")
        if return_reason:
            return status, reason
        return status

    def set_status_completed(self) -> None:
        with self.mu.trans_write_running() as th:
            task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
            if self.schedule != pdp_ns.cisco_pdp_immediately_:
                scheduler = maagic.get_node(th, Constants.SCHEDULER_PATH.format(self.name))
                scheduler.enabled = False
                del task_status.next_runtime
            task_status.state = pdp_ns.cisco_pdp_pdp_completed_
            task_status.reason = Constants.COMPLETED_MSG
            th.apply()
        self.log.info("=======================PHASED PROVISIONING COMPLETED=======================")

    def is_nso_crashed(self) -> bool:
        status = False
        with self.mu.trans_read_oper() as th:
            if th.exists(status_path := Constants.STATUS_PATH.format(self.name)):
                task_status = maagic.get_node(th, status_path)
                for failed_node in task_status.failed_nodes:
                    if Constants.NSO_CRASH_ERR_MSG in failed_node.failure_reason:
                        status = True
                        break
        return status

    # ========Scheduler APIs==========

    def setup_scheduler(self, time: str, only_validate: bool = False):
        """Create/Validate tailf:scheduler for a pdp task.
        """
        schedule_task_id = Constants.SCHEDULER_PREFIX.format(self.name)
        data = Variables()
        data.add('SCHEDULER_TASK', schedule_task_id)
        data.add('TIME', time)
        data.add('TASK', self.name)
        data.add('USER', self.mu.l_user)
        data.add('IS_HA_PRIMARY', str(is_ha_primary()))
        with self.mu.trans_write_running() as th:
            root = maagic.get_root(th)
            template = Template(root.phased_provisioning)
            template.apply('cisco-pdp-scheduler', data)
            if only_validate:
                self.log.debug(f"{self.log_prefix} Validating scheduler.")
                th.validate(False, True)
            else:
                self.log.debug(f"{self.log_prefix} Creating/Updating scheduler"
                               f" {schedule_task_id} for the task.")
                task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
                task_status.schedule_task_id = schedule_task_id
                th.apply()
                self.update_next_schedule()

    def update_next_schedule(self, th=None):
        """Get the tailf:scheduler associated with the pdp task next schedule time.
        """
        if th:
            action = maagic.get_node(th, Constants.FUTURE_SCHEDULE.format(self.name))
            outparams = action()
            if outparams.next_run_time:
                return str(outparams.next_run_time.as_list()[0])
            return None
        else:
            with self.mu.trans_write_running() as th:
                action = maagic.get_node(th, Constants.FUTURE_SCHEDULE.format(self.name))
                outparams = action()
                if outparams.next_run_time:
                    task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
                    runtime = str(outparams.next_run_time.as_list()[0])
                    task_status.next_runtime = runtime
                    self.log.debug(f"{self.log_prefix} Task next-runtime: {runtime}")
                th.apply()

    def has_future_schedule(self):
        """Checks if the tailf:scheduler associated with the pdp task has future schedules.
        """
        with self.mu.trans_read_running() as th:
            action = maagic.get_node(th, Constants.FUTURE_SCHEDULE.format(self.name))
            outparams = action()
            if outparams.next_run_time:
                return True
            return False

    def suspend_scheduler(self):
        """Suspend the tailf:scheduler associated with the pdp task.
        """
        self.log.debug(f"{self.log_prefix} Disabling scheduler for the task.")
        with self.mu.trans_write_running() as th:
            scheduler = maagic.get_node(th, Constants.SCHEDULER_PATH.format(self.name))
            scheduler.enabled = False
            task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
            del task_status.next_runtime
            th.apply()

    def delete_scheduler(self):
        """Delete the tailf:scheduler associated with the pdp task.
        """
        self.log.debug(f"{self.log_prefix} Deleting scheduler for the task.")
        with self.mu.trans_write_running() as th:
            th.safe_delete(Constants.SCHEDULER_PATH.format(self.name))
            task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.name))
            del task_status.next_runtime
            del task_status.schedule_task_id
            th.apply()


class TaskProcessData:
    def __init__(self, task_data: TaskData, log, window):
        self.log = log
        task_data.load()
        self.task_data = task_data
        self.start_time = get_current_time()
        if window:
            self.window = window
        else:
            self.reset_window()

    def reset_window(self, current_time=None):
        if window_time := self.task_data.window_time:
            window_time_unit = self.task_data.window_time_unit
            if current_time:
                self.window = get_window_time(current_time, window_time, window_time_unit)
            else:
                self.window = get_window_time(self.start_time, window_time, window_time_unit)
        else:
            self.window = None
        self.log.info(f"{self.task_data.log_prefix} new window after reset: {self.window}")

    def is_break_batch_process(self):
        return (self._is_invalid_ha_mode() or self._is_window_depleted()
                or self._is_error_budget_depleted() or self.task_data.is_task_suspended()
                or self.task_data.is_nso_crashed())

    def _is_window_depleted(self):
        is_depleted = False
        if self.window:
            state = pdp_ns.cisco_pdp_pdp_in_progress_
            is_depleted, reason = validate_window(
                self.task_data.avg_exec_time, self.window, self.task_data.name, self.log)
            if is_depleted:
                # So window has depleted and queue is not empty()
                # test: tailf:schedule has future schedules.
                # If Yes: then task stays in progress
                # If No: then task is marked suspended
                if not self.task_data.has_future_schedule():
                    state = pdp_ns.cisco_pdp_pdp_suspended_
                    reason = Constants.PENDING_ERROR_MSG
                self.task_data.update_task_status(state, reason)
        return is_depleted

    def _is_error_budget_depleted(self):
        is_depleted = False
        if self.task_data.is_overdraft():
            self.log.info("Suspending task, because current error budget has been depleted out of "
                          f"allocated error budget {self.task_data.error_budget}.")
            self.task_data.update_task_status(pdp_ns.cisco_pdp_pdp_suspended_,
                                              Constants.EXCEEDED_ERROR_MSG)
            if self.task_data.schedule != pdp_ns.cisco_pdp_immediately_:
                self.task_data.suspend_scheduler()
            is_depleted = True
        return is_depleted

    def _is_invalid_ha_mode(self):
        try:
            validate_ha_mode()
            return False
        except Exception as ex:
            self.task_data.update_task_status(
                pdp_ns.cisco_pdp_pdp_in_progress_,
                f"batch process stopped because, {str(ex)}")
            self.log.exception("Stopping batch process because, ", ex)
            return True
