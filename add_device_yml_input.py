import yaml
import ncs

def load_yaml(filename):
    """Load YAML file containing device configurations."""
    with open(filename, 'r') as file:
        return yaml.safe_load(file)

def add_device(device, m):
    """Function to add a device to NSO from YAML input."""
    with ncs.maapi.Session(m, 'admin', 'system'):
        with m.start_write_trans() as t:
            print(f'Setting the device "{device["name"]}" configuration...')

            # Get a reference to the device list
            root = ncs.maagic.get_root(t)
            device_list = root.devices.device

            if device["name"] not in device_list:
                dev = device_list.create(device["name"])
                dev.address = device["address"]
                dev.port = device.get("port", 22)
                dev.description = device.get("desc", "Device created via YAML input")
                dev.authgroup = device["authgroup"]
                dev_type = dev.device_type.cli
                dev_type.ned_id = device["ned"]
                dev.state.admin_state = 'unlocked'
                
                print('Committing the device configuration...')
                t.apply()
                print(f'Device "{device["name"]}" committed!')

            else:
                print(f'Device "{device["name"]}" already exists in NSO...')

        # Perform SSH key fetch & sync-from outside transaction
        root = ncs.maagic.get_root(m)
        dev = root.devices.device[device["name"]]
        print('Fetching SSH keys...')
        output = dev.ssh.fetch_host_keys()
        print(f'Result: {output.result}')
        print('Syncing configuration...')
        output = dev.sync_from()
        print(f'Result: {output.result}')
        if not output.result:
            print(f'Error: {output.info}')

def main():
    """Main function to read YAML and configure devices."""
    yaml_file = "devices.yml"  # Change this to your YAML file path
    devices = load_yaml(yaml_file)
    
    with ncs.maapi.Maapi() as m:
        for device in devices["devices"]:
            add_device(device, m)

if __name__ == '__main__':
    main()
