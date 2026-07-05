"""Dehumidifier device — a Tuya-local switchable load.

Runs opportunistically on excess solar export (see control/loop.py): on when
grid export exceeds the threshold, off when it drops 200 W below, with
min-run/min-off windows to prevent short-cycling and a manual-override timer.
"""
import time
from typing import Dict, Optional

from ..clients.tuya import DehumidifierClient
from .base import SwitchableLoad


class Dehumidifier(SwitchableLoad):
    kind = "dehumidifier"
    label = "dehum"

    def __init__(self, device_id: str, ip: str, local_key: str, version: float):
        self._client = DehumidifierClient(device_id, ip, local_key, version)

    @classmethod
    def from_settings(cls, settings: dict) -> Optional["Dehumidifier"]:
        device_id = (settings.get("dehum_device_id") or "").strip()
        ip = (settings.get("dehum_ip") or "").strip()
        key = (settings.get("dehum_local_key") or "").strip()
        if not (device_id and ip and key):
            return None
        return cls(device_id, ip, key, float(settings.get("dehum_version") or 3.3))

    def read(self) -> Dict:
        try:
            status = self._client.get_status()
            if not status:
                raise RuntimeError("empty status")
            return {
                "reachable": True, "error": None,
                "power": status["power"],
                "humidity": status["humidity"],
                "tank_full": status["tank_full"],
            }
        except Exception as e:
            return {"reachable": False, "error": str(e),
                    "power": None, "humidity": None, "tank_full": False}

    def raw_status(self) -> dict:
        return self._client.raw_status()

    def set_power(self, on: bool) -> None:
        if not self._client.set_power(on):
            raise RuntimeError(f"set_power({on}) failed")


def run_auto_control(dehum: Dehumidifier, settings: dict, state, readings: Optional[dict]) -> None:
    """One auto-control step: poll the dehumidifier and, when auto mode is on,
    switch it based on grid export. Reads/writes the dehum_* keys in state.
    Never raises — errors land in state['dehum_error'].
    """
    auto_enabled = bool(settings.get("dehum_auto_enabled", False))
    threshold = float(settings.get("dehum_excess_threshold_w") or 500.0)
    min_run_s = float(settings.get("dehum_min_run_minutes") or 30) * 60
    min_off_s = float(settings.get("dehum_min_off_minutes") or 15) * 60

    try:
        status = dehum.read()
        if not status["reachable"]:
            raise RuntimeError(status["error"] or "unreachable")

        power = status["power"]
        tank_full = status["tank_full"]
        auto_on = False

        now_ts = time.time()
        manual_until = state.get("dehum_manual_override_until")
        auto_on_since = state.get("dehum_auto_on_since")
        auto_off_since = state.get("dehum_auto_off_since")

        # Expire the manual override
        if manual_until and now_ts >= manual_until:
            manual_until = None
            state.set("dehum_manual_override_until", None)

        if auto_enabled and readings is not None and not tank_full and not manual_until:
            grid_w = readings.get("grid_power_w", 0) or 0

            if not power and grid_w > threshold:
                # Respect min-off time before turning back on
                if auto_off_since is None or (now_ts - auto_off_since) >= min_off_s:
                    dehum.set_power(True)
                    power = True
                    auto_on_since = now_ts
                    auto_off_since = None
                auto_on = power
            elif power and grid_w < (threshold - 200):
                # Respect min-run time before turning off
                if auto_on_since is None or (now_ts - auto_on_since) >= min_run_s:
                    dehum.set_power(False)
                    power = False
                    auto_off_since = now_ts
                    auto_on_since = None
                else:
                    auto_on = True  # still in min-run window
            elif power:
                auto_on = True

        state.update({
            "dehum_power": power,
            "dehum_humidity": status["humidity"],
            "dehum_tank_full": tank_full,
            "dehum_auto_on": auto_on,
            "dehum_error": None,
            "dehum_auto_on_since": auto_on_since,
            "dehum_auto_off_since": auto_off_since,
        })
    except Exception as e:
        state.set("dehum_error", str(e))
