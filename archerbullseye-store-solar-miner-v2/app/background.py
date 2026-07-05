"""Background loops (weather + pool) and the thread launcher."""
import threading
from typing import Optional

from .clients.openmeteo import geocode, get_weather, parse_weather
from .control.loop import ControlLoop
from .control.notifications import Notifier
from .control.ramp import RampController
from .control.telegram_commands import telegram_loop
from .db import get_settings, update_settings
from .devices.miner import MinerFleet
from .localtime import utcnow_str
from .state import AppState


def weather_loop(state: AppState) -> None:
    while True:
        try:
            settings = get_settings()
            lat = float(settings.get("location_lat") or 0.0)
            lon = float(settings.get("location_lon") or 0.0)
            radiation_threshold = float(settings.get("radiation_threshold_wm2") or 300.0)

            # Self-heal: if a location (ZIP) is set but coordinates are missing
            # (Search skipped, or a save zeroed them), resolve them here so the
            # forecast comes back without any user action.
            name = str(settings.get("location_name") or "").strip()
            if lat == 0.0 and lon == 0.0 and name:
                try:
                    geo = geocode(name)
                    if geo and geo.get("lat") and geo.get("lon"):
                        lat, lon = float(geo["lat"]), float(geo["lon"])
                        update_settings({"location_lat": lat, "location_lon": lon})
                except Exception as e:
                    print(f"Weather geocode retry error: {e}")

            if lat != 0.0 or lon != 0.0:
                raw = get_weather(lat, lon)
                if raw is not None:
                    parsed = parse_weather(raw, radiation_threshold=radiation_threshold)
                    state.update({"weather": parsed,
                                  "weather_last_updated": utcnow_str()})
        except Exception as e:
            print(f"Weather loop error: {e}")

        state.weather_refresh.wait(timeout=1800)
        state.weather_refresh.clear()


def _fetch_btc_price() -> Optional[float]:
    """Current BTC/USD from mempool.space (free, no auth)."""
    try:
        import requests
        resp = requests.get("https://mempool.space/api/v1/prices", timeout=8)
        resp.raise_for_status()
        return float(resp.json().get("USD", 0) or 0) or None
    except Exception:
        return None


def pool_loop(state: AppState) -> None:
    from .clients.luxor import LuxPoolClient
    while True:
        try:
            price = _fetch_btc_price()
            if price:
                state.set("btc_price_usd", price)

            settings = get_settings()
            api_key = settings.get("lux_pool_api_key", "")
            username = settings.get("lux_pool_username", "")
            api_url = settings.get("lux_pool_api_url", "")
            if api_key and username:
                client = LuxPoolClient(api_key=api_key, username=username, api_url=api_url)
                summary = client.get_summary()
                if summary is not None:
                    state.update({"pool": summary,
                                  "pool_last_updated": utcnow_str()})
        except Exception as e:
            print(f"Pool loop error: {e}")
        # Refresh every 5 minutes, or wake early on a manual refresh.
        state.pool_refresh.wait(timeout=300)
        state.pool_refresh.clear()


def start_background_threads(state: AppState, fleet: MinerFleet,
                             ramp: RampController, notifier: Notifier) -> None:
    control = ControlLoop(state, fleet, ramp, notifier)
    threads = [
        threading.Thread(target=control.run_forever, daemon=True, name="control-loop"),
        threading.Thread(target=weather_loop, args=(state,), daemon=True, name="weather-loop"),
        threading.Thread(target=pool_loop, args=(state,), daemon=True, name="pool-loop"),
        threading.Thread(target=telegram_loop, args=(state, fleet), daemon=True, name="telegram-loop"),
    ]
    for t in threads:
        t.start()
