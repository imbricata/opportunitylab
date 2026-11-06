#!/usr/bin/env python3
"""One-shot live validation for the hardened Polymarket book path."""

from __future__ import annotations

import json
import time

from opportunity_lab.fastlane import (
    MarketBookStream,
    best_ask,
    current_slug,
    market_metadata,
)


def main():
    slug, _, _ = current_slug()
    market = market_metadata(slug, verify_tokens=True)
    stream = MarketBookStream()
    stream.start()
    stream.set_market(slug, market["up_token"], market["down_token"])
    stream.activate_market(slug)
    deadline = time.time() + 12
    quotes = {}
    while time.time() < deadline:
        quotes = {
            side: stream.quote(side, max_silence=5, max_book_age=2)
            for side in ("up", "down")
        }
        if all(quotes.values()):
            break
        time.sleep(0.1)
    stream.stop()
    rest = {
        side: best_ask(market[f"{side}_token"])[0]
        for side in ("up", "down")
    }
    websocket_prices = {
        side: quote["price"] if quote else None for side, quote in quotes.items()
    }
    have_all = all(websocket_prices.values()) and all(rest.values())
    direct_distance = (
        sum(abs(websocket_prices[side] - rest[side]) for side in ("up", "down"))
        if have_all else None
    )
    swapped_distance = (
        abs(websocket_prices["up"] - rest["down"])
        + abs(websocket_prices["down"] - rest["up"])
        if have_all else None
    )
    result = {
        "slug": slug,
        "token_mapping_verified": market["token_mapping_verified"],
        "gamma_token_mapping_agrees": market["gamma_token_mapping_agrees"],
        "websocket": websocket_prices,
        "rest": rest,
        "direct_distance": direct_distance,
        "swapped_distance": swapped_distance,
        "orientation_matches": (
            have_all and direct_distance <= swapped_distance + 1e-9
        ),
    }
    print(json.dumps(result, sort_keys=True))
    if not result["token_mapping_verified"] or not result["orientation_matches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
