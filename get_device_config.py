import requests
from rich import print
import json

# Headers for the request
headers = {
    'Accept': 'application/yang-data+json',
    'Content-Type': 'application/yang-data+json',
    'Authorization': 'Basic YWRtaW46YWRtaW4=',
}

# Make the GET request
response = requests.get('http://10.1.10.99:9280/restconf/data/tailf-ncs:devices/device=R1', headers=headers, verify=False)
response_snmp = requests.get('http://10.1.10.99:9280/restconf/data/tailf-ncs:devices/device=R1/config/tailf-ned-cisco-ios:snmp-server', headers=headers, verify=False)
# Print the response to console
# print(response.text)
print(response_snmp.text)

# # Save the response to a JSON file
# try:
#     # Parse response as JSON
#     response_data = response.json()
    
#     # Save JSON data to file
#     with open('response_output.json', 'w') as file:
#         json.dump(response_data, file, indent=4)
#         print("[green]Response saved to 'response_output.json'[/green]")
# except ValueError:
#     # Handle cases where response is not valid JSON
#     with open('response_output.json', 'w') as file:
#         file.write(response.text)
#         print("[yellow]Response saved as plain text to 'response_output.json'[/yellow]")
