#!/usr/bin/env python3
"""Compare Polymarket REST and WebSocket books for the current BTC 5m market."""

from __future__ import annotations

import json
import time
import urllib.request

import websocket


def get_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "opportunitylab-diagnostic/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def rest_book(token: str) -> tuple[float | None, dict[float, float]]:
    raw = get_json(f"https://clob.polymarket.com/book?token_id={token}")
    asks = {
        float(level["price"]): float(level["size"])
        for level in raw.get("asks", [])
        if float(level["size"]) > 0
    }
    return (min(asks) if asks else None), asks


def main():
    start = int(time.time() // 300 * 300)
    slug = f"btc-updown-5m-{start}"
    market = get_json(f"https://gamma-api.polymarket.com/markets/slug/{slug}")
    outcomes = json.loads(market["outcomes"])
    tokens = json.loads(market["clobTokenIds"])
    outcome_by_token = dict(zip(tokens, outcomes))
    rest_initial = {token: rest_book(token)[0] for token in tokens}
    print(json.dumps({
        "slug": slug,
        "tokens": {outcome_by_token[token]: token for token in tokens},
        "rest_initial": {outcome_by_token[token]: rest_initial[token] for token in tokens},
    }, sort_keys=True), flush=True)

    books: dict[str, dict[float, float]] = {token: {} for token in tokens}
    mismatches = []
    samples = []
    ws = websocket.create_connection(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market", timeout=2
    )
    ws.send(json.dumps({
        "assets_ids": tokens, "type": "market", "custom_feature_enabled": True,
    }))
    deadline = time.time() + 35
    next_ping = time.time() + 8
    while time.time() < deadline:
        if time.time() >= next_ping:
            ws.send("PING")
            next_ping = time.time() + 8
        try:
            raw = ws.recv()
        except (TimeoutError, websocket.WebSocketTimeoutException):
            continue
        if str(raw).strip().upper() in {"PING", "PONG"}:
            continue
        payload = json.loads(raw)
        for item in payload if isinstance(payload, list) else [payload]:
            event = item.get("event_type")
            if event == "book":
                token = str(item.get("asset_id"))
                if token not in books:
                    continue
                books[token] = {
                    float(level["price"]): float(level["size"])
                    for level in item.get("asks", [])
                    if float(level["size"]) > 0
                }
                samples.append({
                    "event": "snapshot", "outcome": outcome_by_token[token],
                    "maintained_ask": min(books[token]) if books[token] else None,
                    "rest_ask": rest_initial[token],
                })
                print(json.dumps(samples[-1], sort_keys=True), flush=True)
            elif event == "price_change":
                for change in item.get("price_changes", []):
                    token = str(change.get("asset_id"))
                    if token not in books:
                        continue
                    print(json.dumps({
                        "event": "price_change", "outcome": outcome_by_token[token],
                        "side": change.get("side"), "price": change.get("price"),
                        "size": change.get("size"), "best_bid": change.get("best_bid"),
                        "best_ask": change.get("best_ask"),
                    }, sort_keys=True), flush=True)
                    if str(change.get("side")).upper() != "SELL":
                        continue
                    price = float(change["price"])
                    size = float(change["size"])
                    if size > 0:
                        books[token][price] = size
                    else:
                        books[token].pop(price, None)
                    maintained = min(books[token]) if books[token] else None
                    advertised = float(change["best_ask"]) if change.get("best_ask") else None
                    if maintained != advertised:
                        mismatches.append({
                            "outcome": outcome_by_token[token], "side": change.get("side"),
                            "price": price, "size": size, "maintained": maintained,
                            "advertised_best_ask": advertised,
                        })
                        print(json.dumps({"mismatch": mismatches[-1]}, sort_keys=True), flush=True)
    ws.close()
    rest_final = {}
    for token in tokens:
        try:
            rest_final[token] = rest_book(token)[0]
        except Exception:
            rest_final[token] = None
    print(json.dumps({
        "snapshots": samples,
        "maintained_final": {
            outcome_by_token[token]: min(books[token]) if books[token] else None
            for token in tokens
        },
        "rest_final": {outcome_by_token[token]: rest_final[token] for token in tokens},
        "price_change_mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
