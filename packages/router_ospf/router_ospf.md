# Cisco NSO FASTMAP-Based OSPF Configuration Service

## **Overview**
This document describes the implementation of a **FASTMAP-based OSPF service** in Cisco NSO. The service allows users to configure OSPF settings dynamically on multiple devices, including process ID, router ID, and network statements.

## **NSO Service Implementation**

### **1. YANG Data Model**
The YANG model defines the service structure, specifying devices, OSPF process ID, router ID, and network configurations.

#### **File: `router_ospf.yang`**
```yang
module router_ospf {

  namespace "http://example.com/router_ospf";
  prefix router_ospf;

  import ietf-inet-types { prefix inet; }
  import tailf-common { prefix tailf; }
  import tailf-ncs { prefix ncs; }

  description "OSPF Configuration Service using FASTMAP";

  revision 2025-02-14 {
    description "Initial revision.";
  }

  list router_ospf {
    key name;
    description "OSPF Service Instances";

    leaf name {
      type string;
      description "Service Instance Name";
    }

    uses ncs:service-data;
    ncs:servicepoint router_ospf-servicepoint;

    list devices {
      key device_name;
      description "List of OSPF-enabled devices";

      leaf device_name {
        type leafref {
          path "/ncs:devices/ncs:device/ncs:name";
        }
        mandatory true;
      }

      leaf process_id {
        type uint32;
        description "OSPF Process ID";
        mandatory true;
      }
      
      leaf router_id {
        type inet:ipv4-address;
        description "OSPF Router ID";
        mandatory true;
      }
      
      list networks {
        key network_address;
        description "List of OSPF Networks";

        leaf network_address {
          type inet:ipv4-address;
          description "Network IP Address";
          mandatory true;
        }

        leaf wildcard_mask {
          type inet:ipv4-address;
          description "Wildcard Mask for the OSPF Network";
          mandatory true;
        }

        leaf area {
          type uint32;
          description "OSPF Area ID";
          mandatory true;
        }
      }
    }
  }
}
```

---

### **2. NSO Python Service Logic**
The Python script processes the input data and applies the **FASTMAP service template** for OSPF configuration.

#### **File: `router_ospf.py`**
```python
# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'Service create: {service._path}')
        
        template = ncs.template.Template(service)
        
        for device in service.devices:
            vars = ncs.template.Variables()
            vars.add("device_name_py", device.device_name)
            vars.add("process_id_py", device.process_id)
            vars.add("router_id_py", device.router_id)
            
            # Apply base OSPF template
            template.apply('router_ospf-template', vars)
            
            # Apply network configurations
            for net in device.networks:
                net_vars = ncs.template.Variables()
                net_vars.add("device_name_py", device.device_name)
                net_vars.add("process_id_py", device.process_id)
                net_vars.add("network_address_py", net.network_address)
                net_vars.add("wildcard_mask_py", net.wildcard_mask)
                net_vars.add("area_py", net.area)
                
                template.apply('router_ospf-network-template', net_vars)
            
            self.log.info(f"Applied OSPF config to device {device.device_name}")

class ROUTER_OSPF(ncs.application.Application):
    def setup(self):
        self.log.info('ROUTER_OSPF Service Running')
        self.register_service('router_ospf-servicepoint', ServiceCallbacks)
    
    def teardown(self):
        self.log.info('ROUTER_OSPF Service Stopped')
```

---

### **3. XML Service Templates**

#### **File: `router_ospf-template.xml`** (Base OSPF Configuration)
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
          </ospf>
        </router>
      </config>
    </device>
  </devices>
</config-template>
```

#### **File: `router_ospf-network-template.xml`** (Network Configuration)
```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device>
      <name>{\$device_name_py}</name>
      <config>
        <router xmlns="urn:ios">
          <ospf>
            <id>{\$process_id_py}</id>
            <network>
              <ip>{\$network_address_py}</ip>
              <mask>{\$wildcard_mask_py}</mask>
              <area>{\$area_py}</area>
            </network>
          </ospf>
        </router>
      </config>
    </device>
  </devices>
</config-template>
```

---

## **4. Building and Deploying the NSO Package**

### **Step 1: Navigate to Package Source Directory**
```bash
cd ~/ncs-run/packages/router_ospf/src
```

### **Step 2: Build the Package**
```bash
make all
```

### **Step 3: Load or Reload the Package in NSO**
```bash
ncs_cli -C -u admin
config
packages package router_ospf reload
commit
exit
```

---

## **5. NSO CLI Configuration Commands**
To create an OSPF service instance for multiple devices:
```bash
config
router_ospf ospf_core
  devices PE-53
    process_id 65005
    router_id 192.168.168.53
    networks 192.168.53.0 wildcard_mask 0.0.0.255 area 0
    networks 192.168.168.53 wildcard_mask 0.0.0.0 area 0
  exit
commit
```

---

## **Conclusion**
This FASTMAP-based **OSPF Service for NSO** automates OSPF configuration across multiple devices dynamically. It allows for flexible network statements while ensuring maintainability through **YANG**, **Python**, and **XML templates**.

🚀 **NSO will now efficiently configure and manage OSPF across your network!** 🎯

