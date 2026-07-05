"""Telegram command loop — long-polls the bot and dispatches commands.

Security: only messages from the configured chat id are acted on.
"""
import time

from ..clients.telegram import TelegramBot
from ..db import get_settings
from ..devices.miner import MinerFleet
from ..state import AppState
from .commands import start_miners, stop_miners


def build_info_message(state: AppState) -> str:
    """The /info reply: current power readings and miner/dehumidifier toggles."""
    s = state.snapshot()
    readings = s.get("readings")
    miner_running = s.get("miner_running")
    miners = dict(s.get("miners") or {})
    miner_hold = s.get("miner_hold_active")
    smart_active = s.get("smart_start_active")
    pool = s.get("pool") or {}
    dehum_power = s.get("dehum_power")
    dehum_hum = s.get("dehum_humidity")
    dehum_tank = s.get("dehum_tank_full")

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
    lines.append(f"⛏ Miners: <b>{miner_str}</b>")
    # Per-miner breakdown when more than one is configured.
    if len(miners) > 1:
        for label in sorted(miners):
            m = miners[label]
            if not m.get("reachable"):
                per_str = "unreachable ⚠️"
            elif m.get("running") is None:
                per_str = "unknown"
            else:
                per_str = "ON ⛏" if m.get("running") else "OFF 💤"
            if m.get("hold"):
                per_str += " (held until midnight)"
            lines.append(f"   • {label}: {per_str}")

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


def handle_command(cmd: str, bot: TelegramBot, state: AppState, fleet: MinerFleet) -> None:
    """Dispatch an incoming command (already lower-cased, no leading slash)."""
    if cmd in ("startminer", "minerstart"):
        bot.send("⏳ Starting miner and resuming automatic control…")
        ok, err = start_miners(state, fleet)
        if not ok:
            bot.send(f"❌ Couldn't start miner: {err}")
        # On success the control loop sends the verified ✅ confirmation.
    elif cmd in ("stopminer", "minerstop"):
        bot.send("⏳ Stopping miner and disabling it until midnight…")
        ok, err = stop_miners(state, fleet)
        if not ok:
            bot.send(f"❌ Couldn't stop miner: {err}")
        # On success the control loop sends the verified 🛑 confirmation.
    elif cmd == "info":
        bot.send(build_info_message(state))
    elif cmd in ("help", "start", "commands"):
        bot.send(
            "<b>Solar Miner commands</b>\n"
            "/startMiner — start mining &amp; resume automatic control\n"
            "/stopMiner — stop &amp; keep off until midnight\n"
            "/info — current power readings and toggles"
        )


def telegram_loop(state: AppState, fleet: MinerFleet) -> None:
    offset = None
    webhook_cleared = False
    while True:
        try:
            settings = get_settings()
            token = (settings.get("telegram_bot_token") or "").strip()
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
                    handle_command(cmd, bot, state, fleet)
                except Exception as e:
                    print(f"Telegram command error: {e}")
            if not updates:
                time.sleep(3)  # avoid hammering on auth/network errors
        except Exception as e:
            print(f"Telegram loop error: {e}")
            time.sleep(5)
