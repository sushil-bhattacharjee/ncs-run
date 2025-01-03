import requests

url = "http://10.1.10.39:8080//restconf/data/tailf-ncs:devices/device=CSR1K17031R6/config/tailf-ned-cisco-ios:ip/route/ip-route-forwarding-list=0.0.0.0,0.0.0.0,10.10.20.254"

payload = "{\r\n    \"severity\": \"critical\"\r\n}"
headers = {
  'Accept': 'application/yang-data+xml',
  'Content-Type': 'application/yang-data+xml',
  'Authorization': 'Basic YWRtaW46YWRtaW4='
}

response = requests.request("GET", url, headers=headers, data=payload, verify=False)

print(response.text)
