from __future__ import annotations

import itertools
from collections import defaultdict

from .config import Config
from .markets import Leg, Market, Opportunity, normalized_title, title_similarity, words


def _leg(market: Market, side: str) -> Leg | None:
    price = market.ask(side)
    size = market.ask_size(side)
    if price is None or size is None or price <= 0 or price >= 1 or size <= 0:
        return None
    return Leg(
        venue=market.venue,
        market_id=market.market_id,
        side=side,
        price=price,
        max_qty=size,
        fee_coefficient=market.fee_coefficient,
        title=market.title,
    )


def _worthwhile(opportunity: Opportunity, cfg: Config) -> bool:
    return (
        opportunity.max_qty > 0
        and opportunity.net_edge_per_unit >= cfg.prediction_min_net_edge
        and opportunity.edge_fraction > 0
    )


def complement_arbitrage(markets: list[Market], cfg: Config) -> list[Opportunity]:
    found = []
    for market in markets:
        if not market.is_open or market.volume < cfg.prediction_min_volume_usd:
            continue
        yes = _leg(market, "yes")
        no = _leg(market, "no")
        if not yes or not no:
            continue
        opportunity = Opportunity(
            strategy="binary_complement",
            title=f"Buy both sides: {market.title}",
            legs=(yes, no),
            guaranteed_payout_per_unit=1.0,
            evidence={
                "relationship": "YES and NO are exhaustive complements",
                "venue": market.venue,
                "rules": market.rules,
            },
            expires_ts=market.close_ts,
        )
        if _worthwhile(opportunity, cfg):
            found.append(opportunity)
    return found


def exclusive_basket_arbitrage(markets: list[Market], cfg: Config) -> list[Opportunity]:
    groups: dict[tuple[str, str], list[Market]] = defaultdict(list)
    for market in markets:
        if market.is_open and market.mutually_exclusive:
            groups[(market.venue, market.event_id)].append(market)

    found = []
    for (_, event_id), group in groups.items():
        candidates = [market for market in group if _leg(market, "no")]
        candidates.sort(key=lambda market: market.yes_bid or 0, reverse=True)
        # Any subset of mutually exclusive propositions is still mutually
        # exclusive. Buy NO on the smallest profitable high-bid subset.
        for count in range(2, min(len(candidates), 20) + 1):
            subset = candidates[:count]
            legs = tuple(_leg(market, "no") for market in subset)
            if any(leg is None for leg in legs):
                continue
            opportunity = Opportunity(
                strategy="exclusive_no_basket",
                title=f"Mutually exclusive NO basket: {subset[0].event_title or event_id}",
                legs=tuple(legs),  # type: ignore[arg-type]
                guaranteed_payout_per_unit=float(count - 1),
                evidence={
                    "relationship": "At most one proposition can resolve YES",
                    "event_id": event_id,
                    "market_count": count,
                    "markets": [market.title for market in subset],
                    "collective_exhaustiveness_not_assumed": True,
                },
                expires_ts=max(
                    (market.close_ts or 0 for market in subset), default=None
                ),
            )
            if _worthwhile(opportunity, cfg):
                found.append(opportunity)
                break
    return found


def directional_arbitrage(markets: list[Market], cfg: Config) -> list[Opportunity]:
    groups: dict[tuple[str, str, str], list[Market]] = defaultdict(list)
    supported = {"greater", "greater_than", "above", "less", "less_than", "below"}
    for market in markets:
        if (
            market.is_open
            and market.strike_type in supported
            and market.floor_strike is not None
        ):
            groups[(market.venue, market.event_id, market.strike_type)].append(market)

    found = []
    for (_, event_id, strike_type), group in groups.items():
        for left, right in itertools.combinations(group, 2):
            low, high = sorted((left, right), key=lambda m: m.floor_strike or 0)
            if strike_type in {"greater", "greater_than", "above"}:
                subset, superset = high, low
            else:
                subset, superset = low, high
            no_subset = _leg(subset, "no")
            yes_superset = _leg(superset, "yes")
            if not no_subset or not yes_superset:
                continue
            opportunity = Opportunity(
                strategy="directional_implication",
                title=f"Threshold implication: {subset.title} => {superset.title}",
                legs=(no_subset, yes_superset),
                guaranteed_payout_per_unit=1.0,
                evidence={
                    "relationship": "subset YES logically implies superset YES",
                    "event_id": event_id,
                    "subset": subset.title,
                    "superset": superset.title,
                    "strike_type": strike_type,
                    "subset_strike": subset.floor_strike,
                    "superset_strike": superset.floor_strike,
                },
                expires_ts=max(subset.close_ts or 0, superset.close_ts or 0),
            )
            if _worthwhile(opportunity, cfg):
                found.append(opportunity)
    return found


def cross_venue_candidates(markets: list[Market], cfg: Config) -> list[Opportunity]:
    by_venue: dict[str, list[Market]] = defaultdict(list)
    for market in markets:
        if market.is_open and market.volume >= cfg.prediction_min_volume_usd:
            by_venue[market.venue].append(market)
    venues = sorted(by_venue)
    found: list[Opportunity] = []
    for first_index, first_venue in enumerate(venues):
        for second_venue in venues[first_index + 1 :]:
            for left in by_venue[first_venue]:
                left_tokens = set(words(left.title))
                if len(left_tokens) < 2:
                    continue
                for right in by_venue[second_venue]:
                    if len(left_tokens & set(words(right.title))) < 2:
                        continue
                    if left.close_ts and right.close_ts and abs(left.close_ts - right.close_ts) > 7 * 86400:
                        continue
                    similarity = title_similarity(left.title, right.title)
                    if similarity < cfg.cross_venue_similarity:
                        continue
                    if left.category and right.category:
                        lcat, rcat = left.category.lower(), right.category.lower()
                        if lcat != rcat and not ({lcat, rcat} <= {"sports", "sport"}):
                            continue
                    for yes_market, no_market in ((left, right), (right, left)):
                        yes_leg = _leg(yes_market, "yes")
                        no_leg = _leg(no_market, "no")
                        if not yes_leg or not no_leg:
                            continue
                        opportunity = Opportunity(
                            strategy="cross_venue_equivalent",
                            title=f"Cross-venue equivalent: {yes_market.title}",
                            legs=(yes_leg, no_leg),
                            guaranteed_payout_per_unit=1.0,
                            confidence=0,
                            deterministic=False,
                            requires_ai=True,
                            evidence={
                                "relationship": "candidate equivalent contracts",
                                "similarity": similarity,
                                "normalized_titles_equal": normalized_title(left.title)
                                == normalized_title(right.title),
                                "left": left.compact(),
                                "right": right.compact(),
                                "yes_venue": yes_market.venue,
                                "no_venue": no_market.venue,
                            },
                            expires_ts=min(
                                value
                                for value in (left.close_ts, right.close_ts)
                                if value is not None
                            )
                            if left.close_ts or right.close_ts
                            else None,
                        )
                        if _worthwhile(opportunity, cfg):
                            found.append(opportunity)
    return found


def scan_structural(markets: list[Market], cfg: Config) -> list[Opportunity]:
    opportunities = []
    opportunities.extend(complement_arbitrage(markets, cfg))
    opportunities.extend(exclusive_basket_arbitrage(markets, cfg))
    opportunities.extend(directional_arbitrage(markets, cfg))
    opportunities.extend(cross_venue_candidates(markets, cfg))
    return sorted(opportunities, key=lambda item: item.edge_fraction, reverse=True)

