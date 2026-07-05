"""End-of-day SOC projection.

Projects battery SOC at the end of today's solar window by walking the
remaining hourly forecast, accumulating PV production (through the learned
efficiency map) minus house load (learned hourly profile) minus miner draw.
Used to stop the miners early when running them would miss the EOD target.
"""
from datetime import datetime
from typing import Dict, Optional


def estimate_eod_soc(
    soc: float,
    battery_kwh: float,
    hourly_wx: list,
    pv_peak_kw: float,
    eff_map: Dict[int, float],
    load_profile: Dict[int, float],
    miner_power_w: float,
    include_miner: bool,
) -> Optional[float]:
    """Iterates remaining forecast slots until radiation drops below 50 W/m².
    Returns None if there isn't enough data to estimate."""
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

        miner = (miner_power_w / 1000.0) if include_miner else 0.0

        energy_kwh += pv - (load_w / 1000.0) - miner
        energy_kwh = max(0.0, min(battery_kwh, energy_kwh))

    if not found_solar:
        return None

    return round((energy_kwh / battery_kwh) * 100.0, 1)
