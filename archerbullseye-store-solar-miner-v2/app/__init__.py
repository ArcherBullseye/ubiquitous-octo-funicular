"""Solar Miner v2 — solar-surplus-driven load controller.

Layers (each only reaches downward):

    web/        HTTP API + dashboard
    control/    decision logic: smart start, ramp, EOD, notifications, loops
    devices/    hardware behind capability interfaces (miners, dehumidifier,
                thermostats-to-come)
    clients/    thin API clients (Solis, LuxOS, Luxor, Open-Meteo, Telegram, Tuya)
    db / state / config    storage, shared runtime state, settings schema

create_app() builds everything; run.py starts the loops and serves.
"""
import os
import warnings

warnings.filterwarnings("ignore", message=".*urllib3.*", category=Warning)
warnings.filterwarnings("ignore", message=".*OpenSSL.*", category=Warning)
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

from flask import Flask

from .control.notifications import Notifier
from .control.ramp import RampController
from .db import get_settings, init_db, update_settings
from .devices.miner import MinerFleet
from .state import AppState
from .web.routes import create_blueprint


def _seed_settings_from_env() -> None:
    """First-run convenience: seed empty settings from environment variables."""
    settings_now = get_settings()
    seed_map = {
        "SOLIS_API_KEY":     "solis_api_key",
        "SOLIS_API_SECRET":  "solis_api_secret",
        "SOLIS_INVERTER_SN": "solis_inverter_sn",
        "LUXOS_MINER_IP":    "miner_ip",
    }
    seeds = {
        db_key: os.getenv(env_key, "")
        for env_key, db_key in seed_map.items()
        if os.getenv(env_key, "") and not settings_now.get(db_key)
    }
    if seeds:
        update_settings(seeds)


def create_app():
    """Build the Flask app + core objects. Returns (flask_app, state, fleet,
    ramp, notifier); the caller starts the background threads."""
    init_db()
    _seed_settings_from_env()

    state = AppState()
    fleet = MinerFleet()
    ramp = RampController(fleet)
    notifier = Notifier(state)

    flask_app = Flask(__name__,
                      template_folder="web/templates",
                      static_folder="web/static")
    flask_app.register_blueprint(create_blueprint(state, fleet))
    return flask_app, state, fleet, ramp, notifier
