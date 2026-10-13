"""Validation — contract sections 59-65.

Every payload entering the system is validated here (type, required fields,
enum, range, precision, timestamp, cross-field consistency, business rules)
before it is stored or used.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .enums import (
    AiAction, DataQualityStatus, LiquidityStatus, MarketRegime,
    OrderStatus, SnapshotStatus, VolatilityRegime,
)
from .utils import d, parse_iso, utc_now_iso

ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)

FINANCIAL_KEYS = {
    "price", "amount", "volume", "fee", "balance", "pnl",
    "cost", "notional", "value", "quantity",
}


@dataclass
class ValidationIssue:
    field: str
    code: str          # SS50-style machine-readable code
    message: str
    severity: str = "HIGH"


@dataclass
class ValidationResult:
    ok: bool = True
    issues: list = field(default_factory=list)

    def add(self, fld: str, code: str, msg: str, severity: str = "HIGH") -> None:
        self.issues.append(ValidationIssue(fld, code, msg, severity))
        self.ok = False

    def first_code(self) -> str:
        return self.issues[0].code if self.issues else ""


# ---------------------------------------------------------------- primitives

def validate_iso_timestamp(value: Any, res: ValidationResult, fld: str,
                           max_future_seconds: int = 120,
                           allow_future: bool = False) -> bool:
    if not isinstance(value, str) or not ISO_RE.match(value):
        res.add(fld, "INVALID_TIMESTAMP_FORMAT",
                "timestamp must be ISO-8601 with timezone (SS2)")
        return False
    try:
        dt = parse_iso(value)
    except ValueError:
        res.add(fld, "INVALID_TIMESTAMP_VALUE", "unparseable timestamp (SS2)")
        return False
    if allow_future:
        return True  # planned timestamps (e.g. SS42 expires_at) may be future
    now = parse_iso(utc_now_iso())
    if dt > now.replace(microsecond=0) and (dt - now).total_seconds() > max_future_seconds:
        res.add(fld, "FUTURE_TIMESTAMP", "timestamp is implausibly in the future (SS2)")
        return False
    return True


def validate_positive(value: Any, res: ValidationResult, fld: str,
                      allow_zero: bool = False) -> bool:
    try:
        dec = d(value)
    except (TypeError, InvalidOperation):
        res.add(fld, "NOT_A_NUMBER", "expected a numeric value (SS65)")
        return False
    if dec.is_nan() or dec.is_infinite():
        res.add(fld, "NOT_FINITE", "NaN/Infinity forbidden (SS65)")
        return False
    if dec < 0 or (dec == 0 and not allow_zero):
        res.add(fld, "INVALID_RANGE", "value must be %s0 (SS65)" %
                (">= " if allow_zero else "> "))
        return False
    return True


def validate_number(value: Any, res: ValidationResult, fld: str) -> bool:
    try:
        dec = d(value)
    except (TypeError, InvalidOperation):
        res.add(fld, "NOT_A_NUMBER", "expected a numeric value")
        return False
    if dec.is_nan() or dec.is_infinite():
        res.add(fld, "NOT_FINITE", "NaN/Infinity forbidden (SS65)")
        return False
    return True


def validate_enum(value: Any, allowed, res: ValidationResult, fld: str) -> bool:
    if value not in allowed:
        res.add(fld, "INVALID_ENUM",
                "value %r not in allowed set (SS60)" % (value,))
        return False
    return True


# ------------------------------------------------------------- entity rules

def validate_orderbook(book: dict, res: ValidationResult) -> None:
    """SS11: descending bids, ascending asks, cross, positivity, merge check."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    def levels(side):
        out = []
        for lv in side:
            price_ok = validate_positive(lv.get("price"), res, "orderbook.price")
            amount_ok = validate_positive(lv.get("amount"), res, "orderbook.amount")
            if price_ok and amount_ok:
                out.append((d(lv["price"]), d(lv["amount"])))
        return out

    bid_levels = levels(bids)
    ask_levels = levels(asks)

    for (p1, _), (p2, _) in zip(bid_levels, bid_levels[1:]):
        if p2 > p1:
            res.add("orderbook.bids", "ORDERBOOK_NOT_DESCENDING",
                    "bids must be descending (SS11)")
            break
    for (p1, _), (p2, _) in zip(ask_levels, ask_levels[1:]):
        if p2 < p1:
            res.add("orderbook.asks", "ORDERBOOK_NOT_ASCENDING",
                    "asks must be ascending (SS11)")
            break

    if bid_levels and ask_levels:
        if bid_levels[0][0] >= ask_levels[0][0]:
            res.add("orderbook", "CROSSED_BOOK",
                    "best bid must be < best ask (SS11)")

    seen = set()
    for p, _ in bid_levels + ask_levels:
        if p in seen:
            res.add("orderbook", "DUPLICATE_PRICE_LEVEL",
                    "duplicate price level must be merged (SS11)")
            break
        seen.add(p)


def validate_market(market: dict, res: ValidationResult) -> None:
    """SS5: base/quote rules, market type."""
    if not market.get("base_asset"):
        res.add("base_asset", "MISSING_FIELD", "market without base asset (SS5)")
    if not market.get("quote_asset"):
        res.add("quote_asset", "MISSING_FIELD", "market without quote asset (SS5)")
    if market.get("base_asset") and market.get("quote_asset") \
            and market["base_asset"] == market["quote_asset"]:
        res.add("base_asset", "CROSS_FIELD", "base_asset == quote_asset (SS5, SS64)")
    validate_enum(market.get("market_type", "SPOT"), {"SPOT"}, res, "market_type")


def validate_take_profit(targets: list, res: ValidationResult) -> None:
    """SS37: close percentages must sum to exactly 100."""
    total = 0
    for t in targets or []:
        try:
            total += d(t.get("close_percent", t.get("percentage", 0)))
        except Exception:
            res.add("take_profit", "NOT_A_NUMBER", "bad close_percent")
            return
    if total != 100:
        res.add("take_profit", "CROSS_FIELD",
                "take_profit close_percent must sum to 100 (SS37, got %s)" % total)


def validate_order_consistency(order: dict, res: ValidationResult) -> None:
    """SS64: matched + unmatched = amount; status in enum."""
    amount = order.get("amount")
    matched = order.get("matched_amount", 0)
    unmatched = order.get("unmatched_amount", None)
    try:
        amt, m = d(amount), d(matched)
        u = d(unmatched) if unmatched is not None else amt - m
        if abs((m + u) - amt) > d("1e-12"):
            res.add("amount", "CROSS_FIELD",
                    "matched_amount + unmatched_amount != amount (SS64)")
    except Exception:
        res.add("amount", "NOT_A_NUMBER", "bad amounts")
    validate_enum(order.get("status"), [e.value for e in OrderStatus],
                  res, "status")


def validate_portfolio_balances(assets: list, res: ValidationResult) -> None:
    """SS21/SS65: balances non-negative; total = available + locked."""
    for a in assets or []:
        name = a.get("asset", "?")
        for k in ("available", "locked", "total"):
            if not validate_positive(a.get(k, 0), res, "assets.%s.%s" % (name, k),
                                     allow_zero=True):
                continue
        try:
            avail, locked, total = d(a["available"]), d(a["locked"]), d(a["total"])
            if abs((avail + locked) - total) > d("1e-12"):
                res.add("assets.%s.total" % name, "CROSS_FIELD",
                        "total != available + locked (SS21, SS64)")
        except Exception:
            res.add("assets.%s" % name, "NOT_A_NUMBER", "bad balance numbers")


def validate_ai_decision(decision: dict, registry_symbols: set,
                         res: ValidationResult) -> None:
    """SS31/SS32/SS73/SS74/SS75: schema + enums + ranges + market universe."""
    for fld in ("decision_id", "snapshot_id", "cycle_id", "symbol",
                "action", "confidence", "created_at", "expires_at"):
        if fld not in decision or decision[fld] in (None, ""):
            res.add(fld, "MISSING_FIELD", "required field missing (SS31)")

    validate_enum(decision.get("action"),
                  [e.value for e in AiAction],
                  res, "action")

    conf = decision.get("confidence")
    try:
        c = d(conf)
        if not (0 <= c <= 1):
            res.add("confidence", "INVALID_RANGE", "confidence must be 0..1")
    except Exception:
        res.add("confidence", "NOT_A_NUMBER", "confidence must be numeric")

    symbol = decision.get("symbol")
    if registry_symbols and symbol not in registry_symbols:
        res.add("symbol", "UNKNOWN_MARKET",
                "symbol not in market_registry (SS75)")

    if decision.get("action") in ("BUY", "SELL"):
        entry = decision.get("entry") or {}
        for k in ("price", "min", "max"):
            if entry.get(k) is None:
                res.add("entry.%s" % k, "MISSING_FIELD", "entry.%s required" % k)
        if entry.get("min") is not None and entry.get("max") is not None \
                and d(entry["min"]) > d(entry["max"]):
            res.add("entry", "CROSS_FIELD", "entry.min > entry.max (SS64)")

        sl = decision.get("stop_loss") or {}
        if not sl.get("price"):
            res.add("stop_loss.price", "MISSING_FIELD", "stop loss required")
        if not sl.get("reason") and not sl.get("method"):
            res.add("stop_loss", "MISSING_FIELD", "stop loss needs reason/method (SS36)")
        validate_take_profit(decision.get("take_profit"), res)

        ps = decision.get("position_size") or {}
        if not validate_positive(ps.get("amount", 0), res, "position_size.amount"):
            res.add("position_size", "HALLUCINATION",
                    "negative/zero amount (SS74)")
        if decision.get("risk", {}).get("max_loss") is not None:
            validate_positive(decision["risk"]["max_loss"], res, "risk.max_loss",
                              allow_zero=True)  # exits carry max_loss=0

    # timestamps (expires_at is a planned TTL, so future is valid — SS42)
    if decision.get("created_at"):
        validate_iso_timestamp(decision["created_at"], res, "created_at")
    if decision.get("expires_at"):
        validate_iso_timestamp(decision["expires_at"], res, "expires_at",
                               allow_future=True)


def validate_snapshot_header(snap: dict, res: ValidationResult) -> None:
    """SS7/SS63: required snapshot header fields + enums."""
    for fld in ("schema_version", "snapshot_id", "cycle_id", "created_at", "status"):
        if fld not in snap:
            res.add(fld, "MISSING_FIELD", "snapshot missing %s" % fld)
    validate_enum(snap.get("status"), [e.value for e in SnapshotStatus],
                  res, "status")
    if snap.get("created_at"):
        validate_iso_timestamp(snap["created_at"], res, "created_at")
    validate_iso_timestamp(snap.get("created_at", utc_now_iso()), res, "created_at")


def data_quality_from_issues(issues: list) -> dict:
    """Derive a SS20 data_quality object from validation issues."""
    if not issues:
        return {"status": "GOOD", "score": 1.0, "checks": {
            "price_valid": True, "timestamp_valid": True,
            "orderbook_valid": True, "ohlc_valid": True,
            "trades_valid": True, "fresh": True,
        }, "warnings": []}
    codes = {i.code for i in issues}
    status = DataQualityStatus.INVALID if len(issues) > 2 or "CROSSED_BOOK" in codes \
        else DataQualityStatus.WARNING
    score = max(0.0, 1.0 - 0.15 * len(issues))
    return {"status": status.value, "score": round(score, 4),
            "checks": {}, "warnings": [
                {"code": i.code, "message": i.message, "field": i.field}
                for i in issues]}
