#!/usr/bin/env python3
"""Conservative one-day audit for the TWAP-native BTC terminal strategy.

The input bundle contains exact Chainlink TWAP60 ticks, official market outcomes,
and one-second full-depth CLOB snapshots. A signal is counted only when a later
snapshot can fill five shares within the live limit and depth cushion.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

from opportunity_lab.fastlane import (
    blended_probability,
    market_conditioned_up_probability,
    market_midpoint_up_probability,
    simulate_fok_fill,
    up_probability,
)


def read_markets(path: Path) -> dict[str, dict]:
    markets = {}
    with gzip.open(path, "rt") as handle:
        for line in handle:
            row = json.loads(line)
            if not row.get("resolved") or len(row.get("token_ids") or []) != 2:
                continue
            prices = row.get("outcome_prices") or []
            if prices not in (["1", "0"], ["0", "1"]):
                continue
            row["result"] = "up" if prices[0] == "1" else "down"
            markets[row["slug"]] = row
    return markets


def read_twap(path: Path) -> tuple[list[float], list[float], dict[int, float]]:
    times, values, exact = [], [], {}
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            ts = int(row["feed_ts_ms"]) / 1000
            value = float(row["value"])
            times.append(ts)
            values.append(value)
            exact[int(ts)] = value
    return times, values, exact


def read_final_books(path: Path, markets: dict[str, dict]) -> dict[str, list]:
    books: dict[str, list] = defaultdict(list)
    with gzip.open(path, "rt") as handle:
        for line in handle:
            row = json.loads(line)
            market = markets.get(row.get("slug"))
            if market is None:
                continue
            ts = int(row.get("event_ts_ms") or 0) / 1000
            if not market["end_sec"] - 65 <= ts <= market["end_sec"] - 4:
                continue
            levels = []
            for level in (row.get("payload") or {}).get("asks") or []:
                price, size = float(level["price"]), float(level["size"])
                if 0 < price < 1 and size > 0:
                    levels.append((price, size))
            if levels:
                books[row["slug"]].append(
                    (ts, str(row["asset_id"]), tuple(sorted(levels)))
                )
    for events in books.values():
        events.sort()
    return books


def sigma_at(times: list[float], values: list[float], now: float) -> float | None:
    right = bisect.bisect_right(times, now)
    left = bisect.bisect_left(times, now - 120, 0, right)
    if right - left < 8:
        return None
    squared = elapsed = 0.0
    for index in range(left + 1, right):
        dt = times[index] - times[index - 1]
        if 0 < dt <= 10:
            squared += math.log(values[index] / values[index - 1]) ** 2
            elapsed += dt
    return math.sqrt(squared / elapsed) if elapsed > 0 else None


def run_case(
    markets: dict[str, dict], books: dict[str, dict], times: list[float],
    values: list[float], exact: dict[int, float], *, max_ask: float,
    min_confidence: float, oracle_weight: float, min_edge: float,
    min_seconds: float = 5.0, max_seconds: float = 11.0,
    model_kind: str = "twap_native_v1",
) -> dict:
    trades = []
    gates: dict[str, int] = defaultdict(int)
    for slug, events in books.items():
        market = markets[slug]
        start_value = exact.get(int(market["start_sec"]))
        if start_value is None:
            continue
        up_token, down_token = map(str, market["token_ids"])
        latest = {}
        for event_index, (ts, asset, levels) in enumerate(events):
            latest[asset] = (ts, levels)
            seconds_left = market["end_sec"] - ts
            if not min_seconds <= seconds_left <= max_seconds:
                continue
            gates["in_window"] += 1
            if up_token not in latest or down_token not in latest:
                gates["missing_side"] += 1
                continue
            if ts - latest[up_token][0] > 2 or ts - latest[down_token][0] > 2:
                gates["stale_side"] += 1
                continue
            gates["paired_book"] += 1
            index = bisect.bisect_right(times, ts) - 1
            if index < 0 or ts - times[index] > 2:
                gates["stale_twap"] += 1
                continue
            sigma = sigma_at(times, values, ts)
            if sigma is None:
                gates["missing_sigma"] += 1
                continue
            up_ask = latest[up_token][1][0][0]
            down_ask = latest[down_token][1][0][0]
            market_up = market_midpoint_up_probability(up_ask, down_ask)
            if market_up is None:
                continue
            oracle_up = up_probability(
                values[index], start_value, seconds_left, sigma, 0.20, 0.85, 2.0
            )
            if model_kind == "market_conditioned_v8":
                margin_bps = 10_000 * (values[index] / start_value - 1)
                probability = market_conditioned_up_probability(
                    oracle_up, market_up, up_ask + down_ask - 1,
                    margin_bps, sigma,
                )
            else:
                probability = blended_probability(oracle_up, market_up, oracle_weight)
            side = "up" if probability >= 0.5 else "down"
            confidence = probability if side == "up" else 1 - probability
            token = up_token if side == "up" else down_token
            ask = up_ask if side == "up" else down_ask
            limit = min(0.99, ask + 0.02)
            fee_at_limit = 0.07 * limit * (1 - limit)
            if (
                ask > max_ask or confidence < min_confidence
                or confidence - limit - fee_at_limit < min_edge
            ):
                gates["signal_gate"] += 1
                continue
            gates["signal"] += 1
            if sum(size for price, size in latest[token][1] if price <= limit) < 6.25:
                gates["decision_depth"] += 1
                continue
            delayed = next(
                (
                    (later_ts, later_levels)
                    for later_ts, later_asset, later_levels in events[event_index + 1:]
                    if ts + 0.35 <= later_ts <= ts + 1.25
                    and later_asset == token
                ),
                None,
            )
            if delayed is None:
                gates["no_delayed_snapshot"] += 1
                continue
            fill = simulate_fok_fill(delayed[1], 5.0, limit, 0.07, 1.25)
            if fill is None:
                gates["delayed_fill"] += 1
                continue
            pnl = (5.0 if market["result"] == side else 0.0) - fill["total_cost"]
            trades.append({
                "slug": slug, "side": side, "result": market["result"],
                "ask": ask, "fill": fill["average_price"], "pnl": pnl,
            })
            break
    wins = sum(row["side"] == row["result"] for row in trades)
    pnl = sum(row["pnl"] for row in trades)
    return {
        "trades": len(trades), "wins": wins, "losses": len(trades) - wins,
        "pnl": pnl, "roi": pnl / (5 * len(trades)) if trades else 0.0,
        "rows": trades, "gates": dict(gates),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--twap", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--books", type=Path, required=True)
    args = parser.parse_args()
    markets = read_markets(args.markets)
    times, values, exact = read_twap(args.twap)
    books = read_final_books(args.books, markets)

    declared = run_case(
        markets, books, times, values, exact, max_ask=0.68,
        min_confidence=0.78, oracle_weight=0.75, min_edge=0.04,
    )
    print(json.dumps({
        "case": "predeclared_twap_native_v1", "markets": len(markets),
        **{key: value for key, value in declared.items() if key != "rows"},
    }, sort_keys=True))
    for row in declared["rows"]:
        print(json.dumps(row, sort_keys=True))

    legacy_shape = run_case(
        markets, books, times, values, exact, max_ask=0.68,
        min_confidence=0.78, oracle_weight=0.75, min_edge=-1.0,
        model_kind="market_conditioned_v8",
    )
    print(json.dumps({
        "case": "v8_shape_on_correct_twap", "markets": len(markets),
        **{key: value for key, value in legacy_shape.items() if key != "rows"},
    }, sort_keys=True))

    cases = []
    for max_seconds in (11.0, 20.0, 30.0, 45.0, 60.0):
        for max_ask in (0.68, 0.72, 0.76, 0.80):
            for confidence in (0.70, 0.74, 0.78):
                for weight in (0.65, 0.75, 0.85):
                    result = run_case(
                        markets, books, times, values, exact, max_ask=max_ask,
                        min_confidence=confidence, oracle_weight=weight,
                        min_edge=0.03, max_seconds=max_seconds,
                    )
                    cases.append((
                        result["pnl"], result["trades"], max_seconds, max_ask,
                        confidence, weight, result,
                    ))
    viable = [case for case in cases if case[1] >= 5]
    for _, _, max_seconds, max_ask, confidence, weight, result in sorted(
        viable, reverse=True
    )[:15]:
        print(json.dumps({
            "case": "sensitivity", "max_ask": max_ask,
            "min_confidence": confidence, "oracle_weight": weight,
            "max_seconds": max_seconds, "min_edge": 0.03,
            **{key: value for key, value in result.items() if key not in {"rows", "gates"}},
        }, sort_keys=True))


if __name__ == "__main__":
    main()
