#!/usr/bin/env python3
"""Price-only sensitivity audit using unthrottled BTC 5m best-ask updates.

This deliberately does not claim fills because the best-bid/ask stream has no
sizes. It checks whether a TWAP signal survives 350 ms repricing and is useful
for rejecting unprofitable parameter families before depth-aware forward use.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
from collections import defaultdict
from pathlib import Path

from opportunity_lab.fastlane import (
    blended_probability,
    market_conditioned_up_probability,
    market_midpoint_up_probability,
    up_probability,
)
from scripts.backtest_twap_native import read_markets, read_twap, sigma_at


def read_quotes(path: Path, markets: dict[str, dict]) -> dict:
    quotes = defaultdict(lambda: defaultdict(list))
    with gzip.open(path, "rt") as handle:
        for line in handle:
            row = json.loads(line)
            market = markets.get(row.get("slug"))
            if market is None:
                continue
            ts = int(row.get("event_ts_ms") or 0) / 1000
            if not market["end_sec"] - 65 <= ts <= market["end_sec"] - 4:
                continue
            ask = (row.get("payload") or {}).get("best_ask")
            if ask is None:
                continue
            ask = float(ask)
            if 0 < ask < 1:
                quotes[row["slug"]][str(row["asset_id"])].append((ts, ask))
    return quotes


def at(series: list[tuple[float, float]], ts: float) -> float | None:
    index = bisect.bisect_right(series, (ts, float("inf"))) - 1
    return series[index][1] if index >= 0 else None


def audit(
    markets: dict, quotes: dict, times: list[float], values: list[float],
    exact: dict[int, float], *, model: str, max_seconds: int, max_ask: float,
    min_confidence: float, oracle_weight: float, min_edge: float,
    trend_lookback: int = 10, trend_multiplier: float = 0.0,
    sigma_cache: dict[float, float | None] | None = None,
) -> dict:
    rows = []
    for slug, by_token in quotes.items():
        market = markets[slug]
        up_token, down_token = map(str, market["token_ids"])
        if up_token not in by_token or down_token not in by_token:
            continue
        start_value = exact.get(int(market["start_sec"]))
        if start_value is None:
            continue
        for seconds_left in range(max_seconds, 4, -1):
            ts = market["end_sec"] - seconds_left
            up_ask, down_ask = at(by_token[up_token], ts), at(by_token[down_token], ts)
            market_up = market_midpoint_up_probability(up_ask, down_ask)
            if market_up is None:
                continue
            index = bisect.bisect_right(times, ts) - 1
            past_index = bisect.bisect_right(times, ts - trend_lookback) - 1
            if index < 0 or past_index < 0 or ts - times[index] > 2:
                continue
            if sigma_cache is None:
                sigma = sigma_at(times, values, ts)
            elif ts in sigma_cache:
                sigma = sigma_cache[ts]
            else:
                sigma = sigma_at(times, values, ts)
                sigma_cache[ts] = sigma
            if sigma is None:
                continue
            slope = (values[index] - values[past_index]) / max(
                1e-9, times[index] - times[past_index]
            )
            projected = values[index] + trend_multiplier * slope * seconds_left
            oracle_up = up_probability(
                projected, start_value, seconds_left, sigma, 0.20, 0.85, 2.0
            )
            if model == "v8":
                probability = market_conditioned_up_probability(
                    oracle_up, market_up, up_ask + down_ask - 1,
                    10_000 * (values[index] / start_value - 1), sigma,
                )
            else:
                probability = blended_probability(oracle_up, market_up, oracle_weight)
            side = "up" if probability >= 0.5 else "down"
            confidence = probability if side == "up" else 1 - probability
            series = by_token[up_token if side == "up" else down_token]
            ask = up_ask if side == "up" else down_ask
            limit = min(0.99, ask + 0.02)
            fee = 0.07 * limit * (1 - limit)
            if (
                ask > max_ask or confidence < min_confidence
                or confidence - limit - fee < min_edge
            ):
                continue
            delayed_ask = at(series, ts + 0.35)
            if delayed_ask is None or delayed_ask > limit:
                continue
            fill_fee = 0.07 * delayed_ask * (1 - delayed_ask)
            cost = 5 * (delayed_ask + fill_fee)
            result = market["result"]
            rows.append({
                "slug": slug, "side": side, "result": result,
                "seconds_left": seconds_left, "ask": ask,
                "delayed_ask": delayed_ask,
                "pnl": (5 if result == side else 0) - cost,
            })
            break
    wins = sum(row["side"] == row["result"] for row in rows)
    pnl = sum(row["pnl"] for row in rows)
    return {
        "trades": len(rows), "wins": wins, "losses": len(rows) - wins,
        "pnl": pnl, "roi": pnl / (5 * len(rows)) if rows else 0,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--twap", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, required=True)
    args = parser.parse_args()
    markets = read_markets(args.markets)
    times, values, exact = read_twap(args.twap)
    quotes = read_quotes(args.quotes, markets)
    sigma_cache: dict[float, float | None] = {}

    cases = []
    for max_seconds in (11, 20, 30, 45, 60):
        for max_ask in (0.64, 0.68, 0.72, 0.76, 0.80):
            for confidence in (0.70, 0.74, 0.78, 0.82):
                for weight in (0.65, 0.75, 0.85):
                    for trend in (0.0, 0.5, 1.0):
                        result = audit(
                            markets, quotes, times, values, exact,
                            model="blend", max_seconds=max_seconds,
                            max_ask=max_ask, min_confidence=confidence,
                            oracle_weight=weight, min_edge=0.03,
                            trend_multiplier=trend,
                            sigma_cache=sigma_cache,
                        )
                        if result["trades"] >= 10:
                            cases.append((result["pnl"], max_seconds, max_ask, confidence, weight, trend, result))
    for _, max_seconds, max_ask, confidence, weight, trend, result in sorted(cases, reverse=True)[:20]:
        print(json.dumps({
            "max_seconds": max_seconds, "max_ask": max_ask,
            "min_confidence": confidence, "oracle_weight": weight,
            "trend_multiplier": trend,
            **{key: value for key, value in result.items() if key != "rows"},
        }, sort_keys=True))


if __name__ == "__main__":
    main()
