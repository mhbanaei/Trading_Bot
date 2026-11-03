"""Tests for forecast.py, the gainer forecast gate and fill reconciliation.

Run: python tests/test_forecast.py
"""
from __future__ import annotations

import random
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nobitex_bot import analysis, forecast  # noqa: E402
from nobitex_bot.engine import (realized_on_last_sell,  # noqa: E402
                                realized_pnl_from_fills)

STEPS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
START = 1_700_000_000


def closes(n=260, drift=0.002, noise=0.0004, seed=7):
    rnd = random.Random(seed)
    price = 100.0
    out = []
    for _ in range(n):
        price *= 1.0 + drift + rnd.uniform(-noise, noise)
        out.append(price)
    return out


def candles(series, tf):
    step = STEPS[tf]
    out = []
    prev = series[0]
    for i, close in enumerate(series):
        out.append({
            "time": START + i * step,
            "open": prev,
            "high": max(prev, close) * 1.0005,
            "low": min(prev, close) * 0.9995,
            "close": close,
            "volume": 1000.0 + i,
        })
        prev = close
    return out


def frames_for(series):
    return {tf: analysis.analyze_frame(candles(series, tf), tf)
            for tf in ("5m", "15m", "1h", "4h", "1d")}


def market(last_price, change=3.0):
    return {"symbol": "TESTUSDT", "last_price": last_price,
            "change_24h": change, "spread_percent": 0.05,
            "liquidity": {"score": 0.8, "status": "OK"},
            "data_quality": {"score": 0.9}}


CTX = {"btc_trend": "UP", "btc_change_24h": 1.0, "breadth": 0.6,
       "market_median_change_24h": 1.0}


class ForecastTests(unittest.TestCase):
    def test_uptrend_projects_up(self):
        series = closes(drift=0.002, seed=11)
        frames = frames_for(series)
        fc = forecast.project(frames, horizon_bars=200, cost_pct=0.0)
        self.assertEqual(fc["direction"], "UP")
        self.assertGreaterEqual(fc["probability_up"], 0.55)
        self.assertGreater(fc["expected_net_pct"], 0.0)
        self.assertEqual(set(fc["horizons"]), {"100", "200", "500"})
        # this synthetic series is deliberately low-volatility: its whole
        # 200-bar expected move is smaller than a 0.7% round trip, so the
        # tradeable verdict must NOT be UP once costs are charged
        paid = forecast.project(frames, horizon_bars=200, cost_pct=0.7)
        self.assertLess(paid["expected_net_pct"], 0.0)
        self.assertNotEqual(paid["direction"], "UP")

    def test_downtrend_projects_down_and_vetoes(self):
        series = closes(drift=-0.002, seed=12)
        frames = frames_for(series)
        fc = forecast.project(frames, horizon_bars=200, cost_pct=0.7)
        self.assertEqual(fc["direction"], "DOWN")
        self.assertLessEqual(fc["probability_up"], 0.45)
        verdict = analysis.decide("TESTUSDT", frames, market(series[-1], -4.0),
                                  CTX, mode=analysis.RISK, cost_pct=0.7)
        self.assertIn("FORECAST_DOWNTREND", verdict["vetoes"])
        self.assertFalse(verdict["allow_buy"])

    def test_band_widens_with_the_horizon(self):
        fc = forecast.project(frames_for(closes(seed=13)))
        h = fc["horizons"]
        self.assertGreater(h["500"]["band_pct"], h["200"]["band_pct"])
        self.assertGreater(h["200"]["band_pct"], h["100"]["band_pct"])
        ratio = h["500"]["band_pct"] / h["100"]["band_pct"]
        self.assertAlmostEqual(ratio, 5 ** 0.5, delta=0.35)

    def test_cost_is_subtracted_from_the_net_path(self):
        fc = forecast.project(frames_for(closes(seed=14)), horizon_bars=200,
                              cost_pct=0.7)
        main = fc["horizons"]["200"]
        self.assertAlmostEqual(main["expected_net_pct"],
                               main["expected_return_pct"] - 0.7,
                               delta=0.01)

    def test_empty_frames_are_neutral(self):
        fc = forecast.project({})
        self.assertEqual(fc["direction"], "SIDEWAYS")
        self.assertAlmostEqual(fc["probability_up"], 0.5, places=4)
        self.assertEqual(fc["drivers"], ["NO_CLEAR_EDGE"])
        self.assertEqual(fc["expected_net_pct"], 0.0)

    def test_gainer_without_forecast_edge_is_blocked(self):
        flat = closes(drift=0.0, noise=0.00005, seed=15)
        frames = frames_for(flat)
        verdict = analysis.decide("TESTUSDT", frames, market(flat[-1], 2.5),
                                  CTX, mode=analysis.RISK, cost_pct=0.7,
                                  top_gainer=True)
        self.assertIn("FORECAST_NO_EDGE", verdict["vetoes"])
        self.assertFalse(verdict["allow_buy"])

    def test_uptrend_gainer_passes_the_forecast_gate(self):
        series = closes(drift=0.002, seed=16)
        frames = frames_for(series)
        verdict = analysis.decide("TESTUSDT", frames, market(series[-1], 4.0),
                                  CTX, mode=analysis.RISK, cost_pct=0.7,
                                  top_gainer=True)
        self.assertNotIn("FORECAST_NO_EDGE", verdict["vetoes"])
        self.assertNotIn("FORECAST_DOWNTREND", verdict["vetoes"])

    def test_decide_always_carries_the_projection(self):
        series = closes(seed=17)
        verdict = analysis.decide("TESTUSDT", frames_for(series),
                                  market(series[-1]), CTX,
                                  mode=analysis.SAFE, cost_pct=0.7)
        self.assertIn("forecast", verdict)
        self.assertIn("horizons", verdict["forecast"])


class ReconcileTests(unittest.TestCase):
    RATE = Decimal("0.0025")

    def fills(self):
        return [
            {"trade_id": "live_a", "market": "TESTUSDT", "side": "BUY",
             "price": "100", "amount": "1", "fee_amount": "0",
             "timestamp": "2026-01-01T00:00:00Z"},
            {"trade_id": "live_b", "market": "TESTUSDT", "side": "BUY",
             "price": "110", "amount": "1", "fee_amount": "0",
             "timestamp": "2026-01-01T01:00:00Z"},
            {"trade_id": "live_c", "market": "TESTUSDT", "side": "SELL",
             "price": "200", "amount": "2", "fee_amount": "0",
             "timestamp": "2026-01-01T02:00:00Z"},
        ]

    def test_average_cost_realized_pnl(self):
        realized, balanced, qty = realized_pnl_from_fills(
            self.fills(), self.RATE)
        self.assertTrue(balanced)
        self.assertEqual(qty, Decimal("0"))
        # buy cost includes the estimated taker fee on both buys, proceeds
        # include the estimated fee on the sell
        expected = ((Decimal("400") - Decimal("400") * self.RATE)
                    - (Decimal("100") + Decimal("100") * self.RATE
                       + Decimal("110") + Decimal("110") * self.RATE))
        self.assertAlmostEqual(float(realized), float(expected), places=6)

    def test_missing_fee_is_charged(self):
        no_fee, _, _ = realized_pnl_from_fills(self.fills(), 0)
        with_fee, _, _ = realized_pnl_from_fills(self.fills(), self.RATE)
        self.assertGreater(no_fee, with_fee)

    def test_uncovered_sell_is_skipped(self):
        rows = [{"trade_id": "live_x", "market": "TESTUSDT",
                 "side": "SELL", "price": "100", "amount": "5",
                 "fee_amount": "0", "timestamp": "2026-01-01T00:00:00Z"}]
        realized, balanced, qty = realized_pnl_from_fills(rows, self.RATE)
        self.assertIsNone(realized)
        self.assertFalse(balanced)
        self.assertEqual(qty, Decimal("0"))

    def test_replay_is_idempotent(self):
        first, _, _ = realized_pnl_from_fills(self.fills(), self.RATE)
        second, _, _ = realized_pnl_from_fills(self.fills(), self.RATE)
        self.assertEqual(first, second)

    def test_last_sell_pnl_is_the_closing_event(self):
        full, _, _ = realized_pnl_from_fills(self.fills(), self.RATE)
        pnl, basis = realized_on_last_sell(self.fills(), self.RATE)
        self.assertEqual(pnl, full)
        self.assertGreater(basis, Decimal("0"))

    def test_no_sell_has_no_closing_event(self):
        pnl, basis = realized_on_last_sell(self.fills()[:2], self.RATE)
        self.assertIsNone(pnl)
        self.assertIsNone(basis)

    def test_replay_reports_remaining_quantity(self):
        realized, balanced, qty = realized_pnl_from_fills(
            self.fills()[:2], self.RATE)
        self.assertTrue(balanced)
        self.assertEqual(qty, Decimal("2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
