"""Unit tests for exchange price-unit conversion (Rials vs Toman vs USDT).

Regression for the live incident where every tether-market (USDT-quoted)
price was divided by the Rial->Toman divisor (10): the bot then placed
limit BUYs 10x BELOW the market (DAIUSDT 0.998 -> 0.0998, BTCUSDT
83550 -> 8355) so orders rested forever, never filled, and were
cancel/chase-reposted in a loop — «سفارش می‌زند ولی خرید نمی‌شود».

Second regression (open_orders): the exchange echoes the Tether quote as
"Tether", but the code compared it against the UPPERCASED string, so every
tether-quoted order came back as an IRT market (PAXGOLDIRT / DAIIRT) with
the USDT price kept as-if-Toman.

Run:  python -m unittest tests.test_price_units -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.nobitex_api import NobitexAPI
from nobitex_bot.utils import d


# Raw payloads exactly as Nobitex's public endpoints return them
# (USDT markets quoted in USDT, RLS markets in Rials).
STATS = {
    "dai-usdt": {"latest": "0.998", "bestSell": "0.9996", "bestBuy": "0.998"},
    "btc-usdt": {"latest": "83550", "bestSell": "83595", "bestBuy": "83560.02"},
    "btc-rls": {"latest": "213710000000", "bestSell": "213709999970",
                "bestBuy": "213700000050"},
    "usdt-rls": {"latest": "2653110", "bestSell": "2653990",
                 "bestBuy": "2653800"},
    # a coin with NO usdt market — the old rls-fallback poisoned its ticker
    "xyz-rls": {"latest": "500000", "bestSell": "501000", "bestBuy": "499000"},
}


def make_api(tmp: Path) -> NobitexAPI:
    cfg = SimpleNamespace(
        data_dir=str(tmp), nobitex_base_url="", nobitex_username="",
        nobitex_password="", nobitex_totp_secret="",
        rls_to_irt_divisor=10, rest_timeout=12)
    api = NobitexAPI(cfg)
    api._stats_cache = dict(STATS)
    api._stats_ts = __import__("time").time() + 9999  # cache never expires
    # no network in unit tests: a stats cache miss yields empty stats
    api._public_get = lambda path, params=None: {"stats": {}}
    return api


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_px_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        self.api = make_api(self.tmp)


class TestTickerUnits(TempCase):
    def test_usdt_market_price_not_divided(self):
        t = self.api.ticker("DAIUSDT")
        self.assertEqual(t["last_price"], 0.998)
        self.assertEqual(t["best_ask"], 0.9996)
        self.assertEqual(t["best_bid"], 0.998)

    def test_btcusdt_price_not_divided(self):
        t = self.api.ticker("BTCUSDT")
        self.assertEqual(t["last_price"], 83550.0)

    def test_irt_market_price_divided_by_10(self):
        t = self.api.ticker("BTCIRT")
        self.assertEqual(t["last_price"], 21371000000.0)
        self.assertEqual(t["best_ask"], 21370999997.0)

    def test_usdtirt_uses_rls_bridge(self):
        t = self.api.ticker("USDTIRT")
        self.assertEqual(t["last_price"], 265311.0)

    def test_missing_usdt_market_does_not_fall_back_to_rls(self):
        # the old code substituted the RLS price (500000 Rls) for a
        # nonexistent USDT market — a poisoned 50,000x price
        t = self.api.ticker("XYZUSDT")
        self.assertEqual(t["last_price"], 0.0)


class TestOrderbookTradesUnits(TempCase):
    def test_usdt_orderbook_untouched(self):
        self.api._public_get = lambda path, params=None: {
            "bids": [["0.998", "10"]], "asks": [["0.9996", "5"]]}
        book = self.api.orderbook("DAIUSDT")
        self.assertEqual(book["bids"][0]["price"], 0.998)
        self.assertEqual(book["asks"][0]["price"], 0.9996)

    def test_irt_orderbook_divided(self):
        self.api._public_get = lambda path, params=None: {
            "bids": [["213710000000", "1"]], "asks": [["213720000000", "1"]]}
        book = self.api.orderbook("BTCIRT")
        self.assertEqual(book["bids"][0]["price"], 21371000000.0)

    def test_usdt_trades_untouched(self):
        self.api._public_get = lambda path, params=None: {
            "trades": [{"price": "0.998", "amount": "2", "time": 1}]}
        out = self.api.trades("DAIUSDT")
        self.assertEqual(out[0]["price"], 0.998)


class TestOrderStatusUnits(TempCase):
    def _status(self, order_payload, market=""):
        self.api._post = lambda path, body: {"order": order_payload}
        return self.api.order_status("123", market)

    def test_usdt_order_average_price_not_divided(self):
        doc = self._status({"dstCurrency": "usdt", "averagePrice": "0.998",
                            "status": "done", "matchedAmount": "1"})
        self.assertEqual(doc["average_price"], "0.998")

    def test_rls_order_average_price_divided(self):
        doc = self._status({"dstCurrency": "rls", "averagePrice": "2664240",
                            "status": "done", "matchedAmount": "1"})
        self.assertEqual(doc["average_price"], "266424.0")

    def test_market_hint_used_when_dst_missing(self):
        doc = self._status({"averagePrice": "0.998", "status": "done",
                            "matchedAmount": "1"}, market="DAIUSDT")
        self.assertEqual(doc["average_price"], "0.998")
        doc = self._status({"averagePrice": "2664240", "status": "done",
                            "matchedAmount": "1"}, market="BTCIRT")
        self.assertEqual(doc["average_price"], "266424.0")


class TestPlaceLimitOrderUnits(TempCase):
    def test_usdt_order_sends_usdt_price(self):
        captured = {}

        def fake_post(path, body):
            captured.update(body)
            return {"order": {"id": "1", "status": "Active",
                              "matchedAmount": "0"}}

        self.api._post = fake_post
        self.api.place_limit_order("DAIUSDT", "buy", 0.998, 26.31, "bot-1")
        self.assertEqual(captured["dstCurrency"], "usdt")
        self.assertEqual(captured["price"], "0.998")

    def test_irt_order_converts_toman_to_rials(self):
        captured = {}

        def fake_post(path, body):
            captured.update(body)
            return {"order": {"id": "1", "status": "Active",
                              "matchedAmount": "0"}}

        self.api._post = fake_post
        self.api.place_limit_order("BTCIRT", "buy", 21371000000, 0.001,
                                   "bot-1")
        self.assertEqual(captured["dstCurrency"], "rls")
        self.assertEqual(captured["price"], "213710000000")

class TestOpenOrdersUnits(TempCase):
    """open_orders() must land in the bot's canonical symbol + Toman space."""

    def _orders(self, orders):
        self.api._get = lambda path, params=None: {"orders": orders}
        return self.api.open_orders()

    def test_tether_quoted_order_maps_to_usdt_market(self):
        # live payload (order 6540192349): srcCurrency "PAXGold",
        # dstCurrency "Tether" — the mixed-case compare mislabeled this as
        # PAXGOLDIRT and left the 412 USDT price looking like 412 Toman
        out = self._orders([{
            "id": 6540192349, "type": "buy", "execution": "Limit",
            "srcCurrency": "PAXGold", "dstCurrency": "Tether",
            "price": "412", "amount": "0.00637", "matchedAmount": "0",
            "unmatchedAmount": "0.00637", "status": "Active",
            "created_at": "2026-10-07T15:04:40.691002+00:00"}])
        self.assertEqual(out[0]["market"], "PAXGUSDT")
        self.assertEqual(out[0]["price"], "412")
        self.assertEqual(out[0]["status"], "OPEN")

    def test_rls_quoted_order_converts_rials_to_toman(self):
        out = self._orders([{
            "id": 1, "type": "buy", "srcCurrency": "btc",
            "dstCurrency": "﷼", "price": "213710000000", "amount": "0.001",
            "matchedAmount": "0", "unmatchedAmount": "0.001",
            "status": "Active", "created_at": "2026-10-07T15:04:40Z"}])
        self.assertEqual(out[0]["market"], "BTCIRT")
        self.assertEqual(out[0]["price"], "21371000000")

    def test_full_name_base_is_canonicalized(self):
        out = self._orders([{
            "id": 2, "type": "sell", "srcCurrency": "Pyth Network",
            "dstCurrency": "rls", "price": "10000000", "amount": "1",
            "matchedAmount": "0", "unmatchedAmount": "1",
            "status": "Active", "created_at": "2026-10-07T15:04:40Z"}])
        self.assertEqual(out[0]["market"], "PYTHIRT")


class TestEntryReconstructionUnits(unittest.TestCase):
    """A held position's avg entry must never mix Rial and Tether fills.

    Live bug: SAND had IRT-era fills (10k Toman) and, after the first
    USDT-market buy filled (0.0745), _reconstruct_entry averaged BOTH into
    5832.25 "USDT" — a fake ~-205,000 USDT unrealized loss in the UI.
    """

    def setUp(self):
        import shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_entry_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from nobitex_bot.config import Config
        from nobitex_bot.engine import TradingEngine
        cfg = Config()
        cfg.engine.data_dir = str(self.tmp / "data")
        cfg.diagnostics.directory = str(self.tmp / "diagnostics")
        self.e = TradingEngine(cfg)

    def _fill(self, market, side, price, amount, ts):
        self.e.storage.save_fill({
            "trade_id": "t_" + market, "order_id": "o_" + market,
            "decision_id": "", "market": market, "side": side,
            "price": price, "amount": amount,
            "gross_value": str(float(price) * float(amount)),
            "fee_amount": "0", "fee_asset": "USDT", "timestamp": ts})

    def test_usdt_entry_ignores_toman_priced_fills(self):
        self._fill("SANDIRT", "BUY", "9742.58857142", "52.5",
                   "2026-09-26T12:08:28.817Z")
        self._fill("SANDUSDT", "BUY", "0.0745", "35.2",
                   "2026-10-07T15:18:20.759Z")
        avg, _plan, lb_at, cost, lbp = self.e._reconstruct_entry(
            "SANDUSDT", d("35.16656"))
        self.assertAlmostEqual(float(avg), 0.0745, places=8)
        self.assertEqual(lb_at, "2026-10-07T15:18:20.759Z")
        self.assertAlmostEqual(float(cost), 0.0745 * 35.2, places=6)
        self.assertAlmostEqual(float(lbp), 0.0745, places=8)

    def test_irt_entry_ignores_usdt_fills(self):
        self._fill("SANDIRT", "BUY", "9742.58857142", "52.5",
                   "2026-09-26T12:08:28.817Z")
        self._fill("SANDUSDT", "BUY", "0.0745", "35.2",
                   "2026-10-07T15:18:20.759Z")
        avg, _plan, _lb, _cost, _lbp = self.e._reconstruct_entry(
            "SANDIRT", d("52.5"))
        self.assertAlmostEqual(float(avg), 9742.58857142, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
