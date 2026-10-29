"""Safety-hardening pins: defaults, config validation and re-entry locks.

The production risks these tests cover:

* a missing/typoed setting must never start a real-money bot (live trading
  defaults OFF and a saved login no longer force-enables it);
* a broken config.json must stop the start instead of silently falling back
  to defaults (percent signs, quote mode, remote dashboard without password);
* a stop-loss exit must not erase its own re-entry lock, or the same coin
  is bought back on the next cycle;
* the quote selector must also freeze the ladder on the switched-off side;
* the dashboard must reject a foreign Host when it runs password-less on
  loopback (DNS rebinding).

Run:  python -m unittest tests.test_safety_hardening -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.config import (  # noqa: E402
    Config, ConfigError, config_from_dict, load_config, validate_config)
from nobitex_bot.doctor import FAIL, OK, _config_check, _database_check  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.storage import Storage  # noqa: E402
from nobitex_bot.utils import utc_iso  # noqa: E402
from nobitex_bot.webauth import host_header_allowed  # noqa: E402


class TestLiveDefaultsAreSafe(unittest.TestCase):
    def test_dataclass_defaults_never_trade_real_money(self):
        cfg = Config()
        self.assertFalse(cfg.exchange.live_trading)
        self.assertFalse(cfg.exchange.live_by_default)

    def test_missing_live_key_in_the_file_stays_off(self):
        tmp = Path(tempfile.mkdtemp(prefix="nobot_cfg_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "config.json"
        path.write_text('{"exchange": {"mode": "nobitex"}}', encoding="utf-8")
        cfg = load_config(path)
        self.assertFalse(cfg.exchange.live_trading)


class TestConfigValidation(TempCase):
    def _write(self, doc: str) -> Path:
        path = self.tmp / "config.json"
        path.write_text(doc, encoding="utf-8")
        return path

    def test_project_config_is_valid(self):
        cfg = load_config()
        self.assertEqual(cfg.config_errors, [])

    def test_positive_stop_loss_is_rejected(self):
        path = self._write('{"risk": {"default_stop_loss_pct": 5}}')
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertTrue(any("default_stop_loss_pct" in p
                            for p in ctx.exception.problems))

    def test_decimal_percent_is_accepted(self):
        path = self._write(
            '{"risk": {"default_stop_loss_pct": -6.76,'
            ' "default_take_profit_pct": 25.5}}')
        cfg = load_config(path)
        self.assertAlmostEqual(cfg.risk.default_stop_loss_pct, -6.76)
        self.assertAlmostEqual(cfg.risk.default_take_profit_pct, 25.5)

    def test_unknown_quote_mode_is_rejected(self):
        path = self._write('{"exchange": {"trade_quote_markets": "garbage"}}')
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_both_legacy_quote_flags_off_is_rejected(self):
        path = self._write(
            '{"exchange": {"trade_quote_markets": "",'
            ' "prefer_usdt_markets": false, "include_irt_markets": false}}')
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_remote_bind_without_password_is_rejected(self):
        path = self._write(
            '{"engine": {"server_host": "0.0.0.0"},'
            ' "web": {"password": ""}}')
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_bool_strings_are_coerced_not_truthy(self):
        path = self._write(
            '{"exchange": {"live_trading": "false"},'
            ' "risk": {"risk_mode": "0"}}')
        cfg = load_config(path)
        self.assertFalse(cfg.exchange.live_trading)
        self.assertFalse(cfg.risk.risk_mode)

    def test_unknown_key_is_a_warning_not_a_crash(self):
        path = self._write('{"risk": {"max_risk_per_tradee": 0.5}}')
        cfg = load_config(path)
        self.assertEqual(cfg.config_errors, [])
        self.assertTrue(any("max_risk_per_tradee" in w
                            for w in cfg.config_warnings))

    def test_malformed_json_refuses_to_start(self):
        path = self._write("{ not json")
        with self.assertRaises(ConfigError):
            load_config(path)
        # the doctor/settings path can still read it and report
        cfg = load_config(path, strict=False)
        self.assertTrue(cfg.config_errors)

    def test_save_path_validator_rejects_invalid_document(self):
        with self.assertRaises(ConfigError):
            config_from_dict({"risk": {"default_stop_loss_pct": 1.5}},
                             strict=True)
        self.assertEqual(validate_config(Config()), [])


class TestLiveSwitchPersists(TempCase):
    def test_off_switch_is_written_to_config(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({
            "engine": {"data_dir": str(self.tmp / "data")},
            "exchange": {"live_trading": True},
        }), encoding="utf-8")
        cfg = load_config(path)
        e = TradingEngine(cfg)
        res = e.set_live_trading(False)
        self.assertTrue(res["ok"])
        self.assertTrue(res["persisted"])
        self.assertFalse(load_config(path).exchange.live_trading,
                         "a restart must not re-enable real orders")

    def test_logout_leaves_live_off_in_the_file(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({
            "engine": {"data_dir": str(self.tmp / "data")},
            "exchange": {"live_trading": True},
        }), encoding="utf-8")
        cfg = load_config(path)
        e = TradingEngine(cfg)
        e.account_logout()
        self.assertFalse(load_config(path).exchange.live_trading)


class TestLoginNeverEnablesLive(TempCase):
    def test_login_keeps_the_off_switch(self):
        e = TradingEngine(self.cfg)
        e.cfg.exchange.live_trading = False
        e.cfg.exchange.live_by_default = True      # the old shortcut
        e._attach_real_exchange = lambda: {"attached": True}
        e.sync_real_wallet = lambda: {"ok": True, "assets": []}

        class FakeSession:
            username = "u"
            password = "p"
            totp_secret = ""

            def login(self, **kwargs):
                return {"device": "fake",
                        "logged_in_at": utc_iso(datetime.now(timezone.utc))}

        e.nobitex_session = FakeSession()
        res = e.account_login("u", "p")
        self.assertTrue(res["ok"])
        self.assertFalse(e.cfg.exchange.live_trading,
                         "login must not override the explicit OFF switch")


class TestStopExitLockSurvives(TempCase):
    def _engine(self) -> TradingEngine:
        cfg = self.cfg
        cfg.exchange.mode = "demo"
        cfg.exchange.live_trading = False
        e = TradingEngine(cfg)
        e.auto_trading = False
        return e

    def test_closed_position_keeps_its_reentry_lock(self):
        e = self._engine()
        e.storage.upsert_position("BTCUSDT", "0", "100", "0")
        until = utc_iso(datetime.now(timezone.utc) + timedelta(minutes=5))
        e.storage.kv_set("exit_plans", {"BTCUSDT": {
            "stop": 90.0, "stop_pct": -3.0, "stage": 0,
            "stop_lock_until_ts": until, "stop_lock_hadd": 110.0}})
        e._sync_exit_plans()
        plan = e.storage.kv_get("exit_plans", {}).get("BTCUSDT")
        self.assertIsNotNone(plan, "the re-entry lock must not be deleted")
        self.assertEqual(plan.get("stop_lock_until_ts"), until)
        self.assertIn("closed_at", plan)

    def test_expired_lock_is_pruned(self):
        e = self._engine()
        e.storage.upsert_position("BTCUSDT", "0", "100", "0")
        until = utc_iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        e.storage.kv_set("exit_plans", {"BTCUSDT": {
            "stop": 90.0, "stage": 0, "stop_lock_until_ts": until}})
        e._sync_exit_plans()
        self.assertNotIn("BTCUSDT", e.storage.kv_get("exit_plans", {}))

    def test_plain_closed_plan_is_still_removed(self):
        e = self._engine()
        e.storage.upsert_position("BTCUSDT", "0", "100", "0")
        e.storage.kv_set("exit_plans", {"BTCUSDT": {
            "stop": 90.0, "stage": 0}})
        e._sync_exit_plans()
        self.assertNotIn("BTCUSDT", e.storage.kv_get("exit_plans", {}))


class TestLadderHonoursQuoteSelector(TempCase):
    def _engine(self, quote: str) -> TradingEngine:
        self.cfg.exchange.mode = "demo"
        self.cfg.exchange.trade_quote_markets = quote
        self.cfg.exchange.live_trading = False
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.storage.upsert_position("BTCIRT", "1", "100", "0",
                                  last_buy_price=100)
        e.storage.kv_set("exit_plans", {"BTCIRT": {
            "base_price": 100.0, "stage": 0, "stop": 97.0, "stop_pct": -3.0,
            "take_profit_manual": 110.0, "take_profit_manual_pct": 10.0}})
        return e

    def test_off_section_does_not_advance(self):
        e = self._engine("usdt")
        e._advance_profit_locks({"BTCIRT": 120.0})
        plan = e.storage.kv_get("exit_plans", {})["BTCIRT"]
        self.assertEqual(plan.get("stage", 0), 0)

    def test_on_section_still_advances(self):
        e = self._engine("irt")
        e._advance_profit_locks({"BTCIRT": 120.0})
        plan = e.storage.kv_get("exit_plans", {})["BTCIRT"]
        self.assertGreaterEqual(plan.get("stage", 0), 1)


class TestHostHeaderGuard(unittest.TestCase):
    def test_loopback_without_password_rejects_foreign_hosts(self):
        self.assertTrue(host_header_allowed("127.0.0.1", "127.0.0.1:8787",
                                            False))
        self.assertTrue(host_header_allowed("127.0.0.1", "localhost:8787",
                                            False))
        self.assertFalse(host_header_allowed("127.0.0.1", "evil.example",
                                             False))

    def test_password_gate_allows_a_reverse_proxy_host(self):
        self.assertTrue(host_header_allowed("127.0.0.1", "bot.example.com",
                                            True))

    def test_remote_bind_accepts_domains_but_not_junk(self):
        self.assertTrue(host_header_allowed("0.0.0.0", "bot.example.com:443",
                                            False))
        self.assertFalse(host_header_allowed("0.0.0.0", "bad host!", False))


class TestDoctorSafetyChecks(TempCase):
    def test_config_check_reports_invalid_values(self):
        cfg = config_from_dict({"risk": {"default_stop_loss_pct": 2.5}})
        check = _config_check(cfg)
        self.assertEqual(check.status, FAIL)
        self.assertIn("default_stop_loss_pct", check.detail)

    def test_database_check_ok_and_corrupt(self):
        Storage(self.tmp / "data")
        self.cfg.engine.data_dir = str(self.tmp / "data")
        self.assertEqual(_database_check(self.cfg).status, OK)

        bad_dir = self.tmp / "bad"
        bad_dir.mkdir()
        (bad_dir / "trading_bot.db").write_bytes(b"this is not sqlite")
        self.cfg.engine.data_dir = str(bad_dir)
        self.assertEqual(_database_check(self.cfg).status, FAIL)

    def test_corrupt_ledger_fails_with_an_actionable_message(self):
        bad_dir = self.tmp / "bad2"
        bad_dir.mkdir()
        (bad_dir / "trading_bot.db").write_bytes(b"not a database")
        with self.assertRaises(RuntimeError) as ctx:
            Storage(bad_dir)
        self.assertIn("CORRUPT", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
