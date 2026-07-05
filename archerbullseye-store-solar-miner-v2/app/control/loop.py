"""The control loop — one cycle every poll interval:

  1. Read settings, decide Smart Start thresholds (weather + learned PV eff)
  2. Poll the Solis inverter (learning PV efficiency as it goes)
  3. Failsafe if the Solis API has been down for N cycles
  4. Read every miner (phase 1), verify pending manual commands
  5. Decide the shared desired state (hysteresis, PV gate, EOD protection)
  6. Drive the miners (phase 3): ramp when armed, else binary on/off
  7. Telegram alerts, dehumidifier auto-control, state + history write
"""
import os
import time
from typing import Optional

from ..clients.solis import SolisApiError, SolisClient, parse_power_and_soc
from ..db import (get_hourly_load_profile, get_pv_efficiency, get_settings,
                  save_reading, update_pv_efficiency)
from ..devices.dehumidifier import Dehumidifier, run_auto_control
from ..devices.miner import MinerFleet
from ..localtime import local_now, local_today_str, utcnow_str
from ..state import AppState
from . import eod, smart_start
from .notifications import Notifier, get_bot
from .ramp import RampController


class ControlLoop:
    def __init__(self, state: AppState, fleet: MinerFleet,
                 ramp: RampController, notifier: Notifier):
        self.state = state
        self.fleet = fleet
        self.ramp = ramp
        self.notifier = notifier

    def run_forever(self) -> None:
        while True:
            loop_start = time.monotonic()
            try:
                self.run_cycle()
            except Exception as e:
                self.state.set("error", f"Control loop error: {e}")
                print(f"Control loop unhandled exception: {e}")

            elapsed = time.monotonic() - loop_start
            try:
                poll_interval = int(get_settings().get("poll_interval_seconds") or 60)
            except Exception:
                poll_interval = 60
            # Sleep until the next interval, or wake early on a manual refresh.
            self.state.control_refresh.wait(timeout=max(0.0, poll_interval - elapsed))
            self.state.control_refresh.clear()

    # ── One cycle ────────────────────────────────────────────────

    def run_cycle(self) -> None:
        state = self.state
        settings = get_settings()

        api_key = settings.get("solis_api_key") or os.getenv("SOLIS_API_KEY", "")
        api_secret = settings.get("solis_api_secret") or os.getenv("SOLIS_API_SECRET", "")
        inverter_sn = settings.get("solis_inverter_sn") or os.getenv("SOLIS_INVERTER_SN", "")
        miners = self.fleet.configured(settings)

        soc_on = float(settings.get("soc_on_threshold") or 85.0)
        smart_min_pv_w = float(settings.get("smart_min_pv_w") or 1000.0)
        api_fail_action = settings.get("api_fail_action", "stop")
        api_fail_cycles = int(settings.get("api_fail_cycles") or 3)
        pv_peak_kw = float(settings.get("pv_peak_kw") or 0.0)
        miner_power_w = self.fleet.total_nameplate_w(settings)

        weather = state.weather()
        cycle_num = state.get("cycle", 0) + 1
        tz = state.tz_offset_seconds()
        local_today = local_today_str(tz)
        local_month = local_now(tz).month

        # Smart Start manual hold — paused until local midnight.
        smart_hold_active = bool(settings.get("smart_hold_date")) and \
            settings.get("smart_hold_date") == local_today
        # Per-miner manual hold — that miner stays off regardless of SOC or
        # Smart Start; the other miner is unaffected. Rolls off at midnight.
        miner_hold = self.fleet.hold_map(settings, local_today)
        miner_hold_active = bool(miner_hold) and all(miner_hold.values())

        efficiency_map = get_pv_efficiency(local_month)
        smart = smart_start.decide(settings, weather, efficiency_map,
                                   miner_power_w, smart_hold_active)

        # ── Poll Solis ───────────────────────────────────────────
        readings = None
        error_str: Optional[str] = None
        action = "none"
        cur_rad = 0.0
        miner_running = None

        if api_key and api_secret and inverter_sn:
            try:
                solis = SolisClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    base_url=settings.get("solis_base_url", "https://www.soliscloud.com:13333"),
                )
                readings = parse_power_and_soc(solis.get_inverter_detail(inverter_sn))
                # Learn PV efficiency for this hour
                if readings and pv_peak_kw > 0:
                    cur_rad = float((weather or {}).get("current_radiation_w", 0) or 0)
                    if cur_rad > 50:  # ignore near-zero radiation (night/overcast)
                        lnow = local_now(tz)
                        update_pv_efficiency(
                            month=lnow.month, hour_of_day=lnow.hour,
                            actual_w=readings["input_power_w"],
                            radiation_wm2=cur_rad, pv_peak_kw=pv_peak_kw,
                        )
                # Recovery notice + reset fail counter on success
                if self.notifier.api_fail_notified:
                    bot = get_bot(settings)
                    if bot and settings.get("tg_api_failure"):
                        bot.send(
                            f"✅ <b>Solis API recovered</b>\n"
                            f"Back online after {self.notifier.api_fail_count_at_notify} failed cycles"
                        )
                    self.notifier.api_fail_notified = False
                self.notifier.api_fail_count = 0
            except SolisApiError as e:
                error_str = f"Solis API error: {e}"
                self.notifier.api_fail_count += 1
            except Exception as e:
                error_str = f"Solis unexpected error: {e}"
                self.notifier.api_fail_count += 1
        else:
            error_str = "Solis credentials not configured"

        # ── API failsafe ─────────────────────────────────────────
        if readings is None and miners and self.notifier.api_fail_count >= api_fail_cycles:
            if api_fail_action in ("stop", "start"):
                # Apply to every miner independently — an unreachable one is
                # skipped so the others still act.
                for m in miners:
                    if api_fail_action == "stop":
                        ok, err = m.try_set_power(False)
                        if ok:
                            action = "fail_stop"
                    elif not miner_hold.get(m.label):
                        # Start failsafe — but never override this miner's hold.
                        ok, err = m.try_set_power(True)
                        if ok:
                            action = "fail_start"
                    else:
                        ok, err = True, None
                    if not ok:
                        error_str = (error_str or "") + f" | {m.label} fail-safe error: {err}"

            if not self.notifier.api_fail_notified:
                self.notifier.api_fail_notified = True
                self.notifier.api_fail_count_at_notify = self.notifier.api_fail_count
                bot = get_bot(settings)
                if bot and settings.get("tg_api_failure"):
                    if miner_hold_active and api_fail_action == "start":
                        action_desc = "Miner kept OFF — disabled until midnight"
                    else:
                        action_desc = {
                            "stop":  "Miner stopped as failsafe",
                            "start": "Miner kept running (failsafe)",
                            "keep":  "No action taken — monitoring",
                        }.get(api_fail_action, "No action")
                    bot.send(
                        f"⚠️ <b>Solis API offline</b>\n"
                        f"{self.notifier.api_fail_count} consecutive failures\n"
                        f"Action: {action_desc}"
                    )

        # ── Control miners ───────────────────────────────────────
        # Two-phase: read every miner, compute one shared desired state from
        # the aggregate, then drive each reachable miner to it.
        hashrate_mhs = 0.0
        per_miner: dict = {}
        eod_state = {"eod_target": None, "eod_projected_with": None,
                     "eod_projected_without": None, "eod_protecting": False}
        ramp_armed = False

        if readings is not None and miners:
            soc = readings["soc"]

            # Phase 1 — read each miner's current state (never raises).
            for m in miners:
                r = m.read()
                r["action"] = "none"
                per_miner[m.label] = r

            reachable = {l: r for l, r in per_miner.items() if r["reachable"]}
            if reachable:
                actually_mining = any(bool(r["running"]) for r in reachable.values())
                hashrate_mhs = sum(r["hashrate_mhs"] or 0.0 for r in reachable.values())
                miner_running = actually_mining
            else:
                actually_mining = None
                error_str = (error_str or "") + " | No miners reachable"

            if reachable:
                self._confirm_pending_commands(settings, reachable, actually_mining)

                # Hysteresis: smart start uses single threshold; normal mode
                # uses the on/off buffer.
                if smart.active:
                    if actually_mining:
                        desired_mining = soc >= smart.effective_soc_on
                    else:
                        # Starting also requires live PV input so we don't
                        # start at midnight just because a sunny day is forecast
                        pv_gate_ok = readings["input_power_w"] >= smart_min_pv_w
                        desired_mining = soc >= smart.effective_soc_on and pv_gate_ok
                elif actually_mining:
                    desired_mining = soc >= smart.effective_soc_off
                else:
                    desired_mining = soc >= smart.effective_soc_on

                # EOD target override: stop the miners early if running would
                # cause us to miss the end-of-day SOC target.
                desired_mining, eod_state = self._apply_eod_override(
                    settings, weather, soc, miner_power_w, pv_peak_kw,
                    local_month, desired_mining)

                # Phase 3 — drive the miners. Compute the ramp plan (pure) for
                # display; only DRIVE profiles when enabled AND armed (not
                # dry-run). Otherwise binary Smart Start / manual logic governs
                # and the plan is shown for observation.
                ramp_enabled = bool(settings.get("ramp_enabled", False))
                ramp_dry_run = bool(settings.get("ramp_dry_run", True))
                # The armed path needs battery capacity to protect the charge.
                ramp_cap_ok = float(settings.get("battery_capacity_kwh") or 0.0) > 0
                reach_order = [m for m in miners if per_miner[m.label]["reachable"]]
                would_arm = bool(ramp_enabled and not ramp_dry_run and ramp_cap_ok)
                ramp_plan = None
                if ramp_enabled and reach_order:
                    committed = self.ramp.last_commanded_w if would_arm else 0.0
                    ramp_plan = self.ramp.compute(settings, readings, weather,
                                                  reach_order, miner_hold, committed)
                    ramp_plan["dry_run"] = ramp_dry_run
                    ramp_plan["needs_battery_capacity"] = (ramp_enabled and not ramp_dry_run
                                                           and not ramp_cap_ok)
                ramp_armed = bool(would_arm and ramp_plan)
                state.set("ramp", ramp_plan)

                # Log ramp decisions whenever a miner's target changes (dry-run
                # included, so its reasoning is trackable before arming).
                if ramp_plan is not None:
                    self.ramp.log_changes(ramp_plan, ramp_armed, readings, reach_order)

                if ramp_armed:
                    for e in self.ramp.apply(reach_order, ramp_plan):
                        error_str = (error_str or "") + f" | ramp {e}"
                    self.ramp.last_commanded_w = ramp_plan["target_total_w"]
                    action = "ramp"
                else:
                    # Binary control (also the dry-run path): drive each
                    # reachable miner to the shared desired state, except a
                    # manually-held miner is forced off.
                    self.ramp.last_commanded_w = 0.0  # re-arm starts from measured draw
                    for m in miners:
                        r = per_miner[m.label]
                        if not r["reachable"]:
                            continue
                        md = desired_mining and not miner_hold.get(m.label)
                        if bool(r["running"]) != md:
                            ok, err = m.try_set_power(md)
                            if ok:
                                r["action"] = "started" if md else "stopped"
                            else:
                                r["action"] = "error_starting" if md else "error_stopping"
                                verb = "start" if md else "stop"
                                error_str = (error_str or "") + f" | {m.label} {verb} error: {err}"
                    acts = {r["action"] for r in reachable.values()}
                    action = next((c for c in ("started", "stopped",
                                               "error_starting", "error_stopping")
                                   if c in acts), "none")

                self.notifier.send_cycle_alerts(
                    settings=settings, soc=soc, actually_mining=actually_mining,
                    smart_active=smart.active, soc_on=soc_on,
                    effective_soc_on=smart.effective_soc_on,
                    effective_soc_off=smart.effective_soc_off,
                    hashrate_mhs=hashrate_mhs,
                    smart_hold_active=smart_hold_active,
                    ramp_armed=ramp_armed,
                )

        elif not miners and error_str is None:
            error_str = "Miner IP not configured"

        # ── Dehumidifier auto control ────────────────────────────
        dehum = Dehumidifier.from_settings(settings)
        if dehum is not None:
            run_auto_control(dehum, settings, state, readings)

        # ── Publish state + persist the reading ──────────────────
        now_str = utcnow_str()
        miners_snapshot = {
            label: {
                "running":      r["running"],
                "hashrate_mhs": r["hashrate_mhs"],
                "reachable":    r["reachable"],
                "error":        r["error"],
                "hold":         miner_hold.get(label, False),
            }
            for label, r in per_miner.items()
        } if per_miner else None

        updates = {
            "readings": readings,
            "miner_running": miner_running,
            "last_updated": now_str,
            "effective_soc_on": smart.effective_soc_on,
            "smart_start_active": smart.active,
            "smart_hold_active": smart_hold_active,
            "miner_hold_active": miner_hold_active,
            "smart_min_pv_w": smart_min_pv_w,
            "cycle": cycle_num,
            "error": error_str,
            "action": action,
            **eod_state,
        }
        if miners_snapshot is not None:
            updates["miners"] = miners_snapshot
        state.update(updates)

        if readings is not None:
            save_reading({
                "ts": now_str,
                "soc": readings["soc"],
                "battery_power_w": readings["battery_power_w"],
                "input_power_w": readings["input_power_w"],
                "grid_power_w": readings["grid_power_w"],
                "load_power_w": readings["load_power_w"],
                "backup_power_w": readings["backup_power_w"],
                "miner_running": miner_running,
                "action": action,
                "effective_soc_on": smart.effective_soc_on,
                "hashrate_mhs": hashrate_mhs,
                "radiation_wm2": cur_rad,
            })

    # ── Helpers ──────────────────────────────────────────────────

    def _confirm_pending_commands(self, settings: dict, reachable: dict,
                                  actually_mining: Optional[bool]) -> None:
        """Verify manual start/stop commands against what the miners actually
        report, then send the confirmation notification. Fires independently
        of the tg_miner_onoff toggle. Confirming also re-baselines prev_mining
        so the duplicate auto on/off edge alert is suppressed."""
        pending = dict(self.state.get("miner_cmd_pending") or {})
        if not pending:
            return
        bot = get_bot(settings)
        confirmed_any = False
        for label in list(pending.keys()):
            r = reachable.get(label)
            if r is None:
                continue  # miner unreachable this cycle — keep waiting
            p = pending[label]
            if bool(r["running"]) == p["want"]:
                if bot:
                    if p["want"]:
                        bot.send(f"✅ <b>{label} is ON</b> — confirmed running.")
                    else:
                        bot.send(f"🛑 <b>{label} is OFF</b> — disabled until midnight "
                                 f"(or until you start it again).")
                pending.pop(label, None)
                confirmed_any = True
            else:
                p["cycles"] += 1
                if p["cycles"] >= 6:
                    if bot:
                        bot.send(f"⚠️ Couldn't confirm {label} changed state — please check it.")
                    pending.pop(label, None)
        if confirmed_any:
            self.notifier.prev_mining = actually_mining
        self.state.set("miner_cmd_pending", pending or None)

    def _apply_eod_override(self, settings: dict, weather: Optional[dict], soc: float,
                            miner_power_w: float, pv_peak_kw: float, local_month: int,
                            desired_mining: bool):
        eod_enabled = bool(settings.get("eod_soc_target_enabled", False))
        eod_target = float(settings.get("eod_soc_target") or 80.0)
        battery_kwh = float(settings.get("battery_capacity_kwh") or 0.0)
        out = {"eod_target": eod_target if eod_enabled else None,
               "eod_projected_with": None, "eod_projected_without": None,
               "eod_protecting": False}
        if eod_enabled and eod_target > 0.0 and battery_kwh > 0.0 and pv_peak_kw > 0.0:
            hourly_wx = (weather or {}).get("hourly", [])
            load_profile = get_hourly_load_profile()
            eff_map = get_pv_efficiency(local_month)
            common = dict(soc=soc, battery_kwh=battery_kwh, hourly_wx=hourly_wx,
                          pv_peak_kw=pv_peak_kw, eff_map=eff_map,
                          load_profile=load_profile, miner_power_w=miner_power_w)
            out["eod_projected_with"] = eod.estimate_eod_soc(include_miner=True, **common)
            out["eod_projected_without"] = eod.estimate_eod_soc(include_miner=False, **common)
            if desired_mining and out["eod_projected_with"] is not None \
                    and out["eod_projected_with"] < eod_target:
                desired_mining = False
                out["eod_protecting"] = True
        return desired_mining, out
