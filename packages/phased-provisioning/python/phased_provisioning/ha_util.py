# -*- mode: python; python-indent: 4 -*-
import ncs
from _ncs import events
from phased_provisioning.constants import Constants, HAEvents
from phased_provisioning.maapi_util import MaapiUtil

# Global variable to check current ha mode
current_ha_mode = None

# Global variable to check previous ha mode
previous_ha_mode = None

# Global variable to check if ha switch over happened
is_ha_switch_over = False


def is_ha_primary():
    if current_ha_mode == "primary":
        return True
    return False


def is_ha_enabled(mu: MaapiUtil):
    is_ha = False
    with mu.trans_read_oper() as th:
        is_ha = th.exists(Constants.HA_PATH)
        if not is_ha:
            global current_ha_mode
            current_ha_mode = None
    return is_ha


def is_ha():
    if current_ha_mode:
        return True
    return False


def update_ha_mode(mu: MaapiUtil, log: ncs.log.Log):
    global current_ha_mode
    global previous_ha_mode
    global is_ha_switch_over
    with mu.trans_read_oper() as th:
        mode = str(ncs.maagic.get_node(th, Constants.HA_MODE_PATH))
        log.info(f"NSO instance current HA mode is {mode}")
        if mode != current_ha_mode:
            if previous_ha_mode:
                if mode != previous_ha_mode:
                    log.info(f"HA switch over happened {previous_ha_mode} -> {mode}")
                    is_ha_switch_over = True
                else:
                    log.info("No HA switch over happened.")
                    is_ha_switch_over = False
            previous_ha_mode = current_ha_mode
            current_ha_mode = mode
        else:
            is_ha_switch_over = False
        log.info(f"NSO instance previous HA mode is {previous_ha_mode}")


def is_ha_switchover_happened():
    return is_ha_switch_over


def validate_ha_mode():
    if current_ha_mode is not None and is_ha_primary() is False:
        raise Exception(Constants.HA_MODE_ERR_MSG)


def is_secondary_connected(mu: MaapiUtil):
    with mu.trans_read_oper() as th:
        ha_node = ncs.maagic.get_node(th, Constants.HA_PATH)
        if ha_node.connected_secondary:
            return True
    return False


def is_reattempt_psp_tasks_in_ha(mu: MaapiUtil, ha_notifs, re_attempt: bool, log: ncs.log.Log):
    if 'hnot' in ha_notifs and 'type' in ha_notifs['hnot']:
        ha_event = ha_notifs['hnot']['type']
        log.info(f"HA event received: {HAEvents.get_event(ha_event)}")
        if ((ha_event == events.HA_INFO_IS_PRIMARY and is_secondary_connected(mu))
                or (ha_event == events.HA_INFO_SECONDARY_ARRIVED and not re_attempt)):
            update_ha_mode(mu, log)
            re_attempt = True
        elif ha_event in [events.HA_INFO_SECONDARY_INITIALIZED, events.HA_INFO_IS_NONE]:
            update_ha_mode(mu, log)
            re_attempt = False
    log.info(f"re-attempt phased-provisioning tasks in HA: {re_attempt}")
    return re_attempt
