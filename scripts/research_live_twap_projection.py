#!/usr/bin/env python3
"""Out-of-sample projection audit over the production observation database."""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from opportunity_lab.fastlane import market_midpoint_up_probability
from scripts.research_twap_projection import (
    log_sigma,
    mean_between,
    projection_probability,
    value_at,
)


def load_ticks(db, source: str):
    rows = db.execute(
        """SELECT source_ts,price FROM fast_price_ticks
        WHERE source=? ORDER BY source_ts""",
        (source,),
    ).fetchall()
    return [float(row[0]) for row in rows], [float(row[1]) for row in rows]


def build_rows(db, since: float):
    twap_times, twap_values = load_ticks(db, "chainlink_twap60")
    spot_times, spot_values = load_ticks(db, "chainlink")
    rows = db.execute(
        """SELECT s.*,w.venue_result FROM fast_market_samples s
        JOIN fast_window_results w USING(slug)
        WHERE s.ts>=? AND w.venue_result IN ('up','down')
        AND json_extract(s.evidence_json,'$.settlement_source')='chainlink_twap60'
        ORDER BY s.slug,s.ts""",
        (since,),
    ).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        evidence = json.loads(row["evidence_json"])
        seconds_left = float(row["seconds_left"])
        if not 5 <= seconds_left <= 60:
            continue
        ts = float(row["ts"])
        current_twap = value_at(twap_times, twap_values, ts)
        current_spot = value_at(spot_times, spot_values, ts)
        outgoing = mean_between(
            spot_times, spot_values, ts - 60, float(row["end_ts"]) - 60
        )
        sigma = log_sigma(spot_times, spot_values, ts)
        if None in {current_twap, current_spot, outgoing, sigma}:
            continue
        up_ask, down_ask = evidence.get("up_ask"), evidence.get("down_ask")
        market_up = market_midpoint_up_probability(up_ask, down_ask)
        if market_up is None:
            continue
        projected_up, projected, std = projection_probability(
            current_twap=current_twap, target=float(row["price_to_beat"]),
            current_spot=current_spot, outgoing_spot=outgoing,
            seconds_left=seconds_left, spot_sigma=sigma,
            uncertainty_scale=1.0,
        )
        grouped[row["slug"]].append({
            "ts": ts, "seconds_left": seconds_left,
            "projected_up": projected_up, "market_up": market_up,
            "up_ask": float(up_ask), "down_ask": float(down_ask),
            "up_depth": float(evidence.get("up_depth") or 0),
            "down_depth": float(evidence.get("down_depth") or 0),
            "result": row["venue_result"],
        })
    return grouped


def run(grouped, params: dict):
    immediate, delayed = [], []
    for slug, rows in grouped.items():
        candidate = None
        for row in rows:
            if row["seconds_left"] > params["max_seconds"]:
                continue
            p_up = (
                (1 - params["market_weight"]) * row["projected_up"]
                + params["market_weight"] * row["market_up"]
            )
            side = "up" if p_up >= 0.5 else "down"
            fair = p_up if side == "up" else 1 - p_up
            ask = row[f"{side}_ask"]
            depth = row[f"{side}_depth"]
            limit = min(0.99, ask + 0.02)
            fee = 0.07 * limit * (1 - limit)
            if (
                ask <= params["max_ask"]
                and fair >= params["min_confidence"]
                and fair - limit - fee >= params["min_edge"]
                and depth >= 6.25
            ):
                candidate = dict(row, side=side, fair=fair, ask=ask, limit=limit)
                break
        if candidate is None:
            continue
        fee = 0.07 * candidate["ask"] * (1 - candidate["ask"])
        cost = 5 * (candidate["ask"] + fee)
        immediate.append(
            (5 if candidate["side"] == candidate["result"] else 0) - cost
        )
        times = [row["ts"] for row in rows]
        index = bisect.bisect_left(times, candidate["ts"] + 0.35)
        if index < len(rows) and rows[index]["ts"] <= candidate["ts"] + 4.0:
            later = rows[index]
            # Re-selecting the side would be hindsight. Keep the original side.
            later_ask = later.get(f"{candidate['side']}_ask")
            later_depth = float(later.get(f"{candidate['side']}_depth") or 0)
            if (
                later_ask is not None and float(later_ask) <= candidate["limit"]
                and later_depth >= 6.25
            ):
                later_ask = float(later_ask)
                later_fee = 0.07 * later_ask * (1 - later_ask)
                later_cost = 5 * (later_ask + later_fee)
                delayed.append(
                    (5 if candidate["side"] == candidate["result"] else 0)
                    - later_cost
                )
    return {
        "markets": len(grouped),
        "immediate_trades": len(immediate),
        "immediate_wins": sum(value > 0 for value in immediate),
        "immediate_pnl": sum(immediate),
        "delayed_proxy_trades": len(delayed),
        "delayed_proxy_wins": sum(value > 0 for value in delayed),
        "delayed_proxy_pnl": sum(delayed),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--since", type=float, default=1789569600)
    args = parser.parse_args()
    db = sqlite3.connect(args.database)
    db.row_factory = sqlite3.Row
    grouped = build_rows(db, args.since)
    slugs = sorted(grouped, key=lambda slug: grouped[slug][0]["ts"])
    midpoint = len(slugs) // 2
    first = {slug: grouped[slug] for slug in slugs[:midpoint]}
    second = {slug: grouped[slug] for slug in slugs[midpoint:]}
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
        print(json.dumps({
            "fixed": name, "params": params,
            "first": run(first, params), "second": run(second, params),
            "total": run(grouped, params),
        }, sort_keys=True))
    cases = []
    for max_seconds in (20, 30, 45, 60):
        for max_ask in (0.68, 0.76, 0.84):
            for confidence in (0.70, 0.80, 0.90):
                for edge in (0.02, 0.05, 0.08):
                    for market_weight in (0.25, 0.5, 0.75):
                        params = {
                            "max_seconds": max_seconds, "max_ask": max_ask,
                            "min_confidence": confidence, "min_edge": edge,
                            "market_weight": market_weight,
                            "uncertainty_scale": 1.0,
                        }
                        result = run(grouped, params)
                        cases.append((result["delayed_proxy_pnl"], params, result))
    for _, params, result in sorted(cases, key=lambda row: row[0], reverse=True)[:20]:
        print(json.dumps({"params": params, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
