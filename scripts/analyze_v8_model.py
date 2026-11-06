from __future__ import annotations

import math
import sqlite3
import sys


def clamp(value, low=1e-5, high=1 - 1e-5):
    return max(low, min(high, value))


def sigmoid(value):
    value = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def fit_logistic(xs, ys, l2=0.25, steps=5000, rate=0.04):
    weights = [0.0] * (len(xs[0]) + 1)
    n = len(xs)
    for _ in range(steps):
        gradient = [0.0] * len(weights)
        for features, target in zip(xs, ys):
            prediction = sigmoid(weights[0] + sum(w * x for w, x in zip(weights[1:], features)))
            error = prediction - target
            gradient[0] += error
            for index, value in enumerate(features, 1):
                gradient[index] += error * value
        for index in range(1, len(weights)):
            gradient[index] += l2 * weights[index]
        for index in range(len(weights)):
            weights[index] -= rate * gradient[index] / n
    return weights


def predict(weights, features):
    return sigmoid(weights[0] + sum(w * x for w, x in zip(weights[1:], features)))


def score(rows, weights, threshold):
    pnl = risk = 0.0
    wins = trades = 0
    for row in rows:
        probability = predict(weights, row["features"])
        up_cost = row["up_ask"] + 0.07 * row["up_ask"] * (1 - row["up_ask"]) + 0.02
        down_cost = row["down_ask"] + 0.07 * row["down_ask"] * (1 - row["down_ask"]) + 0.02
        candidates = [
            (probability - up_cost, "up", up_cost),
            ((1 - probability) - down_cost, "down", down_cost),
        ]
        edge, side, cost = max(candidates)
        if edge < threshold or cost >= 0.98:
            continue
        won = side == row["result"]
        pnl += 1 - cost if won else -cost
        risk += cost
        trades += 1
        wins += won
    return trades, wins, pnl, pnl / risk if risk else 0.0


def score_confidence_cap(rows, weights, confidence, ask_cap):
    pnl = risk = 0.0
    wins = trades = 0
    for row in rows:
        probability = predict(weights, row["features"])
        side = "up" if probability >= 0.5 else "down"
        predicted = probability if side == "up" else 1 - probability
        ask = row["up_ask"] if side == "up" else row["down_ask"]
        if predicted < confidence or ask > ask_cap:
            continue
        cost = min(0.999, ask + 0.07 * ask * (1 - ask) + 0.02)
        won = side == row["result"]
        pnl += 1 - cost if won else -cost
        risk += cost
        trades += 1
        wins += won
    return trades, wins, pnl, pnl / risk if risk else 0.0


def main(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    start = float(db.execute(
        "SELECT value FROM settings WHERE key='fast_livebook_experiment_start'"
    ).fetchone()[0])
    raw = db.execute(
        """WITH ranked AS (
          SELECT s.*,w.venue_result,
          ROW_NUMBER() OVER(PARTITION BY s.slug ORDER BY ABS(s.seconds_left-8)) rn
          FROM fast_market_samples s JOIN fast_window_results w USING(slug)
          WHERE s.ts>=? AND s.up_ask IS NOT NULL AND s.down_ask IS NOT NULL
            AND w.venue_result IN ('up','down')
        ) SELECT * FROM ranked WHERE rn=1 AND ABS(seconds_left-8)<=3 ORDER BY slug""",
        (start,),
    ).fetchall()
    prepared = []
    for row in raw:
        oracle = clamp(float(row["up_probability"]))
        market = clamp((float(row["up_ask"]) + 1 - float(row["down_ask"])) / 2)
        spread = float(row["up_ask"]) + float(row["down_ask"]) - 1
        prepared.append({
            "slug": row["slug"],
            "raw": [
                math.log(oracle / (1 - oracle)),
                math.log(market / (1 - market)),
                spread,
                abs(float(row["margin_bps"] or 0)),
                float(row["sigma_per_s"] or 0) * 10000,
            ],
            "up_ask": float(row["up_ask"]),
            "down_ask": float(row["down_ask"]),
            "result": row["venue_result"],
            "target": 1.0 if row["venue_result"] == "up" else 0.0,
        })
    split = int(0.70 * len(prepared))
    train, valid = prepared[:split], prepared[split:]
    means = [sum(row["raw"][i] for row in train) / len(train) for i in range(5)]
    scales = []
    for i, mean in enumerate(means):
        variance = sum((row["raw"][i] - mean) ** 2 for row in train) / len(train)
        scales.append(max(1e-9, math.sqrt(variance)))
    for row in prepared:
        row["features"] = [(value - mean) / scale for value, mean, scale in zip(row["raw"], means, scales)]
    weights = fit_logistic(
        [row["features"] for row in train], [row["target"] for row in train]
    )
    for label, rows in (("train", train), ("valid", valid)):
        predictions = [predict(weights, row["features"]) for row in rows]
        brier = sum((p - row["target"]) ** 2 for p, row in zip(predictions, rows)) / len(rows)
        accuracy = sum((p >= 0.5) == bool(row["target"]) for p, row in zip(predictions, rows)) / len(rows)
        print(label, {"n": len(rows), "brier": round(brier, 4), "accuracy": round(accuracy, 4)})
        for threshold in (0.02, 0.04, 0.06, 0.08, 0.10):
            values = score(rows, weights, threshold)
            print(label, threshold, tuple(round(v, 4) if isinstance(v, float) else v for v in values))
    robust = []
    for confidence in (0.60, 0.70, 0.80, 0.90, 0.95):
        for ask_cap in (0.50, 0.60, 0.70, 0.80, 0.90):
            a = score_confidence_cap(train, weights, confidence, ask_cap)
            b = score_confidence_cap(valid, weights, confidence, ask_cap)
            if a[0] >= 5 and b[0] >= 5 and a[2] > 0 and b[2] > 0:
                robust.append((min(a[3], b[3]), confidence, ask_cap, a, b))
    for _, confidence, ask_cap, a, b in sorted(robust, reverse=True):
        print("robust", confidence, ask_cap, a, b)
    print({"robust_configs": len(robust)})
    print({"weights": [round(value, 5) for value in weights], "means": means, "scales": scales})


if __name__ == "__main__":
    main(sys.argv[1])
