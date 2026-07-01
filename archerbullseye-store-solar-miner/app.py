import os
import time
import threading
import warnings
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", message=".*urllib3.*", category=Warning)
warnings.filterwarnings("ignore", message=".*OpenSSL.*", category=Warning)
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from db import init_db, get_settings, update_settings, save_reading, get_recent_readings, update_pv_efficiency, get_pv_efficiency, get_pv_efficiency_detail, reset_pv_efficiency, get_hourly_load_profile
from solis_api import SolisClient, SolisApiError, parse_power_and_soc
from luxos_api import LuxOsClient, LuxOsError
from weather import get_weather, parse_weather, geocode as do_geocode
from telegram_bot import TelegramBot
from lux_pool import LuxPoolClient
from dehumidifier import DehumidifierClient

load_dotenv()

APP_VERSION = "1.5.11"

app = Flask(__name__)

state = {
    "readings": None,
    "miner_running": None,
    "weather": None,
    "pool": None,
    "btc_price_usd": None,
    "last_updated": None,
    "weather_last_updated": None,
    "pool_last_updated": None,
    "effective_soc_on": 80.0,
    "smart_start_active": False,
    "smart_hold_active": False,
    "miner_hold_active": False,
    "miner_cmd_pending": None,
    "cycle": 0,
    "error": None,
    "action": "none",
    "eod_target": None,
    "eod_projected_with": None,
    "eod_projected_without": None,
    "eod_protecting": False,
    "dehum_power": None,
    "dehum_humidity": None,
    "dehum_tank_full": False,
    "dehum_auto_on": False,
    "dehum_error": None,
    "dehum_auto_on_since": None,
    "dehum_auto_off_since": None,
    "dehum_manual_override_until": None,
}
state_lock = threading.Lock()

luxos_client: Optional[LuxOsClient] = None
luxos_lock = threading.Lock()
_luxos_ip: Optional[str] = None

SESSION_FILE = Path("data/luxos_session.txt")
weather_refresh = threading.Event()
control_refresh = threading.Event()  # wakes control_loop for an immediate re-poll
pool_refresh = threading.Event()     # wakes pool_loop for an immediate re-poll

# Notification state — only written by control_loop (no lock needed)
notify_state = {
    "api_fail_count": 0,
    "api_fail_notified": False,
    "api_fail_count_at_notify": 0,
    "prev_mining": None,
    # None = not yet primed; first cycle after (re)start establishes a silent
    # baseline so edge-triggered alerts don't re-fire on every upgrade/restart.
    "prev_smart_active": None,
    "soc_low_notified": None,
    "soc_full_notified": None,
    "prev_hashrate_mhs": None,
    "last_daily_date": None,
    "last_weekly_recap_date": None,
    "sats_milestone_last": None,
    "sats_milestone_date": None,
    "last_sunny_day_date": None,
}


# Cache for Luxor daily revenue history — refreshed once per UTC day
_revenue_cache: dict = {"utc_date": None, "rows": []}
_revenue_lock = threading.Lock()


# ── Session helpers ──────────────────────────────────────────────

def _save_session(sid: Optional[str]) -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        if sid:
            SESSION_FILE.write_text(sid)
        elif SESSION_FILE.exists():
            SESSION_FILE.unlink()
    except Exception:
        pass


def _load_session() -> Optional[str]:
    try:
        if SESSION_FILE.exists():
            return SESSION_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


def get_or_create_luxos(ip: str) -> LuxOsClient:
    global luxos_client, _luxos_ip
    with luxos_lock:
        if luxos_client is not None and _luxos_ip == ip and luxos_client._session_id:
            return luxos_client

        if luxos_client is not None and _luxos_ip != ip:
            try:
                luxos_client.close()
                _save_session(None)
            except Exception:
                pass
            luxos_client = None

        client = LuxOsClient(ip=ip)
        _luxos_ip = ip

        try:
            client.logon()
            _save_session(client._session_id)
        except LuxOsError as e:
            if "Another session is active" in str(e):
                saved_sid = _load_session()
                if saved_sid:
                    try:
                        tmp = LuxOsClient(ip=ip)
                        tmp._session_id = saved_sid
                        tmp.logoff()
                        _save_session(None)
                    except Exception:
                        pass
                client.logon()
                _save_session(client._session_id)
            else:
                raise

        luxos_client = client
        return luxos_client


def _clear_luxos_client() -> None:
    global luxos_client, _luxos_ip
    with luxos_lock:
        if luxos_client is not None:
            try:
                luxos_client.close()
                _save_session(None)
            except Exception:
                pass
        luxos_client = None
        _luxos_ip = None


# ── Telegram helpers ─────────────────────────────────────────────

def _get_bot(settings: dict) -> Optional[TelegramBot]:
    token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    if token and chat_id:
        return TelegramBot(token, chat_id)
    return None


def _local_today_str() -> str:
    """Local calendar date (YYYY-MM-DD) from the weather-derived UTC offset."""
    with state_lock:
        tz_offset = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
    return (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).date().isoformat()


def _do_miner_stop() -> tuple:
    """Stop the miner and hold it off until local midnight (manual override).

    Single source of truth for the dashboard Stop button and Telegram /stopMiner.
    Sets miner_hold_date so the control loop keeps the miner off and pauses
    automatic SOC control + Smart Start until the local date rolls over. Returns
    (ok: bool, error: Optional[str]).
    """
    settings = get_settings()
    miner_ip = settings.get("miner_ip") or os.getenv("LUXOS_MINER_IP", "")
    if not miner_ip:
        return False, "Miner IP not configured"
    update_settings({"miner_hold_date": _local_today_str()})
    try:
        client = get_or_create_luxos(miner_ip)
        client.stop_mining()
    except Exception as e:
        return False, str(e)
    with state_lock:
        state["miner_running"]     = False
        state["miner_hold_active"] = True
        state["miner_cmd_pending"] = {"want": False, "cycles": 0}
    return True, None


def _do_miner_start() -> tuple:
    """Clear the manual hold and start the miner, handing control back to
    automatic SOC management (which governs again from the next cycle).

    Single source of truth for the dashboard Start button and Telegram /startMiner.
    Returns (ok: bool, error: Optional[str]).
    """
    settings = get_settings()
    miner_ip = settings.get("miner_ip") or os.getenv("LUXOS_MINER_IP", "")
    if not miner_ip:
        return False, "Miner IP not configured"
    update_settings({"miner_hold_date": ""})
    try:
        client = get_or_create_luxos(miner_ip)
        client.start_mining()
    except Exception as e:
        return False, str(e)
    with state_lock:
        state["miner_running"]     = True
        state["miner_hold_active"] = False
        state["miner_cmd_pending"] = {"want": True, "cycles": 0}
    return True, None


def _fmt_ths(mhs: float) -> str:
    if mhs <= 0:
        return "0 MH/s"
    if mhs >= 1_000_000:
        return f"{mhs / 1_000_000:.1f} TH/s"
    if mhs >= 1_000:
        return f"{mhs / 1_000:.1f} GH/s"
    return f"{mhs:.0f} MH/s"


def _send_notifications(settings: dict, soc: Optional[float], actually_mining: bool,
                        smart_active: bool, soc_on: float, effective_soc_on: float,
                        effective_soc_off: float, hashrate_mhs: float,
                        smart_hold_active: bool = False) -> None:
    bot = _get_bot(settings)
    if not bot:
        return

    # ── Miner ON / OFF ──────────────────────────────────────────
    if settings.get("tg_miner_onoff") and notify_state["prev_mining"] is not None:
        if actually_mining and not notify_state["prev_mining"] and soc is not None:
            smart_tag = " 🧠 <i>Smart Start</i>" if smart_active else ""
            bot.send(
                f"⛏ <b>Miner started</b>{smart_tag}\n"
                f"SOC: {soc:.1f}% | Threshold: {effective_soc_on:.0f}%"
            )
        elif not actually_mining and notify_state["prev_mining"] and soc is not None:
            bot.send(
                f"💤 <b>Miner stopped</b>\n"
                f"SOC: {soc:.1f}% | Off threshold: {effective_soc_off:.0f}%"
            )

    # ── Smart Start ON / OFF ─────────────────────────────────────
    if settings.get("tg_smart_start") and notify_state["prev_smart_active"] is not None:
        with state_lock:
            wx = state.get("weather")
        sunny = wx.get("remaining_sunny_hours", 0) if wx else 0
        if smart_active and not notify_state["prev_smart_active"]:
            bot.send(
                f"🧠 <b>Smart Start activated</b>\n"
                f"☀️ {sunny} sunny hours remaining\n"
                f"Mining from {effective_soc_on:.0f}% SOC (normal: {soc_on:.0f}%)"
            )
        elif not smart_active and notify_state["prev_smart_active"]:
            bot.send(
                f"🌤 <b>Smart Start deactivated</b>\n"
                f"Returning to normal {soc_on:.0f}% SOC threshold"
            )

    # ── SOC low warning ──────────────────────────────────────────
    if settings.get("tg_soc_low") and soc is not None:
        low_pct = float(settings.get("tg_soc_low_pct") or 20.0)
        if notify_state["soc_low_notified"] is None:
            # Prime silently on first cycle so a restart while already low
            # doesn't re-announce on every upgrade.
            notify_state["soc_low_notified"] = soc < low_pct
        elif soc < low_pct and not notify_state["soc_low_notified"]:
            bot.send(
                f"🔋 <b>Battery low: {soc:.1f}%</b>\n"
                f"Miner is {'ON ⛏' if actually_mining else 'OFF 💤'}"
            )
            notify_state["soc_low_notified"] = True
        elif soc >= low_pct + 5:
            notify_state["soc_low_notified"] = False

    # ── Battery fully charged ────────────────────────────────────
    if settings.get("tg_soc_full") and soc is not None:
        if notify_state["soc_full_notified"] is None:
            notify_state["soc_full_notified"] = soc >= 99.5
        elif soc >= 99.5 and not notify_state["soc_full_notified"]:
            bot.send(
                f"🌞 <b>Battery fully charged!</b>\n"
                f"{'Miner running ⛏' if actually_mining else 'Ready to mine whenever you need'}"
            )
            notify_state["soc_full_notified"] = True
        elif soc < 95:
            notify_state["soc_full_notified"] = False

    # ── Hashrate drop ────────────────────────────────────────────
    if settings.get("tg_hashrate_drop") and hashrate_mhs > 0 and actually_mining:
        drop_pct = float(settings.get("tg_hashrate_drop_pct") or 25.0)
        prev_hr = notify_state.get("prev_hashrate_mhs") or 0
        if prev_hr > 0:
            drop = (prev_hr - hashrate_mhs) / prev_hr * 100
            if drop >= drop_pct:
                bot.send(
                    f"📉 <b>Hashrate dropped {drop:.0f}%</b>\n"
                    f"{_fmt_ths(hashrate_mhs)} (was {_fmt_ths(prev_hr)})\n"
                    f"Possible thermal throttling or board issue"
                )
        notify_state["prev_hashrate_mhs"] = hashrate_mhs

    # ── Sats milestone ───────────────────────────────────────────
    if settings.get("tg_sats_milestone"):
        with state_lock:
            pool = state.get("pool") or {}
            tz_offset = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
        pool_sats = pool.get("sats_today", 0) or 0
        milestone = int(settings.get("tg_sats_milestone_amount") or 1000)
        if pool_sats > 0 and milestone > 0:
            local_today = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).strftime("%Y-%m-%d")
            current_ms = (pool_sats // milestone) * milestone
            if notify_state["sats_milestone_last"] is None \
                    or notify_state["sats_milestone_date"] != local_today:
                # Prime silently on the first cycle and at each local-day
                # rollover. sats_today is a rolling 24h figure (not a calendar
                # reset), so baseline to the current value: that way we announce
                # milestones the running total climbs to *during the new day*,
                # without re-spamming yesterday's carried-over total at midnight
                # or re-announcing on restart.
                notify_state["sats_milestone_last"] = current_ms
                notify_state["sats_milestone_date"] = local_today
            elif current_ms > notify_state["sats_milestone_last"]:
                notify_state["sats_milestone_last"] = current_ms
                bot.send(
                    f"💰 <b>Sats milestone reached!</b>\n"
                    f"{current_ms:,}+ sats earned today\n"
                    f"({pool_sats:,} sats total)"
                )

    # ── Daily summary ────────────────────────────────────────────
    if settings.get("tg_daily_summary"):
        target_hour = int(settings.get("tg_daily_hour") or 7)
        with state_lock:
            tz_offset = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
        local_now = datetime.now(timezone.utc) + timedelta(seconds=tz_offset)
        today = local_now.strftime("%Y-%m-%d")
        if local_now.hour == target_hour and notify_state["last_daily_date"] != today:
            notify_state["last_daily_date"] = today
            rows = get_recent_readings(hours=24)
            if rows:
                poll_sec = int(settings.get("poll_interval_seconds") or 60)
                mining_cycles = sum(1 for r in rows if r.get("miner_running"))
                mining_hours = mining_cycles * poll_sec / 3600
                peak_soc = max(r["soc"] for r in rows)
                min_soc = min(r["soc"] for r in rows)
                hr_vals = [r.get("hashrate_mhs") or 0 for r in rows if r.get("miner_running")]
                avg_hr = (sum(hr_vals) / len(hr_vals)) if hr_vals else 0
                with state_lock:
                    pool = state.get("pool") or {}
                sats_str = f"{pool.get('sats_today', 0):,} sats" if pool else "N/A"
                bot.send(
                    f"📊 <b>Daily Mining Summary</b>\n"
                    f"⛏ Mining time: {mining_hours:.1f}h\n"
                    f"⚡ Avg hashrate: {_fmt_ths(avg_hr)}\n"
                    f"📈 Peak SOC: {peak_soc:.1f}%\n"
                    f"📉 Min SOC: {min_soc:.1f}%\n"
                    f"💰 Sats today: {sats_str}"
                )

    # ── Weekly recap ─────────────────────────────────────────────
    if settings.get("tg_weekly_recap"):
        recap_day  = int(settings.get("tg_weekly_recap_day") or 0)   # 0=Mon … 6=Sun
        recap_hour = int(settings.get("tg_weekly_recap_hour") or 8)
        with state_lock:
            tz_offset = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
        local_now = datetime.now(timezone.utc) + timedelta(seconds=tz_offset)
        today = local_now.strftime("%Y-%m-%d")
        if local_now.weekday() == recap_day and local_now.hour == recap_hour:
            if notify_state["last_weekly_recap_date"] != today:
                notify_state["last_weekly_recap_date"] = today
                rows = _fetch_daily_sats_rows(7)
                if rows:
                    total = sum(r["sats"] for r in rows)
                    lines = "\n".join(
                        f"  {r['date']}: {r['sats']:,}" for r in rows
                    )
                    bot.send(
                        f"📅 <b>Weekly Sats Recap</b>\n"
                        f"{lines}\n"
                        f"─────────────────\n"
                        f"<b>Total: {total:,} sats</b>"
                    )

    # ── Good solar day ahead (once per day at the configured local hour) ──
    # A morning heads-up, not a live event. Fires at most once per local day,
    # only during the target hour, and only when Smart Start is enabled, not
    # manually held, and the forecast clears the sunny-hours threshold.
    if settings.get("tg_sunny_day_ahead"):
        with state_lock:
            wx = state.get("weather")
            tz_offset = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
        if wx:
            local_now = datetime.now(timezone.utc) + timedelta(seconds=tz_offset)
            local_today = local_now.strftime("%Y-%m-%d")
            target_hour = int(settings.get("tg_sunny_day_hour") or 8)
            sunny = wx.get("remaining_sunny_hours", 0) or 0
            sunny_thresh = float(settings.get("sunny_hours_threshold") or 3.0)
            smart_on = float(settings.get("smart_soc_on_threshold") or 60.0)
            smart_start_enabled = bool(settings.get("smart_start_enabled", True))
            if (local_now.hour == target_hour
                    and notify_state["last_sunny_day_date"] != local_today
                    and sunny >= sunny_thresh
                    and smart_start_enabled
                    and not smart_hold_active):
                notify_state["last_sunny_day_date"] = local_today
                bot.send(
                    f"☀️ <b>Good solar day ahead!</b>\n"
                    f"{int(sunny)} sunny hours remaining\n"
                    f"Smart Start will activate at {smart_on:.0f}% SOC"
                )

    # Update previous state for next cycle
    notify_state["prev_mining"] = actually_mining
    notify_state["prev_smart_active"] = smart_active


# ── Control loop ─────────────────────────────────────────────────

def _estimate_eod_soc(
    soc: float,
    battery_kwh: float,
    hourly_wx: list,
    pv_peak_kw: float,
    eff_map: dict,
    load_profile: dict,
    miner_power_w: float,
    include_miner: bool,
) -> Optional[float]:
    """
    Project battery SOC (%) at end of today's solar generation window.
    Iterates remaining hourly forecast slots until radiation drops below 50 W/m²,
    accumulating net energy (PV - house load - miner if include_miner).
    Returns None if insufficient data to estimate.
    """
    if battery_kwh <= 0 or not hourly_wx:
        return None

    now_hour = datetime.now().hour
    energy_kwh = (soc / 100.0) * battery_kwh
    found_solar = False

    for slot in hourly_wx:
        try:
            slot_hour = int(str(slot.get("time", "")).split(":")[0])
        except (ValueError, IndexError):
            continue
        if slot_hour < now_hour:
            continue

        rad = float(slot.get("radiation_w") or 0)
        if rad < 50:
            if found_solar:
                break  # past end of solar window
            continue   # pre-dawn hours before generation starts

        found_solar = True

        # PV production this hour
        theoretical = pv_peak_kw * rad / 1000.0
        eff = eff_map.get(slot_hour)
        pv = theoretical * eff if (eff and eff > 0.05) else theoretical

        # House load this hour (fall back to 400 W if no history yet)
        load_w = load_profile.get(slot_hour, 400.0)

        # Miner draw
        miner = (miner_power_w / 1000.0) if include_miner else 0.0

        energy_kwh += pv - (load_w / 1000.0) - miner
        energy_kwh = max(0.0, min(battery_kwh, energy_kwh))

    if not found_solar:
        return None

    return round((energy_kwh / battery_kwh) * 100.0, 1)


def control_loop() -> None:
    while True:
        loop_start = time.monotonic()
        try:
            settings = get_settings()

            api_key    = settings.get("solis_api_key")    or os.getenv("SOLIS_API_KEY", "")
            api_secret = settings.get("solis_api_secret") or os.getenv("SOLIS_API_SECRET", "")
            inverter_sn = settings.get("solis_inverter_sn") or os.getenv("SOLIS_INVERTER_SN", "")
            miner_ip   = settings.get("miner_ip")         or os.getenv("LUXOS_MINER_IP", "")

            poll_interval       = int(settings.get("poll_interval_seconds") or 60)
            soc_on              = float(settings.get("soc_on_threshold") or 85.0)
            soc_off             = float(settings.get("soc_off_threshold") or 80.0)
            smart_start_enabled = bool(settings.get("smart_start_enabled", True))
            smart_soc_on        = float(settings.get("smart_soc_on_threshold") or 60.0)
            smart_soc_off       = float(settings.get("smart_soc_off_threshold") or 55.0)
            sunny_hours_threshold = float(settings.get("sunny_hours_threshold") or 3.0)
            smart_min_pv_w      = float(settings.get("smart_min_pv_w") or 1000.0)
            api_fail_action     = settings.get("api_fail_action", "stop")
            api_fail_cycles     = int(settings.get("api_fail_cycles") or 3)

            # Determine effective thresholds (smart start)
            with state_lock:
                current_weather = state.get("weather")
                cycle_num = state.get("cycle", 0) + 1

            pv_peak_kw   = float(settings.get("pv_peak_kw") or 0.0)
            miner_power_w = float(settings.get("miner_power_w") or 0.0)

            # Smart Start manual hold — paused until local midnight. While the
            # stored hold date matches today's local date, Smart Start is off and
            # the miner reverts to normal-mode thresholds. Rolls off automatically
            # after midnight when the local date advances.
            _hold_tz = int((current_weather or {}).get("utc_offset_seconds") or 0)
            _local_today = (datetime.now(timezone.utc) + timedelta(seconds=_hold_tz)).date().isoformat()
            smart_hold_active = bool(settings.get("smart_hold_date")) and \
                settings.get("smart_hold_date") == _local_today

            # Miner manual hold — force-stop until local midnight (via /stopMiner or
            # the dashboard Stop button). While active, automatic SOC control and
            # Smart Start are overridden and the miner is kept off. Rolls off
            # automatically after midnight when the local date advances.
            miner_hold_active = bool(settings.get("miner_hold_date")) and \
                settings.get("miner_hold_date") == _local_today

            smart_active = False
            effective_soc_on  = soc_on
            effective_soc_off = soc_off

            if smart_start_enabled and not smart_hold_active and current_weather is not None:
                hourly = current_weather.get("hourly") or []
                if pv_peak_kw > 0 and miner_power_w > 0:
                    # Learned efficiency model: predict actual output per forecast hour
                    with state_lock:
                        _wx_tz = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
                    _local_month = (datetime.now(timezone.utc) + timedelta(seconds=_wx_tz)).month
                    efficiency_map = get_pv_efficiency(_local_month)
                    profitable_hours = 0
                    for slot in hourly:
                        rad   = float(slot.get("radiation_w") or 0)
                        htime = slot.get("time", "")  # "HH:MM"
                        try:
                            hour_of_day = int(htime.split(":")[0])
                        except (ValueError, IndexError):
                            continue
                        eff = efficiency_map.get(hour_of_day)
                        if eff is not None:
                            # We have learned data — use predicted output vs miner draw
                            if rad * pv_peak_kw * 1000.0 * eff >= miner_power_w:
                                profitable_hours += 1
                        else:
                            # No learned data yet — count hours above radiation threshold
                            if rad > float(settings.get("radiation_threshold_wm2") or 300.0):
                                profitable_hours += 1
                    remaining_sunny = current_weather.get("remaining_sunny_hours", 0)
                    if profitable_hours >= sunny_hours_threshold:
                        smart_active = True
                    elif remaining_sunny >= sunny_hours_threshold:
                        # Efficiency model only covers 8 slots — fall back to full-day count
                        smart_active = True
                else:
                    # PV peak or miner watts not configured — use raw radiation count
                    remaining_sunny = current_weather.get("remaining_sunny_hours", 0)
                    if remaining_sunny >= sunny_hours_threshold:
                        smart_active = True

                if smart_active:
                    effective_soc_on  = smart_soc_on
                    effective_soc_off = smart_soc_off

            # ── Poll Solis ───────────────────────────────────────
            readings  = None
            error_str = None
            action    = "none"
            cur_rad   = 0.0
            miner_running = None

            if api_key and api_secret and inverter_sn:
                try:
                    solis = SolisClient(
                        api_key=api_key,
                        api_secret=api_secret,
                        base_url=settings.get("solis_base_url", "https://www.soliscloud.com:13333"),
                    )
                    inverter_data = solis.get_inverter_detail(inverter_sn)
                    readings = parse_power_and_soc(inverter_data)
                    # Learn PV efficiency for this hour
                    if readings and pv_peak_kw > 0:
                        with state_lock:
                            wx = state.get("weather")
                        cur_rad = float(wx.get("current_radiation_w", 0) if wx else 0)
                        if cur_rad > 50:  # ignore near-zero radiation (night/overcast)
                            tz_offset = int((wx or {}).get("utc_offset_seconds") or 0)
                            local_now = datetime.now(timezone.utc) + timedelta(seconds=tz_offset)
                            update_pv_efficiency(
                                month=local_now.month,
                                hour_of_day=local_now.hour,
                                actual_w=readings["input_power_w"],
                                radiation_wm2=cur_rad,
                                pv_peak_kw=pv_peak_kw,
                            )
                    # Reset fail counter on success
                    if notify_state["api_fail_notified"]:
                        bot = _get_bot(settings)
                        if bot and settings.get("tg_api_failure"):
                            bot.send(
                                f"✅ <b>Solis API recovered</b>\n"
                                f"Back online after {notify_state['api_fail_count_at_notify']} failed cycles"
                            )
                        notify_state["api_fail_notified"] = False
                    notify_state["api_fail_count"] = 0
                except SolisApiError as e:
                    error_str = f"Solis API error: {e}"
                    notify_state["api_fail_count"] += 1
                except Exception as e:
                    error_str = f"Solis unexpected error: {e}"
                    notify_state["api_fail_count"] += 1
            else:
                error_str = "Solis credentials not configured"

            # ── API Failsafe ─────────────────────────────────────
            if readings is None and miner_ip and notify_state["api_fail_count"] >= api_fail_cycles:
                if api_fail_action in ("stop", "start"):
                    try:
                        client = get_or_create_luxos(miner_ip)
                        if api_fail_action == "stop":
                            client.stop_mining()
                            action = "fail_stop"
                        elif not miner_hold_active:
                            client.start_mining()
                            action = "fail_start"
                    except LuxOsError as e:
                        error_str = (error_str or "") + f" | Fail-safe action error: {e}"

                if not notify_state["api_fail_notified"]:
                    notify_state["api_fail_notified"] = True
                    notify_state["api_fail_count_at_notify"] = notify_state["api_fail_count"]
                    bot = _get_bot(settings)
                    if bot and settings.get("tg_api_failure"):
                        if miner_hold_active and api_fail_action == "start":
                            # Manual hold overrides the start failsafe — be honest.
                            action_desc = "Miner kept OFF — disabled until midnight"
                        else:
                            action_desc = {
                                "stop":  "Miner stopped as failsafe",
                                "start": "Miner kept running (failsafe)",
                                "keep":  "No action taken — monitoring",
                            }.get(api_fail_action, "No action")
                        bot.send(
                            f"⚠️ <b>Solis API offline</b>\n"
                            f"{notify_state['api_fail_count']} consecutive failures\n"
                            f"Action: {action_desc}"
                        )

            # ── Control miner ────────────────────────────────────
            hashrate_mhs = 0.0

            if readings is not None and miner_ip:
                soc = readings["soc"]
                try:
                    client = get_or_create_luxos(miner_ip)
                    try:
                        actually_mining = client.is_mining()
                        hashrate_mhs = client.last_hashrate_mhs
                    except LuxOsError:
                        _clear_luxos_client()
                        client = get_or_create_luxos(miner_ip)
                        actually_mining = client.is_mining()
                        hashrate_mhs = client.last_hashrate_mhs

                    miner_running = actually_mining

                    # Verified confirmation for a manual start/stop command. Fires
                    # once the loop reads the miner actually at the requested state,
                    # independent of the tg_miner_onoff toggle; suppresses the
                    # duplicate auto on/off message for the same transition.
                    with state_lock:
                        pending = state.get("miner_cmd_pending")
                    if pending is not None:
                        bot = _get_bot(settings)
                        if actually_mining == pending["want"]:
                            if bot:
                                if actually_mining:
                                    bot.send("✅ <b>Miner is ON</b> — confirmed running.")
                                else:
                                    bot.send("🛑 <b>Miner is OFF</b> — disabled until midnight "
                                             "(or until you start it again).")
                            notify_state["prev_mining"] = actually_mining
                            with state_lock:
                                state["miner_cmd_pending"] = None
                        else:
                            pending["cycles"] += 1
                            if pending["cycles"] >= 6:
                                if bot:
                                    bot.send("⚠️ Couldn't confirm the miner changed state — "
                                             "please check it.")
                                with state_lock:
                                    state["miner_cmd_pending"] = None
                            else:
                                with state_lock:
                                    state["miner_cmd_pending"] = pending

                    # Hysteresis: smart start uses single threshold; normal mode uses on/off buffer
                    if smart_active:
                        if actually_mining:
                            # Already running — keep going based on SOC alone
                            desired_mining = soc >= effective_soc_on
                        else:
                            # Starting — also require minimum PV input so we don't
                            # start at midnight just because a sunny day is forecast
                            pv_gate_ok = readings["input_power_w"] >= smart_min_pv_w
                            desired_mining = soc >= effective_soc_on and pv_gate_ok
                    elif actually_mining:
                        desired_mining = soc >= effective_soc_off
                    else:
                        desired_mining = soc >= effective_soc_on

                    # Manual hold (via /stopMiner or dashboard Stop) overrides all
                    # automatic control and keeps the miner off until local midnight.
                    if miner_hold_active:
                        desired_mining = False

                    # EOD target override: project SOC at end of solar day and stop
                    # the miner early if running would cause us to miss the target.
                    eod_enabled = bool(settings.get("eod_soc_target_enabled", False))
                    eod_target  = float(settings.get("eod_soc_target") or 80.0)
                    battery_kwh = float(settings.get("battery_capacity_kwh") or 0.0)
                    eod_projected_with    = None
                    eod_projected_without = None
                    eod_protecting        = False
                    if eod_enabled and eod_target > 0.0 and battery_kwh > 0.0 and pv_peak_kw > 0.0:
                        hourly_wx   = (current_weather or {}).get("hourly", [])
                        load_profile = get_hourly_load_profile()
                        _eod_tz = int((current_weather or {}).get("utc_offset_seconds") or 0)
                        _eod_month = (datetime.now(timezone.utc) + timedelta(seconds=_eod_tz)).month
                        eff_map      = get_pv_efficiency(_eod_month)
                        eod_projected_with = _estimate_eod_soc(
                            soc=soc, battery_kwh=battery_kwh,
                            hourly_wx=hourly_wx, pv_peak_kw=pv_peak_kw,
                            eff_map=eff_map, load_profile=load_profile,
                            miner_power_w=miner_power_w, include_miner=True,
                        )
                        eod_projected_without = _estimate_eod_soc(
                            soc=soc, battery_kwh=battery_kwh,
                            hourly_wx=hourly_wx, pv_peak_kw=pv_peak_kw,
                            eff_map=eff_map, load_profile=load_profile,
                            miner_power_w=miner_power_w, include_miner=False,
                        )
                        if desired_mining and eod_projected_with is not None \
                                and eod_projected_with < eod_target:
                            desired_mining = False
                            eod_protecting = True

                    with state_lock:
                        state["eod_target"]            = eod_target if eod_enabled else None
                        state["eod_projected_with"]    = eod_projected_with
                        state["eod_projected_without"] = eod_projected_without
                        state["eod_protecting"]        = eod_protecting

                    if actually_mining != desired_mining:
                        if desired_mining:
                            try:
                                client.start_mining()
                                action = "started"
                            except LuxOsError:
                                _clear_luxos_client()
                                try:
                                    client = get_or_create_luxos(miner_ip)
                                    client.start_mining()
                                    action = "started"
                                except LuxOsError as e2:
                                    error_str = (error_str or "") + f" | Miner start error: {e2}"
                                    action = "error_starting"
                        else:
                            try:
                                client.stop_mining()
                                action = "stopped"
                            except LuxOsError:
                                _clear_luxos_client()
                                try:
                                    client = get_or_create_luxos(miner_ip)
                                    client.stop_mining()
                                    action = "stopped"
                                except LuxOsError as e2:
                                    error_str = (error_str or "") + f" | Miner stop error: {e2}"
                                    action = "error_stopping"
                    else:
                        action = "none"

                    # ── Telegram notifications ───────────────────
                    _send_notifications(
                        settings=settings,
                        soc=soc,
                        actually_mining=actually_mining,
                        smart_active=smart_active,
                        soc_on=soc_on,
                        effective_soc_on=effective_soc_on,
                        effective_soc_off=effective_soc_off,
                        hashrate_mhs=hashrate_mhs,
                        smart_hold_active=smart_hold_active,
                    )

                except LuxOsError as e:
                    error_str = (error_str or "") + f" | Miner error: {e}"
                    action = "error"
                except Exception as e:
                    error_str = (error_str or "") + f" | Miner unexpected error: {e}"
                    action = "error"

            elif not miner_ip and error_str is None:
                error_str = "Miner IP not configured"

            # ── Dehumidifier auto control ─────────────────────────
            dehum_id  = settings.get("dehum_device_id", "").strip()
            dehum_ip  = settings.get("dehum_ip", "").strip()
            dehum_key = settings.get("dehum_local_key", "").strip()
            dehum_auto = bool(settings.get("dehum_auto_enabled", False))
            dehum_threshold = float(settings.get("dehum_excess_threshold_w") or 500.0)
            dehum_ver = float(settings.get("dehum_version") or 3.3)
            min_run_s = float(settings.get("dehum_min_run_minutes") or 30) * 60
            min_off_s = float(settings.get("dehum_min_off_minutes") or 15) * 60

            if dehum_id and dehum_ip and dehum_key:
                try:
                    dc = DehumidifierClient(dehum_id, dehum_ip, dehum_key, dehum_ver)
                    status = dc.get_status()
                    if status:
                        dehum_power    = status["power"]
                        dehum_humidity = status["humidity"]
                        dehum_tank     = status["tank_full"]
                        dehum_auto_on  = False

                        now_ts = time.time()
                        with state_lock:
                            manual_until  = state.get("dehum_manual_override_until")
                            auto_on_since  = state.get("dehum_auto_on_since")
                            auto_off_since = state.get("dehum_auto_off_since")

                        # expire manual override
                        if manual_until and now_ts >= manual_until:
                            manual_until = None
                            with state_lock:
                                state["dehum_manual_override_until"] = None

                        if dehum_auto and readings is not None and not dehum_tank and not manual_until:
                            grid_w = readings.get("grid_power_w", 0) or 0

                            if not dehum_power and grid_w > dehum_threshold:
                                # Respect min-off time before turning back on
                                if auto_off_since is None or (now_ts - auto_off_since) >= min_off_s:
                                    dc.set_power(True)
                                    dehum_power = True
                                    auto_on_since = now_ts
                                    auto_off_since = None
                                dehum_auto_on = dehum_power
                            elif dehum_power and grid_w < (dehum_threshold - 200):
                                # Respect min-run time before turning off
                                if auto_on_since is None or (now_ts - auto_on_since) >= min_run_s:
                                    dc.set_power(False)
                                    dehum_power = False
                                    auto_off_since = now_ts
                                    auto_on_since = None
                                else:
                                    dehum_auto_on = True  # still in min-run window
                            elif dehum_power:
                                dehum_auto_on = True

                        with state_lock:
                            state["dehum_power"]               = dehum_power
                            state["dehum_humidity"]             = dehum_humidity
                            state["dehum_tank_full"]            = dehum_tank
                            state["dehum_auto_on"]              = dehum_auto_on
                            state["dehum_error"]                = None
                            state["dehum_auto_on_since"]        = auto_on_since
                            state["dehum_auto_off_since"]       = auto_off_since
                except Exception as e:
                    with state_lock:
                        state["dehum_error"] = str(e)

            now_str = datetime.now(timezone.utc).isoformat()

            with state_lock:
                state["readings"]          = readings
                state["miner_running"]     = miner_running
                state["last_updated"]      = now_str
                state["effective_soc_on"]  = effective_soc_on
                state["smart_start_active"] = smart_active
                state["smart_hold_active"]  = smart_hold_active
                state["miner_hold_active"]  = miner_hold_active
                state["smart_min_pv_w"]    = smart_min_pv_w
                state["cycle"]             = cycle_num
                state["error"]             = error_str
                state["action"]            = action

            if readings is not None:
                save_reading({
                    "ts":              now_str,
                    "soc":             readings["soc"],
                    "battery_power_w": readings["battery_power_w"],
                    "input_power_w":   readings["input_power_w"],
                    "grid_power_w":    readings["grid_power_w"],
                    "load_power_w":    readings["load_power_w"],
                    "backup_power_w":  readings["backup_power_w"],
                    "miner_running":   miner_running,
                    "action":          action,
                    "effective_soc_on": effective_soc_on,
                    "hashrate_mhs":    hashrate_mhs,
                    "radiation_wm2":   cur_rad,
                })

        except Exception as e:
            with state_lock:
                state["error"] = f"Control loop error: {e}"
            print(f"Control loop unhandled exception: {e}")

        elapsed = time.monotonic() - loop_start
        try:
            settings = get_settings()
            poll_interval = int(settings.get("poll_interval_seconds") or 60)
        except Exception:
            poll_interval = 60
        # Sleep until the next interval, or wake early on a manual refresh.
        control_refresh.wait(timeout=max(0.0, poll_interval - elapsed))
        control_refresh.clear()


# ── Weather loop ─────────────────────────────────────────────────

def weather_loop() -> None:
    while True:
        try:
            settings = get_settings()
            lat = float(settings.get("location_lat") or 0.0)
            lon = float(settings.get("location_lon") or 0.0)
            radiation_threshold = float(settings.get("radiation_threshold_wm2") or 300.0)

            if lat != 0.0 or lon != 0.0:
                raw = get_weather(lat, lon)
                if raw is not None:
                    parsed = parse_weather(raw, radiation_threshold=radiation_threshold)
                    now_str = datetime.now(timezone.utc).isoformat()
                    with state_lock:
                        state["weather"] = parsed
                        state["weather_last_updated"] = now_str
        except Exception as e:
            print(f"Weather loop error: {e}")

        weather_refresh.wait(timeout=1800)
        weather_refresh.clear()


# ── Pool loop ────────────────────────────────────────────────────

def _fetch_btc_price() -> Optional[float]:
    """Fetch current BTC/USD price from mempool.space (free, no auth)."""
    try:
        import requests as _req
        resp = _req.get("https://mempool.space/api/v1/prices", timeout=8)
        resp.raise_for_status()
        return float(resp.json().get("USD", 0) or 0) or None
    except Exception:
        return None


def pool_loop() -> None:
    while True:
        try:
            price = _fetch_btc_price()
            if price:
                with state_lock:
                    state["btc_price_usd"] = price

            settings = get_settings()
            api_key  = settings.get("lux_pool_api_key", "")
            username = settings.get("lux_pool_username", "")
            api_url  = settings.get("lux_pool_api_url", "")
            if api_key and username:
                client = LuxPoolClient(api_key=api_key, username=username, api_url=api_url)
                summary = client.get_summary()
                if summary is not None:
                    with state_lock:
                        state["pool"] = summary
                        state["pool_last_updated"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            print(f"Pool loop error: {e}")
        # Refresh every 5 minutes, or wake early on a manual refresh.
        pool_refresh.wait(timeout=300)
        pool_refresh.clear()


def _build_info_message() -> str:
    """Build the /info reply: current power readings and miner/dehumidifier toggles."""
    with state_lock:
        readings      = state.get("readings")
        miner_running = state.get("miner_running")
        miner_hold    = state.get("miner_hold_active")
        smart_active  = state.get("smart_start_active")
        pool          = state.get("pool") or {}
        dehum_power   = state.get("dehum_power")
        dehum_hum     = state.get("dehum_humidity")
        dehum_tank    = state.get("dehum_tank_full")

    lines = ["<b>⚡ Solar Miner status</b>"]
    if readings:
        lines.append(f"🔋 SOC: {readings['soc']:.1f}%")
        lines.append(f"☀️ PV input: {readings['input_power_w'] / 1000:.2f} kW")
        lines.append(f"🔌 Grid: {readings['grid_power_w'] / 1000:.2f} kW")
        lines.append(f"🏠 Load: {readings['load_power_w'] / 1000:.2f} kW")
        lines.append(f"🔋 Battery: {readings['battery_power_w'] / 1000:.2f} kW")
        lines.append(f"🔌 Backup: {readings['backup_power_w'] / 1000:.2f} kW")
    else:
        lines.append("⚠️ No Solis readings yet")

    if miner_running is None:
        miner_str = "unknown"
    else:
        miner_str = "ON ⛏" if miner_running else "OFF 💤"
    if miner_hold:
        miner_str += " — disabled until midnight"
    elif smart_active:
        miner_str += " 🧠"
    lines.append(f"⛏ Miner: <b>{miner_str}</b>")

    if dehum_power is None:
        dehum_str = "unknown"
    else:
        dehum_str = "ON" if dehum_power else "OFF"
    if dehum_hum is not None:
        dehum_str += f" ({dehum_hum:.0f}% RH)"
    if dehum_tank:
        dehum_str += " ⚠️ tank full"
    lines.append(f"💧 Dehumidifier: <b>{dehum_str}</b>")

    sats = pool.get("sats_today")
    if sats is not None:
        lines.append(f"💰 Sats (24h): {sats:,}")
    hr = pool.get("hashrate_ths")
    if hr:
        lines.append(f"⚙️ Hashrate: {hr:.1f} TH/s")

    return "\n".join(lines)


def _handle_telegram_command(cmd: str, bot: TelegramBot) -> None:
    """Dispatch an incoming Telegram command (already lower-cased, no leading slash)."""
    if cmd in ("startminer", "minerstart"):
        bot.send("⏳ Starting miner and resuming automatic control…")
        ok, err = _do_miner_start()
        if not ok:
            bot.send(f"❌ Couldn't start miner: {err}")
        # On success the control loop sends the verified ✅ confirmation.
    elif cmd in ("stopminer", "minerstop"):
        bot.send("⏳ Stopping miner and disabling it until midnight…")
        ok, err = _do_miner_stop()
        if not ok:
            bot.send(f"❌ Couldn't stop miner: {err}")
        # On success the control loop sends the verified 🛑 confirmation.
    elif cmd == "info":
        bot.send(_build_info_message())
    elif cmd in ("help", "start", "commands"):
        bot.send(
            "<b>Solar Miner commands</b>\n"
            "/startMiner — start mining &amp; resume automatic control\n"
            "/stopMiner — stop &amp; keep off until midnight\n"
            "/info — current power readings and toggles"
        )


def telegram_loop() -> None:
    """Long-poll Telegram for commands. Only acts on the configured chat."""
    offset = None
    webhook_cleared = False
    while True:
        try:
            settings = get_settings()
            token   = (settings.get("telegram_bot_token") or "").strip()
            chat_id = str(settings.get("telegram_chat_id") or "").strip()
            if not token or not chat_id:
                time.sleep(10)
                continue
            bot = TelegramBot(token, chat_id)
            if not webhook_cleared:
                bot.delete_webhook()
                webhook_cleared = True
            updates = bot.get_updates(offset=offset, timeout=25)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                # Security: only the configured chat may control the miner.
                if str((msg.get("chat") or {}).get("id")) != chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                cmd = text.split()[0].lstrip("/").split("@")[0].lower()
                try:
                    _handle_telegram_command(cmd, bot)
                except Exception as e:
                    print(f"Telegram command error: {e}")
            if not updates:
                time.sleep(3)  # avoid hammering on auth/network errors
        except Exception as e:
            print(f"Telegram loop error: {e}")
            time.sleep(5)


# ── Flask routes ─────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Wake all background loops for an immediate re-poll of every data source."""
    control_refresh.set()
    weather_refresh.set()
    pool_refresh.set()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    with state_lock:
        snapshot = dict(state)
    settings = get_settings()
    tz_offset = int((snapshot.get("weather") or {}).get("utc_offset_seconds") or 0)
    _cur_month = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).month
    snapshot["pv_efficiency"] = get_pv_efficiency(_cur_month)
    snapshot["pv_peak_kw"] = float(settings.get("pv_peak_kw") or 0.0)
    snapshot["eod_soc_target"]         = float(settings.get("eod_soc_target") or 80.0)
    snapshot["eod_soc_target_enabled"] = bool(settings.get("eod_soc_target_enabled", False))
    snapshot["today_sats"] = (snapshot.get("pool") or {}).get("sats_today", 0) or 0
    snapshot["sunny_hours_threshold"] = float(settings.get("sunny_hours_threshold") or 3.0)
    # Derive hold state from settings here too (not just the control-loop value) so
    # the dashboard reflects a Hold/Resume click immediately, not on the next poll.
    local_today = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).date().isoformat()
    snapshot["smart_hold_active"] = bool(settings.get("smart_hold_date")) and \
        settings.get("smart_hold_date") == local_today
    snapshot["miner_hold_active"] = bool(settings.get("miner_hold_date")) and \
        settings.get("miner_hold_date") == local_today
    snapshot["app_version"] = APP_VERSION
    return jsonify(snapshot)


@app.route("/api/history")
def api_history():
    hours = int(request.args.get("hours", 2))
    rows = get_recent_readings(hours)
    return jsonify(rows)


def _fetch_daily_sats_rows(days: int, include_today: bool = True) -> list[dict]:
    """
    Returns [{date, sats}] for `days` calendar days (local time).
    Historical days come from Luxor's settled revenue API (cached per UTC day).
    When include_today is True the most recent bar is today and uses the live
    rolling-24h sats_today from the pool summary; when False the most recent bar
    is yesterday and every bar comes from the settled revenue API (avoids the
    rolling-24h window bleeding yesterday's earnings onto today's bar).
    """
    settings = get_settings()
    api_key  = settings.get("lux_pool_api_key", "")
    username = settings.get("lux_pool_username", "")
    api_url  = settings.get("lux_pool_api_url", "")

    with state_lock:
        tz_offset  = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
        today_live = (state.get("pool") or {}).get("sats_today", 0) or 0

    local_today = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).date()
    today_str   = local_today.isoformat()
    # Most recent bar: today, or yesterday when today is excluded.
    last_day    = local_today if include_today else local_today - timedelta(days=1)

    history_by_date: dict = {}
    if api_key and username:
        # Luxor finalizes each day's revenue at 05:00 UTC. Key the cache on that
        # boundary (now - 5h) so we refetch right when new data posts, instead of
        # at 00:00 UTC — which would otherwise cache a still-empty prior day for
        # up to a full day.
        lux_day = (datetime.now(timezone.utc) - timedelta(hours=5)).date()
        with _revenue_lock:
            if _revenue_cache["utc_date"] != lux_day:
                try:
                    client = LuxPoolClient(api_key=api_key, username=username, api_url=api_url)
                    start = (local_today - timedelta(days=days + 1)).isoformat()
                    _revenue_cache["rows"]     = client.get_revenue_history(start, today_str)
                    _revenue_cache["utc_date"] = lux_day
                except Exception as e:
                    print(f"Revenue history fetch error: {e}")
            history_by_date = {r["date"]: r["sats"] for r in _revenue_cache["rows"]}

    if include_today:
        history_by_date[today_str] = today_live

    return [
        {"date": (last_day - timedelta(days=i)).isoformat(),
         "sats": history_by_date.get((last_day - timedelta(days=i)).isoformat(), 0)}
        for i in range(days - 1, -1, -1)
    ]


@app.route("/api/daily_sats")
def api_daily_sats():
    days = int(request.args.get("days", 7))
    include_today = request.args.get("include_today", "1") != "0"
    return jsonify(_fetch_daily_sats_rows(days, include_today))


@app.route("/api/dehum/test")
def api_dehum_test():
    settings = get_settings()
    dehum_id  = settings.get("dehum_device_id", "").strip()
    dehum_ip  = settings.get("dehum_ip", "").strip()
    dehum_key = settings.get("dehum_local_key", "").strip()
    dehum_ver = float(settings.get("dehum_version") or 3.3)
    if not (dehum_id and dehum_ip and dehum_key):
        return jsonify({"error": "Not configured"}), 400
    try:
        dc = DehumidifierClient(dehum_id, dehum_ip, dehum_key, dehum_ver)
        raw = dc.raw_status()
        return jsonify({"raw": raw})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dehum/power", methods=["POST"])
def api_dehum_power():
    on = request.json.get("on")
    if on is None:
        return jsonify({"error": "missing 'on' field"}), 400
    settings = get_settings()
    dehum_id  = settings.get("dehum_device_id", "").strip()
    dehum_ip  = settings.get("dehum_ip", "").strip()
    dehum_key = settings.get("dehum_local_key", "").strip()
    dehum_ver = float(settings.get("dehum_version") or 3.3)
    if not (dehum_id and dehum_ip and dehum_key):
        return jsonify({"error": "Dehumidifier not configured"}), 400
    try:
        override_hours = float(settings.get("dehum_manual_override_hours") or 2)
        dc = DehumidifierClient(dehum_id, dehum_ip, dehum_key, dehum_ver)
        ok = dc.set_power(bool(on))
        if ok:
            with state_lock:
                state["dehum_power"] = bool(on)
                state["dehum_auto_on"] = False
                state["dehum_manual_override_until"] = time.time() + override_hours * 3600
                state["dehum_auto_on_since"] = None
                state["dehum_auto_off_since"] = None
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset_efficiency", methods=["POST"])
def api_reset_efficiency():
    reset_pv_efficiency()
    return jsonify({"ok": True})


@app.route("/api/pv_efficiency")
def api_pv_efficiency():
    settings = get_settings()
    pv_peak_kw = float(settings.get("pv_peak_kw") or 0.0)
    rows = get_pv_efficiency_detail()
    with state_lock:
        tz_offset = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
    current_month = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).month
    return jsonify({"rows": rows, "pv_peak_kw": pv_peak_kw, "current_month": current_month})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    settings = get_settings()
    secret = settings.get("solis_api_secret", "")
    if len(secret) > 4:
        settings["solis_api_secret"] = "••••" + secret[-4:]
    else:
        settings["solis_api_secret"] = "••••"
    if settings.get("dehum_local_key"):
        settings["dehum_local_key"] = "••••"
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def api_post_settings():
    data = request.get_json(force=True) or {}
    if "solis_api_secret" in data and str(data["solis_api_secret"]).startswith("••••"):
        del data["solis_api_secret"]
    if "dehum_local_key" in data and str(data["dehum_local_key"]).startswith("••••"):
        del data["dehum_local_key"]

    numeric_keys = [
        "poll_interval_seconds", "soc_on_threshold", "soc_off_threshold",
        "smart_soc_on_threshold", "smart_soc_off_threshold",
        "sunny_hours_threshold", "radiation_threshold_wm2", "smart_min_pv_w",
        "location_lat", "location_lon",
        "battery_capacity_kwh", "pv_peak_kw", "miner_power_w",
        "eod_soc_target", "dehum_excess_threshold_w", "dehum_version",
        "dehum_min_run_minutes", "dehum_min_off_minutes", "dehum_manual_override_hours",
        "api_fail_cycles",
        "tg_soc_low_pct", "tg_hashrate_drop_pct", "tg_daily_hour",
        "tg_sats_milestone_amount", "tg_weekly_recap_day", "tg_weekly_recap_hour",
        "tg_sunny_day_hour",
    ]
    for k in numeric_keys:
        if k in data:
            try:
                if k in ("poll_interval_seconds", "api_fail_cycles", "tg_daily_hour",
                         "tg_sats_milestone_amount", "tg_weekly_recap_day", "tg_weekly_recap_hour",
                         "tg_sunny_day_hour"):
                    data[k] = int(data[k])
                else:
                    data[k] = float(data[k])
            except (ValueError, TypeError):
                pass

    bool_keys = [
        "smart_start_enabled", "eod_soc_target_enabled", "dehum_auto_enabled",
        "tg_miner_onoff", "tg_smart_start", "tg_api_failure",
        "tg_soc_low", "tg_soc_full", "tg_hashrate_drop",
        "tg_daily_summary", "tg_sats_milestone", "tg_sunny_day_ahead", "tg_weekly_recap",
    ]
    for k in bool_keys:
        if k in data:
            val = data[k]
            data[k] = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes", "on")

    update_settings(data)

    if float(data.get("location_lat") or 0) or float(data.get("location_lon") or 0):
        weather_refresh.set()

    return jsonify({"ok": True})


@app.route("/api/geocode")
def api_geocode():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "missing q parameter"}), 400
    result = do_geocode(q)
    if result is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(result)


@app.route("/api/miner/start", methods=["POST"])
def api_miner_start():
    ok, err = _do_miner_start()
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": err}), 400 if err == "Miner IP not configured" else 500


@app.route("/api/miner/stop", methods=["POST"])
def api_miner_stop():
    ok, err = _do_miner_stop()
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": err}), 400 if err == "Miner IP not configured" else 500


@app.route("/api/smart/hold", methods=["POST"])
def api_smart_hold():
    """Pause/resume Smart Start until local midnight.

    hold=true stores today's local date; Smart Start stays off (miner reverts to
    normal thresholds) until the local date rolls over at midnight. hold=false
    clears it immediately.
    """
    data = request.get_json(force=True) or {}
    hold = bool(data.get("hold"))
    with state_lock:
        tz_offset = int((state.get("weather") or {}).get("utc_offset_seconds") or 0)
    local_today = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).date().isoformat()
    update_settings({"smart_hold_date": local_today if hold else ""})
    with state_lock:
        state["smart_hold_active"] = hold
    return jsonify({"ok": True, "hold": hold})


@app.route("/api/miner/quick")
def api_miner_quick():
    """Direct poll of the miner — bypasses control loop for fast status updates."""
    try:
        settings = get_settings()
        miner_ip = settings.get("miner_ip") or os.getenv("LUXOS_MINER_IP", "")
        if not miner_ip:
            return jsonify({"error": "Miner IP not configured"}), 400
        client = get_or_create_luxos(miner_ip)
        mining = client.is_mining()
        hashrate_mhs = client.last_hashrate_mhs
        with state_lock:
            state["miner_running"] = mining
        return jsonify({"mining": mining, "hashrate_mhs": hashrate_mhs})
    except LuxOsError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    settings = get_settings()
    bot = _get_bot(settings)
    if not bot:
        return jsonify({"error": "Telegram not configured — add bot token and chat ID"}), 400
    validation = bot.validate()
    if not validation.startswith("@"):
        return jsonify({"error": validation}), 400
    ok = bot.send(
        f"✅ <b>Solar Miner connected!</b>\n"
        f"Notifications are working. Bot: {validation}"
    )
    if ok:
        return jsonify({"ok": True, "bot": validation})
    return jsonify({"error": "Message send failed — check chat ID"}), 500


@app.route("/api/pool/status")
def api_pool_status():
    with state_lock:
        pool = state.get("pool")
        ts   = state.get("pool_last_updated")
    return jsonify({"pool": pool, "last_updated": ts})


if __name__ == "__main__":
    init_db()

    # Prevent duplicate notifications on restart — seed with today so they
    # don't re-fire if the container restarts within the same trigger hour.
    notify_state["last_daily_date"] = date.today().isoformat()
    notify_state["last_weekly_recap_date"] = date.today().isoformat()
    notify_state["last_sunny_day_date"] = date.today().isoformat()

    settings_now = get_settings()
    seed_map = {
        "SOLIS_API_KEY":    "solis_api_key",
        "SOLIS_API_SECRET": "solis_api_secret",
        "SOLIS_INVERTER_SN":"solis_inverter_sn",
        "LUXOS_MINER_IP":   "miner_ip",
    }
    seeds = {}
    for env_key, db_key in seed_map.items():
        env_val = os.getenv(env_key, "")
        if env_val and not settings_now.get(db_key):
            seeds[db_key] = env_val
    if seeds:
        update_settings(seeds)

    t_control = threading.Thread(target=control_loop, daemon=True, name="control-loop")
    t_control.start()

    t_weather = threading.Thread(target=weather_loop, daemon=True, name="weather-loop")
    t_weather.start()

    t_pool = threading.Thread(target=pool_loop, daemon=True, name="pool-loop")
    t_pool.start()

    t_telegram = threading.Thread(target=telegram_loop, daemon=True, name="telegram-loop")
    t_telegram.start()

    app.run(host="0.0.0.0", port=3000, debug=False)
