"""
PostgreSQL database layer — candles + paper signals + results.
Uses psycopg3 (psycopg) — no libpq system dependency needed.
"""
import os
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)


@contextmanager
def conn():
    c = psycopg.connect(DATABASE_URL)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id          SERIAL PRIMARY KEY,
                instrument  TEXT NOT NULL,
                granularity TEXT NOT NULL,
                time        TIMESTAMPTZ NOT NULL,
                open        DOUBLE PRECISION,
                high        DOUBLE PRECISION,
                low         DOUBLE PRECISION,
                close       DOUBLE PRECISION,
                volume      INTEGER,
                UNIQUE (instrument, granularity, time)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles (instrument, granularity, time DESC)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_signals (
                id          SERIAL PRIMARY KEY,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                strategy    TEXT NOT NULL,
                instrument  TEXT NOT NULL,
                direction   TEXT NOT NULL,
                entry       DOUBLE PRECISION,
                sl          DOUBLE PRECISION,
                tp          DOUBLE PRECISION,
                sl_pips     DOUBLE PRECISION,
                tp_pips     DOUBLE PRECISION,
                rr          DOUBLE PRECISION,
                session     TEXT,
                notes       TEXT,
                outcome     TEXT,
                outcome_at  TIMESTAMPTZ,
                pips        DOUBLE PRECISION,
                resolved    BOOLEAN DEFAULT FALSE
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_signals_strategy ON paper_signals (strategy, created_at DESC)")
        print("[DB] Tables ready")


def insert_candles(instrument: str, granularity: str, candles: list) -> int:
    if not candles:
        return 0
    with conn() as c:
        rows = [(instrument, granularity, r["time"], r["open"], r["high"], r["low"], r["close"], r.get("volume", 0)) for r in candles]
        cur = c.cursor()
        cur.executemany("""
            INSERT INTO candles (instrument, granularity, time, open, high, low, close, volume)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (instrument, granularity, time) DO NOTHING
        """, rows)
        return cur.rowcount if cur.rowcount else 0


def get_candles(instrument: str, granularity: str, limit: int = 200) -> list:
    with conn() as c:
        cur = c.cursor(row_factory=dict_row)
        cur.execute("""
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE instrument = %s AND granularity = %s
            ORDER BY time ASC
            LIMIT %s
        """, (instrument, granularity, limit))
        return cur.fetchall()


def get_candles_range(instrument: str, granularity: str, from_dt, to_dt=None) -> list:
    with conn() as c:
        cur = c.cursor(row_factory=dict_row)
        if to_dt:
            cur.execute("""
                SELECT time, open, high, low, close, volume
                FROM candles
                WHERE instrument = %s AND granularity = %s
                  AND time >= %s AND time <= %s
                ORDER BY time ASC
            """, (instrument, granularity, from_dt, to_dt))
        else:
            cur.execute("""
                SELECT time, open, high, low, close, volume
                FROM candles
                WHERE instrument = %s AND granularity = %s
                  AND time >= %s
                ORDER BY time ASC
            """, (instrument, granularity, from_dt))
        return cur.fetchall()


def log_paper_signal(strategy: str, instrument: str, direction: str,
                     entry: float, sl: float, tp: float,
                     sl_pips: float, tp_pips: float, rr: float,
                     session: str = "", notes: str = "") -> int:
    with conn() as c:
        cur = c.execute("""
            INSERT INTO paper_signals
              (strategy, instrument, direction, entry, sl, tp, sl_pips, tp_pips, rr, session, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (strategy, instrument, direction, entry, sl, tp, sl_pips, tp_pips, rr, session, notes))
        return cur.fetchone()[0]


def resolve_signals() -> int:
    from datetime import timezone, datetime as dt
    with conn() as c:
        cur = c.cursor(row_factory=dict_row)
        cur.execute("""
            SELECT id, instrument, direction, entry, sl, tp, created_at
            FROM paper_signals
            WHERE resolved = FALSE AND created_at < NOW() - INTERVAL '30 minutes'
        """)
        signals = cur.fetchall()

    resolved = 0
    for sig in signals:
        inst      = sig["instrument"]
        direction = sig["direction"]
        entry     = sig["entry"]
        sl        = sig["sl"]
        tp        = sig["tp"]
        created   = sig["created_at"]

        pip_size = 0.1 if "XAU" in inst else 0.01 if "JPY" in inst else 0.0001
        candles  = get_candles_range(inst, "M15", created)

        outcome = None
        outcome_at = None
        pips = None

        for c in candles:
            if c["time"] < created:
                continue
            high = c["high"]
            low  = c["low"]
            if direction == "LONG":
                if low  <= sl: outcome = "SL"; outcome_at = c["time"]; pips = -round(abs(entry - sl) / pip_size, 1); break
                if high >= tp: outcome = "TP"; outcome_at = c["time"]; pips =  round(abs(tp - entry) / pip_size, 1); break
            else:
                if high >= sl: outcome = "SL"; outcome_at = c["time"]; pips = -round(abs(sl - entry) / pip_size, 1); break
                if low  <= tp: outcome = "TP"; outcome_at = c["time"]; pips =  round(abs(entry - tp) / pip_size, 1); break

        if not outcome:
            age = (dt.now(timezone.utc) - created.replace(tzinfo=timezone.utc)).total_seconds()
            if age > 172800:
                outcome    = "EXPIRED"
                outcome_at = dt.now(timezone.utc)
                pips       = 0.0

        if outcome:
            with conn() as c2:
                c2.execute("""
                    UPDATE paper_signals
                    SET outcome = %s, outcome_at = %s, pips = %s, resolved = TRUE
                    WHERE id = %s
                """, (outcome, outcome_at, pips, sig["id"]))
            resolved += 1

    return resolved
