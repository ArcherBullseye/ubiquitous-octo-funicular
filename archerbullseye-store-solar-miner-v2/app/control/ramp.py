"""Surplus-tracking power-profile ramp.

The miners are a dimmable load. Each cycle the controller reads the Solis
grid + battery flow and picks each reachable miner's LuxOS profile so total
miner draw soaks the available surplus — without exporting to the grid and
without stealing the battery charge still needed to reach full by end of the
solar day.

    headroom_w = grid_power_w + (battery_power_w − charge_reserve_w)
      grid  > 0  → exporting  → room to draw more
      grid  < 0  → importing  → must shed
      battery charging above the reserve → room to draw more (charge slower)

Asymmetric response: shed fast (jump straight down), ramp up slowly (bounded
step per cycle). Priority fill X1 → X2. Miners sit on the backup/EPS port, so
backup_power_w is the measured total miner draw; because that measurement lags
a freshly-set profile by a cycle or two, the last COMMANDED total is used as a
floor when armed — otherwise the lag causes over-ramping.
"""
from typing import Dict, List, Optional, Tuple

from ..db import log_ramp_event
from ..devices.miner import Miner, MinerFleet
from ..localtime import utcnow_str

DEADBAND_W = 200.0   # ignore |headroom| smaller than this (anti-chatter)
UP_STEP_W = 700.0    # max power increase per cycle (slow ramp up)


def charge_reserve_w(settings: dict, soc: float, weather: Optional[dict]) -> float:
    """Battery charging power to reserve so SOC still reaches the target by
    end of the solar day. 0 once at/above target or when not computable."""
    battery_kwh = float(settings.get("battery_capacity_kwh") or 0.0)
    if battery_kwh <= 0:
        return 0.0
    if bool(settings.get("eod_soc_target_enabled", False)):
        target_soc = float(settings.get("eod_soc_target") or 98.0)
    else:
        target_soc = 98.0
    if soc >= target_soc:
        return 0.0
    remaining_hours = float((weather or {}).get("remaining_sunny_hours", 0) or 0)
    remaining_hours = max(remaining_hours, 0.25)  # guard div-by-zero / last-light
    margin = float(settings.get("ramp_charge_margin") or 1.25)
    energy_needed_kwh = (target_soc - soc) / 100.0 * battery_kwh
    return max(0.0, energy_needed_kwh / remaining_hours * 1000.0 * margin)


class RampController:
    def __init__(self, fleet: MinerFleet):
        self.fleet = fleet
        # Total watts commanded last cycle — the "current draw" floor while
        # armed (measured backup power lags a fresh profileset).
        self.last_commanded_w = 0.0
        self._log_prev: Dict[str, str] = {}  # label -> last-logged target

    @staticmethod
    def _pick_profile(ladder: List[dict], target_watts: float, cap_watts: float) -> Optional[dict]:
        """Highest rung whose watts ≤ min(target, cap). None → below the floor (sleep)."""
        limit = min(target_watts, cap_watts)
        eligible = [p for p in ladder if p["watts"] <= limit]
        return eligible[-1] if eligible else None  # ladder is ascending

    def compute(self, settings: dict, readings: dict, weather: Optional[dict],
                order: List[Miner], held: Dict[str, bool], committed_w: float = 0.0) -> dict:
        """Compute the ramp plan for this cycle (pure — no miner writes).

        `order` is the reachable miners in priority order; `held` is
        {label: bool}. Returns the plan dict stored in state['ramp'] and
        (when armed) driven by apply()."""
        cap = float(settings.get("miner_max_watts") or 2960.0)
        grid = float(readings.get("grid_power_w") or 0.0)
        battery = float(readings.get("battery_power_w") or 0.0)
        backup = float(readings.get("backup_power_w") or 0.0)
        soc = float(readings.get("soc") or 0.0)

        reserve = charge_reserve_w(settings, soc, weather)
        current_total = max(0.0, backup, committed_w)
        headroom = grid + (battery - reserve)

        active = [m for m in order if not held.get(m.label)]
        n = len(active)
        # Lowest rung across active miners — the floor a miner must clear to run.
        floor = 0.0
        if active:
            l0 = self.fleet.ladder(active[0])
            floor = l0[0]["watts"] if l0 else 0.0

        raw_target = current_total + headroom
        # Asymmetric: shed fast (jump straight down), ramp up slowly.
        if raw_target > current_total:
            cap_up = current_total + UP_STEP_W
            if current_total < floor:
                # Starting from sleep: allow reaching the floor so a miner can
                # wake (the rate limit alone would strand it below the lowest
                # profile).
                cap_up = max(cap_up, floor)
            raw_target = min(raw_target, cap_up)
        target_total = max(0.0, min(raw_target, n * cap))

        # Priority fill: X1 up to cap, then X2 with the remainder.
        per: Dict[str, dict] = {}
        remaining = target_total
        for m in active:
            ladder = self.fleet.ladder(m)
            prof = self._pick_profile(ladder, remaining, cap) if ladder else None
            if prof is None:
                per[m.label] = {"target_watts": 0.0, "target_profile": None,
                                "target_hashrate": 0.0, "sleep": True}
            else:
                per[m.label] = {"target_watts": prof["watts"], "target_profile": prof["name"],
                                "target_hashrate": prof.get("hashrate_ths"), "sleep": False}
                remaining -= prof["watts"]
        for m in order:  # held miners: shown as excluded
            if m.label not in per:
                per[m.label] = {"target_watts": 0.0, "target_profile": None,
                                "target_hashrate": 0.0, "sleep": True, "held": True}

        return {
            "reserve_w": round(reserve),
            "headroom_w": round(headroom),
            "current_total_w": round(current_total),
            "target_total_w": round(target_total),
            "changed": abs(target_total - current_total) >= DEADBAND_W,
            "per_miner": per,
        }

    def apply(self, order: List[Miner], plan: dict) -> List[str]:
        """Drive each miner to its planned profile (sleep, or wake + set rung).
        Only called when the ramp is armed. Skips redundant writes. Returns
        error strings."""
        errors = []
        per = plan.get("per_miner", {})
        for m in order:
            p = per.get(m.label)
            if not p:
                continue
            try:
                # Below the floor OR manually held → keep it off.
                m.set_level(None if p.get("sleep") else p["target_profile"])
            except Exception as e:
                errors.append(f"{m.label}: {e}")
        return errors

    def log_changes(self, plan: dict, armed: bool, readings: dict, order: List[Miner]) -> None:
        """Append a ramp_log row when any miner's target profile changes vs.
        the previous cycle (captures the ramp progressing over time; works in
        dry-run too so its reasoning is trackable before arming)."""
        changes = []
        for m in order:
            pm = plan["per_miner"].get(m.label, {})
            cur = "sleep" if pm.get("sleep") else (pm.get("target_profile") or "sleep")
            if cur != self._log_prev.get(m.label):
                if pm.get("sleep"):
                    changes.append(f"{m.label}→off")
                else:
                    changes.append(f"{m.label}→{pm.get('target_profile')} "
                                   f"({(pm.get('target_watts') or 0) / 1000:.2f} kW)")
            self._log_prev[m.label] = cur
        if not changes:
            return
        try:
            log_ramp_event({
                "ts": utcnow_str(),
                "armed": armed,
                "soc": readings.get("soc"),
                "grid_w": readings.get("grid_power_w"),
                "battery_w": readings.get("battery_power_w"),
                "reserve_w": plan.get("reserve_w"),
                "headroom_w": plan.get("headroom_w"),
                "current_w": plan.get("current_total_w"),
                "target_w": plan.get("target_total_w"),
                "detail": ", ".join(changes),
            })
        except Exception as e:
            print(f"ramp log error: {e}")
