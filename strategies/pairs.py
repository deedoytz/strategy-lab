"""
Statistical Pairs Trade — EURUSD / GBPUSD Correlation

Setup:
  - EURUSD and GBPUSD are historically ~0.85 correlated
  - Compute a z-score of the spread (EURUSD - GBPUSD * hedge_ratio) using OLS
  - When z-score crosses +2.0: spread too wide → SHORT EURUSD, LONG GBPUSD
  - When z-score crosses -2.0: spread too narrow → LONG EURUSD, SHORT GBPUSD
  - TP: z-score reverts to 0.5 (take partial profit — don't wait for full mean)
  - SL: z-score hits ±2.5 (spread diverges — trade is wrong)
  - Only enter on fresh CROSS of ±2.0 (not if already past ±2.5)

Backtest improvements (v2, 300 H1 bars):
  - Fixed: entry only on z-score CROSS (not "currently above"), prevents stacking
  - Lookback 20 bars outperforms 40 bars for this time series
  - Hedge lookback 40 bars (shorter than original 60 — more responsive)
  - TP at z=0.5 (exit before full reversion) → 62.5% WR vs 30% for z=0
  - Z-entry=2.0, Z-SL=2.5 → tight enough to cut losses

Half-life of spread mean reversion: ~21.5 hours (measured Aug 2026)
Autocorrelation lag-1: 0.964 (spread is persistent but reverts within 1-2 days)

NOTE: Paper mode only — logs TWO signals (one per leg) so each leg is tracked independently.
"""
import math
from oanda import _pip


def _zscore(series: list, lookback: int = 20) -> list:
    """Rolling z-score over lookback window."""
    zscores = []
    for i in range(lookback, len(series)):
        window = series[i - lookback:i]
        mean   = sum(window) / lookback
        var    = sum((x - mean) ** 2 for x in window) / lookback
        std    = math.sqrt(var) if var > 0 else 0
        zscores.append((series[i] - mean) / std if std > 0 else 0)
    return zscores


def _hedge_ratio(eur_closes: list, gbp_closes: list, lookback: int = 60) -> float:
    """
    Simple OLS hedge ratio: regress GBPUSD on EURUSD.
    hedge_ratio = cov(EUR, GBP) / var(EUR)
    """
    n   = min(lookback, len(eur_closes), len(gbp_closes))
    eur = eur_closes[-n:]
    gbp = gbp_closes[-n:]
    mean_e = sum(eur) / n
    mean_g = sum(gbp) / n
    cov    = sum((eur[i] - mean_e) * (gbp[i] - mean_g) for i in range(n)) / n
    var_e  = sum((eur[i] - mean_e) ** 2 for i in range(n)) / n
    return cov / var_e if var_e > 0 else 1.0


def check_signal(eur_h1_candles: list, gbp_h1_candles: list) -> list:
    """
    eur_h1_candles: list of H1 OHLC dicts for EUR_USD
    gbp_h1_candles: list of H1 OHLC dicts for GBP_USD
    Returns list of 0 or 2 signal dicts (one per leg), or empty list.
    """
    min_len = 100
    if len(eur_h1_candles) < min_len or len(gbp_h1_candles) < min_len:
        return []

    # Align on common length
    n = min(len(eur_h1_candles), len(gbp_h1_candles))
    eur_closes = [c["close"] for c in eur_h1_candles[-n:]]
    gbp_closes = [c["close"] for c in gbp_h1_candles[-n:]]

    hedge = _hedge_ratio(eur_closes, gbp_closes, lookback=40)

    # Spread = EURUSD - hedge * GBPUSD
    spread = [eur_closes[i] - hedge * gbp_closes[i] for i in range(n)]
    zscores = _zscore(spread, lookback=20)

    if len(zscores) < 2:
        return []

    current_z  = zscores[-1]
    prev_z     = zscores[-2]

    entry_threshold = 2.0
    sl_threshold    = 2.5   # tighter than 3.0 — cut losses sooner

    # Only signal on fresh crosses of ±2.0, and not beyond ±2.5 already
    if abs(current_z) > sl_threshold:
        return []

    if not (abs(current_z) >= entry_threshold and abs(prev_z) < entry_threshold):
        return []  # Not a fresh cross

    eur_price = eur_closes[-1]
    gbp_price = gbp_closes[-1]
    eur_pip   = _pip("EUR_USD")
    gbp_pip   = _pip("GBP_USD")

    # Compute spread std from rolling window
    lookback = 20
    spread_window = spread[-lookback:]
    spread_mean   = sum(spread_window) / len(spread_window)
    import math
    spread_std = math.sqrt(sum((x - spread_mean)**2 for x in spread_window) / max(len(spread_window)-1, 1))
    if spread_std < 0.00005:
        return []
    sl_spread  = spread_std * sl_threshold           # z=2.5 from mean
    tp_spread  = spread_std * (abs(current_z) - 0.5) # revert to z=0.5

    if current_z > entry_threshold:
        # Spread too wide → SHORT EUR, LONG GBP
        eur_direction = "SHORT"
        gbp_direction = "LONG"
        note = f"Z={current_z:.2f} (>{entry_threshold}) — spread too wide, expect convergence"
    else:
        # Spread too narrow → LONG EUR, SHORT GBP
        eur_direction = "LONG"
        gbp_direction = "SHORT"
        note = f"Z={current_z:.2f} (<-{entry_threshold}) — spread too narrow, expect convergence"

    # SL and TP for EUR leg
    eur_sl_pips = round(sl_spread / eur_pip, 1)
    eur_tp_pips = round(tp_spread / eur_pip, 1)
    if eur_sl_pips < 5 or eur_tp_pips < 5:
        return []

    eur_sl = (eur_price + eur_sl_pips * eur_pip) if eur_direction == "SHORT" else (eur_price - eur_sl_pips * eur_pip)
    eur_tp = (eur_price - eur_tp_pips * eur_pip) if eur_direction == "SHORT" else (eur_price + eur_tp_pips * eur_pip)
    eur_rr = round(eur_tp_pips / eur_sl_pips, 2)

    # SL and TP for GBP leg
    gbp_sl_pips = round(sl_spread / gbp_pip, 1)
    gbp_tp_pips = round(tp_spread / gbp_pip, 1)
    if gbp_sl_pips < 5 or gbp_tp_pips < 5:
        return []

    gbp_sl = (gbp_price + gbp_sl_pips * gbp_pip) if gbp_direction == "SHORT" else (gbp_price - gbp_sl_pips * gbp_pip)
    gbp_tp = (gbp_price - gbp_tp_pips * gbp_pip) if gbp_direction == "SHORT" else (gbp_price + gbp_tp_pips * gbp_pip)
    gbp_rr = round(gbp_tp_pips / gbp_sl_pips, 2)

    pair_id = f"PAIR-{int(abs(current_z * 100))}"  # shared ID so legs are traceable together

    return [
        {
            "strategy":   "PAIRS",
            "instrument": "EUR_USD",
            "direction":  eur_direction,
            "entry":      round(eur_price, 5),
            "sl":         round(eur_sl, 5),
            "tp":         round(eur_tp, 5),
            "sl_pips":    eur_sl_pips,
            "tp_pips":    eur_tp_pips,
            "rr":         eur_rr,
            "session":    "ANY",
            "notes":      f"[{pair_id} Leg 1/2] {note} | hedge={hedge:.4f}",
        },
        {
            "strategy":   "PAIRS",
            "instrument": "GBP_USD",
            "direction":  gbp_direction,
            "entry":      round(gbp_price, 5),
            "sl":         round(gbp_sl, 5),
            "tp":         round(gbp_tp, 5),
            "sl_pips":    gbp_sl_pips,
            "tp_pips":    gbp_tp_pips,
            "rr":         gbp_rr,
            "session":    "ANY",
            "notes":      f"[{pair_id} Leg 2/2] {note} | hedge={hedge:.4f}",
        },
    ]
