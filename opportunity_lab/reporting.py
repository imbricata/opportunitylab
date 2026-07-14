from __future__ import annotations

import json
import time

from .db import balance


def status_text(db) -> str:
    cash = balance(db)
    open_rows = db.execute(
        "SELECT coin,open_ts,spot_cost,perp_margin,funding_pnl FROM positions WHERE status='open'"
    ).fetchall()
    realized = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(realized_pnl),0) FROM positions WHERE status='closed'"
    ).fetchone()
    ai = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_usage WHERE status='ok'"
    ).fetchone()
    lines = [
        "🧪 Opportunity Lab — FAKE MONEY",
        f"virtual cash: ${cash:.2f}",
        f"open: {len(open_rows)} | closed: {realized[0]} | realized: ${realized[1]:+.3f}",
        f"AI usage: ${ai[1]:.4f} ({ai[0]} calls)",
    ]
    for row in open_rows:
        age = (time.time() - row["open_ts"]) / 3600
        allocated = row["spot_cost"] + row["perp_margin"]
        lines.append(
            f"• {row['coin']}: ${allocated:.2f} allocated, {age:.1f}h, "
            f"funding ${row['funding_pnl']:+.3f}"
        )
    return "\n".join(lines)


def candidates_text(db, hours: int = 24) -> str:
    rows = db.execute(
        """SELECT coin,score,projected_net_usd,state,ai_decision,reason
        FROM candidates WHERE ts>? ORDER BY score DESC LIMIT 5""",
        (time.time() - hours * 3600,),
    ).fetchall()
    if not rows:
        return "🧪 No candidates in the last 24h. This is valid evidence, not an error."
    lines = ["🧪 Top fake-money candidates (24h)"]
    for row in rows:
        decision = row["ai_decision"] or row["state"]
        lines.append(
            f"• {row['coin']} score {row['score']:.0f}, projected "
            f"${row['projected_net_usd']:+.3f}, {decision}"
        )
    return "\n".join(lines)


def cost_text(db) -> str:
    day = time.time() - 86400
    week = time.time() - 7 * 86400
    daily = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_usage WHERE status='ok' AND ts>?",
        (day,),
    ).fetchone()
    weekly = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_usage WHERE status='ok' AND ts>?",
        (week,),
    ).fetchone()
    return (
        "💵 Opportunity Lab AI cost\n"
        f"24h: ${daily[1]:.4f} ({daily[0]} calls)\n"
        f"7d: ${weekly[1]:.4f} ({weekly[0]} calls)\n"
        "Market collection and local scoring use no AI tokens."
    )


def digest_text(db) -> str:
    day = time.time() - 86400
    observations = db.execute(
        "SELECT COUNT(*) FROM market_snapshots WHERE ts>?", (day,)
    ).fetchone()[0]
    candidates = db.execute(
        "SELECT COUNT(*),COALESCE(MAX(score),0) FROM candidates WHERE ts>?", (day,)
    ).fetchone()
    opened = db.execute(
        "SELECT COUNT(*) FROM positions WHERE open_ts>?", (day,)
    ).fetchone()[0]
    closed = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(realized_pnl),0) FROM positions WHERE close_ts>?",
        (day,),
    ).fetchone()
    return "\n".join(
        [
            "📋 Opportunity Lab daily — FAKE MONEY",
            f"observations: {observations}",
            f"candidates: {candidates[0]} | best rank: {candidates[1]:.0f}/100",
            f"shadow opened: {opened} | closed: {closed[0]} | PnL: ${closed[1]:+.3f}",
            status_text(db).split("\n", 1)[1],
            candidates_text(db),
        ]
    )[:4000]

