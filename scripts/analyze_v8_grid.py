from __future__ import annotations

import sqlite3
import sys


def metrics(rows, oracle_weight, threshold, max_seconds):
    chosen = {}
    for row in rows:
        if row["slug"] in chosen or not (2.5 <= row["seconds_left"] <= max_seconds):
            continue
        if row["ask"] is None or row["up_ask"] is None or row["down_ask"] is None:
            continue
        if abs(row["margin_bps"] or 0) < 2.5 or row["sources_agree"] == 0:
            continue
        if not row["selected_live_book"] or row["ask"] > 0.92:
            continue
        market_up = (row["up_ask"] + 1.0 - row["down_ask"]) / 2.0
        market_fair = market_up if row["side"] == "up" else 1.0 - market_up
        fair = oracle_weight * row["fair_value"] + (1.0 - oracle_weight) * market_fair
        fee = 0.07 * row["ask"] * (1.0 - row["ask"])
        if fair - row["ask"] - fee < threshold:
            continue
        # The two observed live/paper pairs paid 1-4 cents/share more. Charge
        # two cents to every counterfactual instead of optimizing on paper fills.
        cost = min(0.999, row["ask"] + fee + 0.02)
        pnl = 1.0 - cost if row["side"] == row["venue_result"] else -cost
        chosen[row["slug"]] = (pnl, cost, row["side"] == row["venue_result"])
    n = len(chosen)
    pnl = sum(item[0] for item in chosen.values())
    risk = sum(item[1] for item in chosen.values())
    wins = sum(item[2] for item in chosen.values())
    return n, wins, pnl, (pnl / risk if risk else 0.0)


def main(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    start = float(db.execute(
        "SELECT value FROM settings WHERE key='fast_livebook_experiment_start'"
    ).fetchone()[0])
    rows = db.execute(
        """SELECT s.ts,s.slug,s.side,s.seconds_left,s.ask,s.fair_value,
        w.venue_result,
        CAST(json_extract(s.evidence_json,'$.up_ask') AS REAL) up_ask,
        CAST(json_extract(s.evidence_json,'$.down_ask') AS REAL) down_ask,
        CAST(json_extract(s.evidence_json,'$.margin_bps') AS REAL) margin_bps,
        CAST(json_extract(s.evidence_json,'$.sources_agree') AS INTEGER) sources_agree,
        CAST(json_extract(s.evidence_json,'$.selected_live_book') AS INTEGER)
          selected_live_book
        FROM fast_screen_decisions s JOIN fast_window_results w USING(slug)
        WHERE s.ts>=? AND w.venue_result IN ('up','down')
        ORDER BY s.slug,s.ts""",
        (start,),
    ).fetchall()
    slugs = sorted({row["slug"] for row in rows})
    cutoff = slugs[int(0.70 * len(slugs))]
    train = [row for row in rows if row["slug"] < cutoff]
    valid = [row for row in rows if row["slug"] >= cutoff]
    results = []
    for weight in (0.0, 0.1, 0.2, 0.3, 0.5):
        for threshold in (0.02, 0.04, 0.06, 0.08, 0.10):
            for seconds in (5, 8, 12, 20):
                a = metrics(train, weight, threshold, seconds)
                b = metrics(valid, weight, threshold, seconds)
                if a[0] >= 5 and b[0] >= 5 and a[2] > 0 and b[2] > 0:
                    results.append((min(a[3], b[3]), weight, threshold, seconds, a, b))
    for _, weight, threshold, seconds, train_m, valid_m in sorted(results, reverse=True)[:20]:
        print({
            "oracle_weight": weight,
            "edge": threshold,
            "max_seconds": seconds,
            "train": tuple(round(value, 3) if isinstance(value, float) else value for value in train_m),
            "valid": tuple(round(value, 3) if isinstance(value, float) else value for value in valid_m),
        })
    print({"resolved_rows": len(rows), "candidate_configs": len(results), "cutoff_slug": cutoff})


if __name__ == "__main__":
    main(sys.argv[1])
