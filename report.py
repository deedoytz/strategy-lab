"""
Weekly performance report — queries paper_signals table and builds summary.
"""
from datetime import datetime, timezone, timedelta
from db import conn
import psycopg2.extras


def build_report(days: int = 7) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    with conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT strategy, instrument, direction, rr, outcome, pips, created_at
            FROM paper_signals
            WHERE created_at >= %s AND resolved = TRUE
            ORDER BY strategy, created_at
        """, (since,))
        rows = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) AS n FROM paper_signals WHERE resolved = FALSE AND created_at >= %s", (since,))
        pending = cur.fetchone()["n"]

    if not rows:
        return f"📋 *Strategy Lab — Weekly Report*\nNo resolved signals in the last {days} days.\nPending: {pending}"

    # Group by strategy
    from collections import defaultdict
    by_strategy = defaultdict(list)
    for r in rows:
        by_strategy[r["strategy"]].append(r)

    label_map = {"ORB": "🟦 London ORB", "TREND": "🟩 EMA Trend", "RSI_REVERSION": "🟧 RSI Reversion", "PAIRS": "🟪 Pairs Trade"}
    lines = [f"📋 *Strategy Lab — {days}-Day Report*\n_{since.strftime('%b %d')} → {datetime.now(timezone.utc).strftime('%b %d, %Y')}_\n"]

    grand_total = grand_wins = grand_pips = 0

    for strategy, signals in sorted(by_strategy.items()):
        total    = len(signals)
        wins     = sum(1 for s in signals if s["outcome"] == "TP")
        losses   = sum(1 for s in signals if s["outcome"] == "SL")
        expired  = sum(1 for s in signals if s["outcome"] == "EXPIRED")
        win_rate = round(wins / total * 100) if total else 0
        total_pips = sum(s["pips"] or 0 for s in signals)
        avg_rr   = round(sum(s["rr"] or 0 for s in signals) / total, 2) if total else 0

        verdict = "🟢 Edge" if (win_rate >= 50 and total >= 5) else ("🔴 No edge" if total >= 5 else "⏳ More data")

        label = label_map.get(strategy, strategy)
        lines.append(
            f"{label}\n"
            f"  Signals: {total} | W: {wins} L: {losses} Exp: {expired}\n"
            f"  Win rate: {win_rate}% | Pips: {total_pips:+.1f} | Avg RR: {avg_rr}\n"
            f"  Verdict: {verdict}"
        )

        # Per instrument breakdown
        by_inst = defaultdict(list)
        for s in signals:
            by_inst[s["instrument"]].append(s)
        for inst, sigs in sorted(by_inst.items()):
            iw = sum(1 for s in sigs if s["outcome"] == "TP")
            ip = sum(s["pips"] or 0 for s in sigs)
            lines.append(f"    {inst}: {len(sigs)} trades | {iw}W | {ip:+.1f}p")

        lines.append("")
        grand_total += total
        grand_wins  += wins
        grand_pips  += total_pips

    grand_wr = round(grand_wins / grand_total * 100) if grand_total else 0
    lines.append(
        f"─────────────────────\n"
        f"*All strategies:* {grand_total} signals | {grand_wr}% WR | {grand_pips:+.1f} pips\n"
        f"*Pending (unresolved):* {pending}"
    )

    return "\n".join(lines)


def build_daily_report() -> str:
    from collections import defaultdict
    from datetime import timezone, timedelta, datetime as dt

    today = dt.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    since = dt.combine(yesterday, dt.min.time(), tzinfo=timezone.utc)
    until = dt.combine(today,     dt.min.time(), tzinfo=timezone.utc)

    with conn() as c:
        cur = c.execute("""
            SELECT strategy, instrument, direction, rr, sl_pips, tp_pips, outcome, pips, created_at, resolved
            FROM paper_signals
            WHERE created_at >= %s AND created_at < %s
            ORDER BY created_at
        """, (since, until), row_factory=dict_row)
        rows = cur.fetchall()

    date_label = yesterday.strftime("%A, %B %d, %Y")
    label_map  = {"ORB": "🟦 ORB", "TREND": "🟩 Trend", "RSI_REVERSION": "🟧 RSI", "PAIRS": "🟪 Pairs"}

    if not rows:
        return (
            f"📋 *Strategy Lab — Daily Report*\n"
            f"_{date_label}_\n\n"
            f"No signals generated yesterday."
        )

    lines = [f"📋 *Strategy Lab — Daily Report*\n_{date_label}_\n"]

    by_strategy = defaultdict(list)
    for r in rows:
        by_strategy[r["strategy"]].append(r)

    total_signals = len(rows)
    total_resolved = sum(1 for r in rows if r["resolved"])
    total_wins  = sum(1 for r in rows if r["outcome"] == "TP")
    total_pips  = sum(r["pips"] or 0 for r in rows if r["resolved"])

    for strategy, sigs in sorted(by_strategy.items()):
        label    = label_map.get(strategy, strategy)
        resolved = [s for s in sigs if s["resolved"]]
        wins     = sum(1 for s in resolved if s["outcome"] == "TP")
        losses   = sum(1 for s in resolved if s["outcome"] == "SL")
        pending  = len(sigs) - len(resolved)
        pips     = sum(s["pips"] or 0 for s in resolved)

        lines.append(f"{label} — {len(sigs)} signal(s)")
        for s in sigs:
            inst      = s["instrument"]
            direction = s["direction"]
            outcome   = s["outcome"] or "⏳ pending"
            pips_str  = f"{s['pips']:+.1f}p" if s["pips"] is not None else ""
            emoji     = "✅" if s["outcome"] == "TP" else "❌" if s["outcome"] == "SL" else "⏳"
            lines.append(f"  {emoji} {inst} {direction} | RR {s['rr']} | {outcome} {pips_str}")

        if resolved:
            lines.append(f"  W: {wins} L: {losses} | Net: {pips:+.1f} pips")
        if pending:
            lines.append(f"  ⏳ {pending} still resolving")
        lines.append("")

    lines.append(
        f"─────────────────────\n"
        f"*Total:* {total_signals} signals | {total_resolved} resolved | "
        f"{total_wins}W | Net: {total_pips:+.1f} pips"
    )

    return "\n".join(lines)


if __name__ == "__main__":
    print(build_report())
