"""
EMA Trend + ATR Pullback Strategy

Setup:
  - Trend confirmed when EMA20 > EMA50 > EMA200 (LONG) or EMA20 < EMA50 < EMA200 (SHORT) on H4
  - Entry: price pulls back to EMA20 on H1, then 1H candle closes back in trend direction
  - SL: 1.5× ATR(14) below entry (LONG) or above entry (SHORT)
  - TP: 3× ATR from entry
  - Filter: 4H trend must also be aligned (same as H4 EMA stack)
"""
from oanda import _pip


def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k   = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _atr(candles: list, period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


def check_signal(instrument: str, h4_candles: list, h1_candles: list) -> dict | None:
    """
    instrument: e.g. "EUR_USD"
    h4_candles: list of dicts with open/high/low/close
    h1_candles: list of dicts with open/high/low/close
    Returns signal dict or None.
    """
    if len(h4_candles) < 210 or len(h1_candles) < 25:
        return None

    pip = _pip(instrument)

    # H4 EMA stack
    closes_4h = [c["close"] for c in h4_candles]
    ema20_4h  = _ema(closes_4h, 20)
    ema50_4h  = _ema(closes_4h, 50)
    ema200_4h = _ema(closes_4h, 200)

    if not (ema20_4h and ema50_4h and ema200_4h):
        return None

    e20 = ema20_4h[-1]
    e50 = ema50_4h[-1]
    e200 = ema200_4h[-1]

    bull_trend = e20 > e50 > e200
    bear_trend = e20 < e50 < e200

    if not (bull_trend or bear_trend):
        return None

    direction = "LONG" if bull_trend else "SHORT"

    # ATR on H4
    atr = _atr(h4_candles[-30:], 14)
    if atr == 0:
        return None

    # H1 pullback to EMA20
    closes_1h = [c["close"] for c in h1_candles]
    ema20_1h  = _ema(closes_1h, 20)
    if not ema20_1h:
        return None

    prev_1h   = h1_candles[-2]
    last_1h   = h1_candles[-1]
    ema20_now = ema20_1h[-1]

    # Pullback: previous candle touched EMA20, last candle closed back in trend direction
    touched_ema = (prev_1h["low"] <= ema20_now <= prev_1h["high"])

    if direction == "LONG":
        confirmed = touched_ema and last_1h["close"] > last_1h["open"]
    else:
        confirmed = touched_ema and last_1h["close"] < last_1h["open"]

    if not confirmed:
        return None

    entry   = last_1h["close"]
    sl_dist = atr * 1.5
    tp_dist = atr * 3.0

    if direction == "LONG":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist

    sl_pips = sl_dist / pip
    tp_pips = tp_dist / pip
    rr      = round(tp_pips / sl_pips, 2)

    # Minimum RR filter
    if rr < 1.5:
        return None

    return {
        "strategy":   "TREND",
        "instrument": instrument,
        "direction":  direction,
        "entry":      round(entry, 5),
        "sl":         round(sl, 5),
        "tp":         round(tp, 5),
        "sl_pips":    round(sl_pips, 1),
        "tp_pips":    round(tp_pips, 1),
        "rr":         rr,
        "session":    "ANY",
        "notes":      f"EMA20={e20:.5f} EMA50={e50:.5f} EMA200={e200:.5f} ATR={atr:.5f}",
    }
