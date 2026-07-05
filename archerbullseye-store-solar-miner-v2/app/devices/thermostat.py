"""Thermostat device — future integration, interface reserved.

The plan: thermostats join the surplus-allocation stack as SwitchableLoads
(or DimmableLoads for multi-stage systems), so pre-cooling/pre-heating the
house becomes another way to soak excess solar — alongside the miners and
the dehumidifier — prioritized by comfort first, then miners.

To land this:

  1. Pick the client (e.g. a local Ecobee/Nest/Z-Wave bridge or Tuya
     thermostat) and add it under clients/.
  2. Implement read() -> {reachable, current_temp_f, setpoint_f, mode,
     running, power_w_estimate} and set_power()/set_level() below.
  3. Add its settings rows to config.SCHEMA (thermostat_ip, comfort band,
     pre-cool offset, estimated compressor watts, ...).
  4. Register it in the control loop's device list; the ramp/surplus logic
     already speaks the capability interfaces.
  5. Add a dashboard card (web/templates + static/js/dashboard.js).

Until then this stub documents the contract and keeps the wiring points
obvious; it is not instantiated anywhere.
"""
from typing import Dict, Optional

from .base import SwitchableLoad


class Thermostat(SwitchableLoad):
    kind = "thermostat"

    def __init__(self, label: str):
        self.label = label

    @classmethod
    def from_settings(cls, settings: dict) -> Optional["Thermostat"]:
        # Gate on the reserved settings key; returns None until implemented.
        if not settings.get("thermostat_enabled"):
            return None
        raise NotImplementedError("Thermostat integration not implemented yet")

    def read(self) -> Dict:
        raise NotImplementedError

    def set_power(self, on: bool) -> None:
        raise NotImplementedError
