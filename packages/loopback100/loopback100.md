# **Loopback100 Python-Template for Cisco NSO**

## **Overview**
This document provides step-by-step instructions to create a **Loopback100 Service** using Python and XML templates in Cisco NSO. The service allows configuring **Loopback100 interfaces** on multiple devices with FastMap support for future modifications.

## **1. Creating the NSO Package**
Run the following command to generate a new service package:

```sh
ncs-make-package --service-skeleton python-and-template --component-class loopback100.Loopback100 loopback100
```

## **2. Updating the YANG Model (`loopback100.yang`)**
This defines the Loopback100 service structure.

```yang
module loopback100 {
  namespace "http://example.com/loopback100";
  prefix loopback100;

  import ietf-inet-types { prefix inet; }
  import tailf-common { prefix tailf; }
  import tailf-ncs { prefix ncs; }

  description "Loopback100 service supporting multiple devices inside the service.";

  revision 2025-02-10 {
    description "Added support for multiple devices.";
  }

  list loopback100 {
    key name;
    uses ncs:service-data;
    ncs:servicepoint "loopback100-servicepoint";

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

      leaf ip_address {
        type inet:ipv4-address;
        mandatory true;
      }
    }
  }
}
```

## **3. Updating Python Service Logic (`loopback100.py`)**
This script applies the Loopback100 configuration to multiple devices.

```python
# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'🟢 Processing Service {service._path}')

        for device_entry in service.devices:
            device = device_entry.name  # Get device name
            ip_address = device_entry.ip_address  # Get IP address

            self.log.info(f'🟡 Applying Loopback100 {ip_address} on {device}')

            vars = ncs.template.Variables()
            vars.add("DEVICE", device)
            vars.add("IP_ADDRESS", ip_address)

            template = ncs.template.Template(service)
            template.apply("loopback100-template", vars)

            self.log.info(f'✅ Successfully assigned {ip_address} to {device}')

class Loopback100(ncs.application.Application):
    def setup(self):
        self.log.info("Loopback100 Service RUNNING")
        self.register_service("loopback100-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("Loopback100 Service STOPPED")
```

## **4. Updating XML Template (`loopback100-template.xml`)**
This template ensures the configuration is applied to both IOS-XE and IOS-XR devices.

```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device tags="merge">
      <name>{\$DEVICE}</name>
      <config>
        <!-- IOS-XE -->
        <interface xmlns="urn:ios">
          <Loopback>
            <name>100</name>
            <description>Created by NSO Loopback100 Service</description>
            <ip>
              <address>
                <primary>
                  <address>{\$IP_ADDRESS}</address>
                  <mask>255.255.255.255</mask>
                </primary>
              </address>
            </ip>
          </Loopback>
        </interface>

        <!-- IOS-XR -->
        <interface xmlns="http://tail-f.com/ned/cisco-ios-xr">
          <Loopback>
            <id>100</id>
            <description>Created by NSO Loopback100 Service</description>
            <ipv4>
              <address>
                <ip>{\$IP_ADDRESS}</ip>
                <mask>255.255.255.255</mask>
              </address>
            </ipv4>
          </Loopback>
        </interface>
      </config>
    </device>
  </devices>
</config-template>
```

## **5. Compiling and Building the Package**
```sh
make -C packages/loopback100/src/
```

## **6. Reloading the Package in NSO (Admin Mode)**
```sh
ncs_cli -C -u admin
admin@ncs# packages reload
```

## **7. Deploying the Loopback100 Service Example**
```sh
admin@ncs# configure
admin@ncs(config)# loopback100 LB100
admin@ncs(config-loopback100-LB100)# devices PE-51 ip_address 192.168.100.51
admin@ncs(config-devices-PE-51)# exit
admin@ncs(config-loopback100-LB100)# devices PE-52 ip_address 192.168.100.52
admin@ncs(config-devices-PE-52)# commit dry-run outformat native
```

## **8. Debugging Issues**
If the service does not apply changes, check logs:
```sh
tail -f logs/ncs-python-vm-loopback100.log
```
Common Fixes:
- **Ensure the Python script logs messages**
- **Use `tags="merge"` in the XML template**
- **Confirm variable names match between Python and XML**

## **9. Conclusion**
✅ Successfully built a **Python-based NSO Service** to create and manage **Loopback100 interfaces** with FastMap. This template ensures future updates and modifications are seamless.

---

🚀 **Next Steps**:
- Test adding a new device **MI6-64**
- Modify **PE-53, PE-54** IPs using this service
- Explore compliance reporting

