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


def directional_setup(features: dict) -> dict:
    """Long/Short setup of ONE market from its OWN candles (SS30d).

    درخواست کاربر: «تب AI Decisions بر اساس واقعی نمودار هر کوین بگوید
    کی می‌شود LONG گرفت، کی SHORT» — این تابع همان تحلیل را همه‌جا
    قابل‌اجرا می‌کند: دادهٔ candle همان بازار (که collector آورده)،
    شاخص‌های تکنیکالِ همان بازار و ساختار همان بازار.

    خروجی : {long: {ready, score, reasons}, short: {ready, score,
    reasons}, action: BUY|SELL|HOLD|WAIT} — همهٔ عددها بین 0 تا 1؛
    reasons لیست کدهای قابل‌نمایش در فارسی.

    نکتهٔ Short: این صرافی معاملهٔ Short را نمی‌پذیرد؛ short_ready یعنی
    ساختار نزولی قوی برای «فروش/خرید نکن»، نه اینکه سفارش Short شونده
    واقعی باشد.
    """
    tech = features.get("technicals", {})
    st = features.get("structure", {})
    vol = features.get("volatility", {})
    liq = features.get("liquidity", {})
    dq = clamp01(features.get("data_quality", {}).get("score") or 0)
    def _base_r(dqf: float) -> dict:
        return {"ready": False, "score": round(dqf, 4),
                "reasons": ["DATA_QUALITY_LOW"]}
    if dq < 0.4:
        return {"long": _base_r(0.0), "short": _base_r(0.0),
                "action": "WAIT", "evidence": {"data_quality": round(dq, 4)}}

    ema9 = (tech.get("ema") or {}).get("9")
    ema20 = (tech.get("ema") or {}).get("20")
    ema50 = (tech.get("ema") or {}).get("50")
    rsi = tech.get("rsi")
    macd = tech.get("macd") or {}
    hist = macd.get("histogram")
    bb = tech.get("bollinger") or {}
    vr = tech.get("volume_ratio")
    trend = str(st.get("trend") or "").upper()
    st_map = st.get("structure") or {}
    br = st.get("breakout") or {}
    breakout_dir = str(br.get("direction") or "").upper()
    breakout_st = float(br.get("strength") or 0)
    regime = str(vol.get("volatility_regime") or "").upper()
    atr_pct = float(vol.get("atr_percent") or 0)
    liq_status = str(liq.get("status") or "").upper()
    liq_score = float(liq.get("score") or 0)

    long_score = 0.45
    short_score = 0.45
    long_reasons: list = []
    short_reasons: list = []

    # ساختار بازار: HH/HL در برابر LH/LL
    if trend == "UPTREND":
        long_score += 0.20
        long_reasons.append("UPTREND_STRUCTURE")
        short_score -= 0.15
        short_reasons.append("AGAINST_UPTREND")
    elif trend == "DOWNTREND":
        short_score += 0.20
        short_reasons.append("DOWNTREND_STRUCTURE")
        long_score -= 0.15
        long_reasons.append("AGAINST_DOWNTREND")
    else:
        long_reasons.append("RANGE_MARKET")
        short_reasons.append("RANGE_MARKET")

    # ترتیب EMA ها
    if ema9 and ema20 and ema50:
        if ema9 > ema20 > ema50:
            long_score += 0.15
            long_reasons.append("EMA9_GT_EMA20_GT_EMA50")
            short_score -= 0.10
            short_reasons.append("EMA_STACK_BEARISH_CONFLICT")
        elif ema9 < ema20 < ema50:
            short_score += 0.15
            short_reasons.append("EMA9_LT_EMA20_LT_EMA50")
            long_score -= 0.10
            long_reasons.append("EMA_STACK_BULLISH_CONFLICT")

    # RSI: نه خریدِ سقف، نه فروشِ کف
    if rsi is not None:
        if rsi >= 72:
            long_score -= 0.15
            long_reasons.append("RSI_OVERBOUGHT")
            short_score += 0.10
            short_reasons.append("RSI_OVERBOUGHT_REVERSAL")
        elif rsi <= 28:
            short_score -= 0.12
            short_reasons.append("RSI_OVERSOLD")
            long_score += 0.10
            long_reasons.append("RSI_OVERSOLD_REVERSAL")
        elif 45 <= rsi <= 62:
            long_score += 0.06
            long_reasons.append("RSI_HEALTHY")

    # MACD histogram
    if hist is not None:
        if hist > 0:
            long_score += 0.08
            long_reasons.append("MACD_POSITIVE")
            short_score -= 0.05
        elif hist < 0:
            short_score += 0.08
            short_reasons.append("MACD_NEGATIVE")
            long_score -= 0.05

    # Bollinger: شکست میانه
    if bb.get("middle") and bb.get("upper"):
        last_close = float(features.get("last_price") or 0)
        if last_close > 0:
            mid = float(bb.get("middle") or 0)
            up = float(bb.get("upper") or 0)
            lo = float(bb.get("lower") or 0)
            if lo and last_close <= lo:
                short_score -= 0.10
                short_reasons.append("PRICE_BELOW_BB_LOWER")
            elif up and last_close >= up:
                long_score -= 0.10
                long_reasons.append("PRICE_ABOVE_BB_UPPER")
            elif last_close >= mid:
                long_score += 0.05
                long_reasons.append("PRICE_ABOVE_BB_MID")

    # شکست‌ها
    if br.get("detected"):
        if breakout_dir == "UP":
            long_score += 0.15
            long_reasons.append("BREAKOUT_UP")
            short_score -= 0.10
        elif breakout_dir == "DOWN":
            short_score += 0.15
            short_reasons.append("BREAKDOWN_DETECTED")
            long_score -= 0.10

    # نقدداش: بدون نقدینگی چیزی قاطع نیست
    if liq_score < 0.30 or liq_status in ("POOR", "CRITICAL", "UNKNOWN"):
        long_reasons.append("LIQUIDITY_LOW")
        short_reasons.append("LIQUIDITY_LOW")
        long_score -= 0.10
        short_score -= 0.10

    # ولتیلید: بازار کم‌نوسان یا انفجاری = سیگنال کمتر
    if regime in ("VERY_LOW", "UNKNOWN"):
        long_reasons.append("LOW_VOLATILITY")
        short_reasons.append("LOW_VOLATILITY")
        long_score -= 0.05
        short_score -= 0.05
    elif regime == "EXTREME" or atr_pct > 0.06:
        long_reasons.append("EXTREME_VOLATILITY")
        short_reasons.append("EXTREME_VOLATILITY")
        long_score -= 0.10
        short_score -= 0.10

    # حجم تأیید: حرکت با حجم پشتیبانی شود
    if vr is not None:
        try:
            vr = float(vr)
        except (TypeError, ValueError):
            vr = None
    if vr is not None:
        if vr >= 1.5:
            if hist is not None and hist > 0:
                long_score += 0.08
                long_reasons.append("VOLUME_CONFIRMED_UP")
            elif hist is not None and hist < 0:
                short_score += 0.08
                short_reasons.append("VOLUME_CONFIRMED_DOWN")
        elif vr < 0.6:
            long_reasons.append("VOLUME_DRYING_UP")
            short_reasons.append("VOLUME_DRYING_UP")
            long_score -= 0.08
            short_score -= 0.08

    long_score = clamp01(long_score)
    short_score = clamp01(short_score)

    def _fa_score_ok(s: float, req: float = 0.62) -> bool:
        return s >= req

    long_ready = _fa_score_ok(long_score)
    short_ready = _fa_score_ok(short_score)
    if long_ready and not short_ready:
        action = "BUY"
    elif short_ready and not long_ready:
        action = "SELL"
    elif long_ready and short_ready:
        # هر دو قوی: بازار پرنوسان/تناقض — صبر
        action = "WAIT"
        long_reasons.append("CONFLICTING_SIGNALS")
        short_reasons.append("CONFLICTING_SIGNALS")
    else:
        action = "HOLD"

    return {
        "long": {"ready": long_ready, "score": round(long_score, 4),
                 "reasons": long_reasons[-10:]},
        "short": {"ready": short_ready, "score": round(short_score, 4),
                  "reasons": short_reasons[-10:]},
        "action": action,
        "liquidity_status": liq_status,
        "regime": regime,
        "atr_percent": round(atr_pct, 6),
    }


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

    # تحلیل Long/Short از دادهٔ تکنیکالِ همان بازار — «جدول AI Decisions»
    # کاربر فقط وقتی قابل‌نمایش است که این تحلیل روی هر بازار از چرخهٔ
    # داده گرفته باشد، نه صرفاً روی top-N.'
    return {
        "symbol": symbol,
        "rank": 0,
        "score": round(score, 4),
        "signals": {k: round(v, 4) for k, v in signals.items()},
        "reasons": reasons,
        "directional": directional_setup(features),
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
