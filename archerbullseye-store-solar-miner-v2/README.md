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


## Home workspace

Open **Home** from the dashboard to manage Zigbee sensors and smart plugs,
Wi-Fi Tuya devices, and automation rules. Choose **Discover devices** to:

1. Check the LAN range (for example `192.168.88.0/24`). Broadcast discovery is
   supplemented by a bounded TCP port 6668 scan for Docker bridge networks.
2. Enter the User Code from Smart Life / Tuya Smart → Me → Settings → Account
   and Security. Generate a QR code, scan it with that app, approve access,
   then choose **I've approved · Load devices**.
3. Select devices, review hub associations and LAN addresses, and import.
   Required hubs are included automatically. Existing device settings,
   identities, and automation references are preserved.

Account sign-in retrieves names, local keys, and Zigbee node IDs. The account
list also includes devices outside this LAN; review addresses before import.
LAN discovery cannot enumerate Zigbee children on its own. Missing connection
details create disabled entries for you to finish in **Configure**. Unknown
local mappings require manual setup. Only single boolean switch plugs and
verified temperature/humidity mappings are imported. Discovery/import never
switch a plug. Sensor reports and controls remain local after import.

Remembering the account is optional. Saved tokens are in `data/tuya-account.json`
with owner-only permissions (0600); local keys and tokens are never returned
in the discovery API. Disconnect removes saved account credentials and leaves
imported devices intact. An account can also be reconnected to load newly
paired devices. Keep the existing LAN/firewall configuration for the hub.

For manual setup, add a gateway once, then select it for each Zigbee child.
Each device needs its own Tuya device ID and Zigbee node ID. Use the model's verified temperature, humidity, or boolean power DP.
The default sensor mapping matches the TH02 Z3-P3Z (103 °C, 101 humidity).

Drag the icon on a device card to reorder cards. Drag condition and action
blocks into the rule builder, or click blocks and use their arrow controls.
Rules support all/any conditions, numeric thresholds, plug state, local time
windows, hold duration, and cooldown. Actions can request plug ON/OFF or send
Telegram messages through the existing bot. **Save workspace** applies changes.
Each rule establishes a fresh baseline after startup, reconnecting, or editing.
A later crossing runs the actions. Missing or stale readings pause evaluation.
Actions are queued in their displayed order. Device confirmations appear in
Activity, and a failed action is logged while remaining actions still run.

Manual plug commands create a 15-minute automation hold, retained across app
restarts. **Resume rules** clears the hold. Conditions must cross again to
trigger a rule after a skipped action. Time windows use the workspace timezone
(default America/Detroit) and include daylight saving changes.

The first startup migrates the v2.0.8 sensor, hub, and enabled range alerts into
the workspace. Migration runs once. Thereafter, edit Home configuration in the
workspace. Settings are stored transactionally with a revision check, so two
windows cannot silently overwrite each other's edits. Local keys are masked
in API responses. Each gateway has one socket-owning worker for all its
children, and plug commands are serialized through that worker.

Freshness timestamps indicate when a report was received, including cached hub
snapshots. Readings remain in memory until restart. The dashboard summary lists
all sensors, and the workspace contains each plug's manual controls.

Run regression checks with `python3 -m unittest discover -s tests -v`.
