"""Settings schema — the single source of truth for every persisted setting.

Each setting declares its default, type, and whether it's a secret. Everything
else derives from this table:

  * db.get_settings() fills in defaults for missing keys
  * the settings API coerces incoming values by `kind` (no hand-kept key lists)
  * secrets are masked on GET and un-masked writes are ignored on POST

To add a setting (e.g. for a future thermostat), add ONE row here and a form
field in the dashboard — nothing else to wire up.
"""
from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class Setting:
    default: Any
    kind: str            # "str" | "int" | "float" | "bool"
    secret: bool = False


SCHEMA: Dict[str, Setting] = {
    # ── Solis Cloud ──────────────────────────────────────────────
    "solis_api_key":     Setting("", "str"),
    "solis_api_secret":  Setting("", "str", secret=True),
    "solis_inverter_sn": Setting("", "str"),
    "solis_base_url":    Setting("https://www.soliscloud.com:13333", "str"),

    # ── Miners (X1 primary, X2 secondary) ────────────────────────
    "miner_ip":        Setting("", "str"),
    "miner2_ip":       Setting("", "str"),
    "miner_power_w":   Setting(0.0, "float"),   # nameplate draw, W
    "miner2_power_w":  Setting(0.0, "float"),
    # Per-miner hold: local date (YYYY-MM-DD) the miner is force-stopped
    # until midnight. Rolls off automatically when the local date advances.
    "miner_hold_date":  Setting("", "str"),
    "miner2_hold_date": Setting("", "str"),

    # ── Basic SOC control ────────────────────────────────────────
    "poll_interval_seconds": Setting(60, "int"),
    "soc_on_threshold":      Setting(85.0, "float"),
    "soc_off_threshold":     Setting(80.0, "float"),

    # ── Smart Start ──────────────────────────────────────────────
    "smart_start_enabled":     Setting(True, "bool"),
    "smart_soc_on_threshold":  Setting(60.0, "float"),
    "smart_soc_off_threshold": Setting(55.0, "float"),
    "sunny_hours_threshold":   Setting(3.0, "float"),
    "radiation_threshold_wm2": Setting(300.0, "float"),
    "smart_min_pv_w":          Setting(1000.0, "float"),
    "smart_hold_date":         Setting("", "str"),  # Smart Start paused until midnight

    # ── Solar system ─────────────────────────────────────────────
    "battery_capacity_kwh": Setting(0.0, "float"),
    "pv_peak_kw":           Setting(0.0, "float"),

    # ── Power-profile ramp (surplus tracking) ────────────────────
    "ramp_enabled":       Setting(False, "bool"),
    "ramp_dry_run":       Setting(True, "bool"),
    "miner_max_watts":    Setting(2960.0, "float"),  # ramp ceiling per miner
    "ramp_charge_margin": Setting(1.25, "float"),    # >1 finishes battery charge early

    # ── End-of-day battery target ────────────────────────────────
    "eod_soc_target_enabled": Setting(False, "bool"),
    "eod_soc_target":         Setting(80.0, "float"),

    # ── API failsafe ─────────────────────────────────────────────
    "api_fail_action": Setting("stop", "str"),   # stop | keep | start
    "api_fail_cycles": Setting(3, "int"),

    # ── Location / weather ───────────────────────────────────────
    "location_lat":  Setting(0.0, "float"),
    "location_lon":  Setting(0.0, "float"),
    "location_name": Setting("", "str"),

    # ── Luxor pool ───────────────────────────────────────────────
    "lux_pool_api_key":  Setting("", "str", secret=True),
    "lux_pool_username": Setting("", "str"),
    "lux_pool_api_url":  Setting("", "str"),

    # ── Telegram ─────────────────────────────────────────────────
    "telegram_bot_token": Setting("", "str", secret=True),
    "telegram_chat_id":   Setting("", "str"),
    "tg_miner_onoff":          Setting(True, "bool"),
    "tg_smart_start":          Setting(True, "bool"),
    "tg_api_failure":          Setting(True, "bool"),
    "tg_soc_low":              Setting(True, "bool"),
    "tg_soc_low_pct":          Setting(20.0, "float"),
    "tg_soc_full":             Setting(True, "bool"),
    "tg_hashrate_drop":        Setting(True, "bool"),
    "tg_hashrate_drop_pct":    Setting(25.0, "float"),
    "tg_daily_summary":        Setting(True, "bool"),
    "tg_daily_hour":           Setting(7, "int"),
    "tg_sats_milestone":       Setting(True, "bool"),
    "tg_sats_milestone_amount": Setting(1000, "int"),
    "tg_sunny_day_ahead":      Setting(True, "bool"),
    "tg_sunny_day_hour":       Setting(8, "int"),
    "tg_weekly_recap":         Setting(False, "bool"),
    "tg_weekly_recap_day":     Setting(0, "int"),
    "tg_weekly_recap_hour":    Setting(8, "int"),

    # ── Dehumidifier (Tuya local) ────────────────────────────────
    "dehum_device_id":             Setting("", "str"),
    "dehum_ip":                    Setting("", "str"),
    "dehum_local_key":             Setting("", "str", secret=True),
    "dehum_version":               Setting(3.4, "float"),
    "dehum_auto_enabled":          Setting(False, "bool"),
    "dehum_excess_threshold_w":    Setting(500.0, "float"),
    "dehum_min_run_minutes":       Setting(30, "int"),
    "dehum_min_off_minutes":       Setting(15, "int"),
    "dehum_manual_override_hours": Setting(2, "int"),

    # ── Thermostats (future) ─────────────────────────────────────
    # Reserved for the thermostat device type (see devices/thermostat.py).
    # Wire real settings here when the integration lands.
    "thermostat_enabled": Setting(False, "bool"),
}

DEFAULTS: Dict[str, Any] = {k: s.default for k, s in SCHEMA.items()}


def coerce(key: str, value: Any) -> Any:
    """Coerce an incoming value to its declared type. Unknown keys and
    un-coercible values pass through unchanged (update_settings drops
    unknown keys anyway)."""
    spec = SCHEMA.get(key)
    if spec is None:
        return value
    try:
        if spec.kind == "bool":
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("true", "1", "yes", "on")
        if spec.kind == "int":
            return int(value)
        if spec.kind == "float":
            return float(value)
        return "" if value is None else str(value)
    except (ValueError, TypeError):
        return value


def coerce_all(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: coerce(k, v) for k, v in data.items()}


def mask_secrets(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of settings with secret values replaced by a •••• mask
    (keeping the last 4 chars of longer secrets as a hint)."""
    out = dict(settings)
    for key, spec in SCHEMA.items():
        if spec.secret and out.get(key):
            val = str(out[key])
            out[key] = "••••" + val[-4:] if len(val) > 8 else "••••"
    return out


def strip_masked(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop masked secret values from an incoming settings payload so a
    round-tripped mask never overwrites the stored secret."""
    return {
        k: v for k, v in data.items()
        if not (SCHEMA.get(k) and SCHEMA[k].secret and str(v).startswith("••••"))
    }
