import ncs 
with ncs.maapi.single_write_trans('admin', 'system') as t: 
    root = ncs.maagic.get_root(t) 
    root.devices.device['R1'].config.hostname = 'R1'
    t.apply()