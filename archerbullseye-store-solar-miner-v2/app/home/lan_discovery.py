"""Bounded LAN discovery that works from a bridged Docker container."""
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def local_ip(value):
    try:
        ip = ipaddress.ip_address(value)
        return str(ip) if ip.version == 4 and any(ip in network for network in PRIVATE_NETWORKS) else None
    except ValueError:
        return None


def network_range(value):
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError('Enter a LAN network such as 192.168.88.0/24.')
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValueError('Enter a LAN network such as 192.168.88.0/24.') from None
    if network.version != 4 or network.num_addresses > 1024 or not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):
        raise ValueError('Choose a private IPv4 network with up to 1,024 addresses, such as 192.168.88.0/24.')
    return network


def probe(address):
    try:
        with socket.create_connection((address, 6668), timeout=0.35):
            return address
    except OSError:
        return None


def scan_network(network, configured, readings):
    from tinytuya import scanner
    broadcasts = {}
    warnings = []
    try:
        found = scanner.devices(scantime=6, verbose=False, poll=False, byID=False, show_timer=False,
                                forcescan=False, tuyadevices=[])
        for ip, raw in found.items():
            address = local_ip(ip)
            device_id = raw.get('gwId') or raw.get('id')
            if address and device_id:
                broadcasts[device_id] = {'address': address, 'version': raw.get('version')}
    except Exception:
        warnings.append('Broadcast discovery was unavailable. Checking the LAN range and configured hubs instead.')
    # Docker bridge networks often do not receive LAN broadcasts. Probe the supplied
    # LAN for open Tuya ports, then let account metadata or the user identify them.
    known = {}
    for hub in configured['hubs']:
        address = local_ip(hub.get('address', ''))
        if address:
            known[hub['device_id']] = {'address': address, 'version': hub.get('version')}
    for device in configured['devices']:
        if device['transport'] == 'wifi' and local_ip(device.get('address', '')):
            known[device['device_id']] = {'address': device['address'], 'version': device.get('version')}
    trusted_addresses = set()
    for device in configured['devices']:
        if readings.get(device['id'], {}).get('status') == 'connected':
            owner = next((h for h in configured['hubs'] if h['id'] == device.get('hub_id')), None) if device['transport'] == 'zigbee' else device
            if owner and local_ip(owner.get('address', '')):
                trusted_addresses.add(owner['address'])
    # Never open a competing socket to an enabled Home connection, even when
    # its children are sleeping or offline. Its worker owns that socket.
    active_addresses = {h.get('address') for h in configured['hubs'] if h.get('enabled')}
    active_addresses.update(d.get('address') for d in configured['devices']
                            if d.get('enabled') and d['transport'] == 'wifi')
    addresses = set(known_item['address'] for known_item in known.values())
    if network:
        addresses.update(str(ip) for ip in network.hosts())
    reachable = set(trusted_addresses)
    with ThreadPoolExecutor(max_workers=24) as pool:
        reachable.update(ip for ip in pool.map(probe, sorted(addresses - trusted_addresses - active_addresses)) if ip)
    reachable.update(item['address'] for item in broadcasts.values())
    matches = {key: value for key, value in known.items() if value['address'] in reachable}
    matches.update(broadcasts)
    return {'matches': matches, 'reachable': sorted(reachable), 'warnings': warnings}
