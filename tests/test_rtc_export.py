"""Privacy tests for the sanitized RTC export pipeline.

Every case builds its own private databases in ``tmp_path``; no exchange
call, no real order and no live database is touched.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nobitex_bot import rtc_export
from nobitex_bot.diagnostics import Diagnostics
from nobitex_bot.rtc_export import (
    EXPORT_FILES,
    ExportSanitizer,
    PrivacyError,
    select_fields,
    validate_payload,
    validate_text,
)


def seed(tmp_path: Path) -> Diagnostics:
    """A private database pair with identifiers we must never publish."""
    diag = Diagnostics(tmp_path / "private", background=False)
    try:
        raise RuntimeError("connection reset by 10.0.0.7")
    except RuntimeError as exc:
        diag.record_error(
            "EXECUTION", "ERROR",
            "submit failed for order ord_123456789 at "
            "C:\\Users\\Administrator\\bot\\nobitex_bot\\execution.py",
            module="execution._fail_real", symbol="BTCIRT",
            market_section="IRT", order_id="ord_123456789",
            trade_id="trd_987654321", cycle_id="cycle_1122334455",
            exc=exc)
    diag.record_failed_trade(
        symbol="ETHUSDT", intended_action="SELL",
        failure_reason="insufficient balance for account_id=99887766",
        failure_category="INSUFFICIENT_BALANCE", execution_mode="live",
        strategy="stop_loss", order_status="REJECTED",
        expected_price="1000", actual_price="1012",
        intended_amount="10", executed_amount="4",
        exchange_response={"code": "InsufficientBalance",
                           "message": "contact owner@example.com"},
        market_conditions={"spread_pct": 0.4},
        portfolio_before={"USDT": "123456.78", "IRT": "9876543210"},
        portfolio_after={"USDT": "120000.00"})
    diag.record_failed_trade(
        symbol="ADAIRT", intended_action="BUY",
        failure_reason="SmallOrder: Order value is limited to above 0.5.",
        failure_category="MIN_ORDER", execution_mode="paper")
    diag.save_lesson(pattern_key="FT:MIN_ORDER",
                     pattern="3 unsuccessful BUY attempts share MIN_ORDER",
                     root_cause="dust-sized order", occurrences=3,
                     confidence=0.62, recommended_correction="raise size",
                     related_failed_trade_ids=["ft_abc123def456"],
                     ai_explanation="see order ord_55555 for context")
    return diag


def export(tmp_path: Path, **kwargs) -> dict:
    diag = seed(tmp_path)
    diag.flush(5)
    target = tmp_path / "RTC"
    try:
        return rtc_export.export_all(diag.data_dir, target,
                                     project_root=tmp_path, **kwargs)
    finally:
        diag.close()


def all_text(root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in sorted(root.glob("*"))
                     if path.suffix in (".json", ".md"))


# --------------------------------------------------------------- file set
def test_every_documented_export_is_written(tmp_path):
    out = export(tmp_path)
    assert out["ok"] is True
    for name in EXPORT_FILES:
        assert (tmp_path / "RTC" / name).exists(), name
    assert set(out["counts"]) >= {"errors", "failed_trades", "lessons"}


def test_no_database_or_sidecar_reaches_the_export_directory(tmp_path):
    export(tmp_path)
    leftovers = [p.name for p in (tmp_path / "RTC").iterdir()
                 if p.suffix in (".db", ".db-wal", ".db-shm",
                                 ".db-journal", ".sync-tmp")]
    assert leftovers == []


# -------------------------------------------------------------- privacy
def test_no_identifiers_paths_or_balances_survive(tmp_path):
    export(tmp_path)
    text = all_text(tmp_path / "RTC")
    for leaked in ("ord_123456789", "trd_987654321", "cycle_1122334455",
                   "account_id=99887766", "99887766",
                   "C:\\Users\\Administrator", "Administrator",
                   "owner@example.com", "123456.78", "9876543210",
                   "portfolio_before", "portfolio_after",
                   "Traceback (most recent call last)"):
        assert leaked not in text, leaked


def test_identifiers_are_pseudonymized_and_correlate_inside_one_export(
        tmp_path):
    san = ExportSanitizer([], tmp_path)
    first = san.identifier("ord_123456789")
    assert first.startswith("id_") and "123456789" not in first
    assert san.identifier("ord_123456789") == first      # stable per export
    assert ExportSanitizer([], tmp_path).identifier("ord_123456789") != first
    export(tmp_path)
    doc = json.loads((tmp_path / "RTC" / "error_summary.json").read_text())
    refs = {row["error_ref"] for row in doc["recurring"]}
    assert refs and all(r.startswith("id_") for r in refs)


def test_source_locations_replace_raw_stack_traces(tmp_path):
    export(tmp_path)
    doc = json.loads((tmp_path / "RTC" / "error_summary.json").read_text())
    frames = doc["recurring"][0]["frames"]
    assert frames, "the recorded exception must keep a source location"
    for frame in frames:
        assert "/" not in frame["file"] and "\\" not in frame["file"]
        assert frame["line"] >= 0
    text = all_text(tmp_path / "RTC")
    assert "Traceback" not in text
    assert ".py\\" not in text and ".py/" not in text


def test_relative_metrics_replace_absolute_amounts(tmp_path):
    export(tmp_path)
    doc = json.loads(
        (tmp_path / "RTC" / "execution_metrics.json").read_text())
    assert doc["recorded_attempts"] == 2
    slippage = doc["slippage_bps_by_category"]
    assert slippage and slippage[0]["median_bps"] == pytest.approx(120.0)
    fills = doc["fill_ratio"]
    assert fills["samples"] == 1 and fills["median"] == pytest.approx(0.4)


# --------------------------------------------------------------- allowlist
def test_field_allowlist_drops_unknown_columns():
    row = {"symbol": "BTCIRT", "order_id": "ord_1",
           "portfolio_before": "{}", "attempt_id": "ft_1"}
    assert select_fields(row, ("symbol",)) == {"symbol": "BTCIRT"}


def test_denied_keys_are_detected_in_any_spelling():
    for key in ("apiKey", "API_KEY", "password", "accountId",
                "portfolio_before", "clientOrderId", "walletAddress"):
        assert rtc_export.is_denied_key(key), key
    assert not rtc_export.is_denied_key("symbol")
    assert not rtc_export.is_denied_key("failure_category")


# -------------------------------------------------------------- validation
def test_validation_refuses_emails_paths_tokens_and_wallets():
    for bad in ({"x": "write to someone@example.com"},
                {"x": "at C:\\Users\\Administrator\\bot.py"},
                {"x": "at /home/operator/bot.py"},
                {"x": "0x" + "a" * 40},
                {"x": "Traceback (most recent call last):"},
                {"token": "abc"}):
        with pytest.raises(PrivacyError):
            validate_payload(bad)
    validate_payload({"symbol": "BTCIRT", "count": 3})


def test_validation_refuses_known_secret_values():
    secret = "top-secret-value-123456"
    with pytest.raises(PrivacyError):
        validate_payload({"message": "failed with " + secret},
                         secrets=[secret])
    with pytest.raises(PrivacyError):
        validate_text("Authorization: Bearer " + "Q" * 30)


def test_a_leaking_document_aborts_the_export_and_writes_nothing(
        tmp_path, monkeypatch):
    def leaky(san, errors):
        doc = {"schema_version": 1, "dataset": "error_summary",
               "synthetic": False, "generated_at": "2026-01-01T00:00:00Z",
               "totals": {}, "recurring": [{"message": "mail a@b.co"}]}
        return doc

    monkeypatch.setattr(rtc_export, "error_summary", leaky)
    with pytest.raises(PrivacyError):
        export(tmp_path)
    target = tmp_path / "RTC"
    assert not list(target.glob("*.json")), "nothing may be written"
    assert not list(target.glob("*.md"))


def test_validate_export_files_reports_missing_reports(tmp_path):
    problems = rtc_export.validate_export_files(tmp_path / "empty")
    assert len(problems) == len(EXPORT_FILES)
    export(tmp_path)
    assert rtc_export.validate_export_files(tmp_path / "RTC") == []


# ----------------------------------------------------------------- content
def test_lessons_stay_proposals(tmp_path):
    export(tmp_path)
    doc = json.loads(
        (tmp_path / "RTC" / "learning_lessons.json").read_text())
    assert doc["lessons"]
    assert all(lesson["verification_status"] == "proposed"
               for lesson in doc["lessons"])
    assert "PROPOSALS" in doc["note"]


def test_reproduction_cases_are_labelled_synthetic(tmp_path):
    export(tmp_path)
    doc = json.loads(
        (tmp_path / "RTC" / "reproduction_cases.json").read_text())
    assert doc["all_synthetic"] is True
    assert doc["cases"] and len(doc["cases"]) >= 10
    for case in doc["cases"]:
        assert case["synthetic"] is True
        assert case["observed_incident"] is False
        assert case["inputs"] and case["expected"]
    # a synthetic fixture must not invent an incident count
    assert all("occurrences" not in case for case in doc["cases"])


def test_schema_export_contains_structure_not_rows(tmp_path):
    export(tmp_path)
    doc = json.loads(
        (tmp_path / "RTC" / "database_schema.json").read_text())
    roles = {db["role"]: db["tables"] for db in doc["databases"]}
    tables = {table["name"] for table in roles["errors"]}
    assert "errors" in tables
    assert {table["name"] for table in roles["failed_trades"]} >= {
        "failed_trades", "lessons"}
    for db in doc["databases"]:
        for table in db["tables"]:
            assert set(table) == {"name", "columns", "indexes"}
            for column in table["columns"]:
                assert set(column) == {"name", "type", "not_null",
                                       "primary_key"}
    assert "backup me" not in json.dumps(doc)


def test_error_and_trade_aggregates_are_present(tmp_path):
    export(tmp_path)
    errors = json.loads(
        (tmp_path / "RTC" / "error_summary.json").read_text())
    assert errors["totals"]["distinct_errors"] == 1
    assert errors["totals"]["occurrences"] == 1
    assert errors["by_category"][0]["value"] == "EXECUTION"
    trades = json.loads(
        (tmp_path / "RTC" / "failed_trade_summary.json").read_text())
    assert trades["totals"]["attempts"] == 2
    categories = {item["category"] for item in trades["by_failure_category"]}
    assert {"INSUFFICIENT_BALANCE", "MIN_ORDER"} <= categories
    assert sum(item["share"] for item in trades["by_failure_category"]) == \
        pytest.approx(1.0)
    report = (tmp_path / "RTC" / "diagnostics_report.md").read_text()
    assert "# Diagnostics report" in report
    assert "synthetic" in report


# ------------------------------------------------------- source untouched
def test_export_is_read_only_on_the_private_databases(tmp_path):
    diag = seed(tmp_path)
    diag.flush(5)
    before = {}
    for name in ("error.db", "failed_trade.db"):
        path = diag.data_dir / name
        with sqlite3.connect(str(path)) as conn:
            before[name] = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master").fetchone()[0], \
                path.stat().st_size
    rtc_export.export_all(diag.data_dir, tmp_path / "RTC",
                          project_root=tmp_path)
    for name, snapshot in before.items():
        path = diag.data_dir / name
        with sqlite3.connect(str(path)) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == snapshot[0]
        assert path.exists()
    diag.close()


def test_empty_databases_still_produce_valid_reports(tmp_path):
    diag = Diagnostics(tmp_path / "empty", background=False)
    try:
        out = rtc_export.export_all(diag.data_dir, tmp_path / "RTC",
                                    project_root=tmp_path)
    finally:
        diag.close()
    assert out["counts"]["errors"] == 0
    assert rtc_export.validate_export_files(tmp_path / "RTC") == []
