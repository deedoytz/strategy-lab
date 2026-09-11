"""
Gold NY Session Momentum (XAU_USD only)

Why ORB fails on Gold:
  - ORB uses the Asian range (00:00-07:00 UTC) which is 150-400 pips wide on Gold
  - TP at 1.5× that range = 225-600 pip target in one London session — rarely hit
  - SL on the opposite side of the full range = massive losses when wrong

This strategy instead:
  - Uses the London session (08:00-13:00 UTC) as the ranging period
  - Trades the breakout at NY open (13:00-14:00 UTC) — that's when real Gold moves happen
  - Tighter TP = 1.0× London range (Gold's NY move is fast and mean-reverts less)
  - SL = 0.5× London range (tight, because NY open gives a clean directional push)
  - Filters: only trade if London range is 80-300 pips (skip choppy + anomalous days)
  - Skip if price is already more than 0.5× range beyond the breakout (stale entry)

RR = 1.0 / 0.5 = 2.0 (fixed)
"""

from datetime import datetime, timezone, timedelta
from oanda import fetch_candles_range, get_current_price, _pip

INSTRUMENT   = "XAU_USD"
MIN_RANGE    = 80.0    # pips — skip if London range too tight (no momentum day)
MAX_RANGE    = 300.0   # pips — skip anomalous/news days with huge ranges
BUFFER_PIPS  = 10.0    # confirmation buffer above/below London high/low
SL_MULT      = 0.5     # SL = 0.5× London range
TP_MULT      = 1.0     # TP = 1.0× London range → RR = 2.0


def get_london_range(now: datetime) -> dict | None:
    """Compute London session high/low for today (08:00-13:00 UTC)."""
    london_start = now.replace(hour=8,  minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    london_end   = now.replace(hour=13, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

    try:
        candles = fetch_candles_range(INSTRUMENT, "M15", london_start, london_end)
    except Exception as e:
        print(f"[GoldNY] Failed to fetch London candles: {e}")
        return None

    if not candles:
        return None

    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]
    pip   = _pip(INSTRUMENT)

    high       = max(highs)
    low        = min(lows)
    range_pips = (high - low) / pip

    return {
        "high":       high,
        "low":        low,
        "range_pips": round(range_pips, 1),
        "candles":    len(candles),
    }


def check_signal() -> dict | None:
    """
    Called at 13:00-14:00 UTC (NY open window).
    Returns signal dict if breakout detected, else None.
    """
    now = datetime.now(timezone.utc)
    pip = _pip(INSTRUMENT)
    buf = BUFFER_PIPS * pip

    london = get_london_range(now)
    if not london:
        print("[GoldNY] No London range data")
        return None

    if london["range_pips"] < MIN_RANGE:
        print(f"[GoldNY] London range too tight: {london['range_pips']:.1f}p (min {MIN_RANGE})")
        return None
    if london["range_pips"] > MAX_RANGE:
        print(f"[GoldNY] London range too wide: {london['range_pips']:.1f}p (max {MAX_RANGE}) — skipping")
        return None

    price_data = get_current_price(INSTRUMENT)
    if not price_data:
        print("[GoldNY] Could not fetch current price")
        return None

    mid        = price_data["mid"]
    london_hi  = london["high"]
    london_lo  = london["low"]
    range_size = london_hi - london_lo

    broke_up   = mid > london_hi + buf
    broke_down = mid < london_lo - buf

    if broke_up and broke_down:
        return None  # whipsaw — skip

    if broke_up:
        direction = "LONG"
        entry     = mid
    elif broke_down:
        direction = "SHORT"
        entry     = mid
    else:
        print(f"[GoldNY] No breakout — mid={mid:.2f} inside range [{london_lo:.2f}, {london_hi:.2f}]")
        return None

    # Check if entry is too far from the breakout level (stale)
    if direction == "LONG":
        breakout_level = london_hi + buf
        if mid - breakout_level > range_size * 0.5:
            print(f"[GoldNY] LONG entry stale — already {(mid - breakout_level) / pip:.1f}p past breakout")
            return None
        sl = entry - range_size * SL_MULT
        tp = entry + range_size * TP_MULT
    else:
        breakout_level = london_lo - buf
        if breakout_level - mid > range_size * 0.5:
            print(f"[GoldNY] SHORT entry stale — already {(breakout_level - mid) / pip:.1f}p past breakout")
            return None
        sl = entry + range_size * SL_MULT
        tp = entry - range_size * TP_MULT

    sl_pips = abs(entry - sl) / pip
    tp_pips = abs(tp - entry) / pip
    rr      = round(tp_pips / sl_pips, 2) if sl_pips > 0 else 0

    if sl_pips < 20:
        return None

    return {
        "strategy":   "GOLD_NY",
        "instrument": INSTRUMENT,
        "direction":  direction,
        "entry":      round(entry, 2),
        "sl":         round(sl, 2),
        "tp":         round(tp, 2),
        "sl_pips":    round(sl_pips, 1),
        "tp_pips":    round(tp_pips, 1),
        "rr":         rr,
        "session":    "NY",
        "notes":      (
            f"London range {london['range_pips']:.1f}p | "
            f"Hi={london_hi:.2f} Lo={london_lo:.2f} | "
            f"Entry={entry:.2f} buf={BUFFER_PIPS}p"
        ),
    }
