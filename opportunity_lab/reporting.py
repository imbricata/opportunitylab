from __future__ import annotations

import json
import time

from .config import CFG
from .db import (
    BTC_V8_STRATEGY_ID,
    FAST_EXPERIMENT_VERSION,
    FAST_SHADOW_VERSION,
    fast_experiment_start,
    fast_shadow_balance,
    fast_shadow_start,
    get_setting,
)


def help_text() -> str:
    return "/status — show bot status"


def status_text(db, live_broker=None) -> str:
    """Minimal whole-factory status, safe in a process with no wallet secrets."""
    started = fast_experiment_start(db)
    preflight = get_setting(db, "live_last_preflight", {})
    runtime = get_setting(db, "live_runtime_status", {}) or {}
    active_strategy_id = str(runtime.get("strategy_version_id") or BTC_V8_STRATEGY_ID)
    fast = db.execute(
        """SELECT
        SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END),
        COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0)
        FROM fast_paper_trades WHERE open_ts>=? AND strategy_version_id=?""",
        (started, active_strategy_id),
    ).fetchone()
    budget = float(runtime.get("budget", 0) or 0)
    reason = str(runtime.get("budget_reason") or runtime.get("gate") or "starting")
    runtime_age = time.time() - float(runtime.get("ts", 0) or 0)
    health = get_setting(db, "factory_health_status", {}) or {}
    health_label = str(health.get("status") or "starting").upper()
    friendly = {
        "daily_loss_limit": "daily stop",
        "daily_order_cap": "daily trade cap",
        "total_order_cap": "total trade cap",
        "unreconciled_live_order": "awaiting settlement",
        "drawdown_stop": "drawdown stop",
        "cohort_loss_stop": "cohort loss stop",
        "session_loss_limit": "session stop",
        "preflight_missing": "checking wallet",
        "preflight_stale": "refreshing wallet",
        "preflight_error": "wallet check error",
        "arming_expired": "arming expired",
        "disabled": "disabled",
        "factory_health_pause": "health safeguard",
        "factory_allocation_inactive": "allocation paused",
        "factory_allocation_missing": "allocation missing",
        "strategy_allocation_paused": "allocation paused",
        "live_circuit_breaker": "circuit breaker (paper probation)",
        "strategy_quarantined": "strategy quarantined",
    }
    if runtime_age > CFG.factory_heartbeat_stale_seconds:
        label = "STALE"
    elif reason.startswith("ready:"):
        label = "LIVE"
    else:
        label = f"PAUSED · {friendly.get(reason, reason.replace('_', ' '))}"
    balance = float(preflight.get("balance_usd", 0))
    total, wins = db.execute(
        """SELECT COUNT(*),COALESCE(SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),0)
        FROM fast_live_orders WHERE state NOT IN ('rejected','skipped')
        AND strategy_version_id=?""", (active_strategy_id,)
    ).fetchone()
    pending = db.execute(
        """SELECT COUNT(*) FROM fast_live_orders WHERE state IN
        ('submitting','matched','uncertain','live','delayed','resolved_waiting_redemption')"""
    ).fetchone()[0]
    live_pnl = db.execute(
        """SELECT COALESCE(SUM(realized_pnl),0) FROM fast_live_orders
        WHERE realized_pnl IS NOT NULL AND strategy_version_id=?""",
        (active_strategy_id,),
    ).fetchone()[0]
    if budget > 0:
        risk = f"${budget:.2f} next risk"
    else:
        risk = "no new risk"
    live_pnl = float(live_pnl)
    live_pnl_text = f"+${live_pnl:.2f}" if live_pnl >= 0 else f"-${abs(live_pnl):.2f}"
    all_live = db.execute(
        """SELECT COUNT(*),COALESCE(SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),0),
        COALESCE(SUM(realized_pnl),0) FROM fast_live_orders
        WHERE state NOT IN ('rejected','skipped')"""
    ).fetchone()
    hypothesis = db.execute(
        """SELECT COUNT(*),COALESCE(SUM(CASE WHEN lifecycle IN
        ('forward_paper','backtest') THEN 1 ELSE 0 END),0)
        FROM factory_hypotheses"""
    ).fetchone()
    paper_strategies = db.execute(
        """SELECT COUNT(*) FROM factory_strategy_versions
        WHERE lifecycle IN ('forward_paper','backtest')"""
    ).fetchone()[0]
    automation = db.execute(
        """SELECT COUNT(*) specs,
        COALESCE(SUM(state='forward_paper'),0) paper,
        COALESCE(SUM(state='live_eligible'),0) eligible,
        COALESCE(SUM(state='quarantined'),0) quarantined,
        COALESCE(SUM(state='blocked_adapter'),0) blocked
        FROM factory_strategy_specs"""
    ).fetchone()
    forward_trades = db.execute(
        "SELECT COUNT(*) FROM factory_variant_trades WHERE mode='forward'"
    ).fetchone()[0]
    day = int(time.time() // 86400) * 86400
    ai_spend = float(db.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE ts>=?",
        (day,),
    ).fetchone()[0])
    capital = db.execute(
        """SELECT estimated_external_net,pending_orders FROM factory_capital_snapshots
        ORDER BY ts DESC LIMIT 1"""
    ).fetchone()
    contribution = (
        f" · est. external {float(capital[0]):+.2f}"
        if capital is not None and not int(capital[1]) else ""
    )
    strategy_equity = get_setting(db, "live_strategy_equity", {}) or {}
    equity = float(strategy_equity.get("equity_usd", 0) or 0)
    peak = float(strategy_equity.get("high_water_usd", 0) or 0)
    equity_drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0
    live_shadow = db.execute(
        """SELECT COUNT(*),COALESCE(SUM(outcome IN
        ('actual_submitting','affordability_retry_submitting')),0)
        FROM fast_live_shadow_decisions"""
    ).fetchone()
    cohort = db.execute(
        """SELECT * FROM live_risk_cohorts WHERE strategy_version_id=?
        ORDER BY created_ts DESC LIMIT 1""",
        (active_strategy_id,),
    ).fetchone()
    lines = [
        f"Factory · {health_label}",
        f"${balance:.2f} wallet{contribution} · AI ${ai_spend:.3f}/${CFG.max_ai_usd_per_day:.2f}",
        f"BTC {active_strategy_id} · {label} · {risk}",
        f"Strategy equity ${equity:.2f} · peak ${peak:.2f} · drawdown {equity_drawdown:.1%}",
        f"Live {wins}/{total} wins/trades · PnL {live_pnl_text} · {pending} pending",
        f"Exact live-shadow · {live_shadow[0]} decisions/{live_shadow[1]} submit-eligible",
        f"All BTC live {all_live[1]}/{all_live[0]} · PnL {float(all_live[2]):+.2f}",
        f"Paper {float(fast[1] or 0):+.2f} · {fast[0] or 0} settled",
        f"Research · {paper_strategies} paper templates · {hypothesis[0]} hypotheses",
        f"Automation · {automation['paper']} challengers · {forward_trades} forward trades · "
        f"{automation['eligible']} eligible/{automation['quarantined']} quarantined/"
        f"{automation['blocked']} blocked",
    ]
    if cohort is not None:
        from .live import risk_cohort_snapshot

        try:
            cohort_evidence = json.loads(cohort["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            cohort_evidence = {}
        trailing_loss_cap = (
            False
            if cohort_evidence.get("loss_basis") == "net_from_cohort_start"
            else CFG.live_cohort_trailing_loss_cap
        )
        cohort_state = risk_cohort_snapshot(
            db, cohort, trailing_loss_cap=trailing_loss_cap
        )
        lines.insert(
            4,
            f"Live cohort #{cohort['id']} {cohort['state']} · "
            f"PnL {cohort_state['realized_pnl_usd']:+.2f} · "
            f"${cohort_state['remaining_loss_usd']:.2f} room to "
            f"-${cohort_state['loss_cap_usd']:.2f} net stop",
        )
    if CFG.fast_v7_shadow_enabled:
        shadow_started = fast_shadow_start(db)
        shadow = db.execute(
            """SELECT SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END),
            COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0)
            FROM fast_shadow_trades WHERE strategy_version=? AND open_ts>=?""",
            (FAST_SHADOW_VERSION, shadow_started),
        ).fetchone()
        shadow_cash = fast_shadow_balance(db, CFG.fast_starting_cash_usd)
        lines.append(
            f"V7 shadow ${shadow_cash:.2f} · PnL {float(shadow[2] or 0):+.2f} · "
            f"{shadow[0] or 0} open/{shadow[1] or 0} settled"
        )
    return "\n".join(lines)


def candidates_text(db, hours: int = 24) -> str:
    return "The broad candidate scanner is tabled. Use /research for BTC oracle evidence."


def cost_text(db) -> str:
    day = time.time() - 86400
    week = time.time() - 7 * 86400
    daily_v2 = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE ts>?",
        (day,),
    ).fetchone()
    weekly_v2 = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE ts>?",
        (week,),
    ).fetchone()
    return (
        "💵 Opportunity Lab AI cost\n"
        f"24h: ${float(daily_v2[1]):.4f} ({daily_v2[0]} calls/reservations)\n"
        f"7d: ${float(weekly_v2[1]):.4f} ({weekly_v2[0]} calls/reservations)\n"
        "Market collection and local scoring use no AI tokens."
    )


def digest_text(db) -> str:
    day = max(time.time() - 86400, fast_experiment_start(db))
    fast_samples = db.execute(
        "SELECT COUNT(*) FROM fast_market_samples WHERE ts>?", (day,)
    ).fetchone()[0]
    attempts = db.execute(
        "SELECT COUNT(*) FROM fast_execution_attempts WHERE ts>?", (day,)
    ).fetchone()[0]
    trades = db.execute(
        """SELECT COUNT(*),COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0)
        FROM fast_paper_trades WHERE open_ts>?""", (day,)
    ).fetchone()
    return "\n".join(
        [
            "📋 BTC Oracle Lab daily",
            f"24h evidence: {fast_samples} final-window samples | {attempts} delayed attempts",
            f"paper trades: {trades[0]} | settled PnL: ${trades[1]:+.3f}",
            status_text(db).split("\n", 1)[1],
            research_text(db),
        ]
    )[:4000]


def research_text(db) -> str:
    started = fast_experiment_start(db)
    lines = [
        "🔬 BTC Oracle Lab research",
        f"cohort: {FAST_EXPERIMENT_VERSION} since "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(started))}",
    ]
    fast = db.execute(
        """SELECT CASE
          WHEN seconds_left<=5 THEN '0-5s'
          WHEN seconds_left<=15 THEN '6-15s'
          WHEN seconds_left<=30 THEN '16-30s'
          ELSE '31s+'
        END bucket,COUNT(*) trades,
        SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) settled,
        COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0) pnl
        FROM fast_paper_trades WHERE open_ts>=? GROUP BY bucket
        ORDER BY MIN(seconds_left)""", (started,)
    ).fetchall()
    if fast:
        lines.append("BTC fast lane by entry time:")
        for row in fast:
            lines.append(
                f"• {row['bucket']}: {row['trades']} trades/{row['settled'] or 0} settled, ${row['pnl']:+.2f}"
            )

    attempts = db.execute(
        """SELECT outcome,COUNT(*) count FROM fast_execution_attempts
        WHERE ts>=? GROUP BY outcome ORDER BY count DESC""", (started,)
    ).fetchall()
    if attempts:
        lines.append("delayed execution outcomes:")
        lines.append("• " + " | ".join(f"{row['outcome']} {row['count']}" for row in attempts))

    screens = db.execute(
        """SELECT reason,COUNT(*) count FROM fast_screen_decisions
        WHERE ts>=? GROUP BY reason ORDER BY count DESC""", (started,)
    ).fetchall()
    if screens:
        lines.append("entry-window screen outcomes:")
        lines.append("• " + " | ".join(f"{row['reason']} {row['count']}" for row in screens))

    validation = db.execute(
        """SELECT COUNT(*) total,
        COALESCE(SUM(CASE WHEN result_matches=1 THEN 1 ELSE 0 END),0) matches,
        COALESCE(SUM(CASE WHEN result_matches=0 THEN 1 ELSE 0 END),0) mismatches
        FROM fast_window_results WHERE end_ts>=?""", (started,)
    ).fetchone()
    if validation[0]:
        lines.append(
            f"oracle reconstruction: {validation['matches']}/{validation['total']} match venue "
            f"({validation['mismatches']} mismatches)"
        )

    feed_lag = db.execute(
        """SELECT source,COUNT(*) samples,
        AVG(MAX(0,received_ts-source_ts))*1000 avg_ms
        FROM fast_price_ticks WHERE source_ts>? GROUP BY source ORDER BY source""",
        (max(time.time() - 86400, started),),
    ).fetchall()
    if feed_lag:
        lines.append("24h feed receive lag:")
        lines.append("• " + " | ".join(
            f"{row['source']} {row['avg_ms']:.0f}ms/{row['samples']}" for row in feed_lag
        ))

    counts = db.execute(
        """SELECT COUNT(*) samples,COUNT(DISTINCT slug) windows
        FROM fast_market_samples WHERE ts>=?""", (started,)
    ).fetchone()
    lines.append(f"evidence: {counts['samples']} samples across {counts['windows']} windows")
    if not fast and not attempts:
        lines.append("No settled sample yet; absence of trades is retained as a result.")
    if CFG.fast_v7_shadow_enabled:
        shadow_started = fast_shadow_start(db)
        shadow = db.execute(
            """SELECT COUNT(*) trades,
            SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) settled,
            COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0) pnl
            FROM fast_shadow_trades WHERE strategy_version=? AND open_ts>=?""",
            (FAST_SHADOW_VERSION, shadow_started),
        ).fetchone()
        lines.append(
            f"v7 shadow: {shadow['trades']} trades/{shadow['settled'] or 0} settled, "
            f"${shadow['pnl']:+.2f}"
        )
    return "\n".join(lines)[:4000]
