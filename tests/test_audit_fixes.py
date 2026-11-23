"""Regression pins for the full-repository audit (see docs/audit-2026-11.md).

Four defects, each with a reproducible failure:

1. ``ai.py`` appended to ``gate_notes`` inside the analysis-veto block while
   the list was created AFTER it — enabling ``analysis.enabled`` raised
   ``NameError: name 'gate_notes' is not defined`` for every candidate that
   carried an analysis verdict (i.e. the whole multi-timeframe engine).
2. ``nobitex_api.place_limit_order`` accepted an HTTP 200 with no ``order``
   object and returned ``status=OPEN`` with an empty exchange id: a phantom
   order that can never be reconciled.
3. The duplicate-buy guard in ``risk.py`` only read EXECUTION rows, so a
   real resting BUY (one adopted from the exchange after a lost submit
   response) did not block a second automatic BUY of the same market.
4. A manual dashboard click must keep overriding that guard.

Run:  python -m unittest tests.test_audit_fixes -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from tests.test_ai_committee import candidate, snapshot  # noqa: E402
from nobitex_bot.ai import HeuristicAI  # noqa: E402
from nobitex_bot.collector import SnapshotCollector  # noqa: E402
from nobitex_bot.config import Config  # noqa: E402
from nobitex_bot.demo_market import DemoMarket  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.events import EventBus  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI, NobitexError  # noqa: E402
from nobitex_bot.storage import Storage  # noqa: E402

TMPDIRS: list = []


def tearDownModule() -> None:  # pragma: no cover - cleanup helper
    import shutil
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


def _api() -> NobitexAPI:
    tmp = Path(tempfile.mkdtemp(prefix="nobot_audit_"))
    TMPDIRS.append(tmp)
    cfg = SimpleNamespace(
        data_dir=str(tmp), nobitex_base_url="", nobitex_username="",
        nobitex_password="", nobitex_totp_secret="",
        rls_to_irt_divisor=10, rest_timeout=12)
    return NobitexAPI(cfg)


def _buy_decision(symbol="ATOMUSDT", price=1.0, stop=0.95) -> dict:
    return {
        "decision_id": "dec_audit_test",
        "snapshot_id": "snap",
        "cycle_id": "cycle_audit",
        "symbol": symbol,
        "action": "BUY",
        "confidence": 0.9,
        "time_horizon": "INTRADAY",
        "reason_codes": ["AUDIT"],
        "created_at": "2026-10-29T10:00:00.000Z",
        "expires_at": "2099-01-01T00:00:00.000Z",
        "entry": {"price": price, "min": price, "max": price},
        "stop_loss": {"price": stop, "method": "PERCENT_STOP"},
        "take_profit": [{"price": price * 1.05, "close_percent": 100}],
        "position_size": {"amount": 10.0, "quote_value": 10.0},
        "risk": {"max_loss": 1.0},
        "cost": {"expected_net_return": 0.01},
    }


GOOD_SNAP = {"symbol": "ATOMUSDT", "last_price": 1.0,
             "data_quality": {"score": 0.95},
             "liquidity": {"status": "GOOD", "score": 0.9}}
GOOD_PF = {"risk": {"total_equity": 1000.0, "exposure_ratio": 0.0,
                    "cash_ratio": 1.0, "drawdown": {"current": 0.0}},
           "assets": [{"asset": "USDT", "available": "1000"}],
           "account": {}}
GOOD_PRICES = {"ATOMUSDT": 1.0, "USDTIRT": 100000.0}


class TestAnalysisBranchNotes(unittest.TestCase):
    """Enabling the multi-timeframe engine must not raise NameError."""

    def test_analysis_vetoes_land_in_gate_notes(self):
        cfg = Config()
        cfg.analysis.enabled = True
        cand = candidate()
        cand["analysis"] = {"allow_buy": False,
                            "vetoes": ["OVEREXTENDED", "WEAK_VOLUME"]}
        dec = HeuristicAI(cfg).decide(
            cand, snapshot(),
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})
        self.assertNotEqual(dec["action"], "BUY")
        self.assertIn("OVEREXTENDED", dec["why"]["gate_notes"])
        self.assertIn("WEAK_VOLUME", dec["why"]["gate_notes"])

    def test_analysis_approval_still_reaches_the_front(self):
        cfg = Config()
        cfg.analysis.enabled = True
        cand = candidate()
        cand["analysis"] = {"allow_buy": True, "vetoes": []}
        dec = HeuristicAI(cfg).decide(
            cand, snapshot(),
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})
        self.assertEqual(dec["action"], "BUY")


class TestUnconfirmedOrderReply(unittest.TestCase):
    """HTTP 200 without an order object is not a placement."""

    def test_missing_order_object_raises(self):
        api = _api()
        api._post = lambda path, body: {"status": "ok"}
        with self.assertRaises(NobitexError) as ctx:
            api.place_limit_order("BTCUSDT", "BUY", 100.0, 1.0, "bot-x-1")
        self.assertEqual(ctx.exception.code, "ORDER_UNCONFIRMED")

    def test_real_reply_is_returned_with_its_exchange_id(self):
        api = _api()
        api._post = lambda path, body: {
            "status": "ok",
            "order": {"id": 12345, "status": "Active",
                      "matchedAmount": "0"}}
        out = api.place_limit_order("BTCUSDT", "BUY", 100.0, 1.0, "bot-x-2")
        self.assertEqual(out["order_id"], "12345")
        self.assertEqual(out["status"], "OPEN")


class TestRestingOrderBlocksDuplicateBuy(TempCase):
    """A real order already committed blocks a second automatic BUY."""

    def _engine(self) -> TradingEngine:
        return TradingEngine(self.cfg)

    def _save_open_buy(self, engine: TradingEngine, symbol="ATOMUSDT"):
        engine.storage.save_order({
            "order_id": "exch_999",
            "client_order_id": "bot-cycle-audit-ATOMUSDT-001",
            "decision_id": "",
            "market": symbol,
            "side": "BUY",
            "order_type": "LIMIT",
            "price": "1.0",
            "amount": "10",
            "matched_amount": "0",
            "unmatched_amount": "10",
            "status": "OPEN",
            "fee_asset": "USDT",
            "exchange_order_id": "999",
            "created_at": "2026-10-29T10:00:00.000Z",
            "updated_at": "2026-10-29T10:00:00.000Z",
        })

    def test_auto_buy_is_blocked_by_a_resting_order(self):
        engine = self._engine()
        self._save_open_buy(engine)
        rd = engine.risk.evaluate(_buy_decision(), dict(GOOD_SNAP),
                                  dict(GOOD_PF), dict(GOOD_PRICES))
        self.assertEqual(rd["status"], "REJECTED")
        self.assertIn("BUY_ORDER_ALREADY_ACTIVE", rd["rejection_reasons"])

    def test_without_a_resting_order_the_same_buy_is_approved(self):
        engine = self._engine()
        rd = engine.risk.evaluate(_buy_decision(), dict(GOOD_SNAP),
                                  dict(GOOD_PF), dict(GOOD_PRICES))
        self.assertEqual(rd["status"], "APPROVED",
                         rd.get("rejection_reasons"))

    def test_manual_click_is_not_blocked_by_the_resting_order(self):
        engine = self._engine()
        self._save_open_buy(engine)
        rd = engine.risk.evaluate(_buy_decision(), dict(GOOD_SNAP),
                                  dict(GOOD_PF), dict(GOOD_PRICES),
                                  manual=True)
        self.assertEqual(rd["status"], "APPROVED",
                         rd.get("rejection_reasons"))


class TestCandlesArePersisted(unittest.TestCase):
    """The candles the backtest replays are actually written, once per bar."""

    def test_collector_stores_candles_without_duplicating_them(self):
        tmp = Path(tempfile.mkdtemp(prefix="nobot_candles_"))
        TMPDIRS.append(tmp)
        cfg = Config()
        cfg.engine.data_dir = str(tmp)
        events = EventBus(tmp / "events.jsonl")
        storage = Storage(tmp)
        collector = SnapshotCollector(
            cfg, DemoMarket(cfg.exchange, events), events, storage)
        market = collector._collect_market("BTCIRT")
        first = len(storage.candles("BTCIRT", "30m", 1000))
        collector._collect_market("BTCIRT")
        second = len(storage.candles("BTCIRT", "30m", 1000))
        self.assertGreater(first, 0)
        self.assertEqual(first, len(market["ohlc"]["candles"]))
        # a cycle may cross one 30m boundary; anything more is duplication
        self.assertLessEqual(second, first + 1)


if __name__ == "__main__":
    unittest.main()
