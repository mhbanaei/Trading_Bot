"""Feature engine — contract sections 12, 15-19.

Pure-Python implementations of the required indicators and derived features.
Missing data yields null (SS59) — never zero.
"""
from __future__ import annotations

import math
from decimal import Decimal

from .enums import LiquidityStatus, MarketRegime, VolatilityRegime
from .utils import clamp01, d


# ------------------------------------------------------------- indicators

def ema(values: list, period: int):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def sma(values: list, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: list, period: int = 14):
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: list):
    if len(values) < 35:
        return {"value": None, "signal": None, "histogram": None}
    fast = _ema_series(values, 12)
    slow = _ema_series(values, 26)
    line = [f - s for f, s in zip(fast, slow)][26 - 12:]
    signal_series = _ema_series(line, 9)
    if not line or not signal_series:
        return {"value": None, "signal": None, "histogram": None}
    hist = line[-1] - signal_series[-1]
    return {"value": round(line[-1], 8), "signal": round(signal_series[-1], 8),
            "histogram": round(hist, 8)}


def _ema_series(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def stochastic(highs: list, lows: list, closes: list, period: int = 14):
    if len(closes) < period + 3:
        return {"k": None, "d": None}
    ks = []
    for i in range(period, len(closes) + 1):
        window_h = highs[i - period:i]
        window_l = lows[i - period:i]
        hh, ll = max(window_h), min(window_l)
        if hh == ll:
            ks.append(50.0)
        else:
            ks.append((closes[i - 1] - ll) / (hh - ll) * 100.0)
    if len(ks) < 3:
        return {"k": None, "d": None}
    k = ks[-1]
    d_val = sum(ks[-3:]) / 3.0
    return {"k": round(k, 4), "d": round(d_val, 4)}


def roc(values: list, period: int = 9):
    if len(values) < period + 1:
        return None
    base = values[-period - 1]
    if not base:
        return None
    return round((values[-1] / base - 1.0) * 100.0, 4)


def bollinger(values: list, period: int = 20, mult: float = 2.0):
    if len(values) < period:
        return {"upper": None, "middle": None, "lower": None, "bandwidth": None}
    window = values[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = math.sqrt(var)
    upper, lower = mid + mult * sd, mid - mult * sd
    bandwidth = (upper - lower) / mid if mid else None
    return {"upper": round(upper, 8), "middle": round(mid, 8),
            "lower": round(lower, 8), "bandwidth": round(bandwidth, 6)}


def obv(closes: list, volumes: list):
    if len(closes) != len(volumes) or not closes:
        return None
    total = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            total += volumes[i]
        elif closes[i] < closes[i - 1]:
            total -= volumes[i]
    return total


def volume_ratio(volumes: list, period: int = 20):
    if len(volumes) < period + 1:
        return None
    avg = sum(volumes[-period - 1:-1]) / period
    if not avg:
        return None
    return round(volumes[-1] / avg, 4)


# ------------------------------------------------- orderbook derived (SS12)

def orderbook_features(book: dict, last_price: float) -> dict:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    spread = (best_ask - best_bid) if (best_bid and best_ask) else None
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else last_price
    spread_pct = (spread / mid) if (spread is not None and mid) else None

    bid_vol = sum(float(x["amount"]) for x in bids)
    ask_vol = sum(float(x["amount"]) for x in asks)
    imbalance = ((bid_vol - ask_vol) / (bid_vol + ask_vol)
                 if (bid_vol + ask_vol) else None)

    def depth(side_levels: list, pct: float) -> float:
        """Notional within pct of mid."""
        limit = mid * (1 - pct) if side_levels is bids else mid * (1 + pct)
        total = 0.0
        for lv in side_levels:
            price = float(lv["price"])
            if side_levels is bids and price < limit:
                break
            if side_levels is asks and price > limit:
                break
            total += float(lv["amount"]) * price
        return total

    d1 = depth(bids, 0.01) + depth(asks, 0.01)
    d2 = depth(bids, 0.02) + depth(asks, 0.02)
    d5 = depth(bids, 0.05) + depth(asks, 0.05)

    def slippage(levels: list, walk_notional: float, is_buy: bool) -> float:
        """Average price impact for walking the book for notional."""
        if not mid or not levels:
            return None
        remaining = walk_notional
        cost = 0.0
        filled = 0.0
        for lv in levels:
            price = float(lv["price"])
            amount = float(lv["amount"])
            take = min(remaining, amount * price)
            cost += take * price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        if filled <= 0:
            return None
        vwap = cost / filled
        return abs(vwap / mid - 1.0)

    walk = 500.0  # quote units of simulated flow
    buy_slip = slippage(asks, walk, True)
    sell_slip = slippage(bids, walk, False)

    depth_score = clamp01(math.log10(max(d1, 1.0) + 1) / 5)
    liquidity_score = clamp01(
        0.5 * depth_score +
        0.3 * clamp01(1.0 - (spread_pct or 0.01) * 250) +
        0.2 * clamp01(1.0 - (buy_slip or 0.01) * 120))

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_percent": round(spread_pct, 8) if spread_pct is not None else None,
        "bid_volume": round(bid_vol, 8),
        "ask_volume": round(ask_vol, 8),
        "bid_ask_imbalance": round(imbalance, 6) if imbalance is not None else None,
        "depth_1pct": round(d1, 2),
        "depth_2pct": round(d2, 2),
        "depth_5pct": round(d5, 2),
        "buy_slippage_estimate": round(buy_slip, 6) if buy_slip is not None else None,
        "sell_slippage_estimate": round(sell_slip, 6) if sell_slip is not None else None,
        "estimated_market_impact": round((buy_slip or 0) * 0.6, 6),
        "liquidity_score": round(liquidity_score, 4),
    }


# --------------------------------------------------------- liquidity (SS15)

def liquidity_score(book_feat: dict, quote_volume_24h: float) -> dict:
    spread_pct = book_feat.get("spread_percent") or 0.01
    depth1 = book_feat.get("depth_1pct") or 0.0
    slip = book_feat.get("buy_slippage_estimate") or 0.01
    impact = book_feat.get("estimated_market_impact") or 0.01

    spread_score = clamp01(1.0 - spread_pct * 200)
    depth_score = clamp01(math.log10(max(depth1, 1.0)) / 6)
    vol_score = clamp01(math.log10(max(quote_volume_24h, 10.0)) / 8)
    slippage_score = clamp01(1.0 - slip * 100)
    impact_score = clamp01(1.0 - impact * 100)
    score = clamp01(0.30 * spread_score + 0.25 * depth_score +
                    0.20 * vol_score + 0.15 * slippage_score +
                    0.10 * impact_score)

    if score >= 0.85:
        status = LiquidityStatus.EXCELLENT
    elif score >= 0.65:
        status = LiquidityStatus.GOOD
    elif score >= 0.45:
        status = LiquidityStatus.MEDIUM
    elif score >= 0.25:
        status = LiquidityStatus.POOR
    else:
        status = LiquidityStatus.CRITICAL

    return {
        "score": round(score, 4),
        "spread_score": round(spread_score, 4),
        "depth_score": round(depth_score, 4),
        "volume_score": round(vol_score, 4),
        "slippage_score": round(slippage_score, 4),
        "market_impact_score": round(impact_score, 4),
        "estimated_buy_slippage": book_feat.get("buy_slippage_estimate"),
        "estimated_sell_slippage": book_feat.get("sell_slippage_estimate"),
        "status": status.value,
    }


# -------------------------------------------------------- volatility (SS16)

def atr(candles: list, period: int = 14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l = float(candles[i]["high"]), float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def volatility_features(candles: list, last_price: float) -> dict:
    if len(candles) < 21 or not last_price:
        return {"atr": None, "atr_percent": None,
                "realized_volatility": None, "historical_volatility": None,
                "volatility_regime": VolatilityRegime.UNKNOWN.value}
    a = atr(candles, 14)
    closes = [float(c["close"]) for c in candles]
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    n = min(len(rets), 30)
    if n:
        mean = sum(rets[-n:]) / n
        var = sum((r - mean) ** 2 for r in rets[-n:]) / n
        realized = math.sqrt(var) * math.sqrt(len(rets[-n:]))
    else:
        realized = None
    a_pct = (a / last_price) if a else None

    if a_pct is None:
        regime = VolatilityRegime.UNKNOWN.value
    elif a_pct < 0.004:
        regime = VolatilityRegime.VERY_LOW.value
    elif a_pct < 0.010:
        regime = VolatilityRegime.LOW.value
    elif a_pct < 0.025:
        regime = VolatilityRegime.MEDIUM.value
    elif a_pct < 0.05:
        regime = VolatilityRegime.HIGH.value
    else:
        regime = VolatilityRegime.EXTREME.value

    return {"atr": round(a, 8) if a else None,
            "atr_percent": round(a_pct, 6) if a_pct else None,
            "realized_volatility": round(realized, 6) if realized else None,
            "historical_volatility": round(realized, 6) if realized else None,
            "volatility_regime": regime}


# ------------------------------------------------------- technicals (SS17)

def technical_features(candles: list) -> dict:
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]

    macd_out = macd(closes)
    stoch = stochastic(highs, lows, closes)
    boll = bollinger(closes)

    return {
        "ema": {str(p): (round(v, 8) if (v := ema(closes, p)) is not None else None)
                for p in (9, 20, 50, 100, 200)},
        "sma": {str(p): (round(v, 8) if (v := sma(closes, p)) is not None else None)
                for p in (20, 50, 200)},
        "rsi": (lambda v: round(v, 4) if v is not None else None)(rsi(closes, 14)),
        "macd": macd_out,
        "stochastic": stoch,
        "roc": roc(closes, 9),
        "bollinger": boll,
        "obv": obv(closes, volumes),
        "volume_ratio": volume_ratio(volumes, 20),
    }


# --------------------------------------------------- market structure (SS18)

def market_structure(candles: list) -> dict:
    if len(candles) < 40:
        return {"trend": "UNKNOWN", "structure": {}, "support_levels": [],
                "resistance_levels": [], "breakout": {"detected": False}}
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    def pivots(series: list, look: int = 5) -> list:
        pts = []
        for i in range(look, len(series) - look):
            window = series[i - look:i + look + 1]
            if series[i] == max(window):
                pts.append((i, series[i], "high"))
            if series[i] == min(window):
                pts.append((i, series[i], "low"))
        return pts[-8:]

    ph = [p for p in pivots(highs) if p[2] == "high"]
    pl = [p for p in pivots(lows) if p[2] == "low"]
    hh = len(ph) >= 2 and ph[-1][1] > ph[-2][1]
    hl = len(pl) >= 2 and pl[-1][1] > pl[-2][1]
    lh = len(ph) >= 2 and ph[-1][1] < ph[-2][1]
    ll = len(pl) >= 2 and pl[-1][1] < pl[-2][1]

    if hh and hl:
        trend = "UPTREND"
    elif lh and ll:
        trend = "DOWNTREND"
    else:
        trend = "RANGE"

    support = sorted({round(min(pl[-2:][i][1], 0) if False else pl[i][1], 8)
                      for i in range(max(0, len(pl) - 2), len(pl))})
    resistance = sorted({round(ph[i][1], 8)
                         for i in range(max(0, len(ph) - 2), len(ph))})

    last = closes[-1]
    res_levels = sorted(resistance)
    breakout = {"detected": False, "direction": None, "strength": None}
    if res_levels and last > res_levels[-1] * 1.001:
        breakout = {"detected": True, "direction": "UP",
                    "strength": round(clamp01((last / res_levels[-1] - 1) * 500), 4)}
    elif support and last < min(support) * 0.999:
        breakout = {"detected": True, "direction": "DOWN",
                    "strength": round(clamp01((min(support) / last - 1) * 500), 4)}

    return {
        "trend": trend,
        "structure": {"higher_high": bool(hh), "higher_low": bool(hl),
                      "lower_high": bool(lh), "lower_low": bool(ll)},
        "support_levels": support,
        "resistance_levels": res_levels,
        "breakout": breakout,
    }


# ------------------------------------------------- market regime (SS19)

def market_regime(candles: list, vol: dict) -> dict:
    closes = [float(c["close"]) for c in candles]
    if len(closes) < 30:
        return {"regime": MarketRegime.UNCERTAIN.value,
                "confidence": 0.0, "detected_at": None}
    e9 = ema(closes, 9) or closes[-1]
    e20 = ema(closes, 20) or closes[-1]
    e50 = ema(closes, 50) or closes[-1]
    slope = (closes[-1] - closes[-10]) / closes[-10] if closes[-10] else 0
    a_pct = vol.get("atr_percent") or 0

    if a_pct > 0.05:
        regime, conf = MarketRegime.HIGH_VOLATILITY.value, 0.7
    elif a_pct < 0.004:
        regime, conf = MarketRegime.LOW_VOLATILITY.value, 0.7
    elif e9 > e20 > e50 and slope > 0.002:
        regime, conf = MarketRegime.TRENDING_UP.value, min(0.95, 0.6 + abs(slope) * 20)
    elif e9 < e20 < e50 and slope < -0.002:
        regime, conf = MarketRegime.TRENDING_DOWN.value, min(0.95, 0.6 + abs(slope) * 20)
    elif abs(slope) < 0.001:
        regime, conf = MarketRegime.RANGING.value, 0.75
    else:
        regime, conf = MarketRegime.UNCERTAIN.value, 0.4

    from .utils import utc_now_iso
    return {"regime": regime, "confidence": round(conf, 4),
            "detected_at": utc_now_iso()}
