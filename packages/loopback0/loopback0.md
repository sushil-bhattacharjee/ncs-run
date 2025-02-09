# Loopback0 Service Setup and Explanation

## **1. Creating the NSO Package**
```sh
ncs-make-package --no-java --service-skeleton python-and-template --component-class loopback0.Loopback0 --dest packages/loopback0 loopback0
```

## **2. Updating YANG Model (`loopback0.yang`)**
```yang
module loopback0 {
  namespace "http://example.com/loopback0";
  prefix loopback0;

  import ietf-inet-types { prefix inet; }
  import tailf-common { prefix tailf; }
  import tailf-ncs { prefix ncs; }

  description "Loopback0 service supporting multiple devices entered one by one.";

  revision 2025-02-09 {
    description "Added support for multiple devices entered individually.";
  }

  list loopback0 {
    key name;
    uses ncs:service-data;
    ncs:servicepoint "loopback0-servicepoint";

    leaf name {
      type string;
      description "Service Instance Name";
    }

    leaf ip_prefix {
      tailf:info "IP prefix used to configure an address loopback interface";
      type inet:ipv4-prefix;
      mandatory true;
    }

    list devices {
      key name;
      description "List of Devices";
      
      leaf name {
        type leafref {
          path "/ncs:devices/ncs:device/ncs:name";
        }
      }
    }
  }
}
```

## **3. Updating Python Service Logic (`loopback0.py`)**
```python
# -*- mode: python; python-indent: 4 -*-
import ncs
import ipaddress
from ncs.application import Service

# This service dynamically assigns a Loopback0 IP address
# based on the last two digits of the device name.
# It also supports configuring multiple devices in one go.
# Example, if device name is PE-51,CE-61 and the assigned ip_prefix=192.168.168.0/24
#The for PE-51 Loopback0=192.168.168.51/32; for CE-61 Loopback0=192.168.168.61/32

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'Service create(service={service._path})')

        # Ensure ip_prefix exists in service model
        if not hasattr(service, "ip_prefix"):
            raise ValueError("ip_prefix is missing from the service configuration!")

        # Extract network prefix from input
        network = ipaddress.IPv4Network(service.ip_prefix, strict=False)

        # Iterate through devices and assign IPs
        for device_entry in service.devices:
            device = device_entry.name  # Extract device name
            last_two_chars = device[-2:]  # Extract last two characters of device name

            # Convert last two chars to integer (default to 99 if conversion fails)
            try:
                last_octet = int(last_two_chars)
            except ValueError:
                last_octet = 99  # Default if conversion fails

            # Generate IP address using the last octet
            ip_address = str(network.network_address + last_octet)

            # Apply service template
            vars = ncs.template.Variables()
            vars.add("DEVICE", device)
            vars.add("IP_ADDRESS", ip_address)

            template = ncs.template.Template(service)
            template.apply("loopback0-template", vars)
            self.log.info(f"Loopback0 {ip_address} assigned to {device}")

class Loopback0(ncs.application.Application):
    def setup(self):
        self.log.info("Loopback0 Service RUNNING")
        self.register_service("loopback0-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("Loopback0 Service STOPPED")
```

## **4. Updating XML Template (`loopback0-template.xml`)**
```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device>
      <name>{/device}</name>
      <config>
        <!-- This template applies Loopback0 to multiple devices at once. -->
        <!-- The IP address is derived from the last two digits of the device name. -->
        
        <!-- IOS-XE -->
        <interface xmlns="urn:ios">
          <Loopback>
            <name>0</name>
            <description>Created by NSO Loopback0 Service</description>
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
            <id>0</id>
            <description>Created by NSO Loopback0 Service</description>
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

## **5. Deploying a Loopback0 Service Example**
```sh
admin@ncs# config
admin@ncs(config)# loopback0 LB0_PE ip_prefix 192.168.168.0/24
admin@ncs(config-loopback0-LB0_PE)# devices PE-51
admin@ncs(config-devices-PE-51)# devices PE-52
admin@ncs(config-devices-PE-52)# devices P-60
admin@ncs(config-loopback0-LB0_PE)# commit dry-run outformat native
admin@ncs(config-loopback0-LB0_PE)# commit
```

### **Summary**
✅ Dynamically assigns Loopback0 IP addresses based on the last two digits of the device name.
✅ Supports configuring multiple devices in one go.
✅ Uses YANG, Python service logic, and XML templates.
✅ Simplifies bulk configuration deployment for network automation.

