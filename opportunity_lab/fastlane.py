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
from .db import (
    BTC_V8_STRATEGY_ID,
    FAST_EXPERIMENT_VERSION,
    FAST_SHADOW_VERSION,
    fast_experiment_balance,
    fast_experiment_start,
    fast_shadow_balance,
    ledger_entry,
)
from .markets import as_float, parse_ts


log = logging.getLogger("opportunity_lab.fastlane")
USER_AGENT = "opportunitylab/0.2 paper-research contact=local-user"
_MARKET_CACHE: dict[str, tuple[float, dict]] = {}
V8_WEIGHTS = (-0.33948, 0.79366, 2.28177, -0.09376, 0.13831, 0.03189)
V8_MEANS = (-0.1411542559, -0.1315965892, 0.0197558528, 2.5221996870, 0.2116332439)
V8_SCALES = (0.9791949002, 3.1578595996, 0.0262463998, 2.4173332637, 0.1522968872)


def _json_get(url: str, timeout: int = 15):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class PriceStream:
    """In-memory BTC tape: settlement oracles plus independent spot venues."""

    def __init__(self):
        # Multiple trade feeds can emit thousands of updates per minute. Keep
        # enough history that the mandatory five-minute Chainlink boundary is
        # not evicted before the final-seconds decision.
        self._ticks = deque(maxlen=30000)
        self._pending = deque()
        self._lock = threading.Lock()
        self._threads = []
        self._apps = []
        self._stop = threading.Event()
        self.error = ""
        self.connected = False
        self.source_status = {
            "chainlink": False, "chainlink_twap60": False,
            "binance": False, "coinbase": False, "kraken": False,
        }

    def start(self) -> bool:
        try:
            import websocket  # type: ignore
        except ImportError:
            self.error = "websocket-client dependency is not installed"
            log.error(self.error)
            return False
        if any(thread.is_alive() for thread in self._threads):
            return True
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=target, args=(websocket,), daemon=True, name=name
            )
            for name, target in (
                ("price-rtds", self._run_rtds),
                ("price-twap60", self._run_twap60),
                ("price-coinbase", self._run_coinbase),
                ("price-kraken", self._run_kraken),
            )
        ]
        for thread in self._threads:
            thread.start()
        return True

    def stop(self):
        self._stop.set()
        for app in list(self._apps):
            try:
                app.close()
            except Exception:
                pass

    def _append(self, source: str, symbol: str, source_ts: float, price: float):
        if not math.isfinite(price) or price <= 0:
            return
        tick = {
            "source": source,
            "symbol": symbol,
            "source_ts": source_ts,
            "received_ts": time.time(),
            "price": price,
        }
        with self._lock:
            self._ticks.append(tick)
            self._pending.append(tick)
            self.source_status[source] = True
            if source in {"chainlink", "chainlink_twap60"}:
                self.connected = True

    def _socket_error(self, source: str, error):
        self.error = f"{source}: {error!r}"[:300]
        log.warning("%s websocket error: %r", source, error)

    @staticmethod
    def _timestamp(value) -> float | None:
        numeric = as_float(value)
        if numeric is not None:
            return numeric / 1000 if numeric > 1e11 else numeric
        return parse_ts(value)

    def _reconnecting(self, websocket, source: str, factory):
        delay = 1
        while not self._stop.is_set():
            app = factory(websocket)
            self._apps.append(app)
            try:
                app.run_forever(ping_interval=20, ping_timeout=8)
            except Exception as exc:
                self.error = f"{source}: {exc!r}"[:300]
            finally:
                if app in self._apps:
                    self._apps.remove(app)
                self.source_status[source] = False
                if source in {"chainlink", "chainlink_twap60"}:
                    self.connected = any(
                        self.source_status.get(item, False)
                        for item in ("chainlink", "chainlink_twap60")
                    )
            if self._stop.wait(delay):
                break
            delay = min(30, delay * 2)

    def _run_rtds(self, websocket):
        def factory(websocket_module):
            def on_open(ws):
                self.connected = True
                self.source_status["chainlink"] = True
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
                        source_ts = self._timestamp(payload.get("timestamp") or item.get("timestamp"))
                        symbol = str(payload.get("symbol") or "").lower()
                        topic = item.get("topic") or ""
                        if value is None or source_ts is None:
                            continue
                        source = "chainlink" if topic == "crypto_prices_chainlink" else "binance"
                        self._append(source, symbol or "btc/usd", source_ts, value)
                except Exception as exc:
                    self.error = f"RTDS parse: {exc!r}"

            def on_error(_ws, error):
                self._socket_error("chainlink-rtds", error)

            def on_close(_ws, _status, _message):
                self.source_status["chainlink"] = False
                self.connected = self.source_status.get("chainlink_twap60", False)

            return websocket_module.WebSocketApp(
                "wss://ws-live-data.polymarket.com",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
        self._reconnecting(websocket, "chainlink", factory)

    def _run_twap60(self, websocket):
        """Collect the exact Chainlink 60-second TWAP used by current BTC 5m markets."""
        def factory(websocket_module):
            def on_open(ws):
                self.error = ""
                ws.send(json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "crypto_prices_twap_sixty",
                        "type": "*",
                        "filters": json.dumps(
                            {"symbol": "btc/usd"}, separators=(",", ":")
                        ),
                    }],
                }, separators=(",", ":")))

            def on_message(_ws, message):
                try:
                    if not str(message).strip() or str(message).strip().upper() in {"PING", "PONG"}:
                        return
                    messages = json.loads(message)
                    if not isinstance(messages, list):
                        messages = [messages]
                    for item in messages:
                        self._ingest_twap60(item)
                except Exception as exc:
                    self.error = f"Chainlink TWAP60 parse: {exc!r}"[:300]

            return websocket_module.WebSocketApp(
                "wss://ws-live-data.polymarket.com",
                on_open=on_open,
                on_message=on_message,
                on_error=lambda _ws, error: self._socket_error(
                    "chainlink-twap60-rtds", error
                ),
            )
        # RTDS currently requires TWAP and spot topics on separate sockets.
        self._reconnecting(websocket, "chainlink_twap60", factory)

    def _ingest_twap60(self, item: dict) -> int:
        """Parse either an RTDS history snapshot or a single TWAP update."""
        if item.get("topic") != "crypto_prices_twap_sixty":
            return 0
        payload = item.get("payload") or {}
        if int(as_float(payload.get("window_s"), 60) or 60) != 60:
            return 0
        points = payload.get("data")
        if not isinstance(points, list):
            points = [payload]
        symbol = str(payload.get("symbol") or "btc/usd").lower()
        inserted = 0
        for point in points:
            if not isinstance(point, dict):
                continue
            value = as_float(point.get("value"))
            source_ts = self._timestamp(point.get("timestamp"))
            if value is None or source_ts is None:
                continue
            self._append("chainlink_twap60", symbol, source_ts, value)
            inserted += 1
        return inserted

    def _run_coinbase(self, websocket):
        def factory(websocket_module):
            def on_open(ws):
                ws.send(json.dumps({
                    "type": "subscribe", "product_ids": ["BTC-USD"],
                    "channels": ["ticker", "heartbeat"],
                }, separators=(",", ":")))

            def on_message(_ws, message):
                try:
                    item = json.loads(message)
                    if item.get("type") != "ticker" or item.get("product_id") != "BTC-USD":
                        return
                    price = as_float(item.get("price"))
                    source_ts = self._timestamp(item.get("time"))
                    if price is not None and source_ts is not None:
                        self._append("coinbase", "btc/usd", source_ts, price)
                except Exception as exc:
                    self.error = f"Coinbase parse: {exc!r}"[:300]

            return websocket_module.WebSocketApp(
                "wss://ws-feed.exchange.coinbase.com",
                on_open=on_open, on_message=on_message,
                on_error=lambda _ws, error: self._socket_error("coinbase", error),
            )
        self._reconnecting(websocket, "coinbase", factory)

    def _run_kraken(self, websocket):
        def factory(websocket_module):
            def on_open(ws):
                ws.send(json.dumps({
                    "method": "subscribe",
                    "params": {"channel": "ticker", "symbol": ["BTC/USD"],
                               "event_trigger": "trades", "snapshot": True},
                }, separators=(",", ":")))

            def on_message(_ws, message):
                try:
                    item = json.loads(message)
                    if item.get("channel") != "ticker" or not item.get("data"):
                        return
                    ticker = item["data"][0]
                    price = as_float(ticker.get("last"))
                    source_ts = self._timestamp(ticker.get("timestamp"))
                    if price is not None and source_ts is not None:
                        self._append("kraken", "btc/usd", source_ts, price)
                except Exception as exc:
                    self.error = f"Kraken parse: {exc!r}"[:300]

            return websocket_module.WebSocketApp(
                "wss://ws.kraken.com/v2",
                on_open=on_open, on_message=on_message,
                on_error=lambda _ws, error: self._socket_error("kraken", error),
            )
        self._reconnecting(websocket, "kraken", factory)

    def ticks(self, since: float | None = None) -> list[dict]:
        with self._lock:
            if since is None:
                return list(self._ticks)
            return [tick for tick in self._ticks if tick["source_ts"] >= since]

    def drain_pending(self) -> list[dict]:
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
            return pending


class MarketBookStream:
    """Public Polymarket level-2 book, rotated to the current five-minute market."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._thread = None
        self._app = None
        self._market = None
        self._assets: dict[str, str] = {}
        self._asks: dict[str, dict[float, float]] = {}
        self._best_ask: dict[str, float | None] = {}
        self._book_valid: dict[str, bool] = {}
        self._updated: dict[str, tuple[float, float]] = {}
        self._last_data_received = 0.0
        self._last_message_received = 0.0
        self._last_pong_received = 0.0
        self._connected_at = 0.0
        self._refreshed_market = None
        self._controlled_close = False
        self._active = False
        self.connected = False
        self.error = ""

    def start(self) -> bool:
        try:
            import websocket  # type: ignore
        except ImportError:
            self.error = "websocket-client dependency is not installed"
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(websocket,), daemon=True, name="polymarket-book"
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self._changed.set()
        if self._app:
            try:
                self._controlled_close = True
                self._app.close()
            except Exception:
                pass

    def set_market(self, slug: str, up_token: str | None, down_token: str | None):
        if not up_token or not down_token:
            return
        with self._lock:
            if self._market == slug:
                return
            self._market = slug
            self._assets = {"up": str(up_token), "down": str(down_token)}
            self._asks = {str(up_token): {}, str(down_token): {}}
            self._best_ask = {str(up_token): None, str(down_token): None}
            self._book_valid = {str(up_token): False, str(down_token): False}
            self._updated = {}
            self._last_data_received = 0.0
            self._refreshed_market = None
            self._active = False
            app = self._app
            connected = self.connected
        if app and connected:
            try:
                self._controlled_close = True
                app.close()
            except Exception:
                pass
        self._changed.set()

    def activate_market(self, slug: str) -> bool:
        """Subscribe only for the final window, when the strategy needs the book."""
        with self._lock:
            if self._market != slug or self._active:
                return False
            self._active = True
            self._refreshed_market = slug
        log.info("activating polymarket book for %s final window", slug)
        self._changed.set()
        return True

    def refresh_before_active_window(self, slug: str) -> bool:
        """Backward-compatible name for activation used by the scan loop."""
        return self.activate_market(slug)

    @staticmethod
    def _source_ts(value) -> float:
        numeric = as_float(value, time.time())
        return numeric / 1000 if numeric and numeric > 1e11 else float(numeric)

    def _apply(self, item: dict):
        event = item.get("event_type")
        received = time.time()
        source_ts = self._source_ts(item.get("timestamp"))
        with self._lock:
            if event == "book":
                asset = str(item.get("asset_id") or "")
                if asset not in self._asks:
                    return
                self._last_data_received = received
                levels = {}
                for level in item.get("asks") or []:
                    price = as_float(level.get("price"))
                    size = as_float(level.get("size"), 0.0)
                    if price is not None and size and 0 < price < 1:
                        levels[price] = size
                self._asks[asset] = levels
                self._best_ask[asset] = min(levels) if levels else None
                self._book_valid[asset] = bool(levels)
                self._updated[asset] = (source_ts, received)
            elif event == "price_change":
                relevant = False
                advertised: dict[str, float | None] = {}
                for change in item.get("price_changes") or []:
                    asset = str(change.get("asset_id") or "")
                    if asset not in self._asks:
                        continue
                    relevant = True
                    if "best_ask" in change:
                        advertised[asset] = as_float(change.get("best_ask"))
                    if str(change.get("side")).upper() != "SELL":
                        continue
                    price = as_float(change.get("price"))
                    size = as_float(change.get("size"), 0.0)
                    if price is None:
                        continue
                    if size and size > 0:
                        self._asks[asset][price] = size
                    else:
                        self._asks[asset].pop(price, None)
                for asset in set(advertised):
                    explicit = advertised[asset]
                    maintained = min(self._asks[asset]) if self._asks[asset] else None
                    self._best_ask[asset] = explicit
                    self._book_valid[asset] = (
                        explicit is not None and maintained is not None
                        and abs(explicit - maintained) <= 1e-9
                    )
                    self._updated[asset] = (source_ts, received)
                if relevant:
                    self._last_data_received = received
            elif event == "best_bid_ask":
                asset = str(item.get("asset_id") or "")
                if asset in self._asks:
                    explicit = as_float(item.get("best_ask"))
                    maintained = min(self._asks[asset]) if self._asks[asset] else None
                    self._best_ask[asset] = explicit
                    self._book_valid[asset] = (
                        explicit is not None and maintained is not None
                        and abs(explicit - maintained) <= 1e-9
                    )
                    self._updated[asset] = (source_ts, received)
                    self._last_data_received = received

    def _run(self, websocket):
        while not self._stop.is_set():
            self._changed.wait(1)
            self._changed.clear()
            with self._lock:
                assets = list(self._assets.values())
                market = self._market
                active = self._active
            if not assets or not market or not active:
                continue

            def on_open(ws):
                self.error = ""
                with self._lock:
                    active_now = self._active
                    subscribed_assets = list(self._assets.values()) if active_now else []
                if not active_now:
                    ws.close()
                    return
                with self._lock:
                    # Never expose levels retained across a disconnect. Wait
                    # for this connection's complete snapshots instead.
                    self._asks = {asset: {} for asset in subscribed_assets}
                    self._best_ask = {asset: None for asset in subscribed_assets}
                    self._book_valid = {asset: False for asset in subscribed_assets}
                    self._updated = {}
                    self._last_data_received = 0.0
                    self._last_message_received = time.time()
                    self._last_pong_received = self._last_message_received
                    self._connected_at = self._last_message_received
                    self._controlled_close = False
                    self.connected = True
                ws.send(json.dumps({
                    "assets_ids": subscribed_assets, "type": "market",
                    "custom_feature_enabled": True,
                }, separators=(",", ":")))

                def heartbeat():
                    # Five seconds leaves margin around Polymarket's required
                    # ten-second application-level heartbeat interval.
                    while not self._stop.wait(5):
                        if self._app is not ws or not self.connected:
                            break
                        try:
                            ws.send("PING")
                        except Exception:
                            break

                threading.Thread(
                    target=heartbeat, daemon=True, name="polymarket-book-heartbeat"
                ).start()

            def on_message(_ws, message):
                try:
                    with self._lock:
                        self._last_message_received = time.time()
                    heartbeat_message = str(message).strip().upper()
                    if heartbeat_message == "PING":
                        _ws.send("PONG")
                        return
                    if heartbeat_message == "PONG":
                        with self._lock:
                            self._last_pong_received = time.time()
                        return
                    payload = json.loads(message)
                    for item in payload if isinstance(payload, list) else [payload]:
                        if isinstance(item, dict):
                            self._apply(item)
                except Exception as exc:
                    self.error = f"book parse: {exc!r}"[:300]

            def on_close(_ws, status, message):
                with self._lock:
                    connected_for = max(0.0, time.time() - self._connected_at)
                    self.connected = False
                    controlled = self._controlled_close
                    self._controlled_close = False
                logger = log.info if controlled else log.warning
                logger(
                    "polymarket book websocket closed status=%r message=%r after %.1fs%s",
                    status, message, connected_for,
                    " (controlled refresh)" if controlled else "",
                )

            def on_error(_ws, error):
                self.error = f"book: {error!r}"[:300]
                log.warning("polymarket book websocket error: %r", error)

            self._app = websocket.WebSocketApp(
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                on_open=on_open, on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            try:
                # This channel requires application-level text PING/PONG.
                self._app.run_forever()
            except Exception as exc:
                self.error = repr(exc)[:300]
            self.connected = False
            # Healthy connections that are dropped should recover quickly.
            # Immediate failures still back off enough to avoid a reconnect loop.
            with self._lock:
                connected_for = max(0.0, time.time() - self._connected_at)
                active = self._active
            reconnect_delay = 0.25 if connected_for >= 10 else 1.0
            if active and not self._changed.is_set() and not self._stop.wait(reconnect_delay):
                self._changed.set()

    def quote(
        self, side: str, max_silence: float = 5.0, max_book_age: float | None = None
    ) -> dict | None:
        with self._lock:
            asset = self._assets.get(side)
            levels = dict(self._asks.get(asset, {})) if asset else {}
            price = self._best_ask.get(asset) if asset else None
            valid = self._book_valid.get(asset, False) if asset else False
            updated = self._updated.get(asset) if asset else None
            last_data_received = self._last_data_received
            connected = self.connected
        if not connected or not levels or price is None or not valid or not updated:
            return None
        maintained = min(levels)
        if abs(price - maintained) > 1e-9 or price not in levels:
            return None
        source_ts, received_ts = updated
        now = time.time()
        book_age = now - received_ts
        market_silence = now - last_data_received
        max_book_age = max_silence if max_book_age is None else max_book_age
        if market_silence > max_silence or book_age > max_book_age:
            return None
        return {
            "price": price, "size": levels[price], "source_ts": source_ts,
            "received_ts": received_ts, "age_seconds": max(0.0, book_age),
            "market_silence_seconds": max(0.0, market_silence),
            "levels": tuple(sorted(levels.items())),
        }


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


def _clob_token_map(raw: dict) -> dict[str, str]:
    token_by_side = {}
    for token in raw.get("tokens") or []:
        side = str(token.get("outcome") or "").strip().lower()
        token_id = str(token.get("token_id") or "").strip()
        if side in {"up", "down"} and token_id:
            token_by_side[side] = token_id
    if set(token_by_side) != {"up", "down"}:
        raise ValueError("CLOB market did not provide explicit Up and Down token identities")
    if token_by_side["up"] == token_by_side["down"]:
        raise ValueError("CLOB market returned duplicate outcome tokens")
    return token_by_side


def market_metadata(
    slug: str, fresh: bool = False, verify_tokens: bool = False
) -> dict:
    cached = _MARKET_CACHE.get(slug)
    if (
        cached and not fresh and time.time() - cached[0] < 600
        and (not verify_tokens or cached[1].get("token_mapping_verified"))
    ):
        return dict(cached[1])
    raw = _json_get(
        "https://gamma-api.polymarket.com/markets/slug/"
        + urllib.parse.quote(slug, safe="")
    )
    outcomes = [str(value).lower() for value in _decode_list(raw.get("outcomes"))]
    tokens = [str(value) for value in _decode_list(raw.get("clobTokenIds"))]
    gamma_token_by_side = dict(zip(outcomes, tokens))
    token_by_side = dict(gamma_token_by_side)
    condition_id = str(raw.get("conditionId") or "")
    mapping_verified = False
    mapping_agrees = None
    if verify_tokens:
        if not condition_id:
            raise ValueError("Gamma market is missing conditionId")
        clob = _json_get(
            "https://clob.polymarket.com/markets/"
            + urllib.parse.quote(condition_id, safe="")
        )
        clob_slug = str(clob.get("market_slug") or "")
        if clob_slug and clob_slug != slug:
            raise ValueError(f"CLOB market slug mismatch: {clob_slug} != {slug}")
        token_by_side = _clob_token_map(clob)
        mapping_verified = True
        mapping_agrees = token_by_side == gamma_token_by_side
        if not mapping_agrees:
            log.warning("Gamma/CLOB outcome-token mapping mismatch for %s", slug)
    event = (raw.get("events") or [{}])[0]
    crypto_config = raw.get("cryptoMarketConfig") or {}
    twap_enabled = bool(crypto_config.get("twapEnabled"))
    twap_lookback = int(as_float(crypto_config.get("twapLookbackSeconds"), 0) or 0)
    settlement_source = (
        "chainlink_twap60" if twap_enabled and twap_lookback == 60
        else "chainlink" if not twap_enabled
        else "unsupported"
    )
    start_ts = parse_ts(event.get("startTime") or raw.get("eventStartTime"))
    end_ts = parse_ts(raw.get("endDate"))
    result = {
        "slug": slug,
        "condition_id": condition_id,
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
        "tick_size": float(raw.get("orderPriceMinTickSize") or 0.01),
        "resolution_source": str(raw.get("resolutionSource") or ""),
        "twap_enabled": twap_enabled,
        "twap_lookback_seconds": twap_lookback,
        "settlement_source": settlement_source,
        "token_mapping_verified": mapping_verified,
        "gamma_token_mapping_agrees": mapping_agrees,
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


def boundary_price(
    ticks: list[dict], source: str, boundary: float, max_delay: float = 5.0
) -> float | None:
    # The first source tick at/after the boundary is reproducible and avoids
    # selecting with future information. Reject gaps rather than substituting
    # a different venue's spot price.
    candidates = _source_ticks(ticks, source, boundary, boundary + max(0.0, max_delay))
    return float(candidates[0]["price"]) if candidates else None


def latest_price(ticks: list[dict], source: str, now: float) -> float | None:
    candidates = _source_ticks(ticks, source, now - 10, now + 1)
    return float(candidates[-1]["price"]) if candidates else None


def latest_tick(ticks: list[dict], source: str, now: float) -> dict | None:
    candidates = _source_ticks(ticks, source, now - 10, now + 1)
    return dict(candidates[-1]) if candidates else None


def source_confirmation(
    ticks: list[dict], start_ts: float, now: float, chain_direction: int,
    sources: tuple[str, ...] = ("binance", "coinbase", "kraken"),
) -> dict:
    details = {}
    agree = disagree = 0
    for source in sources:
        opening = boundary_price(ticks, source, start_ts)
        current = latest_price(ticks, source, now)
        direction = None
        if opening is not None and current is not None:
            direction = 1 if current >= opening else -1
            if direction == chain_direction:
                agree += 1
            else:
                disagree += 1
        details[source] = {
            "start": opening, "current": current, "direction": direction,
        }
    state = None if not agree and not disagree else agree > disagree
    return {
        "agree": agree, "disagree": disagree, "available": agree + disagree,
        "state": state, "sources": details,
    }


def execution_outcome(
    decision_ask: float, delayed_ask: float | None, delayed_depth: float,
    requested_qty: float, max_slippage: float, depth_safety_multiplier: float = 1.0,
) -> str:
    if delayed_ask is None:
        return "no_live_book"
    if delayed_ask > decision_ask + max_slippage + 1e-9:
        return "repriced_beyond_limit"
    if delayed_depth + 1e-9 < requested_qty * depth_safety_multiplier:
        return "insufficient_depth"
    return "fillable"


def execution_timing_reason(
    delay_ms: float, max_delay_seconds: float,
    remaining_seconds: float, min_remaining_seconds: float,
) -> str | None:
    if delay_ms > 1000 * max_delay_seconds:
        return "execution_delay_exceeded"
    if remaining_seconds < min_remaining_seconds:
        return "too_late_after_delay"
    return None


def book_capacity(levels: tuple[tuple[float, float], ...], limit_price: float) -> float:
    return sum(size for price, size in levels if price <= limit_price + 1e-9)


def simulate_fok_fill(
    levels: tuple[tuple[float, float], ...], requested_qty: float,
    limit_price: float, fee_rate: float, depth_safety_multiplier: float,
) -> dict | None:
    """Walk asks like a marketable FOK limit and include price-dependent fees."""
    available = book_capacity(levels, limit_price)
    if available + 1e-9 < requested_qty * max(1.0, depth_safety_multiplier):
        return None
    remaining = requested_qty
    notional = fee = 0.0
    for price, size in levels:
        if price > limit_price + 1e-9 or remaining <= 1e-9:
            break
        taken = min(remaining, size)
        notional += taken * price
        fee += taken * fee_rate * price * (1 - price)
        remaining -= taken
    if remaining > 1e-9 or requested_qty <= 0:
        return None
    return {
        "average_price": notional / requested_qty,
        "fee_per_share": fee / requested_qty,
        "total_fee": fee,
        "total_cost": notional + fee,
        "available_qty": available,
    }


def order_quantity(
    available: float, decision_ask: float, displayed_depth: float,
    max_slippage: float, fee_rate: float, depth_safety_multiplier: float,
) -> tuple[float, float]:
    """Fixed share quantity affordable even if filled at the order's limit price."""
    limit_price = min(0.99, decision_ask + max(0.0, max_slippage))
    limit_fee = fee_rate * limit_price * (1 - limit_price)
    depth_cap = displayed_depth / max(1.0, depth_safety_multiplier)
    qty = min(depth_cap, max(0.0, available) / (limit_price + limit_fee))
    return max(0.0, qty), limit_price


def screen_reason(
    in_entry_window: bool, margin_ok: bool, sources_agree: bool | None,
    live_book: bool, ask: float | None, max_ask: float,
    edge: float, required_edge: float,
) -> str:
    if not in_entry_window:
        return "outside_entry_window"
    if not margin_ok:
        return "margin_too_small"
    if sources_agree is False:
        return "source_disagreement"
    if ask is None:
        return "no_ask"
    if not live_book:
        return "no_fresh_chosen_book"
    if ask > max_ask:
        return "ask_above_cap"
    if edge < required_edge:
        return "edge_below_threshold"
    return "execution_candidate"


def market_midpoint_up_probability(
    up_ask: float | None, down_ask: float | None
) -> float | None:
    """Estimate Polymarket's Up probability from the two-sided spread."""
    if up_ask is None or down_ask is None:
        return None
    if not (0 < up_ask < 1 and 0 < down_ask < 1):
        return None
    up_bid = 1.0 - down_ask
    return max(0.0, min(1.0, (up_bid + up_ask) / 2.0))


def blended_probability(
    oracle_probability: float, market_probability: float, oracle_weight: float
) -> float:
    weight = max(0.0, min(1.0, oracle_weight))
    return weight * oracle_probability + (1.0 - weight) * market_probability


def market_conditioned_up_probability(
    oracle_up: float,
    market_up: float,
    spread: float,
    margin_bps: float,
    sigma_per_s: float,
) -> float:
    """Frozen regularized v8 model fitted before its forward cohort begins."""
    oracle = max(1e-5, min(1 - 1e-5, oracle_up))
    market = max(1e-5, min(1 - 1e-5, market_up))
    raw = (
        math.log(oracle / (1 - oracle)),
        math.log(market / (1 - market)),
        spread,
        abs(margin_bps),
        sigma_per_s * 10000,
    )
    features = [
        (value - mean) / scale
        for value, mean, scale in zip(raw, V8_MEANS, V8_SCALES)
    ]
    score = V8_WEIGHTS[0] + sum(
        weight * value for weight, value in zip(V8_WEIGHTS[1:], features)
    )
    score = max(-30.0, min(30.0, score))
    return 1.0 / (1.0 + math.exp(-score))


def v8_screen_reason(
    in_entry_window: bool,
    market_probability_available: bool,
    live_book: bool,
    ask: float | None,
    max_ask: float,
    confidence: float,
    min_confidence: float,
) -> str:
    if not in_entry_window:
        return "outside_entry_window"
    if not market_probability_available:
        return "market_probability_missing"
    if ask is None:
        return "no_ask"
    if not live_book:
        return "no_fresh_chosen_book"
    if ask > max_ask:
        return "ask_above_cap"
    if confidence < min_confidence:
        return "confidence_below_threshold"
    return "execution_candidate"


def fast_balance(db, cfg: Config) -> float:
    return fast_experiment_balance(db, cfg.fast_starting_cash_usd)


def shadow_balance(db, cfg: Config) -> float:
    return fast_shadow_balance(db, cfg.fast_starting_cash_usd)


def volatility_per_s(
    ticks: list[dict], now: float, lookback: float, source: str = "chainlink"
) -> float | None:
    values = _source_ticks(ticks, source, now - lookback, now + 1)
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


def mean_source_price(
    ticks: list[dict], source: str, start: float, end: float
) -> float | None:
    """Arithmetic mean for a nominally one-hertz source over a short interval."""
    if end <= start:
        return None
    values = _source_ticks(ticks, source, start, end)
    minimum = max(2, int((end - start) * 0.6))
    if len(values) < minimum:
        return None
    return statistics.fmean(float(tick["price"]) for tick in values)


def projected_twap_close(
    current_twap: float, current_spot: float, outgoing_spot: float,
    seconds_left: float, window_seconds: float = 60.0,
) -> float:
    """Project a rolling TWAP by replacing its known outgoing interval."""
    horizon = max(0.0, min(float(window_seconds), float(seconds_left)))
    return current_twap + (
        (current_spot - outgoing_spot) * horizon / float(window_seconds)
    )


def twap_projection_up_probability(
    projected_close: float, target: float, current_spot: float,
    spot_sigma_per_s: float, seconds_left: float,
    uncertainty_scale: float = 1.0, window_seconds: float = 60.0,
) -> tuple[float, float]:
    """Brownian-integral uncertainty for the unknown tail of the TWAP window."""
    horizon = max(0.0, min(float(window_seconds), float(seconds_left)))
    standard_error = (
        current_spot * max(0.0, spot_sigma_per_s) * horizon ** 1.5
        / (math.sqrt(3.0) * float(window_seconds))
    )
    standard_error = max(0.20, standard_error * max(0.25, uncertainty_scale))
    z = max(-12.0, min(12.0, (projected_close - target) / standard_error))
    probability = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return max(0.005, min(0.995, probability)), standard_error


def twap_projection_from_ticks(
    ticks: list[dict], current_twap: float, target: float, now: float,
    end_ts: float, uncertainty_scale: float = 1.0,
    window_seconds: float = 60.0,
) -> dict | None:
    """Build a no-lookahead expiry projection from the live oracle tapes."""
    seconds_left = end_ts - now
    current_spot = latest_price(ticks, "chainlink", now)
    outgoing_spot = mean_source_price(
        ticks, "chainlink", now - window_seconds, end_ts - window_seconds
    )
    spot_sigma = volatility_per_s(ticks, now, 120.0, "chainlink")
    if None in {current_spot, outgoing_spot, spot_sigma}:
        return None
    projected = projected_twap_close(
        current_twap, current_spot, outgoing_spot, seconds_left, window_seconds
    )
    probability, standard_error = twap_projection_up_probability(
        projected, target, current_spot, spot_sigma, seconds_left,
        uncertainty_scale, window_seconds,
    )
    return {
        "probability": probability,
        "projected_close": projected,
        "standard_error": standard_error,
        "current_spot": current_spot,
        "outgoing_spot_mean": outgoing_spot,
        "spot_sigma_per_s": spot_sigma,
    }


def up_probability(
    current: float, target: float, seconds_left: float, sigma_per_s: float,
    logit_slope: float, probability_cap: float, oracle_lag_allowance: float = 2.0,
) -> float:
    sigma = max(0.000005, sigma_per_s) * math.sqrt(
        max(0.25, seconds_left + max(0.0, oracle_lag_allowance))
    )
    z = math.log(current / target) / sigma
    scaled = max(-60.0, min(60.0, max(0.0, logit_slope) * z))
    probability = 1.0 / (1.0 + math.exp(-scaled))
    cap = max(0.5, min(0.99, probability_cap))
    return max(1 - cap, min(cap, probability))


def _store_ticks(db, fresh: list[dict]) -> int:
    """Persist only ticks drained since the last scan; never rescan the full table."""
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


def _open_fast_trade(
    db, cfg: Config, market: dict, evidence: dict, side: str, ask: float, qty: float,
    strategy_version_id: str = BTC_V8_STRATEGY_ID,
):
    if db.execute("SELECT 1 FROM fast_paper_trades WHERE slug=?", (market["slug"],)).fetchone():
        return None
    if db.execute(
        "SELECT COUNT(*) FROM fast_paper_trades WHERE status='open'"
    ).fetchone()[0] >= cfg.fast_max_open_trades:
        return None
    fair = evidence["up_probability"] if side == "up" else 1 - evidence["up_probability"]
    fee_per_share = as_float(evidence.get("fill_fee_per_share"))
    if fee_per_share is None:
        fee_per_share = market["fee_rate"] * ask * (1 - ask)
    unit_cost = ask + fee_per_share
    available = min(cfg.fast_trade_budget_usd, max(0.0, fast_balance(db, cfg)))
    if qty < market["min_order_size"] or qty * unit_cost > available + 1e-9:
        return None
    cost = qty * ask
    fee = qty * fee_per_share
    cur = db.execute(
        """INSERT INTO fast_paper_trades
        (slug,status,side,open_ts,seconds_left,qty,entry_price,entry_fee,
         probability,model_edge,price_to_beat,reference_price,evidence_json,
         strategy_version_id)
        VALUES(?,'open',?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            market["slug"], side, time.time(), evidence["seconds_left"], qty, ask, fee,
            fair, fair - unit_cost, evidence["price_to_beat"], evidence["chainlink_price"],
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            strategy_version_id,
        ),
    )
    trade_id = int(cur.lastrowid)
    ledger_entry(db, "fast_paper_open", -(cost + fee), trade_id, market["slug"])
    return trade_id


def _open_shadow_trade(
    db, cfg: Config, market: dict, evidence: dict, side: str, ask: float,
    qty: float, oracle_fair: float, market_fair: float, blended_fair: float,
):
    if db.execute(
        "SELECT 1 FROM fast_shadow_trades WHERE strategy_version=? AND slug=?",
        (FAST_SHADOW_VERSION, market["slug"]),
    ).fetchone():
        return None
    if db.execute(
        """SELECT COUNT(*) FROM fast_shadow_trades
        WHERE strategy_version=? AND status='open'""",
        (FAST_SHADOW_VERSION,),
    ).fetchone()[0] >= cfg.fast_max_open_trades:
        return None
    fee_per_share = as_float(evidence.get("shadow_fill_fee_per_share"))
    if fee_per_share is None:
        fee_per_share = market["fee_rate"] * ask * (1 - ask)
    available = min(cfg.fast_trade_budget_usd, max(0.0, shadow_balance(db, cfg)))
    unit_cost = ask + fee_per_share
    if qty < market["min_order_size"] or qty * unit_cost > available + 1e-9:
        return None
    fee = qty * fee_per_share
    cur = db.execute(
        """INSERT INTO fast_shadow_trades
        (strategy_version,slug,status,side,open_ts,seconds_left,qty,entry_price,
         entry_fee,oracle_probability,market_probability,blended_probability,
         model_edge,evidence_json)
        VALUES(?,?,'open',?,?,?,?,?,?,?,?,?,?,?)""",
        (
            FAST_SHADOW_VERSION, market["slug"], side, time.time(),
            evidence["seconds_left"], qty, ask, fee, oracle_fair, market_fair,
            blended_fair, blended_fair - unit_cost,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        ),
    )
    return int(cur.lastrowid)


def _record_shadow_decision(
    db, evidence: dict, side: str, ask: float | None, oracle_fair: float,
    market_fair: float | None, blended_fair: float | None,
    fee_per_share: float | None, edge: float | None, required_edge: float,
    reason: str,
) -> None:
    db.execute(
        """INSERT OR IGNORE INTO fast_shadow_decisions
        (ts,strategy_version,slug,side,seconds_left,ask,oracle_probability,
         market_probability,blended_probability,fee_per_share,net_edge,
         required_edge,reason,evidence_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence["timestamp"], FAST_SHADOW_VERSION, evidence["slug"], side,
            evidence["seconds_left"], ask, oracle_fair, market_fair,
            blended_fair, fee_per_share, edge, required_edge, reason,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        ),
    )


def _record_attempt(
    db, market: dict, evidence: dict, side: str, decision_ask: float,
    delayed_ask: float | None, requested_qty: float, delayed_depth: float,
    outcome: str, pre_edge: float, post_edge: float | None, delay_ms: float,
    quote_age_ms: float | None,
):
    db.execute(
        """INSERT INTO fast_execution_attempts
        (ts,slug,side,seconds_left,decision_ask,delayed_ask,requested_qty,
         available_qty,delay_ms,quote_age_ms,modeled_probability,pre_delay_edge,
         post_delay_edge,outcome,evidence_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            time.time(), market["slug"], side, evidence["seconds_left"], decision_ask,
            delayed_ask, requested_qty, delayed_depth, delay_ms, quote_age_ms,
            evidence["up_probability"] if side == "up" else 1 - evidence["up_probability"],
            pre_edge, post_edge, outcome,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        ),
    )


def _record_live_shadow_decision(
    db, market: dict, evidence: dict, side: str, strategy_version_id: str,
    gate_reason: str, source_confirmed: bool | None, base_budget: float,
    effective_budget: float, minimum_qty: float,
    decision_limit_price: float | None, delayed_ask: float | None,
    delayed_minimum_cost: float | None, candidate_qty: float, outcome: str,
) -> None:
    """Persist the exact wallet/risk-policy decision without inventing a fill."""
    db.execute(
        """INSERT INTO fast_live_shadow_decisions
        (ts,slug,strategy_version_id,side,gate_reason,source_confirmed,
         base_budget_usd,effective_budget_usd,minimum_qty,decision_limit_price,
         delayed_ask,delayed_minimum_cost_usd,candidate_qty,outcome,evidence_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(slug) DO UPDATE SET
          ts=excluded.ts,gate_reason=excluded.gate_reason,
          source_confirmed=excluded.source_confirmed,
          base_budget_usd=excluded.base_budget_usd,
          effective_budget_usd=excluded.effective_budget_usd,
          decision_limit_price=excluded.decision_limit_price,
          delayed_ask=excluded.delayed_ask,
          delayed_minimum_cost_usd=excluded.delayed_minimum_cost_usd,
          candidate_qty=excluded.candidate_qty,outcome=excluded.outcome,
          evidence_json=excluded.evidence_json""",
        (
            time.time(), market["slug"], strategy_version_id, side, gate_reason,
            None if source_confirmed is None else int(source_confirmed),
            base_budget, effective_budget, minimum_qty, decision_limit_price,
            delayed_ask, delayed_minimum_cost, candidate_qty, outcome,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        ),
    )


_LAST_VALIDATION = 0.0
_LAST_FAST_PRUNE = 0.0


def prune_fast_ticks(db, now: float | None = None) -> int:
    global _LAST_FAST_PRUNE
    now = time.time() if now is None else now
    if now - _LAST_FAST_PRUNE < 3600:
        return 0
    _LAST_FAST_PRUNE = now
    before = db.total_changes
    db.execute("DELETE FROM fast_price_ticks WHERE source_ts<?", (now - 2 * 86400,))
    return db.total_changes - before


def validate_fast_windows(db) -> int:
    """Compare captured boundary ticks with the venue's eventual resolution."""
    global _LAST_VALIDATION
    now = time.time()
    if now - _LAST_VALIDATION < 30:
        return 0
    _LAST_VALIDATION = now
    rows = db.execute(
        """SELECT slug,MIN(start_ts) start_ts,MAX(end_ts) end_ts
        FROM fast_market_samples
        WHERE end_ts<? AND slug NOT IN (SELECT slug FROM fast_window_results)
        GROUP BY slug ORDER BY end_ts DESC LIMIT 3""",
        (now - 45,),
    ).fetchall()
    validated = 0
    for row in rows:
        try:
            market = market_metadata(row["slug"], fresh=True)
        except Exception:
            continue
        settlement_source = market.get("settlement_source")
        if settlement_source not in {"chainlink", "chainlink_twap60"}:
            continue
        boundary_delay = 1.1 if settlement_source == "chainlink_twap60" else 5.0
        prices = [as_float(value) for value in market["outcome_prices"]]
        result = next(
            (outcome for outcome, price in zip(market["outcomes"], prices)
             if price is not None and price >= 0.999),
            "",
        )
        if not market["closed"] or result not in {"up", "down"}:
            continue
        opening = db.execute(
            """SELECT price,source_ts FROM fast_price_ticks
            WHERE source=? AND source_ts>=? AND source_ts<=?
            ORDER BY source_ts LIMIT 1""",
            (settlement_source, row["start_ts"], row["start_ts"] + boundary_delay),
        ).fetchone()
        ending = db.execute(
            """SELECT price,source_ts FROM fast_price_ticks
            WHERE source=? AND source_ts>=? AND source_ts<=?
            ORDER BY source_ts LIMIT 1""",
            (settlement_source, row["end_ts"], row["end_ts"] + boundary_delay),
        ).fetchone()
        observed = ""
        if opening and ending:
            observed = "up" if float(ending["price"]) >= float(opening["price"]) else "down"
        evidence = {
            "settlement_source": settlement_source,
            "resolution_source": market.get("resolution_source"),
            "twap_lookback_seconds": market.get("twap_lookback_seconds"),
            "opening_source_ts": opening["source_ts"] if opening else None,
            "ending_source_ts": ending["source_ts"] if ending else None,
        }
        db.execute(
            """INSERT OR REPLACE INTO fast_window_results
            (slug,start_ts,end_ts,opening_price,ending_price,observed_result,
             venue_result,result_matches,validated_ts,evidence_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                row["slug"], row["start_ts"], row["end_ts"],
                opening["price"] if opening else None, ending["price"] if ending else None,
                observed or None, result, int(observed == result) if observed else None,
                now, json.dumps(evidence, separators=(",", ":")),
            ),
        )
        validated += 1
    return validated


def settle_fast_trades(db) -> tuple[int, list[str]]:
    cohort_start = fast_experiment_start(db)
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
        if float(row["open_ts"]) >= cohort_start:
            notable.append(
                f"⚡ PAPER BTC {row['side'].upper()} settled {result.upper()}: ${realized:+.2f}"
            )
        settled += 1
    return settled, notable


def settle_shadow_trades(db) -> int:
    """Settle v7 from the already-validated local result table—no new I/O."""
    rows = db.execute(
        """SELECT t.id,t.side,t.qty,t.entry_price,t.entry_fee,w.venue_result
        FROM fast_shadow_trades t
        JOIN fast_window_results w ON w.slug=t.slug
        WHERE t.strategy_version=? AND t.status='open'
          AND w.venue_result IN ('up','down')""",
        (FAST_SHADOW_VERSION,),
    ).fetchall()
    now = time.time()
    for row in rows:
        payout = float(row["qty"]) if row["side"] == row["venue_result"] else 0.0
        realized = payout - float(row["qty"]) * float(row["entry_price"]) - float(row["entry_fee"])
        db.execute(
            """UPDATE fast_shadow_trades SET status='settled',close_ts=?,result=?,
            payout=?,realized_pnl=? WHERE id=?""",
            (now, row["venue_result"], payout, realized, row["id"]),
        )
    return len(rows)


def scan_fast(
    db, cfg: Config, stream: PriceStream, book_stream: MarketBookStream | None = None,
    live_broker=None,
) -> dict:
    ticks = stream.ticks(time.time() - 360)
    stored = _store_ticks(db, stream.drain_pending())
    pruned_ticks = prune_fast_ticks(db)
    settled, notable = settle_fast_trades(db)
    validated = validate_fast_windows(db)
    shadow_settled = settle_shadow_trades(db) if cfg.fast_v7_shadow_enabled else 0
    if live_broker is not None:
        notable.extend(live_broker.drain(db))
        live_broker.mark_resolutions(db)
        live_broker.reconcile_uncertain_orders(db)
    if not cfg.enable_fast_btc:
        db.commit()
        return {"state": "disabled", "stored_ticks": stored, "pruned_ticks": pruned_ticks, "settled": settled,
                "shadow_settled": shadow_settled, "validated": validated, "notable": notable}
    if not stream.connected:
        db.commit()
        return {
            "state": "stream_unavailable", "error": stream.error,
            "stored_ticks": stored, "settled": settled, "shadow_settled": shadow_settled,
            "validated": validated,
            "notable": notable,
        }

    now = time.time()
    from .automation import active_btc_spec

    active_strategy_id, active_parameters = active_btc_spec(db, cfg)
    active_min_seconds = float(
        active_parameters.get("min_seconds_left", cfg.fast_min_seconds_left)
    )
    active_max_seconds = float(
        active_parameters.get("max_seconds_left", cfg.fast_entry_window_seconds)
    )
    active_max_ask = float(active_parameters.get("max_ask", cfg.fast_max_ask))
    active_min_confidence = float(
        active_parameters.get("min_confidence", cfg.fast_v8_min_confidence)
    )
    active_requires_confirmation = bool(
        active_parameters.get("require_source_confirmation", False)
    )
    active_model_kind = str(
        active_parameters.get("model_kind", "market_conditioned_v8")
    )
    active_oracle_weight = float(active_parameters.get("oracle_weight", 0.75))
    active_market_weight = float(active_parameters.get("market_weight", 0.25))
    active_uncertainty_scale = float(
        active_parameters.get("uncertainty_scale", 1.0)
    )
    active_min_net_edge = float(active_parameters.get("min_net_edge", 0.0))
    slug, expected_start, expected_end = current_slug(now)
    seconds_left = expected_end - now
    if live_broker is not None:
        live_broker.refresh_if_due(db, seconds_left)
    market = None
    try:
        market = market_metadata(slug, verify_tokens=True)
        if book_stream:
            book_stream.set_market(slug, market.get("up_token"), market.get("down_token"))
            if seconds_left <= cfg.fast_active_window_seconds + 5:
                book_stream.refresh_before_active_window(slug)
    except Exception as exc:
        if seconds_left <= cfg.fast_active_window_seconds:
            db.commit()
            return {
                "state": "market_error", "slug": slug, "error": repr(exc)[:300],
                "stored_ticks": stored, "settled": settled, "validated": validated,
                "notable": notable,
            }
    if seconds_left > cfg.fast_active_window_seconds:
        db.commit()
        return {
            "state": "observing", "slug": slug, "seconds_left": round(seconds_left, 2),
            "ticks": len(ticks), "stored_ticks": stored, "settled": settled,
            "validated": validated, "feeds": dict(stream.source_status),
            "book_connected": bool(book_stream and book_stream.connected), "notable": notable,
        }
    try:
        market = market or market_metadata(slug, verify_tokens=True)
        start_ts = market["start_ts"] or expected_start
        end_ts = market["end_ts"] or expected_end
        seconds_left = end_ts - now
        settlement_source = market.get("settlement_source")
        if settlement_source not in {"chainlink", "chainlink_twap60"}:
            db.commit()
            return {
                "state": "unsupported_resolution_source", "slug": slug,
                "settlement_source": settlement_source,
                "twap_lookback_seconds": market.get("twap_lookback_seconds"),
                "notable": notable,
            }
        if not stream.source_status.get(settlement_source, False):
            db.commit()
            return {
                "state": "settlement_stream_unavailable", "slug": slug,
                "settlement_source": settlement_source,
                "seconds_left": round(seconds_left, 2), "notable": notable,
            }
        boundary_delay = 1.1 if settlement_source == "chainlink_twap60" else 5.0
        target = boundary_price(ticks, settlement_source, start_ts, boundary_delay)
        chainlink = latest_price(ticks, settlement_source, now)
        sigma = volatility_per_s(
            ticks, now, cfg.fast_volatility_lookback_seconds, settlement_source
        )
        if None in {target, chainlink, sigma}:
            db.commit()
            return {
                "state": "insufficient_reference_history", "slug": slug,
                "seconds_left": round(seconds_left, 2), "stored_ticks": stored,
                "settled": settled, "validated": validated, "notable": notable,
            }
        up_quote = (
            book_stream.quote(
                "up", cfg.fast_max_market_data_silence_seconds,
                cfg.fast_max_book_age_seconds,
            )
            if book_stream else None
        )
        down_quote = (
            book_stream.quote(
                "down", cfg.fast_max_market_data_silence_seconds,
                cfg.fast_max_book_age_seconds,
            )
            if book_stream else None
        )
        live_book = up_quote is not None and down_quote is not None
        if up_quote:
            up_ask, up_depth = up_quote["price"], up_quote["size"]
        else:
            up_ask, up_depth = best_ask(market["up_token"])
        if down_quote:
            down_ask, down_depth = down_quote["price"], down_quote["size"]
        else:
            down_ask, down_depth = best_ask(market["down_token"])
    except Exception as exc:
        log.exception("fast market sample failed")
        db.commit()
        return {
            "state": "market_error", "slug": slug, "error": repr(exc)[:300],
            "stored_ticks": stored, "settled": settled, "validated": validated,
            "notable": notable,
        }

    oracle_probability = up_probability(
        chainlink, target, seconds_left, sigma,
        cfg.fast_probability_logit_slope, cfg.fast_probability_cap,
        cfg.fast_oracle_lag_allowance_seconds,
    )
    margin_bps = 10_000 * (chainlink / target - 1)
    market_up_probability = market_midpoint_up_probability(up_ask, down_ask)
    spread = (
        up_ask + down_ask - 1.0
        if up_ask is not None and down_ask is not None else 0.0
    )
    projection = None
    if active_model_kind == "twap_projection_v2":
        projection = twap_projection_from_ticks(
            ticks, chainlink, target, now, end_ts,
            uncertainty_scale=active_uncertainty_scale,
            window_seconds=float(market.get("twap_lookback_seconds") or 60),
        )
        if projection is None:
            db.commit()
            return {
                "state": "insufficient_projection_history", "slug": slug,
                "seconds_left": round(seconds_left, 2), "notable": notable,
            }
        probability = (
            (1.0 - active_market_weight) * projection["probability"]
            + active_market_weight * market_up_probability
            if market_up_probability is not None else projection["probability"]
        )
        model_description = (
            "TWAP60 rolling-window close projection blended with executable market"
        )
    elif active_model_kind == "twap_native_v1":
        probability = (
            blended_probability(
                oracle_probability, market_up_probability, active_oracle_weight
            )
            if market_up_probability is not None else oracle_probability
        )
        model_description = "TWAP60 oracle/market conservative blend; price-aware entry"
    elif active_model_kind == "market_conditioned_v8":
        probability = (
            market_conditioned_up_probability(
                oracle_probability, market_up_probability, spread, margin_bps, sigma
            )
            if market_up_probability is not None else oracle_probability
        )
        model_description = "v8 regularized market-conditioned terminal probability"
    else:
        db.commit()
        return {
            "state": "unsupported_model_kind", "slug": slug,
            "model_kind": active_model_kind, "notable": notable,
        }
    chain_direction = 1 if probability >= 0.5 else -1
    chosen = "up" if probability >= 0.5 else "down"
    selected_live_book = up_quote is not None if chosen == "up" else down_quote is not None
    confirmation = source_confirmation(ticks, start_ts, now, chain_direction)
    sources_agree = confirmation["state"]
    if confirmation["available"] < cfg.fast_min_confirming_sources:
        sources_agree = None
    chain_tick = latest_tick(ticks, settlement_source, now)
    evidence = {
        "slug": slug, "timestamp": now, "start_ts": start_ts, "end_ts": end_ts,
        "seconds_left": seconds_left, "price_to_beat": target,
        "chainlink_price": chainlink, "chainlink_source_ts": chain_tick["source_ts"] if chain_tick else None,
        "chainlink_receive_lag_ms": (
            1000 * (chain_tick["received_ts"] - chain_tick["source_ts"]) if chain_tick else None
        ), "margin_bps": margin_bps,
        "settlement_price": chainlink,
        "settlement_source": settlement_source,
        "resolution_source": market.get("resolution_source"),
        "twap_lookback_seconds": market.get("twap_lookback_seconds"),
        "sigma_per_s": sigma, "up_probability": probability,
        "oracle_up_probability": oracle_probability,
        "projection_up_probability": (
            projection["probability"] if projection else None
        ),
        "projected_twap_close": (
            projection["projected_close"] if projection else None
        ),
        "projection_standard_error": (
            projection["standard_error"] if projection else None
        ),
        "instant_chainlink_price": (
            projection["current_spot"] if projection else None
        ),
        "outgoing_spot_mean": (
            projection["outgoing_spot_mean"] if projection else None
        ),
        "spot_sigma_per_s": (
            projection["spot_sigma_per_s"] if projection else None
        ),
        "market_up_probability": market_up_probability,
        "market_spread": spread,
        "up_ask": up_ask, "up_depth": up_depth, "down_ask": down_ask,
        "down_depth": down_depth, "live_book": live_book,
        "selected_live_book": selected_live_book,
        "up_book_age_ms": 1000 * up_quote["age_seconds"] if up_quote else None,
        "down_book_age_ms": 1000 * down_quote["age_seconds"] if down_quote else None,
        "up_market_silence_ms": (
            1000 * up_quote["market_silence_seconds"] if up_quote else None
        ),
        "down_market_silence_ms": (
            1000 * down_quote["market_silence_seconds"] if down_quote else None
        ),
        "sources_agree": sources_agree, "confirmation": confirmation,
        "model": model_description,
        "model_kind": active_model_kind,
        "oracle_weight": active_oracle_weight,
        "market_weight": active_market_weight,
        "uncertainty_scale": active_uncertainty_scale,
        "min_net_edge": active_min_net_edge,
        "probability_logit_slope": cfg.fast_probability_logit_slope,
        "probability_cap": cfg.fast_probability_cap,
        "oracle_lag_allowance_seconds": cfg.fast_oracle_lag_allowance_seconds,
        "calibration_windows": 428,
        "model_version": FAST_EXPERIMENT_VERSION,
        "strategy_version_id": active_strategy_id,
        "active_strategy_parameters": active_parameters,
        "modeled_total_delay_ms": 1000 * (
            cfg.fast_submission_latency_seconds + cfg.fast_taker_delay_seconds
        ),
        "depth_safety_multiplier": cfg.fast_depth_safety_multiplier,
        "up_token_id": market["up_token"],
        "down_token_id": market["down_token"],
        "chosen_token_id": market[f"{chosen}_token"],
        "token_mapping_verified": market["token_mapping_verified"],
        "gamma_token_mapping_agrees": market["gamma_token_mapping_agrees"],
        "condition_id": market["condition_id"],
        "tick_size": market["tick_size"],
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
    live_result = None
    ask, depth = (up_ask, up_depth) if chosen == "up" else (down_ask, down_depth)
    fair = probability if chosen == "up" else 1 - probability
    unit_fee = market["fee_rate"] * ask * (1 - ask) if ask is not None else float("inf")
    edge = fair - ask - unit_fee if ask is not None else -1
    worst_limit = (
        min(0.99, ask + max(0.0, cfg.fast_max_slippage))
        if ask is not None else None
    )
    worst_fee = (
        market["fee_rate"] * worst_limit * (1 - worst_limit)
        if worst_limit is not None else None
    )
    worst_net_edge = (
        fair - worst_limit - worst_fee
        if worst_limit is not None and worst_fee is not None else None
    )
    evidence["worst_fill_limit"] = worst_limit
    evidence["worst_fill_net_edge"] = worst_net_edge
    required_edge = 0.0
    confidence = probability if chosen == "up" else 1.0 - probability
    in_entry_window = active_min_seconds <= seconds_left <= active_max_seconds
    required_live_book = (
        live_book if active_model_kind == "twap_projection_v2"
        else selected_live_book
    )
    reason = v8_screen_reason(
        in_entry_window,
        market_up_probability is not None,
        required_live_book or not cfg.fast_require_live_book,
        ask,
        active_max_ask,
        confidence,
        active_min_confidence,
    )
    if (
        reason == "execution_candidate"
        and "min_net_edge" in active_parameters
        and worst_net_edge is not None
        and worst_net_edge < active_min_net_edge
    ):
        reason = "edge_below_threshold"
    if (
        reason == "execution_candidate"
        and active_requires_confirmation
        and sources_agree is not True
    ):
        reason = (
            "source_confirmation_unavailable"
            if sources_agree is None else "source_confirmation_failed"
        )
    if reason == "execution_candidate" and db.execute(
        "SELECT 1 FROM fast_paper_trades WHERE slug=?", (slug,)
    ).fetchone():
        reason = "already_traded"
    eligible = reason == "execution_candidate"
    shadow_opened = None
    shadow_market_up = market_midpoint_up_probability(up_ask, down_ask)
    if shadow_market_up is None:
        shadow_market_fair = None
    else:
        shadow_market_fair = (
            shadow_market_up if chosen == "up" else 1.0 - shadow_market_up
        )
    shadow_blended_fair = (
        blended_probability(fair, shadow_market_fair, cfg.fast_v7_oracle_weight)
        if shadow_market_fair is not None else None
    )
    shadow_edge = (
        shadow_blended_fair - ask - unit_fee
        if shadow_blended_fair is not None and ask is not None else None
    )
    if not cfg.fast_v7_shadow_enabled:
        shadow_reason = "disabled"
    elif shadow_market_fair is None:
        shadow_reason = "market_probability_missing"
    elif not in_entry_window:
        shadow_reason = "outside_entry_window"
    elif ask is None:
        shadow_reason = "no_ask"
    elif not (selected_live_book or not cfg.fast_require_live_book):
        shadow_reason = "no_fresh_chosen_book"
    elif ask > active_max_ask:
        shadow_reason = "ask_above_cap"
    elif shadow_blended_fair is None or shadow_blended_fair < active_min_confidence:
        shadow_reason = "confidence_below_threshold"
    elif shadow_edge is None or shadow_edge < active_min_net_edge:
        shadow_reason = "edge_below_threshold"
    else:
        shadow_reason = "execution_candidate"
    shadow_eligible = shadow_reason == "execution_candidate"
    shadow_evidence = dict(evidence)
    shadow_evidence.update({
        "shadow_version": FAST_SHADOW_VERSION,
        "shadow_oracle_weight": cfg.fast_v7_oracle_weight,
        "shadow_oracle_probability": fair,
        "shadow_market_probability": shadow_market_fair,
        "shadow_blended_probability": shadow_blended_fair,
        "shadow_decision_edge": shadow_edge,
        "shadow_reason": shadow_reason,
    })
    if in_entry_window:
        db.execute(
            """INSERT OR IGNORE INTO fast_screen_decisions
            (ts,slug,side,seconds_left,ask,fair_value,fee_per_share,net_edge,
             required_edge,reason,evidence_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now, slug, chosen, seconds_left, ask, fair,
                unit_fee if ask is not None else None, edge if ask is not None else None,
                required_edge, reason,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            ),
        )
    if eligible:
        decision_started = time.time()
        decision_ask = ask
        decision_quote = up_quote if chosen == "up" else down_quote
        decision_levels = decision_quote["levels"] if decision_quote else ()
        limit_price = min(0.99, decision_ask + max(0.0, cfg.fast_max_slippage))
        decision_depth = book_capacity(decision_levels, limit_price)
        evidence.update({
            "ws_decision_ask": decision_ask,
            "ws_decision_depth": decision_depth,
            "book_source": "clob_websocket_explicit_best_ask",
            "token_mapping_source": "clob_market_tokens",
            "token_mapping_verified": market["token_mapping_verified"],
            "gamma_token_mapping_agrees": market["gamma_token_mapping_agrees"],
        })
        available = min(cfg.fast_trade_budget_usd, max(0.0, fast_balance(db, cfg)))
        requested_qty, limit_price = order_quantity(
            available, decision_ask, decision_depth,
            cfg.fast_max_slippage, market["fee_rate"],
            cfg.fast_depth_safety_multiplier,
        )
        live_budget = 0.0
        live_base_budget = 0.0
        live_reason = "live_broker_unavailable"
        live_qty = 0.0
        live_limit_price = None
        live_shadow_outcome = live_reason
        if live_broker is not None:
            live_budget, live_reason = live_broker.budget(db, active_strategy_id)
            live_base_budget = live_budget
            evidence["live_budget_usd"] = live_budget
            evidence["live_gate_reason"] = live_reason
            if (
                (cfg.live_require_source_confirmation or active_requires_confirmation)
                and sources_agree is not True
            ):
                confirmation_reason = (
                    "source_confirmation_unavailable"
                    if sources_agree is None else "source_confirmation_failed"
                )
                live_result = {"state": "skipped", "reason": confirmation_reason}
                live_shadow_outcome = confirmation_reason
            elif live_reason.startswith("ready:"):
                live_qty, live_limit_price = order_quantity(
                    live_budget, decision_ask, decision_depth,
                    cfg.fast_max_slippage, market["fee_rate"],
                    cfg.fast_depth_safety_multiplier,
                )
                evidence["live_base_budget_usd"] = live_budget
                bridge_budget, bridge_reason = live_broker.minimum_bridge_budget(
                    db, live_budget, live_reason, market["min_order_size"],
                    live_limit_price, market["fee_rate"], confidence,
                    active_strategy_id,
                )
                evidence["live_minimum_bridge_reason"] = bridge_reason
                evidence["live_minimum_bridge_budget_usd"] = bridge_budget
                if bridge_budget > live_budget + 1e-9:
                    live_budget = bridge_budget
                    live_qty, live_limit_price = order_quantity(
                        live_budget, decision_ask, decision_depth,
                        cfg.fast_max_slippage, market["fee_rate"],
                        cfg.fast_depth_safety_multiplier,
                    )
                if live_qty >= market["min_order_size"]:
                    live_result = live_broker.submit_fok(
                        db, market, evidence, chosen, live_qty,
                        live_limit_price, live_budget, active_strategy_id,
                    )
                    if live_result.get("state") == "submitting":
                        live_shadow_outcome = "actual_submitting"
                        notable.append(
                            f"🔴 LIVE BTC {chosen.upper()} submitting up to "
                            f"${live_budget:.2f} at limit {live_limit_price:.2f}"
                        )
                    else:
                        live_shadow_outcome = str(
                            live_result.get("reason") or live_result.get("state") or "skipped"
                        )
                else:
                    live_result = {"state": "skipped", "reason": "below_minimum_size"}
                    live_shadow_outcome = "below_minimum_size"
            else:
                live_result = {"state": "skipped", "reason": live_reason}
                live_shadow_outcome = live_reason
        shadow_requested_qty = 0.0
        shadow_limit_price = limit_price
        if shadow_eligible:
            shadow_available = min(
                cfg.fast_trade_budget_usd, max(0.0, shadow_balance(db, cfg))
            )
            shadow_requested_qty, shadow_limit_price = order_quantity(
                shadow_available, decision_ask, decision_depth,
                cfg.fast_max_slippage, market["fee_rate"],
                cfg.fast_depth_safety_multiplier,
            )
        delayed_quote = None
        outcome = "below_minimum_size"
        delayed_ask = None
        delayed_top_ask = None
        delayed_depth = 0.0
        fill_fee_per_share = None
        quote_age_ms = None
        post_edge = None
        timing_rejection = None
        if requested_qty >= market["min_order_size"]:
            modeled_delay = (
                cfg.fast_submission_latency_seconds + cfg.fast_taker_delay_seconds
            )
            time.sleep(modeled_delay)
            delayed_quote = (
                book_stream.quote(
                    chosen, cfg.fast_max_market_data_silence_seconds,
                    cfg.fast_max_book_age_seconds,
                )
                if book_stream else None
            )
            delay_ms = 1000 * (time.time() - decision_started)
            quote_age_ms = (
                1000 * delayed_quote["age_seconds"] if delayed_quote else None
            )
            remaining_after_delay = end_ts - time.time()
            delayed_top_ask = delayed_quote["price"] if delayed_quote else None
            if delayed_quote:
                delayed_depth = book_capacity(delayed_quote["levels"], limit_price)
            evidence.update({
                "ws_delayed_top_ask": delayed_top_ask,
                "ws_delayed_depth_to_limit": delayed_depth,
                "remaining_after_delay_seconds": remaining_after_delay,
            })
            timing_rejection = execution_timing_reason(
                delay_ms, cfg.fast_max_execution_delay_seconds,
                remaining_after_delay, cfg.fast_min_post_delay_seconds,
            )
            if timing_rejection:
                outcome = timing_rejection
            elif delayed_quote is None:
                outcome = "no_live_book"
            elif delayed_top_ask > limit_price + 1e-9:
                outcome = "repriced_beyond_limit"
            else:
                fill = simulate_fok_fill(
                    delayed_quote["levels"], requested_qty, limit_price,
                    market["fee_rate"], cfg.fast_depth_safety_multiplier,
                )
                if fill is None:
                    outcome = "insufficient_depth"
                else:
                    delayed_ask = fill["average_price"]
                    fill_fee_per_share = fill["fee_per_share"]
                    delayed_depth = fill["available_qty"]
                    evidence.update({
                        "fill_average_price": delayed_ask,
                        "fill_fee_per_share": fill_fee_per_share,
                        "fill_total_cost": fill["total_cost"],
                    })
                    outcome = "fillable"
        else:
            delay_ms = 1000 * (time.time() - decision_started)
        if (
            live_broker is not None
            and cfg.live_delayed_affordability_retry
            and live_result is not None
            and live_result.get("reason") == "below_minimum_size"
        ):
            if timing_rejection:
                live_shadow_outcome = f"retry_{timing_rejection}"
            elif delayed_quote is None:
                live_shadow_outcome = "retry_no_live_book"
            elif delayed_top_ask > limit_price + 1e-9:
                live_shadow_outcome = "retry_repriced_beyond_limit"
            else:
                # This path exists only for an otherwise impossible order. It
                # rechecks every live gate after the modeled delay, then sizes
                # from the fresh ask and a fresh depth snapshot.
                retry_base_budget, retry_reason = live_broker.budget(
                    db, active_strategy_id
                )
                evidence["live_retry_gate_reason"] = retry_reason
                if retry_reason.startswith("ready:"):
                    retry_limit = min(
                        0.99, delayed_top_ask + max(0.0, cfg.fast_max_slippage)
                    )
                    retry_depth = book_capacity(delayed_quote["levels"], retry_limit)
                    retry_qty, retry_limit = order_quantity(
                        retry_base_budget, delayed_top_ask, retry_depth,
                        cfg.fast_max_slippage, market["fee_rate"],
                        cfg.fast_depth_safety_multiplier,
                    )
                    retry_budget, retry_bridge_reason = (
                        live_broker.minimum_bridge_budget(
                            db, retry_base_budget, retry_reason,
                            market["min_order_size"], retry_limit,
                            market["fee_rate"], confidence, active_strategy_id,
                        )
                    )
                    if retry_budget > retry_base_budget + 1e-9:
                        retry_qty, retry_limit = order_quantity(
                            retry_budget, delayed_top_ask, retry_depth,
                            cfg.fast_max_slippage, market["fee_rate"],
                            cfg.fast_depth_safety_multiplier,
                        )
                    live_budget = retry_budget
                    live_qty = retry_qty
                    live_limit_price = retry_limit
                    evidence.update({
                        "live_affordability_retry": True,
                        "live_retry_delay_ms": delay_ms,
                        "live_retry_ask": delayed_top_ask,
                        "live_retry_budget_usd": retry_budget,
                        "live_retry_bridge_reason": retry_bridge_reason,
                        "live_retry_limit_price": retry_limit,
                        "live_retry_qty": retry_qty,
                    })
                    if retry_qty >= market["min_order_size"]:
                        live_result = live_broker.submit_fok(
                            db, market, evidence, chosen, retry_qty,
                            retry_limit, retry_budget, active_strategy_id,
                        )
                        live_shadow_outcome = (
                            "affordability_retry_submitting"
                            if live_result.get("state") == "submitting"
                            else str(live_result.get("reason") or "retry_skipped")
                        )
                        if live_result.get("state") == "submitting":
                            notable.append(
                                f"🔴 LIVE BTC {chosen.upper()} delayed-affordability "
                                f"retry up to ${retry_budget:.2f} at {retry_limit:.2f}"
                            )
                    else:
                        live_shadow_outcome = "retry_below_minimum_size"
                else:
                    live_shadow_outcome = retry_reason
        evidence["live_decision_result"] = live_result
        evidence["live_shadow_outcome"] = live_shadow_outcome
        if outcome == "fillable" and delayed_ask is not None:
            delayed_fee = fill_fee_per_share
            post_edge = fair - delayed_ask - delayed_fee
            evidence.update({
                "decision_ask": decision_ask, "delayed_ask": delayed_ask,
                "execution_delay_ms": delay_ms, "requested_qty": requested_qty,
                "limit_price": limit_price, "post_delay_edge": post_edge,
                "submission_latency_ms": 1000 * cfg.fast_submission_latency_seconds,
                "taker_delay_ms": 1000 * cfg.fast_taker_delay_seconds,
                "depth_safety_multiplier": cfg.fast_depth_safety_multiplier,
                "model_version": FAST_EXPERIMENT_VERSION,
                "delayed_book_age_ms": 1000 * delayed_quote["age_seconds"],
                "delayed_market_silence_ms": (
                    1000 * delayed_quote["market_silence_seconds"]
                ),
            })
            opened = _open_fast_trade(
                db, cfg, market, evidence, chosen, delayed_ask, requested_qty,
                active_strategy_id,
            )
            outcome = "filled" if opened else "portfolio_rejected"
        if shadow_eligible:
            shadow_outcome = "below_minimum_size"
            shadow_post_edge = None
            shadow_fill = None
            if shadow_requested_qty >= market["min_order_size"]:
                shadow_timing_rejection = execution_timing_reason(
                    delay_ms, cfg.fast_max_execution_delay_seconds,
                    end_ts - time.time(), cfg.fast_min_post_delay_seconds,
                )
                if shadow_timing_rejection:
                    shadow_outcome = shadow_timing_rejection
                elif delayed_quote is None:
                    shadow_outcome = "no_live_book"
                elif delayed_quote["price"] > shadow_limit_price + 1e-9:
                    shadow_outcome = "repriced_beyond_limit"
                else:
                    shadow_fill = simulate_fok_fill(
                        delayed_quote["levels"], shadow_requested_qty,
                        shadow_limit_price, market["fee_rate"],
                        cfg.fast_depth_safety_multiplier,
                    )
                    shadow_outcome = "fillable" if shadow_fill else "insufficient_depth"
            if shadow_outcome == "fillable" and shadow_fill is not None:
                shadow_post_edge = (
                    shadow_blended_fair
                    - shadow_fill["average_price"]
                    - shadow_fill["fee_per_share"]
                )
                if shadow_post_edge < required_edge:
                    shadow_outcome = "edge_gone_after_delay"
                else:
                    shadow_evidence.update({
                        "shadow_execution_delay_ms": delay_ms,
                        "shadow_limit_price": shadow_limit_price,
                        "shadow_requested_qty": shadow_requested_qty,
                        "shadow_fill_average_price": shadow_fill["average_price"],
                        "shadow_fill_fee_per_share": shadow_fill["fee_per_share"],
                        "shadow_fill_total_cost": shadow_fill["total_cost"],
                        "shadow_post_delay_edge": shadow_post_edge,
                    })
                    shadow_opened = _open_shadow_trade(
                        db, cfg, market, shadow_evidence, chosen,
                        shadow_fill["average_price"], shadow_requested_qty,
                        fair, shadow_market_fair, shadow_blended_fair,
                    )
                    shadow_outcome = "filled" if shadow_opened else "portfolio_rejected"
            shadow_evidence["shadow_execution_outcome"] = shadow_outcome
        if live_broker is not None:
            delayed_minimum_cost = None
            if delayed_top_ask is not None:
                delayed_unit_fee = (
                    market["fee_rate"] * delayed_top_ask * (1.0 - delayed_top_ask)
                )
                delayed_minimum_cost = market["min_order_size"] * (
                    delayed_top_ask + delayed_unit_fee
                )
            _record_live_shadow_decision(
                db, market, evidence, chosen, active_strategy_id, live_reason,
                sources_agree, live_base_budget, live_budget,
                market["min_order_size"], live_limit_price, delayed_top_ask,
                delayed_minimum_cost, live_qty, live_shadow_outcome,
            )
        _record_attempt(
            db, market, evidence, chosen, decision_ask, delayed_ask, requested_qty,
            delayed_depth, outcome, edge,
            post_edge, delay_ms, quote_age_ms,
        )
        if opened:
            notable.append(
                f"⚡ PAPER BTC {chosen.upper()} at {delayed_ask:.2f} after {delay_ms:.0f}ms, "
                f"{seconds_left:.1f}s left, post-delay edge {post_edge:.1%}"
            )
    if cfg.fast_v7_shadow_enabled and in_entry_window:
        _record_shadow_decision(
            db, shadow_evidence, chosen, ask, fair, shadow_market_fair,
            shadow_blended_fair, unit_fee if ask is not None else None,
            shadow_edge, required_edge, shadow_reason,
        )
    db.commit()
    return {
        "state": "paper_opened" if opened else "sampled",
        "slug": slug, "seconds_left": round(seconds_left, 2),
        "margin_bps": round(margin_bps, 3), "up_probability": round(probability, 4),
        "up_ask": up_ask, "down_ask": down_ask, "model_edge": round(edge, 4),
        "eligible": eligible, "required_edge": round(required_edge, 4),
        "trade_id": opened, "stored_ticks": stored, "live_book": live_book,
        "live_result": live_result,
        "shadow_trade_id": shadow_opened, "shadow_reason": shadow_reason,
        "shadow_settled": shadow_settled,
        "confirmation": confirmation, "settled": settled, "validated": validated,
        "notable": notable,
    }
