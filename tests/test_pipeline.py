"""Phase-2 decision pipeline tests — net edge ranking + hard entry veto.

Run:  python -m unittest tests.test_pipeline -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.pipeline import (
    AVOID, BUY, DecisionPipeline, Evidence, Policy, entry_location_veto,
    expected_edge, round_trip_cost_pct,
)


def good(**over) -> Evidence:
    """A candidate that clears every default gate."""
    base = dict(symbol="AAAUSDT", price=100.0, atr_pct=2.0, extension_atr=1.0,
                dist_resistance_pct=3.0, p_up=0.70, change_24h=3.0,
                spread_pct=0.05, fee_pct=0.25, slippage_pct=0.10,
                data_quality=0.8, liquidity=0.8, liquidity_status="GOOD",
                structure="UPTREND")
    base.update(over)
    return Evidence(**base)


class TestEntryLocationVeto(unittest.TestCase):
    def test_top_gainer_flag_cannot_relax_the_veto(self):
        p = Policy()
        for extended in (2.4, 2.6, 3.5, 8.0):
            plain = good(extension_atr=extended, top_gainer=False)
            gainer = good(extension_atr=extended, top_gainer=True)
            self.assertEqual(entry_location_veto(plain, p),
                             entry_location_veto(gainer, p))
            self.assertEqual(DecisionPipeline().evaluate(plain).action,
                             DecisionPipeline().evaluate(gainer).action)
        # ... and the extended one is actually refused
        d = DecisionPipeline().evaluate(good(extension_atr=2.9, top_gainer=True))
        self.assertEqual(d.action, AVOID)
        self.assertIn("ENTRY_TOO_EXTENDED", d.vetoes)

    def test_unknown_entry_location_fails_closed(self):
        d = DecisionPipeline().evaluate(good(extension_atr=None,
                                             top_gainer=True))
        self.assertEqual(d.action, AVOID)
        self.assertIn("ENTRY_LOCATION_UNKNOWN", d.vetoes)

    def test_just_inside_the_limit_is_allowed(self):
        d = DecisionPipeline().evaluate(good(extension_atr=2.5))
        self.assertNotIn("ENTRY_TOO_EXTENDED", d.vetoes)
        self.assertEqual(d.action, BUY)

    def test_no_room_to_resistance(self):
        d = DecisionPipeline().evaluate(good(dist_resistance_pct=0.4))
        self.assertIn("TOO_CLOSE_TO_RESISTANCE", d.vetoes)
        self.assertEqual(d.action, AVOID)


class TestNetEdge(unittest.TestCase):
    def test_cost_is_the_full_round_trip(self):
        ev = good(fee_pct=0.25, slippage_pct=0.10, spread_pct=0.05)
        self.assertAlmostEqual(round_trip_cost_pct(ev), 0.75, places=9)

    def test_edge_formula(self):
        ev = good(atr_pct=2.0, dist_resistance_pct=3.0, p_up=0.70,
                  expected_move_pct=None)
        net, target, stop, cost = expected_edge(ev, Policy())
        self.assertAlmostEqual(target, 3.0, places=9)
        self.assertAlmostEqual(stop, 2.4, places=9)     # 1.2 ATR
        self.assertAlmostEqual(cost, 0.75, places=9)
        self.assertAlmostEqual(net, 0.70 * 3.0 - 0.30 * 2.4 - 0.75, places=9)
        self.assertGreater(net, 0)

    def test_resistance_caps_the_target(self):
        ev = good(atr_pct=2.0, dist_resistance_pct=0.9)
        _net, target, _stop, _cost = expected_edge(ev, Policy())
        self.assertAlmostEqual(target, 0.9, places=9)

    def test_projection_caps_the_target(self):
        ev = good(atr_pct=2.0, dist_resistance_pct=9.0, expected_move_pct=1.1)
        _net, target, _stop, _cost = expected_edge(ev, Policy())
        self.assertAlmostEqual(target, 1.1, places=9)

    def test_expensive_market_has_no_edge(self):
        d = DecisionPipeline().evaluate(
            good(fee_pct=1.0, slippage_pct=0.3, spread_pct=0.5))
        self.assertEqual(d.action, AVOID)
        self.assertIn("NET_EDGE_BELOW_MIN", d.vetoes)

    def test_low_probability_is_vetoed(self):
        d = DecisionPipeline().evaluate(good(p_up=0.40))
        self.assertEqual(d.action, AVOID)
        self.assertIn("P_UP_BELOW_MIN", d.vetoes)


class TestTradability(unittest.TestCase):
    def test_spread_cap_is_relative_to_atr(self):
        d = DecisionPipeline().evaluate(good(atr_pct=2.0, spread_pct=2.0))
        self.assertIn("SPREAD_TOO_WIDE", d.vetoes)

    def test_untradable_liquidity(self):
        for over in ({"liquidity_status": "CRITICAL"},
                     {"liquidity_status": "UNKNOWN"},
                     {"liquidity": 0.1},
                     {"data_quality": 0.2}):
            d = DecisionPipeline().evaluate(good(**over))
            self.assertEqual(d.action, AVOID, over)

    def test_downtrend_structure_never_buys(self):
        d = DecisionPipeline().evaluate(good(structure="DOWNTREND"))
        self.assertIn("STRUCTURE_DOWNTREND", d.vetoes)

    def test_missing_price_data(self):
        d = DecisionPipeline().evaluate(good(price=0.0))
        self.assertIn("NO_PRICE_DATA", d.vetoes)


class TestRanking(unittest.TestCase):
    def test_rank_follows_net_edge_not_24h_change(self):
        hot = good(symbol="HOTUSDT", change_24h=25.0, atr_pct=0.6,
                   extension_atr=2.4, dist_resistance_pct=1.5, p_up=0.85)
        steady = good(symbol="CALMUSDT", change_24h=1.0, atr_pct=0.6,
                      extension_atr=0.2, dist_resistance_pct=5.0, p_up=0.85)
        buyable, rejected = DecisionPipeline().rank([hot, steady])
        self.assertEqual(rejected, [])
        self.assertEqual([d.symbol for d in buyable],
                         ["CALMUSDT", "HOTUSDT"])
        self.assertGreater(buyable[0].net_edge_pct, buyable[1].net_edge_pct)
        self.assertEqual(buyable[0].rank, 1)

    def test_rejected_candidates_are_kept_with_their_reasons(self):
        buyable, rejected = DecisionPipeline().rank([
            good(symbol="OKUSDT"),
            good(symbol="TOPUSDT", extension_atr=4.0),
        ])
        self.assertEqual([d.symbol for d in buyable], ["OKUSDT"])
        self.assertEqual(rejected[0].symbol, "TOPUSDT")
        self.assertIn("ENTRY_TOO_EXTENDED", rejected[0].vetoes)

    def test_best_never_invents_a_buy(self):
        self.assertIsNone(DecisionPipeline().best([]))
        self.assertIsNone(DecisionPipeline().best([
            good(extension_atr=6.0), good(p_up=0.2)]))

    def test_decision_is_json_serialisable_and_carries_evidence(self):
        d = DecisionPipeline().evaluate(good(top_gainer=True))
        doc = json.loads(json.dumps(d.to_dict()))
        self.assertEqual(doc["action"], BUY)
        self.assertTrue(doc["buy"])
        self.assertEqual(doc["evidence"]["top_gainer"], True)
        self.assertEqual(doc["evidence"]["symbol"], "AAAUSDT")
        self.assertIn("max_extension_atr", doc["policy"])


if __name__ == "__main__":
    unittest.main()
