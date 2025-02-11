# -*- mode: python; python-indent: 4 -*-
import ncs
import _ncs
from .util import get_kp_service_id
from ncs.template import Template
from phased_provisioning.namespaces.ciscoPhasedProvisioning_ns import ns as pdp_ns
from phased_provisioning.pdp_task_data import TaskData


class ValidationPreMod(ncs.application.Service):
    @ncs.application.Service.pre_modification
    def cb_pre_modification(self, tctx, op, kp, root, proplist):

        # This pre-mod is to check for any change in list task and stop commit
        # if task is if progress. Only allow task delete when
        # task-status IS NOT in progress. No update in existing task
        # config. to change task create new

        self.log.info("------ValidationPreMod-----", 'op ', op, 'kp ', str(kp))
        task_name = get_kp_service_id(kp)

        # Create kickers to listen for changes on config task and policy and
        # call action task-change to update task-status oper data.

        template = Template(root.phased_provisioning)
        template.apply('pdp-bootstrap-kickers', None)
        oper_path = root.phased_provisioning.task_status
        if task_name in oper_path:
            task_status = oper_path[task_name].state
            if (task_status == pdp_ns.cisco_pdp_pdp_in_progress_):
                raise Exception("Task is in-progress.: " + task_name)
            elif (op == _ncs.dp.NCS_SERVICE_UPDATE):
                raise Exception("Cannot update Task config: " + task_name)

    def cb_create(self, tctx, root, service, proplist):
        return


class ValidateSchedule(ncs.dp.ValidationPoint):
    @ncs.dp.ValidationPoint.validate
    def cb_validate(self, tctx, keypath, value, validationpoint):
        self.log.info(f"Validating {keypath}: {value}")
        try:
            TaskData('dummy', self.log).setup_scheduler(str(value), True)
        except Exception as e:
            err_str = str(e)
            err_str = err_str[(err_str.rindex(':') + 1):]
            self.log.exception(f"Validation error for {keypath}: {err_str}")
            raise ncs.dp.ValidationError(err_str)
        return ncs.CONFD_OK
