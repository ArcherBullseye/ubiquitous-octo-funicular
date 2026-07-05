"""Smart Start — lower the SOC start threshold on days with enough sun.

Pure decision logic: given settings, the weather forecast, and the learned
per-hour PV efficiency map, decide whether Smart Start is active and what the
effective on/off thresholds are this cycle.
"""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class SmartDecision:
    active: bool
    effective_soc_on: float
    effective_soc_off: float


def decide(settings: dict, weather: Optional[dict], efficiency_map: Dict[int, float],
           miner_power_w: float, hold_active: bool) -> SmartDecision:
    soc_on = float(settings.get("soc_on_threshold") or 85.0)
    soc_off = float(settings.get("soc_off_threshold") or 80.0)
    decision = SmartDecision(False, soc_on, soc_off)

    if not bool(settings.get("smart_start_enabled", True)) or hold_active or weather is None:
        return decision

    sunny_hours_threshold = float(settings.get("sunny_hours_threshold") or 3.0)
    pv_peak_kw = float(settings.get("pv_peak_kw") or 0.0)
    remaining_sunny = weather.get("remaining_sunny_hours", 0)

    if pv_peak_kw > 0 and miner_power_w > 0:
        # Learned efficiency model: count hours where predicted PV output
        # covers the miners' combined draw.
        profitable_hours = 0
        for slot in weather.get("hourly") or []:
            rad = float(slot.get("radiation_w") or 0)
            try:
                hour_of_day = int(str(slot.get("time", "")).split(":")[0])
            except (ValueError, IndexError):
                continue
            eff = efficiency_map.get(hour_of_day)
            if eff is not None:
                if rad * pv_peak_kw * 1000.0 * eff >= miner_power_w:
                    profitable_hours += 1
            else:
                # No learned data yet — count hours above the radiation threshold
                if rad > float(settings.get("radiation_threshold_wm2") or 300.0):
                    profitable_hours += 1
        # Efficiency model only covers the forecast slots — fall back to the
        # full-day sunny-hour count if the slot count comes up short.
        decision.active = (profitable_hours >= sunny_hours_threshold
                           or remaining_sunny >= sunny_hours_threshold)
    else:
        # PV peak or miner watts not configured — raw radiation count only.
        decision.active = remaining_sunny >= sunny_hours_threshold

    if decision.active:
        decision.effective_soc_on = float(settings.get("smart_soc_on_threshold") or 60.0)
        decision.effective_soc_off = float(settings.get("smart_soc_off_threshold") or 55.0)
    return decision
