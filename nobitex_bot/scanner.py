"""Quant scanner — contract section 30.

Ranks markets before AI (AI only sees top-N). Produces a composite score
from trend/momentum/liquidity/volatility/orderbook/risk signals.

حالت ریسک (risk.risk_mode): the composite score is re-weighted so the
24-HOUR CHANGE dominates the ranking — the bot systematically buys the
BIGGEST 24h GAINERS first (momentum rotation) instead of the
multi-factor analytic pick.
"""
from __future__ import annotations

from .utils import clamp01


def _trend_signal(features: dict) -> float:
    tech = features.get("technicals", {})
    ema9 = (tech.get("ema") or {}).get("9")
    ema20 = (tech.get("ema") or {}).get("20")
    ema50 = (tech.get("ema") or {}).get("50")
    rsi = tech.get("rsi")
    structure = features.get("structure", {})
    score = 0.4
    if ema9 and ema20 and ema50:
        if ema9 > ema20 > ema50:
            score = 0.9
        elif ema9 < ema20 < ema50:
            score = 0.1
        else:
            score = 0.5
    if structure.get("trend") == "UPTREND":
        score += 0.1
    elif structure.get("trend") == "DOWNTREND":
        score -= 0.1
    if rsi is not None:
        score += (rsi - 50) / 500  # small tilt
    return clamp01(score)


def _momentum_signal(features: dict) -> float:
    tech = features.get("technicals", {})
    macd = tech.get("macd") or {}
    hist = macd.get("histogram")
    roc = tech.get("roc")
    vr = tech.get("volume_ratio")
    score = 0.5
    if hist is not None:
        score += clamp01(hist / (abs(hist) + 1e-9) * 0.2) * (0.5 if hist > 0 else -0.5) * 0.4
    if roc is not None:
        score += max(-0.3, min(0.3, roc / 10))
    if vr is not None:
        score += clamp01((vr - 1) * 0.1)
    return clamp01(score)


def _momentum_24h_signal(features: dict) -> float:
    """24-hour change (percent, already in %) mapped to [0, 1].

    +10% or more -> 1.0, 0% -> 0.5, -10% or worse -> 0.0. Feeds the
    risk-mode score so the ranking follows the day's top movers.
    """
    chg = features.get("change_24h")
    try:
        chg = float(chg)
    except (TypeError, ValueError):
        return 0.5
    return clamp01(0.5 + chg / 20.0)


def _volatility_suitability(features: dict) -> float:
    vol = features.get("volatility", {})
    regime = vol.get("volatility_regime")
    table = {"VERY_LOW": 0.55, "LOW": 0.8, "MEDIUM": 0.9,
             "HIGH": 0.5, "EXTREME": 0.15, "UNKNOWN": 0.3}
    return table.get(regime, 0.3)


def _risk_signal(features: dict, market_quality: float) -> float:
    liq = features.get("liquidity", {})
    spread_pct = features.get("orderbook", {}).get("spread_percent") or 0.02
    liq_status = liq.get("status", "UNKNOWN")
    base = market_quality * 0.5
    if liq_status in ("EXCELLENT", "GOOD"):
        base += 0.4
    elif liq_status == "MEDIUM":
        base += 0.2
    elif liq_status == "POOR":
        base += 0.0
    else:
        base -= 0.1
    base += clamp01(1.0 - spread_pct * 250) * 0.1
    return clamp01(base)


def scan_market(symbol: str, features: dict, data_quality: dict,
                risk_mode: bool = False,
                min_quote_volume_usdt: float = 0.0) -> dict:
    """Score one market; returns a SS30 candidate payload.

    risk_mode: rank by the 24h change FIRST — composite = 50% day-change
    + 15% classic momentum + 15% trend + 20% tradability (liquidity /
    orderbook / quality). The tradability slice keeps un-tradeable
    markets (no orderbook, dead quality) out of the top of the list.

    PUMP-VOLUME FILTER (risk mode): the classic beginner trap is buying
    the day's biggest gainers WITHOUT checking volume — a token up big on
    thin volume is a trap, not a trend (pump-and-dump). In risk mode a
    24h quote-volume floor is enforced (min_quote_volume_usdt, 0 = off):
    pumps on illiquid markets are marked LOW_VOLUME_PUMP and
    score 0 so they never reach the top of the buy list.
    """
    dq_score = data_quality.get("score", 0.0)
    orderbook_sig = clamp01(features.get("orderbook", {}).get("liquidity_score") or 0)
    mom24 = _momentum_24h_signal(features)
    low_volume_pump = False
    if risk_mode:
        floor = float(min_quote_volume_usdt or 0)
        qv = features.get("quote_volume_24h")
        try:
            qv = float(qv)
        except (TypeError, ValueError):
            qv = None
        if floor > 0 and (qv is None or qv < floor):
            low_volume_pump = True
    signals = {
        "trend": _trend_signal(features),
        "momentum": _momentum_signal(features),
        "momentum_24h": mom24,
        "liquidity": clamp01(features.get("liquidity", {}).get("score") or 0),
        "volatility": _volatility_suitability(features),
        "orderbook": orderbook_sig,
        "risk": _risk_signal(features, dq_score),
    }
    if risk_mode:
        if low_volume_pump:
            score = 0.0
        else:
            score = clamp01(
                0.50 * signals["momentum_24h"] +
                0.15 * signals["momentum"] +
                0.15 * signals["trend"] +
                0.10 * signals["liquidity"] +
                0.05 * signals["orderbook"] +
                0.05 * signals["risk"])
    else:
        score = clamp01(
            0.25 * signals["trend"] +
            0.20 * signals["momentum"] +
            0.20 * signals["liquidity"] +
            0.15 * signals["volatility"] +
            0.10 * signals["orderbook"] +
            0.10 * signals["risk"])

    reasons = []
    if low_volume_pump:
        reasons.append("LOW_VOLUME_PUMP")
    if signals["momentum_24h"] > 0.7:
        reasons.append("TOP_24H_MOVER")
    if signals["momentum_24h"] < 0.3:
        reasons.append("WEAK_24H")
    if signals["trend"] > 0.7:
        reasons.append("TREND_ALIGNMENT")
    if signals["momentum"] > 0.6:
        reasons.append("POSITIVE_MOMENTUM")
    if signals["momentum"] < 0.4:
        reasons.append("NEGATIVE_MOMENTUM")
    if signals["liquidity"] > 0.75:
        reasons.append("HIGH_LIQUIDITY")
    if signals["volatility"] > 0.75:
        reasons.append("SUITABLE_VOLATILITY")
    if features.get("structure", {}).get("breakout", {}).get("detected"):
        reasons.append("BREAKOUT_DETECTED")
    if dq_score < 0.6:
        reasons.append("LOW_DATA_QUALITY")

    return {
        "symbol": symbol,
        "rank": 0,
        "score": round(score, 4),
        "signals": {k: round(v, 4) for k, v in signals.items()},
        "reasons": reasons,
    }


def pre_rank_from_catalog(rows: dict, min_quote_volume_usdt: float = 0.0,
                          extra_starts: int = 12) -> list:
    """Lightweight pre-rank over the WHOLE live catalog (no candles).

    `rows` = MarketUniverse.rows() — every market the exchange lists,
    received in ONE cached request (/market/stats). Each row is scored
    with the same momentum_24h signal the risk-mode rank uses, so the
    day's movers across EVERY listed coin (not just the volume top-N)
    get a chance to enter the deep-analysis pool of the next cycle.

    Returns [(symbol, momentum_score, quote_volume, change_24h)] sorted
    best-first. Liquidity floor keeps raw pumps out; the deep pool is
    still bounded by the caller, so bandwidth stays flat.
    """
    out = []
    for sym, r in (rows or {}).items():
        if not r or not r.get("tradable", True):
            continue
        qv = _f(r.get("quote_volume_24h"))
        if min_quote_volume_usdt and qv < min_quote_volume_usdt:
            continue
        out.append((sym, _momentum_24h_signal(r), qv,
                    _f(r.get("change_24h"))))
    out.sort(key=lambda x: -x[1])
    return out[:max(1, int(extra_starts)) * 2]


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def rank_markets(market_features: list, top_n: int,
                 risk_mode: bool = False,
                 min_quote_volume_usdt: float = 0.0) -> list:
    """market_features: [{symbol, features, data_quality}] -> top-N ranked.

    risk_mode=True ranks by the biggest 24h gainers (see scan_market);
    min_quote_volume_usdt (risk mode only) filters thin-volume pumps.
    """
    candidates = [scan_market(m["symbol"], m["features"], m["data_quality"],
                              risk_mode=risk_mode,
                              min_quote_volume_usdt=min_quote_volume_usdt)
                  for m in market_features]
    candidates.sort(key=lambda c: -c["score"])
    for i, c in enumerate(candidates, 1):
        c["rank"] = i
    return candidates[:max(1, top_n)]
