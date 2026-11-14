"""Read-only Tuya gateway connection; no cloud dependency or device writes."""
import math
import time
from datetime import datetime, timezone


def data_points(message):
    if not isinstance(message, dict):
        return {}
    nested = message.get('data')
    points = message.get('dps') or (nested.get('dps') if isinstance(nested, dict) else None)
    return points if isinstance(points, dict) else {}


def is_sensor_report(message, child, node_id):
    if not isinstance(message, dict):
        return False
    nested = message.get('data')
    cid = message.get('cid') or (nested.get('cid') if isinstance(nested, dict) else None)
    return cid == node_id or message.get('device') is child


def decode_readings(points, settings):
    """Mapping must be configured from this sensor's verified DP schema."""
    result = {}
    for metric, minimum, maximum in [('temperature', -60, 125), ('humidity', 0, 100)]:
        dp = str(settings.get('home_' + metric + '_dp') or '')
        raw = points.get(dp)
        if not dp or raw is None or isinstance(raw, bool):
            continue
        try:
            divisor = float(settings.get('home_' + metric + '_divisor', 1))
            value = float(raw) / divisor
            if metric == 'temperature' and settings.get('home_temperature_unit') == 'F':
                value = (value - 32) * 5 / 9
            if math.isfinite(value) and minimum <= value <= maximum:
                result[metric + ('_c' if metric == 'temperature' else '_pct')] = round(value, 1)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    return result


class ZigbeeListener:
    def __init__(self, state, settings_getter, on_temperature=None):
        self.state = state
        self.settings_getter = settings_getter
        self.on_temperature = on_temperature
        self.values = {}
        self.timestamps = {}
        self.identity = None

    def publish(self, settings, status, error=None):
        self.state.set('home_sensor', {
            'name': settings.get('home_sensor_name') or 'Temperature & humidity',
            'status': status, 'error': error,
            'mapping_configured': bool(settings.get('home_temperature_dp') and settings.get('home_humidity_dp')),
            **self.values, 'updated_at': dict(self.timestamps),
        })

    def accept(self, message, settings):
        points = data_points(message)
        if not points:
            return False
        decoded = decode_readings(points, settings)
        stamp = datetime.now(timezone.utc).isoformat()
        self.values.update(decoded)
        for metric in decoded:
            self.timestamps[metric] = stamp
        self.publish(settings, 'connected')
        if (self.on_temperature and 'temperature_c' in decoded
                and not self.state.home_refresh.is_set()):
            self.on_temperature(settings, decoded['temperature_c'])
        return True

    def run_forever(self):
        import tinytuya
        while True:
            self.state.home_refresh.clear()
            settings = self.settings_getter()
            if self.on_temperature:
                self.on_temperature(settings, None)
            identity = tuple(settings.get(k) for k in (
                'home_hub_id', 'home_hub_ip', 'home_hub_key', 'home_sensor_id', 'home_sensor_node',
                'home_temperature_dp', 'home_humidity_dp', 'home_temperature_divisor',
                'home_humidity_divisor', 'home_temperature_unit'))
            if identity != self.identity:
                self.values, self.timestamps = {}, {}
                self.identity = identity
            if not settings.get('home_hub_enabled') or not all(identity[:5]):
                self.publish(settings, 'unconfigured')
                self.state.home_refresh.wait(10)
                continue
            gateway = None
            try:
                gateway = tinytuya.Device(settings['home_hub_id'], settings['home_hub_ip'],
                    settings['home_hub_key'], version=3.4, persist=True,
                    connection_timeout=3, connection_retry_limit=1, connection_retry_delay=0)
                gateway.set_socketTimeout(3)
                child = tinytuya.Device(settings['home_sensor_id'], parent=gateway,
                                       cid=settings['home_sensor_node'])
                self.publish(settings, 'waiting')
                # status() is addressed specifically to this child.
                response = child.status()
                if isinstance(response, dict) and response.get('Err'):
                    raise ConnectionError('Gateway query failed')
                self.accept(response, settings)
                heartbeat_at = time.monotonic()
                query_at = time.monotonic() + 300
                while not self.state.home_refresh.is_set():
                    now = time.monotonic()
                    if now >= heartbeat_at:
                        gateway.heartbeat(nowait=True)
                        heartbeat_at = now + 9
                    if now >= query_at:
                        self.accept(child.status(), settings)
                        query_at = now + 300
                    message = gateway.receive()
                    if isinstance(message, dict) and message.get('Err'):
                        raise ConnectionError('Gateway connection interrupted')
                    if is_sensor_report(message, child, settings['home_sensor_node']):
                        self.accept(message, settings)
            except Exception:
                # Never place library exceptions or the local key in public status.
                self.publish(settings, 'offline', 'Hub unavailable — reconnecting')
                self.state.home_refresh.wait(10)
            finally:
                if gateway is not None:
                    gateway.close()
