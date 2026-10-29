"""Live SELL must actually fill — not rest above the bid and get cancelled.

Two production bugs are pinned here:

* a live SELL was sent at the last traded price, so it rested above the
  best bid whenever the market ticked down (the stale-order reaper later
  cancelled it; the user's «دکمهٔ فروش را می‌زنم، نمی‌فروشد»);
* when a live order matched immediately, `_submit_real` stored the whole
  fill dict in `orders.status`, sqlite raised «type 'dict' is not
  supported» and the request failed AFTER the exchange accepted the order.

Run:  python -m unittest tests.test_live_sell_fill -v
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from tests.test_live_universe import FakeExchange, _row  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.execution import ExecutionEngine  # noqa: E402


class LiveSellCase(TempCase):
    LAST = Decimal("1.76")
    BID = LAST * Decimal("0.999")

    def setUp(self):
        super().setUp()
        self.cfg.exchange.live_trading = True
        self.engine = TradingEngine(self.cfg)
        self.fx = FakeExchange(
            {"ATOMUSDT": _row("ATOMUSDT", float(self.LAST))},
            {"ATOM": Decimal("1.0")})
        self.executor = ExecutionEngine(
            self.cfg, self.engine.storage, self.engine.events,
            self.engine.portfolio, exchange=self.fx)
        # `_live()` checks isinstance(NobitexAPI); the fake adapter stands in
        # for the real one so the live branch is what gets exercised.
        self.executor._live = lambda: True
        self.engine.portfolio.overwrite_balances({
            "ATOM": {"available": Decimal("1.0"), "locked": Decimal("0")},
            "USDT": {"available": Decimal("50.0"), "locked": Decimal("0")}})

    def _sell(self):
        return self.executor.execute(
            {"decision_id": "d1", "symbol": "ATOMUSDT", "action": "SELL"},
            {"status": "APPROVED", "approved_position_size": 1.0,
             "risk_decision_id": "r1"},
            {"ATOMUSDT": self.LAST}, "cyc_live_sell")


class TestLiveSellPrice(LiveSellCase):
    def test_sell_crosses_the_best_bid(self):
        price = self.executor._crossing_sell_price("ATOMUSDT", self.LAST)
        self.assertLess(price, self.BID,
                        "a SELL at/above the bid can rest and never fill")
        self.assertGreater(price, self.BID * Decimal("0.99"))

    def test_sell_order_is_priced_below_the_bid_and_fills(self):
        ex = self._sell()
        self.assertEqual(ex["status"], "FILLED")
        self.assertEqual(len(self.fx.orders), 1)
        self.assertLess(self.fx.orders[0]["price"], self.BID)
        self.assertEqual(self.fx.orders[0]["side"], "SELL")

    def test_immediate_fill_does_not_crash_on_a_dict_status(self):
        self._sell()
        row = self.engine.storage.orders(limit=1)[0]
        self.assertEqual(row["status"], "FILLED")
        self.assertIsInstance(row["status"], str)
        self.assertEqual(Decimal(row["matched_amount"]), Decimal("1"))

    def test_sell_without_an_orderbook_falls_back_to_the_ticker(self):
        class NoBook(FakeExchange):
            def orderbook(self, symbol, depth=20):
                raise RuntimeError("orderbook unavailable")

        fx = NoBook({"ATOMUSDT": _row("ATOMUSDT", float(self.LAST))},
                    {"ATOM": Decimal("1.0")})
        self.fx = fx
        self.executor.exchange = fx
        price = self.executor._crossing_sell_price("ATOMUSDT", self.LAST)
        self.assertLess(price, self.BID)

    def test_sell_without_any_quote_uses_last_minus_buffer(self):
        class NoData(FakeExchange):
            def orderbook(self, symbol, depth=20):
                raise RuntimeError("no book")

            def ticker(self, symbol):
                raise RuntimeError("no ticker")

        fx = NoData({}, {})
        self.executor.exchange = fx
        price = self.executor._crossing_sell_price("ATOMUSDT", self.LAST)
        self.assertLess(price, self.LAST)
        self.assertGreater(price, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
