"""«فروش» must sell the amount the owner typed — and only real coins.

The dashboard used to sell the whole ledger position with no amount input,
so a partial sale was impossible; worse, a ledger amount larger than the
real wallet produced an order the exchange accepted and then cancelled
(matched 0, «در لیست فروش‌ها است ولی نمی‌فروشد»). These tests pin the sell
preview and the amount path.

Run:  python -m unittest tests.test_manual_sell_amount -v
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


class SellAmountCase(TempCase):
    LAST = Decimal("1.76")
    BID = LAST * Decimal("0.999")

    def setUp(self):
        super().setUp()
        self.cfg.exchange.live_trading = True
        self.engine = TradingEngine(self.cfg)
        self.fx = FakeExchange(
            {"ATOMUSDT": _row("ATOMUSDT", float(self.LAST))},
            {"ATOM": Decimal("1.0")})
        self.engine.exchange = self.fx
        self.engine.executor.exchange = self.fx
        # `_live()` checks isinstance(NobitexAPI); the fake adapter stands in
        # for the real one so the live branch is what gets exercised.
        self.engine.executor._live = lambda: True
        self.engine._real_attached = True
        # the real wallet pull talks to the exchange session, not to the fake
        self.engine.sync_real_wallet = lambda *a, **k: {"ok": True}
        self.engine._sync_after_cancel = lambda *a, **k: None
        self.engine.portfolio.overwrite_balances({
            "ATOM": {"available": Decimal("1.0"), "locked": Decimal("0")},
            "USDT": {"available": Decimal("50.0"),
                     "locked": Decimal("0")}})
        self.engine.storage.upsert_position(
            "ATOMUSDT", Decimal("1.0"), self.LAST, Decimal("0"),
            last_buy_price=self.LAST)

    def _orders(self):
        return self.fx.orders


class TestSellPreview(SellAmountCase):
    def test_preview_reports_the_bid_and_the_asset(self):
        p = self.engine.sell_preview("ATOMUSDT")
        self.assertTrue(p.get("ok"), p)
        self.assertEqual(p["market"], "ATOMUSDT")
        self.assertEqual(p["asset"], "ATOM")
        self.assertEqual(p["quote"], "USDT")
        self.assertAlmostEqual(p["available"], 1.0, places=8)
        self.assertAlmostEqual(p["price"], float(self.BID), places=8)

    def test_preview_caps_the_amount_by_the_real_wallet(self):
        self.engine.portfolio.overwrite_balances({
            "ATOM": {"available": Decimal("0.25"), "locked": Decimal("0")},
            "USDT": {"available": Decimal("50.0"),
                     "locked": Decimal("0")}})
        p = self.engine.sell_preview("ATOMUSDT")
        self.assertAlmostEqual(p["available"], 0.25, places=8)
        self.assertAlmostEqual(p["ledger_amount"], 1.0, places=8)

    def test_preview_has_no_side_effects(self):
        self.engine.sell_preview("ATOMUSDT")
        self.assertEqual(self._orders(), [])


class TestSellAmount(SellAmountCase):
    def test_partial_amount_sells_exactly_that_amount(self):
        r = self.engine.sell_position_now("ATOMUSDT", 0.4)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(self._orders()), 1)
        self.assertEqual(Decimal(str(self._orders()[0]["amount"])),
                         Decimal("0.4"))
        self.assertAlmostEqual(r["sold_amount"], 0.4, places=8)
        self.assertAlmostEqual(r["remaining_amount"], 0.0, places=8)

    def test_missing_amount_sells_the_whole_holding(self):
        r = self.engine.sell_position_now("ATOMUSDT")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(Decimal(str(self._orders()[0]["amount"])),
                         Decimal("1.0"))

    def test_amount_above_the_holding_is_clamped_and_said(self):
        r = self.engine.sell_position_now("ATOMUSDT", 5)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(Decimal(str(self._orders()[0]["amount"])),
                         Decimal("1.0"))
        self.assertAlmostEqual(r["requested_amount"], 1.0, places=8)
        self.assertIn("موجود", str(r.get("note", "")))

    def test_ledger_above_the_wallet_never_oversells(self):
        self.engine.portfolio.overwrite_balances({
            "ATOM": {"available": Decimal("0.3"), "locked": Decimal("0")},
            "USDT": {"available": Decimal("50.0"),
                     "locked": Decimal("0")}})
        r = self.engine.sell_position_now("ATOMUSDT")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(Decimal(str(self._orders()[0]["amount"])),
                         Decimal("0.3"))

    def test_empty_free_wallet_after_a_good_sync_refuses(self):
        # all coins are locked in another order: a fresh wallet pull SAYS so,
        # and selling nothing beats sending an order the exchange cancels
        self.engine.portfolio.overwrite_balances({
            "ATOM": {"available": Decimal("0"),
                     "locked": Decimal("1.0")},
            "USDT": {"available": Decimal("50.0"),
                     "locked": Decimal("0")}})
        r = self.engine.sell_position_now("ATOMUSDT")
        self.assertFalse(r.get("ok"), r)
        self.assertIn("موجودی", str(r.get("error", "")))
        self.assertEqual(self._orders(), [])

    def test_failed_wallet_pull_keeps_the_ledger_amount(self):
        # a 403 / expired token must not look like «no coins»
        self.engine.sync_real_wallet = lambda *a, **k: {"ok": False,
                                                       "error": "403"}
        self.engine.portfolio.overwrite_balances({
            "ATOM": {"available": Decimal("0"),
                     "locked": Decimal("0")}})
        r = self.engine.sell_position_now("ATOMUSDT")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(Decimal(str(self._orders()[0]["amount"])),
                         Decimal("1.0"))

    def test_zero_and_negative_amounts_are_refused(self):
        for bad in (0, -1):
            r = self.engine.sell_position_now("ATOMUSDT", bad)
            self.assertFalse(r.get("ok"), r)
            self.assertIn("مقدار", str(r.get("error", "")))
        self.assertEqual(self._orders(), [])

    def test_below_the_exchange_minimum_is_explained(self):
        # 0.2 ATOM * 1.76 = 0.352 USDT < 0.44 minimum order
        r = self.engine.sell_position_now("ATOMUSDT", 0.2)
        self.assertFalse(r.get("ok"), r)
        self.assertIn("حداقل", str(r.get("error", "")))
        self.assertEqual(self._orders(), [])


class _SellRoutesEngine:
    """Stub engine for the HTTP routes (no trading, just the call shape)."""

    calls: list = []

    @classmethod
    def reset(cls):
        cls.calls = []

    def sell_preview(self, symbol):
        _SellRoutesEngine.calls.append(("preview", symbol))
        return {"ok": True, "market": symbol, "asset": "BTC",
                "available": 2.0, "price": 100.0, "quote": "IRT"}

    def sell_position_now(self, symbol, amount=None):
        _SellRoutesEngine.calls.append(("sell", symbol, amount))
        return {"ok": True, "sold_amount": amount or 0.0,
                "remaining_amount": 0.0}

    def status(self):
        return {"running": False}


class TestSellRoutes(unittest.TestCase):
    """The dashboard dialog talks to these two routes — pin their shape."""

    def setUp(self):
        import http.client  # noqa: F401  (used by _req)
        import json
        import shutil
        import tempfile
        import threading

        from nobitex_bot.config import Config
        from nobitex_bot.web import BotHTTPServer
        from nobitex_bot.webauth import DashboardAuth

        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_sellroute_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cfg = Config()
        cfg.engine.data_dir = str(self.tmp / "data")
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"web": {"password": ""}}),
                            encoding="utf-8")
        self.server = BotHTTPServer(
            ("127.0.0.1", 0), _SellRoutesEngine(),
            DashboardAuth(cfg, config_path=cfg_path))
        self.addCleanup(self.server.server_close)
        self.addCleanup(_SellRoutesEngine.reset)
        _SellRoutesEngine.reset()
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def _req(self, method, path, body=None):
        import http.client
        import json
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, json.dumps(body or {}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_preview_route_is_read_only(self):
        st, body = self._req("GET", "/api/sell_preview?symbol=BTCIRT")
        self.assertEqual(st, 200)
        self.assertEqual(body["available"], 2.0)
        self.assertEqual(_SellRoutesEngine.calls,
                         [("preview", "BTCIRT")])

    def test_amount_reaches_the_engine(self):
        st, body = self._req("POST", "/api/control/sell_now",
                             {"symbol": "BTCIRT", "amount": 0.25})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(_SellRoutesEngine.calls,
                         [("sell", "BTCIRT", 0.25)])

    def test_persian_decimal_separator_is_accepted(self):
        self._req("POST", "/api/control/sell_now",
                  {"symbol": "BTCIRT", "amount": "0٫5"})
        self.assertEqual(_SellRoutesEngine.calls[-1],
                         ("sell", "BTCIRT", 0.5))

    def test_missing_amount_means_the_whole_holding(self):
        self._req("POST", "/api/control/sell_now", {"symbol": "BTCIRT"})
        self.assertEqual(_SellRoutesEngine.calls[-1],
                         ("sell", "BTCIRT", None))

    def test_bad_amount_is_a_persian_error_and_does_not_trade(self):
        st, body = self._req("POST", "/api/control/sell_now",
                             {"symbol": "BTCIRT", "amount": "abc"})
        self.assertEqual(st, 200)
        self.assertFalse(body["ok"])
        self.assertIn("نامعتبر", body["error"])
        self.assertEqual(_SellRoutesEngine.calls, [])


if __name__ == "__main__":
    unittest.main()
