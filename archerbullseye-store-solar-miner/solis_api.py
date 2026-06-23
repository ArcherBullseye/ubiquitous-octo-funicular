import hashlib
import hmac
import base64
import json
from datetime import datetime, timezone
import requests


class SolisApiError(Exception):
    pass


class SolisClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")

    def _sign(self, method: str, content_md5: str, content_type: str, date: str, path: str) -> str:
        string_to_sign = f"{method}\n{content_md5}\n{content_type}\n{date}\n{path}"
        signature = hmac.new(self.api_secret, string_to_sign.encode("utf-8"), hashlib.sha1)
        return base64.b64encode(signature.digest()).decode("utf-8")

    def _post(self, path: str, body: dict) -> dict:
        content_type = "application/json"
        body_bytes = json.dumps(body).encode("utf-8")
        content_md5 = base64.b64encode(hashlib.md5(body_bytes).digest()).decode("utf-8")
        date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        signature = self._sign("POST", content_md5, content_type, date, path)

        headers = {
            "Content-Type": content_type,
            "Content-MD5": content_md5,
            "Date": date,
            "Authorization": f"API {self.api_key}:{signature}",
        }

        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(url, headers=headers, data=body_bytes, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SolisApiError(f"HTTP request failed: {e}") from e

        data = resp.json()
        if not data.get("success"):
            raise SolisApiError(f"Solis API error: {data.get('msg', 'unknown error')} (code={data.get('code')})")
        return data

    def get_inverter_detail(self, inverter_sn: str) -> dict:
        """Returns raw inverter detail record from Solis Cloud."""
        data = self._post("/v1/api/inverterDetail", {"sn": inverter_sn})
        return data.get("data", {})


def parse_power_and_soc(inverter_data: dict) -> dict:
    """
    Extract the values we care about from a Solis inverterDetail response.

    All "*Origin*" / "*OrginV2*" fields are in watts (W).
    Fields without that suffix are in kW — multiply by 1000.
    Sign conventions used here:
      battery  : positive = charging, negative = discharging
      grid     : positive = exporting to grid, negative = importing from grid
    """
    # Battery SOC (%)
    soc = float(inverter_data.get("batteryCapacitySoc") or 0.0)

    # Battery power — batteryPowerOriginV2 is signed watts (negative = discharging)
    battery_power_w = float(inverter_data.get("batteryPowerOriginV2") or 0.0)

    # PV input — pvAndAcCoupledPowerOrigin is in watts
    input_power_w = float(inverter_data.get("pvAndAcCoupledPowerOrigin") or 0.0)

    # Grid — psumOrgin is watts, positive = exporting to grid, negative = importing
    grid_power_w = float(inverter_data.get("psumOrgin") or 0.0)

    # Backup / bypass circuit (miner)
    backup_power_w = float(inverter_data.get("bypassLoadPowerOriginal") or 0.0)

    # familyLoadPowerOrigin is total load including backup/EPS port.
    # Subtract backup to get main panel (house) only.
    load_power_w = max(0.0, float(inverter_data.get("familyLoadPowerOrigin") or 0.0) - backup_power_w)

    return {
        "soc": soc,
        "battery_power_w": battery_power_w,
        "input_power_w": input_power_w,
        "grid_power_w": grid_power_w,
        "load_power_w": load_power_w,
        "backup_power_w": backup_power_w,
    }
