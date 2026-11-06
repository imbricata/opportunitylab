#!/usr/bin/env python3
"""Audit a settlement-mechanics model against TWAP60 BTC 5m archives.

The model starts from the currently published rolling TWAP and projects the
expiry TWAP by removing the known outgoing part of the 60-second window and
replacing the unknown future part with the latest instantaneous Chainlink
price.  Execution is counted only when a later full-depth snapshot can fill a
five-share FOK order within the configured limit and depth cushion.
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

from opportunity_lab.fastlane import market_midpoint_up_probability, simulate_fok_fill
from scripts.backtest_twap_native import read_markets, read_twap


def read_prices(path: Path) -> tuple[list[float], list[float]]:
    times, values = [], []
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            times.append(int(row["feed_ts_ms"]) / 1000)
            values.append(float(row["value"]))
    return times, values


def read_books(path: Path, markets: dict[str, dict]) -> dict[str, dict[str, list]]:
    books: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    with gzip.open(path, "rt") as handle:
        for line in handle:
            row = json.loads(line)
            market = markets.get(row.get("slug"))
            if market is None:
                continue
            ts = int(row.get("event_ts_ms") or 0) / 1000
            if not market["end_sec"] - 65 <= ts <= market["end_sec"] - 3:
                continue
            levels = tuple(sorted(
                (float(level["price"]), float(level["size"]))
                for level in (row.get("payload") or {}).get("asks") or []
                if 0 < float(level["price"]) < 1 and float(level["size"]) > 0
            ))
            books[row["slug"]][str(row["asset_id"])].append((ts, levels))
    return books


def latest(series: list, ts: float, max_age: float = 2.0):
    index = bisect.bisect_right(series, ts, key=lambda row: row[0]) - 1
    if index < 0 or ts - series[index][0] > max_age:
        return None
    return series[index]


def next_snapshot(series: list, ts: float, minimum_delay: float = 0.35):
    index = bisect.bisect_left(
        series, ts + minimum_delay, key=lambda row: row[0]
    )
    if index >= len(series) or series[index][0] > ts + 1.5:
        return None
    return series[index]


def value_at(times: list[float], values: list[float], ts: float, max_age=2.0):
    index = bisect.bisect_right(times, ts) - 1
    if index < 0 or ts - times[index] > max_age:
        return None
    return values[index]


def mean_between(times: list[float], values: list[float], start: float, end: float):
    left = bisect.bisect_left(times, start)
    right = bisect.bisect_right(times, end)
    if right - left < max(2, int((end - start) * 0.6)):
        return None
    return sum(values[left:right]) / (right - left)


def log_sigma(times: list[float], values: list[float], ts: float, lookback=120.0):
    right = bisect.bisect_right(times, ts)
    left = bisect.bisect_left(times, ts - lookback, 0, right)
    squared = elapsed = 0.0
    for index in range(left + 1, right):
        dt = times[index] - times[index - 1]
        if 0 < dt <= 5:
            squared += math.log(values[index] / values[index - 1]) ** 2
            elapsed += dt
    return math.sqrt(squared / elapsed) if elapsed >= 30 else None


def normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def projection_probability(
    *, current_twap: float, target: float, current_spot: float,
    outgoing_spot: float, seconds_left: float, spot_sigma: float,
    uncertainty_scale: float,
) -> tuple[float, float, float]:
    projected = current_twap + (
        (current_spot - outgoing_spot) * seconds_left / 60.0
    )
    # Integral of Brownian motion over the unknown part of the future window.
    standard_error = (
        current_spot * spot_sigma * seconds_left ** 1.5
        / (math.sqrt(3.0) * 60.0)
    )
    standard_error = max(0.20, standard_error * uncertainty_scale)
    probability = normal_cdf((projected - target) / standard_error)
    return max(0.005, min(0.995, probability)), projected, standard_error


def observations(markets, books, twap_times, twap_values, exact, spot_times, spot_values):
    grouped = defaultdict(list)
    for slug, market in markets.items():
        by_token = books.get(slug)
        if not by_token:
            continue
        up_token, down_token = map(str, market["token_ids"])
        if up_token not in by_token or down_token not in by_token:
            continue
        target = exact.get(int(market["start_sec"]))
        if target is None:
            continue
        for seconds_left in range(60, 4, -1):
            ts = market["end_sec"] - seconds_left
            current_twap = value_at(twap_times, twap_values, ts)
            current_spot = value_at(spot_times, spot_values, ts)
            outgoing = mean_between(
                spot_times, spot_values, ts - 60, market["end_sec"] - 60
            )
            sigma = log_sigma(spot_times, spot_values, ts)
            if None in {current_twap, current_spot, outgoing, sigma}:
                continue
            up_book = latest(by_token[up_token], ts)
            down_book = latest(by_token[down_token], ts)
            if up_book is None or down_book is None:
                continue
            up_ask = up_book[1][0][0] if up_book[1] else None
            down_ask = down_book[1][0][0] if down_book[1] else None
            market_up = market_midpoint_up_probability(up_ask, down_ask)
            if market_up is None:
                continue
            projected_up, projected, std = projection_probability(
                current_twap=current_twap, target=target,
                current_spot=current_spot, outgoing_spot=outgoing,
                seconds_left=seconds_left, spot_sigma=sigma,
                uncertainty_scale=1.0,
            )
            grouped[slug].append({
                "ts": ts, "seconds_left": seconds_left, "result": market["result"],
                "target": target, "current_twap": current_twap,
                "projected": projected, "projection_std": std,
                "projected_up": projected_up, "market_up": market_up,
                "up_ask": up_ask, "down_ask": down_ask,
                "up_levels": up_book[1], "down_levels": down_book[1],
                "up_future": next_snapshot(by_token[up_token], ts),
                "down_future": next_snapshot(by_token[down_token], ts),
            })
    return grouped


def probability(row: dict, market_weight: float, uncertainty_scale: float) -> float:
    if uncertainty_scale == 1.0:
        projected_up = row["projected_up"]
    else:
        projected_up = normal_cdf(
            (row["projected"] - row["target"])
            / max(0.20, row["projection_std"] * uncertainty_scale)
        )
    return (
        (1.0 - market_weight) * projected_up
        + market_weight * row["market_up"]
    )


def run_case(grouped, params: dict, slippage=0.02) -> dict:
    trades = []
    for slug, rows in grouped.items():
        for row in rows:
            if row["seconds_left"] > params["max_seconds"]:
                continue
            p_up = probability(
                row, params["market_weight"], params["uncertainty_scale"]
            )
            side = "up" if p_up >= 0.5 else "down"
            fair = p_up if side == "up" else 1 - p_up
            ask = row[f"{side}_ask"]
            limit = min(0.99, ask + slippage)
            fee = 0.07 * limit * (1 - limit)
            if (
                ask > params["max_ask"]
                or fair < params["min_confidence"]
                or fair - limit - fee < params["min_edge"]
            ):
                continue
            future = row[f"{side}_future"]
            if future is None:
                continue
            fill = simulate_fok_fill(future[1], 5.0, limit, 0.07, 1.25)
            if fill is None:
                continue
            pnl = (5.0 if row["result"] == side else 0.0) - fill["total_cost"]
            trades.append({
                "slug": slug, "side": side, "result": row["result"],
                "seconds_left": row["seconds_left"], "ask": ask,
                "fill": fill["average_price"], "fair": fair, "pnl": pnl,
            })
            break
    wins = sum(row["side"] == row["result"] for row in trades)
    pnl = sum(row["pnl"] for row in trades)
    return {
        "trades": len(trades), "wins": wins, "losses": len(trades) - wins,
        "pnl": pnl, "roi": pnl / (5 * len(trades)) if trades else 0.0,
        "rows": trades,
    }


def subset(grouped, predicate):
    return {slug: rows for slug, rows in grouped.items() if predicate(rows[0]["ts"])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--twap", type=Path, required=True)
    parser.add_argument("--spot", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--books", type=Path, required=True)
    args = parser.parse_args()
    markets = read_markets(args.markets)
    twap_times, twap_values, exact = read_twap(args.twap)
    spot_times, spot_values = read_prices(args.spot)
    books = read_books(args.books, markets)
    grouped = observations(
        markets, books, twap_times, twap_values, exact, spot_times, spot_values
    )
    ordered = sorted(row[0]["ts"] for row in grouped.values())
    split_ts = ordered[len(ordered) // 2]
    first = subset(grouped, lambda ts: ts < split_ts)
    second = subset(grouped, lambda ts: ts >= split_ts)

    cases = []
    for max_seconds in (10, 20, 30, 45, 60):
        for max_ask in (0.68, 0.76, 0.84):
            for confidence in (0.70, 0.80, 0.90):
                for edge in (0.02, 0.05, 0.08):
                    for market_weight in (0.0, 0.25, 0.5):
                        for uncertainty in (1.0, 1.5):
                            params = {
                                "max_seconds": max_seconds, "max_ask": max_ask,
                                "min_confidence": confidence, "min_edge": edge,
                                "market_weight": market_weight,
                                "uncertainty_scale": uncertainty,
                            }
                            left = run_case(first, params)
                            right = run_case(second, params)
                            total = run_case(grouped, params)
                            if (
                                total["trades"] >= 8 and left["trades"] >= 3
                                and right["trades"] >= 3
                            ):
                                cases.append((
                                    min(left["roi"], right["roi"]), total["pnl"],
                                    params, left, right, total,
                                ))
    print(json.dumps({
        "markets": len(markets), "modeled_markets": len(grouped),
        "observations": sum(map(len, grouped.values())), "split_ts": split_ts,
    }, sort_keys=True))
    fixed_cases = {
        "archive_top": {
            "max_seconds": 60, "max_ask": 0.84, "min_confidence": 0.80,
            "min_edge": 0.02, "market_weight": 0.50, "uncertainty_scale": 1.0,
        },
        "live_top": {
            "max_seconds": 45, "max_ask": 0.76, "min_confidence": 0.80,
            "min_edge": 0.02, "market_weight": 0.25, "uncertainty_scale": 1.0,
        },
        "intersection": {
            "max_seconds": 45, "max_ask": 0.76, "min_confidence": 0.80,
            "min_edge": 0.02, "market_weight": 0.50, "uncertainty_scale": 1.0,
        },
    }
    for name, params in fixed_cases.items():
        results = [run_case(part, params) for part in (first, second, grouped)]
        print(json.dumps({
            "fixed": name, "params": params,
            "first": {key: value for key, value in results[0].items() if key != "rows"},
            "second": {key: value for key, value in results[1].items() if key != "rows"},
            "total": {key: value for key, value in results[2].items() if key != "rows"},
        }, sort_keys=True))
    for _, _, params, left, right, total in sorted(
        cases, key=lambda item: (item[0], item[1]), reverse=True
    )[:25]:
        print(json.dumps({
            "params": params,
            "first": {key: value for key, value in left.items() if key != "rows"},
            "second": {key: value for key, value in right.items() if key != "rows"},
            "total": {key: value for key, value in total.items() if key != "rows"},
        }, sort_keys=True))


if __name__ == "__main__":
    main()
