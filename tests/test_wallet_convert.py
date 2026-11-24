"""«تبدیل موجودی» — Toman ⇄ Tether manual conversion (engine + web).

Three halves pinned here:
* engine validation: without a real account the dialog's preview/executive
  calls return the Persian login prompt instead of ever reaching the
  exchange; a wrong target or a non-positive amount is refused;
* adapter direction: Toman→Tether MUST go through a `buy` quote (alyou
  receive the coin and spend rials) while Tether→Toman stays the `sell`
  quote — the rls amount passed to the exchange comes from Toman x divisor;
* web routing: the dialog reads GET /api/wallet_convert_preview (no side
  effects) and confirms with POST /api/control/wallet_convert, passing an
  explicit target and an optional amount (None = whole balance).

Run:  python -m unittest tests.test_wallet_convert -v
"""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config             # noqa: E402
from nobitex_bot.engine import TradingEngine      # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI    # noqa: E402
from nobitex_bot.web import BotHTTPServer         # noqa: E402
from nobitex_bot.webauth import DashboardAuth     # noqa: E402


def _demo_engine(tmp: Path) -> TradingEngine:
    cfg = Config()
    cfg.engine.data_dir = str(tmp / "data")
    cfg.diagnostics.directory = str(tmp / "diagnostics")
    cfg.diagnostics.rtc_mirror = ""
    return TradingEngine(cfg)


class TestWalletConvertEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_wconv_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eng = _demo_engine(self.tmp)

    def test_not_attached_is_a_persian_prompt(self):
        out = self.eng.wallet_convert("USDT", 1_000_000)
        self.assertFalse(out["ok"])
        self.assertIn("لاگین", out["error"])
        prev = self.eng.wallet_convert_preview()
        self.assertFalse(prev["real_attached"])
        self.assertIn("error", prev)

    def test_invalid_target_is_refused(self):
        out = self.eng.wallet_convert("BTC", 1)
        self.assertFalse(out["ok"])
        self.assertIn("جهت تبدیل", out["error"])

    def test_nonpositive_amount_is_refused_before_the_exchange(self):
        out = self.eng.wallet_convert("USDT", 0)
        self.assertFalse(out["ok"])
        self.assertIn("صفر", out["error"])
        out = self.eng.wallet_convert("IRT", -3)
        self.assertFalse(out["ok"])
        self.assertIn("صفر", out["error"])


class _FakeSession:
    """Capture exchange_quote/create_trade without touching the network."""

    def __init__(self):
        self.quotes = []

    def exchange_quote(self, coin, amount, direction="sell"):
        self.quotes.append({"coin": coin, "amount": str(amount),
                            "direction": direction})
        return {"quote_id": "Q1", "ref_amount": "10000000",
                "dest_amount": "1.5", "is_sell": direction == "sell",
                "expires_at": "", "raw": {}}

    def exchange_create_trade(self, quote_id):
        return {"status": "ok", "raw": {}}


class TestAdapterConvertDirection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_wconv_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cfg = Config()
        cfg.exchange.data_dir = str(self.tmp)
        self.ex = NobitexAPI(cfg)
        self.ex._refuse_read_only = lambda *_a, **_k: None
        self.session = _FakeSession()
        self.ex.session = self.session

    def test_toman_to_usdt_is_a_buy_quote_with_rials(self):
        res = self.ex.convert_irt_to_usdt(1_500_000)
        q = self.session.quotes[-1]
        self.assertEqual(q["direction"], "buy")
        self.assertEqual(q["coin"], "usdt")
        self.assertEqual(Decimal(q["amount"]),
                         Decimal("15000000"))   # 1.5M toman x10
        self.assertTrue(res["converted"])
        self.assertEqual(res["converted_amount"], 1.5)

    def test_usdt_to_toman_is_a_sell_quote_priced_by_the_pane(self):
        res = self.ex.convert_usdt_to_irt(2.0, usdt_toman=750_000)
        q = self.session.quotes[-1]
        self.assertEqual(q["direction"], "sell")
        self.assertEqual(Decimal(q["amount"]),
                         Decimal("15000000"))   # 2 u x 750k toman x10
        self.assertEqual(res["usdt_sold"], 2.0)
        # 15,000,000 rls received -> 1.5M toman
        self.assertIsInstance(res["converted_amount"], float)

    def test_no_cached_price_blocks_the_sell_quote(self):
        self.ex._stats_cache = {}
        self.ex.session.exchange_quote = \
            lambda *a, **k: self.fail("a quote without a price leaked out")
        with self.assertRaises(Exception) as ctx:
            self.ex.convert_usdt_to_irt(1.0)
        self.assertIn("قیمت تتر", str(ctx.exception))


class _DialogEngine:
    """Two-route stub whose calls the web tests can inspect."""

    last_convert = None

    @classmethod
    def reset(cls):
        _DialogEngine.last_convert = None

    def wallet_convert_preview(self):
        return {"real_attached": True, "irt": 5_000_000.0,
                "usdt": 2.5, "usdt_toman": 700_000.0, "interaction": "both"}

    def wallet_convert(self, target, amount=None):
        _DialogEngine.last_convert = (target, amount)
        return {"ok": True, "direction":
                "irt_to_usdt" if target == "USDT" else "usdt_to_irt",
                "converted_amount": 1.25}

    def status(self):
        return {"running": False}


class TestWebRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_wconv_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cfg = Config()
        cfg.engine.data_dir = str(self.tmp / "data")
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({"web": {"password": ""}}),
                                 encoding="utf-8")
        self.server = BotHTTPServer(
            ("127.0.0.1", 0), _DialogEngine(),
            DashboardAuth(cfg, config_path=self.cfg_path))
        self.addCleanup(self.server.server_close)
        self.addCleanup(_DialogEngine.reset)
        _DialogEngine.reset()  # class attr is shared across tests
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def _req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, json.dumps(body or {}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_preview_is_read_only(self):
        st, body = self._req("GET", "/api/wallet_convert_preview")
        self.assertEqual(st, 200)
        self.assertTrue(body["real_attached"])
        self.assertEqual(body["usdt"], 2.5)
        self.assertIsNone(_DialogEngine.last_convert)

    def test_confirm_passes_target_and_amount(self):
        st, body = self._req("POST", "/api/control/wallet_convert",
                             {"target": "IRT", "amount": 2})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(_DialogEngine.last_convert, ("IRT", 2.0))

    def test_bad_amount_is_a_persian_error(self):
        st, body = self._req("POST", "/api/control/wallet_convert",
                             {"target": "USDT", "amount": "abc"})
        self.assertEqual(st, 200)
        self.assertFalse(body["ok"])
        self.assertIn("نامعتبر", body["error"])


if __name__ == "__main__":
    unittest.main()
