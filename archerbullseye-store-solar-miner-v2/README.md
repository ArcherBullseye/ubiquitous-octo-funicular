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
Power rules default to **While conditions match (maintain power)**. After the
configured hold duration, fresh matched conditions reconcile each target's
confirmed power state, including at startup, after reconnecting, or after edits.
Already-correct targets are not switched again. Missing or stale condition or
target-power readings pause control. Failed/unconfirmed requests retry while
conditions match, respecting the cooldown and a minimum 30-second retry interval.
Successful Telegram actions are not repeated during the same continuous match.
Notification-only rules default to **Only on a new condition crossing**, which
establishes a baseline on startup/recovery and waits for a later false-to-true
crossing. Either behavior can be selected explicitly in the editor. The editor
also shows whether a saved rule is paused, waiting on data, held manually,
waiting for duration/cooldown, awaiting confirmation, or already satisfied.
Actions are queued in their displayed order. Device confirmations appear in
Activity, and a failed action is logged while remaining actions still run.

Manual plug commands create a 15-minute automation hold, retained across app
restarts. **Resume rules** clears the hold. Maintaining rules reconcile again
after the hold ends without needing another threshold crossing. Time windows use the workspace timezone
(default America/Detroit) and include daylight saving changes.

The first startup migrates the v2.0.8 sensor, hub, and enabled range alerts into
the workspace. Migration runs once. Thereafter, edit Home configuration in the
workspace. Settings are stored transactionally with a revision check, so two
windows cannot silently overwrite each other's edits. Local keys are masked
in API responses. Each gateway has one socket-owning worker for all its
children, and plug commands are serialized through that worker.

Freshness timestamps indicate when a report was received, including cached hub
snapshots. Readings remain in memory until restart. The dashboard summary lists
selected devices, and the workspace contains each plug's manual controls.

Powered Zigbee devices use a separate, read-only gateway online-status query
through the existing worker/socket at startup and every 30 seconds. Cached DP
snapshots retain their readings but cannot override a gateway offline report.
A positive online check expires after 90 seconds; unsupported or unavailable
checks display an unconfirmed red icon instead of claiming the child is online.
Only explicitly listed online/offline node IDs are updated, because gateway
responses can arrive in multiple parts. Sleeping sensors keep their existing
report-based freshness. Detection still depends on when the gateway notices a
missing child; this is not an instantaneous mains-power detector. Online/offline
changes appear in Activity. Power-command confirmation also requires an online
connection, so a cached matching relay value cannot falsely confirm success.
Reference: https://github.com/jasonacox/tinytuya/pull/308

Run regression checks with `python3 -m unittest discover -s tests -v`.

### Dashboard conditions and plug actions

The automation builder offers separate **Turn plug ON** and **Turn plug OFF**
actions. Use the **Solar plug ON/OFF pair** shortcut to create two paused rules
for the first configured plug. Review the plug selection and thresholds in
both rules, enable them, then save the workspace. The starting example turns
ON when SOC is at least 95% and excess power is at least 1,000 W for 60 seconds;
it turns OFF when SOC is at most 90% or excess power is at most 0 W for 30 seconds.

Conditions can use Home sensors/plugs, battery SOC, inverter power
flows, charging/discharging, grid import/export, charge reserve, Smart Start,
end-of-day projections/protection, ramp current/target/mode, weather and the
next forecast hour, miner running/reachability/holds/hashrate, pool earnings,
Bitcoin price, dehumidifier readings, and local time windows. Numeric comparisons
include above, below, at least, at most, equals, between (inclusive), and outside
a range. State readings offer true/on and false/off. Live values and units are
shown below each condition, and Check saved conditions uses the same adapter as
the runtime engine without running actions.

Excess power is grid flow + battery charging power − the battery charging
reserve, matching the ramp's headroom calculation. It describes additional
available power after existing loads; negative values mean a deficit. It is
available without enabling the miner ramp. When a charge reserve depends on
the weather forecast, missing/expired forecast data pauses that condition.

Source timestamps are independent: power and miner/dehumidifier readings
expire after 5 minutes, pool/Bitcoin readings after 15 minutes, and weather
after 1 hour. Home sensor/plug reports keep their existing 30-minute freshness
window. Missing, invalid, or stale required readings pause a rule. Power rules
resume maintaining their targets after fresh data returns; crossing rules
establish a new baseline. Manual plug holds and cooldowns continue to apply.

### Dehumidifiers in Home

Device type **Dehumidifier** supports Wi-Fi or a Zigbee hub connection, ON/OFF
commands confirmed by a status read, room humidity, target humidity (read-only),
and a boolean tank-full status. The Ivation mapping used by the existing
integration is provided as editable defaults: power DP 1, room humidity 109,
target humidity 3, tank full 111. Optional readings can be left blank. Unknown
or missing readings remain unavailable rather than becoming zero or false.

Dehumidifiers appear as action targets and condition sources in Home
Automations. Existing power actions remain compatible when an imported device
is changed from Smart plug to Dehumidifier; its ID and saved credentials stay
intact. Tuya category `cs` is classified as a dehumidifier during discovery
when a boolean power mapping is available; optional readings use account
metadata without guessed DP numbers. Turning ON is rejected while a known
tank-full report is active; OFF remains available.

If an enabled Home Wi-Fi dehumidifier matches the dashboard device ID or IP,
Home owns the connection. The original dashboard displays Home's reports and
routes its ON/OFF buttons through Home, including its 15-minute manual hold.
The older excess-solar controller pauses while Home owns that device, and its
settings are retained. Use Home automation rules for automatic control. The
legacy connection/controller remains available when no enabled Home device
matches. No physical devices were operated during development verification.

### Additional device measurements

Home captures bounded numeric/boolean DP reports on each existing connection.
The information button on a device card shows mapped readings, timestamps, and
uninterpreted numeric/boolean reports. Refresh device report queues one status
query on the current worker; it never switches power or opens another socket.
Unknown strings, raw blobs, and nested payloads are excluded from diagnostics.

Discover devices → Load account devices → Update configured-device measurements
adds supported measurement mappings to existing devices. Connection credentials,
IDs, device types, rules, and manual mappings are preserved. New imports also
receive supported mappings. Automatic mappings require local support, a supported
conversion, and explicit numeric scale/units. Unsupported readings stay absent.
Manual mappings are available under Additional device readings in device setup.

Supported measurements include watts, amps, volts, cumulative energy, energy
increments, battery percentage, and additional numeric, boolean, and enum status
readings from the device schema. Current in mA is converted to A; energy in Wh
is converted to kWh. Tuya `add_ele` is an increment, never a cumulative total.
Numeric and boolean mapped readings can be used in automation conditions. Enum
readings are displayed read-only. Per-reading timestamps survive partial reports.

Hardware capability is confirmed only after the update is installed and real
reports are inspected. Development used simulated reports. The published
Ivation IVADUWIFI50WP profile in make-all/tuya-local exposes temperatures, fan,
compressor, defrost and fault status but uses different base DP assignments from
this app's older dashboard defaults. Do not silently replace a user's mappings
or label unknown numeric DPs as electrical measurements based on a similar model.
Reference: https://github.com/make-all/tuya-local/blob/main/custom_components/tuya_local/devices/ivation_50pt_dehumidifier.yaml

### Automation groups (v2.4.0)

Use **New group** in Home → Automations to create a named group and select an
icon. The gear beside a group lets you rename it or change its icon. Expand or
collapse a group by clicking its header. Open/closed state is stored in that
browser and does not need Save workspace. Group names, icons, order, and rule
assignments are saved with the workspace.

Move an automation with its **Automation group** selector, or drag its grip
onto another group's header/body. New automations and solar ON/OFF pairs use
the currently selected group. Existing rules begin in Ungrouped. Removing a
group keeps its rules and moves them to Ungrouped. Save workspace applies
organizational changes; collapsing a group never pauses its automations.

Groups are presentation metadata. Group-only saves preserve existing device
connections, manual holds, threshold-crossing memory, and cooldowns. Validation
allows up to 50 groups, 80-character names, and the supplied icon catalogue;
dangling references and invalid group IDs/icons are rejected atomically. Older
API callers that omit grouping fields preserve existing groups/assignments.

### Home dashboard device selection (v2.5.0)

Use the gear on the dashboard's Home Assistant card to open **Dashboard card**
in Home. Check the devices to show, then **Save workspace**. Sensors show
temperature/humidity, plugs show ON/OFF and supported power draw, and
dehumidifiers show ON/OFF and humidity, with a freshness dot for each device.
Rows follow the order in Devices. Existing installations default to showing
all sensors; an explicitly empty selection hides all device rows. Visibility
changes preserve connections, manual holds, and automation state. Older API
clients preserve saved choices when they omit dashboard visibility settings.

### Dehumidifier pan reporting (v2.6.0)

The dashboard dehumidifier card prioritizes **Pan full** over its ON/OFF state,
shows **Empty the water pan**, highlights the card, and disables its ON button
while the last confirmed pan status is full. A confirmed not-full report clears
the warning. Missing/invalid pan data is **Pan status unavailable**, not an empty
pan. Pan timestamps are independent of power/humidity updates; old or offline
full-pan warnings remain visible as last-known reports with their age. A device
selected in the Home Assistant card also shows **Pan full** in its reading cell.
Only the configured boolean tank-full signal is interpreted; no model DP IDs or
bitmask meanings are inferred. Local previews use simulated device reports.

### Compact desktop dashboard (v2.6.0)

Earnings sits beside Smart Start; Home Assistant sits beside the dehumidifier.
Earnings uses tighter spacing, while the desktop dashboard shares the available
window height across the three main rows so the bottom charts remain visible.
Long lists scroll inside their cards. Ramp Activity stays in its original place
with its existing event list. The mobile layout continues to stack vertically.
Verified locally at 1280×720, 1366×768, and 1512×740 with simulated readings.

### Dashboard polish (v2.6.1)

The Home card is titled **Home Asst**, with its Local/status badge beside the
title. Received is pinned to the bottom and turns red when the newest reading
among selected devices is more than 10 minutes old. Its age keeps updating even
if a status refresh fails, and a new reading clears the warning. Long device
lists scroll above the footer. Home navigation is Devices, Automations, Activity,
then Dashboard card, with extra space before Activity. The energy-flow diagram
has balanced outer margins, with Home/Miners shifted left and Solar/Grid farther
above/below the battery line; static and animated connections follow the nodes.

### Battery charge allowance (v2.7.0)
A small blue companion circle to the left of the battery shows the BMS
charge-current allowance from SolisCloud as a share of the normal reference.
Approximate DC power (allowed amps × reported battery voltage) appears below
it; amps, source timestamp, and interpretation are available on hover. The
main battery bubble is shifted right and continues to show actual charge or
discharge power. The dim arc shows the difference from the normal reference.
The comparison reference defaults to 14 kW, the combined rating of two LG
RESU16H Prime packs at 7 kW each. Adjust the reference in Settings → Solar
System for your battery bank, or use 0 to show the allowance without the
percentage comparison. This reference is display-only and does not change
miner control, charge reserve, or inverter/battery settings. A reduced BMS
allowance does not identify its cause or guarantee achievable charge power.
Solar availability, battery ratings and inverter settings also constrain charging.

Missing/invalid current or units show unavailable, zero is preserved, and
offline, missing-timestamp or reports older than 10 minutes show stale. Age
uses the inverter's dataTimestamp, not the time the cloud request succeeded.
A successful re-fetch of the same old report cannot make it fresh.
The gauge uses SolisCloud. The LG battery monitoring page can provide cell
temperatures and charge limits separately when the battery is reachable on
the local network.

Sources: [SolisCloud API V2.0](https://oss.soliscloud.com/templet/SolisCloud%20Platform%20API%20Document%20V2.0.pdf),
[LG RESU16H Prime specification](https://www.lgessbattery.com/ImageServlet?imageType=PRODUCT&imgPath=20220701171417442%5B20220701171417442%5D.pdf).

Ramp Activity shows the weekday and date above the event time. Hover to see
the full date and timezone. Home Asst reserves space for each freshness dot
when plug power readings are wide.
