"""Event bus — contract sections 50-53.

All events are immutable, machine-readable, and persisted (JSONL + SQLite).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from .enums import ErrorSeverity
from .utils import new_event_id, new_error_id, utc_now_iso

#: Every type publish() accepts. ANY type missing from this list is
#: silently rewritten to SYSTEM_ERROR, which made the live view show a
#: wallet sync / manual sell / ladder step as a red error line. Keep it in
#: sync with the publish() calls (tests/test_live_universe.py pins the ones
#: the engine uses).
EVENT_TYPES = [
    "SNAPSHOT_CREATED", "SNAPSHOT_COMPLETED", "SNAPSHOT_INVALID",
    "SNAPSHOT_HEADER_INVALID", "SNAPSHOT_VALIDATE_FAILED",
    "MARKET_DISCOVERED", "MARKET_DISABLED",
    "MARKET_DISCOVERY_FAILED", "MARKET_COLLECTION_FAILED",
    "WALLET_SYNCED", "EXCHANGE_ATTACHED", "LOGIN_SUCCESS", "LOGOUT",
    "LIVE_TRADING_CHANGED", "BUY_BEST_CHANGED",
    "MARKET_TRANSFER_GATE_CHANGED",
    "MANUAL_BUY_EXECUTED", "MANUAL_SELL", "BEST_TRADE_EXECUTED",
    "BOT_ORDERS_CANCELLED",
    "DEPOSITED_CAPITAL_SET",
    # exit-plan bookkeeping (stop / take-profit / floor / ladder)
    "BACKSTOP_ARMED", "HADD_SET", "PROFIT_LOCK_ADVANCED",
    "LADDER_STOP_RAISED", "LADDER_TOPUP_FIRED", "LADDER_TOPUP_BLOCKED",
    "LADDER_BUY_KEPT_LADDER", "POSITION_ENTRY_REPAIRED",
    "POSITION_MARKET_REFILED",
    "EXIT_CONVERTED_BELOW_MIN", "EXIT_STALE_POSITION_CLEARED",
    "DUST_CONVERT_BLOCKED",
    "AI_ANALYSIS_STARTED", "AI_ANALYSIS_COMPLETED", "AI_DECISION_CREATED",
    "RISK_APPROVED", "RISK_REJECTED",
    "ORDER_SUBMITTED", "ORDER_OPENED", "ORDER_PARTIALLY_FILLED",
    "ORDER_FILLED", "ORDER_CANCELLED", "ORDER_REJECTED",
    "POSITION_OPENED", "POSITION_UPDATED", "POSITION_CLOSED",
    "SAFE_MODE_ENTERED", "SAFE_MODE_EXITED",
    "RISK_MODE_CHANGED", "RISK_MODE_SWEPT", "RISK_MODE_SWEEP_FAILED",
    "RISK_MODE_REGIME_BLOCKED", "QUOTE_MARKETS_BOTH_OFF",
    "AUTO_STOP_SET", "PROFIT_TOPUP_FIRED",
    "DUST_CONVERTED", "DUST_CONVERT_FAILED",
    "SYSTEM_ERROR",
]


class EventBus:
    """Thread-safe publish/subscribe with JSONL persistence (SS52: immutable)."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.events_dir = self.data_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl = self.events_dir / "events.jsonl"
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []
        self.max_bytes = 0

    def set_retention(self, max_mb: float) -> None:
        """Cap events.jsonl size: when it exceeds max_mb MB the oldest half
        is dropped (events are diagnostics, not financial records — fills/
        orders/decisions live in SQLite)."""
        try:
            self.max_bytes = max(0.0, float(max_mb)) * 1024 * 1024
        except (TypeError, ValueError):
            self.max_bytes = 0

    def _prune_events(self) -> None:
        if self.max_bytes <= 0:
            return
        try:
            if self._jsonl.exists() and \
                    self._jsonl.stat().st_size > self.max_bytes:
                lines = self._jsonl.read_text(encoding="utf-8",
                                              errors="replace").splitlines()
                keep = lines[len(lines) // 2:]
                tmp = self._jsonl.with_suffix(".tmp")
                tmp.write_text(
                    "\n".join(keep) + ("\n" if keep else ""),
                    encoding="utf-8")
                tmp.replace(self._jsonl)
        except OSError:
            pass

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def publish(self, event_type: str, source: str, cycle_id: str = "",
                payload: dict | None = None) -> dict:
        if event_type not in EVENT_TYPES:
            event_type = "SYSTEM_ERROR"
        event = {
            "event_id": new_event_id(),
            "event_type": event_type,
            "timestamp": utc_now_iso(),
            "source": source,
            "cycle_id": cycle_id,
            "payload": payload or {},
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            with open(self._jsonl, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._prune_events()
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(event)
            except Exception:
                pass
        return event

    def error(self, component: str, code: str, severity: str,
              message: str, retryable: bool = False,
              safe_mode_required: bool = False, cycle_id: str = "") -> dict:
        """Emit a SS50 machine-readable error event."""
        err = {
            "error_id": new_error_id(),
            "timestamp": utc_now_iso(),
            "component": component,
            "code": code,
            "severity": severity,
            "message": message,
            "retryable": retryable,
            "safe_mode_required": safe_mode_required,
        }
        self.publish("SYSTEM_ERROR", component, cycle_id, err)
        return err

    def recent(self, limit: int = 100) -> list:
        if not self._jsonl.exists():
            return []
        try:
            lines = self._jsonl.read_text(encoding="utf-8").strip().splitlines()
            return [json.loads(x) for x in lines[-limit:]]
        except Exception:
            return []
