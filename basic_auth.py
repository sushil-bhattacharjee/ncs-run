import requests
headers = {
    'Content-Type': 'application/yang-data+json',
    'Authorization': 'Basic YWRtaW46YWRtaW4='
}

response = requests.get('http://localhost:8080/restconf/data/tailf-ncs:devices', headers=headers)
print(response.text)