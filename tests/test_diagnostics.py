"""Tests for error.db / failed_trade.db and the learning layer.

All databases live in tmp_path; no exchange call and no real order is made
or simulated anywhere in this file.
"""
from __future__ import annotations

import json
import sqlite3
import threading

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


# 13 ---------------------------------------------------------- RTC sync
def test_rtc_sync_uses_backup_and_is_readable(tmp_path):
    data = tmp_path / "data"
    rtc = tmp_path / "RTC"
    rtc.mkdir()
    diag = make(data, rtc_dir=rtc)
    diag.record_error("RTC", "WARNING", "sync me")
    diag.record_failed_trade(symbol="ETHUSDT", intended_action="SELL",
                             failure_reason="liquidity", 
                             failure_category="LIQUIDITY")
    out = diag.sync_to_rtc(force=True)
    assert out["ok"] is True
    for name in ("error.db", "failed_trade.db"):
        dest = rtc / name
        assert dest.exists()
        assert not (rtc / (name + ".sync-tmp")).exists()
        with sqlite3.connect(str(dest)) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reader = sqlite3.connect(str(rtc / "error.db"))
    try:
        assert reader.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 1
        # a second sync while an RTC reader holds the file open must neither
        # lose rows nor duplicate them (Windows-safe fallback in backup_to)
        diag.record_error("RTC", "WARNING", "second")
        out = diag.sync_to_rtc(force=True)
        assert out["ok"] is True
        assert reader.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 2
    finally:
        reader.close()
    diag.close()


def test_rtc_sync_respects_the_minimum_interval(tmp_path):
    diag = make(tmp_path / "data", rtc_dir=tmp_path / "RTC",
                rtc_sync_min_interval_s=3600)
    diag.record_error("SYNC", "WARNING", "x")
    assert diag.sync_to_rtc(force=True)["ok"] is True
    skipped = diag.sync_to_rtc()
    assert skipped.get("skipped") is True
    assert diag.sync_to_rtc(force=True)["ok"] is True
    diag.close()


# 14 ------------------------------------------------- backup and recovery
def test_backup_copy_is_usable_after_the_source_is_gone(tmp_path):
    data = tmp_path / "data"
    rtc = tmp_path / "RTC"
    diag = make(data, rtc_dir=rtc)
    diag.record_error("DATABASE", "ERROR", "backup me")
    diag.sync_to_rtc(force=True)
    diag.close()
    # the mirror alone still answers the same query after the source closes
    with sqlite3.connect(str(rtc / "error.db")) as conn:
        row = conn.execute("SELECT message FROM errors").fetchone()
    assert row[0] == "backup me"


# 15 ------------------------------------------- runtime DBs survive updates
def test_sync_rtc_never_deletes_or_copies_runtime_databases(tmp_path,
                                                            monkeypatch):
    import sync_rtc

    src = tmp_path / "src"
    rtc = tmp_path / "RTC"
    (src / "nobitex_bot").mkdir(parents=True)
    (src / "nobitex_bot" / "a.py").write_text("x = 1\n", encoding="utf-8")
    rtc.mkdir()
    (rtc / "error.db").write_bytes(b"SQLite format 3\x00runtime")
    (rtc / "failed_trade.db").write_bytes(b"SQLite format 3\x00runtime")
    (rtc / "stale.py").write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(sync_rtc, "ROOT", src)
    monkeypatch.setattr(sync_rtc, "RTC", rtc)

    written = {(src / "nobitex_bot" / "a.py").resolve()}
    sync_rtc.prune(written)
    assert (rtc / "error.db").exists()
    assert (rtc / "failed_trade.db").exists()
    assert not (rtc / "stale.py").exists()
    # and the copier skips them in the first place
    assert sync_rtc.skip_name("error.db") is True
    assert sync_rtc.skip_name("failed_trade.db") is True


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
    public = cfg.to_public_dict()
    assert "learning_every_cycles" in public["diagnostics"]
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
