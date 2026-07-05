"""HTTP API — same endpoints and payload shapes as Solar Miner v1, so any
external consumer (and the dashboard) keeps working across the upgrade.
"""
import time

from flask import Blueprint, jsonify, render_template, request

from .. import config
from ..clients.openmeteo import geocode as do_geocode
from ..control.commands import start_miners, stop_miners
from ..control.notifications import get_bot
from ..db import (get_pv_efficiency, get_pv_efficiency_detail, get_ramp_log,
                  get_recent_readings, get_settings, reset_pv_efficiency,
                  update_settings)
from ..devices.dehumidifier import Dehumidifier
from ..devices.miner import MinerFleet
from ..localtime import local_now, local_today_str
from ..poolstats import fetch_daily_sats_rows
from ..state import AppState
from ..version import APP_VERSION


def create_blueprint(state: AppState, fleet: MinerFleet) -> Blueprint:
    bp = Blueprint("web", __name__)

    @bp.route("/")
    def index():
        return render_template("index.html")

    @bp.route("/api/refresh", methods=["POST"])
    def api_refresh():
        """Wake all background loops for an immediate re-poll."""
        state.refresh_all()
        return jsonify({"ok": True})

    @bp.route("/api/status")
    def api_status():
        snapshot = state.snapshot()
        settings = get_settings()
        tz = state.tz_offset_seconds()
        snapshot["pv_efficiency"] = get_pv_efficiency(local_now(tz).month)
        snapshot["pv_peak_kw"] = float(settings.get("pv_peak_kw") or 0.0)
        snapshot["eod_soc_target"] = float(settings.get("eod_soc_target") or 80.0)
        snapshot["eod_soc_target_enabled"] = bool(settings.get("eod_soc_target_enabled", False))
        snapshot["today_sats"] = (snapshot.get("pool") or {}).get("sats_today", 0) or 0
        snapshot["sunny_hours_threshold"] = float(settings.get("sunny_hours_threshold") or 3.0)
        # Derive hold state from settings (not just the control-loop value) so
        # the dashboard reflects a Hold/Resume click immediately.
        local_today = local_today_str(tz)
        snapshot["smart_hold_active"] = bool(settings.get("smart_hold_date")) and \
            settings.get("smart_hold_date") == local_today
        hold = fleet.hold_map(settings, local_today)
        miners_snap = dict(snapshot.get("miners") or {})
        for label, row in miners_snap.items():
            row = dict(row)
            row["hold"] = hold.get(label, False)
            miners_snap[label] = row
        snapshot["miners"] = miners_snap
        snapshot["miner_hold_active"] = bool(hold) and all(hold.values())
        snapshot["app_version"] = APP_VERSION
        return jsonify(snapshot)

    @bp.route("/api/history")
    def api_history():
        hours = int(request.args.get("hours", 2))
        return jsonify(get_recent_readings(hours))

    @bp.route("/api/daily_sats")
    def api_daily_sats():
        days = int(request.args.get("days", 7))
        include_today = request.args.get("include_today", "1") != "0"
        return jsonify(fetch_daily_sats_rows(state, days, include_today))

    # ── Settings ─────────────────────────────────────────────────

    @bp.route("/api/settings", methods=["GET"])
    def api_get_settings():
        return jsonify(config.mask_secrets(get_settings()))

    @bp.route("/api/settings", methods=["POST"])
    def api_post_settings():
        data = request.get_json(force=True) or {}
        # Schema-driven: drop round-tripped secret masks, coerce types.
        data = config.coerce_all(config.strip_masked(data))

        # If a location name/ZIP is set but coordinates are missing, resolve
        # them server-side so weather keeps working even if Search was skipped.
        name = str(data.get("location_name") or "").strip()
        if name and (not float(data.get("location_lat") or 0)
                     or not float(data.get("location_lon") or 0)):
            try:
                geo = do_geocode(name)
                if geo and geo.get("lat") and geo.get("lon"):
                    data["location_lat"] = float(geo["lat"])
                    data["location_lon"] = float(geo["lon"])
            except Exception:
                pass

        update_settings(data)

        if float(data.get("location_lat") or 0) or float(data.get("location_lon") or 0):
            state.weather_refresh.set()
        return jsonify({"ok": True})

    @bp.route("/api/geocode")
    def api_geocode():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify({"error": "missing q parameter"}), 400
        result = do_geocode(q)
        if result is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(result)

    # ── Miner control ────────────────────────────────────────────

    def _miner_target():
        """Which miner a start/stop request targets. Body/query
        {"miner": "X1"} — absent or "all" means every configured miner."""
        data = request.get_json(silent=True) or {}
        tgt = (data.get("miner") or request.args.get("miner") or "").strip().upper()
        return tgt if tgt in ("X1", "X2") else None

    @bp.route("/api/miner/start", methods=["POST"])
    def api_miner_start():
        ok, err = start_miners(state, fleet, _miner_target())
        if ok:
            return jsonify({"ok": True})
        return jsonify({"error": err}), 400 if (err or "").endswith("not configured") else 500

    @bp.route("/api/miner/stop", methods=["POST"])
    def api_miner_stop():
        ok, err = stop_miners(state, fleet, _miner_target())
        if ok:
            return jsonify({"ok": True})
        return jsonify({"error": err}), 400 if (err or "").endswith("not configured") else 500

    @bp.route("/api/miner/quick")
    def api_miner_quick():
        """Direct poll of every miner — bypasses the control loop for fast
        status updates after a manual start/stop."""
        settings = get_settings()
        miners = fleet.configured(settings)
        if not miners:
            return jsonify({"error": "Miner IP not configured"}), 400

        per = {m.label: m.read() for m in miners}  # never raises
        hold = fleet.hold_map(settings, local_today_str(state.tz_offset_seconds()))
        reachable = [r for r in per.values() if r["reachable"]]
        mining = any(bool(r["running"]) for r in reachable) if reachable else None
        hashrate_mhs = sum(r["hashrate_mhs"] or 0.0 for r in reachable)

        miners_out = {
            label: {
                "running": r["running"], "hashrate_mhs": r["hashrate_mhs"],
                "reachable": r["reachable"], "error": r["error"],
                "hold": hold.get(label, False),
            }
            for label, r in per.items()
        }
        state.update({"miner_running": mining, "miners": miners_out})
        return jsonify({"mining": mining, "hashrate_mhs": hashrate_mhs, "miners": miners_out})

    @bp.route("/api/miner/profiles")
    def api_miner_profiles():
        """Read-only: each configured miner's dimmable profile ladder +
        current step. Reports whichever miners answer; an offline one comes
        back reachable=False."""
        settings = get_settings()
        miners = fleet.configured(settings)
        if not miners:
            return jsonify({"error": "Miner IP not configured"}), 400
        return jsonify({"miners": {m.label: m.read_profiles() for m in miners}})

    @bp.route("/api/smart/hold", methods=["POST"])
    def api_smart_hold():
        """Pause/resume Smart Start until local midnight."""
        data = request.get_json(force=True) or {}
        hold = bool(data.get("hold"))
        local_today = local_today_str(state.tz_offset_seconds())
        update_settings({"smart_hold_date": local_today if hold else ""})
        state.set("smart_hold_active", hold)
        return jsonify({"ok": True, "hold": hold})

    @bp.route("/api/ramp_log")
    def api_ramp_log():
        """Recent ramp decisions (most-recent first) for the activity panel."""
        try:
            limit = min(int(request.args.get("limit", 60)), 300)
        except (ValueError, TypeError):
            limit = 60
        return jsonify({"events": get_ramp_log(limit)})

    # ── PV efficiency ────────────────────────────────────────────

    @bp.route("/api/pv_efficiency")
    def api_pv_efficiency():
        settings = get_settings()
        return jsonify({
            "rows": get_pv_efficiency_detail(),
            "pv_peak_kw": float(settings.get("pv_peak_kw") or 0.0),
            "current_month": local_now(state.tz_offset_seconds()).month,
        })

    @bp.route("/api/reset_efficiency", methods=["POST"])
    def api_reset_efficiency():
        reset_pv_efficiency()
        return jsonify({"ok": True})

    # ── Dehumidifier ─────────────────────────────────────────────

    @bp.route("/api/dehum/test")
    def api_dehum_test():
        dehum = Dehumidifier.from_settings(get_settings())
        if dehum is None:
            return jsonify({"error": "Not configured"}), 400
        try:
            return jsonify({"raw": dehum.raw_status()})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/api/dehum/power", methods=["POST"])
    def api_dehum_power():
        on = (request.get_json(silent=True) or {}).get("on")
        if on is None:
            return jsonify({"error": "missing 'on' field"}), 400
        settings = get_settings()
        dehum = Dehumidifier.from_settings(settings)
        if dehum is None:
            return jsonify({"error": "Dehumidifier not configured"}), 400
        try:
            override_hours = float(settings.get("dehum_manual_override_hours") or 2)
            dehum.set_power(bool(on))
            state.update({
                "dehum_power": bool(on),
                "dehum_auto_on": False,
                "dehum_manual_override_until": time.time() + override_hours * 3600,
                "dehum_auto_on_since": None,
                "dehum_auto_off_since": None,
            })
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Pool / Telegram ──────────────────────────────────────────

    @bp.route("/api/pool/status")
    def api_pool_status():
        return jsonify({"pool": state.get("pool"),
                        "last_updated": state.get("pool_last_updated")})

    @bp.route("/api/telegram/test", methods=["POST"])
    def api_telegram_test():
        bot = get_bot(get_settings())
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

    return bp
