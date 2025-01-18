import argparse
import ncs 

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', help='Name of the device to add', required=True)
    parser.add_argument('--address', help='Address of the device to add', required=True)
    parser.add_argument('--port', help='Port of the device to add', required=True)
    parser.add_argument('--ned', help='device NED ID', required=True)
    parser.add_argument('--desc', help='device description', default='Device created by maagic_create_device.py')
    parser.add_argument('--authgroup', help='device authgroup', default='default')
    args = parser.parse_args()
    return args

def main(args):
    with ncs.maapi.Maapi() as m:
        with ncs.maapi.Session(m, 'admin', 'system'):
            with m.start_write_trans() as t:
               print(f"Adding device {args.name} with address {args.address}")
               # Get access to the root of the CDB using Maagic API
               root = ncs.maagic.get_root(t)
               device_list = root.devices.device
               
               if args.name not in device_list:
                   device = device_list.create(args.name)
                   device.address = args.address
                   device.port = int(args.port)
                   device.description = args.desc
                   device.authgroup = args.authgroup
                   device.device_type.cli.ned_id = args.ned
                   device.state.admin_state = 'unlocked'
                   print('Committing the device configuration...')
                   t.apply()
                   print(f"Device {args.name} added successfully")
               else:
                     print(f"Device {args.name} already exists")
                     
               # This transaction is no longer valid - since we are moving
               # back under ncs.maapi.Session(m, 'admin', 'python')

               # fetch-host-keys and sync-from does not require a
               # transaction, continue using the Maapi object
               root = ncs.maagic.get_root(m)
               device = root.devices.device[args.name]
               print(f"Fetching host keys for device {args.name}")
               output = device.fetch_host_keys()
               print(f"Host keys fetched for device {args.name}, output: {output.result}")   
               print(f"Syncing device {args.name}")
               output = device.sync_from()
               print(f"Syncing device {args.name}, output: {output.result}")
               if not output.result:
                   print(f"Error Device {args.name}: {output.info}")
    
if __name__ == '__main__':
    args = parse_args()
    main(args)
    
#python3 add_device_to_nso.py --help   
# usage: add_device_to_nso.py [-h] --name NAME --address ADDRESS --ned NED
#                      [--port PORT] [--desc DESC] [--auth AUTH]

# optional arguments:
#   -h, --help         show this help message and exit
#   --name NAME        device name
#   --address ADDRESS  device address
#   --ned NED          device NED ID
#   --port PORT        device port
#   --desc DESC        device description
#   --auth AUTH        device authgroup

# [developer@nso ~]$ python3 add_device_to_nso.py --name core-rtr01 --address 127.0.0.1 --ned cisco-iosxr-cli-7.32 --auth default
# Setting the device "core-rtr0" configuration...
# Device "core-rtr0" configuration already exists...
# Fetching SSH keys...
# Result: unchanged
# Syncing configuration...
# Result: True