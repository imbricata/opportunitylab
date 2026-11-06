import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from opportunity_lab.config import CFG
from opportunity_lab.db import (
    FAST_EXPERIMENT_VERSION,
    FAST_SHADOW_VERSION,
    balance,
    connect,
    ensure_initial_balance,
    fast_experiment_start,
    fast_shadow_balance,
    fast_shadow_start,
    get_setting,
    set_setting,
)
from opportunity_lab.fastlane import (
    MarketBookStream,
    PriceStream,
    _open_shadow_trade,
    blended_probability,
    boundary_price,
    _clob_token_map,
    book_capacity,
    execution_outcome,
    execution_timing_reason,
    market_midpoint_up_probability,
    market_conditioned_up_probability,
    market_metadata,
    order_quantity,
    projected_twap_close,
    screen_reason,
    settle_shadow_trades,
    simulate_fok_fill,
    source_confirmation,
    twap_projection_up_probability,
    up_probability,
    v8_screen_reason,
    volatility_per_s,
)
from opportunity_lab.live import (
    LIVE_CONFIRMATION,
    LiveBroker,
    daily_strategy_pnl,
    daily_loss_fraction,
    drawdown_risk_fraction,
    live_order_budget,
    market_buy_amount,
    minimum_order_bridge_budget,
    order_cap_reached,
    resolved_position_result,
    redemption_credit_matches,
    strategy_equity_snapshot,
    total_buy_cost,
    validated_cash_outflow_cost,
)
from opportunity_lab.markets import Leg, Market, Opportunity
from opportunity_lab.main import _next_broad_scan
from opportunity_lab.paper import manage_trades, open_trade, record_opportunity
from opportunity_lab.strategies import complement_arbitrage, directional_arbitrage


def market(**changes):
    base = Market(
        venue="test", market_id="m1", event_id="e1", title="Test outcome",
        rules="Resolves YES or NO", status="open", yes_bid=0.42, yes_ask=0.43,
        no_bid=0.50, no_ask=0.51, yes_bid_size=20, yes_ask_size=20,
        no_bid_size=20, no_ask_size=20, volume=1000, fee_coefficient=0.05,
    )
    return replace(base, **changes)


class StrategyTests(unittest.TestCase):
    def test_complement_arb_is_fee_aware(self):
        cfg = replace(CFG, prediction_min_net_edge=0.001)
        found = complement_arbitrage([market()], cfg)
        self.assertEqual(len(found), 1)
        self.assertGreater(found[0].net_edge_per_unit, 0)
        expensive = market(yes_ask=0.50, no_ask=0.50)
        self.assertEqual(complement_arbitrage([expensive], cfg), [])

    def test_threshold_implication_buys_superset_yes_and_subset_no(self):
        cfg = replace(CFG, prediction_min_net_edge=0.001)
        low = market(
            market_id="low", title="Temperature above 70", strike_type="greater",
            floor_strike=70, yes_ask=0.40, no_ask=0.70,
        )
        high = market(
            market_id="high", title="Temperature above 80", strike_type="greater",
            floor_strike=80, yes_ask=0.20, no_ask=0.50,
        )
        found = directional_arbitrage([low, high], cfg)
        self.assertEqual(len(found), 1)
        self.assertEqual(
            {(leg.market_id, leg.side) for leg in found[0].legs},
            {("high", "no"), ("low", "yes")},
        )


class PaperLedgerTests(unittest.TestCase):
    def test_open_and_settle_are_cash_reconciled(self):
        with tempfile.TemporaryDirectory() as td:
            db = connect(Path(td) / "lab.db")
            ensure_initial_balance(db, 100)
            cfg = replace(CFG, auto_paper_trade=True, paper_trade_budget_usd=10)
            opportunity = Opportunity(
                strategy="binary_complement", title="both", guaranteed_payout_per_unit=1,
                legs=(
                    Leg("test", "m1", "yes", 0.43, 20, 0.05),
                    Leg("test", "m1", "no", 0.51, 20, 0.05),
                ),
            )
            opportunity_id = record_opportunity(db, cfg, opportunity, "approved")
            trade_id = open_trade(db, cfg, opportunity_id, opportunity)
            self.assertIsNotNone(trade_id)
            self.assertLess(balance(db), 100)

            class Connector:
                venue = "test"

                @staticmethod
                def get_market(_market_id):
                    return market(status="closed", result="yes")

            result = manage_trades(db, cfg, [Connector()])
            db.commit()
            self.assertEqual(result["settled"], 1)
            row = db.execute(
                "SELECT status,realized_pnl FROM paper_trades_v2 WHERE id=?", (trade_id,)
            ).fetchone()
            self.assertEqual(row["status"], "settled")
            self.assertAlmostEqual(balance(db), 100 + row["realized_pnl"])


class FastLaneTests(unittest.TestCase):
    def test_price_stream_drains_only_new_ticks(self):
        stream = PriceStream()
        stream._append("chainlink", "btc/usd", 100.0, 50000.0)
        self.assertEqual(len(stream.drain_pending()), 1)
        self.assertEqual(stream.drain_pending(), [])
        self.assertEqual(len(stream.ticks(since=99.0)), 1)

    def test_twap60_snapshot_is_parsed_as_distinct_settlement_source(self):
        stream = PriceStream()
        inserted = stream._ingest_twap60({
            "topic": "crypto_prices_twap_sixty",
            "payload": {
                "symbol": "btc/usd", "window_s": 60,
                "data": [
                    {"timestamp": 1789568000000, "value": 50000.25},
                    {"timestamp": 1789568001000, "value": 50001.50},
                ],
            },
        })
        self.assertEqual(inserted, 2)
        ticks = stream.drain_pending()
        self.assertEqual({tick["source"] for tick in ticks}, {"chainlink_twap60"})
        self.assertEqual(
            [tick["source_ts"] for tick in ticks],
            [1789568000.0, 1789568001.0],
        )
        self.assertTrue(stream.source_status["chainlink_twap60"])

    def test_twap_boundary_can_require_exact_boundary_second(self):
        ticks = [
            {"source": "chainlink_twap60", "source_ts": 101.2, "price": 50000},
        ]
        self.assertIsNone(
            boundary_price(ticks, "chainlink_twap60", 100.0, max_delay=1.1)
        )
        self.assertEqual(
            boundary_price(ticks, "chainlink_twap60", 100.0, max_delay=1.3),
            50000,
        )

    def test_clob_token_map_uses_explicit_outcomes_not_array_position(self):
        raw = {"tokens": [
            {"token_id": "down-token", "outcome": "Down"},
            {"token_id": "up-token", "outcome": "Up"},
        ]}
        self.assertEqual(
            _clob_token_map(raw), {"up": "up-token", "down": "down-token"}
        )

    def test_market_metadata_selects_declared_twap60_settlement_feed(self):
        gamma = {
            "conditionId": "condition", "outcomes": '["Up","Down"]',
            "clobTokenIds": '["up-token","down-token"]',
            "eventStartTime": "2026-09-16T00:00:00Z",
            "endDate": "2026-09-16T00:05:00Z",
            "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "cryptoMarketConfig": {
                "twapEnabled": True, "twapLookbackSeconds": 60,
            },
        }
        clob = {"market_slug": "test-twap", "tokens": [
            {"token_id": "up-token", "outcome": "Up"},
            {"token_id": "down-token", "outcome": "Down"},
        ]}
        with patch(
            "opportunity_lab.fastlane._json_get", side_effect=[gamma, clob]
        ):
            result = market_metadata("test-twap", fresh=True, verify_tokens=True)
        self.assertEqual(result["settlement_source"], "chainlink_twap60")
        self.assertEqual(result["twap_lookback_seconds"], 60)

    def test_new_execution_version_starts_a_new_cohort(self):
        with tempfile.TemporaryDirectory() as td:
            db = connect(Path(td) / "lab.db")
            set_setting(db, "fast_livebook_experiment_start", 100.0)
            set_setting(db, "fast_experiment_version", "legacy")
            started = fast_experiment_start(db)
            self.assertGreater(started, 100.0)
            self.assertEqual(get_setting(db, "fast_experiment_version"), FAST_EXPERIMENT_VERSION)

    def test_broad_scan_is_aligned_after_boundary(self):
        self.assertEqual(_next_broad_scan(601), 905)

    def test_boundary_requires_tick_after_boundary(self):
        ticks = [
            {"source": "chainlink", "source_ts": 99.9, "price": 100},
            {"source": "chainlink", "source_ts": 100.2, "price": 101},
        ]
        self.assertEqual(boundary_price(ticks, "chainlink", 100), 101)
        self.assertIsNone(boundary_price(ticks, "binance", 100))

    def test_probability_moves_with_margin(self):
        self.assertGreater(up_probability(101, 100, 5, 0.001, 0.2, 0.85), 0.5)
        self.assertLess(up_probability(99, 100, 5, 0.001, 0.2, 0.85), 0.5)
        self.assertLessEqual(up_probability(110, 100, 5, 0.001, 0.2, 0.85), 0.85)

    def test_twap_projection_replaces_only_unknown_tail(self):
        projected = projected_twap_close(
            current_twap=100.0, current_spot=110.0, outgoing_spot=90.0,
            seconds_left=6.0, window_seconds=60.0,
        )
        self.assertAlmostEqual(projected, 102.0)
        probability, standard_error = twap_projection_up_probability(
            projected, 100.0, 110.0, 0.001, 6.0,
        )
        self.assertGreater(probability, 0.99)
        self.assertGreaterEqual(standard_error, 0.20)

    def test_v7_blends_oracle_with_two_sided_market_midpoint(self):
        market_up = market_midpoint_up_probability(0.40, 0.62)
        self.assertAlmostEqual(market_up, 0.39)
        self.assertAlmostEqual(blended_probability(0.80, market_up, 0.50), 0.595)
        self.assertIsNone(market_midpoint_up_probability(0.40, None))

    def test_v8_market_conditioning_and_confidence_screen(self):
        aligned = market_conditioned_up_probability(0.80, 0.80, 0.02, 4.0, 0.00002)
        opposed = market_conditioned_up_probability(0.80, 0.20, 0.02, 4.0, 0.00002)
        self.assertAlmostEqual(aligned, 0.8886057692)
        self.assertGreater(aligned, opposed)
        self.assertEqual(
            v8_screen_reason(True, True, True, 0.70, 0.80, 0.75, 0.70),
            "execution_candidate",
        )
        self.assertEqual(
            v8_screen_reason(True, True, True, 0.70, 0.80, 0.65, 0.70),
            "confidence_below_threshold",
        )
        self.assertEqual(
            v8_screen_reason(True, True, True, 0.81, 0.80, 0.90, 0.70),
            "ask_above_cap",
        )
        self.assertTrue(FAST_EXPERIMENT_VERSION.endswith("v8"))

    def test_v7_shadow_has_isolated_balance_and_local_settlement(self):
        with tempfile.TemporaryDirectory() as td:
            db = connect(Path(td) / "lab.db")
            fast_shadow_start(db)
            cfg = replace(CFG, fast_starting_cash_usd=20, fast_trade_budget_usd=5)
            evidence = {
                "seconds_left": 10.0,
                "shadow_fill_fee_per_share": 0.0147,
            }
            trade_id = _open_shadow_trade(
                db,
                cfg,
                {"slug": "v7-window", "fee_rate": 0.07, "min_order_size": 5},
                evidence,
                "up",
                0.30,
                10.0,
                0.80,
                0.39,
                0.595,
            )
            self.assertIsNotNone(trade_id)
            self.assertAlmostEqual(fast_shadow_balance(db, 20), 16.853)
            db.execute(
                """INSERT INTO fast_window_results
                (slug,start_ts,end_ts,opening_price,ending_price,observed_result,
                 venue_result,result_matches,validated_ts,evidence_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("v7-window", 1, 2, 100, 101, "up", "up", 1, time.time(), "{}"),
            )
            self.assertEqual(settle_shadow_trades(db), 1)
            self.assertAlmostEqual(fast_shadow_balance(db, 20), 26.853)
            row = db.execute(
                "SELECT status,result,realized_pnl FROM fast_shadow_trades WHERE id=?",
                (trade_id,),
            ).fetchone()
            self.assertEqual((row["status"], row["result"]), ("settled", "up"))
            self.assertAlmostEqual(row["realized_pnl"], 6.853)
            self.assertTrue(FAST_SHADOW_VERSION.endswith("v7"))

    def test_irregular_tick_volatility_is_finite(self):
        ticks = [
            {"source": "chainlink", "source_ts": 90 + i, "price": 100 + (i % 2) * 0.1}
            for i in range(10)
        ]
        sigma = volatility_per_s(ticks, 100, 20)
        self.assertIsNotNone(sigma)
        self.assertGreater(sigma, 0)

    def test_independent_sources_confirm_direction_from_own_boundaries(self):
        ticks = [
            {"source": "coinbase", "source_ts": 100.1, "price": 200},
            {"source": "coinbase", "source_ts": 109.0, "price": 201},
            {"source": "kraken", "source_ts": 100.2, "price": 300},
            {"source": "kraken", "source_ts": 109.0, "price": 299},
        ]
        result = source_confirmation(ticks, 100, 109, 1)
        self.assertEqual(result["agree"], 1)
        self.assertEqual(result["disagree"], 1)
        self.assertFalse(result["state"])

    def test_delayed_fill_rejects_slippage_and_depth(self):
        self.assertEqual(execution_outcome(0.70, 0.73, 10, 5, 0.02), "repriced_beyond_limit")
        self.assertEqual(execution_outcome(0.70, 0.71, 4, 5, 0.02), "insufficient_depth")
        self.assertEqual(execution_outcome(0.70, 0.71, 6, 5, 0.02), "fillable")
        self.assertEqual(
            execution_outcome(0.70, 0.71, 6, 5, 0.02, 1.25),
            "insufficient_depth",
        )
        self.assertEqual(
            execution_timing_reason(1001, 1.0, 5.0, 0.75),
            "execution_delay_exceeded",
        )
        self.assertEqual(
            execution_timing_reason(500, 1.0, 0.5, 0.75),
            "too_late_after_delay",
        )

    def test_fixed_quantity_is_sized_at_limit_with_depth_cushion(self):
        qty, limit_price = order_quantity(5, 0.50, 100, 0.02, 0.07, 1.25)
        self.assertEqual(limit_price, 0.52)
        worst_fee = 0.07 * limit_price * (1 - limit_price)
        self.assertLessEqual(qty * (limit_price + worst_fee), 5.0 + 1e-9)
        self.assertLessEqual(qty * 1.25, 100)

    def test_live_budget_is_balance_dependent_and_preserves_reserve(self):
        self.assertAlmostEqual(live_order_budget(20, 100, 0.18, 0, 0), 3.6)
        self.assertAlmostEqual(live_order_budget(30, 100, 0.18, 0, 0), 5.4)
        self.assertEqual(live_order_budget(20, 100, 0.18, 3, 0), 3)
        self.assertEqual(live_order_budget(9, 100, 0.18, 0, 8), 1)

    def test_minimum_bridge_requires_worst_cost_edge_and_fraction_room(self):
        args = dict(
            base_budget=2.39, balance_usd=15.90, allowance_usd=100,
            minimum_qty=5, fee_rate=0.07, max_fraction=0.24,
            min_edge=0.03, max_order_usd=0, reserve_usd=0,
            normal_risk_tier=True,
        )
        budget, reason = minimum_order_bridge_budget(
            limit_price=0.74, confidence=0.80, **args
        )
        self.assertEqual(reason, "bridged_to_minimum")
        self.assertGreater(budget, args["base_budget"])
        self.assertAlmostEqual(budget, 5 * (0.74 + 0.07 * 0.74 * 0.26))
        self.assertEqual(
            minimum_order_bridge_budget(
                limit_price=0.74, confidence=0.76, **args
            )[1],
            "insufficient_worst_cost_edge",
        )
        self.assertEqual(
            minimum_order_bridge_budget(
                limit_price=0.82, confidence=0.95, **args
            )[1],
            "bridge_fraction_cap",
        )
        self.assertEqual(
            minimum_order_bridge_budget(
                limit_price=0.74, confidence=0.90,
                **{**args, "normal_risk_tier": False},
            )[1],
            "risk_tier",
        )

    def test_live_market_buy_uses_cent_precision_and_stays_below_budget(self):
        self.assertEqual(market_buy_amount(6.39, 0.48, 3.33), 3.06)
        self.assertEqual(market_buy_amount(10, 0.48, 3.33), 3.33)

    def test_live_cost_includes_crypto_taker_fee(self):
        self.assertAlmostEqual(total_buy_cost(3.189999, 8.62162, 0.07), 3.330669, places=4)
        self.assertAlmostEqual(
            validated_cash_outflow_cost(18.492591, 15.161922, 3.189999, 3.32866638),
            3.330669,
            places=6,
        )
        self.assertIsNone(
            validated_cash_outflow_cost(18.492591, 10.0, 3.189999, 3.32866638)
        )

    def test_redeemable_position_resolves_owned_side(self):
        self.assertEqual(
            resolved_position_result("down", {"redeemable": True, "curPrice": 0}),
            "up",
        )
        self.assertEqual(
            resolved_position_result("down", {"redeemable": True, "curPrice": 1}),
            "down",
        )
        self.assertIsNone(
            resolved_position_result("down", {"redeemable": False, "curPrice": 0})
        )

    def test_redeemed_winner_requires_cash_credit_and_zero_tokens(self):
        self.assertTrue(
            redemption_credit_matches(19.750929, 15.161922, 1.818399, 6.407406, 0)
        )
        self.assertFalse(
            redemption_credit_matches(13.343523, 15.161922, 1.818399, 6.407406, 0)
        )
        self.assertFalse(
            redemption_credit_matches(19.750929, 15.161922, 1.818399, 6.407406, 6.407406)
        )

    def test_zero_disables_order_count_caps(self):
        self.assertFalse(order_cap_reached(100, 0))
        self.assertFalse(order_cap_reached(100, -1))
        self.assertTrue(order_cap_reached(2, 2))

    def test_live_drawdown_tapers_risk_before_stopping(self):
        cfg = replace(CFG)
        self.assertEqual(drawdown_risk_fraction(20, 20, cfg)[0], 0.15)
        self.assertEqual(drawdown_risk_fraction(17, 20, cfg)[0], 0.10)
        self.assertEqual(drawdown_risk_fraction(15.8, 20, cfg)[0], 0.07)
        self.assertEqual(drawdown_risk_fraction(14.8, 20, cfg)[0], 0.0)

    def test_strategy_drawdown_ignores_deposits_and_tracks_realized_peak(self):
        with tempfile.TemporaryDirectory() as td:
            db = connect(Path(td) / "lab.db")
            set_setting(db, "live_session_initial_balance", 20.0)
            for slug, updated, pnl in (("win", time.time() - 10, 5.0), ("loss", time.time(), -8.0)):
                db.execute(
                    """INSERT INTO fast_live_orders
                    (slug,condition_id,token_id,side,created_ts,updated_ts,state,
                     requested_qty,limit_price,risk_budget_usd,balance_before_usd,
                     realized_pnl,evidence_json,strategy_version_id)
                    VALUES(?,?,?,?,?,?,'settled',?,?,?,?,?,'{}','btc_5m_v8')""",
                    (slug, "c", "t", "up", updated - 1, updated, 5, .5, 2.5, 20, pnl),
                )
            db.commit()
            snapshot = strategy_equity_snapshot(db, 27.0)
            self.assertEqual(snapshot["equity_usd"], 17.0)
            self.assertEqual(snapshot["high_water_usd"], 25.0)
            self.assertEqual(snapshot["external_net_usd"], 10.0)
            self.assertEqual(daily_strategy_pnl(db), -3.0)
            self.assertEqual(drawdown_risk_fraction(17, 25, CFG)[0], 0.0)

    def test_delayed_cheaper_quote_can_become_minimum_size_affordable(self):
        base = 1.59
        blocked, reason = minimum_order_bridge_budget(
            base_budget=base, balance_usd=22.76, allowance_usd=100,
            minimum_qty=5, limit_price=.44, fee_rate=.07, confidence=.90,
            max_fraction=.18, min_edge=.06, max_order_usd=0, reserve_usd=0,
            normal_risk_tier=False,
        )
        self.assertEqual((blocked, reason), (base, "risk_tier"))
        affordable, reason = minimum_order_bridge_budget(
            base_budget=base, balance_usd=22.76, allowance_usd=100,
            minimum_qty=5, limit_price=.23, fee_rate=.07, confidence=.90,
            max_fraction=.18, min_edge=.06, max_order_usd=0, reserve_usd=0,
            normal_risk_tier=False,
        )
        qty, _ = order_quantity(affordable, .21, 100, .02, .07, 1.25)
        self.assertEqual(reason, "not_needed")
        self.assertGreaterEqual(qty, 5)

    def test_live_daily_stop_is_wider_only_at_low_balance(self):
        cfg = replace(CFG)
        self.assertEqual(daily_loss_fraction(18.49, cfg), 0.30)
        self.assertEqual(daily_loss_fraction(49.99, cfg), 0.30)
        self.assertEqual(daily_loss_fraction(50.00, cfg), 0.18)

    def test_live_allows_no_expiry_and_rejects_an_expired_positive_timestamp(self):
        base = replace(
            CFG,
            live_trading_enabled=True,
            live_confirmation=LIVE_CONFIRMATION,
            live_armed_until=time.time() + 60,
            polymarket_private_key="0x" + "1" * 64,
            polymarket_api_key="key",
            polymarket_api_secret="secret",
            polymarket_api_passphrase="passphrase",
            polymarket_signature_type=0,
        )
        self.assertIsNone(LiveBroker(base).gate_reason())
        self.assertEqual(
            LiveBroker(replace(base, live_confirmation="yes")).gate_reason(),
            "confirmation_missing",
        )
        self.assertIsNone(LiveBroker(replace(base, live_armed_until=0)).gate_reason())
        self.assertEqual(
            LiveBroker(replace(base, live_armed_until=time.time() - 1)).gate_reason(),
            "arming_expired",
        )

    def test_fok_fill_walks_depth_and_fees_each_level(self):
        levels = ((0.50, 3.0), (0.51, 4.0), (0.53, 100.0))
        self.assertEqual(book_capacity(levels, 0.52), 7.0)
        fill = simulate_fok_fill(levels, 5.0, 0.52, 0.07, 1.25)
        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill["average_price"], 0.504)
        self.assertGreater(fill["total_fee"], 0)
        self.assertIsNone(simulate_fok_fill(levels, 5.0, 0.50, 0.07, 1.0))

    def test_screen_preserves_near_miss_reason(self):
        result = screen_reason(True, True, True, True, 0.78, 0.92, 0.058, 0.06)
        self.assertEqual(result, "edge_below_threshold")

    def test_missing_ask_is_distinct_from_stale_book(self):
        self.assertEqual(
            screen_reason(True, True, True, False, None, 0.92, -1, 0.06),
            "no_ask",
        )
        self.assertEqual(
            screen_reason(True, True, True, False, 0.50, 0.92, 0.20, 0.06),
            "no_fresh_chosen_book",
        )

    def test_live_book_applies_snapshot_and_sell_delta(self):
        book = MarketBookStream()
        book.set_market("test", "up", "down")
        book.connected = True
        book._apply({
            "event_type": "book", "asset_id": "up", "timestamp": 100000,
            "asks": [{"price": "0.60", "size": "8"}, {"price": "0.61", "size": "9"}],
        })
        self.assertEqual(book.quote("up", max_silence=10)["price"], 0.60)
        book._apply({
            "event_type": "price_change", "timestamp": 100001,
            "price_changes": [{
                "asset_id": "up", "side": "SELL", "price": "0.60",
                "size": "0", "best_ask": "0.61",
            }],
        })
        self.assertEqual(book.quote("up", max_silence=10)["price"], 0.61)

    def test_mirrored_buy_price_never_becomes_other_tokens_ask(self):
        book = MarketBookStream()
        book.set_market("test", "up", "down")
        book.connected = True
        book._apply({
            "event_type": "book", "asset_id": "down", "timestamp": 100000,
            "asks": [{"price": "0.92", "size": "8"}],
        })
        book._apply({
            "event_type": "price_change", "timestamp": 100001,
            "price_changes": [{
                "asset_id": "down", "side": "BUY", "price": "0.08",
                "size": "25", "best_ask": "0.92",
            }],
        })
        self.assertEqual(book.quote("down", max_silence=10)["price"], 0.92)

    def test_explicit_best_ask_mismatch_fails_closed(self):
        book = MarketBookStream()
        book.set_market("test", "up", "down")
        book.connected = True
        book._apply({
            "event_type": "book", "asset_id": "down", "timestamp": 100000,
            "asks": [{"price": "0.92", "size": "8"}],
        })
        book._apply({
            "event_type": "price_change", "timestamp": 100001,
            "price_changes": [{
                "asset_id": "down", "side": "BUY", "price": "0.08",
                "size": "25", "best_ask": "0.08",
            }],
        })
        self.assertIsNone(book.quote("down", max_silence=10))

    def test_book_freshness_rejects_old_side_even_if_other_side_updates(self):
        book = MarketBookStream()
        book.set_market("test", "up", "down")
        book.connected = True
        book._apply({
            "event_type": "book", "asset_id": "up", "timestamp": 100000,
            "asks": [{"price": "0.60", "size": "8"}],
        })
        with book._lock:
            source_ts, _ = book._updated["up"]
            book._updated["up"] = (source_ts, time.time() - 10)
        book._apply({
            "event_type": "book", "asset_id": "down", "timestamp": 100001,
            "asks": [{"price": "0.40", "size": "8"}],
        })
        quote = book.quote("up", max_silence=5)
        self.assertIsNone(quote)
        with book._lock:
            book._last_data_received = time.time() - 6
        self.assertIsNone(book.quote("up", max_silence=5))

    def test_market_rotation_closes_book_until_final_window(self):
        class App:
            def __init__(self):
                self.messages = []
                self.closed = False

            def send(self, message):
                self.messages.append(message)

            def close(self):
                self.closed = True

        book = MarketBookStream()
        book.set_market("old", "old-up", "old-down")
        app = App()
        book._app = app
        book.connected = True
        book.set_market("new", "new-up", "new-down")
        self.assertTrue(app.closed)
        self.assertFalse(book._active)
        self.assertEqual(app.messages, [])

    def test_pre_window_refresh_closes_old_connection_only_once(self):
        class App:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        book = MarketBookStream()
        book.set_market("test", "up", "down")
        self.assertTrue(book.refresh_before_active_window("test"))
        self.assertTrue(book._active)
        self.assertFalse(book.refresh_before_active_window("test"))


if __name__ == "__main__":
    unittest.main()
