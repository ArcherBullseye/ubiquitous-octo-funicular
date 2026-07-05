"""Local-time helpers.

The app runs in UTC (container has no site timezone); the site's UTC offset
comes from the weather forecast (`utc_offset_seconds`). Everything that needs
"the user's local date/hour" — holds that roll off at midnight, daily
notifications, EOD projections — goes through these helpers.
"""
from datetime import datetime, timedelta, timezone


def local_now(tz_offset_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=tz_offset_seconds)


def local_today_str(tz_offset_seconds: int) -> str:
    """Local calendar date, YYYY-MM-DD."""
    return local_now(tz_offset_seconds).date().isoformat()


def utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()
