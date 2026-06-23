#!/usr/bin/env python3
"""
Solis Cloud -> LuxOS miner controller.

Polls Solis Cloud every POLL_INTERVAL_SECONDS seconds. If battery SOC
rises above SOC_ON_THRESHOLD the miner is started; if it falls below
SOC_OFF_THRESHOLD the miner is paused.
"""

import os
import time
import warnings
from typing import Optional

warnings.filterwarnings("ignore", category=Warning, module="urllib3")

from dotenv import load_dotenv

from solis_api import SolisClient, SolisApiError, parse_power_and_soc
from luxos_api import LuxOsClient, LuxOsError

load_dotenv()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def print_status(cycle: int, readings: dict, current_state: str, desired_state: str, action: str) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    soc = readings["soc"]
    batt = readings["battery_power_w"]
    inp = readings["input_power_w"]
    grid = readings["grid_power_w"]
    load = readings["load_power_w"]
    backup = readings["backup_power_w"]

    batt_dir = "charging" if batt > 0 else "discharging" if batt < 0 else "idle"
    grid_dir = "exporting" if grid > 0 else "importing" if grid < 0 else "idle"

    print(f"\n{'='*55}")
    print(f"  Cycle #{cycle}  —  {now}")
    print(f"{'='*55}")
    print(f"  Battery SOC     : {soc:.1f}%")
    print(f"  Battery power   : {abs(batt):.0f} W  ({batt_dir})")
    print(f"  Input power     : {inp:.0f} W")
    print(f"  Output — grid   : {abs(grid):.0f} W  ({grid_dir})")
    print(f"  Output — load   : {load:.0f} W")
    print(f"  Output — backup : {backup:.0f} W")
    print(f"{'─'*55}")
    print(f"  Miner current   : {current_state}")
    print(f"  Miner desired   : {desired_state}")
    print(f"  Action          : {action}")
    print(f"{'='*55}")


def main() -> None:
    solis = SolisClient(
        api_key=require_env("SOLIS_API_KEY"),
        api_secret=require_env("SOLIS_API_SECRET"),
        base_url=os.getenv("SOLIS_BASE_URL", "https://www.soliscloud.com:13333"),
    )
    inverter_sn = require_env("SOLIS_INVERTER_SN")

    luxos = LuxOsClient(ip=require_env("LUXOS_MINER_IP"))

    soc_on = float(os.getenv("SOC_ON_THRESHOLD", "80.0"))
    soc_off = float(os.getenv("SOC_OFF_THRESHOLD", "80.0"))
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

    print(f"Controller starting  |  SOC on≥{soc_on:.0f}%  off<{soc_off:.0f}%  poll={poll_interval}s")
    print(f"Miner: {require_env('LUXOS_MINER_IP')}:4028  (no auth required)")

    # Establish and hold the miner session for the lifetime of the process.
    # LuxOS only allows one active session at a time.
    try:
        luxos.logon()
        print(f"Miner session opened: {luxos._session_id}")
    except LuxOsError as e:
        print(f"WARNING: Could not open miner session at startup: {e}")

    cycle = 0

    try:
        while True:
            loop_start = time.monotonic()
            cycle += 1

            # --- Poll Solis Cloud ---
            try:
                inverter_data = solis.get_inverter_detail(inverter_sn)
                readings = parse_power_and_soc(inverter_data)
            except SolisApiError as e:
                print(f"\n[cycle #{cycle}]  ERROR  Solis API: {e} — skipping cycle")
                elapsed = time.monotonic() - loop_start
                time.sleep(max(0.0, poll_interval - elapsed))
                continue

            soc = readings["soc"]

            # --- Query miner's actual current state ---
            try:
                actually_mining = luxos.is_mining()
                current_state = "ON  (mining)" if actually_mining else "OFF (sleeping)"
            except LuxOsError as e:
                actually_mining = None
                current_state = f"UNKNOWN  ({e})"

            # --- Determine desired state from SOC ---
            desired_mining = soc >= soc_on
            desired_state = f"ON  (SOC {soc:.1f}% >= {soc_on:.0f}%)" if desired_mining else f"OFF (SOC {soc:.1f}% < {soc_off:.0f}%)"

            # --- Act if current != desired ---
            action = "none"
            if actually_mining is not None and actually_mining != desired_mining:
                if desired_mining:
                    try:
                        luxos.start_mining()
                        action = "STARTED miner"
                    except LuxOsError as e:
                        action = f"ERROR starting miner: {e}"
                else:
                    try:
                        luxos.stop_mining()
                        action = "STOPPED miner"
                    except LuxOsError as e:
                        action = f"ERROR stopping miner: {e}"
            elif actually_mining is None:
                action = "skipped (miner unreachable)"
            else:
                action = "none — already in correct state"

            print_status(cycle, readings, current_state, desired_state, action)

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, poll_interval - elapsed))

    finally:
        luxos.close()
        print("Miner session closed")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user")
