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

        c.execute("""
            CREATE TABLE IF NOT EXISTS manual_signals (
                id          SERIAL PRIMARY KEY,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                source      TEXT,
                instrument  TEXT NOT NULL,
                direction   TEXT NOT NULL,
                entry_low   DOUBLE PRECISION NOT NULL,
                entry_high  DOUBLE PRECISION NOT NULL,
                sl          DOUBLE PRECISION NOT NULL,
                tp1_low     DOUBLE PRECISION,
                tp1_high    DOUBLE PRECISION,
                tp2         DOUBLE PRECISION,
                tp3         DOUBLE PRECISION,
                notes       TEXT,
                status      TEXT DEFAULT 'PENDING',
                filled_at   TIMESTAMPTZ,
                tp1_hit_at  TIMESTAMPTZ,
                tp2_hit_at  TIMESTAMPTZ,
                tp3_hit_at  TIMESTAMPTZ,
                sl_hit_at   TIMESTAMPTZ
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_manual_signals_open ON manual_signals (instrument, status)")
        print("[DB] Tables ready")


def manual_signal_exists(instrument: str, entry_low: float, entry_high: float, sl: float) -> bool:
    """Dedup guard so re-running the seed on every deploy doesn't insert duplicates."""
    with conn() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT 1 FROM manual_signals
            WHERE instrument = %s AND entry_low = %s AND entry_high = %s AND sl = %s
            LIMIT 1
        """, (instrument, entry_low, entry_high, sl))
        return cur.fetchone() is not None


def add_manual_signal(instrument: str, direction: str, entry_low: float, entry_high: float, sl: float,
                       tp1_low: float = None, tp1_high: float = None, tp2: float = None, tp3: float = None,
                       source: str = "manual", notes: str = "") -> int:
    with conn() as c:
        cur = c.execute("""
            INSERT INTO manual_signals
              (source, instrument, direction, entry_low, entry_high, sl, tp1_low, tp1_high, tp2, tp3, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (source, instrument, direction, entry_low, entry_high, sl, tp1_low, tp1_high, tp2, tp3, notes))
        return cur.fetchone()[0]


def get_manual_signals(instrument: str = None, open_only: bool = True) -> list:
    with conn() as c:
        cur = c.cursor(row_factory=dict_row)
        query = "SELECT * FROM manual_signals WHERE 1=1"
        params = []
        if instrument:
            query += " AND instrument = %s"
            params.append(instrument)
        if open_only:
            query += " AND status != 'CLOSED'"
        query += " ORDER BY created_at DESC"
        cur.execute(query, params)
        return cur.fetchall()


def update_manual_signal(signal_id: int, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE manual_signals SET {set_clause} WHERE id = %s", (*fields.values(), signal_id))


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
            SELECT time, open, high, low, close, volume FROM (
                SELECT time, open, high, low, close, volume
                FROM candles
                WHERE instrument = %s AND granularity = %s
                ORDER BY time DESC
                LIMIT %s
            ) sub ORDER BY time ASC
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


def signal_exists_today(strategy: str, instrument: str, direction: str = "") -> bool:
    """Return True if a signal for this strategy+instrument (optionally direction) was already logged today (UTC)."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    with conn() as c:
        cur = c.cursor()
        if direction:
            cur.execute("""
                SELECT 1 FROM paper_signals
                WHERE strategy = %s AND instrument = %s AND direction = %s
                  AND created_at >= %s::date
                LIMIT 1
            """, (strategy, instrument, direction, str(today)))
        else:
            cur.execute("""
                SELECT 1 FROM paper_signals
                WHERE strategy = %s AND instrument = %s
                  AND created_at >= %s::date
                LIMIT 1
            """, (strategy, instrument, str(today)))
        return cur.fetchone() is not None


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
            created_aware = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            age = (dt.now(timezone.utc) - created_aware).total_seconds()
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
