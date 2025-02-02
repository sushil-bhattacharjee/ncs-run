import ipaddress
net = ipaddress.IPv4Network('10.0.0.0/24')
address = next(net.hosts())
print(address)