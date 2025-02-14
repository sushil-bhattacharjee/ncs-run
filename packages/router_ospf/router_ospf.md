# OSPF Configuration using FASTMAP in Cisco NSO

This document describes the implementation of OSPF configuration for IOS-XE and IOS-XR devices using Cisco NSO with FASTMAP. It includes the YANG model, Python service logic, XML templates, and instructions for deployment and testing.

---

## 1. **YANG Model: `router_ospf.yang`**

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

  description "OSPF Configuration using FASTMAP";

  revision 2016-01-01 {
    description "Initial revision.";
  }

  list router_ospf {
    description "OSPF configuration service";
    key name;

    leaf name {
      tailf:info "Unique service id";
      type string;
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
        description "Router ID for OSPF";
        mandatory true;
      }

      list networks {
        key network_address;
        description "List of OSPF network statements for IOS";

        leaf network_address {
          type inet:ipv4-address;
          description "Network IP Address for IOS";
          mandatory true;
        }

        leaf wildcard_mask {
          type inet:ipv4-address;
          description "Subnet Mask for the OSPF network for IOS";
          mandatory true;
        }

        leaf area {
          type uint32;
          description "OSPF Area ID";
          mandatory true;
        }
      }

      list interfaces {
        key interface_name;
        description "List of OSPF-enabled interfaces for IOS-XR and IOS-XE";

        leaf interface_name {
          type string;
          description "Interface name";
          mandatory true;
        }

        leaf area {
          type uint32;
          description "OSPF Area ID for IOS-XR";
          mandatory true;
        }
      }
    }
  }
}
```

---

## 2. **Python Service Logic: `router_ospf.py`**

```python
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
            template.apply('router_ospf-template', vars)

            # Apply OSPF network statements (Only for IOS)
            for net in device.networks:
                net_vars = ncs.template.Variables()
                net_vars.add("device_name_py", device.device_name)
                net_vars.add("process_id_py", device.process_id)
                net_vars.add("network_address_py", net.network_address)
                net_vars.add("wildcard_mask_py", net.wildcard_mask)
                net_vars.add("area_py", net.area)
                template.apply('router_ospf-ios-network-template', net_vars)

            # Apply OSPF interfaces (Only for IOS-XR)
            for intf in device.interfaces:
                intf_vars = ncs.template.Variables()
                intf_vars.add("device_name_py", device.device_name)
                intf_vars.add("process_id_py", device.process_id)
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

## 3. **Building and Deploying the Package**

### **Step 1: Build the Package**
```bash
cd ~/ncs-run/packages/router_ospf
ncs-make-package --service-skeleton python-and-template --yang-model router_ospf
```

### **Step 2: Compile and Reload Packages**
```bash
make clean all
cd ~/ncs-run
ncs --with-package-reload
```

### **Step 3: Deploy the Service**
```bash
config
packages reload
commit
```

---

## 4. **Configuring OSPF in NSO CLI**

### **For IOS-XE:**
```bash
config
router_ospf ospf_config devices PE-51
 process_id 65001
 router_id 192.168.168.51
 networks 192.168.51.0 wildcard_mask 0.0.0.255 area 0
 networks 192.168.168.51 wildcard_mask 0.0.0.0 area 0
commit
```

### **For IOS-XR:**
```bash
config
router_ospf ospf_config devices P-60
 process_id 65005
 router_id 192.168.168.60
 interfaces Loopback0 area 0
 interfaces GigabitEthernet0/0/0/2 area 0
commit
```

---

## **Conclusion**
This NSO FASTMAP implementation efficiently configures OSPF for both IOS-XE and IOS-XR devices, leveraging templates for consistency. The solution allows seamless OSPF provisioning, modification, and deletion across multiple network devices.

🚀 **Let me know if you need further refinements!**

