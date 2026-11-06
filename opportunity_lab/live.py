from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from .config import Config
from .db import BTC_V8_STRATEGY_ID, get_setting, set_setting


log = logging.getLogger("opportunity_lab.live")
LIVE_CONFIRMATION = "I_ACCEPT_REAL_MONEY_LOSS"
TERMINAL_REJECTIONS = (
    "couldn't be fully filled",
    "could not be fully filled",
    "fok_order_not_filled",
    "not enough balance",
    "minimum",
    "market is not yet ready",
    "invalid order",
    "invalid amounts",
    "invalid signature",
    "unauthorized",
    "geoblock",
)
BLOCKING_STATES = (
    "submitting",
    "matched",
    "uncertain",
    "live",
    "delayed",
    "resolved_waiting_redemption",
)


def _usd_units(value) -> float:
    """Convert CLOB fixed-point pUSD strings (6 decimals) to dollars."""
    if value is None:
        return 0.0
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0.0
    # API balance and order amount fields are integer fixed-math values. A
    # decimal response is already human-readable and is retained as-is.
    if raw == raw.to_integral_value() and abs(raw) >= 1000:
        raw /= Decimal(1_000_000)
    return float(raw)


def live_order_budget(
    balance_usd: float,
    allowance_usd: float,
    risk_fraction: float,
    max_order_usd: float,
    reserve_usd: float,
) -> float:
    """Cash cap for one order; never forces a minimum stake."""
    limits = [
        balance_usd * max(0.0, risk_fraction),
        max(0.0, balance_usd - max(0.0, reserve_usd)),
        max(0.0, allowance_usd),
    ]
    if max_order_usd > 0:
        limits.append(max_order_usd)
    return max(0.0, min(limits))


def minimum_order_bridge_budget(
    base_budget: float,
    balance_usd: float,
    allowance_usd: float,
    minimum_qty: float,
    limit_price: float,
    fee_rate: float,
    confidence: float,
    max_fraction: float,
    min_edge: float,
    max_order_usd: float,
    reserve_usd: float,
    normal_risk_tier: bool,
) -> tuple[float, str]:
    """Raise a strong signal only to the venue minimum, within a hard fraction."""
    unit_cost = limit_price + fee_rate * limit_price * (1.0 - limit_price)
    required = max(0.0, minimum_qty) * unit_cost
    if required <= base_budget + 1e-9:
        return base_budget, "not_needed"
    if not normal_risk_tier or max_fraction <= 0:
        return base_budget, "risk_tier"
    if confidence + 1e-9 < unit_cost + max(0.0, min_edge):
        return base_budget, "insufficient_worst_cost_edge"
    bridge_cap = live_order_budget(
        balance_usd, allowance_usd, max_fraction, max_order_usd, reserve_usd
    )
    if required > bridge_cap + 1e-9:
        return base_budget, "bridge_fraction_cap"
    return required, "bridged_to_minimum"


def market_buy_amount(
    requested_qty: float, limit_price: float, risk_budget_usd: float
) -> float:
    """Return a cent-precision collateral amount capped by the risk budget."""
    amount = min(
        Decimal(str(requested_qty)) * Decimal(str(limit_price)),
        Decimal(str(risk_budget_usd)),
    )
    return float(max(Decimal("0"), amount).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def total_buy_cost(maker_amount: float, share_qty: float, fee_rate: float) -> float:
    """Reconstruct the collateral outflow, including the crypto taker fee."""
    if maker_amount <= 0 or share_qty <= 0:
        return max(0.0, maker_amount)
    average_price = maker_amount / share_qty
    fee = share_qty * max(0.0, fee_rate) * average_price * (1.0 - average_price)
    return maker_amount + max(0.0, fee)


def validated_cash_outflow_cost(
    balance_before: float,
    balance_after: float,
    maker_amount: float,
    risk_budget: float,
) -> float | None:
    """Accept an exact wallet debit only when it is consistent with this fill."""
    outflow = balance_before - balance_after
    tolerance = max(0.02, 0.01 * max(0.0, risk_budget))
    if maker_amount - 1e-6 <= outflow <= risk_budget + tolerance:
        return outflow
    return None


def resolved_position_result(side: str, position: dict) -> str | None:
    """Interpret an owned position only after Polymarket marks it redeemable."""
    if not position.get("redeemable"):
        return None
    try:
        price = float(position.get("curPrice"))
    except (TypeError, ValueError):
        return None
    if price >= 0.99:
        return side
    if price <= 0.01:
        return "down" if side == "up" else "up"
    return None


def redemption_credit_matches(
    balance_usd: float,
    balance_before_usd: float,
    cost_usd: float,
    payout_usd: float,
    outcome_token_balance: float,
) -> bool:
    """Confirm that a winning token was consumed and its payout reached cash."""
    expected_balance = balance_before_usd - cost_usd + payout_usd
    return outcome_token_balance <= 1e-6 and balance_usd >= expected_balance - 0.01


def order_cap_reached(order_count: int, configured_cap: int) -> bool:
    """A non-positive count cap means ongoing trading rather than a trial quota."""
    return configured_cap > 0 and order_count >= configured_cap


def drawdown_risk_fraction(balance_usd: float, high_water_usd: float, cfg: Config) -> tuple[float, float]:
    """Return (risk fraction, drawdown) with progressively tighter sizing."""
    high_water = max(balance_usd, high_water_usd, 1e-9)
    drawdown = max(0.0, 1.0 - balance_usd / high_water)
    if drawdown >= cfg.live_stop_at_drawdown:
        return 0.0, drawdown
    if drawdown >= cfg.live_defensive_at_drawdown:
        return max(0.0, cfg.live_defensive_risk_fraction), drawdown
    if drawdown >= cfg.live_reduce_at_drawdown:
        return max(0.0, cfg.live_reduced_risk_fraction), drawdown
    return max(0.0, cfg.live_risk_fraction), drawdown


def strategy_equity_snapshot(db, wallet_balance_usd: float) -> dict[str, float]:
    """Reconstruct trading equity without treating deposits as strategy profit.

    The live order stream is single-flight, so its settled PnL sequence is a
    deterministic equity curve. Wallet cash remains the sizing constraint, but
    deposits and withdrawals affect only ``external_net_usd`` and cannot reset
    drawdown or session-loss controls.
    """
    initial = float(get_setting(db, "live_session_initial_balance", wallet_balance_usd))
    rows = db.execute(
        """SELECT realized_pnl FROM fast_live_orders
        WHERE state='settled' AND realized_pnl IS NOT NULL
        ORDER BY updated_ts,id"""
    ).fetchall()
    equity = initial
    high_water = initial
    realized = 0.0
    for row in rows:
        pnl = float(row[0])
        realized += pnl
        equity += pnl
        high_water = max(high_water, equity)
    return {
        "initial_usd": initial,
        "realized_pnl_usd": realized,
        "equity_usd": equity,
        "high_water_usd": high_water,
        "external_net_usd": float(wallet_balance_usd) - equity,
    }


def risk_cohort_snapshot(
    db, cohort, *, trailing_loss_cap: bool = True
) -> dict[str, float]:
    """Return PnL and loss room using trailing-peak or net-start accounting."""
    running = 0.0
    peak = 0.0
    rows = db.execute(
        """SELECT realized_pnl FROM fast_live_orders
        WHERE risk_cohort_id=? AND state='settled' AND realized_pnl IS NOT NULL
        AND created_ts>=? ORDER BY updated_ts,id""",
        (int(cohort["id"]), float(cohort["created_ts"])),
    ).fetchall()
    for row in rows:
        running += float(row[0])
        peak = max(peak, running)
    loss_cap = float(cohort["loss_cap_usd"])
    drawdown_usd = (
        max(0.0, peak - running) if trailing_loss_cap else max(0.0, -running)
    )
    remaining = (
        loss_cap - drawdown_usd
        if trailing_loss_cap else loss_cap + running
    )
    return {
        "realized_pnl_usd": running,
        "peak_pnl_usd": peak,
        "drawdown_usd": drawdown_usd,
        "loss_cap_usd": loss_cap,
        "remaining_loss_usd": max(0.0, remaining),
        "loss_utilization": drawdown_usd / loss_cap if loss_cap > 0 else 1.0,
        "trailing_loss_cap": float(trailing_loss_cap),
    }


def cohort_risk_fraction(snapshot: dict[str, float], cfg: Config) -> tuple[float, float]:
    """Map exact cohort loss-cap utilization onto the standard risk tiers."""
    utilization = max(0.0, float(snapshot["loss_utilization"]))
    if utilization >= 1.0:
        return 0.0, cfg.live_stop_at_drawdown
    mapped_drawdown = utilization * cfg.live_stop_at_drawdown
    if mapped_drawdown >= cfg.live_defensive_at_drawdown:
        return max(0.0, cfg.live_defensive_risk_fraction), mapped_drawdown
    if mapped_drawdown >= cfg.live_reduce_at_drawdown:
        return max(0.0, cfg.live_reduced_risk_fraction), mapped_drawdown
    return max(0.0, cfg.live_risk_fraction), mapped_drawdown


def daily_strategy_pnl(
    db, now: float | None = None, risk_cohort_id: int | None = None
) -> float:
    """Settled UTC-day PnL, independent of wallet deposits and withdrawals."""
    now = time.time() if now is None else now
    day_start = int(now // 86400) * 86400
    if risk_cohort_id is None:
        value = db.execute(
            """SELECT COALESCE(SUM(realized_pnl),0) FROM fast_live_orders
            WHERE state='settled' AND realized_pnl IS NOT NULL AND updated_ts>=?""",
            (day_start,),
        ).fetchone()[0]
    else:
        value = db.execute(
            """SELECT COALESCE(SUM(realized_pnl),0) FROM fast_live_orders
            WHERE risk_cohort_id=? AND state='settled'
            AND realized_pnl IS NOT NULL AND updated_ts>=?""",
            (risk_cohort_id, day_start),
        ).fetchone()[0]
    return float(value or 0.0)


def active_risk_cohort(db, strategy_version_id: str):
    return db.execute(
        """SELECT * FROM live_risk_cohorts
        WHERE strategy_version_id=? AND state='active'
        ORDER BY created_ts DESC LIMIT 1""",
        (strategy_version_id,),
    ).fetchone()


def daily_loss_fraction(day_start_balance_usd: float, cfg: Config) -> float:
    """Allow a wider daily experiment at tiny balances, then tighten it."""
    if day_start_balance_usd < cfg.live_low_balance_threshold_usd:
        return max(0.0, cfg.live_low_balance_daily_loss_fraction)
    return max(0.0, cfg.live_daily_loss_fraction)


def _minimum_allowance(response: dict) -> float:
    direct = response.get("allowance")
    if direct is not None:
        return _usd_units(direct)
    values = list((response.get("allowances") or {}).values())
    converted = [_usd_units(value) for value in values]
    return min(converted) if converted else 0.0


class LiveBroker:
    """Fail-closed, single-flight adapter for Polymarket live trading.

    The network submission runs in a background thread because the official
    SDK may poll an already-matched trade for up to 30 seconds. A durable
    `submitting` row is committed before the thread starts. Any crash or
    ambiguous network error therefore halts future orders instead of retrying.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = None
        self._preflight: dict | None = None
        self._results: queue.Queue[tuple[int, dict | None, str | None]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._last_position_reconcile = 0.0
        self._last_uncertain_reconcile = 0.0

    @property
    def requested(self) -> bool:
        return bool(self.cfg.live_trading_enabled)

    def gate_reason(self, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        if not self.cfg.live_trading_enabled:
            return "disabled"
        if self.cfg.live_confirmation != LIVE_CONFIRMATION:
            return "confirmation_missing"
        # A positive timestamp is an optional expiry. Zero deliberately means
        # no time limit; the explicit enable/confirmation and runtime risk
        # controls still fail closed independently.
        if 0 < self.cfg.live_armed_until <= now:
            return "arming_expired"
        required = (
            self.cfg.polymarket_private_key,
            self.cfg.polymarket_api_key,
            self.cfg.polymarket_api_secret,
            self.cfg.polymarket_api_passphrase,
        )
        if not all(required):
            return "credentials_missing"
        if self.cfg.polymarket_signature_type not in {0, 1, 2, 3}:
            return "invalid_signature_type"
        if self.cfg.polymarket_signature_type != 0 and not self.cfg.polymarket_funder:
            return "funder_missing"
        return None

    def _make_client(self):
        if self._client is not None:
            return self._client
        from py_clob_client_v2 import ApiCreds, ClobClient

        creds = ApiCreds(
            api_key=self.cfg.polymarket_api_key,
            api_secret=self.cfg.polymarket_api_secret,
            api_passphrase=self.cfg.polymarket_api_passphrase,
        )
        self._client = ClobClient(
            host=self.cfg.polymarket_clob_url,
            chain_id=self.cfg.polymarket_chain_id,
            key=self.cfg.polymarket_private_key,
            creds=creds,
            signature_type=self.cfg.polymarket_signature_type,
            funder=self.cfg.polymarket_funder,
            retry_on_error=False,
        )
        return self._client

    @staticmethod
    def _geoblock() -> dict:
        request = urllib.request.Request(
            "https://polymarket.com/api/geoblock",
            headers={"User-Agent": "opportunitylab-live-canary/1.0"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())

    def refresh(self, db) -> dict:
        gate = self.gate_reason()
        if gate:
            self._preflight = {"ok": False, "reason": gate, "checked_ts": time.time()}
            return dict(self._preflight)
        if self._thread and self._thread.is_alive():
            return dict(self._preflight or {"ok": False, "reason": "submission_in_flight"})
        try:
            geo = self._geoblock()
            if geo.get("blocked"):
                self._preflight = {
                    "ok": False,
                    "reason": "geoblocked",
                    "country": geo.get("country"),
                    "region": geo.get("region"),
                    "checked_ts": time.time(),
                }
                return dict(self._preflight)
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            client = self._make_client()
            funds = client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.cfg.polymarket_signature_type,
                )
            )
            open_orders = client.get_open_orders(only_first_page=True)
            balance = _usd_units(funds.get("balance"))
            allowance = _minimum_allowance(funds)
            self.reconcile_stale_submissions(db, client, open_orders)
            self._reconcile_matched_cash_outflow(db, balance)
            self._reconcile_redeemed_winners(db, balance, client)
            self._preflight = {
                "ok": balance > 0 and allowance > 0 and not open_orders,
                "reason": (
                    "no_balance" if balance <= 0 else
                    "no_allowance" if allowance <= 0 else
                    "venue_open_orders" if open_orders else "ready"
                ),
                "balance_usd": balance,
                "allowance_usd": allowance,
                "venue_open_orders": len(open_orders),
                "country": geo.get("country"),
                "region": geo.get("region"),
                "checked_ts": time.time(),
            }
            if self._preflight["ok"] and get_setting(db, "live_session_initial_balance") is None:
                set_setting(db, "live_session_initial_balance", balance)
            if self._preflight["ok"]:
                previous_high = float(get_setting(db, "live_balance_high_water", balance))
                set_setting(db, "live_balance_high_water", max(previous_high, balance))
                strategy = strategy_equity_snapshot(db, balance)
                set_setting(db, "live_strategy_equity", strategy)
            set_setting(db, "live_last_preflight", self._preflight)
            db.commit()
        except Exception as exc:
            log.exception("live preflight failed")
            self._preflight = {
                "ok": False,
                "reason": "preflight_error",
                "error": repr(exc)[:300],
                "checked_ts": time.time(),
            }
        return dict(self._preflight)

    def reconcile_stale_submissions(self, db, client, open_orders=None) -> int:
        """Fail closed after a crash between venue acceptance and queue drain."""
        rows = db.execute(
            """SELECT * FROM fast_live_orders
            WHERE state='submitting' AND created_ts<? ORDER BY created_ts""",
            (time.time() - 5,),
        ).fetchall()
        if not rows:
            return 0
        try:
            from py_clob_client_v2.clob_types import TradeParams
        except Exception:
            return 0
        reconciled = 0
        for row in rows:
            try:
                trades = client.get_trades(
                    TradeParams(
                        market=row["condition_id"], asset_id=row["token_id"],
                        after=max(0, int(float(row["created_ts"]) - 10)),
                    ),
                    only_first_page=False,
                )
            except Exception:
                log.exception("stale submission trade lookup failed for %s", row["id"])
                continue
            matches = []
            for trade in trades or []:
                try:
                    size = float(trade.get("size") or 0)
                    price = float(trade.get("price") or 0)
                except (TypeError, ValueError):
                    size = price = 0.0
                requested = float(row["requested_qty"])
                limit = float(row["limit_price"])
                risk = float(row["risk_budget_usd"])
                if (
                    str(trade.get("asset_id")) == str(row["token_id"])
                    and str(trade.get("side", "")).upper() == "BUY"
                    and requested * 0.80 <= size <= requested * 1.25
                    and 0 < price <= limit + 1e-9
                    and size * price <= risk + max(0.02, risk * 0.02)
                ):
                    matches.append((trade, size))
            if len(matches) == 1:
                trade, size = matches[0]
                db.execute(
                    """UPDATE fast_live_orders SET state='matched',updated_ts=?,
                    actual_qty=?,order_id=?,venue_status=?,response_json=?,error=NULL
                    WHERE id=? AND state='submitting'""",
                    (
                        time.time(), size,
                        str(trade.get("taker_order_id") or trade.get("id") or ""),
                        str(trade.get("status") or "matched"),
                        json.dumps(trade, sort_keys=True, separators=(",", ":")), row["id"],
                    ),
                )
                reconciled += 1
            elif time.time() - float(row["created_ts"]) > 120:
                db.execute(
                    """UPDATE fast_live_orders SET state='uncertain',updated_ts=?,
                    error='startup reconciliation found no unique matching trade'
                    WHERE id=? AND state='submitting'""",
                    (time.time(), row["id"]),
                )
                reconciled += 1
        if reconciled:
            db.commit()
        return reconciled

    def reconcile_uncertain_orders(self, db) -> int:
        """Resolve 'uncertain' orders against venue truth.

        Submission errors outside TERMINAL_REJECTIONS (e.g. a 503) park an
        order as 'uncertain'. Nothing else cleared that state, so a single
        venue hiccup permanently blocked the live_orders health check. Once
        the window has settled, query the venue for an actual fill: a unique
        matching trade upgrades the row to 'matched' (the normal settlement
        flow finishes it); no trade on a settled window means the order never
        reached the book, so the row is terminally rejected.
        """
        now = time.time()
        if now - self._last_uncertain_reconcile < 60:
            return 0
        rows = db.execute(
            """SELECT * FROM fast_live_orders
            WHERE state='uncertain' AND created_ts<? ORDER BY created_ts""",
            (now - 900,),
        ).fetchall()
        if not rows:
            self._last_uncertain_reconcile = now
            return 0
        self._last_uncertain_reconcile = now
        try:
            from py_clob_client_v2.clob_types import TradeParams
            client = self._make_client()
        except Exception:
            log.exception("uncertain order reconciliation could not build client")
            return 0
        reconciled = 0
        for row in rows:
            settled_window = db.execute(
                """SELECT 1 FROM fast_window_results
                WHERE slug=? AND validated_ts IS NOT NULL""",
                (row["slug"],),
            ).fetchone()
            try:
                trades = client.get_trades(
                    TradeParams(
                        market=row["condition_id"], asset_id=row["token_id"],
                        after=max(0, int(float(row["created_ts"]) - 10)),
                    ),
                    only_first_page=False,
                )
            except Exception:
                log.exception("uncertain order trade lookup failed for %s", row["id"])
                continue
            matches = []
            for trade in trades or []:
                try:
                    size = float(trade.get("size") or 0)
                    price = float(trade.get("price") or 0)
                except (TypeError, ValueError):
                    size = price = 0.0
                requested = float(row["requested_qty"])
                limit = float(row["limit_price"])
                risk = float(row["risk_budget_usd"])
                if (
                    str(trade.get("asset_id")) == str(row["token_id"])
                    and str(trade.get("side", "")).upper() == "BUY"
                    and requested * 0.80 <= size <= requested * 1.25
                    and 0 < price <= limit + 1e-9
                    and size * price <= risk + max(0.02, risk * 0.02)
                ):
                    matches.append((trade, size))
            if len(matches) == 1:
                trade, size = matches[0]
                db.execute(
                    """UPDATE fast_live_orders SET state='matched',updated_ts=?,
                    actual_qty=?,order_id=?,venue_status=?,error=NULL WHERE id=?""",
                    (
                        now, size,
                        str(trade.get("taker_order_id") or trade.get("id") or ""),
                        str(trade.get("status") or "matched"), row["id"],
                    ),
                )
                reconciled += 1
            elif not matches and settled_window:
                note = " | reconciliation: settled window with no venue trade"
                db.execute(
                    """UPDATE fast_live_orders SET state='rejected',updated_ts=?,
                    error=? WHERE id=? AND state='uncertain'""",
                    (now, (str(row["error"] or "") + note)[:1000], row["id"]),
                )
                reconciled += 1
        if reconciled:
            db.commit()
        return reconciled

    def _reconcile_matched_cash_outflow(self, db, balance_usd: float) -> int:
        rows = db.execute(
            """SELECT id,balance_before_usd,risk_budget_usd,response_json
            FROM fast_live_orders WHERE state='matched'"""
        ).fetchall()
        if len(rows) != 1:
            return 0
        row = rows[0]
        try:
            response = json.loads(row["response_json"] or "{}")
            maker_amount = _usd_units(response.get("makingAmount"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0
        exact_cost = validated_cash_outflow_cost(
            float(row["balance_before_usd"]),
            balance_usd,
            maker_amount,
            float(row["risk_budget_usd"]),
        )
        if exact_cost is None:
            return 0
        db.execute(
            "UPDATE fast_live_orders SET actual_cost_usd=? WHERE id=?",
            (exact_cost, row["id"]),
        )
        return 1

    def _reconcile_redeemed_winners(self, db, balance_usd: float, client) -> int:
        rows = db.execute(
            """SELECT id,token_id,balance_before_usd,actual_cost_usd,actual_qty
            FROM fast_live_orders
            WHERE state='resolved_waiting_redemption' AND side=result"""
        ).fetchall()
        settled = 0
        for row in rows:
            try:
                from py_clob_client_v2 import AssetType, BalanceAllowanceParams

                token_funds = client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=row["token_id"],
                        signature_type=self.cfg.polymarket_signature_type,
                    )
                )
                token_balance = _usd_units(token_funds.get("balance"))
            except Exception:
                log.exception("winning-token reconciliation failed for order %s", row["id"])
                continue
            if not redemption_credit_matches(
                balance_usd,
                float(row["balance_before_usd"]),
                float(row["actual_cost_usd"]),
                float(row["actual_qty"]),
                token_balance,
            ):
                continue
            db.execute(
                "UPDATE fast_live_orders SET state='settled',updated_ts=? WHERE id=?",
                (time.time(), row["id"]),
            )
            settled += 1
        return settled

    def refresh_if_due(self, db, seconds_left: float) -> dict | None:
        if not self.requested:
            return None
        # Never insert authentication/balance calls into the final decision
        # path. A missing or stale cache simply prevents the trade.
        if seconds_left <= self.cfg.fast_entry_window_seconds + 5:
            return self._preflight
        age = time.time() - float((self._preflight or {}).get("checked_ts", 0))
        if age >= max(10.0, self.cfg.live_preflight_max_age_seconds / 2):
            self.refresh(db)
        self.reconcile_resolved_positions(db)
        return self._preflight

    def budget(self, db, strategy_version_id: str = BTC_V8_STRATEGY_ID) -> tuple[float, str]:
        gate = self.gate_reason()
        if gate:
            return 0.0, gate
        factory_pause = get_setting(db, "factory_live_pause", {}) or {}
        if factory_pause.get("active"):
            return 0.0, "factory_health_pause"
        allocation = db.execute(
            """SELECT a.state,a.max_risk_fraction,a.max_order_usd,
            s.state strategy_state FROM factory_allocations a
            LEFT JOIN factory_strategy_specs s USING(strategy_version_id)
            WHERE a.strategy_version_id=?""",
            (strategy_version_id,),
        ).fetchone()
        if allocation is not None and allocation["state"] != "active":
            return 0.0, "strategy_allocation_paused"
        if allocation is not None and allocation["strategy_state"] not in (None, "live"):
            return 0.0, "strategy_quarantined"
        breaker = get_setting(db, f"live_circuit_breaker:{strategy_version_id}", {}) or {}
        if breaker.get("active") and strategy_version_id in (breaker.get("lineage") or []):
            return 0.0, "live_circuit_breaker"
        preflight = self._preflight
        if not preflight:
            return 0.0, "preflight_missing"
        if time.time() - float(preflight.get("checked_ts", 0)) > self.cfg.live_preflight_max_age_seconds:
            return 0.0, "preflight_stale"
        if not preflight.get("ok"):
            return 0.0, str(preflight.get("reason") or "preflight_failed")
        pending = db.execute(
            "SELECT COUNT(*) FROM fast_live_orders WHERE state IN (%s)"
            % ",".join("?" for _ in BLOCKING_STATES),
            BLOCKING_STATES,
        ).fetchone()[0]
        if pending:
            return 0.0, "unreconciled_live_order"
        total = db.execute(
            "SELECT COUNT(*) FROM fast_live_orders WHERE state NOT IN ('rejected','skipped')"
        ).fetchone()[0]
        if order_cap_reached(total, self.cfg.live_max_orders_total):
            return 0.0, "total_order_cap"
        day = time.time() - 86400
        daily = db.execute(
            "SELECT COUNT(*) FROM fast_live_orders WHERE created_ts>=? AND state NOT IN ('rejected','skipped')",
            (day,),
        ).fetchone()[0]
        if order_cap_reached(daily, self.cfg.live_max_orders_per_day):
            return 0.0, "daily_order_cap"
        balance = float(preflight["balance_usd"])
        strategy = strategy_equity_snapshot(db, balance)
        cohort = (
            active_risk_cohort(db, strategy_version_id)
            if self.cfg.live_auto_cohort_enabled else None
        )
        cohort_state = (
            risk_cohort_snapshot(
                db, cohort,
                trailing_loss_cap=self.cfg.live_cohort_trailing_loss_cap,
            )
            if cohort is not None else None
        )
        cohort_remaining = None
        if cohort_state is not None:
            risk_fraction, drawdown = cohort_risk_fraction(cohort_state, self.cfg)
            cohort_remaining = float(cohort_state["remaining_loss_usd"])
            if risk_fraction <= 0 or cohort_remaining <= 0:
                db.execute(
                    """UPDATE live_risk_cohorts SET state='stopped',ended_ts=?,
                    stop_reason='hard cohort loss cap reached' WHERE id=? AND state='active'""",
                    (time.time(), cohort["id"]),
                )
                db.commit()
                return 0.0, "cohort_loss_stop"
            session_pnl = float(cohort_state["realized_pnl_usd"])
        else:
            strategy_equity = float(strategy["equity_usd"])
            strategy_high_water = float(strategy["high_water_usd"])
            risk_fraction, drawdown = drawdown_risk_fraction(
                strategy_equity, strategy_high_water, self.cfg
            )
            session_pnl = float(strategy["realized_pnl_usd"])
        if (
            self.cfg.live_max_session_loss_usd > 0
            and session_pnl <= -self.cfg.live_max_session_loss_usd
        ):
            return 0.0, "session_loss_limit"
        if allocation is not None and float(allocation["max_risk_fraction"]) > 0:
            risk_fraction = min(risk_fraction, float(allocation["max_risk_fraction"]))
        if risk_fraction <= 0:
            return 0.0, "drawdown_stop"

        if cohort is not None:
            day_pnl = daily_strategy_pnl(db, risk_cohort_id=int(cohort["id"]))
            loss_fraction = daily_loss_fraction(balance, self.cfg)
            if day_pnl <= -balance * loss_fraction:
                return 0.0, "daily_loss_limit"
        else:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            day_key = get_setting(db, "live_balance_day")
            day_pnl = daily_strategy_pnl(db)
            day_start_equity = strategy_equity - day_pnl
            if day_key != day:
                set_setting(db, "live_balance_day", day)
                set_setting(db, "live_day_start_balance", balance)
                set_setting(db, "live_day_start_strategy_equity", day_start_equity)
                db.commit()
            loss_fraction = daily_loss_fraction(day_start_equity, self.cfg)
            if strategy_equity <= day_start_equity * (1.0 - loss_fraction):
                return 0.0, "daily_loss_limit"
        allocation_max = float(allocation["max_order_usd"]) if allocation is not None else 0.0
        configured_max = self.cfg.live_max_order_usd
        effective_max = (
            min(value for value in (allocation_max, configured_max) if value > 0)
            if allocation_max > 0 and configured_max > 0
            else max(allocation_max, configured_max)
        )
        if cohort_remaining is not None:
            effective_max = (
                min(effective_max, cohort_remaining)
                if effective_max > 0 else cohort_remaining
            )
        amount = live_order_budget(
            balance,
            float(preflight["allowance_usd"]),
            risk_fraction,
            effective_max,
            self.cfg.live_min_cash_reserve_usd,
        )
        if amount <= 0:
            return 0.0, "cash_reserve"
        suffix = f":cohort={int(cohort['id'])}" if cohort is not None else ""
        return amount, f"ready:{risk_fraction:.3f}:dd={drawdown:.3f}{suffix}"

    def minimum_bridge_budget(
        self,
        db,
        base_budget: float,
        budget_reason: str,
        minimum_qty: float,
        limit_price: float,
        fee_rate: float,
        confidence: float,
        strategy_version_id: str = BTC_V8_STRATEGY_ID,
    ) -> tuple[float, str]:
        preflight = self._preflight or {}
        allocation = db.execute(
            """SELECT max_order_usd FROM factory_allocations
            WHERE strategy_version_id=?""", (strategy_version_id,)
        ).fetchone()
        allocation_max = float(allocation[0]) if allocation is not None else 0.0
        configured_max = self.cfg.live_max_order_usd
        effective_max = (
            min(value for value in (allocation_max, configured_max) if value > 0)
            if allocation_max > 0 and configured_max > 0
            else max(allocation_max, configured_max)
        )
        cohort = (
            active_risk_cohort(db, strategy_version_id)
            if self.cfg.live_auto_cohort_enabled else None
        )
        if cohort is not None:
            remaining = risk_cohort_snapshot(
                db, cohort,
                trailing_loss_cap=self.cfg.live_cohort_trailing_loss_cap,
            )["remaining_loss_usd"]
            effective_max = (
                min(effective_max, remaining) if effective_max > 0 else remaining
            )
        confirmed_spec = db.execute(
            """SELECT 1 FROM factory_strategy_specs
            WHERE strategy_version_id=? AND state='live'
            AND json_extract(parameters_json,'$.require_source_confirmation')=1""",
            (strategy_version_id,),
        ).fetchone()
        if (
            self.cfg.live_cohort_minimum_floor_enabled
            and cohort is not None
            and confirmed_spec is not None
        ):
            unit_cost = limit_price + fee_rate * limit_price * (1.0 - limit_price)
            required = max(0.0, minimum_qty) * unit_cost
            if required <= base_budget + 1e-9:
                return base_budget, "not_needed"
            floor_cap = live_order_budget(
                float(preflight.get("balance_usd", 0)),
                float(preflight.get("allowance_usd", 0)),
                1.0,
                effective_max,
                self.cfg.live_min_cash_reserve_usd,
            )
            if required <= floor_cap + 1e-9:
                return required, "cohort_minimum_floor"
            return base_budget, "cohort_minimum_floor_cap"
        if not self.cfg.live_minimum_bridge_enabled:
            return base_budget, "disabled"
        normal_prefix = f"ready:{self.cfg.live_risk_fraction:.3f}:"
        reduced_prefix = f"ready:{self.cfg.live_reduced_risk_fraction:.3f}:"
        if budget_reason.startswith(normal_prefix):
            bridge_allowed = True
            bridge_fraction = self.cfg.live_minimum_bridge_max_fraction
            bridge_edge = self.cfg.live_minimum_bridge_min_edge
        elif budget_reason.startswith(reduced_prefix):
            bridge_allowed = True
            bridge_fraction = self.cfg.live_reduced_bridge_max_fraction
            bridge_edge = self.cfg.live_reduced_bridge_min_edge
        else:
            bridge_allowed = False
            bridge_fraction = self.cfg.live_reduced_bridge_max_fraction
            bridge_edge = self.cfg.live_reduced_bridge_min_edge
        return minimum_order_bridge_budget(
            base_budget=base_budget,
            balance_usd=float(preflight.get("balance_usd", 0)),
            allowance_usd=float(preflight.get("allowance_usd", 0)),
            minimum_qty=minimum_qty,
            limit_price=limit_price,
            fee_rate=fee_rate,
            confidence=confidence,
            max_fraction=bridge_fraction,
            min_edge=bridge_edge,
            max_order_usd=effective_max,
            reserve_usd=self.cfg.live_min_cash_reserve_usd,
            normal_risk_tier=bridge_allowed,
        )

    def submit_fok(
        self,
        db,
        market: dict,
        evidence: dict,
        side: str,
        qty: float,
        limit_price: float,
        risk_budget_usd: float,
        strategy_version_id: str = BTC_V8_STRATEGY_ID,
    ) -> dict:
        if self._thread and self._thread.is_alive():
            return {"state": "skipped", "reason": "submission_in_flight"}
        existing = db.execute(
            "SELECT id,state FROM fast_live_orders WHERE slug=?", (market["slug"],)
        ).fetchone()
        if existing:
            return {"state": "skipped", "reason": "already_attempted"}
        amount = market_buy_amount(qty, limit_price, risk_budget_usd)
        if amount <= 0:
            return {"state": "skipped", "reason": "order_amount_below_one_cent"}
        balance = float((self._preflight or {}).get("balance_usd", 0))
        cohort = (
            active_risk_cohort(db, strategy_version_id)
            if self.cfg.live_auto_cohort_enabled else None
        )
        cohort_id = int(cohort["id"]) if cohort is not None else None
        evidence = dict(evidence)
        evidence["live_risk_cohort_id"] = cohort_id
        now = time.time()
        cur = db.execute(
            """INSERT INTO fast_live_orders
            (slug,condition_id,token_id,side,created_ts,updated_ts,state,
             requested_qty,limit_price,risk_budget_usd,balance_before_usd,evidence_json,
             strategy_version_id,risk_cohort_id)
            VALUES(?,?,?,?,?,?,'submitting',?,?,?,?,?,?,?)""",
            (
                market["slug"], market["condition_id"], market[f"{side}_token"], side,
                now, now, qty, limit_price, risk_budget_usd, balance,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                strategy_version_id, cohort_id,
            ),
        )
        row_id = int(cur.lastrowid)
        db.commit()
        self._thread = threading.Thread(
            target=self._submit_worker,
            args=(row_id, market[f"{side}_token"], amount, limit_price, market["tick_size"]),
            daemon=True,
            name="polymarket-live-submit",
        )
        self._thread.start()
        return {"state": "submitting", "live_order_id": row_id}

    def _submit_worker(
        self, row_id: int, token_id: str, amount: float, limit_price: float, tick_size: float
    ) -> None:
        try:
            from py_clob_client_v2 import (
                MarketOrderArgs,
                OrderType,
                PartialCreateOrderOptions,
                Side,
            )

            response = self._make_client().create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=token_id,
                    price=limit_price,
                    amount=amount,
                    side=Side.BUY,
                    user_usdc_balance=float((self._preflight or {}).get("balance_usd", 0)),
                ),
                options=PartialCreateOrderOptions(
                    tick_size=format(tick_size, ".10g"), neg_risk=False
                ),
                order_type=OrderType.FOK,
            )
            self._results.put((row_id, response, None))
        except Exception as exc:
            self._results.put((row_id, None, repr(exc)[:1000]))

    def drain(self, db) -> list[str]:
        notable: list[str] = []
        while True:
            try:
                row_id, response, error = self._results.get_nowait()
            except queue.Empty:
                break
            now = time.time()
            if error is not None:
                lowered = error.lower()
                state = "rejected" if any(text in lowered for text in TERMINAL_REJECTIONS) else "uncertain"
                db.execute(
                    "UPDATE fast_live_orders SET state=?,updated_ts=?,error=? WHERE id=?",
                    (state, now, error, row_id),
                )
                notable.append(f"⚠️ LIVE order {row_id} {state}: {error[:180]}")
                continue
            response = response if isinstance(response, dict) else {"raw": response}
            success = bool(response.get("success"))
            venue_status = str(response.get("status") or "").lower()
            error_msg = str(response.get("errorMsg") or "")
            if success and venue_status == "matched":
                state = "matched"
            elif not success or error_msg:
                state = "rejected"
            else:
                # FOK should be all-or-nothing. Any other successful status is
                # treated as ambiguous and freezes the canary for review.
                state = "uncertain"
            maker_amount = _usd_units(response.get("makingAmount")) or None
            actual_qty = _usd_units(response.get("takingAmount")) or None
            actual_cost = (
                total_buy_cost(
                    maker_amount,
                    actual_qty,
                    self.cfg.polymarket_crypto_fee_coefficient,
                )
                if maker_amount is not None and actual_qty is not None
                else maker_amount
            )
            db.execute(
                """UPDATE fast_live_orders SET state=?,updated_ts=?,actual_qty=?,
                actual_cost_usd=?,order_id=?,venue_status=?,response_json=?,error=? WHERE id=?""",
                (
                    state, now, actual_qty, actual_cost, response.get("orderID"),
                    venue_status, json.dumps(response, sort_keys=True, default=str),
                    error_msg or None, row_id,
                ),
            )
            notable.append(
                f"{'🔴' if state == 'matched' else '⚠️'} LIVE order {row_id} {state.upper()} "
                f"{('$' + format(actual_cost, '.2f')) if actual_cost is not None else ''}".strip()
            )
        if notable:
            db.commit()
        return notable

    def mark_resolutions(self, db) -> int:
        rows = db.execute(
            """SELECT l.id,l.side,l.requested_qty,l.limit_price,l.actual_qty,l.actual_cost_usd,
            w.venue_result FROM fast_live_orders l JOIN fast_window_results w ON w.slug=l.slug
            WHERE l.state='matched' AND w.venue_result IN ('up','down')"""
        ).fetchall()
        for row in rows:
            qty = float(row["actual_qty"] or row["requested_qty"])
            cost = float(row["actual_cost_usd"] or qty * float(row["limit_price"]))
            payout = qty if row["side"] == row["venue_result"] else 0.0
            state = (
                "resolved_waiting_redemption"
                if row["side"] == row["venue_result"]
                else "settled"
            )
            db.execute(
                """UPDATE fast_live_orders SET state=?,updated_ts=?,result=?,realized_pnl=?
                WHERE id=?""",
                (state, time.time(), row["venue_result"], payout - cost, row["id"]),
            )
        if rows:
            db.commit()
        return len(rows)

    def reconcile_resolved_positions(self, db) -> int:
        """Resolve owned live positions without relying on Gamma's lagging closed flag."""
        now = time.time()
        if now - self._last_position_reconcile < 15:
            return 0
        rows = db.execute(
            """SELECT id,condition_id,side,actual_qty,requested_qty,actual_cost_usd,
            limit_price FROM fast_live_orders WHERE state='matched'"""
        ).fetchall()
        if not rows:
            return 0
        self._last_position_reconcile = now
        resolved = 0
        for row in rows:
            try:
                url = "https://data-api.polymarket.com/positions?" + urllib.parse.urlencode(
                    {"user": self.cfg.polymarket_funder, "market": row["condition_id"]}
                )
                request = urllib.request.Request(
                    url, headers={"User-Agent": "opportunitylab-live/1.0"}
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    positions = json.loads(response.read().decode())
            except Exception:
                log.exception("live position reconciliation failed for order %s", row["id"])
                continue
            position = next(
                (
                    item for item in positions
                    if str(item.get("outcome") or "").strip().lower() == row["side"]
                ),
                None,
            )
            if not position:
                continue
            result = resolved_position_result(row["side"], position)
            if result is None:
                continue
            qty = float(row["actual_qty"] or row["requested_qty"])
            cost = float(row["actual_cost_usd"] or qty * float(row["limit_price"]))
            won = row["side"] == result
            state = "resolved_waiting_redemption" if won else "settled"
            payout = qty if won else 0.0
            db.execute(
                """UPDATE fast_live_orders SET state=?,updated_ts=?,result=?,realized_pnl=?
                WHERE id=? AND state='matched'""",
                (state, now, result, payout - cost, row["id"]),
            )
            resolved += 1
        if resolved:
            db.commit()
        return resolved

    def status(self, db) -> dict:
        gate = self.gate_reason()
        counts = db.execute(
            "SELECT state,COUNT(*) n FROM fast_live_orders GROUP BY state"
        ).fetchall()
        return {
            "requested": self.requested,
            "gate": gate,
            "preflight": dict(self._preflight or {}),
            "orders": {row["state"]: row["n"] for row in counts},
        }

    def shutdown(self, db, timeout: float = 15.0) -> None:
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(max(0.0, timeout))
        self.drain(db)
        if thread and thread.is_alive():
            db.execute(
                """UPDATE fast_live_orders SET state='uncertain',updated_ts=?,
                error='process stopped while submission worker was still active'
                WHERE state='submitting'""",
                (time.time(),),
            )
            db.commit()
