# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'Service create(service={service._path})')

        for device_entry in service.devices:
            device = device_entry.name
            router_id = device_entry.router_id

            vars = ncs.template.Variables()
            vars.add("DEVICE", device)
            vars.add("ROUTER_ID", router_id)

            # Apply MPLS LDP router ID and interface configurations
            template = ncs.template.Template(service)

            # First, apply the router ID
            template.apply("core_mpls-template-router-id", vars)

            # Iterate through interfaces and apply configurations
            for intf_entry in device_entry.interfaces:
                vars.add("INTERFACE", intf_entry.name)
                template.apply("core_mpls-template-interface", vars)

            self.log.info(f"Configured MPLS LDP for {device} with router ID {router_id}")

class CoreMpls(ncs.application.Application):
    def setup(self):
        self.log.info("Core MPLS Service RUNNING")
        self.register_service("core_mpls-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("Core MPLS Service STOPPED")
