# Cisco NSO OSPF Service with MPLS LDP Auto-Config

## Overview
This document provides the Cisco NSO-based service model to configure OSPF with optional MPLS LDP Auto-Config. The model supports both IOS and IOS-XR devices.

---

## 1. YANG Model

```yang
module router_ospf {
  namespace "http://example.com/router_ospf";
  prefix router_ospf;

  import ietf-inet-types {
    prefix inet;
  }
  import tailf-common {
    prefix tailf;
  }
  import tailf-ncs {
    prefix ncs;
  }

  description "OSPF Routing Service with MPLS LDP Auto Config";

  revision 2025-02-15 {
    description "Added MPLS LDP Auto Config Support.";
  }

  list router_ospf {
    key name;
    leaf name {
      type string;
    }

    uses ncs:service-data;
    ncs:servicepoint router_ospf-servicepoint;

    list devices {
      key device_name;

      leaf device_name {
        type leafref {
          path "/ncs:devices/ncs:device/ncs:name";
        }
        mandatory true;
      }

      leaf process_id {
        type uint32;
        mandatory true;
      }
      leaf router_id {
        type inet:ipv4-address;
        mandatory true;
      }
      leaf mpls_ldp_autoconfig {
        type boolean;
        default false;
      }
      list networks {
        key network_address;

        leaf network_address {
          type inet:ipv4-address;
          mandatory true;
        }

        leaf wildcard_mask {
          type inet:ipv4-address;
          mandatory true;
        }

        leaf area {
          type uint32;
          mandatory true;
        }
      }
      list interfaces {
        key interface_name;

        leaf interface_name {
          type string;
          mandatory true;
        }
        leaf area {
          type uint32;
          mandatory true;
        }
      }
    }
  }
}
```

---

## 2. XML Templates

### **router_ospf-template.xml**

```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device>
      <name>{\$device_name_py}</name>
      <config>
        <router xmlns="urn:ios">
          <ospf>
            <id>{\$process_id_py}</id>
            <router-id>{\$router_id_py}</router-id>
            <mpls when="{mpls_ldp_autoconfig='true'}">
              <ldp>
                <autoconfig/>
              </ldp>
            </mpls>
          </ospf>
        </router>
        <router xmlns="http://tail-f.com/ned/cisco-ios-xr">
          <ospf>
            <name>{\$process_id_py}</name>
            <router-id>{\$router_id_py}</router-id>
            <mpls when="{mpls_ldp_autoconfig='true'}">
              <ldp>
                <auto-config/>
              </ldp>
            </mpls>
          </ospf>
        </router>
      </config>
    </device>
  </devices>
</config-template>
```

---

## 3. Python Service Code

```python
# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        
        template = ncs.template.Template(service)
        
        for device in service.devices:
            vars = ncs.template.Variables()
            vars.add("device_name_py", device.device_name)
            vars.add("process_id_py", device.process_id)
            vars.add("router_id_py", device.router_id)
            vars.add("mpls_ldp_autoconfig", str(device.mpls_ldp_autoconfig).lower())
            template.apply('router_ospf-template', vars)

            for net in device.networks:
                net_vars = ncs.template.Variables()
                net_vars.add("device_name_py", device.device_name)
                net_vars.add("network_address_py", net.network_address)
                net_vars.add("wildcard_mask_py", net.wildcard_mask)
                net_vars.add("area_py", net.area)
                template.apply('router_ospf-ios-network-template', net_vars)
            
            for intf in device.interfaces:
                intf_vars = ncs.template.Variables()
                intf_vars.add("device_name_py", device.device_name)
                intf_vars.add("interface_name_py", intf.interface_name)
                intf_vars.add("area_py", intf.area)
                template.apply("router_ospf-xr-interface-template", intf_vars)
        
        self.log.info(f"Applied OSPF config to device {device.device_name}")

class ROUTER_OSPF(ncs.application.Application):
    def setup(self):
        self.log.info('ROUTER_OSPF RUNNING')
        self.register_service('router_ospf-servicepoint', ServiceCallbacks)
    def teardown(self):
        self.log.info('ROUTER_OSPF FINISHED')
```

---

## 4. Compilation & Deployment Commands

```bash
# Navigate to the package directory
cd ~/ncs-run/packages/router_ospf

# Build the package
make -C src/ all

# Reload the package
ncs_cli -u admin <<EOF
configure
packages reload
commit
EOF
```

---

## 5. NSO CLI Configuration for IOS-XE & IOS-XR

### **Adding OSPF Configuration for an IOS Device**

```bash
admin@ncs# configure
admin@ncs(config)# router_ospf ospf_config devices PE-51
admin@ncs(config-devices-PE-51)# process_id 65005
admin@ncs(config-devices-PE-51)# router_id 192.168.168.51
admin@ncs(config-devices-PE-51)# mpls_ldp_autoconfig true
admin@ncs(config-devices-PE-51)# commit
```

### **Adding OSPF Configuration for an IOS-XR Device**

```bash
admin@ncs# configure
admin@ncs(config)# router_ospf ospf_config devices P-60
admin@ncs(config-devices-P-60)# process_id 65005
admin@ncs(config-devices-P-60)# router_id 192.168.168.60
admin@ncs(config-devices-P-60)# mpls_ldp_autoconfig true
admin@ncs(config-devices-P-60)# commit
```

---

## 6. Verification Commands

```bash
# Verify the service configuration
admin@ncs# show configuration router_ospf ospf_config

# Verify the applied OSPF configuration on devices
admin@ncs# devices device PE-51 config router ospf 65005
admin@ncs# devices device P-60 config router ospf 65005
```

