"""LAN/account discovery with private candidates and transactional, additive import."""
import copy
import json
import threading
import time
import uuid

from . import tuya_account
from .telemetry import schema_readings, primary_dps
from .lan_discovery import local_ip, network_range, scan_network
from .store import ConflictError, load_document, save_document

GATEWAYS = {'wgsxj', 'lyqwg', 'bywg', 'zigbee', 'wg2', 'dgnzk', 'videohub', 'xnwg', 'qtyycp', 'alexa_yywg', 'gywg', 'cnwg', 'wfcon'}
TEMP_CODES = {'temp_current', 'va_temperature', 'prm_temperature'}
HUMID_CODES = {'humidity_value', 'va_humidity', 'prm_content', 'humidity_indoor', 'humidity_current'}
TARGET_CODES = {'humidity_set', 'dehumidity_set_value'}


def mapping(raw):
    if raw.get('product_id') == 'vtA4pDd6PLUZzXgZ':
        return 'sensor', dict(temperature_dp='103', humidity_dp='101', temperature_divisor=1,
                              humidity_divisor=1, temperature_unit='C')
    metrics, switches, extra_switches, tank_dp = {}, [], False, ''
    for relation in raw.get('relations') or []:
        if relation.get('supportLocal') is not True:
            continue
        dp = str(relation.get('dpId', ''))
        if not dp.isdigit() or not 1 <= int(dp) <= 9999:
            continue
        code = relation.get('statusCode') or relation.get('dpCode')
        kind = str(relation.get('valueType', '')).lower()
        # Conversion expressions require a model-specific decoder; don't guess.
        if relation.get('valueConvert') not in (None, '', 'default'):
            continue
        if isinstance(code, str) and code.startswith('switch_') and code != 'switch_1':
            extra_switches = True
        if code in ('switch', 'switch_1') and kind in ('bool', 'boolean'):
            switches.append(dp)
        if code in ('water_full', 'tank_full') and kind in ('bool', 'boolean'):
            tank_dp = dp
        if code in TEMP_CODES | HUMID_CODES | TARGET_CODES and kind in ('integer', 'value'):
            try:
                desc = relation.get('valueDesc') or {}
                desc = json.loads(desc) if isinstance(desc, str) else desc
                scale = desc.get('scale')
                if type(scale) is not int or not 0 <= scale <= 4:
                    continue
                unit = str(desc.get('unit', '')).replace('°', '').replace('℃', 'C').replace('℉', 'F').upper()
                if code in TEMP_CODES and unit not in ('C', 'F'):
                    continue
                metrics['temperature' if code in TEMP_CODES else 'target' if code in TARGET_CODES else 'humidity'] = (dp, 10 ** scale, unit)
            except (ValueError, TypeError, AttributeError):
                continue
    if raw.get('category') == 'cs':
        if len(switches) != 1 or extra_switches:
            return None, {}
        humidity = metrics.get('humidity', ('', 1, ''))
        target = metrics.get('target', ('', 1, ''))
        return 'dehumidifier', dict(power_dp=switches[0], humidity_dp=humidity[0],
            humidity_divisor=humidity[1], target_humidity_dp=target[0],
            target_humidity_divisor=target[1], tank_full_dp=tank_dp)
    if 'temperature' in metrics and 'humidity' in metrics:
        t, h = metrics['temperature'], metrics['humidity']
        return 'sensor', dict(temperature_dp=t[0], temperature_divisor=t[1], temperature_unit=t[2],
                              humidity_dp=h[0], humidity_divisor=h[1])
    if len(switches) == 1 and not extra_switches:
        return 'plug', {'power_dp': switches[0]}
    return None, {}


def catalog(raw_devices, lan, document):
    existing = {d['device_id'] for d in document['hubs'] + document['devices']}
    unique = {str(d['id']): d for d in raw_devices}
    # The node ID also identifies a child when an account doesn't send `sub`.
    gateways = [d for d in unique.values() if not d.get('sub') and not d.get('node_id') and d.get('category') in GATEWAYS]
    gateway_ids = {str(d['id']) for d in gateways} | {h['device_id'] for h in document['hubs']}
    result = []
    for device_id, raw in unique.items():
        is_hub = device_id in gateway_ids
        child = bool(raw.get('sub') or raw.get('node_id')) and not is_hub
        kind, points = ('hub', {}) if is_hub else mapping(raw)
        if not is_hub:
            used = primary_dps(dict(kind=kind, **points))
            points['telemetry'] = schema_readings(raw, used)
        parent = ''
        if child:
            explicit = raw.get('gateway_id') or raw.get('gatewayId') or raw.get('parent_id')
            candidates = [g for g in gateways if str(g['id']) == explicit]
            if not candidates:
                candidates = [g for g in gateways if raw.get('local_key') and g.get('local_key') == raw['local_key']]
            if not candidates:
                candidates = [g for g in gateways if g.get('home_id') == raw.get('home_id')]
            if len(candidates) == 1:
                parent = str(candidates[0]['id'])
            elif explicit in gateway_ids:
                parent = explicit
        found = lan.get('matches', {}).get(device_id, {})
        prior = next((h for h in document['hubs'] if h['device_id'] == device_id), {})
        address = found.get('address') or local_ip(raw.get('ip', '')) or prior.get('address', '')
        version_known = bool(found.get('version') or prior.get('version'))
        try:
            version = float(found.get('version') or prior.get('version') or 3.4)
        except (TypeError, ValueError):
            version = 3.4
        if version not in (3.3, 3.4, 3.5):
            version, version_known = 3.4, False
        key = raw.get('local_key') or ''
        result.append(dict(id=device_id, name=str(raw.get('name') or device_id)[:120], kind=kind,
            transport='zigbee' if child else 'wifi', hub_device_id=parent,
            node_id=str(raw.get('node_id') or ''), address=address, version=version, version_known=version_known,
            local_key=key if len(key) == 16 else '', existing=device_id in existing,
            on_lan=address in lan.get('reachable', []) if address else False, **points))
    return result


def public_candidates(candidates):
    result = []
    for item in candidates:
        public = {key: value for key, value in item.items() if key != 'local_key'}
        public['has_key'] = bool(item['local_key'])
        public['supported'] = item['kind'] is not None
        result.append(public)
    return result


class DiscoveryService:
    def __init__(self, home, state):
        self.home = home
        self.state = state
        self.lock = threading.RLock()
        self.busy = False
        self.phase = 'idle'
        self.error = None
        self.lan = {'matches': {}, 'reachable': [], 'warnings': []}
        self.pending = None
        self.qr_image = None
        self.account = tuya_account.read_account()
        self.remember = self.account is not None
        self.candidates = []
        self.expires = 0
        self.batch = None
        self.warnings = []

    def _expire(self):
        if self.expires and time.time() > self.expires and not self.busy:
            self.pending = self.qr_image = self.batch = None
            self.candidates = []
            self.phase = 'expired'
            self.expires = 0

    def snapshot(self):
        with self.lock:
            self._expire()
            return dict(phase=self.phase, busy=self.busy, error=self.error,
                connected=self.account is not None, remembered=self.remember,
                qr_image=self.qr_image, batch=self.batch,
                lan=dict(count=len(self.lan['reachable']), reachable=self.lan['reachable']),
                warnings=self.lan['warnings'] + self.warnings,
                candidates=public_candidates(self.candidates))

    def _start(self, phase, operation):
        with self.lock:
            if self.busy:
                raise ValueError('Discovery is still working. Wait for it to finish.')
            self._expire()
            self.busy, self.phase, self.error = True, phase, None
            def run():
                try:
                    operation()
                except tuya_account.AccountError as exc:
                    with self.lock:
                        self.error = str(exc)
                except Exception:
                    # SDK exceptions may contain credentials. Keep them out of UI/logs.
                    with self.lock:
                        self.error = 'Discovery could not complete. Check the network connection or reconnect your Tuya account, then try again.'
                finally:
                    with self.lock:
                        self.busy = False
            threading.Thread(target=run, name='home-discovery', daemon=True).start()

    def scan(self, subnet):
        network = network_range(subnet)
        def operation():
            found = scan_network(network, load_document(), self.home.snapshot())
            with self.lock:
                self.lan = found
                self.candidates, self.batch = [], None
                self.phase = 'network_ready'
        self._start('scanning', operation)

    def signin(self, user_code, remember):
        if not isinstance(remember, bool):
            raise ValueError('Remember account must be true or false.')
        def operation():
            pending, qr = tuya_account.begin_signin(user_code)
            with self.lock:
                self.pending, self.qr_image = pending, qr
                self.remember = remember
                self.expires = time.time() + 180
                self.phase = 'awaiting_signin'
        self._start('signing_in', operation)

    def _sync(self):
        def on_token(account):
            if self.remember:
                tuya_account.write_account(account)
        devices, warnings = tuya_account.fetch_devices(self.account, on_token)
        candidates = catalog(devices, self.lan, load_document())
        with self.lock:
            self.candidates, self.warnings = candidates, warnings
            self.batch = uuid.uuid4().hex
            self.expires = time.time() + 1200
            self.phase = 'ready'

    def confirm(self):
        with self.lock:
            self._expire()
            if not self.pending:
                raise ValueError('Request a new QR code first.')
            pending = self.pending
        def operation():
            account = tuya_account.confirm_signin(pending)
            if self.remember:
                tuya_account.write_account(account)
            else:
                tuya_account.forget_account()
            with self.lock:
                self.account, self.pending, self.qr_image = account, None, None
            self._sync()
        self._start('loading_account', operation)

    def sync(self):
        if not self.account:
            raise ValueError('Connect your Tuya account first.')
        self._start('loading_devices', self._sync)

    def disconnect(self):
        with self.lock:
            if self.busy:
                raise ValueError('Wait for discovery to finish before disconnecting.')
            tuya_account.forget_account()
            self.account = self.pending = self.qr_image = self.batch = None
            self.candidates = []
            self.remember, self.phase = False, 'network_ready'

    def update_measurements(self, data):
        with self.lock:
            self._expire()
            if self.busy or not self.batch or data.get('batch') != self.batch:
                raise ValueError('Load account devices again before updating measurements.')
            doc = load_document()
            if data.get('revision') != doc['revision']:
                raise ConflictError('The workspace changed. Reload before updating measurements.')
            available = {item['id']:item for item in self.candidates}
            changed = 0
            for device in doc['devices']:
                candidate = available.get(device['device_id'])
                if not candidate: continue
                existing = {item['id']:item for item in device.get('telemetry', [])}
                before = copy.deepcopy(existing)
                for item in candidate.get('telemetry', []):
                    if item['dp'] in primary_dps(device):
                        continue
                    if item['id'] not in existing or existing[item['id']].get('origin') == 'tuya':
                        existing[item['id']] = item
                if existing != before:
                    device['telemetry'] = list(existing.values())
                    changed += 1
            if not changed:
                raise ValueError('No additional measurement mappings were found. Existing manual mappings were preserved.')
            saved = save_document(doc)
            self.state.home_refresh.set()
            return saved, changed

    def import_devices(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('items'), list) or not 1 <= len(data['items']) <= 200:
            raise ValueError('Select the devices to import.')
        with self.lock:
            self._expire()
            if self.busy or not self.batch or data.get('batch') != self.batch:
                raise ValueError('Scan and load your devices again before importing.')
            doc = load_document()
            if data.get('revision') != doc['revision']:
                raise ConflictError('The workspace changed. Reload it before importing.')
            available = {c['id']: c for c in self.candidates}
            choices = {}
            for row in data['items']:
                if not isinstance(row, dict) or row.get('id') not in available or row['id'] in choices:
                    raise ValueError('Choose each discovered device only once.')
                item = copy.deepcopy(available[row['id']])
                if not item['kind']:
                    raise ValueError('This model needs manual setup; its local mapping is unavailable.')
                # Only LAN address and parent selection are editable in the review.
                if item['transport'] == 'wifi':
                    address = row.get('address', item['address'])
                    if address and not local_ip(address):
                        raise ValueError('Enter a private LAN IP address for each hub or Wi-Fi device.')
                    item['address'] = local_ip(address) or ''
                    if 'version' in row:
                        if type(row['version']) not in (int, float) or row['version'] not in (3.3, 3.4, 3.5):
                            raise ValueError('Choose Tuya protocol 3.3, 3.4, or 3.5.')
                        item['version'], item['version_known'] = row['version'], True
                if item['transport'] == 'zigbee':
                    item['hub_device_id'] = row.get('hub_device_id') or item['hub_device_id']
                choices[item['id']] = item
            # Include a selected child's account hub when it isn't already configured.
            known_hubs = {h['device_id']: h for h in doc['hubs']}
            for item in list(choices.values()):
                parent = item.get('hub_device_id')
                if item['transport'] == 'zigbee' and parent not in known_hubs:
                    if parent not in available or available[parent]['kind'] != 'hub':
                        raise ValueError('Choose a hub for every Zigbee device.')
                    if parent not in choices:
                        choices[parent] = copy.deepcopy(available[parent])
            existing = {d['device_id'] for d in doc['devices'] + doc['hubs']}
            imported, disabled = 0, 0
            for item in sorted(choices.values(), key=lambda c: c['kind'] != 'hub'):
                if item['id'] in existing:
                    continue
                common = dict(id='import-' + uuid.uuid4().hex, name=item['name'], device_id=item['id'])
                if item['kind'] == 'hub':
                    row = dict(common, address=item['address'], local_key=item['local_key'], version=item['version'])
                    row['enabled'] = bool(row['address'] and row['local_key'] and item['version_known'])
                    doc['hubs'].append(row)
                    known_hubs[item['id']] = row
                else:
                    row = dict(common, kind=item['kind'], transport=item['transport'], node_id=item['node_id'],
                        hub_id='', power_dp=item.get('power_dp', '1'), temperature_dp=item.get('temperature_dp', '103'),
                        humidity_dp=item.get('humidity_dp', '101'), temperature_divisor=item.get('temperature_divisor', 1),
                        humidity_divisor=item.get('humidity_divisor', 1), temperature_unit=item.get('temperature_unit', 'C'))
                    row['telemetry'] = item.get('telemetry', [])
                    if item['kind'] == 'dehumidifier':
                        row.update(target_humidity_dp=item.get('target_humidity_dp', ''),
                                   target_humidity_divisor=item.get('target_humidity_divisor', 1),
                                   tank_full_dp=item.get('tank_full_dp', ''))
                    if item['transport'] == 'zigbee':
                        hub = known_hubs.get(item['hub_device_id'])
                        row['hub_id'] = hub['id'] if hub else ''
                        row['enabled'] = bool(hub and hub['enabled'] and row['node_id'])
                    else:
                        row.update(address=item['address'], local_key=item['local_key'], version=item['version'])
                        row['enabled'] = bool(row['address'] and row['local_key'] and item['version_known'])
                    doc['devices'].append(row)
                existing.add(item['id'])
                imported += 1
                disabled += not row['enabled']
            if not imported:
                raise ValueError('These devices are already configured. Existing settings were preserved.')
            saved = save_document(doc)
            self.state.home_refresh.set()
            for candidate in self.candidates:
                candidate['existing'] = candidate['id'] in existing
            return saved, imported, disabled
