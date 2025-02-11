# -*- mode: python; python-indent: 4 -*-
import ncs
import ipaddress
from ncs.application import NanoService


class ServiceCallbacks(NanoService):
    @NanoService.create
    def cb_nano_create(self, tctx, root, service, plan, component, state, proplist, component_proplist):
        self.log.info(f'NanoService create(service={service._path})')

        # Check if prefix-allocations exist
        if service.name in root.prefix_allocations:
            ip_prefix = root.prefix_allocations[service.name].ip_prefix
            self.log.debug(f'Value of ip-prefix leaf for {service.name} is {ip_prefix}')
            
            # Allocate first available IP from the prefix
            net = ipaddress.IPv4Network(ip_prefix)
            ip_address = next(net.hosts())
            self.log.debug(f'Assigned IP Address: {ip_address}')

            # Apply the template with the assigned IP
            vars = ncs.template.Variables()
            vars.add('IP_ADDRESS', str(ip_address))
            template = ncs.template.Template(service)
            template.apply('loopback_nano-template', vars)

            self.log.info(f'Template applied successfully for {service.name}')
        else:
            self.log.error(f'Prefix allocation for {service.name} not found!')


# ---------------------------------------------
# COMPONENT THREAD THAT WILL BE STARTED BY NCS
# ---------------------------------------------
class Loopback(ncs.application.Application):
    def setup(self):
        self.log.info('Loopback Nano Service RUNNING')

        # Register the Nano Service
        self.register_nano_service(
            'loopback_nano',                  # Service point
            'loopback_nano:loopback',         # Component type
            'loopback_nano:loopback-configured',  # Plan state
            ServiceCallbacks
        )

    def teardown(self):
        self.log.info('Loopback Nano Service FINISHED')
