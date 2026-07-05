"""Device capability interfaces.

Contract notes for implementers:

  * read()/status methods must NEVER raise — an unreachable device reports
    reachable=False so the control loop keeps driving the others.
  * Writes may raise; the caller decides how to surface the error.
  * Devices are cheap wrappers — they hold identity + a client, not state.
    Runtime state lives in AppState; persistent knobs live in settings.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class Device(ABC):
    """Anything the app can observe and (usually) control."""

    kind: str    # "miner", "dehumidifier", "thermostat", ...
    label: str   # display/reference name, e.g. "X1"

    @abstractmethod
    def read(self) -> Dict:
        """Current state snapshot. Never raises: on failure returns
        {..., "reachable": False, "error": str}."""


class SwitchableLoad(Device):
    """A load that can be switched on/off."""

    @abstractmethod
    def set_power(self, on: bool) -> None:
        """Turn the load on or off. May raise on failure."""


class DimmableLoad(SwitchableLoad):
    """A load with a discrete ladder of power levels (low → high).

    The surplus-tracking ramp controller drives any DimmableLoad: it picks
    the highest rung that fits the available solar surplus. For miners a rung
    is a LuxOS profile; a future thermostat could expose stage-1/stage-2
    compressor levels the same way.
    """

    @abstractmethod
    def ladder(self) -> List[Dict]:
        """Ascending power rungs: [{"name", "watts", ...}]. Empty if unknown.
        Should be cached — ladders are static per device model."""

    @abstractmethod
    def set_level(self, rung_name: Optional[str]) -> None:
        """Drive to the named rung; None means switch the load off/sleep."""
