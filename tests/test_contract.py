"""Contract tests — section 95 of the data contract.

Run:  python -m unittest tests.test_contract -v
Covers: valid payloads, missing fields, wrong types, invalid enums,
negative amounts, invalid timestamps, cross-field inconsistency, and the
SS100 pipeline ordering (AI never executes; risk is mandatory).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config, PaperCfg
from nobitex_bot.validation import (
    ValidationResult, data_quality_from_issues, validate_ai_decision,
    validate_iso_timestamp, validate_order_consistency, validate_orderbook,
    validate_portfolio_balances, validate_positive, validate_take_profit,
)


def make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.engine.data_dir = str(tmp / "data")
    cfg.engine.cycle_seconds = 5
    cfg.exchange.demo_seed = 7
    cfg.paper = PaperCfg(starting_usdt=10000.0, starting_irt=0.0)
    return cfg


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = make_cfg(self.tmp)


class TestValidationPrimitives(TempCase):
    def test_valid_timestamp_ok(self):
        res = ValidationResult()
        self.assertTrue(validate_iso_timestamp(
            "2026-09-10T12:30:00.000Z", res, "ts"))

    def test_missing_timezone_rejected(self):
        res = ValidationResult()
        self.assertFalse(validate_iso_timestamp(
            "2026-09-10T12:30:00", res, "ts"))
        self.assertEqual(res.issues[0].code, "INVALID_TIMESTAMP_FORMAT")

    def test_future_timestamp_rejected(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        res = ValidationResult()
        self.assertFalse(validate_iso_timestamp(future, res, "ts"))

    def test_planned_expiry_allowed_future(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)
                  ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        res = ValidationResult()
        self.assertTrue(validate_iso_timestamp(future, res, "expires_at",
                                               allow_future=True))

    def test_negative_price_rejected(self):
        res = ValidationResult()
        self.assertFalse(validate_positive(-1.5, res, "price"))

    def test_nan_rejected(self):
        res = ValidationResult()
        self.assertFalse(validate_positive(float("nan"), res, "price"))


class TestOrderbookRules(TempCase):
    def test_healthy_book_ok(self):
        res = ValidationResult()
        book = {"bids": [{"price": 100.0, "amount": 1.0},
                         {"price": 99.0, "amount": 2.0}],
                "asks": [{"price": 101.0, "amount": 1.0},
                         {"price": 102.0, "amount": 2.0}]}
        validate_orderbook(book, res)
        self.assertTrue(res.ok)

    def test_crossed_book_rejected(self):
        res = ValidationResult()
        book = {"bids": [{"price": 102.0, "amount": 1.0}],
                "asks": [{"price": 101.0, "amount": 1.0}]}
        validate_orderbook(book, res)
        self.assertFalse(res.ok)

    def test_bids_ascending_rejected(self):
        res = ValidationResult()
        book = {"bids": [{"price": 99.0, "amount": 1.0},
                         {"price": 100.0, "amount": 1.0}],
                "asks": [{"price": 101.0, "amount": 1.0}]}
        validate_orderbook(book, res)
        self.assertFalse(res.ok)

    def test_duplicate_level_rejected(self):
        res = ValidationResult()
        book = {"bids": [{"price": 100.0, "amount": 1.0},
                         {"price": 100.0, "amount": 2.0}],
                "asks": [{"price": 101.0, "amount": 1.0}]}
        validate_orderbook(book, res)
        self.assertFalse(res.ok)


class TestCrossField(TempCase):
    def test_take_profit_sum_100(self):
        res = ValidationResult()
        validate_take_profit([{"close_percent": 50}, {"close_percent": 50}],
                             res)
        self.assertTrue(res.ok)

    def test_take_profit_sum_wrong(self):
        res = ValidationResult()
        validate_take_profit([{"close_percent": 50}, {"close_percent": 40}],
                             res)
        self.assertFalse(res.ok)

    def test_order_amount_consistency(self):
        res = ValidationResult()
        validate_order_consistency(
            {"amount": "1.0", "matched_amount": "0.4",
             "unmatched_amount": "0.6", "status": "OPEN"}, res)
        self.assertTrue(res.ok)

    def test_order_amount_inconsistent(self):
        res = ValidationResult()
        validate_order_consistency(
            {"amount": "1.0", "matched_amount": "0.4",
             "unmatched_amount": "0.7", "status": "OPEN"}, res)
        self.assertFalse(res.ok)

    def test_portfolio_total_mismatch(self):
        res = ValidationResult()
        validate_portfolio_balances(
            [{"asset": "BTC", "available": "1", "locked": "0.5",
              "total": "2"}], res)
        self.assertFalse(res.ok)


class TestAiDecisionValidation(TempCase):
    def _decision(self, **over):
        now = datetime.now(timezone.utc)
        dec = {
            "decision_id": "dec_test_001",
            "snapshot_id": "snap_x",
            "cycle_id": "cycle_x",
            "symbol": "BTCUSDT",
            "action": "BUY",
            "confidence": 0.8,
            "time_horizon": "INTRADAY",
            "entry": {"price": 100.0, "min": 99.0, "max": 101.0},
            "stop_loss": {"price": 95.0, "reason": "ATR_SUPPORT"},
            "take_profit": [{"price": 110.0, "close_percent": 50},
                            {"price": 120.0, "close_percent": 50}],
            "position_size": {"amount": 0.01, "quote_value": 1.0},
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "expires_at": (now + timedelta(minutes=30)
                           ).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        dec.update(over)
        return dec

    def test_valid_buy_ok(self):
        res = ValidationResult()
        validate_ai_decision(self._decision(), {"BTCUSDT"}, res)
        self.assertTrue(res.ok)

    def test_unknown_symbol_rejected(self):
        res = ValidationResult()
        validate_ai_decision(self._decision(symbol="FAKEUSDT"),
                             {"BTCUSDT"}, res)
        codes = {i.code for i in res.issues}
        self.assertIn("UNKNOWN_MARKET", codes)

    def test_invalid_action_enum(self):
        res = ValidationResult()
        validate_ai_decision(self._decision(action="YOLO"),
                             {"BTCUSDT"}, res)
        self.assertFalse(res.ok)

    def test_missing_stop_loss(self):
        dec = self._decision()
        dec.pop("stop_loss")
        res = ValidationResult()
        validate_ai_decision(dec, {"BTCUSDT"}, res)
        self.assertFalse(res.ok)

    def test_confidence_out_of_range(self):
        res = ValidationResult()
        validate_ai_decision(self._decision(confidence=1.5),
                             {"BTCUSDT"}, res)
        self.assertFalse(res.ok)

    def test_hold_needs_no_entry(self):
        res = ValidationResult()
        dec = self._decision(action="HOLD")
        for k in ("entry", "stop_loss", "take_profit", "position_size"):
            dec.pop(k)
        validate_ai_decision(dec, {"BTCUSDT"}, res)
        self.assertTrue(res.ok)


class TestPipeline(TempCase):
    """Full SS100 pass on the demo exchange, in-memory assertions."""

    def _engine(self):
        from nobitex_bot.engine import TradingEngine
        return TradingEngine(self.cfg)

    def test_cycle_produces_snapshot(self):
        e = self._engine()
        s = e.run_cycle()
        self.assertIsNotNone(e.latest_snapshot)
        self.assertEqual(e.latest_snapshot["status"], "COMPLETE")
        self.assertEqual(e.latest_snapshot["schema_version"], "1.0")

    def test_snapshot_checksum_without_touching_the_disk(self):
        """Market data comes straight from the API — no snapshot files.

        The old pipeline wrote a ~400KB snapshot_*.json every cycle (and a
        ~800MB events log on top of it). The data folder must stay small, so
        the snapshot lives in memory only and still carries its checksum.
        """
        e = self._engine()
        e.run_cycle()
        snaps = sorted((self.tmp / "data" / "snapshots").glob("snapshot_*.json"))
        self.assertEqual(snaps, [])
        doc = e.latest_snapshot
        self.assertIn("integrity", doc)
        self.assertEqual(doc["integrity"]["algorithm"], "SHA-256")
        self.assertTrue(doc["integrity"]["checksum"])

    def test_snapshot_files_can_be_enabled_explicitly(self):
        e = self._engine()
        e.cfg.engine.persist_snapshots = True
        e.cfg.engine.max_snapshots = 2
        e.collector.cfg = e.cfg
        for _ in range(4):
            e.run_cycle()
        snaps = sorted((self.tmp / "data" / "snapshots").glob("snapshot_*.json"))
        self.assertEqual(len(snaps), 2)
        doc = json.loads(snaps[-1].read_text(encoding="utf-8"))
        self.assertEqual(doc["integrity"]["algorithm"], "SHA-256")

    def test_decisions_validated_and_persisted(self):
        e = self._engine()
        e.run_cycle()
        self.assertTrue(e.latest_decisions)
        self.assertTrue(e.storage.decisions_recent(5))

    def test_buy_flow_executes(self):
        e = self._engine()
        for _ in range(6):
            e.run_cycle()
        self.assertGreaterEqual(
            len([f for f in e.storage.fills(limit=50)]), 1)

    def test_buy_best_never_emits_a_plan_less_buy(self):
        """«خرید بهترین گزینه» must always carry a complete BUY plan.

        Live bug: when the heuristic was unconvinced (HOLD/AVOID => no entry
        plan) the engine simply flipped action to "BUY" on that decision.
        It then had no entry/stop_loss/take_profit/position_size, so the
        SS73 validation rejected it EVERY cycle and no buy was ever sent to
        the exchange — even with idle USDT. buy_best must fall back to the
        complete percentage plan (same one the manual button uses).
        """
        from nobitex_bot.utils import utc_now_iso
        e = self._engine()
        e.cfg.ai.buy_best = True

        def always_hold(cand, *a, **k):
            return {"decision_id": "dec_test_hold_" + cand["symbol"],
                    "snapshot_id": "", "cycle_id": "",
                    "symbol": cand["symbol"], "action": "HOLD",
                    "confidence": 0.5, "time_horizon": "INTRADAY",
                    "reason_codes": [], "created_at": utc_now_iso(),
                    "expires_at": utc_now_iso()}

        e.ai.heuristic.decide = always_hold
        e.run_cycle()

        buys = [d for d in e.latest_decisions if d.get("action") == "BUY"]
        self.assertTrue(buys, "buy_best must still propose the top market")
        for b in buys:
            self.assertTrue((b.get("entry") or {}).get("price"))
            self.assertTrue((b.get("stop_loss") or {}).get("price"))
            self.assertEqual(
                sum(t.get("close_percent", 0) for t in b["take_profit"]), 100)
            self.assertGreater(
                (b.get("position_size") or {}).get("amount", 0), 0)
        rows = [r for r in e.storage.decisions_recent(60)
                if r.get("symbol")]
        rejected = [r for r in rows if not r.get("validation_ok")
                    and (r.get("action") or "").upper() == "BUY"]
        self.assertFalse(rejected,
                         "plan-less BUY rejected by validation: %s" % rejected)

    def _plant_exit_scenario(self, e):
        """Deterministic exit scenario (SS36/37 + SS46).

        Opens a real position via PaperPortfolio.apply_fill at half the
        current price, registers its AI decision, lets one cycle register
        the perf_pending row, then plants a take-profit below the current
        price so the engine's own exit manager must close it next cycle.
        Returns the market symbol.
        """
        from decimal import Decimal
        # exit-path minimum-order guard reads exchange.min_order_usdt —
        # the test positions are tiny (100 USDT), so disable the floor
        e.cfg.exchange.min_order_usdt = 0.0
        e.run_cycle()  # baseline snapshot + candidates
        usdt_cands = [c for c in e.latest_candidates
                      if c["symbol"].endswith("USDT")]
        market = (usdt_cands or e.latest_candidates)[0]["symbol"]
        snap_m = next(m for m in e.latest_snapshot["markets"]
                      if m["symbol"] == market)
        price = Decimal(str(snap_m["last_price"]))
        entry = (price / 2).quantize(Decimal("0.00000001"))
        amount = (Decimal("100") / entry).quantize(Decimal("0.00000001"))

        # fund the paper account and buy through the real fill path
        e.portfolio.balances["USDT"]["available"] += Decimal("100000")
        now = datetime.now(timezone.utc)
        dec = {
            "decision_id": "dec_test_exit",
            "snapshot_id": e.latest_snapshot["snapshot_id"],
            "cycle_id": "cycle_test",
            "symbol": market,
            "action": "BUY",
            "confidence": 0.8,
            "time_horizon": "INTRADAY",
            "entry": {"price": float(entry), "min": float(entry),
                      "max": float(entry)},
            "stop_loss": {"price": float(entry * Decimal("0.9")),
                          "method": "MARKET_STRUCTURE", "reason": "TEST"},
            "take_profit": [{"price": float(entry * Decimal("1.15")),
                             "close_percent": 100}],
            "position_size": {"amount": float(amount),
                              "quote_value": float(amount * entry)},
            "risk": {"max_loss": 10.0, "risk_reward": 1.5},
            "cost": {"estimated_total_cost": 0.0025,
                     "expected_gross_return": 0.15,
                     "expected_net_return": 0.14},
            "reason_codes": ["TEST_ENTRY"],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        }
        e.storage.save_ai_decision(dec, True)
        fill = {
            "trade_id": "trd_test_buy", "order_id": "ord_test_buy",
            "decision_id": dec["decision_id"], "market": market,
            "side": "BUY", "price": str(entry), "amount": str(amount),
            "gross_value": str(entry * amount), "fee_amount": "0",
            "fee_asset": "USDT", "timestamp": now.isoformat(),
        }
        e.portfolio.apply_fill(fill, Decimal("0"))

        # one cycle so _measure_performance registers the open position
        e.run_cycle()

        # TP just below the live price -> next cycle must trigger the exit
        e.storage.kv_set("exit_plans", {market: {
            "stop": str(entry * Decimal("0.9")), "stage": 0,
            "tps": [{"price": str((price * Decimal("0.98")).quantize(
                Decimal("0.00000001"))), "close_percent": 100}],
        }})
        return market

    def test_sell_exits_eventually(self):
        e = self._engine()
        self._plant_exit_scenario(e)
        e.run_cycle()
        sells = [f for f in e.storage.fills(limit=200) if f["side"] == "SELL"]
        self.assertTrue(sells, "expected the forced exit plan to fill a SELL")

    def test_performance_measured_after_close(self):
        e = self._engine()
        self._plant_exit_scenario(e)
        e.run_cycle()
        self.assertGreaterEqual(e.storage.performance_summary()
                                ["measured_decisions"], 1)

    def test_safe_mode_blocks_new_orders(self):
        e = self._engine()
        e.run_cycle()
        e.enter_safe_mode(["TEST"])
        before = len(e.storage.orders(limit=1000))
        for _ in range(3):
            e.run_cycle()
        self.assertEqual(len(e.storage.orders(limit=1000)), before)

    def test_auto_trading_off_blocks_orders(self):
        e = self._engine()
        e.run_cycle()
        e.auto_trading = False
        before = len(e.storage.orders(limit=1000))
        e.run_cycle()
        self.assertEqual(len(e.storage.orders(limit=1000)), before)


class TestRiskEngine(TempCase):
    def _engine(self):
        from nobitex_bot.engine import TradingEngine
        return TradingEngine(self.cfg)

    def test_risk_recorded_for_every_buy(self):
        e = self._engine()
        for _ in range(6):
            e.run_cycle()
        # every BUY decision has a risk decision row
        self.assertTrue(e.storage.risk_recent(50))

    def test_ai_cannot_execute_without_risk(self):
        """SS32/SS100: no execution exists without an approved risk decision."""
        e = self._engine()
        for _ in range(6):
            e.run_cycle()
        execs = e.storage.executions_recent(100)
        risk_ids = {r["risk_decision_id"] for r in e.storage.risk_recent(100)}
        for ex in execs:
            self.assertIn(ex.get("risk_decision_id"), risk_ids)


if __name__ == "__main__":
    unittest.main()
