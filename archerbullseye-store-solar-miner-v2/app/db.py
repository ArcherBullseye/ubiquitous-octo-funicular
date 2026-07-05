"""SQLite storage.

Schema-compatible with Solar Miner v1 (data/controller.db): point this app at
an existing v1 data directory and all history, settings, learned efficiency,
and the ramp log carry over unchanged. Nothing is ever pruned — history is
kept forever by design.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .config import DEFAULTS

DB_PATH = Path("data/controller.db")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            CREATE TABLE IF NOT EXISTS pv_efficiency (
                month        INTEGER NOT NULL,
                hour_of_day  INTEGER NOT NULL,
                avg_ratio    REAL DEFAULT 0.0,
                sample_count INTEGER DEFAULT 0,
                PRIMARY KEY (month, hour_of_day)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_sats (
                date TEXT PRIMARY KEY,
                sats INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ramp_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT,
                armed       INTEGER,
                soc         REAL,
                grid_w      REAL,
                battery_w   REAL,
                reserve_w   REAL,
                headroom_w  REAL,
                current_w   REAL,
                target_w    REAL,
                detail      TEXT
            )
        """)
        # v1 migration ledger — a v1 database opens cleanly with its
        # already-applied migrations skipped; older ones still run.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS _db_migrations (
                name TEXT PRIMARY KEY
            )
        """)
        def _needs(name: str) -> bool:
            return not cur.execute(
                "SELECT 1 FROM _db_migrations WHERE name=?", (name,)
            ).fetchone()
        # daily_sats v2: switched from MAX to delta accumulation — clear data
        # recorded under the old strategy so it relearns cleanly.
        if _needs("daily_sats_delta_v2"):
            cur.execute("DELETE FROM daily_sats")
            cur.execute("INSERT INTO _db_migrations(name) VALUES('daily_sats_delta_v2')")
        # pv_efficiency to per-month schema — old rows can't be mapped to a month.
        if _needs("pv_efficiency_monthly_v1"):
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
        # Migrate: columns added over v1's life (no-op on current schemas).
        for ddl in (
            "ALTER TABLE readings ADD COLUMN hashrate_mhs REAL DEFAULT 0.0",
            "ALTER TABLE readings ADD COLUMN radiation_wm2 REAL DEFAULT 0.0",
        ):
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError:
                pass
        for key, value in DEFAULTS.items():
            cur.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


# ── Settings ─────────────────────────────────────────────────────

def get_settings() -> Dict[str, Any]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()

    result = dict(DEFAULTS)
    for row in rows:
        if row["key"] in DEFAULTS:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = row["value"]
    return result


def update_settings(updates: Dict[str, Any]) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        for key, value in updates.items():
            if key in DEFAULTS:
                cur.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
        conn.commit()
    finally:
        conn.close()


# ── Readings history ─────────────────────────────────────────────

def save_reading(r: Dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO readings
                (ts, soc, battery_power_w, input_power_w, grid_power_w,
                 load_power_w, backup_power_w, miner_running, action,
                 effective_soc_on, hashrate_mhs, radiation_wm2)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("ts", _utcnow()),
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


def get_recent_readings(hours: int = 2) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, ts, soc, battery_power_w, input_power_w, grid_power_w,
                   load_power_w, backup_power_w, miner_running, action,
                   effective_soc_on, hashrate_mhs, radiation_wm2
            FROM readings
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            ORDER BY ts ASC
            """,
            (f"-{hours} hours",),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_hourly_load_profile(days: int = 7) -> Dict[int, float]:
    """{hour_of_day: avg_load_w} learned from the last N days of readings."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT CAST(strftime('%H', ts) AS INTEGER) AS hour,
                   AVG(load_power_w) AS avg_w
            FROM readings
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
              AND load_power_w > 0
            GROUP BY hour
            """,
            (f"-{days} days",),
        ).fetchall()
        return {int(r["hour"]): float(r["avg_w"]) for r in rows}
    finally:
        conn.close()


# ── Ramp activity log ────────────────────────────────────────────

def log_ramp_event(e: Dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO ramp_log
                (ts, armed, soc, grid_w, battery_w, reserve_w, headroom_w,
                 current_w, target_w, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                e.get("ts", _utcnow()),
                1 if e.get("armed") else 0,
                e.get("soc"), e.get("grid_w"), e.get("battery_w"),
                e.get("reserve_w"), e.get("headroom_w"),
                e.get("current_w"), e.get("target_w"),
                e.get("detail", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_ramp_log(limit: int = 60) -> List[Dict[str, Any]]:
    """Most-recent ramp events first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ramp_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Learned PV efficiency ────────────────────────────────────────

def update_pv_efficiency(month: int, hour_of_day: int, actual_w: float,
                         radiation_wm2: float, pv_peak_kw: float) -> None:
    """Update the rolling per-month/hour efficiency ratio (actual / theoretical)."""
    if pv_peak_kw <= 0 or radiation_wm2 <= 0:
        return
    theoretical = radiation_wm2 * pv_peak_kw * 1000.0
    ratio = max(0.0, min(1.5, actual_w / theoretical))
    conn = _connect()
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT avg_ratio, sample_count FROM pv_efficiency WHERE month = ? AND hour_of_day = ?",
            (month, hour_of_day),
        ).fetchone()
        if row:
            alpha = 0.15
            new_avg = alpha * ratio + (1 - alpha) * row["avg_ratio"]
            cur.execute(
                "UPDATE pv_efficiency SET avg_ratio = ?, sample_count = ? WHERE month = ? AND hour_of_day = ?",
                (new_avg, row["sample_count"] + 1, month, hour_of_day),
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
    """{hour: avg_ratio} for the given month, borrowing from the nearest month
    when this month has no learned data yet (>=3 samples required)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT month, hour_of_day, avg_ratio FROM pv_efficiency WHERE sample_count >= 3"
        ).fetchall()
    finally:
        conn.close()

    by_hour: Dict[int, Dict[int, float]] = {}
    for row in rows:
        by_hour.setdefault(row["hour_of_day"], {})[row["month"]] = row["avg_ratio"]

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


def get_pv_efficiency_detail() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT month, hour_of_day, avg_ratio, sample_count FROM pv_efficiency ORDER BY month, hour_of_day"
        ).fetchall()
        return [
            {"month": r["month"], "hour": r["hour_of_day"],
             "ratio": r["avg_ratio"], "samples": r["sample_count"]}
            for r in rows
        ]
    finally:
        conn.close()


def reset_pv_efficiency() -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM pv_efficiency")
        conn.commit()
    finally:
        conn.close()


# ── Daily sats ───────────────────────────────────────────────────

def add_daily_sats_delta(date_str: str, delta: int) -> None:
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


def get_daily_sats(days: int = 7) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT date, sats FROM daily_sats ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))
    finally:
        conn.close()
