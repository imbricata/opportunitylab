from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ai, news
from .config import Config
from .db import (
    ai_cache_get,
    ai_cache_put,
    balance,
    get_setting,
    prune_research_data,
    set_setting,
    store_news_items,
    upsert_prediction_markets,
)
from .engine import scan as scan_funding
from .markets import Leg, Market, Opportunity
from .paper import manage_trades, open_trade, record_opportunity
from .prediction_venues import enabled_connectors
from .strategies import scan_structural


log = logging.getLogger("opportunity_lab.lab")


def _today_start(cfg: Config) -> float:
    now = datetime.now(ZoneInfo(cfg.timezone))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _ai_budget_available(db, cfg: Config) -> bool:
    start = _today_start(cfg)
    old = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_usage WHERE ts>=?",
        (start,),
    ).fetchone()
    new = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(estimated_cost_usd),0) FROM ai_calls_v2 WHERE ts>=?",
        (start,),
    ).fetchone()
    return (
        int(old[0]) + int(new[0]) < cfg.max_ai_calls_per_day
        and float(old[1]) + float(new[1]) < cfg.max_ai_usd_per_day
    )


def _record_ai(db, cfg: Config, purpose: str, status: str, reference: str, usage=None, error=None):
    usage = usage or {}
    db.execute(
        """INSERT INTO ai_calls_v2
        (ts,purpose,model,input_tokens,cached_input_tokens,cache_write_tokens,
         output_tokens,reasoning_tokens,estimated_cost_usd,status,reference_id,error)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            time.time(), purpose, cfg.ai_model,
            usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0),
            usage.get("cache_write_tokens", 0), usage.get("output_tokens", 0),
            usage.get("reasoning_tokens", 0), usage.get("estimated_cost_usd", 0),
            status, reference, repr(error)[:1000] if error else None,
        ),
    )


def _cache_key(purpose: str, evidence: dict, cfg: Config | None = None) -> str:
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    identity = (
        f"{purpose}\0{ai.AI_REVIEW_VERSION}\0{cfg.ai_model if cfg else ''}\0"
    ).encode()
    return hashlib.sha256(identity + raw).hexdigest()


def _equivalence_review(db, cfg: Config, opportunity: Opportunity):
    # Prices and depth do not change whether two resolution rules are equal.
    source = opportunity.evidence
    def contract(value):
        if not isinstance(value, dict):
            return value
        return {
            key: value.get(key)
            for key in (
                "venue", "market_id", "title", "rules", "event_title", "close_ts",
                "strike_type", "floor_strike", "cap_strike", "source_urls", "metadata",
            )
        }

    evidence = {
        "relationship": source.get("relationship"),
        "left": contract(source.get("left")),
        "right": contract(source.get("right")),
        "yes_venue": source.get("yes_venue"),
        "no_venue": source.get("no_venue"),
    }
    key = _cache_key("contract_equivalence", evidence, cfg)
    cached = ai_cache_get(db, key)
    if cached is not None:
        return cached, True
    if not cfg.openai_api_key or not _ai_budget_available(db, cfg):
        return None, False
    try:
        result, _usage = ai.review_equivalence(
            db, cfg, evidence, reference=opportunity.fingerprint
        )
        ai_cache_put(db, key, "contract_equivalence", result, 7 * 86400)
        return result, False
    except Exception as exc:
        log.exception("contract equivalence review failed")
        return None, False


def _review_structural(db, cfg: Config, opportunities: list[Opportunity]) -> tuple[int, int, list[str]]:
    recorded = opened = 0
    notable = []
    for opportunity in opportunities[:30]:
        approved = opportunity.deterministic
        reason = "deterministic payout inequality"
        ai_result = None
        if opportunity.requires_ai:
            ai_result, _ = _equivalence_review(db, cfg, opportunity)
            approved = bool(
                ai_result
                and ai_result.get("equivalent")
                and ai_result.get("same_resolution_event")
                and ai_result.get("same_deadline")
                and ai_result.get("same_outcome_polarity")
                and int(ai_result.get("confidence", 0)) >= cfg.cross_venue_ai_confidence
            )
            reason = ai_result.get("summary", "AI unavailable") if ai_result else "awaiting AI"
            if approved:
                opportunity = replace(
                    opportunity, confidence=int(ai_result["confidence"]), requires_ai=False
                )
        state = "approved" if approved else ("awaiting_ai" if ai_result is None else "rejected")
        opportunity_id = record_opportunity(db, cfg, opportunity, state, reason, ai_result)
        if opportunity_id is None:
            continue
        recorded += 1
        if approved and open_trade(db, cfg, opportunity_id, opportunity):
            opened += 1
            notable.append(
                f"🧪 PAPER {opportunity.strategy}: {opportunity.edge_fraction:.1%} modeled edge"
            )
    return recorded, opened, notable


def _news_opportunity(cfg: Config, market: Market, item: news.NewsItem, result: dict):
    if (
        result.get("decision") != "trade"
        or not result.get("news_directly_relevant")
        or result.get("resolution_rule_conflict")
        or int(result.get("confidence", 0)) < cfg.news_ai_confidence
    ):
        return None
    fair_yes = float(result["fair_yes_probability"])
    choices = []
    for side, fair in (("yes", fair_yes), ("no", 1 - fair_yes)):
        price = market.ask(side)
        size = market.ask_size(side)
        if price is None or size is None or size <= 0:
            continue
        unit_cost = price + market.fee_coefficient * price * (1 - price)
        choices.append((fair - unit_cost, side, fair, price, size))
    if not choices:
        return None
    edge, side, fair, price, size = max(choices)
    if edge < cfg.news_ai_edge:
        return None
    leg = Leg(
        venue=market.venue, market_id=market.market_id, side=side,
        price=price, max_qty=size, fee_coefficient=market.fee_coefficient,
        title=market.title,
    )
    return Opportunity(
        strategy="news_directional",
        title=f"News reaction ({side.upper()}): {market.title}",
        legs=(leg,),
        expected_payout_per_unit=fair,
        confidence=int(result["confidence"]),
        deterministic=False,
        evidence={
            "relationship": f"news:{item.item_id}",
            "news": {"source": item.source, "title": item.title, "url": item.url},
            "market": market.compact(),
            "ai": result,
        },
        expires_ts=market.close_ts,
    )


def _scan_news(db, cfg: Config, markets: list[Market]) -> tuple[int, int, list[str]]:
    if not cfg.enable_news:
        return 0, 0, []
    last = float(get_setting(db, "last_news_scan", 0))
    if time.time() - last < cfg.news_interval_seconds:
        return 0, 0, []
    items = news.collect(cfg, markets)
    inserted = store_news_items(db, items)
    set_setting(db, "last_news_scan", time.time())
    opened = 0
    notable = []
    for relevance, market, item in news.relevant_pairs(markets, items):
        evidence = {
            "market": market.compact(),
            "news": {
                "source": item.source, "title": item.title,
                "summary": item.summary[:800], "published_ts": item.published_ts,
            },
            "local_keyword_relevance": relevance,
            "current_time": time.time(),
        }
        key = hashlib.sha256(
            f"news_market\0{market.venue}\0{market.market_id}\0{item.item_id}".encode()
        ).hexdigest()
        result = ai_cache_get(db, key)
        if result is None:
            if not cfg.openai_api_key or not _ai_budget_available(db, cfg):
                break
            reference = f"{market.venue}:{market.market_id}:{item.item_id[:12]}"
            try:
                result, _usage = ai.review_news(
                    db, cfg, evidence, reference=reference
                )
                ai_cache_put(db, key, "news_market", result, 2 * 86400)
            except Exception as exc:
                log.exception("news review failed")
                continue
        opportunity = _news_opportunity(cfg, market, item, result)
        if opportunity is None:
            continue
        opportunity_id = record_opportunity(
            db, cfg, opportunity, "approved", result["summary"], result
        )
        if opportunity_id and open_trade(db, cfg, opportunity_id, opportunity):
            opened += 1
            notable.append(
                f"📰 PAPER {opportunity.legs[0].side.upper()} {market.title[:100]}"
            )
    return inserted, opened, notable


def scan_all(db, cfg: Config) -> dict:
    connectors = enabled_connectors(cfg)
    markets: list[Market] = []
    venue_errors = {}
    for connector in connectors:
        try:
            fetched = connector.fetch_markets()
            markets.extend(fetched)
            log.info("collected %d markets from %s", len(fetched), connector.venue)
        except Exception as exc:
            log.exception("venue collection failed: %s", connector.venue)
            venue_errors[connector.venue] = repr(exc)[:300]

    quote_rows = upsert_prediction_markets(db, markets)
    managed = manage_trades(db, cfg, connectors)
    structural = scan_structural(markets, cfg)
    recorded, prediction_opened, notable = _review_structural(db, cfg, structural)
    news_inserted, news_opened, news_notable = _scan_news(db, cfg, markets)
    notable.extend(news_notable)

    funding = {"pairs": 0, "candidates": 0, "opened": 0, "closed": 0, "notable": []}
    if cfg.enable_funding:
        try:
            funding = scan_funding(db, cfg)
            notable.extend(funding.get("notable", []))
        except Exception as exc:
            log.exception("funding scan failed")
            venue_errors["hyperliquid"] = repr(exc)[:300]

    prune_research_data(db)
    db.commit()
    return {
        "markets": len(markets),
        "quote_rows": quote_rows,
        "structural_candidates": len(structural),
        "opportunities_recorded": recorded,
        "paper_opened": prediction_opened + news_opened + int(funding.get("opened", 0)),
        "paper_closed": managed["closed"] + managed["settled"] + int(funding.get("closed", 0)),
        "news_inserted": news_inserted,
        "funding_pairs": funding.get("pairs", 0),
        "balance": balance(db),
        "errors": venue_errors,
        "notable": notable,
    }
