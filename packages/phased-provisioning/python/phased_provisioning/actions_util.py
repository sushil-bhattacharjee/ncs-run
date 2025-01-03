# -*- mode: python; python-indent: 4 -*-
import socket
from datetime import datetime
from threading import Thread, current_thread
import ncs
from _ncs import events
from ncs import maagic
from phased_provisioning.pdp_task_map import TaskMap
from phased_provisioning.batch_process import batch_process
from phased_provisioning.namespaces.ciscoPhasedProvisioning_ns import ns as pdp_ns
from phased_provisioning.constants import Constants
from phased_provisioning.util import get_current_time, get_window_time, validate_window
from phased_provisioning.ha_util import (is_ha, is_ha_switchover_happened,
                                         is_reattempt_psp_tasks_in_ha)
from phased_provisioning.maapi_util import MaapiUtil
from phased_provisioning.pdp_task_data import TaskData, TaskProcessData


class ProcessPolicyUpdate:
    def __init__(self, log):
        self.log = log
        self.changes = {}

    def iterate(self, kp, op, oldv, newv):
        if len(kp) == 4 and op in [ncs.MOP_CREATED, ncs.MOP_DELETED]:
            return ncs.ITER_STOP
        else:
            if op == ncs.MOP_DELETED:
                if str(kp).endswith(f"{pdp_ns.cisco_pdp_future_}/{pdp_ns.cisco_pdp_time_}"):
                    self.changes['schedule-immediate'] = None
            elif op == ncs.MOP_VALUE_SET:
                if str(kp).endswith(pdp_ns.cisco_pdp_error_budget_):
                    new_eb = pdp_ns.cisco_pdp_ignore_failures_ if newv == "enum<0>" else newv
                    self.changes['error-budget'] = new_eb
                elif str(kp).endswith(f"{pdp_ns.cisco_pdp_future_}/{pdp_ns.cisco_pdp_time_}"):
                    self.changes['schedule-time'] = newv
                else:
                    self.changes['other-updates'] = newv
            return ncs.ITER_RECURSE


def start_batch_process(mu: MaapiUtil, task_name: str, log: ncs.log.Log, reset_window: bool = True,
                        window: datetime = None, next_run: bool = False, error_budget: str = None):
    queue = TaskMap.getInstance().get(task_name)
    if queue is None:
        # if TaskMap is not recreated before for any failure
        TaskMap.getInstance().recreate(mu)
        queue = TaskMap.getInstance().get(task_name)
    tp_data: TaskProcessData = queue.process
    if tp_data is None:
        log.info(f"{task_name} - #####Initiating new batch processing for the task.#####")
        task_data = TaskData(task_name, log, mu)
        task_data.update_task_status(
            pdp_ns.cisco_pdp_pdp_in_progress_, clear_reason=True, last_run=True,
            next_run=next_run, error_budget=error_budget)
        tp_data = TaskProcessData(task_data, log, window)
        batch_processor = Thread(target=batch_process,
                                 args=(tp_data, queue, log, mu),
                                 name=task_name)
        batch_processor.start()
    else:
        log.info(f"{task_name} - #####Batch process is running for the task.#####")
        if reset_window:
            tp_data.reset_window(get_current_time())


def can_task_be_paused(task_name: str, mu: MaapiUtil):
    with mu.trans_read_oper() as th:
        task_status = maagic.get_node(th, Constants.STATUS_PATH.format(task_name))
        return not (task_status.state == pdp_ns.cisco_pdp_pdp_completed_
                    or task_status.state == pdp_ns.cisco_pdp_pdp_suspended_)


def task_cleanup(task_name: str):
    with MaapiUtil().trans_write_running() as t:
        root = maagic.get_root(t)
        oper_path = root.phased_provisioning.task_status
        if task_name in oper_path:
            del oper_path[task_name]
        # delete scheduler for the task
        scheduler_id = Constants.SCHEDULER_PREFIX.format(task_name)
        if scheduler_id in root.scheduler.task:
            del root.scheduler.task[scheduler_id]
        # delete id from TaskMap
        TaskMap.getInstance().delete(task_name)
        t.apply()


def eligible_to_execute(mu: MaapiUtil, task_name: str, is_pkg_load_ha: bool, log: ncs.log.Log):
    is_eligible = True
    window = None
    with mu.trans_read_running() as rth:
        root = maagic.get_root(rth)
        task_status = root.phased_provisioning.task_status[task_name]
        task = root.phased_provisioning.task[task_name]
        if (task_status.state == pdp_ns.cisco_pdp_pdp_in_progress_
                and (task_status.reason is None
                     or Constants.HA_MODE_ERR_MSG in task_status.reason)):
            if Constants.SCHEDULER_PREFIX.format(task_name) in root.scheduler.task:
                last_run = datetime.strptime(task_status.last_runtime, '%Y-%m-%dT%H:%M:%S.%f%z')
                log.debug(f"Task {task_name } last run time: {last_run}")
                policy = root.phased_provisioning.policies.policy[task.policy]
                window_time = policy.schedule.future.window.window_time
                window_time_unit = policy.schedule.future.window.unit
                window = get_window_time(last_run, window_time, window_time_unit)
                log.debug(f"Task {task_name} window time: {window}")
                if task_status.pending_nodes:
                    is_depleted, reason = validate_window(
                        float(task_status.avg_node_exec_time), window, task_name, log)
                    if is_depleted:
                        TaskData(task_name, log, mu).update_task_status(
                            pdp_ns.cisco_pdp_pdp_in_progress_, reason)
                        is_eligible = False
            if (is_ha() and not is_pkg_load_ha and is_eligible
                    and task_status.reason is None and not is_ha_switchover_happened()):
                # Do not re-execute task if in a HA setup, no package reload and
                # task is in-progress and no ha switch over happened
                is_eligible = False
        else:
            is_eligible = False
    log.info(f"Task: {task_name} eligibility to re-execute now: {is_eligible}")
    return is_eligible, window


def set_action_output(output: maagic.ActionParams, result: bool, info: str):
    output.result = result
    output.info = info


def re_trigger_batch_process(mu: MaapiUtil, log: ncs.log.Log):
    tm_instance: TaskMap = TaskMap.getInstance()
    is_pkg_load_ha = tm_instance.is_empty()
    log.info(f"is package reload happended: {is_pkg_load_ha}")
    tm_instance.recreate(mu)
    log.debug(str(tm_instance))
    for task in tm_instance.map:
        is_eligible, window = eligible_to_execute(mu, task, is_pkg_load_ha, log)
        if is_eligible:
            start_batch_process(mu, task, log, False, window)


def ha_events_handler(sock: socket, mu: MaapiUtil, log: ncs.log.Log):
    try:
        log.info(f"{current_thread().name} Started...")
        events.notifications_connect(sock, events.NOTIF_HA_INFO, ncs.ADDR, ncs.PORT)
        re_attempt = False

        while True:
            ha_notifs = []
            try:
                ha_notifs = events.read_notification(sock)
            except Exception:
                log.info(f"{current_thread().name} Stopped.")
                break
            re_attempt = is_reattempt_psp_tasks_in_ha(mu, ha_notifs, re_attempt, log)
            if re_attempt:
                re_trigger_batch_process(mu, log)
    except Exception as ex:
        log.exception("Exception while handling HA events: ", ex)
