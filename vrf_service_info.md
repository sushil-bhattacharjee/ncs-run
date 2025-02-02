https://chatgpt.com/c/679cdef3-b258-800b-adbc-e515843af8cd
## Step 1: Create the NSO Package
```sh
ncs-make-package --no-java --service-skeleton python-and-template --component-class vrf_service.VrfService --dest packages/vrf_service vrf_service
```

## Step 2: Update YANG Model (vrf_service.yang)
```yang
module vrf_service {

  namespace "http://example.com/vrf_service";
  prefix vrf_service;

  import ietf-inet-types {
    prefix inet;
  }
  import tailf-common {
    prefix tailf;
  }
  import tailf-ncs {
    prefix ncs;
  }

  description
    "VRF Service Model";

  revision 2025-02-02 {
    description
      "Initial revision.";
  }

  list vrf_service {
    key name;

    uses ncs:service-data;
    ncs:servicepoint vrf_service-servicepoint;

    leaf name {
      tailf:info "Service instance name";
      type string;
    }

    leaf device {
      tailf:info "Device Name";
      type leafref {
        path "/ncs:devices/ncs:device/ncs:name";
      }
    }

    leaf vrf_name {
      tailf:info "VRF Name";
      type string;
    }

    leaf vrf_rd {
      tailf:info "Route Distinguisher";
      type string;
    }

    leaf vrf_export_asn {
      tailf:info "Route Target Export ASN";
      type string;
    }

    leaf vrf_import_asn {
      tailf:info "Route Target Import ASN";
      type string;
    }
  }
}
```

## Step 3: Update Python Service Logic (vrf_service.py)
```python
# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

VRF_NAMES = ["vrf_name_pythonA", "vrf_name_pythonB", "vrf_name_pythonC", "vrf_name_pythonD", "vrf_name_pythonE"]

class VrfServiceCallbacks(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f'Creating VRF service: {service.name}')

        vrf_name = VRF_NAMES.pop(0)
        self.log.info(f'Assigned VRF Name: {vrf_name}')

        vars = ncs.template.Variables()
        vars.add('vrf_name', vrf_name)
        vars.add('device', service.device)
        vars.add('vrf_rd', service.vrf_rd)
        vars.add('vrf_export_asn', service.vrf_export_asn)
        vars.add('vrf_import_asn', service.vrf_import_asn)

        template = ncs.template.Template(service)
        template.apply('vrf_service-template', vars)

        self.log.info(f'VRF {vrf_name} applied to device {service.device}')

class VrfService(ncs.application.Application):
    def setup(self):
        self.log.info('VRF Service RUNNING')
        self.register_service("vrf_service-servicepoint", VrfServiceCallbacks)

    def teardown(self):
        self.log.info('VRF Service STOPPED')
```

## Step 4: Update XML Template (vrf_service-template.xml)
```xml
<config-template xmlns="http://tail-f.com/ns/config/1.0">
  <devices xmlns="http://tail-f.com/ns/ncs">
    <device>
      <name>{/device}</name>
      <config>
        <vrf xmlns="urn:ios">
          <definition>
            <name>{\$vrf_name}</name>
            <rd>{/vrf_rd}</rd>
            <address-family>
              <ipv4>
                <route-target>
                  <export>
                    <asn-ip>{/vrf_export_asn}</asn-ip>
                  </export>
                  <import>
                    <asn-ip>{/vrf_import_asn}</asn-ip>
                  </import>
                </route-target>
              </ipv4>
            </address-family>
          </definition>
        </vrf>
      </config>
    </device>
  </devices>
</config-template>
```

## Step 5: Compile and Build the Package
```sh
make -C packages/vrf_service/src/
```

## Step 6: Reload Package in NSO (Under Admin Mode)
```sh
ncs_cli -C -u admin
admin@ncs# packages reload
```

## Step 7: Deploy a VRF Service Example
```sh
admin@ncs# config
admin@ncs(config)# vrf_service vrfservice1 device R1 vrf_rd 65001:11 vrf_import_asn 65001:11 vrf_export_asn 65001:11
admin@ncs(config-vrf_service-vrfservice1)# commit dry-run outformat native
native {
    device {
        name R1
        data vrf definition vrf_name_pythonB
              rd 65001:11
              address-family ipv4
               route-target export 65001:11
               route-target import 65001:11
               exit-address-family
              !
             !
    }
}
admin@ncs(config-vrf_service-vrfservice1)# commit
```

