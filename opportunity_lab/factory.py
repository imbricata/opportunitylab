from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import urllib.error
from dataclasses import replace
from pathlib import Path

from . import ai, news
from .config import Config, ROOT
from .db import (
    BTC_V6_STRATEGY_ID,
    BTC_V8_STRATEGY_ID,
    FAST_EXPERIMENT_VERSION,
    fast_experiment_start,
    get_setting,
    set_setting,
)


log = logging.getLogger("opportunity_lab.factory")
RELEVANT_TERMS = {
    "arbitrage", "bot", "strategy", "oracle", "settlement", "mispricing",
    "weather", "odds", "sportsbook", "kalshi", "polymarket", "liquidity",
    "fee", "fees", "spread", "hedge", "market maker", "market making",
    "prediction market", "stale", "latency", "fill", "rebate",
}


def _hash(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _file_hash(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if path.exists():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def enqueue_notification(
    db,
    *,
    kind: str,
    message: str,
    severity: str = "info",
    dedupe_key: str | None = None,
    due_ts: float | None = None,
) -> bool:
    key = dedupe_key or _hash({"kind": kind, "message": message})
    cur = db.execute(
        """INSERT OR IGNORE INTO notification_outbox
        (created_ts,due_ts,severity,kind,dedupe_key,message)
        VALUES(?,?,?,?,?,?)""",
        (time.time(), due_ts or time.time(), severity, kind, key, message[:4000]),
    )
    db.commit()
    return bool(cur.rowcount)


def due_notifications(db, limit: int = 10):
    return db.execute(
        """SELECT * FROM notification_outbox
        WHERE sent_ts IS NULL AND due_ts<=? ORDER BY id LIMIT ?""",
        (time.time(), max(1, limit)),
    ).fetchall()


def mark_notification(db, row_id: int, error: Exception | None = None) -> None:
    if error is None:
        db.execute(
            "UPDATE notification_outbox SET sent_ts=?,last_error=NULL WHERE id=?",
            (time.time(), row_id),
        )
    else:
        row = db.execute(
            "SELECT attempts FROM notification_outbox WHERE id=?", (row_id,)
        ).fetchone()
        attempts = int(row[0] if row else 0) + 1
        delay = min(3600, 15 * (2 ** min(attempts, 8)))
        db.execute(
            """UPDATE notification_outbox SET attempts=?,due_ts=?,last_error=?
            WHERE id=?""",
            (attempts, time.time() + delay, repr(error)[:1000], row_id),
        )
    db.commit()


def bootstrap_factory(db, cfg: Config) -> None:
    now = time.time()
    btc_spec = {
        "strategy_key": "btc_5m_terminal",
        "version": FAST_EXPERIMENT_VERSION,
        "venue": "polymarket",
        "entry_window_seconds": [cfg.fast_min_seconds_left, cfg.fast_entry_window_seconds],
        "min_confidence": cfg.fast_v8_min_confidence,
        "max_ask": cfg.fast_max_ask,
        "execution": "fresh CLOB FOK with latency, fee, depth and slippage gates",
        "promotion": "manually promoted before factory bootstrap; evidence retained",
    }
    builtins = [
        (
            BTC_V6_STRATEGY_ID, "btc_5m_terminal", "v6",
            "BTC five-minute terminal V6", "polymarket", "oracle_timing", "retired", 0,
            {
                "model_version": "logit20-cap85-book50-fokdepth-latency1s-bankroll20-v6",
                "status": "retired after live forward evidence; retained for attribution",
            },
        ),
        (
            BTC_V8_STRATEGY_ID, "btc_5m_terminal", FAST_EXPERIMENT_VERSION,
            "BTC five-minute terminal V8", "polymarket", "oracle_timing", "live", 1,
            btc_spec,
        ),
        (
            "binary_complement_v1", "binary_complement", "fee-aware-v1",
            "Binary complement arbitrage", "multi", "cross_contract", "forward_paper", 0,
            {"template": "buy exhaustive YES+NO only below payout after fees"},
        ),
        (
            "exclusive_no_basket_v1", "exclusive_no_basket", "logic-v1",
            "Mutually-exclusive NO basket", "multi", "cross_contract", "forward_paper", 0,
            {"template": "buy NO basket only when exclusivity is explicit"},
        ),
        (
            "directional_implication_v1", "directional_implication", "logic-v1",
            "Threshold implication arbitrage", "multi", "cross_contract", "forward_paper", 0,
            {"template": "trade only machine-verifiable threshold implications"},
        ),
        (
            "cross_venue_equivalent_v1", "cross_venue_equivalent", "audited-v1",
            "Cross-venue contract equivalence", "multi", "cross_venue", "forward_paper", 0,
            {"template": "AI audits wording; deterministic simulator owns fills"},
        ),
        (
            "news_directional_v1", "news_directional", "skeptical-v1",
            "News-to-contract reaction", "multi", "event_reaction", "forward_paper", 0,
            {"template": "paper-only semantic review with exact resolution rules"},
        ),
    ]
    code_hash = _file_hash(
        (
            ROOT / "opportunity_lab" / "fastlane.py",
            ROOT / "opportunity_lab" / "strategies.py",
            ROOT / "opportunity_lab" / "paper.py",
        )
    )
    for strategy_id, key, version, name, venue, family, lifecycle, live_allowed, spec in builtins:
        spec_json = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        db.execute(
            """INSERT INTO factory_strategy_versions
            (strategy_version_id,strategy_key,version,name,venue,family,lifecycle,
             live_allowed,created_ts,updated_ts,code_hash,config_hash,spec_json,notes)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_version_id) DO NOTHING""",
            (
                strategy_id, key, version, name, venue, family, lifecycle, live_allowed,
                now, now, code_hash, _hash(spec), spec_json,
                "Factory V1 bootstrap",
            ),
        )
    db.execute(
        """INSERT INTO factory_allocations
        (strategy_version_id,state,max_risk_fraction,max_order_usd,weight,updated_ts,reason)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(strategy_version_id) DO NOTHING""",
        (
            BTC_V8_STRATEGY_ID, "active", cfg.live_risk_fraction,
            cfg.live_max_order_usd, 1.0, now,
            "Existing positive live strategy; only live allocation in Factory V1",
        ),
    )
    db.commit()


def _metric_row(db, strategy_id: str, mode: str, values: dict) -> None:
    ts = int(time.time() // 300) * 300
    risk = float(values.get("capital_risked", 0) or 0)
    pnl = float(values.get("pnl", 0) or 0)
    db.execute(
        """INSERT OR REPLACE INTO factory_metrics
        (strategy_version_id,mode,ts,trades,wins,open_trades,pnl,capital_risked,roi,metrics_json)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            strategy_id, mode, ts, int(values.get("trades", 0) or 0),
            int(values.get("wins", 0) or 0), int(values.get("open_trades", 0) or 0),
            pnl, risk, pnl / risk if risk else 0.0,
            json.dumps(values, sort_keys=True, separators=(",", ":")),
        ),
    )


def update_strategy_metrics(db, cfg: Config) -> dict:
    bootstrap_factory(db, cfg)
    from .automation import active_btc_spec

    active_strategy_id, _ = active_btc_spec(db, cfg)
    started = fast_experiment_start(db)
    paper = db.execute(
        """SELECT COUNT(*) trades,
        SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
        SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_trades,
        COALESCE(SUM(CASE WHEN status='settled' THEN realized_pnl ELSE 0 END),0) pnl,
        COALESCE(SUM(CASE WHEN status='settled' THEN qty*entry_price+entry_fee ELSE 0 END),0) risk
        FROM fast_paper_trades WHERE open_ts>=? AND strategy_version_id=?""",
        (started, active_strategy_id),
    ).fetchone()
    paper_values = {
        "trades": paper["trades"], "wins": paper["wins"],
        "open_trades": paper["open_trades"], "pnl": paper["pnl"],
        "capital_risked": paper["risk"], "cohort_start": started,
    }
    _metric_row(db, active_strategy_id, "paper", paper_values)
    live = db.execute(
        """SELECT COUNT(*) trades,
        SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
        SUM(CASE WHEN state IN ('submitting','matched','uncertain','live','delayed',
          'resolved_waiting_redemption') THEN 1 ELSE 0 END) open_trades,
        COALESCE(SUM(realized_pnl),0) pnl,
        COALESCE(SUM(COALESCE(actual_cost_usd,risk_budget_usd)),0) risk
        FROM fast_live_orders
        WHERE strategy_version_id=? AND state NOT IN ('rejected','skipped')""",
        (active_strategy_id,),
    ).fetchone()
    live_values = {
        "trades": live["trades"], "wins": live["wins"],
        "open_trades": live["open_trades"], "pnl": live["pnl"],
        "capital_risked": live["risk"],
    }
    _metric_row(db, active_strategy_id, "live", live_values)

    for mode in ("paper", "live"):
        if mode == "paper":
            row = db.execute(
                """SELECT COUNT(*) trades,
                SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_trades,
                COALESCE(SUM(realized_pnl),0) pnl,
                COALESCE(SUM(qty*entry_price+entry_fee),0) risk
                FROM fast_paper_trades WHERE strategy_version_id=?""",
                (BTC_V6_STRATEGY_ID,),
            ).fetchone()
        else:
            row = db.execute(
                """SELECT COUNT(*) trades,
                SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                SUM(CASE WHEN state IN ('submitting','matched','uncertain','live','delayed',
                  'resolved_waiting_redemption') THEN 1 ELSE 0 END) open_trades,
                COALESCE(SUM(realized_pnl),0) pnl,
                COALESCE(SUM(COALESCE(actual_cost_usd,risk_budget_usd)),0) risk
                FROM fast_live_orders WHERE strategy_version_id=?
                AND state NOT IN ('rejected','skipped')""",
                (BTC_V6_STRATEGY_ID,),
            ).fetchone()
        _metric_row(
            db, BTC_V6_STRATEGY_ID, mode,
            {
                "trades": row["trades"], "wins": row["wins"],
                "open_trades": row["open_trades"], "pnl": row["pnl"],
                "capital_risked": row["risk"], "historical": True,
            },
        )

    mapping = {
        "binary_complement": "binary_complement_v1",
        "exclusive_no_basket": "exclusive_no_basket_v1",
        "directional_implication": "directional_implication_v1",
        "cross_venue_equivalent": "cross_venue_equivalent_v1",
        "news_directional": "news_directional_v1",
    }
    for paper_key, strategy_id in mapping.items():
        row = db.execute(
            """SELECT COUNT(*) trades,
            SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
            SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_trades,
            COALESCE(SUM(realized_pnl),0) pnl,
            COALESCE(SUM(entry_cost+open_fees),0) risk
            FROM paper_trades_v2 WHERE strategy=?""",
            (paper_key,),
        ).fetchone()
        _metric_row(
            db, strategy_id, "paper",
            {
                "trades": row["trades"], "wins": row["wins"],
                "open_trades": row["open_trades"], "pnl": row["pnl"],
                "capital_risked": row["risk"],
            },
        )
    db.commit()
    return {"btc_paper": paper_values, "btc_live": live_values}


def update_capital_snapshot(db) -> dict:
    preflight = get_setting(db, "live_last_preflight", {}) or {}
    balance = float(preflight.get("balance_usd", 0) or 0)
    initial = float(get_setting(db, "live_session_initial_balance", balance) or balance)
    live_pnl = float(db.execute(
        "SELECT COALESCE(SUM(realized_pnl),0) FROM fast_live_orders WHERE realized_pnl IS NOT NULL"
    ).fetchone()[0])
    pending = int(db.execute(
        """SELECT COUNT(*) FROM fast_live_orders WHERE state IN
        ('submitting','matched','uncertain','live','delayed','resolved_waiting_redemption')"""
    ).fetchone()[0])
    external = balance - initial - live_pnl if not pending else float("nan")
    detail = {"external_is_estimate": not pending, "preflight_ts": preflight.get("checked_ts")}
    ts = float(preflight.get("checked_ts", time.time()) or time.time())
    db.execute(
        """INSERT OR REPLACE INTO factory_capital_snapshots
        (ts,wallet_balance,session_initial,realized_live_pnl,estimated_external_net,
         pending_orders,detail_json) VALUES(?,?,?,?,?,?,?)""",
        (
            ts, balance, initial, live_pnl, external if not pending else 0.0,
            pending, json.dumps(detail, separators=(",", ":")),
        ),
    )
    db.commit()
    return {
        "wallet_balance": balance, "session_initial": initial,
        "realized_live_pnl": live_pnl, "estimated_external_net": external,
        "pending_orders": pending,
    }


def _source_score(url: str, text: str) -> tuple[float, bool]:
    lowered = text.lower()
    matches = sum(term in lowered for term in RELEVANT_TERMS)
    relevant = matches >= 2 or (matches >= 1 and "prediction" in lowered)
    credibility = 0.15 if "wallstreetbets" in url.lower() else 0.45
    if any(term in lowered for term in ("public wallet", "github", "postmortem", "dataset")):
        credibility += 0.15
    return min(0.8, credibility), relevant


def collect_factory_sources(db, cfg: Config) -> dict:
    inserted = errors = ignored = 0
    now = time.time()
    for index, url in enumerate(cfg.factory_feed_urls):
        try:
            items = news.fetch_feed(url)
        except urllib.error.HTTPError as exc:
            errors += 1
            if exc.code == 429:
                log.warning("factory source rate-limited: %s", url)
            else:
                log.exception("factory source feed failed: %s", url)
            items = []
        except Exception:
            errors += 1
            log.exception("factory source feed failed: %s", url)
            items = []
        for item in items:
            raw = f"{item.title} {item.summary}".strip()
            credibility, relevant = _source_score(url, raw)
            if not relevant:
                ignored += 1
                continue
            content_hash = _hash(raw)
            source_id = _hash({"url": item.url, "content": content_hash})
            cur = db.execute(
                """INSERT OR IGNORE INTO factory_sources
                (source_id,source_type,source_name,url,title,published_ts,discovered_ts,
                 content_hash,summary,raw_text,status,credibility,expires_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id, "forum", item.source, item.url, item.title,
                    item.published_ts, now, content_hash, item.summary[:800],
                    raw[:2400], "new", credibility,
                    now + cfg.factory_source_retention_days * 86400,
                ),
            )
            inserted += cur.rowcount
        if "reddit.com" in url.lower() and index + 1 < len(cfg.factory_feed_urls):
            time.sleep(2.0)
    set_setting(db, "factory_last_source_collect", now)
    db.commit()
    return {"inserted": inserted, "ignored": ignored, "errors": errors}


def ai_spend_today(db) -> float:
    start = int(time.time() // 86400) * 86400
    return float(db.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE ts>=?",
        (start,),
    ).fetchone()[0])


def research_new_sources(db, cfg: Config) -> dict:
    if not cfg.openai_api_key:
        return {"state": "no_api_key", "created": 0}
    if ai_spend_today(db) >= min(cfg.factory_ai_target_usd_per_day, cfg.max_ai_usd_per_day):
        return {"state": "soft_target_reached", "created": 0}
    rows = db.execute(
        """SELECT * FROM factory_sources WHERE status='new'
        ORDER BY credibility DESC,published_ts DESC LIMIT ?""",
        (cfg.factory_max_sources_per_review,),
    ).fetchall()
    if not rows:
        return {"state": "no_new_sources", "created": 0}
    evidence = {
        "sources": [
            {
                "source_id": row["source_id"], "source": row["source_name"],
                "title": row["title"], "summary": row["summary"],
                "credibility_prior": row["credibility"],
            }
            for row in rows
        ],
        "constraints": {
            "paper_only": True, "public_data_only": True,
            "preferred_venue": "polymarket", "small_capital": True,
        },
    }
    batch_key = _hash([row["source_id"] for row in rows])
    # Hypothesis objects contain several mandatory fields; 550 tokens can end
    # mid-JSON even when the model is concise. This remains inexpensive and is
    # still reserved against the same atomic daily dollar ceiling.
    review_cfg = replace(cfg, max_ai_output_tokens=max(1000, cfg.max_ai_output_tokens))
    try:
        result, usage = ai.review_factory_sources(
            db, review_cfg, evidence, reference=f"factory:{batch_key[:16]}"
        )
    except Exception as exc:
        log.exception("factory hypothesis review failed")
        set_setting(db, "factory_last_research", time.time())
        db.commit()
        enqueue_notification(
            db, kind="factory_research_failure", severity="error",
            dedupe_key=f"factory-research-failure-{batch_key}",
            message=f"⚠️ Factory hypothesis review failed safely: {repr(exc)[:300]}",
        )
        return {"state": "ai_error", "created": 0, "sources": len(rows)}
    valid_ids = {row["source_id"] for row in rows}
    created = 0
    now = time.time()
    for candidate in result.get("candidates", [])[:3]:
        source_ids = [value for value in candidate.get("source_ids", []) if value in valid_ids]
        key = _hash(
            {
                "title": candidate.get("title"), "venue": candidate.get("venue"),
                "test": candidate.get("deterministic_test"),
            }
        )
        cur = db.execute(
            """INSERT OR IGNORE INTO factory_hypotheses
            (hypothesis_key,created_ts,updated_ts,title,venue,family,thesis,
             required_data,deterministic_test,risks_json,source_ids_json,
             confidence,lifecycle,strategy_version_id,review_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,? ,NULL,?)""",
            (
                key, now, now, str(candidate.get("title", ""))[:300],
                str(candidate.get("venue", "unknown"))[:80],
                str(candidate.get("family", "other"))[:80],
                str(candidate.get("thesis", ""))[:1200],
                str(candidate.get("required_data", ""))[:1200],
                str(candidate.get("deterministic_test", ""))[:1200],
                json.dumps([candidate.get("strongest_risk", "")], separators=(",", ":")),
                json.dumps(source_ids, separators=(",", ":")),
                int(candidate.get("evidence_confidence", 0)), "idea",
                json.dumps(candidate, sort_keys=True, separators=(",", ":")),
            ),
        )
        created += cur.rowcount
    db.executemany(
        "UPDATE factory_sources SET status='reviewed' WHERE source_id=?",
        [(row["source_id"],) for row in rows],
    )
    set_setting(db, "factory_last_research", now)
    db.commit()
    if created:
        enqueue_notification(
            db, kind="factory_research", severity="info",
            dedupe_key=f"factory-research-{batch_key}",
            message=f"Factory research: {created} new paper hypotheses queued; AI ${usage['estimated_cost_usd']:.4f}.",
        )
    return {"state": "reviewed", "created": created, "sources": len(rows), "usage": usage}


def _start_run(db, kind: str, interval: int) -> tuple[int | None, str]:
    now = time.time()
    stale_before = now - max(1800, interval * 2)
    db.execute(
        """UPDATE factory_runs SET finished_ts=?,state='failed',error=?
        WHERE kind=? AND state='running' AND started_ts<?""",
        (
            now,
            "Recovered stale run after worker termination",
            kind,
            stale_before,
        ),
    )
    bucket = int(now // max(60, interval)) * max(60, interval)
    key = f"{kind}:{bucket}"
    cur = db.execute(
        """INSERT OR IGNORE INTO factory_runs
        (run_key,kind,scheduled_ts,started_ts,state) VALUES(?,?,?,?, 'running')""",
        (key, kind, bucket, now),
    )
    db.commit()
    return (int(cur.lastrowid) if cur.rowcount else None), key


def factory_cycle(db, cfg: Config) -> dict:
    bootstrap_factory(db, cfg)
    if not cfg.factory_enabled:
        return {"state": "disabled"}
    run_id, run_key = _start_run(db, "factory_cycle", 900)
    if run_id is None:
        return {"state": "overlap_skipped", "run_key": run_key}
    result: dict = {"state": "ok", "run_key": run_key}
    try:
        now = time.time()
        if now - float(get_setting(db, "factory_last_source_collect", 0) or 0) >= cfg.factory_source_interval_seconds:
            result["sources"] = collect_factory_sources(db, cfg)
        if cfg.factory_enable_market_scan:
            from .lab import scan_all

            result["market_scan"] = scan_all(db, cfg)
        if now - float(get_setting(db, "factory_last_research", 0) or 0) >= cfg.factory_research_interval_seconds:
            result["research"] = research_new_sources(db, cfg)
        from .automation import automation_cycle

        automation_started = time.time()
        result["automation"] = automation_cycle(db, cfg)
        allocation = result["automation"].get("allocation", {})
        if allocation.get("state") == "promoted":
            enqueue_notification(
                db, kind="strategy_promotion", severity="info",
                dedupe_key=f"promotion-{allocation['to']}",
                message=(
                    f"🧬 Strategy promoted automatically: {allocation['from']} → "
                    f"{allocation['to']} after fixed replay/forward gates."
                ),
            )
        transitions = db.execute(
            """SELECT * FROM factory_lifecycle_events WHERE ts>=?
            AND to_state IN ('quarantined','blocked_adapter') ORDER BY id""",
            (automation_started,),
        ).fetchall()
        for transition in transitions:
            enqueue_notification(
                db, kind="strategy_lifecycle",
                severity="error" if transition["to_state"] == "quarantined" else "info",
                dedupe_key=f"lifecycle-{transition['id']}",
                message=(
                    f"🧬 Strategy {transition['to_state']}: {transition['spec_id']} — "
                    f"{transition['reason'][:300]}"
                ),
            )
        result["metrics"] = update_strategy_metrics(db, cfg)
        result["capital"] = update_capital_snapshot(db)
        db.execute(
            "UPDATE factory_runs SET finished_ts=?,state='complete',detail_json=? WHERE id=?",
            (time.time(), json.dumps(result, default=str, separators=(",", ":")), run_id),
        )
        db.commit()
        return result
    except Exception as exc:
        log.exception("factory cycle failed")
        db.execute(
            "UPDATE factory_runs SET finished_ts=?,state='failed',error=? WHERE id=?",
            (time.time(), repr(exc)[:2000], run_id),
        )
        db.commit()
        enqueue_notification(
            db, kind="factory_failure", severity="error",
            dedupe_key=f"factory-failure-{run_key}",
            message=f"⚠️ Factory cycle failed: {repr(exc)[:500]}",
        )
        raise


def record_runtime_status(db, broker) -> dict:
    from .automation import active_btc_spec

    strategy_version_id, _ = active_btc_spec(db, broker.cfg)
    budget, reason = broker.budget(db, strategy_version_id)
    status = {
        "ts": time.time(), "gate": broker.gate_reason(), "budget": budget,
        "budget_reason": reason, "strategy_version_id": strategy_version_id,
    }
    set_setting(db, "live_runtime_status", status)
    set_setting(db, "btc_runtime_heartbeat", status["ts"])
    db.commit()
    return status


def healthcheck(db, cfg: Config, include_integrity: bool = False) -> dict:
    now = time.time()
    checks: dict[str, tuple[str, str]] = {}
    heartbeat = float(get_setting(db, "btc_runtime_heartbeat", 0) or 0)
    heartbeat_age = now - heartbeat if heartbeat else float("inf")
    checks["btc_heartbeat"] = (
        "ok" if heartbeat_age <= cfg.factory_heartbeat_stale_seconds else "critical",
        f"age={heartbeat_age:.1f}s",
    )
    disk = shutil.disk_usage(cfg.db_path.parent)
    free_gb = disk.free / (1024 ** 3)
    checks["disk"] = (
        "ok" if free_gb >= cfg.factory_disk_pause_free_gb else "critical",
        f"free={free_gb:.2f}GB",
    )
    pending = db.execute(
        """SELECT COUNT(*),COALESCE(MIN(created_ts),0) FROM fast_live_orders
        WHERE state IN ('submitting','matched','uncertain','live','delayed')"""
    ).fetchone()
    oldest_age = now - float(pending[1]) if int(pending[0]) else 0.0
    checks["live_orders"] = (
        "critical" if int(pending[0]) and oldest_age > 900 else "ok",
        f"pending={int(pending[0])},oldest={oldest_age:.0f}s",
    )
    spend = ai_spend_today(db)
    checks["ai_budget"] = (
        "critical" if spend > cfg.max_ai_usd_per_day + 1e-9 else "ok",
        f"spent_or_reserved=${spend:.4f}/${cfg.max_ai_usd_per_day:.2f}",
    )
    if include_integrity:
        try:
            integrity = str(db.execute("PRAGMA quick_check").fetchone()[0])
        except Exception as exc:
            integrity = f"check failed: {repr(exc)[:400]}"
        checks["database"] = (
            "ok" if integrity == "ok" else "critical", integrity[:500]
        )
        set_setting(db, "factory_last_integrity_check", now)
    else:
        # Integrity failures remain latched until a later explicit integrity
        # check proves the database healthy; routine checks cannot erase them.
        stored_database = db.execute(
            "SELECT status,detail FROM factory_health WHERE check_name='database'"
        ).fetchone()
        if stored_database is not None and stored_database["status"] != "ok":
            checks["database"] = (
                str(stored_database["status"]), str(stored_database["detail"])
            )
    previous = get_setting(db, "factory_health_status", {}) or {}
    critical = [name for name, (status, _) in checks.items() if status == "critical"]
    overall = "paused" if critical else "healthy"
    for name, (status, detail) in checks.items():
        old = db.execute(
            "SELECT consecutive_failures FROM factory_health WHERE check_name=?", (name,)
        ).fetchone()
        failures = (int(old[0]) + 1) if old and status != "ok" else (1 if status != "ok" else 0)
        db.execute(
            """INSERT INTO factory_health
            (check_name,checked_ts,status,consecutive_failures,detail) VALUES(?,?,?,?,?)
            ON CONFLICT(check_name) DO UPDATE SET checked_ts=excluded.checked_ts,
            status=excluded.status,consecutive_failures=excluded.consecutive_failures,
            detail=excluded.detail""",
            (name, now, status, failures, detail),
        )
    pause = {"active": bool(critical), "reason": ",".join(critical), "ts": now}
    set_setting(db, "factory_live_pause", pause)
    set_setting(db, "factory_health_status", {"status": overall, "ts": now, "checks": checks})
    db.commit()
    if previous.get("status") != overall:
        enqueue_notification(
            db, kind="health_transition",
            severity="error" if critical else "info",
            dedupe_key=f"health-{overall}-{int(now // 300)}",
            message=(
                f"⚠️ Factory {overall.upper()}: {', '.join(critical)}"
                if critical else "✅ Factory health restored."
            ),
        )
    return {"status": overall, "checks": checks}
