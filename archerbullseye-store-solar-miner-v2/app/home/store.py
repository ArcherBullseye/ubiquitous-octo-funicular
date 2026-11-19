"""Transactional Home configuration, migration, and activity history."""
import json
from contextlib import closing

from ..db import _connect, _utcnow, get_settings
from .schema import validate


class ConflictError(Exception):
    pass


def legacy_document(settings):
    doc = {'revision': 0, 'timezone': 'America/Detroit', 'hubs': [], 'devices': [], 'rules': []}
    if not settings.get('home_sensor_id'):
        return doc
    doc['hubs'] = [{'id': 'original-hub', 'name': 'G08Pro', 'enabled': bool(settings['home_hub_enabled']),
                    'address': settings['home_hub_ip'], 'device_id': settings['home_hub_id'],
                    'local_key': settings['home_hub_key'], 'version': 3.4}]
    sensor = {'id': 'original-sensor', 'name': settings['home_sensor_name'], 'kind': 'sensor',
              'transport': 'zigbee', 'hub_id': 'original-hub', 'enabled': bool(settings['home_hub_enabled']),
              'device_id': settings['home_sensor_id'], 'node_id': settings['home_sensor_node']}
    for key in ('temperature_dp', 'humidity_dp', 'temperature_divisor', 'humidity_divisor', 'temperature_unit'):
        sensor[key] = settings['home_' + key]
    doc['devices'].append(sensor)
    if settings.get('home_temperature_alert_enabled'):
        for side, operator in (('low', 'below'), ('high', 'above')):
            doc['rules'].append({'id': 'original-' + side, 'name': f'Temperature {side} alert',
                'enabled': bool(settings['home_hub_enabled']), 'match': 'all', 'for_seconds': 0, 'cooldown_seconds': 60,
                'conditions': [{'id': side + '-condition', 'source': sensor['id'], 'metric': 'temperature_f',
                                'operator': operator, 'value': settings['home_temperature_alert_' + side + '_f']}],
                'actions': [{'id': side + '-message', 'type': 'telegram',
                             'message': f"{sensor['name']} temperature is {operator} the configured limit."}]})
    return doc


def init_home_store():
    doc = legacy_document(get_settings())
    with closing(_connect()) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS home_workspace (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS home_manual_holds (device_id TEXT PRIMARY KEY, until REAL NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS home_events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, detail TEXT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO home_workspace VALUES (1, ?)', (json.dumps(doc),))


def load_document():
    with closing(_connect()) as conn, conn:
        return json.loads(conn.execute('SELECT document FROM home_workspace WHERE id=1').fetchone()[0])


def save_document(candidate):
    with closing(_connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = json.loads(conn.execute('SELECT document FROM home_workspace WHERE id=1').fetchone()[0])
        if not isinstance(candidate, dict) or candidate.get('revision') != old['revision']:
            raise ConflictError('This workspace changed in another window. Reload before saving.')
        doc = validate(candidate, old)
        doc['revision'] += 1
        conn.execute('UPDATE home_workspace SET document=? WHERE id=1', (json.dumps(doc),))
    return doc


def log_event(kind, name, detail):
    with closing(_connect()) as conn, conn:
        conn.execute('INSERT INTO home_events(ts,kind,name,detail) VALUES (?,?,?,?)',
                     (_utcnow(), kind, name[:120], detail[:1500]))


def recent_events():
    with closing(_connect()) as conn, conn:
        return [dict(r) for r in conn.execute('SELECT * FROM home_events ORDER BY id DESC LIMIT 100')]


def set_manual_hold(device_id, until):
    with closing(_connect()) as conn, conn:
        conn.execute('INSERT OR REPLACE INTO home_manual_holds VALUES (?, ?)', (device_id, until))


def manual_holds():
    with closing(_connect()) as conn, conn:
        return {row['device_id']: row['until'] for row in conn.execute('SELECT * FROM home_manual_holds')}
