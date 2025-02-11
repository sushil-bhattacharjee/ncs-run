# -*- mode: python; python-indent: 4 -*-
import socket
from threading import Thread
import ncs
from phased_provisioning.pdp_actions import (PDPTaskRunHandler, PDPTaskExecutionHandler,
                                             PDPTaskPauseHandler, PDPTaskResumeHandler,
                                             PDPTaskRetryHandler, PDPTaskStatusPurgeHandler,
                                             PDPTaskChangeHandler, PolicyChangeHandler,
                                             PDPBriefExecutionHandler)
from phased_provisioning.pdp_validation import ValidationPreMod, ValidateSchedule
from phased_provisioning.pdp_task_map import TaskMap
from phased_provisioning.actions_util import re_trigger_batch_process, ha_events_handler
from phased_provisioning.maapi_util import MaapiUtil
from phased_provisioning.ha_util import is_ha_enabled, update_ha_mode, is_ha_primary


# ---------------------------------------------
# COMPONENT THREAD THAT WILL BE STARTED BY NCS.
# ---------------------------------------------
class Main(ncs.application.Application):
    def setup(self):
        self.log.info('Main RUNNING')
        self.register_action('pdp-task-run', PDPTaskRunHandler)
        self.register_action('pdp-task-pause', PDPTaskPauseHandler)
        self.register_action('pdp-task-resume', PDPTaskResumeHandler)
        self.register_action('pdp-task-retry', PDPTaskRetryHandler)
        self.register_action('phased-provisioning-execution-handler',
                             PDPTaskExecutionHandler)
        self.register_action('pdp-task-status-purge', PDPTaskStatusPurgeHandler)
        self.register_action("pdp-task-change", PDPTaskChangeHandler)
        self.register_action("pdp-policy-change", PolicyChangeHandler)
        self.register_action('pdp-task-status-short', PDPBriefExecutionHandler)
        self.register_service('pdp-task-validation', ValidationPreMod)
        self.register_validation('pdp-schedule-validate', ValidateSchedule)

        mu = MaapiUtil()
        if is_ha_enabled(mu):
            self.log.info("NSO high-availability setup detected.")
            # If ha enabled then handle ha event notifications
            self.sock = socket.socket()
            self.ha_event_handler = Thread(target=ha_events_handler, name='HAEventsHandlerThread',
                                           args=(self.sock, mu, self.log))
            self.ha_event_handler.start()
            # this is required in case of package reload in an existing HA setup
            update_ha_mode(mu, self.log)
            if is_ha_primary():
                re_trigger_batch_process(mu, self.log)
        else:
            re_trigger_batch_process(mu, self.log)

    def teardown(self):
        TaskMap.getInstance().abort()
        if hasattr(self, "sock"):
            self.sock.close()
            self.ha_event_handler.join()
        self.log.info('Main FINISHED')
