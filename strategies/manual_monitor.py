"""
Manual Signal Monitor — tracks externally-sourced trade calls (e.g. a
signal-provider Telegram post) against live price. Does not generate
signals itself; only watches ones registered via db.add_manual_signal().

Lifecycle per signal:
  PENDING → price trades into [entry_low, entry_high] → FILLED
  FILLED  → price reaches SL  → CLOSED (event "SL")
          → price reaches TP1/TP2/TP3 in order → each fires once;
            TP3 (or SL) closes the signal, TP1/TP2 just mark progress
            since the position is assumed to run with partial closes.
"""
from datetime import datetime, timezone

from oanda import get_current_price
from db import get_manual_signals, update_manual_signal


def check_signals(instrument: str) -> list:
    """Check open manual signals for `instrument` against current price.
    Returns a list of event dicts for anything that just happened."""
    price_data = get_current_price(instrument)
    if not price_data:
        return []
    price = price_data["mid"]
    now = datetime.now(timezone.utc)
    events = []

    for sig in get_manual_signals(instrument=instrument, open_only=True):
        long = sig["direction"] == "LONG"

        if sig["status"] == "PENDING":
            lo, hi = sorted([sig["entry_low"], sig["entry_high"]])
            if lo <= price <= hi:
                update_manual_signal(sig["id"], status="FILLED", filled_at=now)
                sig = {**sig, "status": "FILLED"}
                events.append({"event": "FILLED", "price": price, "signal": sig})
            else:
                continue

        if sig["status"] != "FILLED":
            continue

        hit_sl = (price <= sig["sl"]) if long else (price >= sig["sl"])
        if hit_sl:
            update_manual_signal(sig["id"], status="CLOSED", sl_hit_at=now)
            events.append({"event": "SL", "price": price, "signal": sig})
            continue

        if sig["tp3"] is not None and not sig["tp3_hit_at"]:
            hit = (price >= sig["tp3"]) if long else (price <= sig["tp3"])
            if hit:
                update_manual_signal(sig["id"], status="CLOSED", tp3_hit_at=now)
                events.append({"event": "TP3", "price": price, "signal": sig})
                continue

        if sig["tp2"] is not None and not sig["tp2_hit_at"]:
            hit = (price >= sig["tp2"]) if long else (price <= sig["tp2"])
            if hit:
                update_manual_signal(sig["id"], tp2_hit_at=now)
                events.append({"event": "TP2", "price": price, "signal": sig})

        tp1_target = sig["tp1_low"]
        if tp1_target is not None and not sig["tp1_hit_at"]:
            hit = (price >= tp1_target) if long else (price <= tp1_target)
            if hit:
                update_manual_signal(sig["id"], tp1_hit_at=now)
                events.append({"event": "TP1", "price": price, "signal": sig})

    return events
