"""Tests for error.db / failed_trade.db and the learning layer.

All databases live in tmp_path; no exchange call and no real order is made
or simulated anywhere in this file.
"""
from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from nobitex_bot import diagnostics as diag_mod
from nobitex_bot import learning
from nobitex_bot.config import Config, DiagnosticsCfg, validate_config
from nobitex_bot.diagnostics import Diagnostics, classify_failure


def make(tmp_path, **kw):
    kw.setdefault("background", False)
    return Diagnostics(tmp_path, **kw)


# 1 ------------------------------------------------------------- creation
def test_databases_and_tables_are_created(tmp_path):
    make(tmp_path)   # constructing it must create both files
    for name in ("error.db", "failed_trade.db"):
        assert (tmp_path / name).exists(), name
    with sqlite3.connect(str(tmp_path / "error.db")) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"errors", "meta"} <= tables
    with sqlite3.connect(str(tmp_path / "failed_trade.db")) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"failed_trades", "lessons", "learning_reports"} <= tables


# 2 ---------------------------------------------------------- persistence
def test_records_survive_a_restart(tmp_path):
    diag = make(tmp_path)
    diag.record_error("EXCHANGE_API", "ERROR", "connection reset by peer")
    diag.record_failed_trade(symbol="BTCIRT", intended_action="BUY",
                             failure_reason="insufficient balance",
                             failure_category="INSUFFICIENT_BALANCE")
    diag.close()

    again = make(tmp_path)
    assert len(again.errors()) == 1
    assert len(again.failed_trades()) == 1
    assert again.summary()["errors_total"] == 1
    again.close()


# 3 --------------------------------------------------------- error fields
def test_error_record_contains_context(tmp_path):
    diag = make(tmp_path)
    eid = diag.record_error(
        "EXECUTION", "HIGH", "order rejected", module="execution._fail_real",
        symbol="ETHIRT", market_section="IRT", order_id="ord_1",
        trade_id="trd_1", cycle_id="cycle_1",
        context={"stage": "submit"})
    row = diag.errors()[0]
    assert row["error_id"] == eid
    assert row["category"] == "EXECUTION"
    assert row["severity"] == "ERROR"   # HIGH is stored on the one scale
    assert row["symbol"] == "ETHIRT"
    assert row["market_section"] == "IRT"
    assert row["order_id"] == "ord_1"
    assert row["resolution"] == "open"
    assert json.loads(diag.error(eid)["context"])["stage"] == "submit"


# 4 ------------------------------------------------------------ deduplicate
def test_identical_errors_are_deduplicated_with_counters(tmp_path):
    diag = make(tmp_path)
    a = diag.record_error("DATABASE", "ERROR", "disk I/O error")
    b = diag.record_error("DATABASE", "ERROR", "disk I/O error")
    assert a == b
    rows = diag.errors()
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2
    assert rows[0]["first_seen"] <= rows[0]["last_seen"]
    # a different message is a different problem
    diag.record_error("DATABASE", "ERROR", "database is locked")
    assert len(diag.errors()) == 2


def test_resolved_error_reappearing_is_marked_regressed(tmp_path):
    diag = make(tmp_path)
    eid = diag.record_error("SYNC", "WARNING", "wallet sync timeout")
    assert diag.resolve_error(eid, "resolved")
    diag.record_error("SYNC", "WARNING", "wallet sync timeout")
    assert diag.error(eid)["resolution"] == "regressed"


# 5 ------------------------------------------------------------ stack trace
def test_stack_trace_is_recorded(tmp_path):
    diag = make(tmp_path)
    try:
        raise ValueError("boom-in-test")
    except ValueError as exc:
        diag.record_error("UNKNOWN", "ERROR", "", module="test",
                          exc=exc)
    row = diag.error(diag.errors()[0]["error_id"])
    assert "ValueError" in (row["stack_trace"] or "")
    assert "boom-in-test" in (row["stack_trace"] or "")
    assert row["exception_type"] == "ValueError"


# 6 ------------------------------------------------------------- redaction
def test_sensitive_values_are_redacted(tmp_path):
    secret = "SUPER-SECRET-API-KEY-123456"
    # built at runtime so the source itself holds no token-shaped literal
    fake_token = "abcdefghijklmnopqrstuvwxyz" + "0123456789"
    diag = make(tmp_path, secrets=[secret])
    diag.record_error(
        "CONFIG", "ERROR",
        "failed with key %s and Authorization: Bearer %s"
        % (secret, fake_token),
        context={"password": "hunter2", "nested": {"api_key": secret}})
    row = diag.error(diag.errors()[0]["error_id"])
    blob = json.dumps(row, ensure_ascii=False)
    assert secret not in blob
    assert "hunter2" not in blob
    assert fake_token not in blob
    assert diag_mod.REDACTED in row["message"]


# 7 ------------------------------------------------------- failed trade row
def test_failed_trade_is_recorded_without_inventing_data(tmp_path):
    diag = make(tmp_path)
    aid = diag.record_failed_trade(
        symbol="ADAIRT", intended_action="sell",
        failure_reason="SmallOrder: Order value is limited to above 0.5.",
        execution_mode="live", strategy="stop_loss",
        expected_price="1200", intended_amount="3", executed_amount="0",
        order_status="REJECTED", exchange_response={"code": "SmallOrder"},
        market_conditions={"spread_pct": 0.4},
        portfolio_before={"IRT": "100"})
    rows = diag.failed_trades()
    assert len(rows) == 1
    row = rows[0]
    assert row["attempt_id"] == aid
    assert row["symbol"] == "ADAIRT"
    assert row["intended_action"] == "SELL"
    assert row["market_section"] == "IRT"
    assert row["failure_category"] == "MIN_ORDER"
    assert row["outcome"] == "failed"
    assert row["is_failed"] == 1
    # data that was not supplied stays NULL
    assert row["actual_price"] is None
    assert row["fees"] is None
    assert row["candles"] is None


def test_failure_classification():
    assert classify_failure("SmallOrder: Order value is limited") == "MIN_ORDER"
    assert classify_failure("insufficient balance") == "INSUFFICIENT_BALANCE"
    assert classify_failure("NETWORK: timed out") == "API_NETWORK"
    assert classify_failure("completely unheard of") == "UNKNOWN"


# 8 ------------------------------------------------- error <-> trade link
def test_failed_trade_links_to_its_error(tmp_path):
    diag = make(tmp_path)
    eid = diag.record_error("EXECUTION", "ERROR", "real order rejected",
                            order_id="ord_9")
    diag.record_failed_trade(symbol="BTCUSDT", intended_action="buy",
                             failure_reason="real order rejected",
                             error_id=eid, order_id="ord_9",
                             failure_category="EXCHANGE_BEHAVIOR")
    assert diag.failed_trades()[0]["error_id"] == eid


# 9/10 ------------------------------------------- repeated failures + lesson
def test_repeated_failures_become_a_proposed_lesson(tmp_path):
    diag = make(tmp_path)
    for _ in range(4):
        diag.record_failed_trade(symbol="ZECIRT", intended_action="BUY",
                                 failure_reason="insufficient balance",
                                 failure_category="INSUFFICIENT_BALANCE")
    single = learning.analyze(diag, min_evidence=10)
    assert single["lesson_count"] == 0  # not enough evidence yet
    out = learning.analyze(diag, min_evidence=3)
    assert out["ok"] is True and out["lesson_count"] >= 1
    lessons = diag.lessons()
    assert lessons
    lesson = lessons[0]
    assert lesson["verification_status"] == "proposed"
    assert lesson["occurrences"] >= 3
    assert lesson["recommended_correction"]
    assert lesson["pattern_key"].startswith("FT:")
    # running it again updates instead of duplicating
    count = len(diag.lessons())
    learning.analyze(diag, min_evidence=3)
    assert len(diag.lessons()) == count


def test_lesson_status_can_only_be_moved_explicitly(tmp_path):
    diag = make(tmp_path)
    lid = diag.save_lesson(pattern_key="FT:MIN_ORDER", pattern="p",
                           occurrences=5, confidence=0.7)
    assert diag.set_lesson_status(lid, "verified") is True
    assert diag.lessons(status="verified")[0]["lesson_id"] == lid
    assert diag.set_lesson_status(lid, "nonsense") is False


def test_recurring_errors_become_lessons(tmp_path):
    diag = make(tmp_path)
    for _ in range(4):
        diag.record_error("MARKET_DATA", "WARNING", "market collection failed")
    learning.analyze(diag, min_evidence=3)
    keys = [l["pattern_key"] for l in diag.lessons()]
    assert any(k.startswith("ER:") for k in keys)


# 11 ------------------------------------------------------ AI unavailable
def test_ai_unavailable_falls_back_to_local_analysis(tmp_path):
    diag = make(tmp_path)
    diag.record_failed_trade(symbol="XRPIRT", intended_action="BUY",
                             failure_reason="insufficient balance",
                             failure_category="INSUFFICIENT_BALANCE")
    cfg = type("AI", (), {"llm_base_url": "", "llm_api_key": "",
                          "llm_model": ""})()
    out = learning.ai_report(diag, cfg)
    assert out["ok"] is False
    assert "not configured" in out["reason"]
    assert learning.analyze(diag, min_evidence=1)["ok"] is True


# 12 -------------------------------------------------------- concurrency
def test_concurrent_writes_do_not_lose_records(tmp_path):
    diag = make(tmp_path, background=True)
    errors = []

    def worker(n: int):
        try:
            for i in range(25):
                diag.record_error("WORKER", "WARNING",
                                  "worker %d iteration %d" % (n, i))
                diag.record_failed_trade(
                    symbol="BTCIRT", intended_action="BUY",
                    failure_reason="concurrent failure %d-%d" % (n, i),
                    failure_category="UNKNOWN")
        except Exception as exc:  # pragma: no cover - a failure is the test
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    assert diag.flush(10) is True
    assert diag.summary()["error_occurrences"] >= 150
    assert diag.summary()["failed_trades_total"] == 150
    diag.close()


# 13 ------------------------------------------------ private local backup
def test_local_backup_is_consistent_and_never_touches_the_source(tmp_path):
    diag = make(tmp_path / "data")
    diag.record_error("DATABASE", "ERROR", "backup me")
    diag.record_failed_trade(symbol="ETHUSDT", intended_action="SELL",
                             failure_reason="liquidity",
                             failure_category="LIQUIDITY")
    dest = tmp_path / "backups"
    out = diag.backup_local(dest)
    assert out["ok"] is True
    for name in ("error.db", "failed_trade.db"):
        assert (dest / name).exists()
        assert not (dest / (name + ".sync-tmp")).exists()
        with sqlite3.connect(str(dest / name)) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    diag.close()
    # the backup still answers after the source connection is gone
    with sqlite3.connect(str(dest / "error.db")) as conn:
        assert conn.execute("SELECT message FROM errors").fetchone()[0] == \
            "backup me"


def test_diagnostics_has_no_rtc_mirror(tmp_path):
    """The bot owns private databases only; RTC gets sanitized exports."""
    diag = make(tmp_path)
    assert not hasattr(diag, "sync_to_rtc")
    assert not hasattr(diag, "rtc_dir")
    assert "rtc" not in diag.summary()
    diag.close()


# 15 ------------------------------------------- private DBs stay in place
def test_sync_rtc_deletes_raw_databases_instead_of_shipping_them(
        tmp_path, monkeypatch):
    import sync_rtc

    src = tmp_path / "src"
    rtc = tmp_path / "RTC"
    (src / "nobitex_bot").mkdir(parents=True)
    (src / "nobitex_bot" / "a.py").write_text("x = 1\n", encoding="utf-8")
    private = src / "error.db"
    private.write_bytes(b"SQLite format 3\x00private")
    rtc.mkdir()
    (rtc / "error.db").write_bytes(b"SQLite format 3\x00runtime")
    (rtc / "failed_trade.db").write_bytes(b"SQLite format 3\x00runtime")
    (rtc / "failed_trade.db-wal").write_bytes(b"wal")
    (rtc / "stale.py").write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(sync_rtc, "ROOT", src)
    monkeypatch.setattr(sync_rtc, "RTC", rtc)

    written = {(src / "nobitex_bot" / "a.py").resolve()}
    sync_rtc.prune(written)
    assert not (rtc / "error.db").exists()
    assert not (rtc / "failed_trade.db").exists()
    assert not (rtc / "failed_trade.db-wal").exists()
    assert not (rtc / "stale.py").exists()
    # the private database outside RTC/ is byte-for-byte untouched
    assert private.read_bytes() == b"SQLite format 3\x00private"
    # and the copier never takes them as source files either
    assert sync_rtc.skip_name("error.db") is True
    assert sync_rtc.skip_name("failed_trade.db") is True
    assert sync_rtc.skip_name("failed_trade.db-wal") is True

    # a raw database reappearing in RTC/ is a hard failure, not a skip
    (rtc / "error.db").write_bytes(b"SQLite format 3\x00runtime")
    assert sync_rtc.scan() >= 1


# --------------------------------------------------------------- retention
def test_retention_is_bounded_and_never_touches_lessons(tmp_path):
    diag = make(tmp_path, max_errors=2, max_failed_trades=2,
                retention_days=0)
    for i in range(5):
        # letters, not digits: the fingerprint collapses numbers on purpose
        diag.record_error("UNKNOWN", "ERROR", "distinct error %s"
                          % chr(65 + i))
        diag.record_failed_trade(symbol="BTCIRT", intended_action="BUY",
                                 failure_reason="distinct %d" % i)
    diag.save_lesson(pattern_key="FT:KEEP", pattern="keep me", occurrences=9)
    removed = diag.prune()
    assert diag.summary()["errors_total"] == 2
    assert diag.summary()["failed_trades_total"] == 2
    assert removed["errors"] == 3 and removed["failed_trades"] == 3
    assert len(diag.lessons()) == 1
    diag.close()


# ------------------------------------------------------ logging integration
def test_logging_handler_captures_errors(tmp_path):
    import logging
    diag = make(tmp_path)
    handler = diag_mod.DiagnosticsLogHandler(diag)
    logger = logging.getLogger("test.diagnostics.handler")
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    try:
        raise RuntimeError("logged failure")
    except RuntimeError:
        logger.exception("something broke")
    logger.removeHandler(handler)
    rows = diag.errors()
    assert rows and "something broke" in rows[0]["message"]
    assert rows[0]["severity"] == "ERROR"


def test_writes_never_raise_after_close(tmp_path):
    diag = make(tmp_path)
    diag.close()
    # closed: nothing is written and nothing is raised (the id is unused)
    diag.record_error("UNKNOWN", "ERROR", "after close")
    diag.record_failed_trade(symbol="A", intended_action="BUY")
    assert diag.errors() == []


def test_disabled_instance_is_a_safe_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(diag_mod, "_DEFAULT", None)
    assert diag_mod.record_error("UNKNOWN", "ERROR", "x") == ""
    assert diag_mod.record_failed_trade(symbol="A",
                                       intended_action="BUY") == ""


# ------------------------------------------------------------------ config
def test_config_exposes_and_validates_diagnostics():
    cfg = Config()
    assert isinstance(cfg.diagnostics, DiagnosticsCfg)
    assert cfg.diagnostics.enabled is True
    # the private location is an explicit setting, not an inferred path
    assert cfg.diagnostics.directory == "data"     # "" = project root
    public = cfg.to_public_dict()
    for key in ("directory", "learning_every_cycles"):
        assert key in public["diagnostics"], key
    # no setting can make the bot copy a raw database into RTC/
    for gone in ("rtc_sync", "rtc_mirror", "rtc_sync_min_interval_s"):
        assert gone not in public["diagnostics"]
    assert validate_config(cfg) == []
    bad = Config()
    bad.diagnostics.retention_days = -1
    assert any("retention_days" in p for p in validate_config(bad))


def test_summary_shape(tmp_path):
    diag = make(tmp_path)
    diag.record_error("AI", "CRITICAL", "model down")
    diag.record_failed_trade(symbol="BTCIRT", intended_action="BUY",
                             failure_reason="liquidity",
                             failure_category="LIQUIDITY")
    summary = diag.summary()
    for key in ("errors_total", "critical_errors", "unresolved_errors",
                "repeated_errors", "failed_trades_total", "lessons_total",
                "by_category", "failed_by_category", "dropped_writes"):
        assert key in summary
    assert summary["critical_errors"] == 1
    diag.close()


# ------------------------------------------------ explicit engine location
def test_engine_uses_the_explicit_diagnostics_directory(tmp_path):
    from nobitex_bot.engine import TradingEngine

    cfg = Config()
    cfg.engine.data_dir = str(tmp_path / "data")
    cfg.diagnostics.directory = str(tmp_path / "diag")
    cfg.diagnostics.background_writes = False
    try:
        engine = TradingEngine(cfg)
        assert engine.diagnostics is not None
        assert Path(engine.diagnostics.data_dir).resolve() == \
            (tmp_path / "diag").resolve()
        assert (tmp_path / "diag" / "error.db").exists()
        assert (tmp_path / "diag" / "failed_trade.db").exists()
        assert not (tmp_path / "data" / "error.db").exists()
    finally:
        diag_mod.reset_default()


def test_engine_resolves_a_relative_directory_against_the_project_root(
        tmp_path, monkeypatch):
    import nobitex_bot.engine as engine_mod

    fake_root = tmp_path / "project"
    (fake_root / "nobitex_bot").mkdir(parents=True)
    monkeypatch.setattr(engine_mod, "__file__",
                        str(fake_root / "nobitex_bot" / "engine.py"))
    cfg = Config()
    cfg.engine.data_dir = str(tmp_path / "data")
    cfg.diagnostics.directory = "state"
    cfg.diagnostics.background_writes = False
    try:
        engine = engine_mod.TradingEngine(cfg)
        assert Path(engine.diagnostics.data_dir).resolve() == \
            (fake_root / "state").resolve()
    finally:
        diag_mod.reset_default()


# ----------------------------------------------- dashboard HTTP handlers
class TestDiagnosticsHandlers(unittest.TestCase):
    """Handler-level tests: /api/diagnostics and /api/diagnostics/error.

    A real TradingEngine backs the real HTTP handler, so the signed-out 401,
    the filter plumbing and the stack-trace endpoint are exercised end to
    end without touching a live database or placing any order.
    """

    PW = "diag-pass"

    def setUp(self):
        from nobitex_bot.engine import TradingEngine
        from nobitex_bot.webauth import DashboardAuth
        from nobitex_bot.web import BotHTTPServer

        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_diaghttp_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cfg = Config()
        cfg.engine.data_dir = str(self.tmp / "data")
        cfg.diagnostics.directory = str(self.tmp / "diagnostics")
        cfg.diagnostics.background_writes = False
        cfg.web.password = self.PW
        cfg.web.failure_delay_s = 0
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({
            "web": {"password": self.PW, "failure_delay_s": 0}}),
            encoding="utf-8")
        self.engine = TradingEngine(cfg)
        self.eid = self.engine.diagnostics.record_error(
            "EXECUTION", "ERROR", "order rejected for the handler test",
            module="execution._fail_real", symbol="BTCIRT")
        self.engine.diagnostics.record_failed_trade(
            symbol="BTCIRT", intended_action="BUY",
            failure_reason="insufficient balance",
            failure_category="INSUFFICIENT_BALANCE")
        self.server = BotHTTPServer(("127.0.0.1", 0), self.engine,
                                    DashboardAuth(cfg,
                                                  config_path=cfg_path))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.addCleanup(self.engine.diagnostics.close)
        self.addCleanup(diag_mod.reset_default)

    def _shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1",
                                          self.server.server_address[1],
                                          timeout=10)

    def _get(self, path: str, cookie: str = ""):
        conn = self._conn()
        try:
            conn.request("GET", path, headers={"Cookie": cookie} if cookie
                         else {})
            res = conn.getresponse()
            return res.status, res.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def _login(self) -> str:
        from nobitex_bot.webauth import COOKIE_NAME

        payload = json.dumps({"password": self.PW}).encode("utf-8")
        conn = self._conn()
        try:
            conn.request("POST", "/api/auth/login", body=payload,
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(payload))})
            res = conn.getresponse()
            res.read()
            self.assertEqual(res.status, 200)
            cookie = res.getheader("Set-Cookie", "")
        finally:
            conn.close()
        self.assertIn(COOKIE_NAME + "=", cookie)
        token = cookie.split(COOKIE_NAME + "=", 1)[1].split(";", 1)[0]
        return "%s=%s" % (COOKIE_NAME, token)

    def test_both_endpoints_are_401_when_signed_out(self):
        for path in ("/api/diagnostics",
                     "/api/diagnostics/error?id=" + self.eid):
            status, body = self._get(path)
            self.assertEqual(status, 401, path)
            doc = json.loads(body)
            self.assertTrue(doc["auth_required"], path)
            # nothing about the stored error leaks before authentication
            self.assertNotIn("order rejected for the handler test", body)
            self.assertNotIn(self.eid, body)

    def test_summary_rows_and_filters(self):
        cookie = self._login()
        status, body = self._get("/api/diagnostics", cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["summary"]["errors_total"], 1)
        self.assertEqual([r["error_id"] for r in doc["errors"]], [self.eid])
        self.assertEqual(doc["failed_trades"][0]["failure_category"],
                         "INSUFFICIENT_BALANCE")
        self.assertIn("lessons", doc)
        self.assertEqual(doc["filters"]["category"], "")

        # filters apply to the tables but never to the summary cards
        status, body = self._get(
            "/api/diagnostics?category=AI&failure_category=MIN_ORDER",
            cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["errors"], [])
        self.assertEqual(doc["failed_trades"], [])
        self.assertEqual(doc["summary"]["errors_total"], 1)

        status, body = self._get("/api/diagnostics?category=EXECUTION",
                                 cookie)
        doc = json.loads(body)
        self.assertEqual([r["error_id"] for r in doc["errors"]], [self.eid])

    def test_error_detail_carries_the_stack_trace(self):
        cookie = self._login()
        status, body = self._get("/api/diagnostics/error?id=" + self.eid,
                                 cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["error_id"], self.eid)
        self.assertEqual(doc["message"],
                         "order rejected for the handler test")
        self.assertTrue(str(doc["stack_trace"] or "").strip())

    def test_unknown_or_missing_error_id_is_404(self):
        cookie = self._login()
        for path in ("/api/diagnostics/error?id=err_does_not_exist",
                     "/api/diagnostics/error"):
            status, body = self._get(path, cookie)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(body)["error"], "not found")


if __name__ == "__main__":
    unittest.main()
