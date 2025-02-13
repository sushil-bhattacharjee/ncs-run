# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f"Service create(service={service._path})")

        for device_entry in service.devices:
            device = device_entry.name
            process_id = device_entry.process_id
            router_id = device_entry.router_id
            mpls_ldp_autoconfig = device_entry.mpls_ldp_autoconfig

            # Determine device type (IOS or IOS-XR)
            device_ned_id = root.devices.device[device].device_type.cli.ned_id

            vars = ncs.template.Variables()
            vars.add("DEVICE", device)
            vars.add("PROCESS_ID", process_id)
            vars.add("ROUTER_ID", router_id)

            template = ncs.template.Template(service)

            # ✅ 1️⃣ Apply Router ID first
            template.apply("core_ospf-template-router-id", vars)

            # ✅ 2️⃣ Apply OSPF Areas (MUST be done before interfaces)
            for area_entry in device_entry.areas:
                vars.add("AREA_ID", area_entry.id)
                template.apply("core_ospf-template-area", vars)

                # ✅ 3️⃣ Now Apply Interfaces (AFTER areas)
                for intf_entry in area_entry.interfaces:
                    vars.add("INTERFACE", intf_entry.name)
                    vars.add("NETWORK_TYPE", intf_entry.network_type)

                    # If IOS, we need a subnet mask
                    if hasattr(intf_entry, "subnet_mask") and "ios" in device_ned_id:
                        vars.add("SUBNET_MASK", intf_entry.subnet_mask)
                        template.apply("core_ospf-template-interface-ios", vars)
                    else:
                        template.apply("core_ospf-template-interface-iosxr", vars)

            # ✅ 4️⃣ Apply MPLS LDP Auto-config (if enabled)
            if mpls_ldp_autoconfig:
                template.apply("core_ospf-template-mpls", vars)

            self.log.info(f"✅ Configured OSPF for {device} with router ID {router_id}")

class CoreOspf(ncs.application.Application):
    def setup(self):
        self.log.info("Core OSPF Service RUNNING")
        self.register_service("core_ospf-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("Core OSPF Service STOPPED")




