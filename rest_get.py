import requests
from rich import print as rprint

headers = {
    'Content-Type': 'application/yang-data+json',
    'Authorization': 'Basic YWRtaW46YWRtaW4=' 
    #admin:admin converted to base64encode, it's NSO username and password
}

baseUri = 'http://localhost:8080/restconf/data'
UriConfig = '/tailf-ncs:devices/device=CSR1K17031R6/config'

#device_list = [R1, CSR1K17031R6, XRv779K3]

##XE configuration
XE_ospf = '/tailf-ned-cisco-ios:router/ospf=172'
XE_bgp = '/tailf-ned-cisco-ios:router/bgp=65077'
XE_VRF = '/tailf-ned-cisco-ios:vrf/definition=CustomerA'
XE_GiETH = '/tailf-ned-cisco-ios:interface/GigabitEthernet=1'
XE_lb = '/tailf-ned-cisco-ios:interface/Loopback=200'

##XR Configurations
XR_VRF = '/tailf-ned-cisco-ios-xr:vrf/vrf-list=CustomerA'
XR_ospf = '/tailf-ned-cisco-ios-xr:router/ospf=10'
XR_bgp = '/tailf-ned-cisco-ios-xr:router/bgp/bgp-no-instance=65010'
XR_Gieth = '/tailf-ned-cisco-ios-xr:interface/GigabitEthernet=0%2F0%2F0%2F0'
XR_lb = '/tailf-ned-cisco-ios-xr:interface/Loopback=110'


####    RESULT   ###
#response = requests.get(baseUri+'/tailf-ncs:devices/device=R1', headers=headers)
#response = requests.get(baseUri + UriConfig + '/tailf-ned-cisco-ios:ip/vrf', headers=headers)
response = requests.get(baseUri + UriConfig + XE_lb, headers=headers)
rprint(response.text)