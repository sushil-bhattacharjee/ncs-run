import ncs ## Import ncs module that contains submodules to work with Python API
m = ncs.maapi.Maapi() # Initialize new Maapi object
m.start_user_session('admin', 'system', []) # Start new user session that allow for multiple transactions
transaction = m.start_write_trans() # Start new read transaction, not possible to write data to CDB with it
root = ncs.maagic.get_root(transaction) # Get access to the root of the CDB using Maagic API
for device in root.devices.device: # Iterate through devices in NSO using Maagic root object
    print(f" Device {device.name} address {device.address}")  # Print names and address NSO is using for management of the device
m.close() #Ends session and closes socket. 