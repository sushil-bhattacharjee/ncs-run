# **Core MPLS LDP Service (FASTMAP) - Cisco NSO**

## **1️⃣ Creating the NSO Package**
Run the following command to generate the NSO package for `core_mpls` using Python and XML templates:

```sh
ncs-make-package --service-skeleton python-and-template --component-class core_mpls.CoreMpls --dest packages/core_mpls core_mpls
```

---

## **2️⃣ Updating YANG Model (`core_mpls.yang`)**

```yang
module core_mpls {
  namespace "http://example.com/core_mpls";
  prefix core_mpls;

  import ietf-inet-types { prefix inet; }
  import tailf-common { prefix tailf; }
  import tailf-ncs { prefix ncs; }

  description "MPLS LDP Service using FASTMAP";

  revision 2025-02-12 {
    description "Initial revision.";
  }

  list core_mpls {
    key name;
    uses ncs:service-data;
    ncs:servicepoint "core_mpls-servicepoint";

    leaf name {
      type string;
      description "Service Instance Name";
    }

    list devices {
      key name;
      description "List of Core Devices";

      leaf name {
        type leafref {
          path "/ncs:devices/ncs:device/ncs:name";
        }
        mandatory true;
      }

      leaf router_id {
        type inet:ipv4-address;
        description "Router ID for MPLS LDP";
        mandatory true;
      }

      list interfaces {
        key name;
        description "List of MPLS-enabled Interfaces";

        leaf name {
          type string;
          description "Interface name";
          mandatory true;
        }
      }
    }
  }
}
```

---

## **3️⃣ Updating Python Service Logic (`core_mpls.py`)**

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
            router_id = device_entry.router_id

            vars = ncs.template.Variables()
            vars.add("DEVICE", device)
            vars.add("ROUTER_ID", router_id)

            template = ncs.template.Template(service)
            template.apply("core_mpls-template-router-id", vars)

            for intf_entry in device_entry.interfaces:
                vars.add("INTERFACE", intf_entry.name)
                template.apply("core_mpls-template-interface", vars)

            self.log.info(f"Configured MPLS LDP for {device} with router ID {router_id}")

class CoreMpls(ncs.application.Application):
    def setup(self):
        self.log.info("Core MPLS Service RUNNING")
        self.register_service("core_mpls-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("Core MPLS Service STOPPED")
```

---

## **4️⃣ Updating XML Templates**

### **Router ID Configuration (`core_mpls-template-router-id.xml`)**

```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device tags="merge">
      <name>{\$DEVICE}</name>
      <config>
        <mpls xmlns="urn:ios">
          <ldp>
            <router-id>
              <interface>Loopback0</interface>
            </router-id>
          </ldp>
        </mpls>
        <mpls xmlns="http://tail-f.com/ned/cisco-ios-xr">
          <ldp>
            <router-id>{\$ROUTER_ID}</router-id>
          </ldp>
        </mpls>
        <mpls xmlns="http://tail-f.com/ned/cisco-iosxr-cli-7.55">
          <ldp>
            <router-id>{\$ROUTER_ID}</router-id>
            <address-family>
              <ipv4/>
            </address-family>
          </ldp>
        </mpls>
      </config>
    </device>
  </devices>
</config-template>
```

### **Interface Configuration (`core_mpls-template-interface.xml`)**

```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device tags="merge">
      <name>{\$DEVICE}</name>
      <config>
        <interface xmlns="urn:ios">
          <GigabitEthernet>
            <name>{\$INTERFACE}</name>
            <mpls>
              <ip/>
            </mpls>
          </GigabitEthernet>
        </interface>
        <mpls xmlns="http://tail-f.com/ned/cisco-ios-xr">
          <ldp>
            <interface>
              <name>{\$INTERFACE}</name>
            </interface>
          </ldp>
        </mpls>
      </config>
    </device>
  </devices>
</config-template>
```

---

## **5️⃣ Compiling and Reloading the Package**

```sh
make -C packages/core_mpls/src/
ncs_cli -C -u admin
admin@ncs# packages reload
```

---

## **6️⃣ Configuring MPLS LDP Using `ncs_cli`**

### **For IOS-XR Devices**
```sh
admin@ncs# config
admin@ncs(config)# core_mpls core_ldp devices P-60 router_id 192.168.100.60
admin@ncs(config-core_mpls-core_ldp)# devices P-60 interfaces GigabitEthernet0/0/0/5
admin@ncs(config-core_mpls-core_ldp)# devices P-60 interfaces GigabitEthernet0/0/0/6
admin@ncs(config-core_mpls-core_ldp)# commit
```

### **For IOS-XE Devices**
```sh
admin@ncs# config
admin@ncs(config)# core_mpls core_ldp devices PE-52 router_id 192.168.100.52
admin@ncs(config-core_mpls-core_ldp)# devices PE-52 interfaces GigabitEthernet5
admin@ncs(config-core_mpls-core_ldp)# commit
```

---

## **✅ Verifying the Configuration**

### **Show Configurations**
```sh
admin@ncs# show configuration core_mpls core_ldp
```

### **Commit Dry-Run Output**
```sh
admin@ncs# commit dry-run outformat native
admin@ncs# commit dry-run outformat xml
```

---

## **🎯 Conclusion**
This Python-with-template service (`core_mpls`) is fully FASTMAP-compliant and allows **creation, update, and deletion** of MPLS LDP configurations for both **IOS-XE** and **IOS-XR** core routers. 🚀🔥

