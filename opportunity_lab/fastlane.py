from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

from .config import Config
from .db import balance, ledger_entry
from .markets import as_float, parse_ts


log = logging.getLogger("opportunity_lab.fastlane")
USER_AGENT = "opportunitylab/0.2 paper-research contact=local-user"
_MARKET_CACHE: dict[str, tuple[float, dict]] = {}


def _json_get(url: str, timeout: int = 15):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class PriceStream:
    """Small in-memory bridge for Polymarket's public RTDS price stream."""

    def __init__(self):
        self._ticks = deque(maxlen=5000)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.error = ""
        self.connected = False

    def start(self) -> bool:
        try:
            import websocket  # type: ignore
        except ImportError:
            self.error = "websocket-client dependency is not installed"
            log.error(self.error)
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(websocket,), daemon=True, name="price-rtds"
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _run(self, websocket):
        delay = 1
        while not self._stop.is_set():
            def on_open(ws):
                self.connected = True
                self.error = ""
                ws.send(json.dumps({
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": json.dumps({"symbol": "btc/usd"}, separators=(",", ":")),
                        },
                        {
                            "topic": "crypto_prices",
                            "type": "update",
                            "filters": "btcusdt",
                        },
                    ],
                }, separators=(",", ":")))

            def on_message(_ws, message):
                try:
                    if not str(message).strip() or str(message).strip().upper() in {"PING", "PONG"}:
                        return
                    messages = json.loads(message)
                    if not isinstance(messages, list):
                        messages = [messages]
                    for item in messages:
                        payload = item.get("payload") or {}
                        value = as_float(payload.get("value"))
                        source_ms = as_float(payload.get("timestamp") or item.get("timestamp"))
                        symbol = str(payload.get("symbol") or "").lower()
                        topic = item.get("topic") or ""
                        if value is None or value <= 0 or source_ms is None:
                            continue
                        source = "chainlink" if topic == "crypto_prices_chainlink" else "binance"
                        tick = {
                            "source": source,
                            "symbol": symbol,
                            "source_ts": source_ms / 1000 if source_ms > 1e11 else source_ms,
                            "received_ts": time.time(),
                            "price": value,
                        }
                        with self._lock:
                            self._ticks.append(tick)
                except Exception as exc:
                    self.error = f"RTDS parse: {exc!r}"

            def on_error(_ws, error):
                self.error = repr(error)[:300]

            def on_close(_ws, _status, _message):
                self.connected = False

            app = websocket.WebSocketApp(
                "wss://ws-live-data.polymarket.com",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            try:
                # Control pings keep the lightweight client alive. The server
                # reconnect loop covers intermediaries that require text PING.
                app.run_forever(ping_interval=5, ping_timeout=3)
            except Exception as exc:
                self.error = repr(exc)[:300]
            self.connected = False
            if self._stop.wait(delay):
                break
            delay = min(30, delay * 2)

    def ticks(self) -> list[dict]:
        with self._lock:
            return list(self._ticks)


def current_slug(now: float | None = None) -> tuple[str, float, float]:
    now = time.time() if now is None else now
    start = now - (now % 300)
    return f"btc-updown-5m-{int(start)}", start, start + 300


def _decode_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def market_metadata(slug: str, fresh: bool = False) -> dict:
    cached = _MARKET_CACHE.get(slug)
    if cached and not fresh and time.time() - cached[0] < 600:
        return dict(cached[1])
    raw = _json_get(
        "https://gamma-api.polymarket.com/markets/slug/"
        + urllib.parse.quote(slug, safe="")
    )
    outcomes = [str(value).lower() for value in _decode_list(raw.get("outcomes"))]
    tokens = [str(value) for value in _decode_list(raw.get("clobTokenIds"))]
    token_by_side = dict(zip(outcomes, tokens))
    event = (raw.get("events") or [{}])[0]
    start_ts = parse_ts(event.get("startTime") or raw.get("eventStartTime"))
    end_ts = parse_ts(raw.get("endDate"))
    result = {
        "slug": slug,
        "title": raw.get("question") or slug,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "up_token": token_by_side.get("up"),
        "down_token": token_by_side.get("down"),
        "closed": bool(raw.get("closed")),
        "active": bool(raw.get("active")),
        "accepting_orders": bool(raw.get("acceptingOrders")),
        "outcomes": outcomes,
        "outcome_prices": _decode_list(raw.get("outcomePrices")),
        "fee_rate": float((raw.get("feeSchedule") or {}).get("rate") or 0.07),
        "min_order_size": float(raw.get("orderMinSize") or 5),
    }
    _MARKET_CACHE[slug] = (time.time(), result)
    return dict(result)


def best_ask(token_id: str) -> tuple[float | None, float]:
    book = _json_get(
        "https://clob.polymarket.com/book?"
        + urllib.parse.urlencode({"token_id": token_id})
    )
    levels = []
    for level in book.get("asks") or []:
        price = as_float(level.get("price"))
        size = as_float(level.get("size"), 0.0)
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            levels.append((price, size))
    return min(levels, default=(None, 0.0), key=lambda row: row[0])


def _source_ticks(ticks: list[dict], source: str, start: float, end: float | None = None):
    end = float("inf") if end is None else end
    return sorted(
        (tick for tick in ticks if tick["source"] == source and start <= tick["source_ts"] <= end),
        key=lambda tick: tick["source_ts"],
    )


def boundary_price(ticks: list[dict], source: str, boundary: float) -> float | None:
    # The first source tick at/after the boundary is reproducible and avoids
    # selecting with future information. Reject gaps rather than substituting
    # a different venue's spot price.
    candidates = _source_ticks(ticks, source, boundary, boundary + 5)
    return float(candidates[0]["price"]) if candidates else None


def latest_price(ticks: list[dict], source: str, now: float) -> float | None:
    candidates = _source_ticks(ticks, source, now - 10, now + 1)
    return float(candidates[-1]["price"]) if candidates else None


def volatility_per_s(ticks: list[dict], now: float, lookback: float) -> float | None:
    values = _source_ticks(ticks, "chainlink", now - lookback, now + 1)
    if len(values) < 8:
        return None
    squared = 0.0
    elapsed = 0.0
    previous = values[0]
    for current in values[1:]:
        dt = current["source_ts"] - previous["source_ts"]
        if 0 < dt <= 10:
            squared += math.log(current["price"] / previous["price"]) ** 2
            elapsed += dt
        previous = current
    return math.sqrt(squared / elapsed) if elapsed > 0 else None


def up_probability(
    current: float, target: float, seconds_left: float, sigma_per_s: float, shrink: float
) -> float:
    sigma = max(0.000005, sigma_per_s) * math.sqrt(max(0.25, seconds_left + 1.5))
    z = math.log(current / target) / sigma
    raw = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    probability = 0.5 + (raw - 0.5) * max(0.0, min(1.0, shrink))
    return max(0.03, min(0.97, probability))


def _store_ticks(db, ticks: list[dict]) -> int:
    latest = {
        row["source"]: float(row["latest"])
        for row in db.execute(
            "SELECT source,MAX(source_ts) AS latest FROM fast_price_ticks GROUP BY source"
        ).fetchall()
    }
    fresh = [
        tick for tick in ticks
        if tick["source_ts"] > latest.get(tick["source"], 0.0)
    ]
    before = db.total_changes
    db.executemany(
        """INSERT OR IGNORE INTO fast_price_ticks
        (source,symbol,source_ts,received_ts,price) VALUES(?,?,?,?,?)""",
        [
            (t["source"], t["symbol"], t["source_ts"], t["received_ts"], t["price"])
            for t in fresh
        ],
    )
    return db.total_changes - before


def _open_fast_trade(db, cfg: Config, market: dict, evidence: dict, side: str, ask: float, depth: float):
    if db.execute("SELECT 1 FROM fast_paper_trades WHERE slug=?", (market["slug"],)).fetchone():
        return None
    if db.execute(
        "SELECT COUNT(*) FROM fast_paper_trades WHERE status='open'"
    ).fetchone()[0] >= cfg.fast_max_open_trades:
        return None
    fair = evidence["up_probability"] if side == "up" else 1 - evidence["up_probability"]
    fee_per_share = market["fee_rate"] * ask * (1 - ask)
    unit_cost = ask + fee_per_share
    available = min(cfg.fast_trade_budget_usd, max(0.0, balance(db)))
    qty = min(depth, available / unit_cost)
    if qty < market["min_order_size"]:
        return None
    cost = qty * ask
    fee = qty * fee_per_share
    cur = db.execute(
        """INSERT INTO fast_paper_trades
        (slug,status,side,open_ts,seconds_left,qty,entry_price,entry_fee,
         probability,model_edge,price_to_beat,reference_price,evidence_json)
        VALUES(?,'open',?,?,?,?,?,?,?,?,?,?,?)""",
        (
            market["slug"], side, time.time(), evidence["seconds_left"], qty, ask, fee,
            fair, fair - unit_cost, evidence["price_to_beat"], evidence["chainlink_price"],
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        ),
    )
    trade_id = int(cur.lastrowid)
    ledger_entry(db, "fast_paper_open", -(cost + fee), trade_id, market["slug"])
    return trade_id


def settle_fast_trades(db) -> tuple[int, list[str]]:
    rows = db.execute(
        "SELECT * FROM fast_paper_trades WHERE status='open' AND open_ts<?",
        (time.time() - 60,),
    ).fetchall()
    settled = 0
    notable = []
    for row in rows:
        try:
            market = market_metadata(row["slug"], fresh=True)
        except Exception:
            log.exception("fast settlement lookup failed: %s", row["slug"])
            continue
        prices = [as_float(value) for value in market["outcome_prices"]]
        result = ""
        for outcome, price in zip(market["outcomes"], prices):
            if price is not None and price >= 0.999:
                result = outcome
        if not market["closed"] or result not in {"up", "down"}:
            continue
        payout = float(row["qty"]) if row["side"] == result else 0.0
        realized = payout - float(row["qty"]) * float(row["entry_price"]) - float(row["entry_fee"])
        db.execute(
            """UPDATE fast_paper_trades SET status='settled',close_ts=?,result=?,
            payout=?,realized_pnl=? WHERE id=?""",
            (time.time(), result, payout, realized, row["id"]),
        )
        ledger_entry(db, "fast_paper_settle", payout, row["id"], result)
        notable.append(f"⚡ PAPER BTC {row['side'].upper()} settled {result.upper()}: ${realized:+.2f}")
        settled += 1
    return settled, notable


def scan_fast(db, cfg: Config, stream: PriceStream) -> dict:
    ticks = stream.ticks()
    stored = _store_ticks(db, ticks)
    settled, notable = settle_fast_trades(db)
    if not cfg.enable_fast_btc:
        db.commit()
        return {"state": "disabled", "stored_ticks": stored, "settled": settled, "notable": notable}
    if not stream.connected:
        db.commit()
        return {
            "state": "stream_unavailable", "error": stream.error,
            "stored_ticks": stored, "settled": settled, "notable": notable,
        }

    now = time.time()
    slug, expected_start, expected_end = current_slug(now)
    seconds_left = expected_end - now
    if seconds_left > cfg.fast_active_window_seconds:
        db.commit()
        return {
            "state": "observing", "slug": slug, "seconds_left": round(seconds_left, 2),
            "ticks": len(ticks), "stored_ticks": stored, "settled": settled, "notable": notable,
        }
    try:
        market = market_metadata(slug)
        start_ts = market["start_ts"] or expected_start
        end_ts = market["end_ts"] or expected_end
        seconds_left = end_ts - now
        target = boundary_price(ticks, "chainlink", start_ts)
        chainlink = latest_price(ticks, "chainlink", now)
        binance_start = boundary_price(ticks, "binance", start_ts)
        binance = latest_price(ticks, "binance", now)
        sigma = volatility_per_s(ticks, now, cfg.fast_volatility_lookback_seconds)
        if None in {target, chainlink, sigma}:
            db.commit()
            return {
                "state": "insufficient_reference_history", "slug": slug,
                "seconds_left": round(seconds_left, 2), "stored_ticks": stored,
                "settled": settled, "notable": notable,
            }
        up_ask, up_depth = best_ask(market["up_token"])
        down_ask, down_depth = best_ask(market["down_token"])
    except Exception as exc:
        log.exception("fast market sample failed")
        db.commit()
        return {
            "state": "market_error", "slug": slug, "error": repr(exc)[:300],
            "stored_ticks": stored, "settled": settled, "notable": notable,
        }

    probability = up_probability(chainlink, target, seconds_left, sigma, cfg.fast_probability_shrink)
    margin_bps = 10_000 * (chainlink / target - 1)
    chain_direction = 1 if chainlink >= target else -1
    binance_direction = (
        (1 if binance >= binance_start else -1)
        if binance is not None and binance_start is not None
        else None
    )
    sources_agree = None if binance_direction is None else chain_direction == binance_direction
    evidence = {
        "slug": slug, "timestamp": now, "start_ts": start_ts, "end_ts": end_ts,
        "seconds_left": seconds_left, "price_to_beat": target,
        "chainlink_price": chainlink, "binance_start": binance_start,
        "binance_price": binance, "margin_bps": margin_bps,
        "sigma_per_s": sigma, "up_probability": probability,
        "up_ask": up_ask, "up_depth": up_depth, "down_ask": down_ask,
        "down_depth": down_depth, "sources_agree": sources_agree,
        "model": "shrunk lognormal terminal probability; paper only",
    }
    db.execute(
        """INSERT OR IGNORE INTO fast_market_samples
        (ts,slug,start_ts,end_ts,seconds_left,price_to_beat,reference_price,
         margin_bps,sigma_per_s,up_probability,up_ask,down_ask,evidence_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now, slug, start_ts, end_ts, seconds_left, target, chainlink,
            margin_bps, sigma, probability, up_ask, down_ask,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        ),
    )

    opened = None
    chosen = "up" if chain_direction > 0 else "down"
    ask, depth = (up_ask, up_depth) if chosen == "up" else (down_ask, down_depth)
    fair = probability if chosen == "up" else 1 - probability
    unit_fee = market["fee_rate"] * ask * (1 - ask) if ask is not None else float("inf")
    edge = fair - ask - unit_fee if ask is not None else -1
    required_edge = cfg.fast_min_edge + (
        cfg.fast_unconfirmed_edge_penalty if sources_agree is None else 0.0
    )
    eligible = (
        cfg.fast_min_seconds_left <= seconds_left <= cfg.fast_entry_window_seconds
        and abs(margin_bps) >= cfg.fast_min_margin_bps
        and sources_agree is not False
        and ask is not None and ask <= cfg.fast_max_ask
        and edge >= required_edge
    )
    if eligible:
        opened = _open_fast_trade(db, cfg, market, evidence, chosen, ask, depth)
        if opened:
            notable.append(
                f"⚡ PAPER BTC {chosen.upper()} at {ask:.2f}, {seconds_left:.1f}s left, model edge {edge:.1%}"
            )
    db.commit()
    return {
        "state": "paper_opened" if opened else "sampled",
        "slug": slug, "seconds_left": round(seconds_left, 2),
        "margin_bps": round(margin_bps, 3), "up_probability": round(probability, 4),
        "up_ask": up_ask, "down_ask": down_ask, "model_edge": round(edge, 4),
        "eligible": eligible, "required_edge": round(required_edge, 4),
        "trade_id": opened, "stored_ticks": stored,
        "settled": settled, "notable": notable,
    }
