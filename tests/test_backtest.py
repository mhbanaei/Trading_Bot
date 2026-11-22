"""Phase-3 backtest harness tests.

Run:  python -m unittest tests.test_backtest -v
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.backtest import (
    ARMS, Arm, Costs, _normalise, build_evidence, exit_for, load_csv,
    load_sqlite, metrics, rank_candidates, run_arm, synthetic_candles,
)
from nobitex_bot.pipeline import DecisionPipeline, Evidence, Policy

COSTS = Costs(fee_pct=0.25, slippage_pct=0.10, spread_pct=0.0)


def ev(**over) -> Evidence:
    base = dict(symbol="AAAIRT", price=100.0, atr_pct=2.0, extension_atr=1.0,
                dist_resistance_pct=3.0, p_up=0.70, change_24h=3.0,
                spread_pct=0.0, fee_pct=0.25, slippage_pct=0.10,
                data_quality=1.0, liquidity=0.8, liquidity_status="GOOD",
                structure="UPTREND")
    base.update(over)
    return Evidence(**base)


class TestLoading(unittest.TestCase):
    def test_synthetic_is_deterministic(self):
        a = synthetic_candles("AAAIRT", bars=200, seed=3)
        b = synthetic_candles("AAAIRT", bars=200, seed=3)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 200)
        self.assertNotEqual(a, synthetic_candles("AAAIRT", bars=200, seed=4))

    def test_normalise_drops_broken_candles(self):
        good = {"symbol": "X", "timestamp": 2.0, "open": 1.0, "high": 2.0,
                "low": 0.5, "close": 1.5, "volume": 10.0}
        broken = dict(good, high=0.1)          # high below low
        dup = dict(good)
        out = _normalise([good, broken, dup])
        self.assertEqual(out, [good])

    def test_load_sqlite_empty_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            con = sqlite3.connect(str(db))
            con.execute("CREATE TABLE candles (symbol TEXT, timeframe TEXT, "
                        "timestamp TEXT, open TEXT, high TEXT, low TEXT, "
                        "close TEXT, volume TEXT)")
            con.commit()
            con.close()
            self.assertEqual(load_sqlite(db, "AAAIRT", "15m"), [])

    def test_load_sqlite_reads_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            con = sqlite3.connect(str(db))
            con.execute("CREATE TABLE candles (symbol TEXT, timeframe TEXT, "
                        "timestamp TEXT, open TEXT, high TEXT, low TEXT, "
                        "close TEXT, volume TEXT)")
            con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)", [
                ("AAAIRT", "15m", "1700000000", "1", "2", "0.5", "1.5", "9"),
                ("AAAIRT", "15m", "1700000900", "1.5", "2.5", "1", "2", "9"),
            ])
            con.commit()
            con.close()
            bars = load_sqlite(db, "AAAIRT", "15m")
            self.assertEqual(len(bars), 2)
            self.assertEqual(bars[0]["timestamp"], 1700000000.0)
            self.assertEqual(bars[1]["close"], 2.0)

    def test_load_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AAAIRT_15m.csv"
            with path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp", "open", "high", "low", "close",
                            "volume"])
                w.writerow([1700000000, 1, 2, 0.5, 1.5, 9])
                w.writerow([1700000900, 1.5, 2.5, 1, 2, 9])
            bars = load_csv(path)
            self.assertEqual([b["close"] for b in bars], [1.5, 2.0])
            self.assertEqual(bars[0]["symbol"], "AAAIRT")


class TestSelection(unittest.TestCase):
    def test_veto_removes_the_extended_leader(self):
        hot = ev(symbol="HOTIRT", change_24h=30.0, extension_atr=5.0)
        calm = ev(symbol="CALMIRT", change_24h=1.0)
        pipeline = DecisionPipeline()
        plain = rank_candidates([hot, calm], ARMS[0], pipeline)
        self.assertEqual([e.symbol for e, _k, _v in plain],
                         ["HOTIRT", "CALMIRT"])          # today's behaviour
        for arm in (ARMS[1], ARMS[3]):
            kept = rank_candidates([hot, calm], arm, pipeline)
            self.assertEqual([e.symbol for e, _k, _v in kept], ["CALMIRT"],
                             arm.name)

    def test_net_edge_arm_orders_by_edge_not_change(self):
        a = ev(symbol="AAAIRT", change_24h=20.0, dist_resistance_pct=1.0,
               atr_pct=0.5, extension_atr=2.4, p_up=0.75)
        b = ev(symbol="BBBIRT", change_24h=1.0, dist_resistance_pct=6.0,
               atr_pct=0.5, extension_atr=0.2, p_up=0.75)
        pipeline = DecisionPipeline()
        by_change = rank_candidates([a, b], ARMS[0], pipeline)
        self.assertEqual(by_change[0][0].symbol, "AAAIRT")
        by_edge = rank_candidates([a, b], ARMS[2], pipeline)
        self.assertEqual(by_edge[0][0].symbol, "BBBIRT")
        self.assertGreater(by_edge[0][1], by_edge[1][1])


class TestExecution(unittest.TestCase):
    def test_same_bar_double_touch_is_a_stop(self):
        pos = {"target": 110.0, "stop": 95.0, "bars": 1}
        bar = {"high": 111.0, "low": 94.0, "close": 105.0}
        reason, price = exit_for(pos, bar, max_hold=24, costs=COSTS)
        self.assertEqual(reason, "STOP")
        self.assertAlmostEqual(price, 95.0 * (1 - 0.001), places=9)

    def test_target_fills_at_target_price(self):
        pos = {"target": 110.0, "stop": 95.0, "bars": 1}
        bar = {"high": 110.5, "low": 99.0, "close": 110.0}
        self.assertEqual(exit_for(pos, bar, 24, COSTS), ("TARGET", 110.0))

    def test_timeout_uses_close_minus_slippage(self):
        pos = {"target": 200.0, "stop": 50.0, "bars": 24}
        bar = {"high": 120.0, "low": 99.0, "close": 105.0}
        reason, price = exit_for(pos, bar, 24, COSTS)
        self.assertEqual(reason, "TIMEOUT")
        self.assertAlmostEqual(price, 105.0 * 0.999, places=9)

    def test_entry_uses_the_next_bar_open_plus_slippage(self):
        bars = synthetic_candles("AAAIRT", bars=400, seed=11)
        series = {"AAAIRT": bars}
        pipeline = DecisionPipeline()
        trades = run_arm(Arm("change_24h", "change_24h", False), series,
                         "15m", COSTS, Policy(), slots=1, max_hold=6)
        self.assertTrue(trades, "harness produced no trade on a random walk")
        index = {b["timestamp"]: i for i, b in enumerate(bars)}
        for t in trades:
            i = index[t.entry_ts]
            self.assertAlmostEqual(
                t.entry_price,
                round(bars[i]["open"] * 1.001, 8), places=8)
            self.assertGreater(i, 0)

    def test_net_pct_charges_both_sides(self):
        pos = {"target": 100.0, "stop": 90.0, "bars": 1}
        bar = {"high": 100.0, "low": 99.0, "close": 100.0}
        _reason, exit_price = exit_for(pos, bar, 24, COSTS)
        gross = (exit_price / 99.9 - 1.0) * 100.0
        # 99.9 -> 100.0 is +0.1001% gross, minus 0.5% round-trip fee
        self.assertAlmostEqual(gross - COSTS.charged_pct(), -0.3999, places=3)
        self.assertAlmostEqual(COSTS.charged_pct(), 0.5, places=9)


class TestMetrics(unittest.TestCase):
    def test_metrics_math(self):
        def trade(net, reason="TARGET"):
            return type("T", (), {"net_pct": net, "reason": reason,
                                  "bars_held": 4})()
        m = metrics([trade(2.0), trade(-1.0), trade(3.0, "STOP")])
        self.assertEqual(m["trades"], 3)
        self.assertEqual(m["wins"], 2)
        self.assertAlmostEqual(m["win_rate"], 66.67, places=2)
        self.assertAlmostEqual(m["total_net_pct"], 4.0, places=6)
        self.assertAlmostEqual(m["avg_net_pct"], 1.3333, places=3)
        self.assertAlmostEqual(m["profit_factor"], 5.0, places=6)
        self.assertAlmostEqual(m["max_drawdown_pct"], -1.0, places=6)
        self.assertAlmostEqual(m["stop_rate"], 33.33, places=2)

    def test_empty_metrics(self):
        self.assertEqual(metrics([])["trades"], 0)

    def test_evidence_has_no_future_data(self):
        bars = synthetic_candles("AAAIRT", bars=300, seed=5)
        e = build_evidence(bars, 150, "15m", COSTS, Policy(), max_hold=12)
        self.assertIsNotNone(e)
        # identical evidence from a truncated series: nothing later leaked in
        truncated = build_evidence(bars[:151], 150, "15m", COSTS, Policy(),
                                  max_hold=12)
        self.assertEqual(e, truncated)


if __name__ == "__main__":
    unittest.main()
