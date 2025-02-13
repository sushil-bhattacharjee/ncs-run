# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'Service create(service={service._path})')

        # Iterate through devices and configure multiple interfaces per device
        for device_entry in service.devices:
            device = device_entry.name  # Extract device name

            for interface_entry in device_entry.interfaces:
                interface = interface_entry.interface  # Extract interface
                ip_address = interface_entry.ip_address  # Extract IP address

                # Apply service template
                vars = ncs.template.Variables()
                vars.add("DEVICE", device)
                vars.add("INTERFACE", interface)
                vars.add("IP_ADDRESS", ip_address)

                template = ncs.template.Template(service)
                template.apply("core_intf-template", vars)
                self.log.info(f"Configured {interface} with {ip_address} on {device}")

class CoreIntf(ncs.application.Application):
    def setup(self):
        self.log.info("CoreIntf Service RUNNING")
        self.register_service("core_intf-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("CoreIntf Service STOPPED")


