# Core Interface (core_intf) Python-Template for Cisco NSO

## **1. Overview**
This NSO service package (`core_intf`) allows configuring **multiple interfaces** per device using **FASTMAP**. It supports both **IOS-XE** and **IOS-XR** devices.

- **FASTMAP ensures that existing and new configurations are managed together**, allowing modification of existing interfaces and adding new ones.
- Users can define the **interface name and IP address**, while the **subnet mask is fixed at 255.255.255.0**.

## **2. Creating the NSO Package**
To generate the service package, use the following command:

```sh
ncs-make-package --service-skeleton python-and-template --component-class core_intf.CoreInterface core_intf
```

## **3. Updating YANG Model (`core_intf.yang`)**
```yang
module core_intf {
  namespace "http://example.com/core_intf";
  prefix core_intf;

  import ietf-inet-types { prefix inet; }
  import tailf-common { prefix tailf; }
  import tailf-ncs { prefix ncs; }

  description "Service to configure multiple interfaces per device";

  revision 2025-02-10 {
    description "Added FASTMAP support for multiple interfaces.";
  }

  list core_intf {
    key name;
    uses ncs:service-data;
    ncs:servicepoint "core_intf-servicepoint";

    leaf name {
      type string;
      description "Service Instance Name";
    }

    list devices {
      key name;
      description "List of Devices";

      leaf name {
        type leafref {
          path "/ncs:devices/ncs:device/ncs:name";
        }
        mandatory true;
      }

      list interfaces {
        key interface;
        description "List of Interfaces per Device";

        leaf interface {
          type string;
          mandatory true;
        }

        leaf ip_address {
          type inet:ipv4-address;
          mandatory true;
        }
      }
    }
  }
}
```

## **4. Updating Python Service Logic (`core_intf.py`)**
```python
# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'Service create(service={service._path})')

        for device_entry in service.devices:
            device = device_entry.name

            for interface_entry in device_entry.interfaces:
                interface = interface_entry.interface
                ip_address = interface_entry.ip_address

                vars = ncs.template.Variables()
                vars.add("DEVICE", device)
                vars.add("INTERFACE", interface)
                vars.add("IP_ADDRESS", ip_address)
                
                template = ncs.template.Template(service)
                template.apply("core_intf-template", vars)
                
                self.log.info(f"Configured {interface} with {ip_address} on {device}")

class CoreInterface(ncs.application.Application):
    def setup(self):
        self.log.info("CoreInterface Service RUNNING")
        self.register_service("core_intf-servicepoint", ServiceCallbacks)
    
    def teardown(self):
        self.log.info("CoreInterface Service STOPPED")
```

## **5. Updating XML Template (`core_intf-template.xml`)**
```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device tags="merge">
      <name>{\$DEVICE}</name>
      <config>
        <!-- IOS-XE Configuration -->
        <interface xmlns="urn:ios">
          <GigabitEthernet tags="merge">
            <name>{\$INTERFACE}</name>
            <ip>
              <no-address>
                <address>false</address>
              </no-address>
              <address>
                <primary>
                  <address>{\$IP_ADDRESS}</address>
                  <mask>255.255.255.0</mask>
                </primary>
              </address>
            </ip>
          </GigabitEthernet>
        </interface>

        <!-- IOS-XR Configuration -->
        <interface xmlns="http://tail-f.com/ned/cisco-ios-xr">
          <GigabitEthernet tags="merge">
            <id>{\$INTERFACE}</id>
            <ipv4>
              <address>
                <ip>{\$IP_ADDRESS}</ip>
                <mask>255.255.255.0</mask>
              </address>
            </ipv4>
          </GigabitEthernet>
        </interface>
      </config>
    </device>
  </devices>
</config-template>
```

## **6. Compiling and Building the Package**
```sh
make -C packages/core_intf/src/
```

## **7. Reloading the Package in NSO (Admin Mode)**
```sh
ncs_cli -C -u admin
admin@ncs# packages reload
```

## **8. Deploying Core Interface Service Example**
```sh
admin@ncs# configure
admin@ncs(config)# core_intf core_router_interface
admin@ncs(config-core_intf-core_router_interface)# devices P-60 interface 0/0/0/5 ip_address 192.168.53.60
admin@ncs(config-core_intf-core_router_interface)# devices P-60 interface 0/0/0/6 ip_address 192.168.54.60
admin@ncs(config-core_intf-core_router_interface)# devices PE-56 interface 2 ip_address 192.168.56.56
admin@ncs(config-core_intf-core_router_interface)# commit
```

## **9. Verify Configuration in NSO**
```sh
admin@ncs# show configuration core_intf
```

## **10. Summary**
✅ Supports **multiple interfaces per device** using **FASTMAP**.  
✅ Works with **both IOS-XE and IOS-XR** platforms.  
✅ Allows adding/modifying configurations without losing existing settings.  
✅ **Ensures subnet mask is fixed at 255.255.255.0**.  
✅ Provides **scalability for future modifications**.  

---

With this template, you can dynamically manage core backbone interfaces using NSO efficiently! 🚀

