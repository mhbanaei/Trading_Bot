"""Manual dashboard clicks must win — and must actually trade.

Pinned bugs (user reports):

* «وقتی گزینه خرید رو می‌زنم اررور موتور ریسک می‌ده» — a manual BUY was
  judged by the bot's own soft gates (data quality, per-market cooldown,
  open-position/exposure caps), so a human click died on rules that only
  exist to stop the bot from trading with itself.
* «فروش می‌ره تو لیست فروش‌ها اما فروش نمی‌ره» — a live SELL could be
  ACCEPTED and match nothing, and `_sell_capital_to_irt` cancelled that
  very order milliseconds later, so the coins never left the wallet.

Run:  python -m unittest tests.test_manual_priority -v
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402


def _buy_decision(symbol="ATOMUSDT", price=1.0, stop=0.95):
    return {
        "decision_id": "dec_manual_test",
        "snapshot_id": "snap",
        "cycle_id": "manual_buy",
        "symbol": symbol,
        "action": "BUY",
        "confidence": 0.9,
        "time_horizon": "INTRADAY",
        "reason_codes": ["MANUAL_BUY"],
        "created_at": "2026-10-29T10:00:00.000Z",
        "expires_at": "2099-01-01T00:00:00.000Z",
        "entry": {"price": price, "min": price, "max": price},
        "stop_loss": {"price": stop, "method": "PERCENT_STOP"},
        "take_profit": [{"price": price * 1.05, "close_percent": 100}],
        "position_size": {"amount": 10.0, "quote_value": 10.0},
        "risk": {"max_loss": 1.0},
        "cost": {"expected_net_return": 0.01},
    }


class TestManualOverridesRiskGates(TempCase):
    def _engine(self):
        return TradingEngine(self.cfg)

    def test_manual_buy_overrides_quality_and_liquidity_gates(self):
        e = self._engine()
        snap = {"symbol": "ATOMUSDT", "last_price": 1.0,
                "data_quality": {"score": 0.0},
                "liquidity": {"status": "CRITICAL"}}
        pf = {"risk": {"total_equity": 1000.0, "exposure_ratio": 0.0,
                       "cash_ratio": 1.0, "drawdown": {"current": 0.0}},
              "assets": [{"asset": "USDT", "available": "1000"}],
              "account": {}}
        prices = {"ATOMUSDT": 1.0}
        auto = e.risk.evaluate(_buy_decision(), snap, pf, prices)
        self.assertEqual(auto["status"], "REJECTED")
        self.assertIn("DATA_QUALITY_BELOW_TRADE_THRESHOLD",
                      auto["rejection_reasons"])
        manual = e.risk.evaluate(_buy_decision(), snap, pf, prices,
                                 manual=True)
        self.assertEqual(manual["status"], "APPROVED",
                         manual["rejection_reasons"])
        self.assertTrue(manual["manual_override"])
        self.assertIn("DATA_QUALITY_BELOW_TRADE_THRESHOLD",
                      manual["overridden"])
        self.assertIn("LIQUIDITY_NOT_TRADABLE", manual["overridden"])
        self.assertGreater(manual["approved_position_size"], 0)

    def test_second_manual_buy_click_is_not_blocked_by_cooldown(self):
        e = self._engine()
        e.auto_trading = False
        e.cfg.exchange.trade_quote_markets = "both"
        e.run_cycle()
        rows = (e.latest_snapshot or {}).get("markets") or []
        m = next((r for r in rows if r.get("symbol", "").endswith("USDT")
                  and r.get("last_price")), None)
        if not m:
            self.skipTest("no USDT market in the demo snapshot")
        first = e.manual_buy(m["symbol"], None, 5.0)
        self.assertTrue(first.get("ok"), first.get("error") or first)
        # second click on the SAME market: the automatic path would answer
        # BUY_COOLDOWN_ACTIVE / BUY_ORDER_ALREADY_ACTIVE
        second = e.manual_buy(m["symbol"], None, 5.0)
        self.assertTrue(second.get("ok"),
                        second.get("error") or second)
        self.assertEqual(e.storage.risk_recent(1)[0]["status"],
                         "APPROVED")


class _Venue:
    """Minimal live venue: bids thin at the top, plenty one level down."""

    def __init__(self, fill_on=1):
        self.price = Decimal("1.76")
        self.fill_on = fill_on          # 0-based index of the filling order
        self.placed: list = []
        self.cancelled: list = []
        self.open_sell: list = []

    def orderbook(self, symbol, depth=20):
        return {"symbol": symbol, "timestamp": "t", "sequence": 1,
                "bids": [{"price": float(self.price * Decimal("0.999")),
                          "amount": 0.2},
                         {"price": float(self.price * Decimal("0.990")),
                          "amount": 50.0}],
                "asks": [{"price": float(self.price * Decimal("1.001")),
                          "amount": 0.2},
                         {"price": float(self.price * Decimal("1.010")),
                          "amount": 50.0}],
                "depth_levels": 2, "bid_volume": 50.2,
                "ask_volume": 50.2, "imbalance": 0.0}

    def ticker(self, symbol):
        return {"last_price": float(self.price),
                "best_bid": float(self.price * Decimal("0.999"))}

    def open_orders(self, market=None):
        return list(self.open_sell)

    def place_limit_order(self, market, side, price, amount, cid):
        idx = len(self.placed)
        oid = str(500 + idx)
        filled = idx >= self.fill_on
        amount_d = Decimal(str(amount))
        self.placed.append({"order_id": oid, "market": market, "side": side,
                            "price": Decimal(str(price)),
                            "amount": amount_d})
        return {"order_id": oid,
                "status": "FILLED" if filled else "OPEN",
                "matched_amount": str(amount_d if filled else Decimal("0")),
                "unmatched_amount": str(Decimal("0") if filled
                                        else amount_d)}

    def cancel_order(self, oid):
        self.cancelled.append(str(oid))
        return {"order_id": str(oid), "status": "CANCELLED"}

    def order_status(self, oid, market=""):
        return {"order_id": str(oid), "status": "OPEN",
                "matched_amount": "0", "average_price": 0}


class TestSellNowActuallySells(TempCase):
    def _engine(self, venue):
        self.cfg.exchange.live_trading = True
        e = TradingEngine(self.cfg)
        e.exchange = venue
        e._real_attached = True
        e.executor.exchange = venue
        e.executor._live = lambda: True
        e.sync_real_wallet = lambda: {"ok": True}
        e._sync_after_cancel = lambda: None
        e.backfill_real_fills = lambda: 0
        return e

    def test_book_walk_prices_below_the_level_that_covers_the_size(self):
        venue = _Venue()
        e = self._engine(venue)
        px = e.executor._crossing_sell_price(
            "ATOMUSDT", Decimal("1.76"), Decimal("50"))
        self.assertLess(px, venue.price * Decimal("0.990"),
                        "a 50-coin sell must reach the deep bid, not the "
                        "thin top level")
        px_small = e.executor._crossing_sell_price(
            "ATOMUSDT", Decimal("1.76"), Decimal("0.1"))
        self.assertGreater(px_small, px)

    def test_manual_buy_crosses_the_ask(self):
        venue = _Venue()
        e = self._engine(venue)
        px = e.executor._crossing_buy_price(
            "ATOMUSDT", Decimal("1.76"), Decimal("2"))
        self.assertGreater(px, venue.price * Decimal("1.010"),
                           "a manual BUY must reach the level that covers "
                           "its size, not wait at a pullback price")

    def test_chase_reposts_an_unfilled_sell(self):
        venue = _Venue(fill_on=0)   # the FIRST retry matches
        e = self._engine(venue)
        with mock.patch("time.sleep", lambda *a, **k: None):
            res = e._chase_sell(
                "ATOMUSDT",
                {"exchange_order_id": "1", "matched_amount": "0",
                 "price": 1.76, "decision_id": "d1"},
                2.0, tries=2)
        self.assertEqual(res["attempts"], 1)
        self.assertEqual(venue.cancelled, ["1"])
        self.assertEqual(len(venue.placed), 1)
        self.assertLess(venue.placed[0]["price"], venue.price)
        self.assertAlmostEqual(res["matched"], 2.0)
        self.assertAlmostEqual(res["remaining"], 0.0)

    def test_chase_reports_the_honest_remainder_when_nothing_fills(self):
        venue = _Venue(fill_on=99)
        e = self._engine(venue)
        with mock.patch("time.sleep", lambda *a, **k: None):
            res = e._chase_sell(
                "ATOMUSDT",
                {"exchange_order_id": "1", "matched_amount": "0",
                 "price": 1.76, "decision_id": "d1"},
                2.0, tries=1)
        self.assertAlmostEqual(res["matched"], 0.0)
        self.assertAlmostEqual(res["remaining"], 2.0)

    def test_refreshing_capital_never_cancels_the_working_sell(self):
        venue = _Venue()
        venue.open_sell = [{"order_id": "9", "side": "SELL"}]
        e = self._engine(venue)
        e.portfolio.overwrite_balances({
            "IRT": {"available": Decimal("123456"), "locked": Decimal("0")}})
        out = e._sell_capital_to_irt({"market": "ATOMUSDT"})
        self.assertEqual(venue.cancelled, [],
                         "cancelling the fresh SELL un-sells the coins")
        self.assertAlmostEqual(out["irt_credited"], 123456.0)


if __name__ == "__main__":
    unittest.main()
