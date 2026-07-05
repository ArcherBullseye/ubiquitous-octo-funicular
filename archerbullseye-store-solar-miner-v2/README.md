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
