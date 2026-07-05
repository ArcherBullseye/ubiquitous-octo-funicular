"""Device layer — controllable hardware behind capability interfaces.

Every piece of controllable hardware is a Device (devices/base.py). Devices
declare capabilities the control layer understands:

  SwitchableLoad   can be turned on/off        (miners, dehumidifier, thermostat)
  DimmableLoad     has a discrete power ladder (miners via LuxOS profiles)

The control loop allocates solar surplus to devices through these interfaces,
so adding a new device type (e.g. thermostats — see devices/thermostat.py)
means implementing the interface, not touching the control logic.
"""
from .base import Device, SwitchableLoad, DimmableLoad  # noqa: F401
