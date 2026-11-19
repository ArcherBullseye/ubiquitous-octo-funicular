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
from .store import load_document, log_event, manual_holds, set_manual_hold


def stamp():
    return datetime.now(timezone.utc).isoformat()


def decoded_values(points, device):
    if device['kind'] == 'plug':
        power = points.get(device.get('power_dp', '1'))
        return {'power': power} if isinstance(power, bool) else {}
    settings = {'home_' + key: device.get(key) for key in (
        'temperature_dp', 'humidity_dp', 'temperature_divisor', 'humidity_divisor', 'temperature_unit')}
    values = decode_readings(points, settings)
    if 'temperature_c' in values:
        values['temperature_f'] = round(values['temperature_c'] * 9 / 5 + 32, 1)
    return values


class ConnectionWorker(threading.Thread):
    def __init__(self, service, connection, devices, revision):
        super().__init__(daemon=True, name='home-connection')
        self.service, self.connection, self.devices = service, connection, devices
        self.revision = revision
        self.stop_event = threading.Event()
        self.commands = queue.Queue(maxsize=30)
        self.root = None
        self.children = {}

    def accept(self, device, response):
        if device['transport'] == 'zigbee' and isinstance(response, dict):
            nested = response.get('data') or {}
            cid = response.get('cid') or (nested.get('cid') if isinstance(nested, dict) else None)
            if cid is not None and cid != device['node_id']:
                self.route(response)
                return {}
        points = data_points(response)
        values = decoded_values(points, device)
        if values:
            self.service.report(device['id'], values)
        return values

    def route(self, response):
        if not isinstance(response, dict):
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
            self.accept(device, response)
            self.route(response)
        except Exception:
            # An unavailable child must not block other devices on its gateway.
            self.service.connection_status(device['id'], 'offline')

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
                if values.get('power') is not command['on']:
                    raise ConnectionError('The plug has not confirmed the requested state. Check the device.')
                self.service.finish_command(device['id'], True, command['origin'], 'Confirmed ' + ('ON' if command['on'] else 'OFF'))
            except Exception:
                self.service.finish_command(device['id'], False, command['origin'], 'Plug command failed or could not be confirmed. Check the device.')

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
                for device in self.devices:
                    if self.stop_event.is_set():
                        break
                    # A sleeping Zigbee child may return no snapshot. Live reports can arrive later.
                    self.poll_device(device)
                heartbeat_at = time.monotonic()
                poll_at = time.monotonic() + 30
                while not self.stop_event.is_set():
                    self.drain_commands()
                    if time.monotonic() >= heartbeat_at:
                        self.root.heartbeat(nowait=True)
                        heartbeat_at = time.monotonic() + 9
                    if time.monotonic() >= poll_at:
                        for device in self.devices:
                            if self.stop_event.is_set():
                                break
                            if device['kind'] == 'plug':
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

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.readings)

    def report(self, device_id, values):
        with self.lock:
            row = self.readings.setdefault(device_id, {'updated_at': {}})
            row.update(values)
            row['status'] = 'connected'
            row['updated_at'].update({key: stamp() for key in values})
            self.publish()

    def connection_status(self, device_id, status):
        with self.lock:
            previous = self.readings.get(device_id, {}).get('status')
            self.readings.setdefault(device_id, {'updated_at': {}})['status'] = status
            self.publish()
        if status == 'offline' and previous != 'offline':
            name = next((d['name'] for d in (self.document or {}).get('devices', []) if d['id'] == device_id), 'Home device')
            log_event('connection', name, 'Local connection lost. Retrying.')

    def publish(self):
        summary = {d['id']: {'name': d['name'], 'kind': d['kind'],
                   **copy.deepcopy(self.readings.get(d['id'], {}))}
                   for d in (self.document or {}).get('devices', [])}
        self.state.set('home_devices', summary)
        devices = (self.document or {}).get('devices', [])
        sensor = next((d for d in devices if d['kind'] == 'sensor'), None)
        if sensor:
            self.state.set('home_sensor', {'name': sensor['name'], 'mapping_configured': True,
                                          **copy.deepcopy(self.readings.get(sensor['id'], {}))})
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
            if not device or device['kind'] != 'plug' or not device['enabled'] or not worker:
                raise ValueError('Enable and connect this plug before switching it.')
            row = self.readings[device_id]
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
                self.readings = {d['id']: {'status': 'waiting' if d['enabled'] else 'disabled',
                                 'updated_at': {}, 'hold_until': holds.get(d['id'], 0)}
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
        solar_readings = self.state.get('readings') or {}
        ts = self.state.get('last_updated')
        solar = {'status': 'connected', **solar_readings,
                 'updated_at': {key: ts for key in solar_readings}}
        self.engine.tick(doc, readings, solar, doc.get('timezone', 'America/Detroit'), self.action, log_event)

    def add_worker(self, connection, devices):
        worker = ConnectionWorker(self, connection, devices, self.document['revision'])
        self.workers.append(worker)
        for d in devices:
            self.device_workers[d['id']] = worker
        worker.start()
