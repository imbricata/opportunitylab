from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict

from .config import Config
from .db import balance, ledger_entry
from .markets import Opportunity


log = logging.getLogger("opportunity_lab.paper")


def record_opportunity(
    db, cfg: Config, opportunity: Opportunity, state: str, reason: str = "", ai_json=None
):
    cutoff = time.time() - cfg.opportunity_cooldown_minutes * 60
    prior = db.execute(
        "SELECT id FROM opportunities_v2 WHERE fingerprint=? AND ts>? ORDER BY ts DESC LIMIT 1",
        (opportunity.fingerprint, cutoff),
    ).fetchone()
    if prior is not None:
        return None
    cur = db.execute(
        """INSERT INTO opportunities_v2
        (ts,fingerprint,strategy,title,deterministic,confidence,unit_cost,
         net_edge_per_unit,edge_fraction,max_qty,opportunity_json,state,reason,ai_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opportunity.detected_ts,
            opportunity.fingerprint,
            opportunity.strategy,
            opportunity.title,
            int(opportunity.deterministic),
            opportunity.confidence,
            opportunity.unit_cost,
            opportunity.net_edge_per_unit,
            opportunity.edge_fraction,
            opportunity.max_qty,
            opportunity.to_json(),
            state,
            reason,
            json.dumps(ai_json, sort_keys=True, separators=(",", ":")) if ai_json else None,
        ),
    )
    return int(cur.lastrowid)


def open_trade(db, cfg: Config, opportunity_id: int, opportunity: Opportunity) -> int | None:
    if not cfg.auto_paper_trade:
        return None
    open_total = db.execute(
        "SELECT COUNT(*) FROM paper_trades_v2 WHERE status='open'"
    ).fetchone()[0]
    open_strategy = db.execute(
        "SELECT COUNT(*) FROM paper_trades_v2 WHERE status='open' AND strategy=?",
        (opportunity.strategy,),
    ).fetchone()[0]
    if open_total >= cfg.paper_max_open_trades or open_strategy >= cfg.paper_max_strategy_trades:
        return None
    unit_cost = opportunity.unit_cost
    if unit_cost <= 0 or opportunity.max_qty <= 0:
        return None
    available = min(
        cfg.paper_trade_budget_usd,
        cfg.paper_max_allocation_usd,
        max(0.0, balance(db)),
    )
    qty = min(opportunity.max_qty, available / unit_cost)
    if qty <= 0:
        return None
    entry_cost = qty * sum(leg.price for leg in opportunity.legs)
    fees = sum(leg.fee(qty) for leg in opportunity.legs)
    reserved = entry_cost + fees
    if reserved > balance(db) + 1e-9:
        qty *= balance(db) / reserved
        entry_cost = qty * sum(leg.price for leg in opportunity.legs)
        fees = sum(leg.fee(qty) for leg in opportunity.legs)
        reserved = entry_cost + fees
    if qty <= 0 or reserved <= 0:
        return None
    now = time.time()
    cur = db.execute(
        """INSERT INTO paper_trades_v2
        (opportunity_id,strategy,title,status,open_ts,qty,entry_cost,open_fees,
         marked_value,guaranteed_payout,legs_json,evidence_json)
        VALUES(?,?,?,'open',?,?,?,?,?,?,?,?)""",
        (
            opportunity_id,
            opportunity.strategy,
            opportunity.title,
            now,
            qty,
            entry_cost,
            fees,
            0.0,
            qty * opportunity.guaranteed_payout_per_unit,
            json.dumps([asdict(leg) for leg in opportunity.legs], separators=(",", ":")),
            json.dumps(opportunity.evidence, sort_keys=True, separators=(",", ":")),
        ),
    )
    trade_id = int(cur.lastrowid)
    ledger_entry(db, "paper_open", -reserved, trade_id, opportunity.strategy)
    db.execute(
        "UPDATE opportunities_v2 SET state='opened',trade_id=? WHERE id=?",
        (trade_id, opportunity_id),
    )
    log.info("opened paper trade %s qty=%.4f reserved=%.4f", opportunity.strategy, qty, reserved)
    return trade_id


def _resolved_payout(legs: list[dict], markets: dict[tuple[str, str], object], qty: float):
    payout = 0.0
    for leg in legs:
        market = markets.get((leg["venue"], leg["market_id"]))
        if market is None or not getattr(market, "result", ""):
            return None
        result = str(market.result).lower()
        side = leg["side"].lower()
        yes_won = result in {"yes", "up", "true", "1"}
        no_won = result in {"no", "down", "false", "0"}
        payout += qty if (side == "yes" and yes_won) or (side == "no" and no_won) else 0.0
    return payout


def manage_trades(db, cfg: Config, connectors) -> dict:
    connector_map = {connector.venue: connector for connector in connectors}
    rows = db.execute("SELECT * FROM paper_trades_v2 WHERE status='open'").fetchall()
    closed = 0
    settled = 0
    for row in rows:
        legs = json.loads(row["legs_json"])
        markets = {}
        complete = True
        for leg in legs:
            key = (leg["venue"], leg["market_id"])
            connector = connector_map.get(leg["venue"])
            if connector is None:
                complete = False
                continue
            try:
                market = connector.get_market(leg["market_id"])
            except Exception:
                log.exception("paper mark failed for %s", key)
                complete = False
                continue
            markets[key] = market
            if market is None:
                complete = False

        payout = _resolved_payout(legs, markets, float(row["qty"])) if complete else None
        now = time.time()
        if payout is not None:
            realized = payout - float(row["entry_cost"]) - float(row["open_fees"])
            db.execute(
                """UPDATE paper_trades_v2 SET status='settled',close_ts=?,marked_value=?,
                realized_pnl=?,exit_reason='settled' WHERE id=?""",
                (now, payout, realized, row["id"]),
            )
            ledger_entry(db, "paper_settle", payout, row["id"], row["strategy"])
            settled += 1
            continue

        marked = 0.0
        exit_fees = 0.0
        markable = complete
        for leg in legs:
            market = markets.get((leg["venue"], leg["market_id"]))
            price = market.bid(leg["side"]) if market else None
            if price is None:
                markable = False
                break
            marked += float(row["qty"]) * price
            exit_fees += market.fee(leg["side"], float(row["qty"]), price)
        if markable:
            db.execute(
                "UPDATE paper_trades_v2 SET marked_value=? WHERE id=?",
                (marked - exit_fees, row["id"]),
            )

        max_hours = (
            cfg.paper_directional_max_hold_hours
            if row["strategy"] == "news_directional"
            else cfg.paper_arb_max_hold_hours
        )
        held_hours = (now - float(row["open_ts"])) / 3600
        if markable and held_hours >= max_hours:
            proceeds = max(0.0, marked - exit_fees)
            realized = proceeds - float(row["entry_cost"]) - float(row["open_fees"])
            db.execute(
                """UPDATE paper_trades_v2 SET status='closed',close_ts=?,close_fees=?,
                marked_value=?,realized_pnl=?,exit_reason='max_hold' WHERE id=?""",
                (now, exit_fees, proceeds, realized, row["id"]),
            )
            ledger_entry(db, "paper_close", proceeds, row["id"], "max_hold")
            closed += 1
    return {"open_checked": len(rows), "settled": settled, "closed": closed}
