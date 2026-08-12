"""
OANDA Practice API client — read-only, candle fetching only.
No trade execution. No risk.
"""
import os
import requests
from datetime import datetime, timezone, timedelta

BASE_URL   = os.getenv("OANDA_PRACTICE_URL", "https://api-fxpractice.oanda.com")
API_KEY    = os.getenv("OANDA_API_KEY", "")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "101-002-39491454-001")

INSTRUMENTS = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]

GRANULARITIES = ["M15", "H1", "H4", "D"]  # what we store


def _headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept-Datetime-Format": "RFC3339",
    }


def _pip(instrument: str) -> float:
    if "XAU" in instrument: return 0.1
    if "JPY" in instrument: return 0.01
    return 0.0001


def fetch_candles(instrument: str, granularity: str, count: int = 200) -> list:
    """Fetch the last N complete candles."""
    r = requests.get(
        f"{BASE_URL}/v3/instruments/{instrument}/candles",
        headers=_headers(),
        params={"granularity": granularity, "count": count, "price": "M"},
        timeout=15,
    )
    r.raise_for_status()
    candles = r.json().get("candles", [])
    return [{
        "time":   c["time"],
        "open":   float(c["mid"]["o"]),
        "high":   float(c["mid"]["h"]),
        "low":    float(c["mid"]["l"]),
        "close":  float(c["mid"]["c"]),
        "volume": c.get("volume", 0),
    } for c in candles if c.get("complete", True)]


def fetch_candles_range(instrument: str, granularity: str, from_dt: datetime, to_dt: datetime) -> list:
    """Fetch candles between two datetimes."""
    params = {
        "granularity": granularity,
        "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to":   to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": "BA",  # bid+ask for resolver accuracy
    }
    r = requests.get(
        f"{BASE_URL}/v3/instruments/{instrument}/candles",
        headers=_headers(),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    candles = r.json().get("candles", [])
    out = []
    for c in candles:
        bid = c.get("bid", {})
        ask = c.get("ask", {})
        out.append({
            "time":      c["time"],
            "high":      float(ask.get("h", c["mid"]["h"]) if "mid" in c else ask.get("h", 0)),
            "low":       float(bid.get("l", c["mid"]["l"]) if "mid" in c else bid.get("l", 0)),
            "open":      float(c["mid"]["o"]) if "mid" in c else 0,
            "close":     float(c["mid"]["c"]) if "mid" in c else 0,
        })
    return out


def get_current_price(instrument: str) -> dict | None:
    try:
        r = requests.get(
            f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}/pricing?instruments={instrument}",
            headers=_headers(),
            timeout=10,
        )
        prices = r.json().get("prices", [])
        if not prices:
            return None
        p   = prices[0]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "pip": _pip(instrument)}
    except Exception:
        return None
