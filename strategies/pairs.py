"""
Statistical Pairs Trade — EURUSD / GBPUSD Correlation

Setup:
  - EURUSD and GBPUSD are historically ~0.85 correlated
  - Compute a z-score of the spread (EURUSD - GBPUSD * hedge_ratio)
  - When z-score > +2.0: spread is too wide → SHORT EURUSD, LONG GBPUSD (expect convergence)
  - When z-score < -2.0: spread is too narrow → LONG EURUSD, SHORT GBPUSD (expect convergence)
  - Exit when z-score reverts to 0 (mean)
  - SL: z-score hits ±3.0 (spread diverges further — trade is wrong)

NOTE: Paper mode only — logs TWO signals (one per leg) so each leg is tracked independently.
In live mode this would need simultaneous execution and separate sizing.
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

    hedge = _hedge_ratio(eur_closes, gbp_closes, lookback=60)

    # Spread = EURUSD - hedge * GBPUSD
    spread = [eur_closes[i] - hedge * gbp_closes[i] for i in range(n)]
    zscores = _zscore(spread, lookback=20)

    if not zscores:
        return []

    current_z  = zscores[-1]
    prev_z     = zscores[-2] if len(zscores) >= 2 else current_z

    entry_threshold = 2.0
    sl_threshold    = 3.0

    # Only signal on fresh crosses of ±2.0 (not if already past ±3.0)
    if abs(current_z) > sl_threshold:
        return []

    if not (abs(current_z) >= entry_threshold and abs(prev_z) < entry_threshold):
        return []  # Not a fresh cross

    eur_price = eur_closes[-1]
    gbp_price = gbp_closes[-1]
    eur_pip   = _pip("EUR_USD")
    gbp_pip   = _pip("GBP_USD")

    # Estimate SL/TP in pips based on spread z-score thresholds
    # SL at z=3.0, TP at z=0 — approximate pip distances from spread volatility
    spread_std = abs(spread[-1] / current_z) if current_z != 0 else 0.0001
    sl_spread  = spread_std * sl_threshold
    tp_spread  = spread_std * abs(current_z)  # distance to mean

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
