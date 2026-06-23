import logging
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)


class DehumidifierClient:
    """
    Controls a Tuya-based WiFi dehumidifier via the local LAN protocol.
    Requires tinytuya: pip install tinytuya

    To obtain device_id and local_key:
    1. Install Smart Life or Tuya Smart app and pair the device.
    2. Create a Tuya developer account at iot.tuya.com.
    3. Link your Smart Life account under "Cloud > Development".
    4. Find your device in "Devices" to get the device_id.
    5. Use the tinytuya wizard (python -m tinytuya wizard) to get local_key.

    Common DPs for dehumidifiers:
      1  - power (bool)
      2  - target humidity (int, %)
      3  - mode
      4  - current humidity (int, %) — varies by model
      101 - water tank full (bool)
    """

    def __init__(self, device_id: str, ip: str, local_key: str, version: float = 3.3):
        self.device_id  = device_id.strip()
        self.ip         = ip.strip()
        self.local_key  = local_key.strip()
        self.version    = version

    def _device(self):
        try:
            import tinytuya
        except ImportError:
            raise RuntimeError("tinytuya not installed — run: pip install tinytuya")
        d = tinytuya.OutletDevice(self.device_id, self.ip, self.local_key)
        d.set_version(self.version)
        d.set_socketTimeout(6)
        d.set_socketRetryLimit(2)
        return d

    def raw_status(self) -> dict:
        """Returns raw tinytuya response — useful for debugging."""
        d = self._device()
        return d.status()

    def get_status(self) -> Optional[Dict[str, Any]]:
        try:
            d = self._device()
            data = d.status()
            if not data or "dps" not in data:
                err = data.get("Error") or data.get("Err") or str(data) if data else "empty response"
                raise RuntimeError(f"Bad response from device: {err}")

            dps = data["dps"]
            # DPs for Ivation IVADUWIFI50WP (protocol 3.4), keys are strings:
            #   "1"   = power switch (bool)
            #   "3"   = target humidity setpoint (int, %)
            #   "4"   = setpoint lower bound / min (int, %)
            #   "109" = current room humidity (int, %)
            #   "111" = tank full (bool)
            return {
                "power":      bool(dps.get("1", False)),
                "humidity":   dps.get("109"),
                "tank_full":  bool(dps.get("111", False)),
                "target_hum": dps.get("3"),
                "dps":        dps,
            }
        except Exception as e:
            log.error("Dehumidifier get_status error: %s", e)
            raise

    def set_power(self, on: bool) -> bool:
        try:
            d = self._device()
            if on:
                d.turn_on()
            else:
                d.turn_off()
            return True
        except Exception as e:
            log.error("Dehumidifier set_power(%s) error: %s", on, e)
            return False
