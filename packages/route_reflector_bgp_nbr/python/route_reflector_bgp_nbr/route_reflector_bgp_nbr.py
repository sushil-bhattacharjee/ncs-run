# -*- mode: python; python-indent: 4 -*-
# -*- mode: python; python-indent: 4 -*-
import ncs
from ncs.application import Service

class ServiceCallbacks(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info(f"Processing BGP neighbors for Route Reflector P-60 in service: {service.name}")

        # Ensure that there are neighbors defined in the service model
        if not hasattr(service, "neighbors") or not service.neighbors:
            raise ValueError("No BGP neighbors provided in the service configuration!")

        # Get reference to P-60's existing configuration
        device_name = "P-60"
        existing_neighbors = set()

        # Check if device already has neighbors configured
        if device_name in root.devices.device:
            device_config = root.devices.device[device_name].config
            if hasattr(device_config, "router"):
                bgp_config = device_config.router.bgp.bgp_no_instance[65005]
                if hasattr(bgp_config, "neighbor"):
                    existing_neighbors = {nbr.id for nbr in bgp_config.neighbor}

        # Loop through neighbors and add only new ones
        for neighbor in service.neighbors:
            neighbor_ip = neighbor.ip  # Extract neighbor IP

            if neighbor_ip in existing_neighbors:
                self.log.info(f"Skipping existing neighbor {neighbor_ip} on {device_name}")
                continue  # Skip existing neighbors

            # Apply template
            vars = ncs.template.Variables()
            vars.add("NEIGHBOR_IP", neighbor_ip)

            template = ncs.template.Template(service)
            template.apply("route_reflector_bgp_nbr-template", vars)

            self.log.info(f"Added new BGP neighbor {neighbor_ip} to P-60")

class RouteReflectorBgpNbr(ncs.application.Application):
    def setup(self):
        self.log.info("Route Reflector BGP Neighbor Service RUNNING")
        self.register_service("route_reflector_bgp_nbr-servicepoint", ServiceCallbacks)

    def teardown(self):
        self.log.info("Route Reflector BGP Neighbor Service STOPPED")


