"""
London Opening Range Breakout (ORB)

Setup:
  - Asian session range = high/low of 00:00–07:00 UTC
  - At 08:00 UTC London open: if price breaks above Asian high → LONG
                               if price breaks below Asian low  → SHORT
  - SL = opposite side of range + buffer
  - TP = entry + 1.5× range size
  - Filter: only take if range size >= min_pips (avoid chop)
"""
from datetime import datetime, timezone, timedelta
from oanda import fetch_candles_range, get_current_price, _pip


MIN_RANGE_PIPS = {
    "EUR_USD": 10.0,
    "GBP_USD": 12.0,
    "USD_JPY": 12.0,
    "XAU_USD": 80.0,
}

BUFFER_PIPS = {
    "EUR_USD": 2.0,
    "GBP_USD": 2.0,
    "USD_JPY": 3.0,
    "XAU_USD": 15.0,
}


def get_asian_range(instrument: str, date: datetime) -> dict | None:
    """Compute Asian session high/low for the given UTC date."""
    asian_start = date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    asian_end   = date.replace(hour=7, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

    try:
        candles = fetch_candles_range(instrument, "M15", asian_start, asian_end)
    except Exception as e:
        print(f"[ORB] Failed to fetch Asian candles for {instrument}: {e}")
        return None

    if not candles:
        return None

    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]
    pip   = _pip(instrument)

    asian_high  = max(highs)
    asian_low   = min(lows)
    range_pips  = (asian_high - asian_low) / pip

    return {
        "high":       asian_high,
        "low":        asian_low,
        "range_pips": round(range_pips, 1),
    }


def check_signal(instrument: str) -> dict | None:
    """
    Called at 08:00 UTC. Returns signal dict if breakout detected, else None.
    """
    now = datetime.now(timezone.utc)
    pip = _pip(instrument)
    min_range  = MIN_RANGE_PIPS.get(instrument, 10.0)
    buf_pips   = BUFFER_PIPS.get(instrument, 2.0)
    buf        = buf_pips * pip

    asian = get_asian_range(instrument, now)
    if not asian:
        return None

    if asian["range_pips"] < min_range:
        print(f"[ORB] {instrument} range too tight: {asian['range_pips']:.1f} pips (min {min_range})")
        return None

    price_data = get_current_price(instrument)
    if not price_data:
        return None

    mid         = price_data["mid"]
    asian_high  = asian["high"]
    asian_low   = asian["low"]
    range_size  = asian_high - asian_low
    tp_mult     = 1.5

    if mid > asian_high + buf:
        direction = "LONG"
        entry = mid
        sl    = asian_low - buf
        tp    = entry + (range_size * tp_mult)
        sl_pips = (entry - sl) / pip
        tp_pips = (tp - entry) / pip
    elif mid < asian_low - buf:
        direction = "SHORT"
        entry = mid
        sl    = asian_high + buf
        tp    = entry - (range_size * tp_mult)
        sl_pips = (sl - entry) / pip
        tp_pips = (entry - tp) / pip
    else:
        return None  # No breakout yet

    if sl_pips < 5:
        return None  # SL too tight

    rr = round(tp_pips / sl_pips, 2) if sl_pips > 0 else 0

    return {
        "strategy":   "ORB",
        "instrument": instrument,
        "direction":  direction,
        "entry":      round(entry, 5),
        "sl":         round(sl, 5),
        "tp":         round(tp, 5),
        "sl_pips":    round(sl_pips, 1),
        "tp_pips":    round(tp_pips, 1),
        "rr":         rr,
        "session":    "London",
        "notes":      f"Asian range {asian['range_pips']:.1f} pips | High {asian_high} / Low {asian_low}",
    }
