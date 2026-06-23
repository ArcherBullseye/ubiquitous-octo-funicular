import requests
import logging
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)

LUX_BASE = "https://app.luxor.tech/api/v2"


def _hs_to_ths(hs_str: str) -> float:
    """Convert H/s string (e.g. '6742821452966') to TH/s float."""
    try:
        return int(hs_str) / 1e12
    except (ValueError, TypeError):
        return 0.0


class LuxPoolClient:
    def __init__(self, api_key: str, username: str, api_url: str = ""):
        self.api_key = api_key.strip()
        self.username = username.strip()   # subaccount name (e.g. "solarshed")
        self.base = api_url.rstrip("/") if api_url.strip() else LUX_BASE

    def _get(self, path: str, params: dict) -> Optional[Dict[str, Any]]:
        try:
            resp = requests.get(
                f"{self.base}{path}",
                params=params,
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                timeout=15,
            )
            if not resp.ok:
                log.error("Luxor %s -> %s: %s", path, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except Exception as e:
            log.error("Luxor API error for %s: %s", path, e)
            return None

    def get_summary(self) -> Optional[Dict[str, Any]]:
        """
        Single call to /pool/summary/BTC — returns hashrate, unpaid balance,
        24h revenue, uptime, and active miner count.
        """
        data = self._get("/pool/summary/BTC", {"subaccount_names": self.username})
        if not data:
            return None

        # Hashrate: returned as H/s string, convert to TH/s
        hashrate_ths = _hs_to_ths(data.get("hashrate_5m") or "0")
        if hashrate_ths == 0:
            # Fall back to 1h average if 5m is zero (miner just started/stopped)
            hashrate_ths = _hs_to_ths(data.get("hashrate_1h") or "0")

        # Unpaid balance (BTC → sats)
        unpaid_btc = sum(
            float(b.get("revenue", 0))
            for b in data.get("balance", [])
            if b.get("currency_type") == "BTC"
        )
        unpaid_sats = int(unpaid_btc * 1e8)

        # 24h rolling revenue (BTC → sats)
        rev_24h_btc = sum(
            float(r.get("revenue", 0))
            for r in data.get("revenue_24h", [])
            if r.get("currency_type") == "BTC" and r.get("revenue_type") == "MINING"
        )
        sats_today = int(rev_24h_btc * 1e8)

        # All-time revenue (BTC → sats)
        rev_alltime_btc = sum(
            float(r.get("revenue", 0))
            for r in data.get("revenue_all_time", [])
            if r.get("currency_type") == "BTC" and r.get("revenue_type") == "MINING"
        )

        # Stale hashrate for efficiency display
        stale_ths = _hs_to_ths(data.get("hashrate_stale_1h") or "0")
        efficiency_pct = round(float(data.get("efficiency_5m") or 0) * 100, 1)
        uptime_pct = round(float(data.get("uptime_24h") or 0) * 100, 1)

        return {
            "sats_today": sats_today,
            "hashrate_ths": round(hashrate_ths, 3),
            "hashrate_24h_ths": round(_hs_to_ths(data.get("hashrate_24h") or "0"), 3),
            "stale_ths": round(stale_ths, 3),
            "unpaid_sats": unpaid_sats,
            "alltime_sats": int(rev_alltime_btc * 1e8),
            "active_miners": int(data.get("active_miners") or 0),
            "efficiency_pct": efficiency_pct,
            "uptime_pct": uptime_pct,
            # legacy keys kept for compatibility
            "valid_shares": 0,
            "stale_shares": 0,
        }
