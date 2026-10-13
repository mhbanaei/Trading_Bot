"""Contract enums — sections 8, 15, 16, 20, 24, 32, 39, 51 of the data contract.

Agent may NOT invent new enum values (section 60); validation rejects unknowns.
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum that serializes as its plain value."""

    def __str__(self) -> str:
        return str(self.value)


class SnapshotStatus(StrEnum):
    COLLECTING = "COLLECTING"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    INVALID = "INVALID"
    CORRUPTED = "CORRUPTED"


class DataQualityStatus(StrEnum):
    GOOD = "GOOD"
    WARNING = "WARNING"
    STALE = "STALE"
    INVALID = "INVALID"
    CRITICAL = "CRITICAL"


class LiquidityStatus(StrEnum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    MEDIUM = "MEDIUM"
    POOR = "POOR"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class VolatilityRegime(StrEnum):
    VERY_LOW = "VERY_LOW"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"
    UNKNOWN = "UNKNOWN"


class MarketRegime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNCERTAIN = "UNCERTAIN"


class AiAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    AVOID = "AVOID"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class ExecutionStatus(StrEnum):
    READY = "READY"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ErrorSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"


def enum_values(cls) -> list:
    return [e.value for e in cls]
