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
    paper = db.execute(
        """SELECT
        SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
        SUM(CASE WHEN status IN ('closed','settled') THEN 1 ELSE 0 END),
        COALESCE(SUM(CASE WHEN status IN ('closed','settled') THEN realized_pnl ELSE 0 END),0)
        FROM paper_trades_v2"""
    ).fetchone()
    fast = db.execute(
        """SELECT
        SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
        SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END),
        COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0)
        FROM fast_paper_trades"""
    ).fetchone()
    ai_v2 = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE status='ok'"
    ).fetchone()
    total_realized = float(realized[1]) + float(paper[2] or 0) + float(fast[2] or 0)
    lines = [
        "🧪 Opportunity Lab — FAKE MONEY",
        f"virtual cash: ${cash:.2f}",
        f"open: funding {len(open_rows)} | prediction {paper[0] or 0} | fast {fast[0] or 0}",
        f"settled/closed: {realized[0] + (paper[1] or 0) + (fast[1] or 0)} | realized: ${total_realized:+.3f}",
        f"AI usage: ${float(ai[1]) + float(ai_v2[1]):.4f} ({ai[0] + ai_v2[0]} calls)",
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
    predictions = db.execute(
        """SELECT strategy,title,edge_fraction,state FROM opportunities_v2
        WHERE ts>? ORDER BY edge_fraction DESC LIMIT 5""",
        (time.time() - hours * 3600,),
    ).fetchall()
    if not rows and not predictions:
        return "🧪 No candidates in the last 24h. This is valid evidence, not an error."
    lines = ["🧪 Top fake-money candidates (24h)"]
    for row in rows:
        decision = row["ai_decision"] or row["state"]
        lines.append(
            f"• {row['coin']} score {row['score']:.0f}, projected "
            f"${row['projected_net_usd']:+.3f}, {decision}"
        )
    for row in predictions:
        lines.append(
            f"• {row['strategy']} {row['edge_fraction']:.1%}, {row['state']}: {row['title'][:90]}"
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
    daily_v2 = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE status='ok' AND ts>?",
        (day,),
    ).fetchone()
    weekly_v2 = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE status='ok' AND ts>?",
        (week,),
    ).fetchone()
    return (
        "💵 Opportunity Lab AI cost\n"
        f"24h: ${float(daily[1]) + float(daily_v2[1]):.4f} ({daily[0] + daily_v2[0]} calls)\n"
        f"7d: ${float(weekly[1]) + float(weekly_v2[1]):.4f} ({weekly[0] + weekly_v2[0]} calls)\n"
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
    prediction_observations = db.execute(
        "SELECT COUNT(*) FROM prediction_quotes WHERE ts>?", (day,)
    ).fetchone()[0]
    opportunities = db.execute(
        "SELECT COUNT(*),COALESCE(MAX(edge_fraction),0) FROM opportunities_v2 WHERE ts>?",
        (day,),
    ).fetchone()
    fast_samples = db.execute(
        "SELECT COUNT(*) FROM fast_market_samples WHERE ts>?", (day,)
    ).fetchone()[0]
    return "\n".join(
        [
            "📋 Opportunity Lab daily — FAKE MONEY",
            f"observations: funding {observations} | prediction {prediction_observations} | fast {fast_samples}",
            f"candidates: funding {candidates[0]} | structural/news {opportunities[0]}",
            f"best funding rank: {candidates[1]:.0f}/100 | best market edge: {opportunities[1]:.1%}",
            f"shadow opened: {opened} | closed: {closed[0]} | PnL: ${closed[1]:+.3f}",
            status_text(db).split("\n", 1)[1],
            candidates_text(db),
        ]
    )[:4000]


def research_text(db) -> str:
    lines = ["🔬 Opportunity Lab research — FAKE MONEY"]
    rows = db.execute(
        """SELECT strategy,COUNT(*) trades,
        COALESCE(SUM(CASE WHEN status IN ('closed','settled') THEN realized_pnl ELSE 0 END),0) pnl
        FROM paper_trades_v2 GROUP BY strategy ORDER BY trades DESC"""
    ).fetchall()
    if rows:
        lines.append("prediction/funding-style strategies:")
        for row in rows:
            lines.append(f"• {row['strategy']}: {row['trades']} trades, ${row['pnl']:+.2f}")

    fast = db.execute(
        """SELECT CASE
          WHEN seconds_left<=5 THEN '0-5s'
          WHEN seconds_left<=15 THEN '6-15s'
          WHEN seconds_left<=30 THEN '16-30s'
          ELSE '31s+'
        END bucket,COUNT(*) trades,
        SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) settled,
        COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0) pnl
        FROM fast_paper_trades GROUP BY bucket
        ORDER BY MIN(seconds_left)"""
    ).fetchall()
    if fast:
        lines.append("BTC fast lane by entry time:")
        for row in fast:
            lines.append(
                f"• {row['bucket']}: {row['trades']} trades/{row['settled'] or 0} settled, ${row['pnl']:+.2f}"
            )

    counts = db.execute(
        """SELECT
        (SELECT COUNT(*) FROM prediction_quotes),
        (SELECT COUNT(*) FROM news_items),
        (SELECT COUNT(*) FROM fast_market_samples),
        (SELECT COUNT(*) FROM opportunities_v2)"""
    ).fetchone()
    lines.append(
        f"evidence rows: quotes {counts[0]} | news {counts[1]} | fast samples {counts[2]} | opportunities {counts[3]}"
    )
    if not rows and not fast:
        lines.append("No settled sample yet; absence of trades is retained as a result.")
    return "\n".join(lines)[:4000]
