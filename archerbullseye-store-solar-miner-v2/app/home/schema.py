"""Validation for the saved Home workspace document."""
import copy
import ipaddress
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MASK = '••••'
from .metrics import definitions

NUMERIC_OPERATORS = ('below', 'above', 'at_most', 'at_least', 'equals', 'between', 'outside')


def number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be between {low:g} and {high:g}.')
    return value


def text(obj, key, limit=120):
    value = obj.get(key, '')
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'Invalid {key}.')
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Invalid item ID.')
    return value


def flag(obj, key):
    value = obj.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f'{key} must be true or false.')
    return value


def rows(doc, key, limit):
    value = doc.get(key, [])
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(x, dict) for x in value):
        raise ValueError(f'{key} must be a list of up to {limit} items.')
    ids = [identifier(x.get('id')) for x in value]
    if len(ids) != len(set(ids)):
        raise ValueError(f'Duplicate {key} ID.')
    return value


def connection(row, previous):
    result = {key: text(row, key) for key in ('address', 'device_id')}
    key = text(row, 'local_key', 64)
    result['local_key'] = previous.get('local_key', '') if key == MASK else key
    if result['address']:
        try:
            ipaddress.ip_address(result['address'])
        except ValueError:
            raise ValueError('Enter the device’s LAN IP address.') from None
    if result['local_key'] and len(result['local_key']) != 16:
        raise ValueError('Tuya local keys must contain 16 characters.')
    version = row.get('version', 3.4)
    if version not in (3.3, 3.4, 3.5):
        raise ValueError('Use Tuya protocol 3.3, 3.4, or 3.5.')
    result['version'] = version
    return result


def validate(doc, previous):
    if not isinstance(doc, dict):
        raise ValueError('Expected a Home workspace object.')
    timezone = text(doc, 'timezone') or 'America/Detroit'
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Enter a valid timezone, such as America/Detroit.') from None
    result = {'revision': previous['revision'], 'timezone': timezone, 'hubs': [], 'devices': [], 'rules': []}
    for kind in ('hubs', 'devices'):
        old = {row['id']: row for row in previous[kind]}
        for row in rows(doc, kind, 100):
            item = {'id': row['id'], 'name': text(row, 'name'), 'enabled': flag(row, 'enabled')}
            if not item['name']:
                raise ValueError('Give every device and hub a name.')
            if kind == 'hubs':
                item.update(connection(row, old.get(row['id'], {})))
                if item['enabled'] and not all(item[k] for k in ('address', 'device_id', 'local_key')):
                    raise ValueError(f"Complete the connection details for {item['name']}.")
            else:
                item.update({k: text(row, k) for k in ('kind', 'transport', 'hub_id', 'device_id', 'node_id')})
                if item['kind'] not in ('sensor', 'plug') or item['transport'] not in ('zigbee', 'wifi'):
                    raise ValueError('Choose a sensor or plug, connected by Zigbee or Wi-Fi.')
                if item['transport'] == 'wifi':
                    item.update(connection(row, old.get(row['id'], {})))
                else:
                    hub = next((h for h in result['hubs'] if h['id'] == item['hub_id']), None)
                    if item['enabled'] and (not hub or not hub['enabled'] or not item['node_id']):
                        raise ValueError(f"Choose an enabled hub and node ID for {item['name']}.")
                if item['enabled'] and not item['device_id']:
                    raise ValueError(f"Enter the device ID for {item['name']}.")
                if item['transport'] == 'wifi' and item['enabled'] and not all(item[k] for k in ('address', 'local_key')):
                    raise ValueError(f"Complete the Wi-Fi details for {item['name']}.")
                for dp in ('temperature_dp', 'humidity_dp', 'power_dp'):
                    item[dp] = text(row, dp, 4) or {'temperature_dp': '103', 'humidity_dp': '101', 'power_dp': '1'}[dp]
                    if not item[dp].isdigit() or not 1 <= int(item[dp]) <= 9999:
                        raise ValueError('Data point IDs must be positive numbers.')
                for metric in ('temperature', 'humidity'):
                    key = metric + '_divisor'
                    item[key] = number(row.get(key, 1), key, 0.001, 10000)
                item['temperature_unit'] = row.get('temperature_unit', 'C')
                if item['temperature_unit'] not in ('C', 'F'):
                    raise ValueError('Choose Celsius or Fahrenheit for the raw sensor unit.')
            result[kind].append(item)
    addresses = [h['address'] for h in result['hubs'] if h['enabled']]
    addresses.extend(d['address'] for d in result['devices'] if d['enabled'] and d['transport'] == 'wifi')
    if len(addresses) != len(set(addresses)):
        raise ValueError('Each enabled hub or Wi-Fi device needs its own IP address.')
    if any(d['id'] in ('solar', 'clock') for d in result['devices']):
        raise ValueError('solar and clock are reserved device IDs.')
    endpoints = set()
    for d in result['devices']:
        endpoint = (d['transport'], d.get('hub_id'), d['node_id']) if d['transport'] == 'zigbee' else ('wifi', d.get('address'))
        if d['enabled'] and endpoint in endpoints:
            raise ValueError('Two enabled devices cannot share the same connection.')
        if d['enabled']:
            endpoints.add(endpoint)
    devices = {d['id']: d for d in result['devices']}
    for row in rows(doc, 'rules', 100):
        rule = {'id': row['id'], 'name': text(row, 'name'), 'enabled': flag(row, 'enabled'), 'match': row.get('match', 'all')}
        if not rule['name'] or rule['match'] not in ('all', 'any'):
            raise ValueError('Name the automation and choose all or any conditions.')
        rule['for_seconds'] = number(row.get('for_seconds', 0), 'Hold duration', 0, 86400)
        rule['cooldown_seconds'] = number(row.get('cooldown_seconds', 60), 'Cooldown', 0, 86400)
        rule['conditions'] = []
        rule['actions'] = []
        for c in rows(row, 'conditions', 20):
            source = c.get('source')
            device = devices.get(source)
            available = definitions(source, device)
            if c.get('metric') not in available:
                raise ValueError('Choose an existing device and a supported reading for each condition.')
            if rule['enabled'] and device and not device['enabled']:
                raise ValueError('Enable source devices before enabling their automations.')
            condition = {k: c.get(k) for k in ('id', 'source', 'metric', 'operator')}
            metric_kind = available[c['metric']]['kind']
            if metric_kind == 'time':
                for key in ('value', 'end'):
                    value = c.get(key)
                    if not isinstance(value, str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', value):
                        raise ValueError('Enter start and end times in HH:MM format.')
                    condition[key] = value
                if condition['value'] == condition['end'] or c.get('operator') != 'between':
                    raise ValueError('Time windows need different start and end times.')
            elif metric_kind == 'boolean':
                if c.get('operator') not in ('is_on', 'is_off'):
                    raise ValueError('State conditions must be true/on or false/off.')
            else:
                if c.get('operator') not in NUMERIC_OPERATORS:
                    raise ValueError('Choose a supported numeric comparison.')
                condition['value'] = number(c.get('value'), 'Threshold', -1e12, 1e12)
                if c['operator'] in ('between', 'outside'):
                    condition['end'] = number(c.get('end'), 'Upper threshold', -1e12, 1e12)
                    if condition['end'] <= condition['value']:
                        raise ValueError('The upper threshold must be greater than the lower threshold.')
            rule['conditions'].append(condition)
        for a in rows(row, 'actions', 20):
            action = {'id': a['id'], 'type': a.get('type')}
            if a.get('type') == 'plug':
                target = devices.get(a.get('device_id'))
                if not target or target['kind'] != 'plug':
                    raise ValueError('Choose an existing plug for each switch action.')
                if rule['enabled'] and not target['enabled']:
                    raise ValueError('Enable target plugs before enabling their automations.')
                action.update(device_id=target['id'], on=flag(a, 'on'))
            elif a.get('type') == 'telegram':
                action['message'] = text(a, 'message', 1000)
                if not action['message']:
                    raise ValueError('Enter a message for the Telegram action.')
            else:
                raise ValueError('Choose a plug or Telegram action.')
            rule['actions'].append(action)
        targets = [a['device_id'] for a in rule['actions'] if a['type'] == 'plug']
        if len(targets) != len(set(targets)):
            raise ValueError('Use one action per plug in an automation.')
        if not rule['conditions'] or not rule['actions']:
            raise ValueError('Every automation needs at least one condition and action.')
        result['rules'].append(rule)
    return result


def public_document(doc):
    result = copy.deepcopy(doc)
    for row in result['hubs'] + result['devices']:
        if row.get('local_key'):
            row['local_key'] = MASK
    return result
