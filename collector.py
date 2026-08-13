"""
Strategy Lab — Data Collector + Paper Trader

Scheduled jobs:
  Every 15 min  → fetch latest candles for all instruments/granularities → store to DB
  08:00 UTC     → run ORB check (London open breakout)
  Every 30 min  → run Trend check (EMA/ATR pullback)
  Every 1H      → run RSI Reversion check
  Every 1H      → resolve paper signals (fill outcomes)
  Every Sunday  → weekly performance report via Telegram
"""

import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import requests

from db import init_db, insert_candles, get_candles, log_paper_signal, resolve_signals, signal_exists_today
from oanda import fetch_candles, INSTRUMENTS, GRANULARITIES
from strategies import orb, trend, rsi_reversion, pairs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"[Telegram] {e}")


# ── Historical backfill ───────────────────────────────────────────────────────

def _backfill_history():
    """Fetch deep history on startup so strategies have enough candles to run."""
    counts = {"M15": 500, "H1": 500, "H4": 500, "D": 500}
    for inst in INSTRUMENTS:
        for gran, count in counts.items():
            try:
                candles = fetch_candles(inst, gran, count=count)
                stored  = insert_candles(inst, gran, candles)
                log.info(f"[Backfill] {inst} {gran} → {stored} new candles (fetched {len(candles)})")
            except Exception as e:
                log.warning(f"[Backfill] {inst} {gran}: {e}")


# ── Candle collection ─────────────────────────────────────────────────────────

def job_collect_candles():
    """Fetch and store latest candles for all instruments and granularities."""
    total = 0
    for inst in INSTRUMENTS:
        for gran in GRANULARITIES:
            try:
                count   = 200 if gran in ("M15", "H1") else 100
                candles = fetch_candles(inst, gran, count=count)
                stored  = insert_candles(inst, gran, candles)
                total  += stored
                log.info(f"[Collect] {inst} {gran} → {stored} new candles")
            except Exception as e:
                log.warning(f"[Collect] Failed {inst} {gran}: {e}")
    log.info(f"[Collect] Done — {total} total new candles stored")


# ── Strategy runners ──────────────────────────────────────────────────────────

def _log_signal(sig: dict):
    """Log a paper signal to the DB and notify via Telegram."""
    try:
        sig_id = log_paper_signal(
            strategy=sig["strategy"], instrument=sig["instrument"],
            direction=sig["direction"], entry=sig["entry"],
            sl=sig["sl"], tp=sig["tp"],
            sl_pips=sig["sl_pips"], tp_pips=sig["tp_pips"],
            rr=sig["rr"], session=sig["session"], notes=sig.get("notes", ""),
        )
        log.info(f"[Signal] #{sig_id} {sig['strategy']} {sig['instrument']} {sig['direction']} RR={sig['rr']}")
        tg(
            f"📋 *Paper Signal #{sig_id}* — {sig['strategy']}\n"
            f"{sig['instrument']} {sig['direction']}\n"
            f"Entry: `{sig['entry']}` | SL: `{sig['sl']}` | TP: `{sig['tp']}`\n"
            f"SL: {sig['sl_pips']}p | TP: {sig['tp_pips']}p | RR: {sig['rr']}\n"
            f"_{sig.get('notes', '')}_"
        )
    except Exception as e:
        log.error(f"[Signal] Failed to log: {e}")


def job_orb():
    """08:00 UTC — London ORB check."""
    log.info("[ORB] Running London open check...")
    for inst in INSTRUMENTS:
        try:
            if signal_exists_today("ORB", inst):
                log.info(f"[ORB] {inst} — already signalled today, skipping")
                continue
            sig = orb.check_signal(inst)
            if sig:
                _log_signal(sig)
            else:
                log.info(f"[ORB] {inst} — no breakout")
        except Exception as e:
            log.warning(f"[ORB] {inst} error: {e}")


def job_trend():
    """Every 30 min — EMA trend + ATR pullback check."""
    log.info("[Trend] Running trend check...")
    for inst in INSTRUMENTS:
        try:
            h4 = get_candles(inst, "H4", limit=250)
            h1 = get_candles(inst, "H1", limit=50)
            if len(h4) < 95:
                log.info(f"[Trend] {inst} — not enough H4 data yet ({len(h4)} candles)")
                continue
            sig = trend.check_signal(inst, h4, h1)
            if sig:
                _log_signal(sig)
        except Exception as e:
            log.warning(f"[Trend] {inst} error: {e}")


def job_rsi():
    """Every 1H — RSI trend-pullback check on 4H candles."""
    log.info("[RSI] Running RSI check...")
    for inst in INSTRUMENTS:
        try:
            h4    = get_candles(inst, "H4", limit=120)
            daily = get_candles(inst, "D",  limit=10)   # unused by new strategy, kept for signature
            if len(h4) < 100:
                log.info(f"[RSI] {inst} — not enough H4 data yet ({len(h4)} candles)")
                continue
            sig = rsi_reversion.check_signal(inst, h4, daily)
            if sig:
                _log_signal(sig)
            else:
                log.info(f"[RSI] {inst} — no pullback signal")
        except Exception as e:
            log.warning(f"[RSI] {inst} error: {e}")


def job_pairs():
    """Every 1H — EURUSD/GBPUSD correlation pairs trade check."""
    log.info("[Pairs] Running pairs correlation check...")
    try:
        eur_h1 = get_candles("EUR_USD", "H1", limit=150)
        gbp_h1 = get_candles("GBP_USD", "H1", limit=150)
        if len(eur_h1) < 100 or len(gbp_h1) < 100:
            log.info(f"[Pairs] Not enough H1 data yet (EUR={len(eur_h1)}, GBP={len(gbp_h1)})")
            return
        signals = pairs.check_signal(eur_h1, gbp_h1)
        for sig in signals:
            _log_signal(sig)
        if not signals:
            log.info("[Pairs] No pairs signal")
    except Exception as e:
        log.warning(f"[Pairs] Error: {e}")


def job_resolve():
    """Every 1H — check if paper signals hit TP or SL."""
    try:
        n = resolve_signals()
        if n > 0:
            log.info(f"[Resolve] {n} signals resolved")
    except Exception as e:
        log.warning(f"[Resolve] Error: {e}")


def job_daily_report():
    """Every day at 05:00 UTC — send yesterday's signal summary to Telegram."""
    try:
        from report import build_daily_report
        msg = build_daily_report()
        tg(msg)
        log.info("[Report] Daily report sent")
    except Exception as e:
        log.warning(f"[Report] Daily report error: {e}")


def job_weekly_report():
    """Every Sunday 20:00 UTC — send full weekly performance summary to Telegram."""
    try:
        from report import build_report
        msg = build_report()
        tg(msg)
        log.info("[Report] Weekly report sent")
    except Exception as e:
        log.warning(f"[Report] Error: {e}")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "strategy-lab"}), 200


@app.route("/report", methods=["GET"])
def report_now():
    """Trigger a report manually."""
    try:
        from report import build_report
        msg = build_report()
        tg(msg)
        return jsonify({"status": "sent", "report": msg}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/run/orb",   methods=["POST"])
def run_orb():   job_orb();   return jsonify({"status": "ok"}), 200

@app.route("/run/trend", methods=["POST"])
def run_trend(): job_trend(); return jsonify({"status": "ok"}), 200

@app.route("/run/rsi",   methods=["POST"])
def run_rsi():   job_rsi();   return jsonify({"status": "ok"}), 200

@app.route("/run/pairs", methods=["POST"])
def run_pairs(): job_pairs(); return jsonify({"status": "ok"}), 200

@app.route("/run/daily", methods=["POST"])
def run_daily(): job_daily_report(); return jsonify({"status": "ok"}), 200


@app.route("/debug", methods=["GET"])
def debug():
    """Show candle counts and run a strategy dry-run."""
    from db import get_candles
    out = {}
    for inst in INSTRUMENTS:
        out[inst] = {}
        for gran in GRANULARITIES:
            rows = get_candles(inst, gran, limit=300)
            out[inst][gran] = len(rows)

    # Try trend on EUR_USD
    try:
        h4 = get_candles("EUR_USD", "H4", limit=250)
        h1 = get_candles("EUR_USD", "H1", limit=50)
        sig = trend.check_signal("EUR_USD", h4, h1) if len(h4) >= 210 else None
        out["trend_EUR_USD"] = str(sig) if sig else ("no signal" if len(h4) >= 210 else f"not enough H4 ({len(h4)})")
    except Exception as e:
        out["trend_EUR_USD"] = f"error: {e}"

    try:
        eur = get_candles("EUR_USD", "H1", limit=150)
        gbp = get_candles("GBP_USD", "H1", limit=150)
        sigs = pairs.check_signal(eur, gbp) if len(eur) >= 100 else []
        out["pairs"] = [str(s) for s in sigs] if sigs else f"no signal (EUR={len(eur)}, GBP={len(gbp)})"
    except Exception as e:
        out["pairs"] = f"error: {e}"

    return jsonify(out), 200


@app.route("/candles/<instrument>/<granularity>", methods=["GET"])
def get_candles_endpoint(instrument, granularity):
    """Query stored candles. ?from=2026-08-12T08:45:00Z&limit=20"""
    from flask import request as req
    from db import get_candles_range, get_candles
    try:
        from_str = req.args.get("from")
        limit    = int(req.args.get("limit", 20))
        if from_str:
            from datetime import datetime, timezone
            from_dt = datetime.fromisoformat(from_str.replace("Z", "+00:00"))
            rows = get_candles_range(instrument, granularity, from_dt)[:limit]
        else:
            rows = get_candles(instrument, granularity, limit)
        return jsonify([{**r, "time": str(r["time"])} for r in rows]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Startup ───────────────────────────────────────────────────────────────────

def start():
    log.info("[Lab] Initialising database...")
    init_db()

    log.info("[Lab] Running historical backfill...")
    _backfill_history()
    log.info("[Lab] Running initial candle collection...")
    job_collect_candles()

    scheduler = BackgroundScheduler(timezone="UTC")

    # Candle collection — every 15 min
    scheduler.add_job(job_collect_candles, "cron", minute="*/15", id="collect")

    # ORB — 08:00 UTC and 08:05 UTC (second chance if price slow to break)
    scheduler.add_job(job_orb, "cron", hour=8, minute=0,  id="orb_00")
    scheduler.add_job(job_orb, "cron", hour=8, minute=15, id="orb_15")
    scheduler.add_job(job_orb, "cron", hour=8, minute=30, id="orb_30")

    # Trend — every 30 min during London + NY (08:00–22:00 UTC)
    scheduler.add_job(job_trend, "cron", hour="8-22", minute="0,30", id="trend")

    # RSI — top of every hour
    scheduler.add_job(job_rsi, "cron", minute=0, id="rsi")

    # Pairs — every hour at :10 (after candle collection at :00 and RSI at :00)
    scheduler.add_job(job_pairs, "cron", minute=10, id="pairs")

    # Resolver — every hour at :05
    scheduler.add_job(job_resolve, "cron", minute=5, id="resolve")

    # Daily report — every day at 05:00 UTC (11 PM MDT = prep for next day)
    scheduler.add_job(job_daily_report, "cron", hour=5, minute=0, id="daily")

    # Weekly report — Sunday 20:00 UTC
    scheduler.add_job(job_weekly_report, "cron", day_of_week="sun", hour=20, minute=0, id="weekly")

    scheduler.start()
    log.info("[Lab] Scheduler running — all jobs registered")

    port = int(os.getenv("PORT", 6001))
    log.info(f"[Lab] Flask on port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    start()
