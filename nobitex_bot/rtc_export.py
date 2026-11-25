"""Sanitized analytics exports for the commit-ready ``RTC/`` snapshot.

Two-way separation, enforced here:

* PRIVATE side (never leaves this machine and never enters ``RTC/``):
  ``data/*.db`` (trading ledger, ``error.db``, ``failed_trade.db``), their
  WAL/SHM/journal sidecars, runtime logs and ``config.json``.
* RTC side: JSON/Markdown reports built from those databases by EXPLICIT
  FIELD ALLOWLISTS. No raw database, no sidecar, no credential, no account
  identifier, no wallet address, no absolute path, no balance.

Every value passes three gates before it is written:

1. **allowlist** — only the fields named in ``ALLOWED`` are selected from a
   row; a new column added to a table is ignored until it is named here.
2. **sanitization** — credential redaction, path stripping, pseudonymized
   identifiers (random per-export salt, so they correlate inside one export
   and cannot be reversed), and relative ratios instead of absolute amounts.
3. **validation** — :func:`validate_payload` re-scans the finished document;
   a violation raises :class:`PrivacyError` and aborts the export instead of
   producing a leaky report.

The export is deliberately a *separate process* (``python sync_rtc.py``):
the trading bot only ever writes the private databases.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as secrets_mod
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .diagnostics import ERROR_DB_NAME, FAILED_TRADE_DB_NAME, Redactor
from .utils import utc_now_iso

SCHEMA_VERSION = 1
REPORT_FILE = "diagnostics_report.md"
JSON_FILES = (
    "error_summary.json",
    "failed_trade_summary.json",
    "learning_lessons.json",
    "execution_metrics.json",
    "strategy_metrics.json",
    "data_quality_report.json",
    "database_schema.json",
    "reproduction_cases.json",
)
EXPORT_FILES = JSON_FILES + (REPORT_FILE,)


class PrivacyError(RuntimeError):
    """Raised when a document would expose something that must stay private."""


# ---------------------------------------------------------------- allowlists
ERROR_FIELDS = (
    "error_id", "category", "severity", "module", "file_name",
    "function_name", "line_number", "exception_type", "message",
    "stack_trace", "symbol", "market_section", "recovered",
    "recovery_action", "resolution", "occurrences", "first_seen",
    "last_seen",
)

TRADE_FIELDS = (
    "attempt_id", "timestamp", "symbol", "market_section",
    "intended_action", "execution_mode", "strategy", "failure_category",
    "order_status", "failure_reason", "expected_price", "actual_price",
    "intended_amount", "executed_amount", "outcome", "is_rejected",
    "is_partial", "is_cancelled", "is_failed",
)

LESSON_FIELDS = (
    "lesson_id", "pattern_key", "pattern", "root_cause", "occurrences",
    "confidence",     "recommended_correction", "verification_status",
    "ai_explanation", "created_at", "last_reviewed", "evidence",
)

#: keys that may NEVER appear in an export, at any depth. Matched on the
#: normalized key name (lowercase, non-alphanumerics removed) both exactly
#: and as a substring, so ``portfolio_before`` / ``accountBalance`` / ``apiKey``
#: are refused without having to list every spelling.
DENY_KEYS = (
    "password", "passwd", "token", "secret", "apikey", "username",
    "email", "phone", "address", "privatekey", "cookie", "session",
    "credential", "authorization", "totp", "salt", "saltkey",
    "balance", "portfolio", "equity", "networth", "accountid",
    "accountnumber", "iban", "cardnumber", "nationalid", "refid",
    "orderid", "tradeid", "clientorderid", "cycleid", "decisionid",
    "riskdecisionid", "errorid", "attemptid", "lessonid",
)

#: values that must never appear in an exported string
WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|,)]+")
HOME_PATH = re.compile(r"/(?:home|Users|root)/[^\s\"'<>|,)]+")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
EVM_ADDR = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
BECH32_ADDR = re.compile(r"\bbc1[a-z0-9]{25,}\b")
TRON_ADDR = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
TRACEBACK = re.compile(r"Traceback \(most recent call last\)")
BEARER = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{8,}", re.IGNORECASE)
FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
#: exchange/account identifiers that can appear inside free text
INLINE_ID = re.compile(r"\b(?:ord|trd|cycle|dec|risk|acc|uid)_[A-Za-z0-9]{6,}\b")
INLINE_ID_KEY = re.compile(
    r"(?i)\b(clientorderid|order_id|orderid|trade_id|account_id|uid|account)"
    r"(\s*[:=]\s*)([A-Za-z0-9_\-]{4,})")

BANNED_TEXT = (
    ("e-mail address", EMAIL),
    ("absolute Windows path", WIN_PATH),
    ("home directory path", HOME_PATH),
    ("raw Python traceback", TRACEBACK),
    ("bearer token", BEARER),
    ("wallet address", EVM_ADDR),
    ("wallet address", BECH32_ADDR),
    ("wallet address", TRON_ADDR),
)


def _key_norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_denied_key(key: str) -> bool:
    norm = _key_norm(key)
    return any(part in norm for part in DENY_KEYS)


def select_fields(row: dict, allowed: Iterable[str]) -> dict:
    """Allowlist projection: unknown columns are dropped, never forwarded."""
    return {key: row[key] for key in allowed if key in row}


# ---------------------------------------------------------------- sanitizer
class ExportSanitizer:
    """Redacts, strips paths and pseudonymizes identifiers for one export."""

    def __init__(self, secrets: Iterable[str] | None = None,
                 project_root: str | Path | None = None):
        self._redactor = Redactor([s for s in (secrets or [])
                                   if isinstance(s, str)])
        self._root = str(project_root or "").rstrip("\\/")
        # random per export: identifiers correlate within one snapshot and
        # cannot be reversed or matched against a later export
        self._salt = "rtc-export-" + secrets_mod.token_hex(16)

    # -- strings
    @staticmethod
    def _path_label(match: re.Match) -> str:
        raw = match.group(0).rstrip("\\/")
        name = re.split(r"[\\/]", raw)[-1]
        return "<path>/" + name if name else "<path>"

    def text(self, value: Any, limit: int = 600) -> str:
        if value is None:
            return ""
        out = value if isinstance(value, str) else str(value)
        out = self._redactor.text(out, 0)
        if self._root:
            out = re.sub(re.escape(self._root), "<project>", out,
                         flags=re.IGNORECASE)
        out = WIN_PATH.sub(self._path_label, out)
        out = HOME_PATH.sub(self._path_label, out)
        out = EMAIL.sub("<email>", out)
        out = INLINE_ID.sub("<id>", out)
        out = INLINE_ID_KEY.sub(lambda m: m.group(1) + m.group(2) + "<id>",
                                out)
        out = EVM_ADDR.sub("<wallet>", out)
        out = BECH32_ADDR.sub("<wallet>", out)
        out = TRON_ADDR.sub("<wallet>", out)
        out = out.replace("\r", " ").replace("\n", " ")
        out = re.sub(r"\s+", " ", out).strip()
        if limit and len(out) > limit:
            out = out[:limit] + "…[truncated]"
        return out

    # -- identifiers / numbers
    def identifier(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        digest = hashlib.sha256(
            (self._salt + "|" + raw).encode("utf-8", "ignore")).hexdigest()
        return "id_" + digest[:12]

    @staticmethod
    def number(value: Any, digits: int = 6) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        if out != out or out in (float("inf"), float("-inf")):
            return None
        return round(out, digits)

    # -- tracebacks
    def frames(self, trace: Any) -> list[dict]:
        """Source-code locations only: file base name, line, function."""
        out: list[dict] = []
        for match in FRAME.finditer(str(trace or "")):
            raw = match.group(1) or ""
            name = re.split(r"[\\/]", raw)[-1]
            out.append({
                "file": name if name and name not in (".", "..") else "<unknown>",
                "line": int(match.group(2) or 0),
                "function": self.text(match.group(3) or "", 80) or "<module>",
            })
            if len(out) >= 40:
                break
        return out

    # -- nested JSON blobs
    def jsonable(self, value: Any, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= 3:
            return "<truncated>"
        if isinstance(value, dict):
            out = {}
            for key, item in list(value.items())[:40]:
                if is_denied_key(str(key)):
                    continue
                out[self.text(str(key), 60)] = self.jsonable(item, depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return [self.jsonable(item, depth + 1) for item in list(value)[:40]]
        return self.text(value, 400)

# ---------------------------------------------------------------- validation
def _walk(node: Any, path: str, problems: list[str],
          secrets: Iterable[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if is_denied_key(str(key)):
                problems.append("%s.%s: forbidden key" % (path, key))
            _walk(value, "%s.%s" % (path, key), problems, secrets)
        return
    if isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, "%s[%d]" % (path, index), problems, secrets)
        return
    if isinstance(node, str):
        for secret in secrets:
            if secret and secret in node:
                problems.append("%s: known secret value" % path)
                break
        for label, pattern in BANNED_TEXT:
            if pattern.search(node):
                problems.append("%s: %s" % (path, label))
        return


def validate_payload(payload: Any, *, secrets: Iterable[str] = (),
                     label: str = "export") -> None:
    """Raise :class:`PrivacyError` if a finished document leaks anything."""
    problems: list[str] = []
    _walk(payload, label, problems, [s for s in secrets if s])
    if problems:
        # never echo the offending value, only its location and kind
        raise PrivacyError("; ".join(problems[:8]))


def validate_text(text: str, *, secrets: Iterable[str] = (),
                  label: str = "export") -> None:
    problems: list[str] = []
    _walk(str(text or ""), label, problems, [s for s in secrets if s])
    if problems:
        raise PrivacyError("; ".join(problems[:8]))


def validate_export_files(rtc_dir: str | Path, *,
                          secrets: Iterable[str] = ()) -> list[str]:
    """Problems found in the reports already sitting in ``RTC/``."""
    problems: list[str] = []
    base = Path(rtc_dir)
    for name in JSON_FILES:
        path = base / name
        if not path.exists():
            problems.append("%s: missing" % name)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append("%s: unreadable (%s)" % (name, type(exc).__name__))
            continue
        try:
            validate_payload(payload, secrets=secrets, label=name)
        except PrivacyError as exc:
            problems.append("%s: %s" % (name, exc))
    report = base / REPORT_FILE
    if not report.exists():
        problems.append("%s: missing" % REPORT_FILE)
    else:
        try:
            validate_text(report.read_text(encoding="utf-8"),
                          secrets=secrets, label=REPORT_FILE)
        except PrivacyError as exc:
            problems.append("%s: %s" % (REPORT_FILE, exc))
    return problems


# ------------------------------------------------------------------ reading
def _connect(path: Path) -> sqlite3.Connection | None:
    """Read-only connection; never creates or modifies a database."""
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.as_posix(),
                               uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection | None, sql: str) -> list[dict]:
    if conn is None:
        return []
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    except sqlite3.Error:
        return []


def _schema(conn: sqlite3.Connection | None) -> list[dict]:
    if conn is None:
        return []
    tables: list[dict] = []
    for row in _rows(conn, "SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = row["name"]
        if is_denied_key(name):
            # a table about balances/accounts/credentials is not structure
            # this snapshot needs to publish (the allowlist, not a regex,
            # decides)
            continue
        columns = [
            {"name": c["name"], "type": c["type"] or "",
             "not_null": bool(c["notnull"]), "primary_key": bool(c["pk"])}
            for c in _rows(conn, "PRAGMA table_info(%s)" % json.dumps(name))
            if not is_denied_key(c["name"])
        ]
        indexes = []
        for idx in _rows(conn, "PRAGMA index_list(%s)" % json.dumps(name)):
            cols = [c["name"] for c in
                    _rows(conn, "PRAGMA index_info(%s)"
                          % json.dumps(idx["name"]))
                    if not is_denied_key(c["name"])]
            indexes.append({"name": idx["name"], "unique": bool(idx["unique"]),
                            "columns": cols})
        tables.append({"name": name, "columns": columns, "indexes": indexes})
    return tables


def _counts(rows: list[dict], key: str, *, limit: int = 20) -> list[dict]:
    counter = Counter(str(row.get(key) or "<unknown>") for row in rows)
    return [{"value": value, "count": count}
            for value, count in counter.most_common(limit)]


def _share(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def _median(values: list[float]) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return round(clean[mid], 4)
    return round((clean[mid - 1] + clean[mid]) / 2, 4)


def _stamp(value: Any) -> str:
    """Timestamps only; anything else is dropped rather than guessed."""
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z?$", text):
        return text
    return ""


# --------------------------------------------------------------- datasets
def _header(dataset: str, note: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "generated_at": utc_now_iso(),
        "synthetic": False,
        "note": note,
    }


def error_summary(san: ExportSanitizer, errors: list[dict]) -> dict:
    doc = _header(
        "error_summary",
        "Aggregated error monitor. Messages are sanitized; source locations "
        "are file/line/function only. No credentials, no account data.")
    occurrences = sum(int(r.get("occurrences") or 0) for r in errors)
    doc["totals"] = {
        "distinct_errors": len(errors),
        "occurrences": occurrences,
        "critical": sum(1 for r in errors if r.get("severity") == "CRITICAL"),
        "open": sum(1 for r in errors if r.get("resolution") in
                    ("open", "investigating", "regressed")),
        "repeated": sum(1 for r in errors if int(r.get("occurrences") or 0) > 1),
        "recovered": sum(1 for r in errors if int(r.get("recovered") or 0)),
    }
    doc["by_category"] = _counts(errors, "category")
    doc["by_severity"] = _counts(errors, "severity")
    doc["by_module"] = _counts(errors, "module")
    doc["by_market_section"] = _counts(errors, "market_section")
    doc["by_resolution"] = _counts(errors, "resolution")
    ranked = sorted(errors, key=lambda r: -int(r.get("occurrences") or 0))
    doc["recurring"] = [{
        "error_ref": san.identifier(r.get("error_id")),
        "category": san.text(r.get("category"), 40),
        "severity": san.text(r.get("severity"), 20),
        "module": san.text(r.get("module"), 80),
        "file": san.text(r.get("file_name"), 80),
        "function": san.text(r.get("function_name"), 80),
        "line": int(r.get("line_number") or 0),
        "exception_type": san.text(r.get("exception_type"), 80),
        "message": san.text(r.get("message"), 400),
        "frames": san.frames(r.get("stack_trace")),
        "symbol": san.text(r.get("symbol"), 40),
        "market_section": san.text(r.get("market_section"), 10),
        "occurrences": int(r.get("occurrences") or 0),
        "first_seen": _stamp(r.get("first_seen")),
        "last_seen": _stamp(r.get("last_seen")),
        "recovered": bool(r.get("recovered")),
        "recovery_action": san.text(r.get("recovery_action"), 200),
        "resolution": san.text(r.get("resolution"), 20),
    } for r in ranked[:100]]
    return doc


def _num(value: Any) -> float | None:
    """A finite number, or None when the bot did not know it (0 is known)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _slippage_bps(row: dict) -> float | None:
    expected = _num(row.get("expected_price"))
    actual = _num(row.get("actual_price"))
    if not expected or actual is None:
        return None
    return round((actual - expected) / expected * 10000.0, 3)


def _fill_ratio(row: dict) -> float | None:
    intended = _num(row.get("intended_amount"))
    executed = _num(row.get("executed_amount"))
    if not intended or executed is None:
        return None
    return round(executed / intended, 4)


def failed_trade_summary(san: ExportSanitizer, trades: list[dict]) -> dict:
    doc = _header(
        "failed_trade_summary",
        "Unsuccessful execution ATTEMPTS only. A position closed at its "
        "designed stop-loss is a normal outcome and is NOT here. Amounts are "
        "relative (ratios/percentages); absolute balances are never exported.")
    total = len(trades)
    doc["totals"] = {"attempts": total,
                     "outcomes": dict(Counter(
                         str(r.get("outcome") or "failed") for r in trades))}
    doc["by_failure_category"] = [
        {"category": item["value"], "count": item["count"],
         "share": _share(item["count"], total)}
        for item in _counts(trades, "failure_category")]
    doc["by_symbol"] = _counts(trades, "symbol")
    doc["by_market_section"] = _counts(trades, "market_section")
    doc["by_action"] = _counts(trades, "intended_action")
    doc["by_strategy"] = _counts(trades, "strategy")
    doc["by_execution_mode"] = _counts(trades, "execution_mode")
    doc["by_order_status"] = _counts(trades, "order_status")
    doc["shares"] = {
        "rejected": _share(sum(1 for r in trades
                               if int(r.get("is_rejected") or 0)), total),
        "partial_fill": _share(sum(1 for r in trades
                                   if int(r.get("is_partial") or 0)), total),
        "cancelled": _share(sum(1 for r in trades
                                if int(r.get("is_cancelled") or 0)), total),
        "failed": _share(sum(1 for r in trades
                             if int(r.get("is_failed") or 0)), total),
    }
    reasons: Counter = Counter()
    for row in trades:
        text = san.text(row.get("failure_reason"), 160)
        if text:
            reasons[text] += 1
    doc["top_failure_reasons"] = [
        {"reason": text, "count": count} for text, count in reasons.most_common(20)
    ]
    doc["sample_attempts"] = [{
        "attempt_ref": san.identifier(r.get("attempt_id")),
        "timestamp": _stamp(r.get("timestamp")),
        "symbol": san.text(r.get("symbol"), 40),
        "market_section": san.text(r.get("market_section"), 10),
        "action": san.text(r.get("intended_action"), 10),
        "execution_mode": san.text(r.get("execution_mode"), 10),
        "strategy": san.text(r.get("strategy"), 60),
        "failure_category": san.text(r.get("failure_category"), 40),
        "order_status": san.text(r.get("order_status"), 40),
        "outcome": san.text(r.get("outcome"), 20),
        "fill_ratio": _fill_ratio(r),
        "slippage_bps": _slippage_bps(r),
    } for r in sorted(trades, key=lambda r: str(r.get("timestamp") or ""),
                      reverse=True)[:50]]
    return doc


def _lesson_evidence(san: ExportSanitizer, raw: Any) -> dict:
    try:
        evidence = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        evidence = {}
    if not isinstance(evidence, dict):
        evidence = {}
    allowed = ("count", "symbols", "actions", "market_sections", "outcomes",
               "first", "last", "symbol", "average_per_market", "total",
               "severity", "resolution", "occurrences")
    out = {}
    for key in allowed:
        if key in evidence:
            out[key] = san.jsonable(evidence[key])
    samples = evidence.get("samples")
    if isinstance(samples, list):
        out["samples"] = [{
            "attempt_ref": san.identifier(item.get("attempt_id")),
            "timestamp": _stamp(item.get("timestamp")),
            "symbol": san.text(item.get("symbol"), 40),
            "action": san.text(item.get("action"), 10),
            "outcome": san.text(item.get("outcome"), 20),
            "status": san.text(item.get("status"), 40),
            "reason": san.text(item.get("reason"), 160),
        } for item in samples[:5] if isinstance(item, dict)]
    if evidence.get("error_id"):
        out["error_ref"] = san.identifier(evidence["error_id"])
    return out


def learning_lessons(san: ExportSanitizer, lessons: list[dict]) -> dict:
    doc = _header(
        "learning_lessons",
        "Lessons are PROPOSALS. Nothing here has been applied to the strategy; "
        "only an explicit, tested change may mark one verified.")
    doc["totals"] = {
        "lessons": len(lessons),
        "by_status": dict(Counter(str(r.get("verification_status") or
                                     "proposed") for r in lessons)),
    }
    doc["lessons"] = [{
        "lesson_ref": san.identifier(r.get("lesson_id")),
        "pattern_key": san.text(r.get("pattern_key"), 80),
        "pattern": san.text(r.get("pattern"), 400),
        "root_cause": san.text(r.get("root_cause"), 600),
        "occurrences": int(r.get("occurrences") or 0),
        "confidence": san.number(r.get("confidence"), 4),
        "recommended_correction": san.text(r.get("recommended_correction"), 600),
        "verification_status": san.text(r.get("verification_status"), 20),
        "evidence": _lesson_evidence(san, r.get("evidence")),
        "ai_explanation": san.text(r.get("ai_explanation"), 1200),
        "created_at": _stamp(r.get("created_at")),
        "last_reviewed": _stamp(r.get("last_reviewed")),
    } for r in lessons[:200]]
    return doc


def execution_metrics(san: ExportSanitizer, trades: list[dict]) -> dict:
    doc = _header(
        "execution_metrics",
        "Only unsuccessful attempts are recorded, so no success rate can be "
        "derived. Values are relative (basis points, ratios, shares).")
    total = len(trades)
    by_category: dict[str, list[float]] = {}
    fills: list[float] = []
    for row in trades:
        slip = _slippage_bps(row)
        if slip is not None:
            by_category.setdefault(
                str(row.get("failure_category") or "UNKNOWN"), []).append(slip)
        ratio = _fill_ratio(row)
        if ratio is not None:
            fills.append(ratio)
    doc["recorded_attempts"] = total
    doc["slippage_bps_by_category"] = [
        {"category": category, "samples": len(values),
         "median_bps": _median(values), "worst_bps": round(max(values), 3)}
        for category, values in sorted(by_category.items())
    ]
    doc["fill_ratio"] = {
        "samples": len(fills),
        "median": _median(fills),
        "partial_share": _share(
            sum(1 for r in trades if int(r.get("is_partial") or 0)), total),
    }
    doc["min_order_rejections"] = {
        "count": sum(1 for r in trades
                     if str(r.get("failure_category")) == "MIN_ORDER"),
        "share": _share(sum(1 for r in trades
                            if str(r.get("failure_category")) == "MIN_ORDER"),
                        total),
    }
    doc["network_failures"] = {
        "count": sum(1 for r in trades
                     if str(r.get("failure_category")) == "API_NETWORK"),
        "share": _share(sum(1 for r in trades
                            if str(r.get("failure_category")) == "API_NETWORK"),
                        total),
    }
    return doc


def strategy_metrics(san: ExportSanitizer, trades: list[dict]) -> dict:
    doc = _header(
        "strategy_metrics",
        "Counts of unsuccessful attempts per strategy/symbol. These are "
        "reliability signals, NOT profitability results.")
    groups: dict[str, list[dict]] = {}
    for row in trades:
        key = str(row.get("strategy") or "<unset>")
        groups.setdefault(key, []).append(row)
    doc["strategies"] = []
    for strategy, rows in sorted(groups.items(),
                                 key=lambda kv: -len(kv[1])):
        doc["strategies"].append({
            "strategy": san.text(strategy, 80),
            "attempts": len(rows),
            "symbols": _counts(rows, "symbol", limit=10),
            "failure_categories": _counts(rows, "failure_category", limit=10),
            "outcomes": _counts(rows, "outcome", limit=10),
            "first_seen": min((_stamp(r.get("timestamp")) for r in rows
                               if _stamp(r.get("timestamp"))), default=""),
            "last_seen": max((_stamp(r.get("timestamp")) for r in rows
                              if _stamp(r.get("timestamp"))), default=""),
            "share_of_recorded_failures": _share(len(rows), len(trades)),
        })
    doc["symbols"] = []
    by_symbol: dict[str, list[dict]] = {}
    for row in trades:
        by_symbol.setdefault(str(row.get("symbol") or "<unknown>"),
                             []).append(row)
    for symbol, rows in sorted(by_symbol.items(), key=lambda kv: -len(kv[1]))[:25]:
        doc["symbols"].append({
            "symbol": san.text(symbol, 40),
            "attempts": len(rows),
            "market_section": san.text(rows[0].get("market_section"), 10),
            "failure_categories": _counts(rows, "failure_category", limit=8),
        })
    return doc


DATA_QUALITY_CATEGORIES = ("MARKET_DATA", "DATA_QUALITY", "SYNC")


def data_quality_report(san: ExportSanitizer, errors: list[dict],
                        counts: dict) -> dict:
    doc = _header(
        "data_quality_report",
        "Data-quality and synchronization signals observed by the bot, plus "
        "database health. No file paths, no account data.")
    signals = [r for r in errors
               if str(r.get("category")) in DATA_QUALITY_CATEGORIES]
    doc["signals_total"] = len(signals)
    doc["by_category"] = _counts(signals, "category")
    doc["by_module"] = _counts(signals, "module")
    doc["by_symbol"] = _counts(signals, "symbol")
    doc["signals"] = [{
        "error_ref": san.identifier(r.get("error_id")),
        "category": san.text(r.get("category"), 40),
        "module": san.text(r.get("module"), 80),
        "message": san.text(r.get("message"), 300),
        "occurrences": int(r.get("occurrences") or 0),
        "last_seen": _stamp(r.get("last_seen")),
        "resolution": san.text(r.get("resolution"), 20),
    } for r in sorted(signals,
                      key=lambda r: -int(r.get("occurrences") or 0))[:50]]
    doc["database_health"] = counts
    return doc


def database_schema(errors_schema: list[dict],
                    trades_schema: list[dict],
                    trading_schema: list[dict] | None = None) -> dict:
    doc = _header(
        "database_schema",
        "Structure only (tables, columns, indexes). No rows, no file paths, "
        "no database contents.")
    doc["databases"] = [
        {"role": "errors", "tables": errors_schema},
        {"role": "failed_trades", "tables": trades_schema},
        {"role": "trading_ledger", "tables": trading_schema or []},
    ]
    return doc


#: authored fixtures — NOT recorded incidents. Each one describes inputs a
#: unit/integration test can build to reproduce a software behaviour.
REPRODUCTION_CASES = (
    {
        "id": "irt-sell-http-404",
        "title": "IRT market SELL answers HTTP 404",
        "category": "API_NETWORK",
        "inputs": {"market_section": "IRT", "action": "SELL",
                   "http_status": 404, "retry_count": 0},
        "expected": "The rejection is recorded as one deduplicated API_NETWORK "
                    "error, the cycle continues, and no retry loop exceeds the "
                    "configured backoff.",
    },
    {
        "id": "min-order-rejection",
        "title": "Order below the exchange minimum is rejected",
        "category": "MIN_ORDER",
        "inputs": {"notional_usdt": 0.2, "exchange_minimum_usdt": 0.5,
                   "market_section": "IRT"},
        "expected": "The local size gate refuses the order before submission "
                    "(no network call) and records MIN_ORDER once.",
    },
    {
        "id": "insufficient-balance",
        "title": "Sizing uses a stale free balance",
        "category": "INSUFFICIENT_BALANCE",
        "inputs": {"cached_free_cash": 100.0, "actual_free_cash": 0.0,
                   "intended_notional": 40.0},
        "expected": "The rejection is classified INSUFFICIENT_BALANCE and the "
                    "wallet is re-synced before the next sizing decision.",
    },
    {
        "id": "slippage-limit",
        "title": "Fill price exceeds the slippage budget",
        "category": "SLIPPAGE",
        "inputs": {"expected_price": 1000.0, "actual_price": 1012.0,
                   "max_slippage_fraction": 0.001},
        "expected": "A 120 bps deviation is refused or closed by the plan; the "
                    "attempt is recorded with the relative deviation only.",
    },
    {
        "id": "partial-fill",
        "title": "Order fills partially and the remainder is left resting",
        "category": "PARTIAL_FILL",
        "inputs": {"intended_amount": 10.0, "executed_amount": 4.0,
                   "order_status": "PARTIALLY_FILLED"},
        "expected": "The unfilled remainder is tracked explicitly (chase or "
                    "cancel), and the attempt carries a 0.4 fill ratio.",
    },
    {
        "id": "duplicate-order-guard",
        "title": "Duplicate client order id is rejected by the exchange",
        "category": "INVALID_ORDER",
        "inputs": {"same_client_ref": True, "attempts_in_cycle": 2},
        "expected": "The second submission is refused locally before reaching "
                    "the exchange; one INVALID_ORDER record at most.",
    },
    {
        "id": "ai-timeout",
        "title": "AI endpoint times out mid-cycle",
        "category": "AI",
        "inputs": {"timeout_s": 45, "response": None},
        "expected": "The cycle falls back to the deterministic heuristic, "
                    "records one AI warning with no key material, and trading "
                    "continues.",
    },
    {
        "id": "missing-candles",
        "title": "Candle store returns no frames for a market",
        "category": "DATA_QUALITY",
        "inputs": {"frames": [], "market": "EXAMPLEIRT"},
        "expected": "The market is skipped as DATA_QUALITY (not traded), and "
                    "the signal records the data age or the empty response.",
    },
    {
        "id": "market-isolation",
        "title": "IRT buy is funded with USDT-only cash",
        "category": "MARKET_SELECTION",
        "inputs": {"market_section": "IRT", "interaction": "independent",
                   "usdt_cash": 500.0, "irt_cash": 0.0},
        "expected": "The buy is blocked: sections stay independent unless the "
                    "interaction setting explicitly allows a transfer.",
    },
    {
        "id": "database-locked",
        "title": "SQLite reports 'database is locked'",
        "category": "DATABASE",
        "inputs": {"busy_timeout_ms": 10000, "concurrent_writer": True},
        "expected": "The write retries within the busy timeout, then records "
                    "one DATABASE error and drops the record instead of "
                    "stalling a trading thread.",
    },
    {
        "id": "rtc-export-privacy",
        "title": "Export pipeline sees a credential-shaped value",
        "category": "SYNC",
        "inputs": {"message": "request failed with token <redacted>",
                   "frames": [{"file": "api.py", "line": 10,
                               "function": "call"}]},
        "expected": "The value is redacted, the path becomes a base name, and "
                    "the export aborts instead of writing if validation still "
                    "finds a secret.",
    },
)


def reproduction_cases() -> dict:
    doc = _header(
        "reproduction_cases",
        "AUTHORED SYNTHETIC FIXTURES. They are not recorded incidents: they "
        "describe inputs a test can build to reproduce a software behaviour. "
        "Real observed incidents are the sanitized aggregates in the other "
        "reports.")
    doc["all_synthetic"] = True
    doc["source"] = "authored test fixtures"
    doc["cases"] = [dict(case, synthetic=True,
                         observed_incident=False,
                         assertion="Reproduce with a unit/integration test "
                                   "and assert the stated behaviour; never "
                                   "against a live account.")
                    for case in REPRODUCTION_CASES]
    return doc


# ------------------------------------------------------------------ report
def build_report(docs: dict[str, dict], counts: dict) -> str:
    errors = docs["error_summary"]
    trades = docs["failed_trade_summary"]
    lessons = docs["learning_lessons"]
    quality = docs["data_quality_report"]
    execution = docs["execution_metrics"]
    lines = [
        "# Diagnostics report (sanitized export)",
        "",
        "Generated by `python sync_rtc.py` from the private runtime databases.",
        "Aggregates and sanitized samples only: no credentials, no account",
        "identifiers, no wallet addresses, no balances, no absolute paths, and",
        "no raw databases. The private databases stay on the machine that runs",
        "the bot; this folder is the reviewable snapshot.",
        "",
        "## Error monitor",
        "",
        "| metric | value |",
        "| --- | --- |",
        "| distinct errors | %d |" % errors["totals"]["distinct_errors"],
        "| occurrences | %d |" % errors["totals"]["occurrences"],
        "| critical | %d |" % errors["totals"]["critical"],
        "| unresolved | %d |" % errors["totals"]["open"],
        "| repeated signatures | %d |" % errors["totals"]["repeated"],
        "",
    ]
    if errors["by_category"]:
        lines += ["| category | count |", "| --- | --- |"]
        lines += ["| %s | %d |" % (item["value"], item["count"])
                  for item in errors["by_category"]]
        lines.append("")
    if errors["recurring"]:
        lines += ["Most frequent signatures:", ""]
        for row in errors["recurring"][:10]:
            location = ("%s:%s" % (row["file"], row["line"])
                        if row["file"] else "?")
            lines.append("- `%s` **%s** %s (%d×) — %s"
                         % (row["category"], location, row["module"],
                            row["occurrences"], row["message"][:160]))
        lines.append("")

    lines += [
        "## Failed trade attempts",
        "",
        ("Attempts: **%d**. A losing trade is not a failed trade: a designed "
         "stop-loss exit is a normal outcome and is not counted here."
         % trades["totals"]["attempts"]),
        "",
    ]
    if trades["by_failure_category"]:
        lines += ["| failure category | count | share |", "| --- | --- | --- |"]
        lines += ["| %s | %d | %.1f%% |"
                  % (item["category"], item["count"], item["share"] * 100)
                  for item in trades["by_failure_category"]]
        lines.append("")
    if execution["slippage_bps_by_category"]:
        lines += ["Relative slippage (basis points, median):", ""]
        lines += ["- %s: %s bps over %d samples"
                  % (row["category"], row["median_bps"], row["samples"])
                  for row in execution["slippage_bps_by_category"]]
        lines.append("")

    lines += [
        "## Lessons",
        "",
        "%d lesson(s): %s."
        % (lessons["totals"]["lessons"],
           ", ".join("%s=%d" % (k, v) for k, v
                     in sorted(lessons["totals"]["by_status"].items()))
           or "none"),
        "Every lesson is a proposal. Nothing changes strategy parameters,",
        "risk limits or code until a human validates it with a test.",
        "",
        "## Data quality",
        "",
        "%d signal(s) in %s."
        % (quality["signals_total"],
           ", ".join(item["value"] for item in quality["by_category"])
           or "no category"),
        "Database health: %s."
        % ", ".join("%s=%s" % (k, v)
                    for k, v in sorted(quality["database_health"].items())),
        "",
        "## Reproduction cases",
        "",
        ("`reproduction_cases.json` holds %d authored synthetic fixtures — "
         "not recorded incidents. Each one states inputs and the behaviour a "
         "test must assert." % len(docs["reproduction_cases"]["cases"])),
        "",
        "## Export policy",
        "",
        "- fields are chosen from an explicit allowlist, never `SELECT *`",
        "- identifiers are pseudonymized with a per-export salt",
        "- amounts are relative (ratios, basis points, shares)",
        "- stack traces become file/line/function only",
        "- the document is validated before it is written; a violation",
        "  aborts the export instead of producing a leaky report",
        "- no raw `.db`, `-wal`, `-shm` or `-journal` file is ever copied",
        "",
        "Records scanned: %s."
        % ", ".join("%s=%s" % (k, v) for k, v in sorted(counts.items())),
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------- entry
def export_all(diagnostics_dir: str | Path, rtc_dir: str | Path, *,
               project_root: str | Path | None = None,
               secrets: Iterable[str] = (),
               trading_db: str | Path | None = None) -> dict:
    """Write the sanitized reports into ``rtc_dir``.

    Raises :class:`PrivacyError` (and writes nothing further) if validation
    fails, so a leak can never be committed by accident.
    """
    diag_dir = Path(diagnostics_dir)
    target = Path(rtc_dir)
    target.mkdir(parents=True, exist_ok=True)
    san = ExportSanitizer(secrets, project_root)

    errors_conn = _connect(diag_dir / ERROR_DB_NAME)
    trades_conn = _connect(diag_dir / FAILED_TRADE_DB_NAME)
    try:
        errors = [select_fields(row, ERROR_FIELDS) for row in
                  _rows(errors_conn, "SELECT * FROM errors ORDER BY last_seen")]
        trades = [select_fields(row, TRADE_FIELDS) for row in
                  _rows(trades_conn, "SELECT * FROM failed_trades "
                                     "ORDER BY timestamp")]
        lessons = [select_fields(row, LESSON_FIELDS) for row in
                   _rows(trades_conn, "SELECT * FROM lessons "
                                      "ORDER BY occurrences DESC")]
        errors_schema = _schema(errors_conn)
        trades_schema = _schema(trades_conn)
        quick_check = "missing"
        if errors_conn is not None:
            check = _rows(errors_conn, "PRAGMA quick_check(1)")
            quick_check = (check[0].get("quick_check") if check else "unknown")
    finally:
        for conn in (errors_conn, trades_conn):
            try:
                if conn is not None:
                    conn.close()
            except sqlite3.Error:
                pass

    trading_conn = None
    trading_schema: list[dict] = []
    if trading_db:
        trading_conn = _connect(Path(trading_db))
    try:
        trading_schema = _schema(trading_conn)
    finally:
        if trading_conn is not None:
            trading_conn.close()

    counts = {
        "errors": len(errors),
        "failed_trades": len(trades),
        "lessons": len(lessons),
    }
    health = {
        "error_db_present": (diag_dir / ERROR_DB_NAME).exists(),
        "failed_trade_db_present": (diag_dir / FAILED_TRADE_DB_NAME).exists(),
        "quick_check": quick_check,
        "errors": len(errors),
        "failed_trades": len(trades),
    }
    docs = {
        "error_summary": error_summary(san, errors),
        "failed_trade_summary": failed_trade_summary(san, trades),
        "learning_lessons": learning_lessons(san, lessons),
        "execution_metrics": execution_metrics(san, trades),
        "strategy_metrics": strategy_metrics(san, trades),
        "data_quality_report": data_quality_report(san, errors, health),
        "database_schema": database_schema(errors_schema, trades_schema,
                                          trading_schema),
        "reproduction_cases": reproduction_cases(),
    }

    # validate EVERY document first: a violation must leave no partial export
    payloads = {name: docs[name[:-5]] for name in JSON_FILES}
    for name, payload in payloads.items():
        validate_payload(payload, secrets=secrets, label=name)
    report = build_report(docs, counts)
    validate_text(report, secrets=secrets, label=REPORT_FILE)

    written: list[str] = []
    for name, payload in payloads.items():
        path = target / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        written.append(str(path))
    path = target / REPORT_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(report, encoding="utf-8")
    os.replace(tmp, path)
    written.append(str(path))

    return {"ok": True, "dir": str(target), "files": written,
            "counts": counts}
