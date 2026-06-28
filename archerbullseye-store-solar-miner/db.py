import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

DB_PATH = Path("data/controller.db")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "solis_api_key": "",
    "solis_api_secret": "",
    "solis_inverter_sn": "",
    "solis_base_url": "https://www.soliscloud.com:13333",
    "miner_ip": "",
    "poll_interval_seconds": 60,
    "soc_on_threshold": 85.0,
    "soc_off_threshold": 80.0,
    "smart_start_enabled": True,
    "smart_soc_on_threshold": 60.0,
    "smart_soc_off_threshold": 55.0,
    "sunny_hours_threshold": 3.0,
    "radiation_threshold_wm2": 300.0,
    "smart_min_pv_w": 1000.0,
    "smart_hold_date": "",  # local date (YYYY-MM-DD) Smart Start is paused until midnight
    "miner_hold_date": "",  # local date (YYYY-MM-DD) miner is force-stopped until midnight
    "location_lat": 0.0,
    "location_lon": 0.0,
    "location_name": "",
    "battery_capacity_kwh": 0.0,
    "pv_peak_kw": 0.0,
    "miner_power_w": 0.0,
    # API failsafe
    "api_fail_action": "stop",
    "api_fail_cycles": 3,
    # Lux Pool
    "lux_pool_api_key": "",
    "lux_pool_username": "",
    "lux_pool_api_url": "",  # leave blank to auto-detect
    # Telegram
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tg_miner_onoff": True,
    "tg_smart_start": True,
    "tg_api_failure": True,
    "tg_soc_low": True,
    "tg_soc_low_pct": 20.0,
    "tg_soc_full": True,
    "tg_hashrate_drop": True,
    "tg_hashrate_drop_pct": 25.0,
    "tg_daily_summary": True,
    "tg_daily_hour": 7,
    "tg_sats_milestone": True,
    "tg_sats_milestone_amount": 1000,
    "tg_sunny_day_ahead": True,
    "tg_weekly_recap": False,
    "tg_weekly_recap_day": 0,
    "tg_weekly_recap_hour": 8,
    # End-of-day battery target
    "eod_soc_target_enabled": False,
    "eod_soc_target": 80.0,
    # Dehumidifier (Tuya local)
    "dehum_device_id": "",
    "dehum_ip": "",
    "dehum_local_key": "",
    "dehum_version": 3.4,
    "dehum_auto_enabled": False,
    "dehum_excess_threshold_w": 500.0,
    "dehum_min_run_minutes": 30,
    "dehum_min_off_minutes": 15,
    "dehum_manual_override_hours": 2,
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pv_efficiency (
                month        INTEGER NOT NULL,
                hour_of_day  INTEGER NOT NULL,
                avg_ratio    REAL DEFAULT 0.0,
                sample_count INTEGER DEFAULT 0,
                PRIMARY KEY (month, hour_of_day)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT,
                soc             REAL,
                battery_power_w REAL,
                input_power_w   REAL,
                grid_power_w    REAL,
                load_power_w    REAL,
                backup_power_w  REAL,
                miner_running   INTEGER,
                action          TEXT,
                effective_soc_on REAL,
                hashrate_mhs    REAL DEFAULT 0.0,
                radiation_wm2   REAL DEFAULT 0.0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_sats (
                date TEXT PRIMARY KEY,
                sats INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migrate: hashrate_mhs column
        try:
            cur.execute("ALTER TABLE readings ADD COLUMN hashrate_mhs REAL DEFAULT 0.0")
        except Exception:
            pass
        # Migrate: radiation_wm2 column
        try:
            cur.execute("ALTER TABLE readings ADD COLUMN radiation_wm2 REAL DEFAULT 0.0")
        except Exception:
            pass
        # Migrate: daily_sats v2 — switch from MAX to delta accumulation.
        # Clear any data recorded under the old MAX strategy so it relearns cleanly.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS _db_migrations (
                name TEXT PRIMARY KEY
            )
        """)
        if not cur.execute(
            "SELECT 1 FROM _db_migrations WHERE name='daily_sats_delta_v2'"
        ).fetchone():
            cur.execute("DELETE FROM daily_sats")
            cur.execute("INSERT INTO _db_migrations(name) VALUES('daily_sats_delta_v2')")
        # Migrate pv_efficiency to per-month schema — old data can't be mapped to a month
        if not cur.execute(
            "SELECT 1 FROM _db_migrations WHERE name='pv_efficiency_monthly_v1'"
        ).fetchone():
            cur.execute("DROP TABLE IF EXISTS pv_efficiency")
            cur.execute("""
                CREATE TABLE pv_efficiency (
                    month        INTEGER NOT NULL,
                    hour_of_day  INTEGER NOT NULL,
                    avg_ratio    REAL DEFAULT 0.0,
                    sample_count INTEGER DEFAULT 0,
                    PRIMARY KEY (month, hour_of_day)
                )
            """)
            cur.execute("INSERT INTO _db_migrations(name) VALUES('pv_efficiency_monthly_v1')")
        for key, value in DEFAULT_SETTINGS.items():
            cur.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def get_settings() -> Dict[str, Any]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
    finally:
        conn.close()

    result = dict(DEFAULT_SETTINGS)
    for row in rows:
        key = row["key"]
        if key in DEFAULT_SETTINGS:
            try:
                result[key] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[key] = row["value"]
    return result


def update_settings(updates: Dict[str, Any]) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        for key, value in updates.items():
            if key in DEFAULT_SETTINGS:
                cur.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
        conn.commit()
    finally:
        conn.close()


def save_reading(r: Dict[str, Any]) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO readings
                (ts, soc, battery_power_w, input_power_w, grid_power_w,
                 load_power_w, backup_power_w, miner_running, action, effective_soc_on,
                 hashrate_mhs, radiation_wm2)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("ts", datetime.utcnow().isoformat()),
                r.get("soc"),
                r.get("battery_power_w"),
                r.get("input_power_w"),
                r.get("grid_power_w"),
                r.get("load_power_w"),
                r.get("backup_power_w"),
                1 if r.get("miner_running") else 0,
                r.get("action", "none"),
                r.get("effective_soc_on"),
                r.get("hashrate_mhs", 0.0),
                r.get("radiation_wm2", 0.0),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_pv_efficiency(month: int, hour_of_day: int, actual_w: float, radiation_wm2: float, pv_peak_kw: float) -> None:
    """Update the rolling per-month/hour efficiency ratio (actual / theoretical max)."""
    if pv_peak_kw <= 0 or radiation_wm2 <= 0:
        return
    theoretical = radiation_wm2 * pv_peak_kw * 1000.0
    ratio = max(0.0, min(1.5, actual_w / theoretical))
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT avg_ratio, sample_count FROM pv_efficiency WHERE month = ? AND hour_of_day = ?",
            (month, hour_of_day),
        )
        row = cur.fetchone()
        if row:
            alpha = 0.15
            new_avg = alpha * ratio + (1 - alpha) * row["avg_ratio"]
            new_count = row["sample_count"] + 1
            cur.execute(
                "UPDATE pv_efficiency SET avg_ratio = ?, sample_count = ? WHERE month = ? AND hour_of_day = ?",
                (new_avg, new_count, month, hour_of_day),
            )
        else:
            cur.execute(
                "INSERT INTO pv_efficiency (month, hour_of_day, avg_ratio, sample_count) VALUES (?, ?, ?, 1)",
                (month, hour_of_day, ratio),
            )
        conn.commit()
    finally:
        conn.close()


def get_pv_efficiency(month: int) -> Dict[int, float]:
    """Returns {hour: avg_ratio} for the given month, falling back to adjacent months."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT month, hour_of_day, avg_ratio FROM pv_efficiency WHERE sample_count >= 3"
        )
        by_hour: Dict[int, Dict[int, float]] = {}
        for row in cur.fetchall():
            h, m, r = row["hour_of_day"], row["month"], row["avg_ratio"]
            by_hour.setdefault(h, {})[m] = r
    finally:
        conn.close()

    result: Dict[int, float] = {}
    for hour, month_map in by_hour.items():
        for offset in range(7):
            candidates = [month] if offset == 0 else [
                ((month - 1 + offset) % 12) + 1,
                ((month - 1 - offset) % 12) + 1,
            ]
            for m in candidates:
                if m in month_map:
                    result[hour] = month_map[m]
                    break
            if hour in result:
                break
    return result


def get_pv_efficiency_detail() -> list:
    """Returns all efficiency rows with month, hour, ratio and sample count."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT month, hour_of_day, avg_ratio, sample_count FROM pv_efficiency ORDER BY month, hour_of_day"
        )
        return [
            {"month": row["month"], "hour": row["hour_of_day"], "ratio": row["avg_ratio"], "samples": row["sample_count"]}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def get_hourly_load_profile(days: int = 7) -> Dict[int, float]:
    """Returns {hour_of_day: avg_load_w} from the last N days of readings."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT CAST(strftime('%H', ts) AS INTEGER) AS hour,
                   AVG(load_power_w) AS avg_w
            FROM readings
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
              AND load_power_w > 0
            GROUP BY hour
            """,
            (f"-{days} days",),
        )
        return {int(row["hour"]): float(row["avg_w"]) for row in cur.fetchall()}
    finally:
        conn.close()


def reset_pv_efficiency() -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM pv_efficiency")
        conn.commit()
    finally:
        conn.close()


def add_daily_sats_delta(date_str: str, delta: int) -> None:
    """Add newly-earned sats (positive delta only) to the calendar-day total."""
    if delta <= 0:
        return
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO daily_sats(date, sats) VALUES(?, ?)
            ON CONFLICT(date) DO UPDATE SET sats = sats + excluded.sats
            """,
            (date_str, delta),
        )
        conn.commit()
    finally:
        conn.close()


def get_today_sats(today: str = None) -> int:
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT sats FROM daily_sats WHERE date = ?", (today,)
        ).fetchone()
        return int(row["sats"]) if row else 0
    finally:
        conn.close()


def get_daily_sats(days: int = 7) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT date, sats FROM daily_sats ORDER BY date DESC LIMIT ?",
            (days,),
        )
        return list(reversed([dict(r) for r in cur.fetchall()]))
    finally:
        conn.close()


def get_recent_readings(hours: int = 2) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, ts, soc, battery_power_w, input_power_w, grid_power_w,
                   load_power_w, backup_power_w, miner_running, action, effective_soc_on,
                   hashrate_mhs, radiation_wm2
            FROM readings
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            ORDER BY ts ASC
            """,
            (f"-{hours} hours",),
        )
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
