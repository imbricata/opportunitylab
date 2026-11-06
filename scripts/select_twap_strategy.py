#!/usr/bin/env python3
"""Rank identical TWAP projection parameters across archive and live cohorts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from scripts.backtest_twap_native import read_markets, read_twap
from scripts.research_live_twap_projection import build_rows, run as run_live
from scripts.research_twap_projection import (
    observations,
    read_books,
    read_prices,
    run_case,
)


def split_grouped(grouped):
    slugs = sorted(grouped, key=lambda slug: grouped[slug][0]["ts"])
    midpoint = len(slugs) // 2
    return (
        {slug: grouped[slug] for slug in slugs[:midpoint]},
        {slug: grouped[slug] for slug in slugs[midpoint:]},
    )


def compact(result):
    return {key: value for key, value in result.items() if key != "rows"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--twap", type=Path, required=True)
    parser.add_argument("--spot", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--books", type=Path, required=True)
    parser.add_argument("--live-db", type=Path, required=True)
    parser.add_argument("--since", type=float, default=1789569600)
    args = parser.parse_args()

    markets = read_markets(args.markets)
    twap_times, twap_values, exact = read_twap(args.twap)
    spot_times, spot_values = read_prices(args.spot)
    archive = observations(
        markets, read_books(args.books, markets), twap_times, twap_values,
        exact, spot_times, spot_values,
    )
    db = sqlite3.connect(args.live_db)
    db.row_factory = sqlite3.Row
    live = build_rows(db, args.since)
    archive_first, archive_second = split_grouped(archive)
    live_first, live_second = split_grouped(live)

    ranked = []
    for max_seconds in (30, 45, 60):
        for max_ask in (0.68, 0.72, 0.76, 0.80, 0.84):
            for confidence in (0.75, 0.80, 0.85, 0.90):
                for edge in (0.02, 0.05, 0.08):
                    for market_weight in (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75):
                        params = {
                            "max_seconds": max_seconds, "max_ask": max_ask,
                            "min_confidence": confidence, "min_edge": edge,
                            "market_weight": market_weight,
                            "uncertainty_scale": 1.0,
                        }
                        archive_parts = [
                            run_case(archive_first, params),
                            run_case(archive_second, params),
                            run_case(archive, params),
                        ]
                        live_parts = [
                            run_live(live_first, params),
                            run_live(live_second, params),
                            run_live(live, params),
                        ]
                        archive_total, live_total = archive_parts[2], live_parts[2]
                        if (
                            archive_total["trades"] < 8
                            or live_total["immediate_trades"] < 3
                        ):
                            continue
                        segment_pnls = [
                            archive_parts[0]["pnl"], archive_parts[1]["pnl"],
                            live_parts[0]["immediate_pnl"],
                            live_parts[1]["immediate_pnl"],
                        ]
                        positive_segments = sum(value > 0 for value in segment_pnls)
                        combined_pnl = (
                            archive_total["pnl"] + live_total["immediate_pnl"]
                        )
                        total_trades = (
                            archive_total["trades"]
                            + live_total["immediate_trades"]
                        )
                        ranked.append((
                            positive_segments, min(segment_pnls),
                            combined_pnl / total_trades, combined_pnl,
                            params, archive_parts, live_parts,
                        ))

    print(json.dumps({
        "archive_markets": len(archive), "live_markets": len(live),
        "tested_viable_cases": len(ranked),
    }, sort_keys=True))
    for item in sorted(ranked, reverse=True, key=lambda row: row[:4])[:30]:
        positive, worst, average, combined, params, archive_parts, live_parts = item
        print(json.dumps({
            "positive_segments": positive, "worst_segment_pnl": worst,
            "combined_pnl": combined, "pnl_per_trade": average,
            "params": params,
            "archive_first": compact(archive_parts[0]),
            "archive_second": compact(archive_parts[1]),
            "archive_total": compact(archive_parts[2]),
            "live_first": live_parts[0], "live_second": live_parts[1],
            "live_total": live_parts[2],
        }, sort_keys=True))


if __name__ == "__main__":
    main()
