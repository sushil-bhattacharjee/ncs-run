import json
import requests

NE = {
    "device":{
        "name":"R3",
        "address": "10.1.10.53",
        "port":22,
        "state":{
            "admin-state":"unlocked"

        },
        "authgroup":"sushil",
        "device-type":{
            "cli":{
                "ned-id":"tailf-ned-cisco-ios-id:cisco-ios"
            }
        }
    }
}
def main():
    baseUri = "http://localhost:8080/restconf/data"
    auth = ('admin', 'admin')
    headers = {'Content-Type':'application/yang-data+json'}
    response = requests.put(baseUri+'/devices/device=ios-device', auth=auth, headers=headers, data=json.dumps(NE))

    print(response)
    baseUriOperation = "http://localhost:8080/restconf/operations"

    response=requests.post(baseUriOperation+"/devices/device=ios-device/ssh/request-host-keys", auth=auth, headers=headers)
    response = requests.post(baseUriOperation + "/devices/device=ios3/sync-form, auth=auth, headers=headers")
    print(response)

if __name__=="__main__":
 main()