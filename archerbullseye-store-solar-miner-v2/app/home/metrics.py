"""Condition catalogue and timestamped readings shared by preview and execution."""
import math
from datetime import datetime

from ..control.ramp import charge_reserve_w


def metric(label, unit='', default=0, kind='number', help=''):
    return dict(label=label, unit=unit, default=default, kind=kind, help=help)


DEVICE_METRICS = {
    'sensor': {'temperature_f': metric('Temperature (°F)', '°F', 40),
               'humidity_pct': metric('Humidity (%)', '%', 60)},
    'plug': {'power': metric('Power state', kind='boolean')},
    'dehumidifier': {'power': metric('Power state', kind='boolean'),
        'humidity_pct': metric('Room humidity (%)', '%', 60),
        'target_humidity_pct': metric('Target humidity (%)', '%', 50),
        'tank_full': metric('Tank full', kind='boolean')},
}
SOURCES = {
    'solar': dict(label='Solar, battery & power', metrics={
        'soc': metric('Battery SOC (%)', '%', 95),
        'excess_power_w': metric('Excess power after charge reserve (W)', 'W', 1000,
            help='Grid flow + battery charging − charging reserve. Negative means a deficit. This is additional power available after existing loads.'),
        'input_power_w': metric('Solar production (W)', 'W', 3000),
        'load_power_w': metric('House load (W)', 'W', 1000),
        'backup_power_w': metric('Mining / backup circuit draw (W)', 'W', 2000),
        'battery_power_w': metric('Battery power · + charge / − discharge (W)', 'W', 0),
        'battery_charge_w': metric('Battery charging (W)', 'W', 500),
        'battery_discharge_w': metric('Battery discharging (W)', 'W', 500),
        'grid_power_w': metric('Grid flow · + export / − import (W)', 'W', 0),
        'grid_export_w': metric('Grid export (W)', 'W', 500),
        'grid_import_w': metric('Grid import (W)', 'W', 100),
        'charge_reserve_w': metric('Battery charging reserve (W)', 'W', 500),
        'effective_soc_on': metric('Smart Start SOC threshold (%)', '%', 80),
        'smart_min_pv_w': metric('Smart Start minimum solar (W)', 'W', 1000),
        'eod_target': metric('End-of-day SOC target (%)', '%', 95),
        'smart_start_active': metric('Smart Start active', kind='boolean'),
        'smart_hold_active': metric('Smart Start held', kind='boolean'),
        'eod_projected_with': metric('End-of-day SOC with miners (%)', '%', 95),
        'eod_projected_without': metric('End-of-day SOC without miners (%)', '%', 95),
        'eod_protecting': metric('End-of-day battery protection active', kind='boolean'),
        'ramp_current_w': metric('Miner ramp current draw (W)', 'W', 2000),
        'ramp_dry_run': metric('Miner ramp in dry-run mode', kind='boolean'),
        'ramp_target_w': metric('Miner ramp target (W)', 'W', 2000),
    }),
    'dashboard:weather': dict(label='Weather & forecast', metrics={
        'current_temp_f': metric('Outdoor temperature (°F)', '°F', 40),
        'current_radiation_w': metric('Solar radiation (W/m²)', 'W/m²', 300),
        'remaining_sunny_hours': metric('Sunny hours remaining (h)', 'h', 3),
        'next_hour_temperature_f': metric('Next forecast hour temperature (°F)', '°F', 40),
        'next_hour_radiation_w': metric('Next forecast hour radiation (W/m²)', 'W/m²', 300),
    }),
    'dashboard:mining': dict(label='Mining overview', metrics={
        'running': metric('Any reachable miner running', kind='boolean'),
        'hashrate_ths': metric('Total reachable miner hashrate (TH/s)', 'TH/s', 50),
    }),
    'dashboard:pool': dict(label='Pool, earnings & Bitcoin', metrics={
        'hashrate_ths': metric('Pool live hashrate (TH/s)', 'TH/s', 50),
        'hashrate_24h_ths': metric('Pool 24-hour hashrate (TH/s)', 'TH/s', 50),
        'sats_today': metric('Earnings today (sats)', 'sats', 10000),
        'unpaid_sats': metric('Unpaid balance (sats)', 'sats', 100000),
        'btc_price_usd': metric('Bitcoin price (USD)', 'USD', 100000),
        'today_usd': metric('Earnings today (USD)', 'USD', 1),
        'unpaid_usd': metric('Unpaid balance (USD)', 'USD', 10),
    }),
    'dashboard:dehumidifier': dict(label='Dehumidifier', metrics={
        'power': metric('Power state', kind='boolean'),
        'humidity': metric('Humidity (%)', '%', 60),
        'tank_full': metric('Tank full', kind='boolean'),
        'auto_on': metric('Automatic operation active', kind='boolean'),
    }),
}
for label in ('X1', 'X2'):
    SOURCES['dashboard:miner:' + label] = dict(label='Miner ' + label, metrics={
        'running': metric('Running', kind='boolean'),
        'reachable': metric('Reachable', kind='boolean'),
        'hashrate_ths': metric('Hashrate (TH/s)', 'TH/s', 50),
        'hold': metric('Held until midnight', kind='boolean'),
    })
SOURCES['clock'] = dict(label='Clock', metrics={'time': metric('Local time window', kind='time')})


def definitions(source, device=None):
    if not device:
        return SOURCES.get(source, {}).get('metrics', {})
    result = dict(DEVICE_METRICS.get(device['kind'], {}))
    for item in device.get('telemetry', []):
        if item['kind'] != 'enum':
            result[item['id']] = metric(item['label'] + (' ('+item['unit']+')' if item['unit'] else ''), item['unit'], kind=item['kind'])
    return result


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def row(max_age=300):
    return dict(status='connected', updated_at={}, max_age_seconds=max_age)


def put(target, key, value, stamp):
    if stamp and (type(value) is bool or numeric(value)):
        target[key] = value
        target['updated_at'][key] = stamp


def older_stamp(*stamps):
    try:
        return min(stamps, key=lambda stamp: datetime.fromisoformat(stamp).timestamp())
    except (TypeError, ValueError):
        return None


def dashboard_readings(snapshot, settings):
    """Use the source's last successful poll; reading the workspace never renews it."""
    result = {source: row() for source in SOURCES if source != 'clock'}
    solar = result['solar']
    power = snapshot.get('readings') or {}
    ts = snapshot.get('last_updated')
    for key in ('soc', 'input_power_w', 'load_power_w', 'backup_power_w', 'battery_power_w', 'grid_power_w'):
        put(solar, key, power.get(key), ts)
    for raw, positive, negative in (('grid_power_w', 'grid_export_w', 'grid_import_w'),
                                    ('battery_power_w', 'battery_charge_w', 'battery_discharge_w')):
        value = power.get(raw)
        if numeric(value):
            put(solar, positive, max(0, value), ts)
            put(solar, negative, max(0, -value), ts)
    if all(numeric(power.get(key)) for key in ('soc', 'grid_power_w', 'battery_power_w')):
        reserve = charge_reserve_w(settings, power['soc'], snapshot.get('weather'))
        # When a charge reserve depends on forecast hours, that forecast must
        # also be fresh. At the charge target no forecast is needed (reserve=0).
        reserve_ts = ts
        if reserve > 0:
            weather_ts = snapshot.get('weather_last_updated')
            try:
                if not 0 <= datetime.fromisoformat(ts).timestamp() - datetime.fromisoformat(weather_ts).timestamp() <= 3600:
                    reserve_ts = None
            except (TypeError, ValueError):
                reserve_ts = None
        put(solar, 'charge_reserve_w', reserve, reserve_ts)
        put(solar, 'excess_power_w', power['grid_power_w'] + power['battery_power_w'] - reserve, reserve_ts)
    for key in ('effective_soc_on', 'smart_min_pv_w', 'eod_target', 'smart_start_active', 'smart_hold_active',
                'eod_projected_with', 'eod_projected_without', 'eod_protecting'):
        put(solar, key, snapshot.get(key), ts)
    ramp = snapshot.get('ramp') or {}
    for metric_id, field in (('ramp_target_w', 'target_total_w'), ('ramp_current_w', 'current_total_w'), ('ramp_dry_run', 'dry_run')):
        put(solar, metric_id, ramp.get(field), snapshot.get('ramp_last_updated'))
    weather = snapshot.get('weather') or {}
    weather_row = result['dashboard:weather']
    weather_row['max_age_seconds'] = 3600
    weather_ts = snapshot.get('weather_last_updated')
    for key in ('current_temp_f', 'current_radiation_w', 'remaining_sunny_hours'):
        put(weather_row, key, weather.get(key), weather_ts)
    hourly = weather.get('hourly') or []
    if len(hourly) > 1:
        put(weather_row, 'next_hour_temperature_f', hourly[1].get('temp_f'), weather_ts)
        put(weather_row, 'next_hour_radiation_w', hourly[1].get('radiation_w'), weather_ts)
    mining_ts = snapshot.get('miners_last_updated')
    miners = snapshot.get('miners') or {}
    rates = []
    for label in ('X1', 'X2'):
        miner = miners.get(label) or {}
        target = result['dashboard:miner:' + label]
        put(target, 'reachable', miner.get('reachable'), mining_ts)
        put(target, 'hold', miner.get('hold'), mining_ts)
        if miner.get('reachable') is True:
            put(target, 'running', miner.get('running'), mining_ts)
            rate = miner.get('hashrate_mhs')
            if numeric(rate):
                rates.append(rate / 1e6)
                put(target, 'hashrate_ths', rate / 1e6, mining_ts)
    mining = result['dashboard:mining']
    put(mining, 'running', snapshot.get('miner_running'), mining_ts)
    if rates:
        put(mining, 'hashrate_ths', sum(rates), mining_ts)
    pool = snapshot.get('pool') or {}
    pool_row = result['dashboard:pool']
    pool_row['max_age_seconds'] = 900
    pool_ts = snapshot.get('pool_last_updated')
    for key in ('hashrate_ths', 'hashrate_24h_ths', 'sats_today', 'unpaid_sats'):
        put(pool_row, key, pool.get(key), pool_ts)
    price = snapshot.get('btc_price_usd')
    price_ts = snapshot.get('btc_price_last_updated')
    put(pool_row, 'btc_price_usd', price, price_ts)
    if numeric(price):
        for raw, converted in (('sats_today', 'today_usd'), ('unpaid_sats', 'unpaid_usd')):
            if numeric(pool.get(raw)):
                put(pool_row, converted, pool[raw] / 1e8 * price, older_stamp(price_ts, pool_ts))
    dehum = result['dashboard:dehumidifier']
    if not snapshot.get('dehum_error'):
        for key in ('power', 'humidity', 'tank_full', 'auto_on'):
            stamp_key = 'dehum_tank_last_updated' if key == 'tank_full' and 'dehum_tank_last_updated' in snapshot else 'dehum_last_updated'
            put(dehum, key, snapshot.get('dehum_' + key), snapshot.get(stamp_key))
    return result
