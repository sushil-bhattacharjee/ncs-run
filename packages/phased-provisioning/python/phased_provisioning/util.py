# -*- mode: python; python-indent: 4 -*-
from math import floor
from datetime import timedelta, datetime
from phased_provisioning.constants import Constants, TimeUnit


def get_kp_service_id(kp):
    kpath = str(kp)
    service = kpath[kpath.find("{") + 1: len(kpath) - 1]
    return service


def get_current_time(tz=True):
    if tz:
        return datetime.now().astimezone()
    else:
        return datetime.now()


def get_window_time(current_time, window_time, window_time_unit):
    delta_arg = TimeUnit(window_time_unit).value
    return current_time + timedelta(**{delta_arg: window_time})


def validate_window(avg_exec_time: float, window_time: datetime, task_name: str, log):
    is_depleted = False
    reason = None
    if get_current_time() > window_time:
        is_depleted = True
        reason = Constants.EXCEEDED_WINDOW_MSG
        log.debug(f"{task_name} - {Constants.EXCEEDED_WINDOW_MSG}")
    # ignoring fraction of seconds to utilize remaining window time fully.
    elif floor(avg_exec_time - (
            rem_exec_time := (window_time - get_current_time()).total_seconds())) > 0:
        is_depleted = True
        reason = Constants.INSUFFICIENT_WINDOW_MSG
        log.debug(
            f"{task_name} - "
            f"{Constants.INSUFFICIENT_WINDOW_LOG.format(task_name, rem_exec_time, avg_exec_time)}")
    return is_depleted, reason
