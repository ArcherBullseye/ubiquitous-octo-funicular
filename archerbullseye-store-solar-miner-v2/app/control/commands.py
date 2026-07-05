"""Manual miner start/stop — the single source of truth for the dashboard
buttons and the Telegram /startMiner | /stopMiner commands.

Stopping a miner sets its per-miner hold (kept off until local midnight or
until manually started); starting clears the hold and hands control back to
automatic SOC/Smart Start management.
"""
from typing import Optional, Tuple

from ..db import get_settings, update_settings
from ..devices.miner import MinerFleet
from ..localtime import local_today_str
from ..state import AppState


def miner_command(state: AppState, fleet: MinerFleet, desired: bool,
                  target: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Start (desired=True) or stop (desired=False) miners.

    target=None acts on every configured miner; "X1"/"X2" acts on just that
    one. Succeeds if at least one targeted miner accepted the command.
    """
    settings = get_settings()
    miners = fleet.configured(settings)
    if target is not None:
        miners = [m for m in miners if m.label == target]
    if not miners:
        return False, "Miner IP not configured" if target is None else f"Miner {target} not configured"

    # Per-miner hold: stop → hold until midnight; start → clear hold.
    hold_date = local_today_str(state.tz_offset_seconds()) if not desired else ""
    update_settings({fleet.hold_key(m.label): hold_date for m in miners})

    errs = []
    for m in miners:
        ok, err = m.try_set_power(desired)
        if not ok:
            errs.append(f"{m.label}: {err}")

    # Optimistic state + per-miner pending confirmation (merged so a command
    # on X1 doesn't clear an in-flight one on X2). The control loop verifies
    # on its next read and sends the confirmation notification.
    pending = dict(state.get("miner_cmd_pending") or {})
    miners_state = dict(state.get("miners") or {})
    for m in miners:
        pending[m.label] = {"want": desired, "cycles": 0}
        row = dict(miners_state.get(m.label) or {})
        row["running"] = desired          # optimistic; verified next cycle
        row["hold"] = not desired
        miners_state[m.label] = row
    reachable = [m for m in miners_state.values() if m.get("reachable", True)]
    state.update({
        "miner_cmd_pending": pending,
        "miners": miners_state,
        "miner_running": any(bool(m.get("running")) for m in reachable) if reachable else None,
        "miner_hold_active": all(m.get("hold") for m in miners_state.values()) if miners_state else False,
    })

    if len(errs) == len(miners):
        return False, "; ".join(errs)
    return True, None


def start_miners(state: AppState, fleet: MinerFleet, target: Optional[str] = None):
    return miner_command(state, fleet, True, target)


def stop_miners(state: AppState, fleet: MinerFleet, target: Optional[str] = None):
    return miner_command(state, fleet, False, target)
