from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import replace

from .config import Config
from .markets import Market, as_float, parse_ts


log = logging.getLogger("opportunity_lab.prediction_venues")
USER_AGENT = "opportunitylab/0.2 paper-research contact=local-user"


def _get_json(url: str, timeout: int = 30):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _price(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    result = as_float(value)
    if result is None or not (0 <= result <= 1):
        return None
    return result


def _positive(value, default: float = 0.0) -> float:
    result = as_float(value, default)
    return max(0.0, result if result is not None else default)


class KalshiPublic:
    venue = "kalshi"
    base = "https://external-api.kalshi.com/trade-api/v2"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def fetch_markets(self) -> list[Market]:
        params = urllib.parse.urlencode(
            {
                "status": "open",
                "with_nested_markets": "true",
                "limit": min(200, max(1, self.cfg.kalshi_event_limit)),
                "min_close_ts": int(time.time()),
            }
        )
        payload = _get_json(f"{self.base}/events?{params}")
        result: list[Market] = []
        for event in payload.get("events", []):
            result.extend(self._parse_event(event))
        return result

    def _parse_event(self, event: dict) -> list[Market]:
        sources = tuple(
            source.get("url", "")
            for source in event.get("settlement_sources", [])
            if source.get("url")
        )
        exclusive = bool(event.get("mutually_exclusive"))
        markets = []
        for raw in event.get("markets", []):
            parsed = self._parse_market(raw, event, sources, exclusive)
            if parsed is not None:
                markets.append(parsed)
        return markets

    def _parse_market(
        self, raw: dict, event: dict, sources: tuple[str, ...], exclusive: bool
    ) -> Market | None:
        ticker = raw.get("ticker")
        if not ticker:
            return None
        yes_bid = _price(raw.get("yes_bid_dollars"))
        yes_ask = _price(raw.get("yes_ask_dollars"))
        no_bid = _price(raw.get("no_bid_dollars"))
        no_ask = _price(raw.get("no_ask_dollars"))
        return Market(
            venue=self.venue,
            market_id=ticker,
            event_id=raw.get("event_ticker") or event.get("event_ticker") or ticker,
            title=raw.get("title") or event.get("title") or ticker,
            event_title=event.get("title") or "",
            rules="\n".join(
                part for part in (raw.get("rules_primary"), raw.get("rules_secondary")) if part
            ),
            category=event.get("category") or "",
            close_ts=parse_ts(raw.get("close_time") or raw.get("expiration_time")),
            status=raw.get("status") or "open",
            result=(raw.get("result") or "").lower(),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=_positive(raw.get("yes_bid_size_fp")),
            yes_ask_size=_positive(raw.get("yes_ask_size_fp")),
            no_bid_size=_positive(raw.get("no_bid_size_fp")),
            no_ask_size=_positive(raw.get("no_ask_size_fp")),
            volume=_positive(raw.get("volume_fp")),
            volume_24h=_positive(raw.get("volume_24h_fp")),
            liquidity=_positive(raw.get("liquidity_dollars")),
            fee_coefficient=self.cfg.kalshi_taker_fee_coefficient,
            min_order_size=0.01 if raw.get("fractional_trading_enabled") else 1.0,
            mutually_exclusive=exclusive,
            # Kalshi exposes exclusivity but does not make collective
            # exhaustiveness explicit in this response. Never assume it.
            collectively_exhaustive=False,
            strike_type=(raw.get("strike_type") or "").lower(),
            floor_strike=as_float(raw.get("floor_strike")),
            cap_strike=as_float(raw.get("cap_strike")),
            source_urls=sources,
            metadata={
                "yes_sub_title": raw.get("yes_sub_title") or "",
                "no_sub_title": raw.get("no_sub_title") or "",
                "collateral_return_type": event.get("collateral_return_type") or "",
                "series_ticker": event.get("series_ticker") or "",
            },
        )

    def get_market(self, market_id: str, previous: Market | None = None) -> Market | None:
        payload = _get_json(f"{self.base}/markets/{urllib.parse.quote(market_id)}")
        raw = payload.get("market") or {}
        if not raw:
            return None
        event_stub = {
            "event_ticker": raw.get("event_ticker"),
            "title": previous.event_title if previous else "",
            "category": previous.category if previous else "",
        }
        parsed = self._parse_market(
            raw,
            event_stub,
            previous.source_urls if previous else (),
            previous.mutually_exclusive if previous else False,
        )
        if parsed and previous:
            parsed = replace(
                parsed,
                event_title=previous.event_title,
                collectively_exhaustive=previous.collectively_exhaustive,
                metadata=previous.metadata,
            )
        return parsed


class PolymarketPublic:
    """Public, paper-only international Polymarket discovery connector."""

    venue = "polymarket"
    base = "https://gamma-api.polymarket.com"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @staticmethod
    def _json_array(value) -> list:
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(value or "[]")
            return parsed if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    def fetch_markets(self) -> list[Market]:
        params = urllib.parse.urlencode(
            {
                "active": "true", "closed": "false", "order": "volume24hr",
                "ascending": "false",
                "limit": min(500, max(1, self.cfg.polymarket_market_limit)),
                "offset": 0,
            }
        )
        payload = _get_json(f"{self.base}/markets?{params}")
        rows = payload if isinstance(payload, list) else []
        return [market for raw in rows if (market := self._parse_market(raw))]

    def _parse_market(self, raw: dict) -> Market | None:
        market_id = raw.get("id")
        if not market_id or not raw.get("enableOrderBook", False):
            return None
        outcomes = [
            str(value).strip().lower()
            for value in self._json_array(raw.get("outcomes"))
        ]
        prices = [_price(value) for value in self._json_array(raw.get("outcomePrices"))]
        if len(outcomes) < 2 or {"yes", "no"} - set(outcomes):
            return None
        yes_index, no_index = outcomes.index("yes"), outcomes.index("no")
        yes_bid = _price(raw.get("bestBid"))
        yes_ask = _price(raw.get("bestAsk"))
        if yes_bid is None and yes_index < len(prices):
            yes_bid = prices[yes_index]
        if yes_ask is None and yes_index < len(prices):
            yes_ask = prices[yes_index]
        no_bid = 1 - yes_ask if yes_ask is not None else (
            prices[no_index] if no_index < len(prices) else None
        )
        no_ask = 1 - yes_bid if yes_bid is not None else (
            prices[no_index] if no_index < len(prices) else None
        )
        minimum = max(0.0001, _positive(raw.get("orderMinSize"), 5.0))
        events = raw.get("events") or []
        event = events[0] if events and isinstance(events[0], dict) else {}
        schedule = raw.get("feeSchedule") or {}
        fee_rate = as_float(schedule.get("rate"))
        if not raw.get("feesEnabled"):
            fee_rate = 0.0
        elif fee_rate is None:
            fee_rate = self.cfg.polymarket_default_fee_coefficient
        result = ""
        if raw.get("closed"):
            for outcome, price in zip(outcomes, prices):
                if price is not None and price >= 0.999:
                    result = outcome
                    break
        resolution = raw.get("resolutionSource") or event.get("resolutionSource")
        return Market(
            venue=self.venue, market_id=str(market_id),
            event_id=str(event.get("id") or raw.get("conditionId") or market_id),
            title=raw.get("question") or raw.get("slug") or str(market_id),
            event_title=event.get("title") or "", rules=raw.get("description") or "",
            category=raw.get("category") or event.get("category") or "",
            close_ts=parse_ts(raw.get("endDate") or raw.get("endDateIso")),
            status="closed" if raw.get("closed") else (
                "open" if raw.get("active") and raw.get("acceptingOrders") else "inactive"
            ),
            result=result, yes_bid=yes_bid, yes_ask=yes_ask,
            no_bid=no_bid, no_ask=no_ask,
            # Gamma has BBO but no BBO quantity. Paper sizing is restricted to
            # one minimum lot rather than inventing executable depth.
            yes_bid_size=minimum, yes_ask_size=minimum,
            no_bid_size=minimum, no_ask_size=minimum,
            volume=_positive(raw.get("volumeNum") or raw.get("volume")),
            volume_24h=_positive(raw.get("volume24hr")),
            liquidity=_positive(raw.get("liquidityNum") or raw.get("liquidity")),
            fee_coefficient=max(0.0, float(fee_rate or 0)), min_order_size=minimum,
            mutually_exclusive=bool(event.get("negRisk")),
            collectively_exhaustive=False,
            source_urls=(str(resolution),) if resolution else (),
            metadata={
                "condition_id": raw.get("conditionId") or "",
                "slug": raw.get("slug") or "",
                "fees_enabled": bool(raw.get("feesEnabled")),
                "accepting_orders": bool(raw.get("acceptingOrders")),
                "depth_source": "gamma_bbo_minimum_lot_only",
            },
        )

    def get_market(self, market_id: str, previous: Market | None = None) -> Market | None:
        raw = _get_json(
            f"{self.base}/markets/{urllib.parse.quote(str(market_id), safe='')}"
        )
        parsed = self._parse_market(raw) if isinstance(raw, dict) else None
        return parsed or previous


class PolymarketUSPublic:
    venue = "polymarket_us"
    base = "https://gateway.polymarket.us"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def fetch_markets(self) -> list[Market]:
        limit = min(500, max(1, self.cfg.polymarket_market_limit))
        params = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": 0,
                "orderBy": "volume24hr",
                "orderDirection": "desc",
            }
        )
        payload = _get_json(f"{self.base}/v1/markets?{params}")
        raw_markets = payload.get("markets", payload if isinstance(payload, list) else [])
        return [market for raw in raw_markets if (market := self._parse_market(raw))]

    def _parse_market(self, raw: dict) -> Market | None:
        slug = raw.get("slug")
        if not slug:
            return None
        yes_bid = _price(raw.get("bestBidQuote"))
        yes_ask = _price(raw.get("bestAskQuote"))
        sides = raw.get("marketSides") or []
        if yes_bid is None:
            yes_bid = _price(raw.get("bestBid"))
        if yes_ask is None:
            yes_ask = _price(raw.get("bestAsk"))
        if yes_bid is None or yes_ask is None:
            for side in sides:
                if side.get("long") is True:
                    quoted = _price(side.get("quote") or side.get("price"))
                    yes_bid = yes_bid if yes_bid is not None else quoted
                    yes_ask = yes_ask if yes_ask is not None else quoted
        no_bid = 1 - yes_ask if yes_ask is not None else None
        no_ask = 1 - yes_bid if yes_bid is not None else None
        minimum = _positive(raw.get("minimumTradeQty") or raw.get("orderMinSize"), 1.0)
        event_id = (
            raw.get("eventId")
            or raw.get("participantId")
            or raw.get("gameId")
            or self._event_from_slug(slug)
        )
        theta = as_float(raw.get("feeCoefficient"))
        if theta is None:
            theta = self.cfg.polymarket_us_default_fee_coefficient
        return Market(
            venue=self.venue,
            market_id=slug,
            event_id=str(event_id),
            title=raw.get("title") or raw.get("question") or slug,
            event_title=raw.get("question") or "",
            rules=raw.get("description") or "",
            category=raw.get("category") or "",
            close_ts=parse_ts(raw.get("endDate") or raw.get("gameStartTime")),
            status="closed" if raw.get("closed") else (raw.get("ep3Status") or "open"),
            result=self._result(raw),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            # The listing response has BBO but not BBO depth. Restrict initial
            # paper sizing to one minimum lot until the book is enriched.
            yes_bid_size=minimum,
            yes_ask_size=minimum,
            no_bid_size=minimum,
            no_ask_size=minimum,
            volume=_positive(raw.get("volume")),
            volume_24h=_positive(raw.get("volume24hr")),
            liquidity=_positive(raw.get("liquidity")),
            fee_coefficient=max(0.0, theta),
            min_order_size=max(minimum, 0.0001),
            mutually_exclusive=False,
            collectively_exhaustive=False,
            source_urls=(),
            metadata={
                "market_type": raw.get("marketType") or "",
                "sports_market_type": raw.get("sportsMarketType") or "",
                "line": raw.get("line"),
                "participant_id": raw.get("participantId") or "",
            },
        )

    @staticmethod
    def _event_from_slug(slug: str) -> str:
        # Product is the first slug component on the US exchange. Removing it
        # groups common sports products conservatively; uncertain groups are
        # never treated as exhaustive by deterministic strategies.
        parts = slug.split("-", 1)
        return parts[1] if len(parts) == 2 else slug

    @staticmethod
    def _result(raw: dict) -> str:
        if not raw.get("closed"):
            return ""
        prices = raw.get("outcomePrices")
        outcomes = raw.get("outcomes")
        try:
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            for outcome, price in zip(outcomes or [], prices or []):
                if float(price) >= 0.999:
                    return str(outcome).lower()
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return ""

    def enrich_book(self, market: Market) -> Market:
        slug = urllib.parse.quote(market.market_id, safe="")
        payload = _get_json(f"{self.base}/v1/markets/{slug}/book")
        book = payload.get("marketData") or {}
        bids = book.get("bids") or []
        offers = book.get("offers") or []
        yes_bid = _price(bids[0].get("px")) if bids else market.yes_bid
        yes_ask = _price(offers[0].get("px")) if offers else market.yes_ask
        bid_size = _positive(bids[0].get("qty"), market.min_order_size) if bids else market.yes_bid_size
        ask_size = _positive(offers[0].get("qty"), market.min_order_size) if offers else market.yes_ask_size
        return replace(
            market,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=(1 - yes_ask) if yes_ask is not None else None,
            no_ask=(1 - yes_bid) if yes_bid is not None else None,
            yes_bid_size=bid_size,
            yes_ask_size=ask_size,
            no_bid_size=ask_size,
            no_ask_size=bid_size,
        )

    def get_market(self, market_id: str, previous: Market | None = None) -> Market | None:
        slug = urllib.parse.quote(market_id, safe="")
        payload = _get_json(f"{self.base}/v1/market/slug/{slug}")
        raw = payload.get("market", payload)
        parsed = self._parse_market(raw) if isinstance(raw, dict) else None
        if parsed is None:
            return previous
        try:
            return self.enrich_book(parsed)
        except Exception:
            log.exception("could not enrich Polymarket US book %s", market_id)
            return parsed


def enabled_connectors(cfg: Config):
    connectors = []
    if cfg.enable_kalshi:
        connectors.append(KalshiPublic(cfg))
    if cfg.enable_polymarket:
        connectors.append(PolymarketPublic(cfg))
    if cfg.enable_polymarket_us:
        connectors.append(PolymarketUSPublic(cfg))
    return connectors
