"""One socket owner per Tuya gateway or Wi-Fi device, plus rule scheduling."""
import copy
import queue
import threading
import time
from datetime import datetime, timezone
from html import escape

from ..clients.zigbee import data_points, decode_readings
from ..control.notifications import get_bot
from ..db import get_settings
from .engine import RuleEngine
from .metrics import dashboard_readings, numeric
from .ownership import legacy_owner
from .telemetry import decode_telemetry
from .groups import execution_document
from .store import load_document, log_event, manual_holds, set_manual_hold


CHILD_AVAILABILITY_MAX_AGE = 90


def zigbee_powered(device):
    return device.get('transport') == 'zigbee' and device.get('kind') in ('plug', 'dehumidifier')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def decoded_values(points, device):
    if device['kind'] in ('plug', 'dehumidifier'):
        power = points.get(device.get('power_dp', '1'))
        values = {'power': power} if isinstance(power, bool) else {}
        if device['kind'] == 'dehumidifier':
            for metric, dp, divisor in (('humidity_pct', 'humidity_dp', 'humidity_divisor'),
                                        ('target_humidity_pct', 'target_humidity_dp', 'target_humidity_divisor')):
                raw = points.get(device.get(dp, {'humidity_dp':'109', 'target_humidity_dp':'3'}[dp]))
                if numeric(raw):
                    value = raw / device.get(divisor, 1)
                    if 0 <= value <= 100:
                        values[metric] = value
            tank = points.get(device.get('tank_full_dp', '111'))
            if type(tank) is bool:
                values['tank_full'] = tank
        values.update(decode_telemetry(points, device))
        return values
    settings = {'home_' + key: device.get(key) for key in (
        'temperature_dp', 'humidity_dp', 'temperature_divisor', 'humidity_divisor', 'temperature_unit')}
    values = decode_readings(points, settings)
    if 'temperature_c' in values:
        values['temperature_f'] = round(values['temperature_c'] * 9 / 5 + 32, 1)
    values.update(decode_telemetry(points, device))
    return values


class ConnectionWorker(threading.Thread):
    def __init__(self, service, connection, devices, revision):
        super().__init__(daemon=True, name='home-connection')
        self.service, self.connection, self.devices = service, connection, devices
        self.revision = revision
        self.stop_event = threading.Event()
        self.commands = queue.Queue(maxsize=30)
        self.status_requests = queue.Queue(maxsize=1)
        self.root = None
        self.children = {}

    def availability_report(self, response):
        if not isinstance(response, dict) or response.get('reqType') != 'subdev_online_stat_report':
            return False
        data = response.get('data')
        if not isinstance(data, dict):
            return False
        online, offline = data.get('online', []), data.get('offline', [])
        if (not isinstance(online, list) or not isinstance(offline, list)
                or any(not isinstance(cid, str) for cid in online + offline)):
            return False
        online, offline = set(online), set(offline)
        for device in self.devices:
            if not zigbee_powered(device):
                continue
            cid = device['node_id']
            # Replies can be split across frames. Absence from a list is not
            # evidence that an unmentioned device is offline.
            if cid in offline or cid in online:
                self.service.child_availability(device['id'], cid not in offline)
        return True

    def query_availability(self):
        if not any(zigbee_powered(device) for device in self.devices):
            return
        try:
            self.route(self.check(self.root.subdev_query()))
        except Exception:
            # Some firmware does not support this query. A failed/unsupported
            # query must not disconnect the hub or declare every child offline.
            # Previous positive checks expire rather than being refreshed by DPS.
            pass

    def accept(self, device, response):
        if self.availability_report(response):
            return {}
        if device['transport'] == 'zigbee' and isinstance(response, dict):
            nested = response.get('data') or {}
            cid = response.get('cid') or (nested.get('cid') if isinstance(nested, dict) else None)
            if cid is not None and cid != device['node_id']:
                self.route(response)
                return {}
        points = data_points(response)
        if points:
            self.service.report_points(device['id'], points)
        values = decoded_values(points, device)
        if values:
            self.service.report(device['id'], values)
        return values

    def route(self, response):
        if not isinstance(response, dict):
            return
        if self.availability_report(response):
            return
        nested = response.get('data') or {}
        cid = response.get('cid') or (nested.get('cid') if isinstance(nested, dict) else None)
        for device in self.devices:
            child = self.children[device['id']]
            if device['transport'] == 'wifi' or cid == device['node_id'] or response.get('device') is child:
                self.accept(device, response)
                break

    def check(self, response):
        if isinstance(response, dict) and ('Err' in response or 'Error' in response):
            raise ConnectionError('Device did not answer')
        return response

    def poll_device(self, device):
        try:
            response = self.children[device['id']].status()
            self.check(response)
            if self.availability_report(response):
                return
            if device['kind'] in ('plug', 'dehumidifier') and not data_points(response):
                raise ConnectionError('No device report')
            self.accept(device, response)
        except Exception:
            # An unavailable child must not block other devices on its gateway.
            self.service.connection_status(device['id'], 'offline')

    def read_requested_status(self):
        if self.stop_event.is_set(): return
        try:
            requested = self.status_requests.get_nowait()
        except queue.Empty:
            return
        device = next((d for d in self.devices if d['id'] == requested), None)
        if device:
            self.poll_device(device)

    def drain_commands(self):
        while not self.commands.empty() and not self.stop_event.is_set():
            command = self.commands.get_nowait()
            device = next(d for d in self.devices if d['id'] == command['device_id'])
            try:
                if time.time() - command['created'] > 20 or load_document()['revision'] != self.revision:
                    raise ConnectionError('Command expired after a connection or configuration change.')
                child = self.children[device['id']]
                self.check(child.set_value(int(device.get('power_dp', '1')), command['on']))
                # A successful command response alone does not establish the resulting power state.
                values = self.accept(device, self.check(child.status()))
                if (values.get('power') is not command['on']
                        or self.service.snapshot().get(device['id'], {}).get('status') != 'connected'):
                    raise ConnectionError('The device has not confirmed the requested state. Check the device.')
                self.service.finish_command(device['id'], True, command['origin'], 'Confirmed ' + ('ON' if command['on'] else 'OFF'))
            except Exception:
                self.service.finish_command(device['id'], False, command['origin'], 'Power command failed or could not be confirmed. Check the device.')

    def run(self):
        import tinytuya
        while not self.stop_event.is_set():
            try:
                c = self.connection
                self.root = tinytuya.Device(c['device_id'], c['address'], c['local_key'],
                    version=c.get('version', 3.4), persist=True, connection_timeout=2,
                    connection_retry_limit=1, connection_retry_delay=0)
                self.root.set_socketTimeout(1)
                self.children = {}
                for device in self.devices:
                    self.children[device['id']] = (tinytuya.Device(device['device_id'], parent=self.root, cid=device['node_id'])
                        if device['transport'] == 'zigbee' else self.root)
                    self.service.connection_status(device['id'], 'waiting')
                self.query_availability()
                for device in self.devices:
                    if self.stop_event.is_set():
                        break
                    # A sleeping Zigbee child may return no snapshot. Live reports can arrive later.
                    self.poll_device(device)
                heartbeat_at = time.monotonic()
                poll_at = time.monotonic() + 30
                while not self.stop_event.is_set():
                    self.drain_commands()
                    self.read_requested_status()
                    if time.monotonic() >= heartbeat_at:
                        self.root.heartbeat(nowait=True)
                        heartbeat_at = time.monotonic() + 9
                    if time.monotonic() >= poll_at:
                        self.query_availability()
                        for device in self.devices:
                            if self.stop_event.is_set():
                                break
                            if device['kind'] in ('plug', 'dehumidifier'):
                                self.poll_device(device)
                        poll_at = time.monotonic() + 30
                    self.route(self.check(self.root.receive()))
            except Exception:
                for device in self.devices:
                    self.service.connection_status(device['id'], 'offline')
                self.stop_event.wait(5)
            finally:
                if self.root:
                    self.root.close()
        while not self.commands.empty():
            command = self.commands.get_nowait()
            self.service.finish_command(command['device_id'], False, command['origin'], 'Cancelled after configuration changed.')


class HomeService:
    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()
        self.document = None
        self.workers = []
        self.device_workers = {}
        self.engine = RuleEngine()
        self.readings = {}
        self.raw_reports = {}

    def snapshot(self):
        with self.lock:
            readings = copy.deepcopy(self.readings)
            now = time.time()
            for device in (self.document or {}).get('devices', []):
                if not device['enabled'] or not zigbee_powered(device) or device['id'] not in readings:
                    continue
                row = readings[device['id']]
                row['availability_required'] = True
                row['availability_max_age_seconds'] = CHILD_AVAILABILITY_MAX_AGE
                try:
                    age = now - datetime.fromisoformat(row.get('zigbee_online_at')).timestamp()
                except (TypeError, ValueError, OverflowError):
                    age = float('inf')
                if row.get('zigbee_online') is False:
                    row.update(status='offline', availability_reason='Hub reports device offline')
                elif row.get('zigbee_online') is not True or not 0 <= age <= CHILD_AVAILABILITY_MAX_AGE:
                    row.update(status='waiting' if row.get('status') != 'offline' else 'offline',
                               availability_reason='Waiting for hub to confirm device online')
            return readings

    def child_availability(self, device_id, online):
        with self.lock:
            row = self.readings.setdefault(device_id, {'updated_at': {}})
            previous = row.get('zigbee_online')
            row.update(zigbee_online=online, zigbee_online_at=stamp(),
                       status='connected' if online else 'offline')
            self.publish()
        if previous is not online:
            device = next((d for d in (self.document or {}).get('devices', []) if d['id'] == device_id), {})
            log_event('connection', device.get('name', 'Home device'),
                      'Hub reports device online.' if online else 'Hub reports device offline. Cached readings are retained.')

    def report_points(self, device_id, points):
        # Keep only bounded numeric/boolean DP reports. Arbitrary strings/raw
        # blobs can contain credentials or private text and are not exposed.
        safe = {str(dp):value for dp,value in points.items()
                if str(dp).isdigit() and 1 <= int(dp) <= 9999 and (type(value) is bool or numeric(value))}
        if not safe: return
        with self.lock:
            records = self.raw_reports.setdefault(device_id, {})
            received = stamp()
            for dp,value in list(safe.items())[:128]:
                if dp in records or len(records) < 128:
                    records[dp] = dict(dp=dp, value=value, received_at=received)
            row = self.readings.setdefault(device_id, {'updated_at':{}})
            row.update(status='offline' if row.get('zigbee_online') is False else 'connected', last_report_at=received)
            self.publish()

    def device_info(self, device_id):
        with self.lock:
            device = next((d for d in load_document()['devices'] if d['id'] == device_id), None)
            if not device: raise ValueError('Device not found.')
            row = self.snapshot().get(device_id, {})
            raw = sorted(copy.deepcopy(list(self.raw_reports.get(device_id, {}).values())), key=lambda item:int(item['dp']))
            return dict(name=device['name'], readings=row, telemetry=device.get('telemetry', []), raw=raw)

    def refresh_device(self, device_id):
        with self.lock:
            if not self.document or load_document()['revision'] != self.document['revision']:
                raise ValueError('Home is reconnecting. Try again shortly.')
            worker = self.device_workers.get(device_id)
            if not worker: raise ValueError('Enable and connect this device before requesting a report.')
            try:
                worker.status_requests.put_nowait(device_id)
            except queue.Full:
                raise ValueError('A reading request is already waiting. Try again shortly.') from None

    def report(self, device_id, values):
        with self.lock:
            row = self.readings.setdefault(device_id, {'updated_at': {}})
            row.update(values)
            row['status'] = 'offline' if row.get('zigbee_online') is False else 'connected'
            row['updated_at'].update({key: stamp() for key in values})
            self.publish()

    def connection_status(self, device_id, status):
        with self.lock:
            previous = self.readings.get(device_id, {}).get('status')
            row = self.readings.setdefault(device_id, {'updated_at': {}})
            row['status'] = status
            if status in ('offline', 'waiting') and row.get('zigbee_online') is True:
                row['zigbee_online'] = None
            self.publish()
        if status == 'offline' and previous != 'offline':
            name = next((d['name'] for d in (self.document or {}).get('devices', []) if d['id'] == device_id), 'Home device')
            log_event('connection', name, 'Local connection lost. Retrying.')

    def publish(self):
        readings = self.snapshot()
        summary = {d['id']: {'name': d['name'], 'kind': d['kind'],
                   **readings.get(d['id'], {})}
                   for d in (self.document or {}).get('devices', [])}
        devices = (self.document or {}).get('devices', [])
        selected = (self.document or {}).get('dashboard_devices')
        ids = [d['id'] for d in devices if (d['kind'] == 'sensor' if selected is None else d['id'] in selected)]
        self.state.update({'home_devices': summary, 'home_dashboard_device_ids': ids})
        owner = legacy_owner(self.document or {}, get_settings())
        if owner:
            row = readings.get(owner['id'], {})
            connected = row.get('status') == 'connected'
            self.state.update({'dehum_managed_by_home': owner['id'],
                'dehum_power': row.get('power') if connected else None,
                'dehum_humidity': row.get('humidity_pct') if connected else None,
                'dehum_tank_full': row.get('tank_full'),
                'dehum_tank_last_updated': (row.get('updated_at') or {}).get('tank_full'),
                'dehum_last_updated': (row.get('updated_at') or {}).get('power'),
                'dehum_auto_on': False,
                'dehum_error': None if connected else 'Home connection is waiting or offline.'})
        elif self.state.get('dehum_managed_by_home'):
            self.state.update({'dehum_managed_by_home': None, 'dehum_power': None,
                               'dehum_humidity': None, 'dehum_last_updated': None,
                               'dehum_tank_full': None, 'dehum_tank_last_updated': None, 'dehum_auto_on': False, 'dehum_error': None})
        sensor = next((d for d in devices if d['kind'] == 'sensor'), None)
        if sensor:
            self.state.set('home_sensor', {'name': sensor['name'], 'mapping_configured': True,
                                          **readings.get(sensor['id'], {})})
        else:
            self.state.set('home_sensor', None)

    def finish_command(self, device_id, success, origin, detail):
        with self.lock:
            row = self.readings.get(device_id, {})
            row['pending'] = False
            row['command_error'] = None if success else detail
            self.publish()
        log_event('command' if success else 'error', origin, detail)

    def command(self, device_id, on, origin='Manual control', hold_minutes=15, automatic=False):
        with self.lock:
            if self.document is None or load_document()['revision'] != self.document['revision']:
                raise ValueError('The Home workspace is reconnecting. Try again shortly.')
            device = next((d for d in self.document['devices'] if d['id'] == device_id), None)
            worker = self.device_workers.get(device_id)
            if not device or device['kind'] not in ('plug', 'dehumidifier') or not device['enabled'] or not worker:
                raise ValueError('Enable and connect this device before switching it.')
            row = self.readings[device_id]
            if on and device['kind'] == 'dehumidifier' and row.get('tank_full') is True:
                raise ValueError('Empty the dehumidifier tank before turning it on.')
            if automatic and row.get('hold_until', 0) > time.time():
                raise ValueError(f"{device['name']} is under manual control. This action was skipped.")
            if row.get('pending'):
                raise ValueError(f"{device['name']} already has a pending command.")
            hold_until = time.time() + hold_minutes * 60
            if not automatic:
                set_manual_hold(device_id, hold_until)
            try:
                worker.commands.put_nowait({'device_id': device_id, 'on': on, 'origin': origin, 'created': time.time()})
            except queue.Full:
                raise ValueError('The connection is busy. Try again shortly.') from None
            row.update(pending=True, command_error=None)
            if not automatic:
                row['hold_until'] = hold_until
            self.publish()
        log_event('queued', origin, f"{device['name']} → {'ON' if on else 'OFF'}")

    def action(self, action, rule_name):
        if load_document()['revision'] != self.document['revision']:
            raise ValueError('Workspace changed. Action cancelled.')
        if action['type'] == 'plug':
            self.command(action['device_id'], action['on'], rule_name, automatic=True)
            return
        bot = get_bot(get_settings())
        if not bot:
            raise ValueError('Configure the Telegram bot and chat in Alerts.')
        if not bot.send(f'⌂ <b>{escape(rule_name)}</b>\n{escape(action["message"])}'):
            raise ValueError('Telegram delivery failed. Check the bot settings.')
        log_event('telegram', rule_name, 'Message sent.')

    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                print('Home workspace could not complete a cycle. Retrying.')
            self.state.home_refresh.wait(2)
            self.state.home_refresh.clear()

    def tick(self):
        doc = load_document()
        if (self.document is not None and doc['revision'] != self.document['revision']
                and execution_document(doc) == execution_document(self.document)):
            # Presentation settings do not reconnect devices, cancel
            # manual holds, or reset the rule engine's crossing/cooldown memory.
            with self.lock:
                self.document = doc
                for worker in self.workers:
                    worker.revision = doc['revision']
                self.publish()
        if self.document is None or doc['revision'] != self.document['revision']:
            for worker in self.workers:
                worker.stop_event.set()
            if any(worker.is_alive() for worker in self.workers):
                return
            with self.lock:
                self.engine = RuleEngine()
                self.document = doc
                self.device_workers = {}
                self.workers = []
                holds = manual_holds()
                self.raw_reports = {}
                self.readings = {d['id']: {'status': 'waiting' if d['enabled'] else 'disabled',
                                 'updated_at': {}, 'max_age_seconds': 300 if d['kind'] == 'dehumidifier' else 1800,
                                 'hold_until': holds.get(d['id'], 0)}
                                 for d in doc['devices']}
                for hub in doc['hubs']:
                    children = [d for d in doc['devices'] if d['enabled'] and d['transport'] == 'zigbee' and d['hub_id'] == hub['id']]
                    if hub['enabled'] and children:
                        self.add_worker(hub, children)
                for device in doc['devices']:
                    if device['enabled'] and device['transport'] == 'wifi':
                        self.add_worker(device, [device])
                self.publish()
        readings = self.snapshot()
        dashboard = dashboard_readings(self.state.snapshot(), get_settings())
        self.engine.tick(doc, readings, dashboard['solar'], doc.get('timezone', 'America/Detroit'),
                         self.action, log_event, dashboard=dashboard)

    def add_worker(self, connection, devices):
        worker = ConnectionWorker(self, connection, devices, self.document['revision'])
        self.workers.append(worker)
        for d in devices:
            self.device_workers[d['id']] = worker
        worker.start()
