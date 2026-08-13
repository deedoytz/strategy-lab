"""
London Opening Range Breakout (ORB)

Setup:
  - Asian session range = high/low of 00:00–07:00 UTC (M15 candles)
  - At 07:00–08:30 UTC: check if 07:00 candle HIGH/LOW breaks Asian range
  - LONG:  candle high > Asian high + buffer  (confirmed breakout candle)
  - SHORT: candle low  < Asian low  - buffer
  - Skip if BOTH high and low break (whipsaw / inside-outside candle)
  - SL = opposite end of range + buffer
  - TP = entry ± 1.5× range  (backtest: 1.5× better than 2× on limited data)
  - Filter: range size >= min_pips AND range <= 120 pips (skip anomalous days)

Backtest (H1 proxy, ~12 trading days):
  - Buf=2 TP×1.0 → 54.5% WR (breakeven RR, high win rate)
  - Buf=2 TP×1.5 → 37.5% WR (sub-breakeven for 1.5× RR)
  - Verdict: use TP×1.5 (industry standard), accept that sample is small
"""
from datetime import datetime, timezone, timedelta
from oanda import fetch_candles_range, get_current_price, _pip


MIN_RANGE_PIPS = {
    "EUR_USD":  8.0,
    "GBP_USD":  8.0,
    "USD_JPY":  8.0,
    "XAU_USD": 60.0,
}

MAX_RANGE_PIPS = {
    "EUR_USD": 120.0,
    "GBP_USD": 120.0,
    "USD_JPY": 120.0,
    "XAU_USD": 800.0,
}

BUFFER_PIPS = {
    "EUR_USD": 2.0,
    "GBP_USD": 2.0,
    "USD_JPY": 2.0,
    "XAU_USD": 15.0,
}

TP_MULT = 1.5


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

    max_range = MAX_RANGE_PIPS.get(instrument, 120.0)
    if asian["range_pips"] < min_range:
        print(f"[ORB] {instrument} range too tight: {asian['range_pips']:.1f}p (min {min_range})")
        return None
    if asian["range_pips"] > max_range:
        print(f"[ORB] {instrument} range too wide: {asian['range_pips']:.1f}p (max {max_range}) — anomalous day, skipping")
        return None

    # Fetch the 07:00–08:30 entry window to detect breakout via candle H/L
    # Use a fixed 07:00 start and current time + 1 candle as end (handles any call time)
    entry_start = now.replace(hour=7, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    entry_end   = min(
        now.replace(hour=8, minute=30, second=0, microsecond=0, tzinfo=timezone.utc),
        now + timedelta(minutes=15),  # don't fetch beyond present
    )
    try:
        entry_candles = fetch_candles_range(instrument, "M15", entry_start, entry_end)
    except Exception:
        entry_candles = []

    asian_high  = asian["high"]
    asian_low   = asian["low"]
    range_size  = asian_high - asian_low

    # Detect breakout using entry candle H/L (more reliable than current bid/ask)
    if entry_candles:
        candle_high = max(c["high"] for c in entry_candles)
        candle_low  = min(c["low"]  for c in entry_candles)
        broke_up    = candle_high > asian_high + buf
        broke_down  = candle_low  < asian_low  - buf
        if broke_up and broke_down:
            return None  # Both sides — whipsaw / skip
        if broke_up:
            direction = "LONG"
            entry = asian_high + buf  # enter at the breakout level
        elif broke_down:
            direction = "SHORT"
            entry = asian_low - buf
        else:
            return None  # No breakout yet
    else:
        # Fallback: use live mid price
        price_data = get_current_price(instrument)
        if not price_data:
            return None
        mid = price_data["mid"]
        if mid > asian_high + buf:
            direction = "LONG";  entry = mid
        elif mid < asian_low - buf:
            direction = "SHORT"; entry = mid
        else:
            return None

    if direction == "LONG":
        sl      = asian_low - buf
        tp      = entry + range_size * TP_MULT
        sl_pips = (entry - sl) / pip
        tp_pips = (tp - entry) / pip
    else:
        sl      = asian_high + buf
        tp      = entry - range_size * TP_MULT
        sl_pips = (sl - entry) / pip
        tp_pips = (entry - tp) / pip

    if sl_pips < 5:
        return None

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
