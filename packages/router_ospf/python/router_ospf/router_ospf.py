# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        
        template = ncs.template.Template(service)
        
        # Iterate over each device in the service
        for device in service.devices:
            vars = ncs.template.Variables()
            vars.add("device_name_py", device.device_name)
            vars.add("process_id_py", device.process_id)
            vars.add("router_id_py", device.router_id)
            # Apply OSPF base configuration
            template.apply('router_ospf-template', vars)

            # Apply OSPF network statements only for IOS
            for net in device.networks:
                net_vars = ncs.template.Variables()
                net_vars.add("device_name_py", device.device_name)
                net_vars.add("process_id_py", device.process_id)
                net_vars.add("network_address_py", net.network_address)
                net_vars.add("wildcard_mask_py", net.wildcard_mask)
                net_vars.add("area_py", net.area)
                template.apply('router_ospf-ios-network-template', net_vars)
            # Apply OSPF interface configuration (Only for IOS-XR)
            for intf in device.interfaces:
                intf_vars = ncs.template.Variables()
                intf_vars.add("device_name_py", device.device_name)
                intf_vars.add("process_id_py", device.process_id)
                intf_vars.add("interface_name_py", intf.interface_name)
                intf_vars.add("area_py", intf.area)
                template.apply("router_ospf-xr-interface-template", intf_vars)
                # Apply OSPF interface type configuration for IOS
            self.log.info(f"Applied OSPF config to device {device.device_name}")

class ROUTER_OSPF(ncs.application.Application):
    def setup(self):
        self.log.info('ROUTER_OSPF RUNNING')
        self.register_service('router_ospf-servicepoint', ServiceCallbacks)
    def teardown(self):
        self.log.info('ROUTER_OSPF FINISHED')
