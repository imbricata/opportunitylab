import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from opportunity_lab.config import CFG
from opportunity_lab.db import balance, connect, ensure_initial_balance
from opportunity_lab.fastlane import boundary_price, up_probability, volatility_per_s
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
        self.assertGreater(up_probability(101, 100, 5, 0.001, 0.7), 0.5)
        self.assertLess(up_probability(99, 100, 5, 0.001, 0.7), 0.5)

    def test_irregular_tick_volatility_is_finite(self):
        ticks = [
            {"source": "chainlink", "source_ts": 90 + i, "price": 100 + (i % 2) * 0.1}
            for i in range(10)
        ]
        sigma = volatility_per_s(ticks, 100, 20)
        self.assertIsNotNone(sigma)
        self.assertGreater(sigma, 0)


if __name__ == "__main__":
    unittest.main()
