"""Market regime detection — the layer ABOVE signals.

None of the signals below matter if the market REGIME says the strategy
family is currently a coin-flip: the same trend-following logic that
makes money in a strong uptrend gets chopped to pieces in a downtrend,
and momentum-chasing (the risk mode's whole premise) degenerates in a
range. The regime gates WHAT the engine is allowed to buy this cycle —
it never invents its own trades.

Régimes (from features the collector already computes — no new API):
  STRONG_UPTREND / WEAK_UPTREND / SIDEWAYS /
  WEAK_DOWNTREND / STRONG_DOWNTREND / HIGH_VOLATILITY
"""

from __future__ import annotations

from .utils import clamp01


def detect(features: dict, atr_crash_pct: float = 8.0) -> dict:
    """One market's regime from its feature bundle.

    atr_crash_pct: ATR-percent threshold for the HIGH_VOLATILITY crash
    regime (volatility.atr_percent is a FRACTION — 0.02 = 2%).
    Returns {regime, trend_score, strength, atr_pct, reasons}.
    """
    tech = features.get("technicals", {}) or {}
    ema = tech.get("ema") or {}
    e9 = ema.get("9")
    e20 = ema.get("20")
    e50 = ema.get("50")
    rsi = tech.get("rsi")
    roc = tech.get("roc")
    structure = features.get("structure", {}) or {}
    trend_lbl = structure.get("trend", "UNKNOWN")
    brk = (structure.get("breakout", {}) or {})
    vol = features.get("volatility", {}) or {}
    atr_pct = vol.get("atr_percent")

    # ---- 1) EMA stacking: the backbone of the trend classification
    stack_up = stack_down = False
    if e9 and e20 and e50:
        stack_up = e9 > e20 > e50
        stack_down = e9 < e20 < e50

    # ---- 2) directional strength: ROC (≈ADX-proxy here — the collector
    # does not compute ADX; ROC/10 maps ±3% daily move onto ±0.3)
    roc_sig = 0.0
    if roc is not None:
        try:
            roc_sig = max(-0.3, min(0.3, float(roc) / 10.0))
        except (TypeError, ValueError):
            roc_sig = 0.0

    # ---- 3) composite trend score in [-1, +1]
    score = 0.0
    reasons = []
    if stack_up:
        score += 0.45
        reasons.append("EMA_STACK_UP")
    if stack_down:
        score -= 0.45
        reasons.append("EMA_STACK_DOWN")
    score += roc_sig
    if trend_lbl == "UPTREND":
        score += 0.15
    elif trend_lbl == "DOWNTREND":
        score -= 0.15
    if rsi is not None:
        try:
            score += max(-0.15, min(0.15, (float(rsi) - 50.0) / 250.0))
        except (TypeError, ValueError):
            pass
    score = max(-1.0, min(1.0, score))
    strength = abs(score)

    # ---- 4) breakout context refines the label
    if brk.get("detected"):
        if brk.get("direction") == "UP":
            score += 0.1
            reasons.append("BREAKOUT_UP")
        elif brk.get("direction") == "DOWN":
            score -= 0.1
            reasons.append("BREAKOUT_DOWN")
        score = max(-1.0, min(1.0, score))
        strength = max(strength, abs(score))

    # ---- 5) volatility crash-gate beats everything: a market whose
    # ATR% has exploded is un-tradeable by ANY strategy family here
    # (atr_percent is a fraction: 0.08 = 8%)
    crash = False
    try:
        if atr_pct is not None and float(atr_pct) * 100.0 >= atr_crash_pct:
            crash = True
            reasons.append("ATR_EXPANSION")
    except (TypeError, ValueError):
        pass

    if crash:
        regime = "HIGH_VOLATILITY"
    elif score >= 0.45:
        regime = "STRONG_UPTREND"
    elif score >= 0.15:
        regime = "WEAK_UPTREND"
    elif score <= -0.45:
        regime = "STRONG_DOWNTREND"
    elif score <= -0.15:
        regime = "WEAK_DOWNTREND"
    else:
        regime = "SIDEWAYS"

    return {
        "regime": regime,
        "trend_score": round(score, 4),
        "strength": round(clamp01(strength), 4),
        "atr_pct": atr_pct,
        "reasons": reasons,
    }


def allows_buy(regime: str, risk_mode: bool) -> bool:
    """Buy-gate per regime.

    Risk mode (momentum rotation) needs an UP regime — the whole premise
    is buying today's gainers; in a downtrend/range that is how accounts
    bleed. Safe mode is more permissive (multi-factor analytic pick),
    but a hard downtrend or crash-level volatility blocks both.
    """
    if risk_mode:
        return regime in ("STRONG_UPTREND", "WEAK_UPTREND")
    return regime not in ("STRONG_DOWNTREND", "WEAK_DOWNTREND",
                          "HIGH_VOLATILITY")


def gate_candidates(candidates: list, market_by_symbol: dict,
                    risk_mode: bool,
                    atr_crash_pct: float = 8.0) -> tuple[list, list]:
    """Filter scanner candidates by regime; annotate survivors.

    candidates: scanner output [{symbol, signals, reasons, ...}]
    market_by_symbol: {symbol: feature-bundle snapshot}
    Returns (kept, blocked). Kept candidates get a "regime" field.
    Blocked ones carry {"regime_blocked": regime} for the UI/logs.
    """
    kept, blocked = [], []
    for c in candidates:
        snap = market_by_symbol.get(c.get("symbol")) or {}
        r = detect(snap, atr_crash_pct=atr_crash_pct)
        if allows_buy(r["regime"], risk_mode):
            kept.append({**c, "regime": r["regime"],
                         "regime_trend_score": r["trend_score"]})
        else:
            blocked.append({**c, "regime_blocked": r["regime"]})
    return kept, blocked
