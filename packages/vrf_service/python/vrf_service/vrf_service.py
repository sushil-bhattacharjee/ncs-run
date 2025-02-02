# -*- mode: python; python-indent: 4 -*-
# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

VRF_NAMES = ["vrf_name_pythonA", "vrf_name_pythonB", "vrf_name_pythonC", "vrf_name_pythonD", "vrf_name_pythonE"]

class VrfServiceCallbacks(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'Creating VRF service: {service.name}')

        # Check if VRF_NAMES is empty
        if not VRF_NAMES:
            self.log.error("VRF_NAMES list is empty! Cannot assign a VRF name.")
            raise ValueError("No available VRF names in the list.")

        vrf_name = VRF_NAMES.pop(0)
        self.log.info(f'Assigned VRF Name: {vrf_name}')

        vars = ncs.template.Variables()
        vars.add('vrf_name', vrf_name)
        vars.add('device', service.device)
        vars.add('vrf_rd', service.vrf_rd)
        vars.add('vrf_export_asn', service.vrf_export_asn)
        vars.add('vrf_import_asn', service.vrf_import_asn)

        template = ncs.template.Template(service)
        template.apply('vrf_service-template', vars)

        self.log.info(f'VRF {vrf_name} applied to device {service.device}')

class VrfService(ncs.application.Application):
    def setup(self):
        self.log.info('VRF Service RUNNING')
        self.register_service("vrf_service-servicepoint", VrfServiceCallbacks)

    def teardown(self):
        self.log.info('VRF Service STOPPED')
