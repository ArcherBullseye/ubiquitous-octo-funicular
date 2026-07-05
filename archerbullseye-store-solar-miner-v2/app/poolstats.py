"""Pool revenue history — shared by the daily-sats API and the weekly recap.

Historical days come from Luxor's settled revenue API, cached per Luxor
accounting day; today's bar uses the live rolling-24h figure from the pool
summary when included.
"""
import threading
from datetime import datetime, timedelta, timezone
from typing import List

from .clients.luxor import LuxPoolClient
from .db import get_settings
from .state import AppState

_revenue_cache: dict = {"utc_date": None, "rows": []}
_revenue_lock = threading.Lock()


def fetch_daily_sats_rows(state: AppState, days: int, include_today: bool = True) -> List[dict]:
    """[{date, sats}] for `days` calendar days (local time).

    When include_today is True the most recent bar is today (live rolling-24h
    sats); when False it's yesterday and every bar comes from the settled
    revenue API — avoids the rolling window bleeding yesterday's earnings
    onto today's bar.
    """
    settings = get_settings()
    api_key = settings.get("lux_pool_api_key", "")
    username = settings.get("lux_pool_username", "")
    api_url = settings.get("lux_pool_api_url", "")

    tz_offset = state.tz_offset_seconds()
    today_live = (state.get("pool") or {}).get("sats_today", 0) or 0

    local_today = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).date()
    today_str = local_today.isoformat()
    last_day = local_today if include_today else local_today - timedelta(days=1)

    history_by_date: dict = {}
    if api_key and username:
        # Luxor finalizes each day's revenue at 05:00 UTC. Key the cache on
        # that boundary (now - 5h) so we refetch right when new data posts.
        lux_day = (datetime.now(timezone.utc) - timedelta(hours=5)).date()
        with _revenue_lock:
            if _revenue_cache["utc_date"] != lux_day:
                try:
                    client = LuxPoolClient(api_key=api_key, username=username, api_url=api_url)
                    start = (local_today - timedelta(days=days + 1)).isoformat()
                    _revenue_cache["rows"] = client.get_revenue_history(start, today_str)
                    _revenue_cache["utc_date"] = lux_day
                except Exception as e:
                    print(f"Revenue history fetch error: {e}")
            history_by_date = {r["date"]: r["sats"] for r in _revenue_cache["rows"]}

    if include_today:
        history_by_date[today_str] = today_live

    return [
        {"date": (last_day - timedelta(days=i)).isoformat(),
         "sats": history_by_date.get((last_day - timedelta(days=i)).isoformat(), 0)}
        for i in range(days - 1, -1, -1)
    ]
