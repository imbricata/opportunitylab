from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Iterable


_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "a", "an", "and", "are", "at", "be", "before", "by", "did", "do",
    "does", "for", "from", "happen", "in", "is", "it", "of", "on", "or",
    "the", "this", "to", "will", "win", "with", "yes", "no",
}


def as_float(value, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def parse_ts(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (float, int)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def words(text: str) -> tuple[str, ...]:
    return tuple(token for token in _TOKEN.findall(text.lower()) if token not in _STOP)


def normalized_title(text: str) -> str:
    return " ".join(words(text))


def title_similarity(left: str, right: str) -> float:
    a = normalized_title(left)
    b = normalized_title(right)
    if not a or not b:
        return 0.0
    aset, bset = set(a.split()), set(b.split())
    union = aset | bset
    jaccard = len(aset & bset) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    containment = min(len(aset & bset) / max(1, len(aset)), len(aset & bset) / max(1, len(bset)))
    return round(0.5 * jaccard + 0.35 * sequence + 0.15 * containment, 6)


@dataclass(frozen=True)
class Market:
    venue: str
    market_id: str
    event_id: str
    title: str
    rules: str
    category: str = ""
    event_title: str = ""
    close_ts: float | None = None
    status: str = "open"
    result: str = ""
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    yes_bid_size: float | None = None
    yes_ask_size: float | None = None
    no_bid_size: float | None = None
    no_ask_size: float | None = None
    volume: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    fee_coefficient: float = 0.0
    min_order_size: float = 1.0
    mutually_exclusive: bool = False
    collectively_exhaustive: bool = False
    strike_type: str = ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    source_urls: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status.lower() in {"active", "open", "initialized"} and not self.result

    @property
    def midpoint(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return self.yes_bid if self.yes_bid is not None else self.yes_ask

    def ask(self, side: str) -> float | None:
        return self.yes_ask if side == "yes" else self.no_ask

    def bid(self, side: str) -> float | None:
        return self.yes_bid if side == "yes" else self.no_bid

    def ask_size(self, side: str) -> float | None:
        return self.yes_ask_size if side == "yes" else self.no_ask_size

    def bid_size(self, side: str) -> float | None:
        return self.yes_bid_size if side == "yes" else self.no_bid_size

    def fee(self, side: str, qty: float, price: float | None = None) -> float:
        p = self.ask(side) if price is None else price
        if p is None:
            return float("inf")
        return max(0.0, self.fee_coefficient * qty * p * (1 - p))

    def compact(self) -> dict:
        data = asdict(self)
        data["source_urls"] = list(self.source_urls)
        return data


@dataclass(frozen=True)
class Leg:
    venue: str
    market_id: str
    side: str
    price: float
    max_qty: float
    fee_coefficient: float
    title: str = ""

    def fee(self, qty: float) -> float:
        return max(0.0, self.fee_coefficient * qty * self.price * (1 - self.price))

    def unit_cost(self) -> float:
        return self.price + self.fee_coefficient * self.price * (1 - self.price)


@dataclass(frozen=True)
class Opportunity:
    strategy: str
    title: str
    legs: tuple[Leg, ...]
    detected_ts: float = field(default_factory=time.time)
    guaranteed_payout_per_unit: float = 0.0
    expected_payout_per_unit: float = 0.0
    confidence: int = 100
    deterministic: bool = True
    evidence: dict = field(default_factory=dict)
    expires_ts: float | None = None
    requires_ai: bool = False

    @property
    def unit_cost(self) -> float:
        return sum(leg.unit_cost() for leg in self.legs)

    @property
    def net_edge_per_unit(self) -> float:
        payout = self.guaranteed_payout_per_unit or self.expected_payout_per_unit
        return payout - self.unit_cost

    @property
    def edge_fraction(self) -> float:
        return self.net_edge_per_unit / self.unit_cost if self.unit_cost > 0 else 0.0

    @property
    def max_qty(self) -> float:
        return min((leg.max_qty for leg in self.legs), default=0.0)

    @property
    def fingerprint(self) -> str:
        identity = {
            "strategy": self.strategy,
            "legs": sorted(
                (leg.venue, leg.market_id, leg.side) for leg in self.legs
            ),
            "evidence_key": self.evidence.get("relationship", ""),
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def common_tokens(markets: Iterable[Market]) -> set[str]:
    result: set[str] | None = None
    for market in markets:
        tokens = set(words(market.title))
        result = tokens if result is None else result & tokens
    return result or set()

