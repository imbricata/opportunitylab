import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from opportunity_lab.ai import estimated_cost
from opportunity_lab.db import balance, connect, ensure_initial_balance, ledger_entry
from opportunity_lab.engine import _materially_improved
from opportunity_lab.hyperliquid import average_price, executable_open_prices


class BookTests(unittest.TestCase):
    def test_average_price_walks_depth(self):
        levels = [
            {"px": "10", "sz": "1"},
            {"px": "11", "sz": "2"},
        ]
        self.assertAlmostEqual(average_price(levels, 2), 10.5)
        self.assertIsNone(average_price(levels, 4))

    def test_open_prices_require_both_legs(self):
        spot = {"levels": [[{"px": "9.9", "sz": "10"}], [{"px": "10", "sz": "10"}]]}
        perp = {"levels": [[{"px": "10.1", "sz": "10"}], [{"px": "10.2", "sz": "10"}]]}
        qty, spot_px, perp_px = executable_open_prices(spot, perp, 20)
        self.assertAlmostEqual(qty, 20 / 10.1)
        self.assertEqual(spot_px, 10)
        self.assertEqual(perp_px, 10.1)


class LedgerTests(unittest.TestCase):
    def test_virtual_balance_is_reconciled(self):
        with tempfile.TemporaryDirectory() as td:
            db = connect(Path(td) / "lab.db")
            ensure_initial_balance(db, 100)
            self.assertEqual(balance(db), 100)
            ledger_entry(db, "shadow_open", -20)
            ledger_entry(db, "shadow_close", 21)
            db.commit()
            self.assertEqual(balance(db), 101)


class CostTests(unittest.TestCase):
    def test_nano_cost_estimate(self):
        self.assertAlmostEqual(estimated_cost("gpt-5.4-nano", 2500, 650), 0.0013125)


class ReviewCooldownTests(unittest.TestCase):
    def test_material_change_bypasses_ai_cooldown(self):
        previous = {"score": 75.0, "projected_net_usd": 0.10}
        self.assertFalse(_materially_improved(previous, 80.0, 0.12))
        self.assertTrue(_materially_improved(previous, 85.0, 0.12))
        self.assertTrue(_materially_improved(previous, 80.0, 0.16))


if __name__ == "__main__":
    unittest.main()
