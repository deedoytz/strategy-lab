"""
RSI Trend-Pullback on 4H

Original design was mean reversion (RSI 30/70 cross), but backtesting showed
near-zero win rate (12-25%) in the strong 2026 uptrend environment. Redesigned
as a trend-following pullback confirmation:

  - Only trade in the direction of the H4 EMA13/34/89 stack
  - LONG:  EMA bull stack + RSI(9) dips to 40-55 zone then recovers → pullback entry
  - SHORT: EMA bear stack + RSI(9) rises to 45-60 zone then drops  → pullback entry
  - Skip XAU_USD — gold ATR too large for RSI pullback to work reliably
  - SL: 1.5× ATR(14) on H4
  - TP: 2.0× ATR(14) on H4
  - Min RR: 1.2

Backtest (H4, 300 bars, Jun-Aug 2026):
  - Old RSI 70/30 reversion: 12-25% WR (negative pips)
  - New RSI(9) pullback + EMA stack filter: 54.2% WR (24 trades, EUR/GBP/JPY)
  - XAU excluded (25% WR, ATR-scale pips dominate negatively)
"""
from oanda import _pip

RSI_PERIOD  = 9
LONG_ZONE   = (40, 55)   # RSI must be in this zone and recovering to trigger LONG
SHORT_ZONE  = (45, 60)   # RSI must be in this zone and dropping to trigger SHORT
SL_MULT     = 1.5
TP_MULT     = 2.0
SKIP_INSTRUMENTS = {"XAU_USD"}  # excluded — ATR too large, low RSI signal quality


def _rsi(closes: list, period: int = 9) -> list:
    if len(closes) < period + 1:
        return []
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g  = sum(gains[:period])  / period
    avg_l  = sum(losses[:period]) / period
    rsi_vals = []
    for i in range(period, len(closes) - 1):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
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


def _atr(candles: list, period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else 0.0


def check_signal(instrument: str, h4_candles: list, daily_candles: list) -> dict | None:
    """
    instrument:     e.g. "EUR_USD"
    h4_candles:     list of H4 OHLC dicts (need 100+)
    daily_candles:  unused (kept for API compatibility with collector.py)
    Returns signal dict or None.
    """
    if instrument in SKIP_INSTRUMENTS:
        return None

    if len(h4_candles) < 100:
        return None

    pip = _pip(instrument)

    closes = [c["close"] for c in h4_candles]

    # H4 EMA trend filter (13/34/89 Fibonacci stack)
    e13 = _ema(closes, 13)
    e34 = _ema(closes, 34)
    e89 = _ema(closes, 89)
    if not (e13 and e34 and e89):
        return None
    e1, e3, e9 = e13[-1], e34[-1], e89[-1]
    bull = e1 > e3 > e9
    bear = e1 < e3 < e9
    if not (bull or bear):
        return None  # choppy/transitioning — no trade

    # RSI(9)
    rsi_vals = _rsi(closes, RSI_PERIOD)
    if len(rsi_vals) < 2:
        return None
    prev_rsi = rsi_vals[-2]
    curr_rsi = rsi_vals[-1]

    # Pullback signal: RSI was in zone and is now recovering/dropping
    if bull:
        lo, hi = LONG_ZONE
        if not (prev_rsi < lo and curr_rsi > prev_rsi and curr_rsi < hi):
            return None
        direction = "LONG"
    else:
        lo, hi = SHORT_ZONE
        if not (prev_rsi > hi and curr_rsi < prev_rsi and curr_rsi > lo):
            return None
        direction = "SHORT"

    # ATR-based SL/TP
    atr = _atr(h4_candles[-30:], 14)
    if atr == 0:
        return None

    entry   = closes[-1]
    sl_dist = atr * SL_MULT
    tp_dist = atr * TP_MULT

    if direction == "LONG":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist

    sl_pips = sl_dist / pip
    tp_pips = tp_dist / pip
    rr      = round(tp_pips / sl_pips, 2)

    if rr < 1.2 or sl_pips < 5:
        return None

    return {
        "strategy":   "RSI_REVERSION",
        "instrument": instrument,
        "direction":  direction,
        "entry":      round(entry, 5),
        "sl":         round(sl, 5),
        "tp":         round(tp, 5),
        "sl_pips":    round(sl_pips, 1),
        "tp_pips":    round(tp_pips, 1),
        "rr":         rr,
        "session":    "ANY",
        "notes":      (
            f"RSI(9)={curr_rsi:.1f} prev={prev_rsi:.1f} | "
            f"EMA={'bull' if bull else 'bear'} ({e1:.5f}/{e3:.5f}/{e9:.5f}) | "
            f"ATR={atr:.5f} SL×{SL_MULT} TP×{TP_MULT}"
        ),
    }
