# -*- mode: python; python-indent: 4 -*-
from enum import Enum


class Constants:
    '''
        Class containing constants
    '''
    # Maapi Constants
    MAAPI_PRODUCT = "phased-provisioning"
    MAAPI_VENDOR = "Cisco"
    MAAPI_CTX = "system"

    # HA constants
    HA_PATH = "/tfnm:ncs-state/tfnm2:ha"
    HA_MODE_PATH = HA_PATH + "/mode"
    HA_MODE_ERR_MSG = "HA mode is not primary."

    # PSP constants
    SCHEDULER_PREFIX = 'cisco-pdp-{}'
    POLICY_PATH = "/cisco-pdp:phased-provisioning/policies/policy{{{}}}"
    STATUS_PATH = "/cisco-pdp:phased-provisioning/task-status{{{}}}"
    SCHEDULER_PATH = "/scheduler:scheduler/task{{cisco-pdp-{}}}"
    TASK_PATH = "/cisco-pdp:phased-provisioning/task{{{}}}"
    FUTURE_SCHEDULE = SCHEDULER_PATH + "/get-next-run-times"
    LOCAL_USR_PATH = "/cisco-pdp:phased-provisioning/local-user"
    EXCEEDED_ERROR_MSG = "Task has exceeded the maximum number of errors allowed."
    EXCEEDED_WINDOW_MSG = "batch process stopped, because window time exceeded."
    PENDING_ERROR_MSG = "Task's pending requests couldn't be processed within scheduled time."
    COMPLETED_MSG = "All scheduled requests are processed."
    INSUFFICIENT_WINDOW_LOG = ("{} batch process stopped, because remaining window time"
                               " {} seconds is not sufficient for average batch "
                               "execution time {} seconds.")
    INSUFFICIENT_WINDOW_MSG = "batch process stopped, because in-sufficient window time."
    POLICY_UPDATE_QUERY = ("/phased-provisioning/task[policy='{}']"
                           "[name=/phased-provisioning/task-status"
                           "[(state='in-progress') or (state='init')]/name]")
    NSO_CRASH_ERR_MSG = "application communication failure"

    VAR_VAL = "value"
    VAR_EXPR = "expr"
    ACTION = "ACTION"
    SELF_TEST = "SELF-TEST"


class TimeUnit(Enum):
    """
    This enum class represents the window time unit.
    """
    HOURS = 'hours'
    MINUTES = 'minutes'
    SECONDS = 'seconds'
    DAYS = 'days'


class HAEvents(Enum):
    """
    This enum class represents HA events supported in NSO
    """
    HA_INFO_NOPRIMARY = 1
    HA_INFO_SECONDARY_DIED = 2
    HA_INFO_SECONDARY_ARRIVED = 3
    HA_INFO_SECONDARY_INITIALIZED = 4
    HA_INFO_IS_PRIMARY = 5
    HA_INFO_IS_NONE = 6
    HA_INFO_BESECONDARY_RESULT = 7

    @classmethod
    def get_event(cls, index: int):
        try:
            return cls(index).name
        except Exception:
            return "UNKNOWN_HA_EVENT"
