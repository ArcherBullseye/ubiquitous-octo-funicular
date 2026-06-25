from typing import Optional, Dict, Any, List
import requests
from datetime import datetime

WMO_CODES: Dict[int, tuple] = {
    0:  ("Clear sky",           "☀️"),
    1:  ("Mainly clear",        "🌤️"),
    2:  ("Partly cloudy",       "⛅"),
    3:  ("Overcast",            "☁️"),
    45: ("Foggy",               "🌫️"),
    48: ("Icy fog",             "🌫️"),
    51: ("Light drizzle",       "🌦️"),
    53: ("Moderate drizzle",    "🌦️"),
    55: ("Heavy drizzle",       "🌦️"),
    61: ("Slight rain",         "🌧️"),
    63: ("Moderate rain",       "🌧️"),
    65: ("Heavy rain",          "🌧️"),
    71: ("Slight snow",         "❄️"),
    73: ("Moderate snow",       "❄️"),
    75: ("Heavy snow",          "❄️"),
    80: ("Rain showers",        "🌦️"),
    81: ("Moderate showers",    "🌦️"),
    82: ("Violent showers",     "⛈️"),
    95: ("Thunderstorm",        "⛈️"),
    99: ("Thunderstorm w/hail", "⛈️"),
}


NIGHT_OVERRIDES: Dict[int, str] = {
    0: "🌙",
    1: "🌙",
    2: "☁️",
}


def _icon(code: int, radiation_w: float) -> str:
    """Return weather emoji, swapping to night variants when radiation is 0."""
    _, day_icon = WMO_CODES.get(code, ("Unknown", "❓"))
    if radiation_w == 0 and code in NIGHT_OVERRIDES:
        return NIGHT_OVERRIDES[code]
    return day_icon


def get_weather(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": ["temperature_2m", "weather_code", "shortwave_radiation"],
                "hourly": ["shortwave_radiation", "weather_code", "temperature_2m"],
                "temperature_unit": "fahrenheit",
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def parse_weather(data: Dict[str, Any], radiation_threshold: float = 300.0) -> Dict[str, Any]:
    current = data.get("current", {})
    current_temp_f = float(current.get("temperature_2m", 0.0))
    current_code = int(current.get("weather_code", 0))
    current_radiation = int(current.get("shortwave_radiation", 0))
    current_time_str = current.get("time", "")

    desc, _ = WMO_CODES.get(current_code, ("Unknown", "❓"))
    icon = _icon(current_code, current_radiation)

    hourly = data.get("hourly", {})
    h_times: List[str] = hourly.get("time", [])
    h_radiation: List[float] = hourly.get("shortwave_radiation", [])
    h_codes: List[int] = hourly.get("weather_code", [])
    h_temps: List[float] = hourly.get("temperature_2m", [])

    # Determine current hour prefix for filtering (YYYY-MM-DDTHH)
    if current_time_str:
        current_hour_prefix = current_time_str[:13]  # "YYYY-MM-DDTHH"
        try:
            current_date = current_time_str[:10]  # "YYYY-MM-DD"
        except Exception:
            current_date = ""
    else:
        now = datetime.utcnow()
        current_hour_prefix = now.strftime("%Y-%m-%dT%H")
        current_date = now.strftime("%Y-%m-%d")

    # Count remaining sunny hours today (from current hour onward, today only)
    remaining_sunny_hours = 0
    for t, rad in zip(h_times, h_radiation):
        slot_date = t[:10]
        slot_hour_prefix = t[:13]
        if slot_date == current_date and slot_hour_prefix >= current_hour_prefix:
            if (rad or 0) > radiation_threshold:
                remaining_sunny_hours += 1

    # Build next 10 hourly slots from current hour onward
    hourly_forecast: List[Dict[str, Any]] = []
    for t, rad, code, temp in zip(h_times, h_radiation, h_codes, h_temps):
        slot_hour_prefix = t[:13]
        if slot_hour_prefix >= current_hour_prefix:
            slot_code = int(code or 0)
            slot_desc, _ = WMO_CODES.get(slot_code, ("Unknown", "❓"))
            slot_icon = _icon(slot_code, float(rad or 0))
            # Format time as HH:MM
            time_part = t[11:16] if len(t) >= 16 else t
            hourly_forecast.append({
                "time": time_part,
                "radiation_w": int(rad or 0),
                "icon": slot_icon,
                "desc": slot_desc,
                "temp_f": float(temp or 0.0),
            })
            if len(hourly_forecast) >= 10:
                break

    return {
        "current_temp_f": current_temp_f,
        "current_desc": desc,
        "current_icon": icon,
        "current_radiation_w": current_radiation,
        "remaining_sunny_hours": remaining_sunny_hours,
        "hourly": hourly_forecast,
        "utc_offset_seconds": int(data.get("utc_offset_seconds") or 0),
    }


def geocode(zipcode: str) -> Optional[Dict[str, Any]]:
    """Look up lat/lon for a US ZIP code via zippopotam.us (free, no key)."""
    try:
        resp = requests.get(
            f"https://api.zippopotam.us/us/{zipcode.strip()}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        place = data["places"][0]
        city = place["place name"]
        state = place["state abbreviation"]
        return {
            "lat": float(place["latitude"]),
            "lon": float(place["longitude"]),
            "name": f"{city}, {state} {zipcode.strip()}",
        }
    except Exception:
        return None
