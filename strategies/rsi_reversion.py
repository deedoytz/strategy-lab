"""
RSI Mean Reversion on 4H

Setup:
  - RSI(14) on 4H crosses below 30 → LONG signal
  - RSI(14) on 4H crosses above 70 → SHORT signal
  - Weekly trend filter: only take LONGs when Daily EMA200 is pointing up, SHORTs when down
  - SL: recent swing low/high (last 8 candles) + buffer
  - TP: RSI midline 50 zone (prior structure mid)
  - Min RR: 1.5
"""
from oanda import _pip


def _rsi(closes: list, period: int = 14) -> list:
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsi_vals = []

    for i in range(period, len(closes) - 1):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs    = avg_g / avg_l if avg_l != 0 else 100
        rsi_vals.append(100 - 100 / (1 + rs))

    return rsi_vals


def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k   = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def check_signal(instrument: str, h4_candles: list, daily_candles: list) -> dict | None:
    """
    instrument:     e.g. "EUR_USD"
    h4_candles:     list of H4 OHLC dicts (need 50+)
    daily_candles:  list of Daily OHLC dicts (need 210+ for EMA200)
    Returns signal dict or None.
    """
    if len(h4_candles) < 30 or len(daily_candles) < 210:
        return None

    pip = _pip(instrument)

    # RSI on 4H
    closes_4h = [c["close"] for c in h4_candles]
    rsi_vals  = _rsi(closes_4h, 14)
    if len(rsi_vals) < 2:
        return None

    prev_rsi = rsi_vals[-2]
    last_rsi = rsi_vals[-1]

    # RSI cross
    crossed_oversold   = prev_rsi <= 30 and last_rsi > 30   # was below, now recovering → LONG
    crossed_overbought = prev_rsi >= 70 and last_rsi < 70   # was above, now falling  → SHORT

    if not (crossed_oversold or crossed_overbought):
        return None

    direction = "LONG" if crossed_oversold else "SHORT"

    # Daily EMA200 trend filter
    closes_d  = [c["close"] for c in daily_candles]
    ema200_d  = _ema(closes_d, 200)
    if not ema200_d:
        return None

    ema200_now  = ema200_d[-1]
    ema200_prev = ema200_d[-5]  # 5 days ago
    daily_up    = ema200_now > ema200_prev
    daily_down  = ema200_now < ema200_prev

    # Filter: only trade with the daily trend
    if direction == "LONG" and not daily_up:
        return None
    if direction == "SHORT" and not daily_down:
        return None

    # SL: recent swing over last 8 4H candles
    recent = h4_candles[-8:]
    swing_low  = min(c["low"]  for c in recent)
    swing_high = max(c["high"] for c in recent)
    buf = pip * 5  # 5 pip buffer past swing

    last_close = closes_4h[-1]

    if direction == "LONG":
        sl    = swing_low - buf
        tp_target = swing_high if swing_high > last_close else last_close + (last_close - swing_low) * 0.6
        tp    = last_close + (tp_target - last_close) * 0.6
    else:
        sl    = swing_high + buf
        tp_target = swing_low if swing_low < last_close else last_close - (swing_high - last_close) * 0.6
        tp    = last_close - (last_close - tp_target) * 0.6

    sl_pips = abs(last_close - sl) / pip
    tp_pips = abs(tp - last_close) / pip

    if sl_pips < 5 or tp_pips < 5:
        return None

    rr = round(tp_pips / sl_pips, 2)
    if rr < 1.5:
        return None

    return {
        "strategy":   "RSI_REVERSION",
        "instrument": instrument,
        "direction":  direction,
        "entry":      round(last_close, 5),
        "sl":         round(sl, 5),
        "tp":         round(tp, 5),
        "sl_pips":    round(sl_pips, 1),
        "tp_pips":    round(tp_pips, 1),
        "rr":         rr,
        "session":    "ANY",
        "notes":      f"RSI crossed {'30↑' if direction == 'LONG' else '70↓'} | prev={prev_rsi:.1f} curr={last_rsi:.1f} | EMA200={'up' if daily_up else 'down'}",
    }
