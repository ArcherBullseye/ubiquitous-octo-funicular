# ⚡ Solar Miner v2

Solar-surplus-driven load controller: mines Bitcoin (LuxOS miners) on excess
solar from a Solis inverter/battery system — never exporting to the grid,
while still charging the battery full by end of day. Runs a dehumidifier on
surplus too, with thermostats planned next.

This is the ground-up refactor of Solar Miner v1: same proven control logic,
reorganized for clarity and expansion, with a redesigned dashboard (live
energy-flow diagram, SOC ring, per-miner power-step bars, ramp activity log).

## Architecture

Each layer only reaches downward:

```
run.py                  entry point: build app, start loops, serve :3000
app/
  web/                  HTTP API (v1-compatible endpoints) + dashboard
    routes.py             all /api endpoints
    templates/ static/    dashboard (html / css / js)
  control/              decision logic
    loop.py               the orchestrating control loop
    smart_start.py        sunny-day threshold lowering (pure)
    ramp.py               surplus-tracking power-profile ramp
    eod.py                end-of-day SOC projection (pure)
    commands.py           manual start/stop + per-miner holds
    notifications.py      edge-triggered Telegram alerts
    telegram_commands.py  /startMiner /stopMiner /info bot loop
  devices/              hardware behind capability interfaces
    base.py               Device / SwitchableLoad / DimmableLoad
    miner.py              Miner + MinerFleet (X1/X2, holds, ladders)
    dehumidifier.py       Tuya dehumidifier + excess-solar auto mode
    thermostat.py         future integration — interface reserved
  clients/              thin API clients, no app logic
    solis.py luxos.py luxor.py openmeteo.py telegram.py tuya.py
  config.py             settings schema — single source of truth
  db.py                 SQLite storage (v1-compatible, incl. migrations)
  state.py              thread-safe shared runtime state
  background.py         weather/pool loops + thread launcher
```

### Adding a device type (e.g. thermostats)

1. Client under `app/clients/`, device under `app/devices/` implementing
   `SwitchableLoad` (or `DimmableLoad` for multi-stage systems).
2. Settings rows in `app/config.py` (one line each — type coercion, secret
   masking, and persistence come free).
3. Register in the control loop's device section; the surplus/ramp logic
   already speaks the capability interfaces.
4. Dashboard card in `web/templates/index.html` + `static/js/dashboard.js`.

See `app/devices/thermostat.py` for the wiring points.

## Upgrading from v1

The database is fully compatible — point v2 at the existing `data/` directory
(or Umbrel app data volume) and all settings, history, learned PV efficiency,
and the ramp log carry over. Nothing is ever pruned. Old pre-migration v1
databases are migrated automatically on first start.

## Run

```
pip install -r requirements.txt
python run.py            # http://localhost:3000
```

or `docker compose up -d`.


## Local Home sensor card

The Home Assistant card beside Smart Start reads a Zigbee temperature and
humidity sensor through a Tuya 3.4 gateway over the LAN. The row shows the
sensor name, temperature in °F, humidity, and a freshness dot. Green means both
values arrived within 30 minutes, amber means a value is stale or missing, and
gray means no values have arrived.

Open the gear on the Home card and expand **Hub & sensor setup**. Enter the
hub IP address, hub device ID, local key, sensor device ID, and Zigbee node ID,
then enable the sensor. Keep the local key in settings. The TH02 Z3-P3Z reports
whole degrees Celsius on DP 103 and relative humidity on DP 101. These are the
defaults, with both divisors set to 1. For another sensor, enter its own data
points, divisors, and temperature unit.

Under **Triggers**, enable the Telegram temperature alert and enter your low
and high limits in °F. The alert uses the bot and chat configured in **Alerts**.
The first reading sets the baseline. A later crossing sends one alert for the
new hot or cold state. Returning to the range lets that same limit alert again.

The listener merges partial reports and retains the most recently received
value for each measurement in memory. A queried value can be a cached hub
snapshot, so the timestamp indicates when Solar Miner received it.

Run regression checks with `python3 -m unittest discover -s tests -v`.
