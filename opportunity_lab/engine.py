from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ai
from .config import Config
from .db import balance, ledger_entry
from .hyperliquid import (
    PairSnapshot,
    executable_close_prices,
    executable_open_prices,
    l2_book,
    matching_spot_perps,
)


log = logging.getLogger("opportunity_lab.engine")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _today_start(cfg: Config) -> float:
    now = datetime.now(ZoneInfo(cfg.timezone))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _ai_budget_available(db, cfg: Config) -> bool:
    row = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(estimated_cost_usd),0) FROM ai_usage "
        "WHERE ts>=?",
        (_today_start(cfg),),
    ).fetchone()
    return row[0] < cfg.max_ai_calls_per_day and row[1] < cfg.max_ai_usd_per_day


def _recent_funding(db, coin: str, limit: int = 12) -> list[float]:
    rows = db.execute(
        "SELECT funding_rate FROM market_snapshots WHERE coin=? ORDER BY ts DESC LIMIT ?",
        (coin, limit),
    ).fetchall()
    return [float(r[0]) for r in rows]


def _recent_ai_review(db, coin: str, cfg: Config):
    cutoff = time.time() - cfg.ai_review_cooldown_hours * 3600
    return db.execute(
        """SELECT score,projected_net_usd,ts FROM candidates
        WHERE coin=? AND ts>? AND ai_decision IS NOT NULL
        ORDER BY ts DESC LIMIT 1""",
        (coin, cutoff),
    ).fetchone()


def _materially_improved(previous, score: float, projected_net: float) -> bool:
    if previous is None:
        return True
    score_jump = score >= float(previous["score"]) + 10
    old_net = float(previous["projected_net_usd"])
    profit_jump = projected_net >= max(old_net * 1.5, old_net + 0.03)
    return score_jump or profit_jump


def _score(
    cfg: Config,
    pair: PairSnapshot,
    positive_samples: int,
    projected_net: float,
    friction: float,
    executable_notional: float,
) -> float:
    # A transparent triage rank, explicitly not a probability.
    persistence = 25 * _clamp(positive_samples / 12, 0, 1)
    economics = 35 * _clamp(projected_net / max(0.15, friction * 3), 0, 1)
    min_volume = min(pair.spot_day_volume, pair.perp_day_volume)
    liquidity = 20 * _clamp(math.log10(max(1, min_volume)) / 7, 0, 1)
    basis = 10 * _clamp(1 - abs(pair.basis_pct) / 0.03, 0, 1)
    depth = 10 * _clamp(executable_notional / cfg.position_notional_usd, 0, 1)
    return round(persistence + economics + liquidity + basis + depth, 2)


def _insert_snapshots(db, pairs: list[PairSnapshot], ts: float) -> None:
    bucket = ts - (ts % 60)
    db.executemany(
        """INSERT OR IGNORE INTO market_snapshots
        (ts,coin,spot_coin,funding_rate,perp_mark,spot_mark,
         perp_day_volume,spot_day_volume,basis_pct)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        [
            (
                bucket, p.coin, p.spot_coin, p.funding_rate, p.perp_mark,
                p.spot_mark, p.perp_day_volume, p.spot_day_volume, p.basis_pct,
            )
            for p in pairs
        ],
    )


def _manage_positions(db, cfg: Config, pairs_by_coin: dict[str, PairSnapshot]) -> int:
    closed = 0
    rows = db.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    for pos in rows:
        pair = pairs_by_coin.get(pos["coin"])
        if pair is None:
            continue
        now = time.time()
        elapsed_hours = max(0, (now - pos["last_update_ts"]) / 3600)
        funding_add = pos["qty"] * pair.perp_mark * pair.funding_rate * elapsed_hours
        new_funding = pos["funding_pnl"] + funding_add
        held_hours = (now - pos["open_ts"]) / 3600

        spot_book = l2_book(pos["spot_coin"])
        perp_book = l2_book(pos["coin"])
        close_px = executable_close_prices(spot_book, perp_book, pos["qty"])
        if close_px is None:
            db.execute(
                "UPDATE positions SET funding_pnl=?, last_update_ts=? WHERE id=?",
                (new_funding, now, pos["id"]),
            )
            continue
        spot_exit, perp_exit = close_px
        gross_mark = (
            pos["qty"] * (spot_exit - pos["spot_entry"])
            + pos["qty"] * (pos["perp_entry"] - perp_exit)
            + new_funding
        )
        allocated = pos["spot_cost"] + pos["perp_margin"]
        reason = None
        if held_hours >= cfg.max_hold_hours:
            reason = "max_hold"
        elif held_hours >= 1 and pair.funding_rate <= 0:
            reason = "funding_nonpositive"
        elif gross_mark <= -0.05 * allocated:
            reason = "basis_stop"
        if reason is None:
            db.execute(
                "UPDATE positions SET funding_pnl=?, last_update_ts=? WHERE id=?",
                (new_funding, now, pos["id"]),
            )
            continue

        spot_proceeds = pos["qty"] * spot_exit
        perp_pnl = pos["qty"] * (pos["perp_entry"] - perp_exit)
        close_fees = spot_proceeds * cfg.spot_taker_fee + (
            pos["qty"] * perp_exit * cfg.perp_taker_fee
        )
        returned = spot_proceeds + pos["perp_margin"] + perp_pnl + new_funding - close_fees
        initially_reserved = pos["spot_cost"] + pos["perp_margin"] + pos["open_fees"]
        realized = returned - initially_reserved
        db.execute(
            """UPDATE positions SET status='closed',close_ts=?,spot_exit=?,perp_exit=?,
            close_fees=?,funding_pnl=?,realized_pnl=?,last_update_ts=?,exit_reason=?
            WHERE id=?""",
            (
                now, spot_exit, perp_exit, close_fees, new_funding, realized,
                now, reason, pos["id"],
            ),
        )
        ledger_entry(db, "shadow_close", returned, pos["id"], reason)
        closed += 1
        log.info("closed shadow %s pnl=%+.4f reason=%s", pos["coin"], realized, reason)
    return closed


def _open_position(db, cfg: Config, candidate_id: int, evidence: dict) -> int | None:
    if db.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0] >= cfg.max_open_positions:
        return None
    qty = evidence["execution"]["qty"]
    spot_entry = evidence["execution"]["spot_buy_vwap"]
    perp_entry = evidence["execution"]["perp_short_vwap"]
    spot_cost = qty * spot_entry
    perp_margin = qty * perp_entry  # simulated 1x isolated margin
    open_fees = spot_cost * cfg.spot_taker_fee + perp_margin * cfg.perp_taker_fee
    reserved = spot_cost + perp_margin + open_fees
    if balance(db) < reserved:
        return None
    now = time.time()
    cur = db.execute(
        """INSERT INTO positions
        (coin,spot_coin,status,open_ts,qty,spot_entry,perp_entry,spot_cost,
         perp_margin,open_fees,last_update_ts,candidate_id)
        VALUES(?,?,'open',?,?,?,?,?,?,?,?,?)""",
        (
            evidence["coin"], evidence["spot_coin"], now, qty, spot_entry,
            perp_entry, spot_cost, perp_margin, open_fees, now, candidate_id,
        ),
    )
    position_id = cur.lastrowid
    ledger_entry(db, "shadow_open", -reserved, position_id, evidence["coin"])
    db.execute(
        "UPDATE candidates SET state='opened',position_id=? WHERE id=?",
        (position_id, candidate_id),
    )
    log.info("opened shadow %s qty=%g reserved=%.4f", evidence["coin"], qty, reserved)
    return position_id


def scan(db, cfg: Config) -> dict:
    now = time.time()
    pairs = matching_spot_perps()
    _insert_snapshots(db, pairs, now)
    pairs_by_coin = {p.coin: p for p in pairs}
    closed = _manage_positions(db, cfg, pairs_by_coin)
    created = reviewed = opened = 0
    notable: list[str] = []

    eligible = sorted(pairs, key=lambda p: p.funding_rate, reverse=True)[:12]
    for pair in eligible:
        if pair.funding_rate <= 0:
            continue
        history = _recent_funding(db, pair.coin)
        positive = sum(rate > 0 for rate in history)
        if positive < cfg.min_persistent_samples:
            continue
        if min(pair.spot_day_volume, pair.perp_day_volume) < cfg.min_day_volume_usd:
            continue
        median_rate = statistics.median(history)
        conservative_rate = max(0.0, min(pair.funding_rate, median_rate)) * cfg.funding_retention

        spot_book = l2_book(pair.spot_coin)
        perp_book = l2_book(pair.coin)
        opened_px = executable_open_prices(
            spot_book, perp_book, cfg.position_notional_usd
        )
        if opened_px is None:
            continue
        qty, spot_buy, perp_sell = opened_px
        close_px = executable_close_prices(spot_book, perp_book, qty)
        if close_px is None:
            continue
        spot_sell, perp_buy = close_px
        spot_open_ntl = qty * spot_buy
        perp_open_ntl = qty * perp_sell
        spot_close_ntl = qty * spot_sell
        perp_close_ntl = qty * perp_buy
        fees = (
            (spot_open_ntl + spot_close_ntl) * cfg.spot_taker_fee
            + (perp_open_ntl + perp_close_ntl) * cfg.perp_taker_fee
        )
        spread_loss = qty * (spot_buy - spot_sell) + qty * (perp_buy - perp_sell)
        friction = fees + spread_loss
        projected_funding = perp_open_ntl * conservative_rate * cfg.max_hold_hours
        projected_net = projected_funding - friction
        score = _score(
            cfg, pair, positive, projected_net, friction, perp_open_ntl
        )
        evidence = {
            "strategy": "hyperliquid_spot_long_perp_short_funding",
            "phase": "fake_money_only",
            "coin": pair.coin,
            "spot_coin": pair.spot_coin,
            "timestamp": int(now),
            "virtual_bankroll_usd": cfg.starting_cash_usd,
            "current_funding_rate_hourly": pair.funding_rate,
            "median_recent_funding_rate_hourly": median_rate,
            "funding_retention_assumption": cfg.funding_retention,
            "basis_pct": pair.basis_pct,
            "positive_samples": positive,
            "samples_examined": len(history),
            "spot_day_volume_usd": pair.spot_day_volume,
            "perp_day_volume_usd": pair.perp_day_volume,
            "execution": {
                "qty": qty,
                "spot_buy_vwap": spot_buy,
                "perp_short_vwap": perp_sell,
                "spot_exit_vwap_at_current_book": spot_sell,
                "perp_cover_vwap_at_current_book": perp_buy,
                "perp_notional_usd": perp_open_ntl,
            },
            "costs": {
                "published_base_spot_taker_rate": cfg.spot_taker_fee,
                "published_base_perp_taker_rate": cfg.perp_taker_fee,
                "estimated_roundtrip_fees_usd": fees,
                "current_roundtrip_spread_loss_usd": spread_loss,
                "total_current_friction_usd": friction,
            },
            "projection": {
                "holding_hours": cfg.max_hold_hours,
                "funding_income_usd": projected_funding,
                "net_after_current_friction_usd": projected_net,
            },
            "score": score,
            "score_is_probability": False,
            "source": "Hyperliquid public production info API",
        }
        cur = db.execute(
            """INSERT INTO candidates
            (ts,coin,spot_coin,score,funding_rate,median_funding_rate,basis_pct,
             projected_funding_usd,roundtrip_friction_usd,projected_net_usd,
             executable_notional_usd,positive_samples,evidence_json,state,reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'shortlisted',?)""",
            (
                now, pair.coin, pair.spot_coin, score, pair.funding_rate,
                median_rate, pair.basis_pct, projected_funding, friction,
                projected_net, perp_open_ntl, positive,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                "deterministic screen",
            ),
        )
        candidate_id = cur.lastrowid
        created += 1
        if score < cfg.ai_trigger_score or projected_net <= 0:
            db.execute(
                "UPDATE candidates SET state='rejected',reason='below AI/economic threshold' WHERE id=?",
                (candidate_id,),
            )
            continue
        previous_review = _recent_ai_review(db, pair.coin, cfg)
        if not _materially_improved(previous_review, score, projected_net):
            db.execute(
                """UPDATE candidates SET state='review_suppressed',
                reason='recent AI review; no material improvement' WHERE id=?""",
                (candidate_id,),
            )
            continue
        if not cfg.openai_api_key or not _ai_budget_available(db, cfg):
            db.execute(
                "UPDATE candidates SET state='awaiting_ai',reason='AI unavailable or daily cap reached' WHERE id=?",
                (candidate_id,),
            )
            notable.append(
                f"🔎 FAKE candidate {pair.coin}: rank {score:.0f}/100, "
                f"projected ${projected_net:+.3f}; awaiting AI"
            )
            continue
        try:
            decision, usage = ai.review(cfg, evidence)
            reviewed += 1
            db.execute(
                """INSERT INTO ai_usage
                (ts,model,input_tokens,output_tokens,estimated_cost_usd,candidate_id,status)
                VALUES(?,?,?,?,?,?,'ok')""",
                (
                    time.time(), cfg.ai_model, usage["input_tokens"],
                    usage["output_tokens"], usage["estimated_cost_usd"], candidate_id,
                ),
            )
            db.execute(
                """UPDATE candidates SET ai_decision=?,ai_confidence=?,ai_json=?,
                state=?,reason=? WHERE id=?""",
                (
                    decision["decision"], decision["confidence"],
                    json.dumps(decision, sort_keys=True, separators=(",", ":")),
                    "approved" if decision["decision"] == "approve" else "rejected",
                    decision["summary"], candidate_id,
                ),
            )
            if decision["decision"] == "approve":
                if _open_position(db, cfg, candidate_id, evidence):
                    opened += 1
            notable.append(
                f"🤖 FAKE review {pair.coin}: {decision['decision']} "
                f"({decision['confidence']}/100 evidence confidence)\n"
                f"projected ${projected_net:+.3f} | {decision['summary']}"
            )
        except Exception as exc:
            log.exception("AI review failed for %s", pair.coin)
            db.execute(
                "INSERT INTO ai_usage(ts,model,candidate_id,status,error) VALUES(?,?,?,'error',?)",
                (time.time(), cfg.ai_model, candidate_id, repr(exc)[:1000]),
            )
            db.execute(
                "UPDATE candidates SET state='awaiting_ai',reason='AI call failed' WHERE id=?",
                (candidate_id,),
            )
    db.commit()
    return {
        "pairs": len(pairs), "candidates": created, "reviewed": reviewed,
        "opened": opened, "closed": closed, "balance": balance(db),
        "notable": notable,
    }
