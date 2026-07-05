"""Miner devices + fleet.

Miner wraps one LuxOS miner behind the DimmableLoad interface (its LuxOS
profile ladder is the dimmer). MinerFleet builds the configured miners from
settings (X1 primary, X2 secondary), reads them as a group, and owns the
per-miner manual-hold bookkeeping.
"""
import os
from typing import Dict, List, Optional, Tuple

from ..clients.luxos import LuxOsError, LuxOsPool
from .base import DimmableLoad


class Miner(DimmableLoad):
    kind = "miner"

    def __init__(self, label: str, ip: str, pool: LuxOsPool):
        self.label = label
        self.ip = ip
        self._pool = pool

    # ── Reads (never raise) ──────────────────────────────────────

    def read(self) -> Dict:
        """On/off state + hashrate. An unreachable miner comes back as
        reachable=False so the caller can keep driving the other one."""
        try:
            client = self._pool.get(self.ip)
            try:
                running = client.is_mining()
                hr = client.last_hashrate_mhs
            except LuxOsError:
                # One reconnect retry with a fresh session.
                self._pool.drop(self.ip)
                client = self._pool.get(self.ip)
                running = client.is_mining()
                hr = client.last_hashrate_mhs
            return {"running": running, "hashrate_mhs": hr, "reachable": True, "error": None}
        except Exception as e:
            return {"running": None, "hashrate_mhs": 0.0, "reachable": False, "error": str(e)}

    def read_profiles(self) -> Dict:
        """Profile ladder + current profile (read-only, never raises)."""
        try:
            client = self._pool.get(self.ip)
            cfg = client.get_config()
            ladder = client.get_profiles()
            cur_name = cfg.get("Profile")
            current = next((p for p in ladder if p["name"] == cur_name), None)
            watts = [p["watts"] for p in ladder
                     if isinstance(p.get("watts"), (int, float)) and p["watts"] > 0]
            return {
                "reachable":    True,
                "current_name": cur_name,
                "current_step": cfg.get("ProfileStep"),
                "current":      current,
                "min_watts":    min(watts) if watts else None,
                "max_watts":    max(watts) if watts else None,
                "power_target_supported": bool(cfg.get("IsPowerTargetSupported")),
                "atm_enabled":  bool(cfg.get("IsAtmEnabled")),
                "ladder":       ladder,
                "error":        None,
            }
        except Exception as e:
            return {"reachable": False, "current": None, "ladder": [], "error": str(e)}

    def ladder(self) -> List[Dict]:
        """Ascending, positive-watt profile rungs. Cached by the fleet —
        the ladder is static per miner model."""
        try:
            client = self._pool.get(self.ip)
            rungs = [p for p in client.get_profiles()
                     if isinstance(p.get("watts"), (int, float)) and p["watts"] > 0]
            rungs.sort(key=lambda p: p["watts"])
            return rungs
        except Exception:
            return []

    # ── Writes ───────────────────────────────────────────────────

    def set_power(self, on: bool) -> None:
        """Wake or sleep the miner, with one reconnect retry."""
        client = self._pool.get(self.ip)
        try:
            client.start_mining() if on else client.stop_mining()
        except LuxOsError:
            self._pool.drop(self.ip)
            client = self._pool.get(self.ip)
            client.start_mining() if on else client.stop_mining()

    def try_set_power(self, on: bool) -> Tuple[bool, Optional[str]]:
        """set_power that reports (ok, error) instead of raising."""
        try:
            self.set_power(on)
            return True, None
        except Exception as e:
            return False, str(e)

    def set_level(self, rung_name: Optional[str]) -> None:
        """Drive to a profile rung (waking first if asleep); None = sleep."""
        if rung_name is None:
            self.set_power(False)
            return
        client = self._pool.get(self.ip)
        try:
            cfg = client.get_config()
            cur_profile = cfg.get("Profile")
            awake = bool(cfg.get("IsPowerSupplyOn"))
        except LuxOsError:
            cur_profile, awake = None, False
        if not awake:
            client.start_mining()   # wake, then set the target profile same cycle
        if cur_profile != rung_name:
            client.set_profile(rung_name)


class MinerFleet:
    """The configured miners (X1 primary, X2 secondary) as a group."""

    def __init__(self, pool: Optional[LuxOsPool] = None):
        self.pool = pool or LuxOsPool()
        self._miners: Dict[str, Miner] = {}       # (label,ip) cache
        self._ladders: Dict[str, List[Dict]] = {}  # ip -> cached ladder

    def configured(self, settings: dict) -> List[Miner]:
        """[Miner] for each configured miner, priority order (X1 first)."""
        out = []
        ip1 = settings.get("miner_ip") or os.getenv("LUXOS_MINER_IP", "")
        ip2 = settings.get("miner2_ip", "")
        for label, ip in (("X1", ip1), ("X2", ip2)):
            if not ip:
                continue
            m = self._miners.get(label)
            if m is None or m.ip != ip:
                m = Miner(label, ip, self.pool)
                self._miners[label] = m
            out.append(m)
        return out

    def get(self, settings: dict, label: str) -> Optional[Miner]:
        return next((m for m in self.configured(settings) if m.label == label), None)

    def ladder(self, miner: Miner) -> List[Dict]:
        """Cached profile ladder for a miner (fetched once — static)."""
        cached = self._ladders.get(miner.ip)
        if cached:
            return cached
        rungs = miner.ladder()
        if rungs:
            self._ladders[miner.ip] = rungs
        return rungs

    # ── Manual holds ─────────────────────────────────────────────
    # Stopping a miner manually holds it off until local midnight (or until
    # it's manually started). The hold is stored as today's local date; it
    # rolls off automatically when the date advances.

    @staticmethod
    def hold_key(label: str) -> str:
        return "miner_hold_date" if label == "X1" else "miner2_hold_date"

    def hold_map(self, settings: dict, local_today: str) -> Dict[str, bool]:
        """{label: True while manually held off today}."""
        return {
            m.label: bool(settings.get(self.hold_key(m.label)))
                     and settings.get(self.hold_key(m.label)) == local_today
            for m in self.configured(settings)
        }

    @staticmethod
    def total_nameplate_w(settings: dict) -> float:
        """Combined configured power draw (W) of all miners — used by the
        Smart Start profitability gate and the EOD SOC projection."""
        total = 0.0
        if settings.get("miner_ip") or os.getenv("LUXOS_MINER_IP", ""):
            total += float(settings.get("miner_power_w") or 0.0)
        if settings.get("miner2_ip", ""):
            total += float(settings.get("miner2_power_w") or 0.0)
        return total
