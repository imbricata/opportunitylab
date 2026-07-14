from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


INFO_URL = "https://api.hyperliquid.xyz/info"


def info(payload: dict, timeout: int = 20):
    body = json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(
        INFO_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "opportunity-lab/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


@dataclass(frozen=True)
class PairSnapshot:
    coin: str
    spot_coin: str
    funding_rate: float
    perp_mark: float
    spot_mark: float
    perp_day_volume: float
    spot_day_volume: float

    @property
    def basis_pct(self) -> float:
        return self.perp_mark / self.spot_mark - 1.0


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def matching_spot_perps() -> list[PairSnapshot]:
    perp_meta, perp_ctxs = info({"type": "metaAndAssetCtxs"})
    spot_meta, spot_ctxs = info({"type": "spotMetaAndAssetCtxs"})

    perps = {}
    for market, ctx in zip(perp_meta.get("universe", []), perp_ctxs):
        name = market.get("name")
        mark = _f(ctx.get("markPx"))
        if name and mark > 0 and not market.get("isDelisted", False):
            perps[name] = (ctx, mark)

    token_names = {t["index"]: t.get("name") for t in spot_meta.get("tokens", [])}
    pairs: list[PairSnapshot] = []
    for market, ctx in zip(spot_meta.get("universe", []), spot_ctxs):
        tokens = market.get("tokens") or []
        if len(tokens) != 2 or token_names.get(tokens[1]) != "USDC":
            continue
        base = token_names.get(tokens[0])
        if not base or base not in perps:
            continue
        spot_mark = _f(ctx.get("markPx") or ctx.get("midPx"))
        if spot_mark <= 0:
            continue
        perp_ctx, perp_mark = perps[base]
        # Hyperliquid requires @index for non-PURR spot books.
        spot_coin = market.get("name") if "/" in market.get("name", "") else f"@{market['index']}"
        pairs.append(
            PairSnapshot(
                coin=base,
                spot_coin=spot_coin,
                funding_rate=_f(perp_ctx.get("funding")),
                perp_mark=perp_mark,
                spot_mark=spot_mark,
                perp_day_volume=_f(perp_ctx.get("dayNtlVlm")),
                spot_day_volume=_f(ctx.get("dayNtlVlm")),
            )
        )
    return pairs


def l2_book(coin: str) -> dict:
    return info({"type": "l2Book", "coin": coin})


def average_price(levels: list[dict], qty: float) -> float | None:
    """Volume-weighted price for exactly qty, or None if visible depth is insufficient."""
    remaining = qty
    notional = 0.0
    for level in levels:
        size = _f(level.get("sz"))
        price = _f(level.get("px"))
        take = min(remaining, size)
        if take > 0 and price > 0:
            notional += take * price
            remaining -= take
        if remaining <= 1e-12:
            return notional / qty
    return None


def executable_open_prices(
    spot_book: dict, perp_book: dict, target_notional: float
) -> tuple[float, float, float] | None:
    """Return matched qty, spot buy VWAP, and perp short VWAP."""
    spot_asks = (spot_book.get("levels") or [[], []])[1]
    perp_bids = (perp_book.get("levels") or [[], []])[0]
    if not spot_asks or not perp_bids:
        return None
    reference = max(_f(spot_asks[0].get("px")), _f(perp_bids[0].get("px")))
    if reference <= 0:
        return None
    qty = target_notional / reference
    spot_buy = average_price(spot_asks, qty)
    perp_sell = average_price(perp_bids, qty)
    if spot_buy is None or perp_sell is None:
        return None
    return qty, spot_buy, perp_sell


def executable_close_prices(
    spot_book: dict, perp_book: dict, qty: float
) -> tuple[float, float] | None:
    """Return spot sell VWAP and perp short-cover VWAP."""
    spot_bids = (spot_book.get("levels") or [[], []])[0]
    perp_asks = (perp_book.get("levels") or [[], []])[1]
    spot_sell = average_price(spot_bids, qty)
    perp_buy = average_price(perp_asks, qty)
    if spot_sell is None or perp_buy is None:
        return None
    return spot_sell, perp_buy

