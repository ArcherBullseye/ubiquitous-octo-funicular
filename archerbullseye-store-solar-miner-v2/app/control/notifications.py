"""Edge-triggered Telegram notifications.

Notifier keeps the previous-cycle baselines so alerts fire on transitions,
not continuously. None baselines mean "not yet primed": the first cycle after
a (re)start establishes a silent baseline so alerts don't re-fire on every
upgrade or container restart.
"""
from datetime import date
from typing import Optional

from ..clients.telegram import TelegramBot
from ..db import get_recent_readings
from ..localtime import local_now
from ..poolstats import fetch_daily_sats_rows
from ..state import AppState


def get_bot(settings: dict) -> Optional[TelegramBot]:
    token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    if token and chat_id:
        return TelegramBot(token, chat_id)
    return None


def fmt_ths(mhs: float) -> str:
    if mhs <= 0:
        return "0 MH/s"
    if mhs >= 1_000_000:
        return f"{mhs / 1_000_000:.1f} TH/s"
    if mhs >= 1_000:
        return f"{mhs / 1_000:.1f} GH/s"
    return f"{mhs:.0f} MH/s"


class Notifier:
    def __init__(self, state: AppState):
        self.state = state
        # Seed date-keyed alerts with today so they don't re-fire when the
        # container restarts within the same trigger hour.
        today = date.today().isoformat()
        self.api_fail_count = 0
        self.api_fail_notified = False
        self.api_fail_count_at_notify = 0
        self.prev_mining: Optional[bool] = None
        self.prev_smart_active: Optional[bool] = None
        self.soc_low_notified: Optional[bool] = None
        self.soc_full_notified: Optional[bool] = None
        self.prev_hashrate_mhs: Optional[float] = None
        self.last_daily_date: Optional[str] = today
        self.last_weekly_recap_date: Optional[str] = today
        self.last_sunny_day_date: Optional[str] = today
        self.sats_milestone_last: Optional[int] = None
        self.sats_milestone_date: Optional[str] = None

    def send_cycle_alerts(self, settings: dict, soc: Optional[float], actually_mining: bool,
                          smart_active: bool, soc_on: float, effective_soc_on: float,
                          effective_soc_off: float, hashrate_mhs: float,
                          smart_hold_active: bool = False, ramp_armed: bool = False) -> None:
        bot = get_bot(settings)
        if not bot:
            return

        tz = self.state.tz_offset_seconds()
        wx = self.state.weather()

        # ── Miner ON / OFF ──────────────────────────────────────
        # Suppressed while the ramp is armed: it sleeps/wakes miners at dawn,
        # dusk, and whenever surplus crosses the lowest-profile floor — those
        # are normal ramp moves, not events worth alerting on.
        if settings.get("tg_miner_onoff") and self.prev_mining is not None and not ramp_armed:
            if actually_mining and not self.prev_mining and soc is not None:
                smart_tag = " 🧠 <i>Smart Start</i>" if smart_active else ""
                bot.send(
                    f"⛏ <b>Miner started</b>{smart_tag}\n"
                    f"SOC: {soc:.1f}% | Threshold: {effective_soc_on:.0f}%"
                )
            elif not actually_mining and self.prev_mining and soc is not None:
                bot.send(
                    f"💤 <b>Miner stopped</b>\n"
                    f"SOC: {soc:.1f}% | Off threshold: {effective_soc_off:.0f}%"
                )

        # ── Smart Start ON / OFF ─────────────────────────────────
        if settings.get("tg_smart_start") and self.prev_smart_active is not None:
            sunny = wx.get("remaining_sunny_hours", 0) if wx else 0
            if smart_active and not self.prev_smart_active:
                bot.send(
                    f"🧠 <b>Smart Start activated</b>\n"
                    f"☀️ {sunny} sunny hours remaining\n"
                    f"Mining from {effective_soc_on:.0f}% SOC (normal: {soc_on:.0f}%)"
                )
            elif not smart_active and self.prev_smart_active:
                bot.send(
                    f"🌤 <b>Smart Start deactivated</b>\n"
                    f"Returning to normal {soc_on:.0f}% SOC threshold"
                )

        # ── SOC low warning ──────────────────────────────────────
        if settings.get("tg_soc_low") and soc is not None:
            low_pct = float(settings.get("tg_soc_low_pct") or 20.0)
            if self.soc_low_notified is None:
                self.soc_low_notified = soc < low_pct   # prime silently
            elif soc < low_pct and not self.soc_low_notified:
                bot.send(
                    f"🔋 <b>Battery low: {soc:.1f}%</b>\n"
                    f"Miner is {'ON ⛏' if actually_mining else 'OFF 💤'}"
                )
                self.soc_low_notified = True
            elif soc >= low_pct + 5:
                self.soc_low_notified = False

        # ── Battery fully charged ────────────────────────────────
        if settings.get("tg_soc_full") and soc is not None:
            if self.soc_full_notified is None:
                self.soc_full_notified = soc >= 99.5
            elif soc >= 99.5 and not self.soc_full_notified:
                bot.send(
                    f"🌞 <b>Battery fully charged!</b>\n"
                    f"{'Miner running ⛏' if actually_mining else 'Ready to mine whenever you need'}"
                )
                self.soc_full_notified = True
            elif soc < 95:
                self.soc_full_notified = False

        # ── Hashrate drop ────────────────────────────────────────
        # Suppressed while the ramp is armed — dialing a miner down is a
        # deliberate hashrate drop, not a fault. Keep the baseline current so
        # it doesn't false-alarm the moment the ramp is disarmed.
        if settings.get("tg_hashrate_drop") and hashrate_mhs > 0 and actually_mining:
            if ramp_armed:
                self.prev_hashrate_mhs = hashrate_mhs
            else:
                drop_pct = float(settings.get("tg_hashrate_drop_pct") or 25.0)
                prev_hr = self.prev_hashrate_mhs or 0
                if prev_hr > 0:
                    drop = (prev_hr - hashrate_mhs) / prev_hr * 100
                    if drop >= drop_pct:
                        bot.send(
                            f"📉 <b>Hashrate dropped {drop:.0f}%</b>\n"
                            f"{fmt_ths(hashrate_mhs)} (was {fmt_ths(prev_hr)})\n"
                            f"Possible thermal throttling or board issue"
                        )
                self.prev_hashrate_mhs = hashrate_mhs

        # ── Sats milestone ───────────────────────────────────────
        if settings.get("tg_sats_milestone"):
            pool = self.state.get("pool") or {}
            pool_sats = pool.get("sats_today", 0) or 0
            milestone = int(settings.get("tg_sats_milestone_amount") or 1000)
            if pool_sats > 0 and milestone > 0:
                local_today = local_now(tz).strftime("%Y-%m-%d")
                current_ms = (pool_sats // milestone) * milestone
                if self.sats_milestone_last is None or self.sats_milestone_date != local_today:
                    # Prime silently on the first cycle and at each local-day
                    # rollover. sats_today is a rolling 24h figure, so baseline
                    # to the current value: announce milestones climbed to
                    # during the new day without re-spamming carried-over
                    # totals at midnight or on restart.
                    self.sats_milestone_last = current_ms
                    self.sats_milestone_date = local_today
                elif current_ms > self.sats_milestone_last:
                    self.sats_milestone_last = current_ms
                    bot.send(
                        f"💰 <b>Sats milestone reached!</b>\n"
                        f"{current_ms:,}+ sats earned today\n"
                        f"({pool_sats:,} sats total)"
                    )

        # ── Daily summary ────────────────────────────────────────
        if settings.get("tg_daily_summary"):
            target_hour = int(settings.get("tg_daily_hour") or 7)
            lnow = local_now(tz)
            today = lnow.strftime("%Y-%m-%d")
            if lnow.hour == target_hour and self.last_daily_date != today:
                self.last_daily_date = today
                rows = get_recent_readings(hours=24)
                if rows:
                    poll_sec = int(settings.get("poll_interval_seconds") or 60)
                    mining_cycles = sum(1 for r in rows if r.get("miner_running"))
                    mining_hours = mining_cycles * poll_sec / 3600
                    peak_soc = max(r["soc"] for r in rows)
                    min_soc = min(r["soc"] for r in rows)
                    hr_vals = [r.get("hashrate_mhs") or 0 for r in rows if r.get("miner_running")]
                    avg_hr = (sum(hr_vals) / len(hr_vals)) if hr_vals else 0
                    pool = self.state.get("pool") or {}
                    sats_str = f"{pool.get('sats_today', 0):,} sats" if pool else "N/A"
                    bot.send(
                        f"📊 <b>Daily Mining Summary</b>\n"
                        f"⛏ Mining time: {mining_hours:.1f}h\n"
                        f"⚡ Avg hashrate: {fmt_ths(avg_hr)}\n"
                        f"📈 Peak SOC: {peak_soc:.1f}%\n"
                        f"📉 Min SOC: {min_soc:.1f}%\n"
                        f"💰 Sats today: {sats_str}"
                    )

        # ── Weekly recap ─────────────────────────────────────────
        if settings.get("tg_weekly_recap"):
            recap_day = int(settings.get("tg_weekly_recap_day") or 0)   # 0=Mon … 6=Sun
            recap_hour = int(settings.get("tg_weekly_recap_hour") or 8)
            lnow = local_now(tz)
            today = lnow.strftime("%Y-%m-%d")
            if lnow.weekday() == recap_day and lnow.hour == recap_hour \
                    and self.last_weekly_recap_date != today:
                self.last_weekly_recap_date = today
                rows = fetch_daily_sats_rows(self.state, 7)
                if rows:
                    total = sum(r["sats"] for r in rows)
                    lines = "\n".join(f"  {r['date']}: {r['sats']:,}" for r in rows)
                    bot.send(
                        f"📅 <b>Weekly Sats Recap</b>\n"
                        f"{lines}\n"
                        f"─────────────────\n"
                        f"<b>Total: {total:,} sats</b>"
                    )

        # ── Good solar day ahead ─────────────────────────────────
        # A morning heads-up, not a live event: at most once per local day,
        # only during the target hour, only when Smart Start is enabled, not
        # manually held, and the forecast clears the sunny-hours threshold.
        if settings.get("tg_sunny_day_ahead") and wx:
            lnow = local_now(tz)
            local_today = lnow.strftime("%Y-%m-%d")
            target_hour = int(settings.get("tg_sunny_day_hour") or 8)
            sunny = wx.get("remaining_sunny_hours", 0) or 0
            sunny_thresh = float(settings.get("sunny_hours_threshold") or 3.0)
            smart_on = float(settings.get("smart_soc_on_threshold") or 60.0)
            if (lnow.hour == target_hour
                    and self.last_sunny_day_date != local_today
                    and sunny >= sunny_thresh
                    and bool(settings.get("smart_start_enabled", True))
                    and not smart_hold_active):
                self.last_sunny_day_date = local_today
                bot.send(
                    f"☀️ <b>Good solar day ahead!</b>\n"
                    f"{int(sunny)} sunny hours remaining\n"
                    f"Smart Start will activate at {smart_on:.0f}% SOC"
                )

        # Update previous state for next cycle
        self.prev_mining = actually_mining
        self.prev_smart_active = smart_active
