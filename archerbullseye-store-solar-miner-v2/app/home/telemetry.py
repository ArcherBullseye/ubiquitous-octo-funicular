"""Read-only Tuya measurements, with explicit units/scales and no guessed DP IDs."""
import json
import math
import re

PRESETS = {
    'power_w': ('Power draw', 'W'), 'current_a': ('Current', 'A'),
    'voltage_v': ('Voltage', 'V'), 'energy_kwh': ('Energy total', 'kWh'),
    'energy_increment_kwh': ('Last energy increment', 'kWh'), 'battery_pct': ('Battery', '%'),
}
CODES = {
    'cur_power': ('power_w', 'W'), 'cur_current': ('current_a', 'A'),
    'cur_voltage': ('voltage_v', 'V'), 'total_energy': ('energy_kwh', 'kWh'),
    'total_ele': ('energy_kwh', 'kWh'), 'total_forward_energy': ('energy_kwh', 'kWh'),
    # Tuya add_ele reports increments; it must never be presented as a total.
    'add_ele': ('energy_increment_kwh', 'kWh'),
    'battery_percentage': ('battery_pct', '%'), 'battery_percent': ('battery_pct', '%'),
}
FACTORS = {'W': {'w':1,'mw':0.001,'kw':1000}, 'A': {'a':1,'ma':0.001},
           'V': {'v':1,'mv':0.001}, 'kWh': {'kwh':1,'wh':0.001}, '%': {'%':1}}
LABELS = {'compressor_state':'Compressor running', 'compressor':'Compressor running',
          'defrost':'Defrosting', 'defrosting':'Defrosting', 'fault':'Fault code',
          'fan_speed':'Fan speed', 'fan_level':'Fan speed', 'mode':'Operating mode',
          'child_lock':'Child lock', 'switch_led':'Indicator light'}


def primary_dps(device):
    fields = {'plug':('power_dp',), 'sensor':('temperature_dp','humidity_dp'),
              'dehumidifier':('power_dp','humidity_dp','target_humidity_dp','tank_full_dp')}.get(device.get('kind'), ())
    return {device.get(key) for key in fields if device.get(key)}


def schema_readings(raw, used_dps=()):
    readings = []
    ids, dps = set(), set(str(value) for value in used_dps if value)
    for relation in raw.get('relations') or []:
        if not isinstance(relation, dict) or relation.get('supportLocal') is not True or relation.get('valueConvert') not in (None, '', 'default'):
            continue
        dp, code = str(relation.get('dpId', '')), relation.get('statusCode') or relation.get('dpCode')
        if not dp.isdigit() or not 1 <= int(dp) <= 9999 or dp in dps or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,60}', code):
            continue
        kind = str(relation.get('valueType', '')).lower()
        if code in CODES and kind not in ('integer','value'): continue
        try:
            desc = relation.get('valueDesc') or {}
            desc = json.loads(desc) if isinstance(desc, str) else desc
            if not isinstance(desc, dict): continue
        except (ValueError, TypeError):
            continue
        canonical, unit = CODES.get(code, ('extra_' + code, str(desc.get('unit', ''))[:24]))
        item = dict(id=canonical, dp=dp, label=PRESETS[canonical][0] if canonical in PRESETS else LABELS.get(code,code.replace('_',' ').capitalize()),
                    kind='number', unit=unit, divisor=1, origin='tuya')
        if kind in ('integer', 'value', 'bitmap'):
            scale = desc.get('scale', 0 if kind == 'bitmap' else None)
            if type(scale) is not int or not 0 <= scale <= 6: continue
            factor = 1
            if code in CODES:
                factor = FACTORS[unit].get(str(desc.get('unit', '')).lower())
                if factor is None: continue
            item['divisor'] = (10 ** scale) / factor
        elif kind in ('bool', 'boolean'):
            item.update(kind='boolean', unit='')
        elif kind == 'enum':
            values = desc.get('range')
            if not isinstance(values, list) or not 1 <= len(values) <= 50 or not all(isinstance(v,str) and len(v)<=64 for v in values): continue
            item.update(kind='enum', unit='', values=values)
        else:
            # Arbitrary strings, raw blobs, and credentials are never displayed.
            continue
        if item['id'] in ids: continue
        readings.append(item); ids.add(item['id']); dps.add(dp)
        if len(readings) == 32: break
    return readings


def validate_telemetry(value):
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError('Use up to 32 additional device readings.')
    result, ids = [], set()
    for raw in value:
        if not isinstance(raw, dict): raise ValueError('Invalid device reading.')
        key, dp = raw.get('id'), raw.get('dp')
        if not isinstance(key,str) or (key not in PRESETS and not re.fullmatch(r'extra_[A-Za-z0-9_]{1,64}',key)) or key in ids:
            raise ValueError('Use unique IDs for additional device readings.')
        if not isinstance(dp,str) or not dp.isdigit() or not 1 <= int(dp) <= 9999:
            raise ValueError('Additional reading DP IDs must be positive numbers.')
        label, unit = raw.get('label'), raw.get('unit','')
        if not isinstance(label,str) or not 1 <= len(label.strip()) <= 100 or not isinstance(unit,str) or len(unit)>24:
            raise ValueError('Name each additional reading and use a short unit.')
        kind, divisor = raw.get('kind','number'), raw.get('divisor',1)
        if kind not in ('number','boolean','enum') or type(divisor) not in (float,int) or not math.isfinite(divisor) or not 0 < divisor <= 1e12:
            raise ValueError('Choose a supported reading type and a positive divisor.')
        item = dict(id=key,dp=dp,label=label.strip(),unit=unit,kind=kind,divisor=divisor,
                    origin='tuya' if raw.get('origin') == 'tuya' else 'manual')
        if key in PRESETS and (kind != 'number' or unit != PRESETS[key][1]):
            raise ValueError('Use the correct numeric unit for electrical and battery measurements.')
        if kind == 'enum':
            values = raw.get('values')
            if not isinstance(values,list) or not 1 <= len(values) <= 50 or not all(isinstance(v,str) and len(v)<=64 for v in values):
                raise ValueError('Enum readings need their supported values.')
            item['values'] = values
        result.append(item); ids.add(key)
    return result


def decode_telemetry(points, device):
    values = {}
    for item in device.get('telemetry', []):
        raw = points.get(item['dp'])
        if item['kind'] == 'number':
            if type(raw) not in (float,int) or not math.isfinite(raw): continue
            value = raw / item['divisor']
            if not math.isfinite(value): continue
            values[item['id']] = value
        elif item['kind'] == 'boolean' and type(raw) is bool:
            values[item['id']] = raw
        elif item['kind'] == 'enum' and isinstance(raw,str) and raw in item['values']:
            values[item['id']] = raw
    return values
