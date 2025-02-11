# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'🟢 Processing Service {service._path}')
        
        for device_entry in service.devices:
            device = device_entry.name  
            ip_address = device_entry.ip_address  

            self.log.info(f'🟡 Applying Loopback100 {ip_address} on {device}')  # ✅ Debug log

            vars = ncs.template.Variables()
            vars.add("DEVICE", device)  
            vars.add("IP_ADDRESS", ip_address)  

            template = ncs.template.Template(service)
            template.apply("loopback100-template", vars)  # ✅ Apply template

            self.log.info(f'✅ Successfully assigned {ip_address} to {device}')  # ✅ Confirmation log


class Loopback100(ncs.application.Application):
    def setup(self):
        self.log.info("Loopback100 Service RUNNING")
        self.register_service("loopback100-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("Loopback100 Service STOPPED")
