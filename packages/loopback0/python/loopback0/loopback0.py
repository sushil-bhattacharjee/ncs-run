# -*- mode: python; python-indent: 4 -*-
import ncs
import ipaddress
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f"Service create(service={service._path})")

        # Ensure ip_prefix exists in service model
        if not hasattr(service, "ip_prefix"):
            raise ValueError("ip_prefix is missing from the service configuration!")

        # Extract network prefix from input
        network = ipaddress.IPv4Network(service.ip_prefix, strict=False)

        # Iterate through devices and assign IPs
        for device_entry in service.devices:
            device = device_entry.name  # Extract device name

            # Extract last two characters and convert to integer
            last_two_chars = ''.join(filter(str.isdigit, device[-2:]))  # Only keep digits
            last_octet = int(last_two_chars) if last_two_chars.isdigit() else 99  # Default to 99 if no digits

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




