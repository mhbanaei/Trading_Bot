"""Structured error monitoring and failed-trade intelligence.

Two SQLite databases, both created and migrated automatically:

``<data_dir>/error.db``
    Every application error worth investigating: exchange/API failures,
    execution failures, market-data problems, AI errors, risk-engine errors,
    database faults, background-task crashes and unexpected exceptions.
    Identical errors are DEDUPLICATED into one row with an occurrence
    counter, so a broken network cannot grow the file without bound.

``<data_dir>/failed_trade.db``
    Unsuccessful *executions*: rejected orders, invalid orders, insufficient
    balance, exchange minimums, liquidity/slippage problems, partial fills.
    A trade that LOST MONEY while executing exactly as designed is NOT a
    failed trade and is never recorded here (that is a strategy outcome and
    lives in the ``performance`` table).

Both files are PRIVATE runtime data: they live on the machine that runs the
bot and are never copied into ``RTC/``. The commit-ready snapshot contains
sanitized JSON/Markdown reports instead, produced by a separate process
(``python sync_rtc.py`` -> :mod:`nobitex_bot.rtc_export`).

Design rules:

* Logging never crashes the engine. Every write is inside ``try/except``
  and, by default, happens on a bounded background queue.
* Secrets never reach the database: messages, stack traces and context are
  passed through :class:`Redactor` first.
* Nothing is deleted except by the retention policy (oldest rows beyond
  ``max_errors`` / ``max_failed_trades`` or older than ``retention_days``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Iterable

from .utils import utc_now_iso

ERROR_DB_NAME = "error.db"
FAILED_TRADE_DB_NAME = "failed_trade.db"

#: categories used by the callers (free text is accepted too, but keeping
#: this list close to the code makes the dashboard filters predictable)
ERROR_CATEGORIES = (
    "EXCHANGE_API", "EXECUTION", "MARKET_DATA", "AI", "ANALYSIS", "RISK",
    "DATABASE", "WORKER", "SCHEDULER", "DASHBOARD", "CONFIG", "SYNC",
    "RTC", "UNKNOWN",
)

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: the rest of the codebase talks in HIGH/MEDIUM/LOW; store one scale so the
#: dashboard filters cannot miss a record because of a spelling difference
_SEVERITY_ALIASES = {"WARN": "WARNING", "MEDIUM": "WARNING",
                     "HIGH": "ERROR", "LOW": "INFO", "FATAL": "CRITICAL"}

#: how a failed attempt is classified (requirement section B)
FAILURE_CATEGORIES = (
    "API_NETWORK", "INVALID_ORDER", "INSUFFICIENT_BALANCE", "MIN_ORDER",
    "LIQUIDITY", "SLIPPAGE", "PARTIAL_FILL", "STRATEGY", "MARKET_SELECTION",
    "AI_DECISION", "DATA_QUALITY", "EXCHANGE_BEHAVIOR", "UNKNOWN",
)

RESOLUTIONS = ("open", "investigating", "resolved", "wontfix", "regressed")

#: text -> failure category. First match wins, so order matters.
_FAILURE_HINTS = (
    ("INSUFFICIENT_BALANCE", (
        "insufficient", "balance", "موجودی", "not enough", "no funds",
        "insufficientcash", "insufficient_cash")),
    ("MIN_ORDER", (
        "smallorder", "small order", "minimum", "min order", "below min",
        "حداقل سفارش", "belownotnotional", "below_min_notional")),
    ("INVALID_ORDER", (
        "invalid clientorderid", "invalid order", "invalidorder",
        "bad request", "unprocessable", "order value", "validation")),
    ("LIQUIDITY", (
        "liquidity", "no bid", "no ask", "empty book", "unmatched",
        "not filled", "notfilled", "پر نشد", "orderbook", "depth")),
    ("SLIPPAGE", ("slippage", "price moved", "stale price", "cross")),
    ("PARTIAL_FILL", ("partial", "partially", "بخشی")),
    ("DATA_QUALITY", (
        "price unavailable", "price_unavailable", "no price", "stale",
        "quality", "candle", "data unavailable", "قیمت")),
    ("MARKET_SELECTION", (
        "unknown market", "unknown_market", "market not", "not tradable",
        "symbol", "بازار")),
    ("AI_DECISION", ("ai ", "ai_", "decision", "confidence", "committee")),
    ("API_NETWORK", (
        "network", "timeout", "timed out", "connection", "tls", "ssl",
        "rate limit", "rate_limit", "429", "502", "503", "504", "dns")),
)

SECRET_PATTERNS = (
    re.compile(r"freellmapi-[0-9a-fA-F]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{25,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # "password": "hunter2" / token=abc  -> keep the key, drop the value
    re.compile(r"(?i)(password|passwd|token|api[_-]?key|secret|otp)"
               r"(\"?\s*[:=]\s*\"?)([^\s\",}]{4,})"),
)

REDACTED = "***"


def classify_failure(text: str) -> str:
    """Map a free-form rejection/exception message to a failure category."""
    low = (text or "").lower()
    if not low:
        return "UNKNOWN"
    for category, needles in _FAILURE_HINTS:
        if any(n in low for n in needles):
            return category
    return "UNKNOWN"


class Redactor:
    """Removes credential values from anything before it is persisted.

    ``secrets`` are the CONCRETE values this process knows (config keys,
    dashboard password, …). They are used only for matching and are never
    stored. Shape-based patterns catch tokens this process did not load.
    """

    def __init__(self, secrets: Iterable[str] | None = None):
        cleaned = [s for s in (secrets or [])
                   if isinstance(s, str) and len(s) >= 6]
        # longest first so a value containing another is fully masked
        self._values = sorted(set(cleaned), key=len, reverse=True)

    def text(self, value: Any, limit: int = 8000) -> str:
        if value is None:
            return ""
        try:
            out = value if isinstance(value, str) else str(value)
        except Exception:
            return ""
        for secret in self._values:
            out = out.replace(secret, REDACTED)
        for pattern in SECRET_PATTERNS:
            try:
                if pattern.groups >= 2 and pattern.pattern.startswith("(?i)"):
                    out = pattern.sub(
                        lambda m: m.group(1) + m.group(2) + REDACTED, out)
                else:
                    out = pattern.sub(REDACTED, out)
            except (re.error, IndexError):
                continue
        if limit and len(out) > limit:
            out = out[:limit] + "…[truncated]"
        return out


def _fingerprint(category: str, module: str, exc_type: str,
                 message: str) -> str:
    """Stable dedup key: category + module + exception type + normalized text.

    Numbers and hex ids are collapsed so «order 123 rejected» and «order 456
    rejected» count as the same recurring problem instead of two rows.
    """
    norm = re.sub(r"\b[0-9a-fA-F]{8,}\b", "<id>", message or "")
    norm = re.sub(r"\b\d+\b", "<n>", norm)
    norm = re.sub(r"\s+", " ", norm).strip().lower()[:400]
    raw = "|".join((category or "", module or "", exc_type or "", norm))
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:20]


ERRORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS errors (
    error_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'ERROR',
    module TEXT,
    file_name TEXT,
    function_name TEXT,
    line_number INTEGER,
    exception_type TEXT,
    message TEXT NOT NULL,
    stack_trace TEXT,
    symbol TEXT,
    market_section TEXT,
    order_id TEXT,
    trade_id TEXT,
    cycle_id TEXT,
    context TEXT,
    recovered INTEGER NOT NULL DEFAULT 0,
    recovery_action TEXT,
    resolution TEXT NOT NULL DEFAULT 'open',
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_errors_last ON errors(last_seen);
CREATE INDEX IF NOT EXISTS idx_errors_cat ON errors(category);
CREATE INDEX IF NOT EXISTS idx_errors_res ON errors(resolution);
CREATE INDEX IF NOT EXISTS idx_errors_sev ON errors(severity);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

FAILED_SCHEMA = """
CREATE TABLE IF NOT EXISTS failed_trades (
    attempt_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    symbol TEXT,
    market TEXT,
    market_section TEXT,
    intended_action TEXT,
    execution_mode TEXT,
    strategy TEXT,
    ai_recommendation TEXT,
    decision_id TEXT,
    risk_decision_id TEXT,
    order_id TEXT,
    trade_id TEXT,
    failure_category TEXT NOT NULL,
    order_status TEXT,
    exchange_response TEXT,
    failure_reason TEXT,
    error_id TEXT,
    market_conditions TEXT,
    candles TEXT,
    indicators TEXT,
    orderbook TEXT,
    expected_price TEXT,
    actual_price TEXT,
    intended_amount TEXT,
    executed_amount TEXT,
    fees TEXT,
    slippage TEXT,
    portfolio_before TEXT,
    portfolio_after TEXT,
    outcome TEXT NOT NULL DEFAULT 'failed',
    is_rejected INTEGER NOT NULL DEFAULT 0,
    is_partial INTEGER NOT NULL DEFAULT 0,
    is_cancelled INTEGER NOT NULL DEFAULT 0,
    is_failed INTEGER NOT NULL DEFAULT 1,
    db_synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ft_time ON failed_trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_ft_cat ON failed_trades(failure_category);
CREATE INDEX IF NOT EXISTS idx_ft_sym ON failed_trades(symbol);
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id TEXT PRIMARY KEY,
    pattern_key TEXT NOT NULL UNIQUE,
    related_error_ids TEXT,
    related_failed_trade_ids TEXT,
    pattern TEXT NOT NULL,
    root_cause TEXT,
    evidence TEXT,
    occurrences INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    recommended_correction TEXT,
    verification_status TEXT NOT NULL DEFAULT 'proposed',
    ai_explanation TEXT,
    created_at TEXT NOT NULL,
    last_reviewed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_reports (
    report_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    engine TEXT,
    model TEXT,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class _Db:
    """One SQLite file with a lock and a bounded retry on a busy lock."""

    def __init__(self, path: Path, schema: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=10.0,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=10000")
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self._conn.executescript(schema)
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.ProgrammingError:   # closed connection
                return []

    def backup_to(self, dest: Path) -> None:
        """Transaction-safe copy of this database to ``dest``.

        Writes a temporary file next to the destination and swaps it in, so
        a reader never sees a half-written database. ``journal_mode=DELETE``
        on the copy keeps the backup a single file (no -wal sidecar).

        Destination is a PRIVATE directory chosen by the caller; this method
        is for local backups only and is never used to fill ``RTC/``.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".sync-tmp")
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        with self._lock:
            out = sqlite3.connect(str(tmp))
            try:
                self._conn.backup(out)
                out.execute("PRAGMA journal_mode=DELETE")
                out.commit()
            finally:
                out.close()
            try:
                os.replace(tmp, dest)
                return
            except OSError:
                # Windows refuses to replace a database another process (or
                # an RTC dashboard reading it) has open. Fall back to the
                # backup API straight into the file: SQLite coordinates the
                # lock and every reader keeps seeing a consistent database.
                try:
                    tmp.unlink()
                except OSError:
                    pass
            direct = sqlite3.connect(str(dest), timeout=10.0)
            try:
                self._conn.backup(direct)
                direct.execute("PRAGMA journal_mode=DELETE")
                direct.commit()
            finally:
                direct.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


class Diagnostics:
    """Central error monitor + failed-trade ledger (thread-safe)."""

    def __init__(self, data_dir: str | Path, *,
                 retention_days: float = 30.0, max_errors: int = 20000,
                 max_failed_trades: int = 20000,
                 background: bool = True,
                 secrets: Iterable[str] | None = None,
                 log_dir: str | Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = float(retention_days or 0)
        self.max_errors = max(0, int(max_errors or 0))
        self.max_failed_trades = max(0, int(max_failed_trades or 0))
        self.redactor = Redactor(secrets)
        self.log_dir = Path(log_dir) if log_dir else self.data_dir / "logs"
        self.dropped_writes = 0
        self._closed = False
        self._errors = _Db(self.data_dir / ERROR_DB_NAME, ERRORS_SCHEMA)
        self._trades = _Db(self.data_dir / FAILED_TRADE_DB_NAME, FAILED_SCHEMA)
        self._queue: queue.Queue | None = None
        self._worker: threading.Thread | None = None
        if background:
            self._queue = queue.Queue(maxsize=5000)
            self._worker = threading.Thread(target=self._drain, daemon=True,
                                            name="diagnostics-writer")
            self._worker.start()

    # ------------------------------------------------------------ plumbing
    def _submit(self, job) -> None:
        """Queue a write; never raise and never block a trading thread."""
        if self._closed:
            return
        if self._queue is None:
            try:
                job()
            except Exception:
                self.dropped_writes += 1
            return
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            # bounded storage: a storm must not stall execution. The count
            # is surfaced in summary() so the loss is visible, not silent.
            self.dropped_writes += 1

    def _drain(self) -> None:
        while True:
            job = self._queue.get() if self._queue else None
            if job is None:
                return
            try:
                job()
            except Exception:
                self.dropped_writes += 1
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for queued writes (tests and shutdown). True when drained."""
        if self._queue is None:
            return True
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return self._queue.unfinished_tasks == 0

    def close(self) -> None:
        if self._closed:
            return
        self.flush(5.0)
        self._closed = True
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        self._errors.close()
        self._trades.close()

    # ---------------------------------------------------------- error write
    def record_error(self, category: str = "UNKNOWN", severity: str = "ERROR",
                     message: str = "", *, module: str = "", exc: BaseException | None = None,
                     symbol: str = "", market_section: str = "", order_id: str = "",
                     trade_id: str = "", cycle_id: str = "",
                     context: dict | None = None, recovered: bool = False,
                     recovery_action: str = "", resolution: str = "open") -> str:
        """Persist one (deduplicated) error. Returns its ``error_id``.

        Safe to call from any thread, including inside ``except`` blocks.
        """
        if exc is not None:
            tb = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
            exc_type = type(exc).__name__
            if not message:
                message = str(exc)
        else:
            frame = sys._getframe(1)
            tb = "".join(traceback.format_stack(frame)[-3:])
            exc_type = ""
        message = self.redactor.text(message, 2000) or "(no message)"
        tb_text = self.redactor.text(tb, 12000)
        category = (category or "UNKNOWN").upper()
        severity = _SEVERITY_ALIASES.get((severity or "").upper(),
                                         (severity or "ERROR").upper())
        if severity not in SEVERITIES:
            severity = "ERROR"
        resolution = resolution if resolution in RESOLUTIONS else "open"
        # call-site info: the caller of record_error (or the exception's own
        # last frame when one was handed in, which is more useful)
        file_name = function_name = ""
        line_number = 0
        if exc is not None and exc.__traceback__ is not None:
            frames = traceback.extract_tb(exc.__traceback__)
            if frames:
                file_name = os.path.basename(frames[-1].filename)
                function_name = frames[-1].name
                line_number = int(frames[-1].lineno or 0)
        if not file_name:
            try:
                frame = sys._getframe(1)
                file_name = os.path.basename(frame.f_code.co_filename)
                function_name = frame.f_code.co_name
                line_number = int(frame.f_lineno or 0)
            except Exception:
                pass
        module = module or file_name.rsplit(".", 1)[0] or category.lower()
        safe_context = self._safe_json(context)
        fp = _fingerprint(category, module, exc_type, message)
        # deterministic id: a repeated identical error keeps the SAME id, so
        # callers (and failed_trade.error_id links) stay valid across calls
        error_id = "err_" + fp
        now = utc_now_iso()

        def job() -> None:
            rows = self._errors.query(
                "SELECT error_id, severity FROM errors WHERE fingerprint=?",
                (fp,))
            if rows:
                existing_id, existing_sev = rows[0]
                # escalate, never silently downgrade a critical record
                sev = existing_sev if SEVERITIES.index(
                    existing_sev) >= SEVERITIES.index(severity) else severity
                self._errors.execute(
                    "UPDATE errors SET last_seen=?, occurrences=occurrences+1, "
                    "severity=?, message=?, stack_trace=?, context=?, "
                    "recovered=?, recovery_action=?, "
                    "resolution=CASE WHEN resolution='resolved' "
                    "THEN 'regressed' ELSE resolution END WHERE error_id=?",
                    (now, sev, message, tb_text, safe_context,
                     1 if recovered else 0, recovery_action, existing_id))
                self._write_human_log(category, severity, message, tb_text)
                return
            self._errors.execute(
                "INSERT INTO errors (error_id, fingerprint, first_seen, "
                "last_seen, occurrences, category, severity, module, file_name, "
                "function_name, line_number, exception_type, message, "
                "stack_trace, symbol, market_section, order_id, trade_id, "
                "cycle_id, context, recovered, recovery_action, resolution) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (error_id, fp, now, now, 1, category, severity, module,
                 file_name, function_name, line_number, exc_type, message,
                 tb_text, symbol, market_section, order_id, trade_id,
                 cycle_id, safe_context, 1 if recovered else 0,
                 recovery_action, resolution))
            self._write_human_log(category, severity, message, tb_text)

        self._submit(job)
        return error_id

    def _write_human_log(self, category: str, severity: str, message: str,
                         tb_text: str) -> None:
        """Human-readable companion log (SQLite stays the source of truth)."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = utc_now_iso()
            with open(self.log_dir / "errors.log", "a", encoding="utf-8") as fh:
                fh.write("%s [%s] %s: %s\n" % (stamp, severity, category,
                                               message.replace("\n", " ")))
                if severity in ("ERROR", "CRITICAL") and tb_text:
                    fh.write(tb_text.rstrip() + "\n")
        except OSError:
            pass

    # --------------------------------------------------- failed trade write
    def record_failed_trade(self, *, symbol: str, intended_action: str,
                            failure_category: str = "", failure_reason: str = "",
                            market: str = "", execution_mode: str = "paper",
                            strategy: str = "", ai_recommendation: Any = None,
                            decision_id: str = "", risk_decision_id: str = "",
                            order_id: str = "", trade_id: str = "",
                            order_status: str = "", exchange_response: Any = None,
                            error_id: str = "", market_conditions: Any = None,
                            candles: Any = None, indicators: Any = None,
                            orderbook: Any = None, expected_price: Any = None,
                            actual_price: Any = None, intended_amount: Any = None,
                            executed_amount: Any = None, fees: Any = None,
                            slippage: Any = None, portfolio_before: Any = None,
                            portfolio_after: Any = None,
                            outcome: str = "failed",
                            timestamp: str = "") -> str:
        """Record one unsuccessful trade ATTEMPT (never a normal loss).

        ``outcome`` is one of rejected / partially_filled / cancelled /
        failed / unfilled. Missing execution data stays NULL — it is never
        invented.
        """
        attempt_id = "ft_" + uuid.uuid4().hex[:16]
        category = (failure_category or "").upper()
        if category not in FAILURE_CATEGORIES:
            category = classify_failure("%s %s" % (failure_reason,
                                                   exchange_response))
        symbol = (symbol or "").upper()
        section = ("USDT" if symbol.endswith("USDT") else
                   "IRT" if symbol.endswith("IRT") else "")
        outcome = (outcome or "failed").lower()
        flags = {
            "is_rejected": 1 if outcome == "rejected" else 0,
            "is_partial": 1 if outcome == "partially_filled" else 0,
            "is_cancelled": 1 if outcome == "cancelled" else 0,
            "is_failed": 1 if outcome in ("failed", "unfilled") else 0,
        }
        ts = timestamp or utc_now_iso()

        def job() -> None:
            self._trades.execute(
                "INSERT OR IGNORE INTO failed_trades (attempt_id, timestamp, "
                "symbol, market, market_section, intended_action, "
                "execution_mode, strategy, ai_recommendation, decision_id, "
                "risk_decision_id, order_id, trade_id, failure_category, "
                "order_status, exchange_response, failure_reason, error_id, "
                "market_conditions, candles, indicators, orderbook, "
                "expected_price, actual_price, intended_amount, "
                "executed_amount, fees, slippage, portfolio_before, "
                "portfolio_after, outcome, is_rejected, is_partial, "
                "is_cancelled, is_failed) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?)",
                (attempt_id, ts, symbol, market or symbol, section,
                 (intended_action or "").upper(), execution_mode, strategy,
                 self._safe_json(ai_recommendation), decision_id,
                 risk_decision_id, order_id, trade_id, category, order_status,
                 self._safe_json(exchange_response),
                 self.redactor.text(failure_reason, 2000), error_id,
                 self._safe_json(market_conditions), self._safe_json(candles),
                 self._safe_json(indicators), self._safe_json(orderbook),
                 self._num(expected_price), self._num(actual_price),
                 self._num(intended_amount), self._num(executed_amount),
                 self._num(fees), self._num(slippage),
                 self._safe_json(portfolio_before),
                 self._safe_json(portfolio_after), outcome,
                 flags["is_rejected"], flags["is_partial"],
                 flags["is_cancelled"], flags["is_failed"]))
            self._trades.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES "
                "('last_failed_trade_at', ?)", (ts,))

        self._submit(job)
        return attempt_id

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _num(value) -> str | None:
        if value is None or value == "":
            return None
        try:
            return str(value)
        except Exception:
            return None

    def _safe_json(self, value) -> str | None:
        if value is None:
            return None
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = self.redactor.text(value, 4000)
        return self.redactor.text(text, 8000)

    # ------------------------------------------------------------- queries
    def errors(self, limit: int = 100, *, category: str = "",
               severity: str = "", symbol: str = "", resolution: str = "",
               since: str = "", search: str = "") -> list[dict]:
        sql = ("SELECT error_id, first_seen, last_seen, occurrences, category, "
               "severity, module, file_name, function_name, line_number, "
               "exception_type, message, symbol, market_section, order_id, "
               "trade_id, cycle_id, recovered, recovery_action, resolution "
               "FROM errors WHERE 1=1")
        params: list = []
        if category:
            sql += " AND category=?"
            params.append(category.upper())
        if severity:
            sql += " AND severity=?"
            params.append(severity.upper())
        if resolution:
            sql += " AND resolution=?"
            params.append(resolution.lower())
        if symbol:
            sql += " AND symbol LIKE ?"
            params.append(symbol.upper() + "%")
        if since:
            sql += " AND last_seen>=?"
            params.append(since)
        if search:
            sql += " AND (message LIKE ? OR module LIKE ?)"
            params += ["%" + search + "%"] * 2
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(max(1, int(limit)))
        cols = ["error_id", "first_seen", "last_seen", "occurrences",
                "category", "severity", "module", "file_name",
                "function_name", "line_number", "exception_type", "message",
                "symbol", "market_section", "order_id", "trade_id",
                "cycle_id", "recovered", "recovery_action", "resolution"]
        return [dict(zip(cols, row)) for row in self._errors.query(sql, tuple(params))]

    def error(self, error_id: str) -> dict | None:
        """Full record — including the stack trace. Callers must be
        authenticated administrators (the dashboard handler is)."""
        rows = self._rows("errors", self._errors,
                          "SELECT * FROM errors WHERE error_id=?", (error_id,))
        return rows[0] if rows else None

    def resolve_error(self, error_id: str, resolution: str = "resolved") -> bool:
        if resolution not in RESOLUTIONS:
            return False
        now = utc_now_iso()
        cur = self._errors.execute(
            "UPDATE errors SET resolution=?, resolved_at=? WHERE error_id=?",
            (resolution, None if resolution == "open" else now, error_id))
        return cur.rowcount > 0

    def failed_trades(self, limit: int = 100, *, category: str = "",
                      symbol: str = "", section: str = "",
                      strategy: str = "", since: str = "") -> list[dict]:
        sql = "SELECT * FROM failed_trades WHERE 1=1"
        params: list = []
        if category:
            sql += " AND failure_category=?"
            params.append(category.upper())
        if strategy:
            sql += " AND strategy LIKE ?"
            params.append("%" + strategy + "%")
        if symbol:
            sql += " AND symbol LIKE ?"
            params.append(symbol.upper() + "%")
        if section:
            sql += " AND market_section=?"
            params.append(section.upper())
        if since:
            sql += " AND timestamp>=?"
            params.append(since)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return self._rows("failed_trades", self._trades, sql, tuple(params))

    @staticmethod
    def _rows(_table: str, db: _Db, sql: str, params: tuple) -> list[dict]:
        with db._lock:
            try:
                cur = db._conn.execute(sql, params)
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            except sqlite3.ProgrammingError:   # closed connection
                return []

    def summary(self) -> dict:
        """Counts the dashboard needs (single round trip, no heavy scans)."""
        def scalar(sql: str, params: tuple = ()) -> int:
            rows = self._errors.query(sql, params)
            return int(rows[0][0] or 0) if rows else 0

        errors_total = scalar("SELECT COUNT(*) FROM errors")
        occ = scalar("SELECT COALESCE(SUM(occurrences),0) FROM errors")
        critical = scalar("SELECT COUNT(*) FROM errors WHERE severity='CRITICAL'")
        unresolved = scalar("SELECT COUNT(*) FROM errors "
                            "WHERE resolution IN ('open','investigating','regressed')")
        repeated = scalar("SELECT COUNT(*) FROM errors WHERE occurrences>1")
        by_category = [{"category": r[0], "count": r[1]}
                       for r in self._errors.query(
                           "SELECT category, COUNT(*) FROM errors "
                           "GROUP BY category ORDER BY COUNT(*) DESC")]
        by_severity = [{"severity": r[0], "count": r[1]}
                       for r in self._errors.query(
                           "SELECT severity, COUNT(*) FROM errors "
                           "GROUP BY severity ORDER BY COUNT(*) DESC")]
        trows = self._trades.query(
            "SELECT failure_category, COUNT(*) FROM failed_trades "
            "GROUP BY failure_category ORDER BY COUNT(*) DESC")
        trades_total = sum(int(r[1] or 0) for r in trows)
        lesson_rows = self._trades.query(
            "SELECT COUNT(*), SUM(verification_status='verified') FROM lessons")
        lessons_total = int((lesson_rows[0][0] if lesson_rows else 0) or 0)
        lessons_verified = int((lesson_rows[0][1] if lesson_rows else 0) or 0)
        return {
            "errors_total": errors_total,
            "error_occurrences": occ,
            "critical_errors": critical,
            "unresolved_errors": unresolved,
            "repeated_errors": repeated,
            "by_category": by_category,
            "by_severity": by_severity,
            "failed_trades_total": trades_total,
            "failed_by_category": [{"category": r[0], "count": int(r[1] or 0)}
                                   for r in trows],
            "lessons_total": lessons_total,
            "lessons_verified": lessons_verified,
            "dropped_writes": self.dropped_writes,
            "error_db": str(self._errors.path),
            "failed_trade_db": str(self._trades.path),
        }

    @staticmethod
    def _meta(db: _Db, key: str) -> str:
        rows = db.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else ""

    def _set_meta(self, db: _Db, key: str, value: str) -> None:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                   (key, value))

    # --------------------------------------------------------- learning IO
    def lessons(self, limit: int = 100, *, status: str = "") -> list[dict]:
        sql = "SELECT * FROM lessons"
        params: list = []
        if status:
            sql += " WHERE verification_status=?"
            params.append(status)
        sql += " ORDER BY occurrences DESC, last_reviewed DESC LIMIT ?"
        params.append(max(1, int(limit)))
        out = []
        for row in self._rows("lessons", self._trades, sql, tuple(params)):
            for key in ("related_error_ids", "related_failed_trade_ids",
                        "evidence"):
                try:
                    row[key] = json.loads(row[key]) if row.get(key) else []
                except (TypeError, ValueError):
                    pass
            out.append(row)
        return out

    def save_lesson(self, *, pattern_key: str, pattern: str,
                    root_cause: str = "", evidence: Any = None,
                    occurrences: int = 0, confidence: float = 0.0,
                    recommended_correction: str = "",
                    related_error_ids: list | None = None,
                    related_failed_trade_ids: list | None = None,
                    ai_explanation: str = "") -> str:
        """Upsert by ``pattern_key`` — history is never duplicated or lost.

        ``verification_status`` is only ever *proposed* from analysis: a
        lesson becomes ``verified`` only through an explicit, tested change,
        so nothing can claim the bot "learned" on its own.
        """
        now = utc_now_iso()
        rows = self._trades.query(
            "SELECT lesson_id, created_at FROM lessons WHERE pattern_key=?",
            (pattern_key,))
        if rows:
            lesson_id = rows[0][0]
            self._trades.execute(
                "UPDATE lessons SET pattern=?, root_cause=?, evidence=?, "
                "occurrences=?, confidence=?, recommended_correction=?, "
                "related_error_ids=?, related_failed_trade_ids=?, "
                "ai_explanation=CASE WHEN ?='' THEN ai_explanation ELSE ? END, "
                "last_reviewed=? WHERE lesson_id=?",
                (pattern, root_cause, self._safe_json(evidence),
                 int(occurrences), float(confidence),
                 recommended_correction,
                 self._safe_json(related_error_ids or []),
                 self._safe_json(related_failed_trade_ids or []),
                 ai_explanation, ai_explanation, now, lesson_id))
            return lesson_id
        lesson_id = "lsn_" + uuid.uuid4().hex[:16]
        self._trades.execute(
            "INSERT INTO lessons (lesson_id, pattern_key, related_error_ids, "
            "related_failed_trade_ids, pattern, root_cause, evidence, "
            "occurrences, confidence, recommended_correction, "
            "verification_status, ai_explanation, created_at, last_reviewed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'proposed',?,?,?)",
            (lesson_id, pattern_key, self._safe_json(related_error_ids or []),
             self._safe_json(related_failed_trade_ids or []), pattern,
             root_cause, self._safe_json(evidence), int(occurrences),
             float(confidence), recommended_correction, ai_explanation,
             now, now))
        return lesson_id

    def set_lesson_status(self, lesson_id: str, status: str) -> bool:
        if status not in ("proposed", "verified", "rejected", "regressed"):
            return False
        cur = self._trades.execute(
            "UPDATE lessons SET verification_status=?, last_reviewed=? "
            "WHERE lesson_id=?", (status, utc_now_iso(), lesson_id))
        return cur.rowcount > 0

    def save_learning_report(self, body: str, engine: str = "",
                             model: str = "") -> str:
        report_id = "rep_" + uuid.uuid4().hex[:16]
        self._trades.execute(
            "INSERT INTO learning_reports (report_id, created_at, engine, "
            "model, body) VALUES (?,?,?,?,?)",
            (report_id, utc_now_iso(), engine, model,
             self.redactor.text(body, 20000)))
        return report_id

    def learning_reports(self, limit: int = 10) -> list[dict]:
        return self._rows(
            "learning_reports", self._trades,
            "SELECT * FROM learning_reports ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),))

    # ----------------------------------------------------------- retention
    def prune(self) -> dict:
        """Bounded storage: oldest rows beyond the caps are removed.

        Only diagnostic rows are affected. Order/fill/position history in
        ``trading_bot.db`` is untouched, and a *resolved* error is kept as
        long as any other row so the lesson trail survives.
        """
        removed = {"errors": 0, "failed_trades": 0}
        cutoff = ""
        if self.retention_days > 0:
            cutoff = utc_now_iso()
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(
                days=self.retention_days)).isoformat().replace("+00:00", "Z")
        if self.max_errors > 0:
            total = int(self._errors.query(
                "SELECT COUNT(*) FROM errors")[0][0] or 0)
            excess = total - self.max_errors
            if excess > 0:
                self._errors.execute(
                    "DELETE FROM errors WHERE error_id IN "
                    "(SELECT error_id FROM errors ORDER BY last_seen ASC "
                    "LIMIT ?)", (excess,))
                removed["errors"] += excess
        if cutoff:
            before = int(self._errors.query(
                "SELECT COUNT(*) FROM errors")[0][0] or 0)
            self._errors.execute("DELETE FROM errors WHERE last_seen<?",
                                 (cutoff,))
            removed["errors"] += before - int(self._errors.query(
                "SELECT COUNT(*) FROM errors")[0][0] or 0)
        if self.max_failed_trades > 0:
            total = int(self._trades.query(
                "SELECT COUNT(*) FROM failed_trades")[0][0] or 0)
            excess = total - self.max_failed_trades
            if excess > 0:
                self._trades.execute(
                    "DELETE FROM failed_trades WHERE attempt_id IN "
                    "(SELECT attempt_id FROM failed_trades ORDER BY timestamp "
                    "ASC LIMIT ?)", (excess,))
                removed["failed_trades"] += excess
        if cutoff:
            before = int(self._trades.query(
                "SELECT COUNT(*) FROM failed_trades")[0][0] or 0)
            self._trades.execute("DELETE FROM failed_trades WHERE timestamp<?",
                                 (cutoff,))
            removed["failed_trades"] += before - int(self._trades.query(
                "SELECT COUNT(*) FROM failed_trades")[0][0] or 0)
        return removed

    # -------------------------------------------------------- local backup
    def backup_local(self, dest_dir: str | Path) -> dict:
        """Consistent LOCAL backup of both private databases.

        For an operator's own copies only. It is deliberately not connected
        to the commit-ready snapshot: copying a raw database into ``RTC/`` is
        forbidden, and :mod:`nobitex_bot.rtc_export` produces the sanitized
        reports instead. The source databases are only read (SQLite backup
        API), never modified.
        """
        target = Path(dest_dir)
        out: dict = {"ok": True, "dir": str(target), "files": []}
        try:
            target.mkdir(parents=True, exist_ok=True)
            self.flush(2.0)
            for db, name in ((self._errors, ERROR_DB_NAME),
                             (self._trades, FAILED_TRADE_DB_NAME)):
                dest = target / name
                db.backup_to(dest)
                out["files"].append(str(dest))
        except Exception as exc:  # never break trading over a backup copy
            out["ok"] = False
            out["error"] = str(exc)
            self.record_error("DATABASE", "WARNING",
                              "local diagnostics backup failed: %s" % exc,
                              module="diagnostics.backup_local",
                              recovered=True,
                              recovery_action="main database untouched")
        return out


# --------------------------------------------------------------- default
_DEFAULT: Diagnostics | None = None
_HOOKS_INSTALLED = False
_ORIGINAL_HOOKS: dict[str, Any] = {}


def configure(data_dir: str | Path, **kwargs) -> Diagnostics:
    """Create (or reuse) the process-wide diagnostics instance."""
    global _DEFAULT
    if (_DEFAULT is not None and Path(data_dir) == _DEFAULT.data_dir
            and not _DEFAULT._closed):
        return _DEFAULT
    _DEFAULT = Diagnostics(data_dir, **kwargs)
    return _DEFAULT


def current() -> Diagnostics | None:
    return _DEFAULT


def reset_default() -> None:
    """Close and forget the default instance (tests / shutdown)."""
    global _DEFAULT
    if _DEFAULT is not None:
        _DEFAULT.close()
    _DEFAULT = None


def record_error(category: str = "UNKNOWN", severity: str = "ERROR",
                 message: str = "", **kwargs) -> str:
    """Record on the default instance; a no-op when not configured."""
    if _DEFAULT is None:
        return ""
    try:
        return _DEFAULT.record_error(category, severity, message, **kwargs)
    except Exception:
        return ""


def record_failed_trade(**kwargs) -> str:
    if _DEFAULT is None:
        return ""
    try:
        return _DEFAULT.record_failed_trade(**kwargs)
    except Exception:
        return ""


def install_exception_hooks(diag: Diagnostics | None = None) -> None:
    """Capture uncaught exceptions in the main thread and in threads.

    Chained to the previous hooks, so the console/traceback behaviour the
    operator expects is preserved. Installed once per process.
    """
    global _HOOKS_INSTALLED
    target = diag or _DEFAULT
    if target is None or _HOOKS_INSTALLED:
        return
    _ORIGINAL_HOOKS["excepthook"] = sys.excepthook
    _ORIGINAL_HOOKS["threading"] = getattr(threading, "excepthook", None)

    def excepthook(exc_type, exc, tb):
        try:
            target.record_error("WORKER", "CRITICAL",
                                "%s: %s" % (exc_type.__name__, exc),
                                module="sys.excepthook", exc=exc,
                                context={"uncaught": True})
        except Exception:
            pass
        _ORIGINAL_HOOKS["excepthook"](exc_type, exc, tb)

    def thread_excepthook(args):
        try:
            target.record_error(
                "WORKER", "CRITICAL",
                "%s: %s" % (args.exc_type.__name__, args.exc_value),
                module="threading.excepthook", exc=args.exc_value,
                context={"thread": getattr(args.thread, "name", "")})
        except Exception:
            pass
        original = _ORIGINAL_HOOKS.get("threading")
        if original:
            original(args)

    sys.excepthook = excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = thread_excepthook
    _HOOKS_INSTALLED = True


class DiagnosticsLogHandler(logging.Handler):
    """Route ``logging`` ERROR+ records into error.db.

    Attached to the root logger by the engine and by ``run.py``; keeps
    ordinary console/log files working while SQLite becomes the structured
    source of truth.
    """

    def __init__(self, diag: Diagnostics | None = None, level: int = logging.ERROR):
        super().__init__(level=level)
        self.diag = diag

    def emit(self, record: logging.LogRecord) -> None:
        diag = self.diag or _DEFAULT
        if diag is None:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        try:
            diag.record_error(
                "UNKNOWN", "CRITICAL" if record.levelno >= logging.CRITICAL
                else "ERROR", message,
                module=record.name, exc=record.exc_info[1]
                if record.exc_info else None,
                context={"logger": record.name, "level": record.levelname})
        except Exception:
            self.handleError(record)


def attach_logging(diag: Diagnostics | None = None,
                   level: int = logging.ERROR) -> DiagnosticsLogHandler | None:
    """Attach the handler to the root logger once (idempotent)."""
    target = diag or _DEFAULT
    if target is None:
        return None
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, DiagnosticsLogHandler):
            handler.diag = target
            return handler
    handler = DiagnosticsLogHandler(target, level=level)
    root.addHandler(handler)
    # deliberately do NOT touch the root level: the console/log-file setup
    # stays exactly as the operator configured it, and this handler simply
    # receives the ERROR+ records that already pass through.
    return handler
