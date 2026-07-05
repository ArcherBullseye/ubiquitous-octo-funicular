"""Shared runtime state.

One AppState instance is created at startup and passed to the loops and web
routes. All access goes through the lock-holding helpers so no caller touches
the dict without synchronization.
"""
import threading
from typing import Any, Dict, Optional


class AppState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "readings": None,          # latest Solis power/SOC snapshot
            "miner_running": None,     # aggregate: True if ANY reachable miner is on
            "miners": {},              # per-miner: {"X1": {running, hashrate_mhs, reachable, error, hold}}
            "ramp": None,              # surplus-tracking ramp plan (control/ramp.py)
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
            "miner_cmd_pending": None,  # per-miner manual command awaiting verification
            "smart_min_pv_w": 1000.0,
            "cycle": 0,
            "error": None,
            "action": "none",
            "eod_target": None,
            "eod_projected_with": None,
            "eod_projected_without": None,
            "eod_protecting": False,
            # Dehumidifier
            "dehum_power": None,
            "dehum_humidity": None,
            "dehum_tank_full": False,
            "dehum_auto_on": False,
            "dehum_error": None,
            "dehum_auto_on_since": None,
            "dehum_auto_off_since": None,
            "dehum_manual_override_until": None,
        }
        # Wake events: set one to make its loop re-poll immediately.
        self.control_refresh = threading.Event()
        self.weather_refresh = threading.Event()
        self.pool_refresh = threading.Event()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, updates: Dict[str, Any]) -> None:
        with self._lock:
            self._data.update(updates)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    # ── Convenience accessors ────────────────────────────────────

    def tz_offset_seconds(self) -> int:
        """UTC offset of the site, derived from the weather forecast."""
        with self._lock:
            return int((self._data.get("weather") or {}).get("utc_offset_seconds") or 0)

    def weather(self) -> Optional[dict]:
        with self._lock:
            return self._data.get("weather")

    def refresh_all(self) -> None:
        self.control_refresh.set()
        self.weather_refresh.set()
        self.pool_refresh.set()
