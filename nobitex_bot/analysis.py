"""Professional multi-timeframe decision engine — the analytical core.

The old decision layer looked at ONE timeframe (30m) and asked «is this
thing going up?». That question is why the bot bought the end of rallies:
a rising price is not an opportunity, it is the *result* of one. This
module answers the questions a discretionary analyst asks BEFORE risking
money, and it answers them from closed candles only:

    trend          EMA 9/20/50/200 stacking + slope + market structure
    candles        bodies, wicks, body/wick ratio, reversal & continuation
                   patterns, buying vs selling pressure, volume anomalies
    momentum       RSI / MACD histogram (level AND direction) / StochRSI /
                   ROC / bullish & bearish divergence
    volume         relative volume, OBV slope, up-vs-down volume, climax,
                   drying-up — a breakout without volume is not a breakout
    location       distance from EMA20 in ATR units, distance to the next
                   resistance, pullback depth, consolidation tightness
    structure      HH/HL vs LH/LL, support/resistance levels, confirmed
                   breakouts and failed (false) breakouts
    multi-frame    5m/15m/1h/4h/1d: HIGHER frames set the direction, LOWER
                   frames only time the entry — never the other way round

Two modes share this one engine but not the same strategy:

    SAFE  capital first. A buy needs >= 3 INDEPENDENT confirmations, a good
          entry location, low correction risk and a positive net edge after
          fees+slippage. No setup -> WAIT. Fixed stop/TP are allowed.
    RISK  hunt the market's best growth potential from HERE. Momentum and
          breakout setups are allowed earlier, but chasing an extended move
          is still vetoed. No fixed TP/SL: the position is held while the
          evidence holds and closed when the analysis flips.

Nothing here talks to the exchange, reads wall-clock time or mutates
state: every function is pure, so a backtest and the live bot run the
SAME code on the SAME inputs (that is the only way a backtest means
anything).
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

ENGINE_VERSION = "analysis/1.0"

# timeframe -> seconds per bar (used to detect the still-forming candle)
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400, "1d": 86400}

# higher frames decide the direction; lower frames only time the entry
DIRECTION_FRAMES = ("4h", "1d")
TRADE_FRAME = "1h"
ENTRY_FRAMES = ("15m", "5m")

MIN_CANDLES = {"5m": 60, "15m": 80, "1h": 120, "4h": 120, "1d": 80}

SAFE, RISK = "safe", "risk"

# factor weights (0..1, normalised at use). These are DEFAULTS; the live
# config can override them, and a backtest may tune them — but never the
# live parameters directly (see scripts/backtest_analysis.py).
SAFE_WEIGHTS = {
    "trend": 0.22,
    "structure": 0.18,
    "momentum": 0.16,
    "candles": 0.12,
    "volume": 0.14,
    "entry": 0.12,
    "relative_strength": 0.06,
}
RISK_WEIGHTS = {
    "trend": 0.16,
    "structure": 0.14,
    "momentum": 0.24,
    "candles": 0.10,
    "volume": 0.16,
    "entry": 0.10,
    "relative_strength": 0.10,
}


# --------------------------------------------------------------- helpers
def _f(x, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or abs(v) == float("inf"):
        return default
    return v


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _r(x: float, n: int = 4) -> float:
    return round(float(x), n)


def _tf_seconds(tf: str) -> float:
    return float(TF_SECONDS.get(str(tf), 1800))


def closed_candles(candles: Iterable, tf: str,
                   now_ts: Optional[float] = None) -> list:
    """Drop the candle that is still forming.

    A signal must be computable at the moment it is claimed: an unclosed
    candle's high/low/close change until the bar ends, so a «breakout»
    seen on it can vanish before the close. Every decision path uses this
    function; only monitoring code may look at the live bar.
    """
    out = []
    for c in candles or []:
        if not isinstance(c, dict):
            continue
        t = c.get("time") or c.get("timestamp") or c.get("t")
        if t is not None and now_ts is not None:
            try:
                t = float(t)
                # seconds vs milliseconds timestamps (both appear in the wild)
                if t > 1e12:
                    t /= 1000.0
                if t + _tf_seconds(tf) > float(now_ts):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(c)
    return out


def _ohlc(c: dict) -> tuple:
    return (_f(c.get("open")), _f(c.get("high")), _f(c.get("low")),
            _f(c.get("close")), _f(c.get("volume")))


def _valid_ohlc(c: dict) -> bool:
    o, h, l, cl, _v = _ohlc(c)
    return (h > 0 and l > 0 and cl > 0 and h >= l and
            l <= o <= h and l <= cl <= h)


def sanitize(candles: Iterable) -> list:
    """Keep only candles whose OHLC is internally consistent."""
    return [c for c in (candles or []) if _valid_ohlc(c)]


# ------------------------------------------------------------ indicators
def ema_series(values: list, period: int) -> list:
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
        out.append(e)
    return out


def ema(values: list, period: int) -> Optional[float]:
    s = ema_series(values, period)
    return s[-1] if s else None


def rsi_series(values: list, period: int = 14) -> list:
    if len(values) < period + 1:
        return []
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    ag, al = gains / period, losses / period
    out = []

    def _val(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out.append(_val(ag, al))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        ag = (ag * (period - 1) + gain) / period
        al = (al * (period - 1) + loss) / period
        out.append(_val(ag, al))
    return out


def macd_series(values: list) -> dict:
    if len(values) < 35:
        return {"line": [], "signal": [], "hist": []}
    fast, slow = ema_series(values, 12), ema_series(values, 26)
    line = [f - s for f, s in zip(fast, slow)]
    sig = ema_series(line, 9)
    offset = len(line) - len(sig)
    hist = [line[offset + i] - sig[i] for i in range(len(sig))]
    return {"line": line[offset:], "signal": sig, "hist": hist}


def atr_series(candles: list, period: int = 14) -> list:
    if len(candles) < period + 1:
        return []
    trs = []
    for i in range(1, len(candles)):
        _o, h, l, _c, _v = _ohlc(candles[i])
        prev_close = _f(candles[i - 1].get("close"))
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    out = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


def stochastic_k(candles: list, period: int = 14) -> list:
    if len(candles) < period:
        return []
    out = []
    for i in range(period, len(candles) + 1):
        window = candles[i - period:i]
        hh = max(_ohlc(c)[1] for c in window)
        ll = min(_ohlc(c)[2] for c in window)
        close = _ohlc(window[-1])[3]
        out.append(50.0 if hh == ll else (close - ll) / (hh - ll) * 100.0)
    return out


def obv_series(candles: list) -> list:
    out = [0.0]
    for i in range(1, len(candles)):
        c, p = _ohlc(candles[i])[3], _ohlc(candles[i - 1])[3]
        v = _ohlc(candles[i])[4]
        out.append(out[-1] + (v if c > p else -v if c < p else 0.0))
    return out


# -------------------------------------------------------- candle anatomy
def candle_shape(c: dict) -> dict:
    o, h, l, cl, _v = _ohlc(c)
    body = abs(cl - o)
    rng = max(h - l, 1e-12)
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    return {
        "bull": cl >= o,
        "body": body,
        "range": rng,
        "upper": upper,
        "lower": lower,
        "body_ratio": body / rng,
        "upper_ratio": upper / rng,
        "lower_ratio": lower / rng,
        # where the close sits inside the bar: 1 = at the high, 0 = at the low
        "close_pos": (cl - l) / rng,
        "open_pos": (o - l) / rng,
    }


def candle_pressure(candles: list, n: int = 5) -> float:
    """Buying pressure 0..1 over the last n bars (close position + body)."""
    window = candles[-n:]
    if not window:
        return 0.5
    total = 0.0
    weight = 0.0
    for i, c in enumerate(window):
        sh = candle_shape(c)
        w = 1.0 + i * 0.25          # recent bars matter more
        total += w * (0.7 * sh["close_pos"] + 0.3 * (1.0 if sh["bull"] else 0.0))
        weight += w
    return _clamp(total / weight) if weight else 0.5


def detect_patterns(candles: list) -> list:
    """Reversal / continuation patterns on the last three CLOSED bars."""
    out = []
    if len(candles) < 3:
        return out
    n = len(candles)
    bodies = [candle_shape(c)["body"] for c in candles[-20:]]
    avg_body = (sum(bodies) / len(bodies)) if bodies else 0.0
    avg_body = max(avg_body, 1e-12)
    for offset in (1, 2, 3):
        idx = n - offset
        if idx < 3:
            continue
        c = candles[idx]
        prev = candles[idx - 1]
        prev2 = candles[idx - 2]
        s, sp, sp2 = candle_shape(c), candle_shape(prev), candle_shape(prev2)
        o, h, l, cl, v = _ohlc(c)
        po, _ph, _pl, pc, _pv = _ohlc(prev)

        def add(name: str, direction: str, strength: float) -> None:
            out.append({"name": name, "direction": direction,
                        "strength": _r(_clamp(strength), 3),
                        "bars_ago": offset - 1})

        # Hammer / Shooting star: small body, long rejection wick
        if s["body_ratio"] <= 0.35 and s["lower_ratio"] >= 0.55 \
                and s["upper_ratio"] <= 0.20 and s["range"] > 0.6 * avg_body:
            add("HAMMER", "BULL", 0.55 + 0.35 * s["lower_ratio"])
        if s["body_ratio"] <= 0.35 and s["upper_ratio"] >= 0.55 \
                and s["lower_ratio"] <= 0.20 and s["range"] > 0.6 * avg_body:
            add("SHOOTING_STAR", "BEAR", 0.55 + 0.35 * s["upper_ratio"])
        # Inverted hammer: same shape as shooting star but AFTER a decline
        if s["body_ratio"] <= 0.35 and s["upper_ratio"] >= 0.55 \
                and s["lower_ratio"] <= 0.20:
            if len(candles) >= 6:
                prior = [_ohlc(x)[3] for x in candles[idx - 5:idx]]
                if prior and prior[0] > 0 and prior[-1] < prior[0]:
                    add("INVERTED_HAMMER", "BULL", 0.5 + 0.3 * s["upper_ratio"])
        # Doji: indecision, not a signal by itself
        if s["body_ratio"] <= 0.12:
            add("DOJI", "NEUTRAL", 0.3)
        # Marubozu: conviction bar
        if s["body_ratio"] >= 0.85 and s["body"] > 1.2 * avg_body:
            add("MARUBOZU", "BULL" if s["bull"] else "BEAR", 0.6)
        # Engulfing
        if s["bull"] and not sp["bull"] and cl >= po and o <= pc \
                and s["body"] > sp["body"]:
            add("BULLISH_ENGULFING", "BULL",
                0.6 + 0.3 * _clamp(s["body"] / max(sp["body"], 1e-12) - 1))
        if (not s["bull"]) and sp["bull"] and cl <= po and o >= pc \
                and s["body"] > sp["body"]:
            add("BEARISH_ENGULFING", "BEAR",
                0.6 + 0.3 * _clamp(s["body"] / max(sp["body"], 1e-12) - 1))
        # Morning / evening star (3-bar reversal)
        po2, _ph2, _pl2, pc2, _pv2 = _ohlc(prev2)
        if (not sp["bull"]) and sp["body_ratio"] <= 0.4 and s["bull"] \
                and cl > (po2 + pc2) / 2.0 and pc2 > po2:
            add("MORNING_STAR", "BULL", 0.75)
        if sp["bull"] and sp["body_ratio"] <= 0.4 and (not s["bull"]) \
                and cl < (po2 + pc2) / 2.0 and pc2 < po2:
            add("EVENING_STAR", "BEAR", 0.75)
        # Harami / inside bar: compression before a move
        if h <= _ohlc(prev)[1] and l >= _ohlc(prev)[2]:
            add("INSIDE_BAR", "NEUTRAL", 0.35)
        # Volume anomaly: a bar trading far above the recent average
        vols = [_ohlc(x)[4] for x in candles[max(0, idx - 20):idx]]
        if vols:
            avg_v = sum(vols) / len(vols)
            if avg_v > 0 and v >= 2.2 * avg_v:
                add("VOLUME_SPIKE", "BULL" if s["bull"] else "BEAR", 0.5)
    # de-duplicate, keep the strongest instance of each pattern
    best = {}
    for p in out:
        key = p["name"]
        if key not in best or p["strength"] > best[key]["strength"]:
            best[key] = p
    return sorted(best.values(), key=lambda p: -p["strength"])


def candle_analysis(candles: list) -> dict:
    """Anatomy + patterns + pressure for ONE timeframe."""
    if len(candles) < 10:
        return {"patterns": [], "pressure": 0.5, "bull_run": 0, "bear_run": 0,
                "upper_wick_cluster": False, "volume_anomaly": False,
                "avg_body_ratio": 0.0, "valid": False}
    patterns = detect_patterns(candles)
    shapes = [candle_shape(c) for c in candles[-20:]]
    bull_run = bear_run = 0
    for c in reversed(candles):
        if candle_shape(c)["bull"]:
            if bear_run:
                break
            bull_run += 1
        else:
            if bull_run:
                break
            bear_run += 1
    last3 = shapes[-3:]
    upper_cluster = sum(1 for s in last3 if s["upper_ratio"] >= 0.45) >= 2
    vols = [_ohlc(c)[4] for c in candles[-21:-1]]
    avg_v = (sum(vols) / len(vols)) if vols else 0.0
    last_v = _ohlc(candles[-1])[4]
    return {
        "patterns": patterns,
        "pressure": candle_pressure(candles, 5),
        "bull_run": bull_run,
        "bear_run": bear_run,
        "upper_wick_cluster": upper_cluster,
        "volume_anomaly": bool(avg_v > 0 and last_v >= 2.2 * avg_v),
        "avg_body_ratio": _r(sum(s["body_ratio"] for s in shapes) / len(shapes)),
        "valid": True,
    }


# ------------------------------------------------------------- structure
def swing_points(candles: list, look: int = 3) -> dict:
    highs, lows = [], []
    if len(candles) < look * 2 + 2:
        return {"highs": highs, "lows": lows}
    for i in range(look, len(candles) - look):
        h = _ohlc(candles[i])[1]
        l = _ohlc(candles[i])[2]
        window_h = [_ohlc(c)[1] for c in candles[i - look:i + look + 1]]
        window_l = [_ohlc(c)[2] for c in candles[i - look:i + look + 1]]
        if h >= max(window_h):
            highs.append((i, h))
        if l <= min(window_l):
            lows.append((i, l))
    return {"highs": highs[-6:], "lows": lows[-6:]}


def structure_analysis(candles: list) -> dict:
    """HH/HL vs LH/LL + levels + confirmed/failed breakouts."""
    if len(candles) < 30:
        return {"trend": "UNKNOWN", "valid": False, "support": [],
                "resistance": [], "breakout": {"detected": False},
                "higher_high": False, "higher_low": False,
                "lower_high": False, "lower_low": False,
                "dist_resistance_pct": None, "dist_support_pct": None}
    sp = swing_points(candles)
    highs, lows = sp["highs"], sp["lows"]
    hh = len(highs) >= 2 and highs[-1][1] > highs[-2][1]
    lh = len(highs) >= 2 and highs[-1][1] < highs[-2][1]
    hl = len(lows) >= 2 and lows[-1][1] > lows[-2][1]
    ll = len(lows) >= 2 and lows[-1][1] < lows[-2][1]

    if hh and hl:
        trend = "UPTREND"
    elif lh and ll:
        trend = "DOWNTREND"
    elif hh and ll:
        trend = "EXPANDING"
    else:
        trend = "RANGE"

    last_close = _ohlc(candles[-1])[3]
    resistance = sorted({_r(p, 8) for _i, p in highs[-3:]})
    support = sorted({_r(p, 8) for _i, p in lows[-3:]})
    above = [r for r in resistance if r > last_close]
    below = [s for s in support if s < last_close]
    next_res = min(above) if above else None
    next_sup = max(below) if below else None

    # A breakout is judged against the range that existed BEFORE the last
    # three bars: pivot-based levels alone miss a trap whose pivot has not
    # formed yet (a spike that closes back under the old high).
    breakout = {"detected": False, "direction": None, "confirmed": False,
                "failed": False, "strength": 0.0}
    if len(candles) >= 65:
        # the range is measured on the 50 bars BEFORE the probe window, so
        # a breakout that is still building over the last 10 bars is judged
        # against the level it actually broke, not against its own highs
        back = candles[-60:-10]
        prior_high = max(_ohlc(c)[1] for c in back)
        prior_low = min(_ohlc(c)[2] for c in back)
        last3 = candles[-10:]
        max_close = max(_ohlc(c)[3] for c in last3)
        min_close = min(_ohlc(c)[3] for c in last3)
        last_close3 = _ohlc(candles[-1])[3]
        if prior_high > 0 and max_close > prior_high * 1.001:
            if last_close3 > prior_high * 1.001:
                breakout = {"detected": True, "direction": "UP",
                            "confirmed": True, "failed": False,
                            "strength": _r(_clamp(
                                (last_close3 / prior_high - 1.0) * 120), 3),
                            "level": _r(prior_high, 8)}
            else:
                # cleared the level then closed back under it = bull trap
                breakout = {"detected": True, "direction": "UP",
                            "confirmed": False, "failed": True,
                            "strength": 0.0, "level": _r(prior_high, 8)}
        elif prior_low > 0 and min_close < prior_low * 0.999:
            breakout = {"detected": True, "direction": "DOWN",
                        "confirmed": last_close3 < prior_low * 0.999,
                        "failed": False,
                        "strength": _r(_clamp(
                            (prior_low / max(last_close3, 1e-12) - 1.0)
                            * 120), 3),
                        "level": _r(prior_low, 8)}
    # support already lost: the last close is under the prior 20-bar low
    support_broken = False
    if len(candles) >= 45:
        # a broken FLOOR means the 40-bar low is lost, not that the last
        # few bars dipped — a shallow pullback in an uptrend is healthy
        prior_floor = min(_ohlc(c)[2] for c in candles[-43:-3])
        support_broken = bool(prior_floor > 0 and
                              _ohlc(candles[-1])[3] < prior_floor * 0.999)

    # consolidation: tight range over the last 12 bars = coiling energy
    window = candles[-12:]
    hi = max(_ohlc(c)[1] for c in window)
    lo = min(_ohlc(c)[2] for c in window)
    tightness = _clamp(1.0 - (hi - lo) / max(last_close, 1e-12) / 0.10)

    return {
        "trend": trend,
        "valid": True,
        "higher_high": hh, "higher_low": hl,
        "lower_high": lh, "lower_low": ll,
        "support": support[-3:], "resistance": resistance[-3:],
        "next_resistance": next_res, "next_support": next_sup,
        "dist_resistance_pct": _r((next_res / last_close - 1.0) * 100, 3)
        if next_res else None,
        "dist_support_pct": _r((1.0 - next_sup / last_close) * 100, 3)
        if next_sup else None,
        "breakout": breakout,
        "support_broken": support_broken,
        "tightness": _r(tightness, 3),
    }


# ----------------------------------------------------------- trend state
def trend_analysis(candles: list) -> dict:
    closes = [_ohlc(c)[3] for c in candles]
    if len(closes) < 30:
        return {"valid": False, "align": "UNKNOWN", "strength": 0.0}
    e9, e20, e50, e200 = (ema(closes, 9), ema(closes, 20),
                          ema(closes, 50), ema(closes, 200))
    atr = atr_series(candles)
    atr_v = atr[-1] if atr else 0.0
    price = closes[-1]

    if e9 and e20 and e50 and e9 > e20 > e50:
        align, base = "BULL", 0.85
    elif e9 and e20 and e50 and e9 < e20 < e50:
        align, base = "BEAR", 0.15
    elif e9 and e20 and e9 > e20:
        align, base = "MIXED_UP", 0.6
    elif e9 and e20:
        align, base = "MIXED_DOWN", 0.4
    else:
        align, base = "UNKNOWN", 0.5

    slope = 0.0
    if len(closes) >= 11 and closes[-11] > 0:
        slope = (closes[-1] / closes[-11] - 1.0) * 100.0
    slope_s = _clamp(0.5 + slope / 8.0)
    strength = _clamp(0.6 * base + 0.4 * slope_s) if align != "UNKNOWN" else 0.5

    ext_atr = None
    if e20 and atr_v > 0 and price > 0:
        ext_atr = (price / e20 - 1.0) * e20 / atr_v

    return {
        "valid": True,
        "align": align,
        "strength": _r(strength, 3),
        "ema9": _r(e9, 8) if e9 else None,
        "ema20": _r(e20, 8) if e20 else None,
        "ema50": _r(e50, 8) if e50 else None,
        "ema200": _r(e200, 8) if e200 else None,
        "above_ema200": bool(e200 and price > e200),
        "slope_pct": _r(slope, 3),
        "extension_atr": _r(ext_atr, 3) if ext_atr is not None else None,
        "atr": _r(atr_v, 8),
        "atr_pct": _r(atr_v / price, 6) if atr_v and price else None,
    }


# -------------------------------------------------------- momentum state
def _divergence(candles: list, highs: list, lows: list,
                rsi_vals: list, rsi_offset: int) -> str:
    """Price/RSI divergence between the last two swings."""
    if not rsi_vals or len(candles) < 20:
        return "NONE"

    def rsi_at(idx: int) -> Optional[float]:
        j = idx - rsi_offset
        if 0 <= j < len(rsi_vals):
            return rsi_vals[j]
        return None

    h = [x for x in highs if rsi_at(x[0]) is not None]
    l = [x for x in lows if rsi_at(x[0]) is not None]
    if len(h) >= 2:
        (i1, p1), (i2, p2) = h[-2], h[-1]
        r1, r2 = rsi_at(i1), rsi_at(i2)
        if p2 > p1 and r2 is not None and r1 is not None and r2 < r1 - 1.5:
            return "BEARISH"
    if len(l) >= 2:
        (i1, p1), (i2, p2) = l[-2], l[-1]
        r1, r2 = rsi_at(i1), rsi_at(i2)
        if p2 < p1 and r2 is not None and r1 is not None and r2 > r1 + 1.5:
            return "BULLISH"
    return "NONE"


def momentum_analysis(candles: list) -> dict:
    closes = [_ohlc(c)[3] for c in candles]
    if len(closes) < 40:
        return {"valid": False, "score": 0.5, "divergence": "NONE"}
    rsi_all = rsi_series(closes, 14)
    rsi = rsi_all[-1] if rsi_all else None
    rsi_prev = rsi_all[-2] if len(rsi_all) >= 2 else rsi
    m = macd_series(closes)
    hist = m["hist"][-1] if m["hist"] else None
    hist_prev = m["hist"][-2] if len(m["hist"]) >= 2 else hist
    k_all = stochastic_k(candles, 14)
    k = k_all[-1] if k_all else None
    d = (sum(k_all[-3:]) / 3.0) if len(k_all) >= 3 else k
    roc = ((closes[-1] / closes[-10] - 1.0) * 100.0) if len(closes) > 10 \
        and closes[-10] else 0.0
    sp = swing_points(candles)
    rsi_offset = len(closes) - len(rsi_all)
    div = _divergence(candles, sp["highs"], sp["lows"], rsi_all, rsi_offset)

    score = 0.5
    if rsi is not None:
        if rsi >= 75:
            score -= 0.12          # overbought: not a buy by itself, but
        elif rsi >= 65:            # also not an automatic sell
            score += 0.02
        elif 50 <= rsi < 65:
            score += 0.14
        elif 42 <= rsi < 50:
            score += 0.04
        elif 30 <= rsi < 42:
            score -= 0.06
        else:
            score -= 0.12          # falling knife
        if rsi_prev is not None and rsi > rsi_prev:
            score += 0.05
        elif rsi_prev is not None and rsi < rsi_prev:
            score -= 0.05
    if hist is not None:
        score += 0.10 if hist > 0 else -0.10
        if hist_prev is not None:
            if hist > hist_prev and hist > 0:
                score += 0.05      # momentum accelerating
            elif hist < hist_prev and hist > 0:
                score -= 0.04      # momentum fading while still positive
            elif hist > hist_prev and hist < 0:
                score += 0.04      # bearish momentum fading = turn
    if k is not None and d is not None:
        if k > d and k < 80:
            score += 0.05
        elif k < d and k > 20:
            score -= 0.05
    score += _clamp(roc / 10.0, -0.12, 0.12)
    if div == "BULLISH":
        score += 0.07
    elif div == "BEARISH":
        score -= 0.12              # the classic «top» confirmation

    return {
        "valid": True,
        "score": _r(_clamp(score), 3),
        "rsi": _r(rsi, 2) if rsi is not None else None,
        "rsi_rising": bool(rsi is not None and rsi_prev is not None
                           and rsi > rsi_prev),
        "macd_hist": _r(hist, 8) if hist is not None else None,
        "macd_rising": bool(hist is not None and hist_prev is not None
                            and hist > hist_prev),
        "stoch_k": _r(k, 2) if k is not None else None,
        "stoch_d": _r(d, 2) if d is not None else None,
        "roc": _r(roc, 3),
        "divergence": div,
    }


# ----------------------------------------------------------- volume state
def volume_analysis(candles: list) -> dict:
    if len(candles) < 25:
        return {"valid": False, "score": 0.5, "rel_volume": None}
    vols = [_ohlc(c)[4] for c in candles]
    avg = sum(vols[-21:-1]) / 20.0
    rel = (vols[-1] / avg) if avg > 0 else None
    obv = obv_series(candles)
    obv_slope = 0.0
    if len(obv) >= 11:
        base = abs(obv[-11]) if abs(obv[-11]) > 1e-9 else 1.0
        obv_slope = (obv[-1] - obv[-11]) / base
    up_v = sum(_ohlc(c)[4] for c in candles[-10:] if candle_shape(c)["bull"])
    down_v = sum(_ohlc(c)[4] for c in candles[-10:]
                 if not candle_shape(c)["bull"])
    total = up_v + down_v
    buy_share = (up_v / total) if total > 0 else 0.5
    climax = bool(rel is not None and rel >= 2.5)
    drying = bool(rel is not None and rel <= 0.55)

    score = 0.5
    if rel is not None:
        score += _clamp((rel - 1.0) * 0.22, -0.2, 0.25)
    score += _clamp(obv_slope, -0.2, 0.2)
    score += (buy_share - 0.5) * 0.5
    if drying:
        score -= 0.08
    return {
        "valid": True,
        "score": _r(_clamp(score), 3),
        "rel_volume": _r(rel, 3) if rel is not None else None,
        "buy_volume_share": _r(buy_share, 3),
        "obv_slope": _r(obv_slope, 4),
        "climax": climax,
        "drying": drying,
    }


# ------------------------------------------------------------ one frame
def analyze_frame(candles: list, timeframe: str) -> dict:
    """Everything one timeframe can say, from CLOSED candles."""
    cs = sanitize(candles)
    min_needed = MIN_CANDLES.get(timeframe, 60)
    if len(cs) < min_needed:
        return {"timeframe": timeframe, "valid": False,
                "reason": "NOT_ENOUGH_CANDLES",
                "count": len(cs), "required": min_needed}
    trend = trend_analysis(cs)
    structure = structure_analysis(cs)
    momentum = momentum_analysis(cs)
    volume = volume_analysis(cs)
    candles_a = candle_analysis(cs)
    return {
        "timeframe": timeframe,
        "valid": bool(trend.get("valid") and structure.get("valid")
                      and momentum.get("valid")),
        "count": len(cs),
        "last_close": _r(_ohlc(cs[-1])[3], 8),
        "last_candle_time": (cs[-1].get("time") or cs[-1].get("timestamp")
                             or cs[-1].get("t")),
        "trend": trend,
        "structure": structure,
        "momentum": momentum,
        "volume": volume,
        "candles": candles_a,
    }


# ------------------------------------------------- multi-timeframe fusion
def analyze_timeframes(frames: dict) -> dict:
    """Fuse frames: direction from the high frames, timing from the low."""
    ok = {tf: f for tf, f in (frames or {}).items() if f and f.get("valid")}
    missing = sorted(set((frames or {}).keys()) - set(ok.keys()))
    direction_votes = []
    for tf in DIRECTION_FRAMES:
        f = ok.get(tf)
        if f:
            direction_votes.append(f["trend"]["strength"])
    trade = ok.get(TRADE_FRAME) or {}
    dir_strength = (sum(direction_votes) / len(direction_votes)
                    if direction_votes else 0.5)

    high_trend = "UP" if dir_strength >= 0.6 else (
        "DOWN" if dir_strength <= 0.4 else "NEUTRAL")
    trade_trend = (trade.get("trend") or {}).get("align", "UNKNOWN")
    structure_trend = (trade.get("structure") or {}).get("trend", "UNKNOWN")

    entry_frame = {}
    for tf in ENTRY_FRAMES:
        if tf in ok:
            entry_frame = ok[tf]
            break

    # alignment: high frames and the trade frame must not disagree
    aligned = (high_trend == "UP" and trade_trend in ("BULL", "MIXED_UP")) or \
              (high_trend == "DOWN" and trade_trend in ("BEAR", "MIXED_DOWN"))
    conflict = (high_trend == "UP" and trade_trend in ("BEAR",)) or \
               (high_trend == "DOWN" and trade_trend in ("BULL",))
    entry_pullback = False
    if trade:
        ext = (trade.get("trend") or {}).get("extension_atr")
        rsi = (trade.get("momentum") or {}).get("rsi")
        if high_trend == "UP" and ext is not None and ext <= 0.5:
            entry_pullback = True
        if high_trend == "UP" and rsi is not None and 40 <= rsi <= 62:
            entry_pullback = True

    return {
        "valid": bool(ok),
        "frames": {tf: {"valid": f.get("valid"),
                        "trend": (f.get("trend") or {}).get("align"),
                        "structure": (f.get("structure") or {}).get("trend"),
                        "rsi": (f.get("momentum") or {}).get("rsi"),
                        "momentum": (f.get("momentum") or {}).get("score"),
                        "volume": (f.get("volume") or {}).get("score")}
                   for tf, f in (frames or {}).items()},
        "available": sorted(ok.keys()),
        "missing": missing,
        "direction": high_trend,
        "direction_strength": _r(dir_strength, 3),
        "trade_trend": trade_trend,
        "structure_trend": structure_trend,
        "aligned": bool(aligned),
        "conflict": bool(conflict),
        "entry_pullback": bool(entry_pullback),
        "entry_frame": entry_frame.get("timeframe"),
    }


# --------------------------------------------------------------- scoring
def _avg(values: list) -> float:
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else 0.5


def score_components(mtf: dict, frames: dict, market: dict,
                     context: dict) -> dict:
    """Each factor as 0..1 — the numbers the dashboard shows and the
    decision consumes. No hidden magic: every term is one named signal."""
    trade = frames.get(TRADE_FRAME) or {}
    high = [frames.get(tf) or {} for tf in DIRECTION_FRAMES]
    entry = next((frames.get(tf) for tf in ENTRY_FRAMES
                  if (frames.get(tf) or {}).get("valid")), {}) or {}

    trend_s = _avg([(f.get("trend") or {}).get("strength") for f in high]
                   + [(trade.get("trend") or {}).get("strength")])
    struct = trade.get("structure") or {}
    structure_s = {"UPTREND": 0.9, "RANGE": 0.45, "EXPANDING": 0.4,
                   "DOWNTREND": 0.1, "UNKNOWN": 0.4}.get(
        struct.get("trend", "UNKNOWN"), 0.4)
    if struct.get("breakout", {}).get("confirmed") and \
            struct.get("breakout", {}).get("direction") == "UP":
        structure_s = min(1.0, structure_s + 0.1)
    if struct.get("breakout", {}).get("failed"):
        structure_s = max(0.0, structure_s - 0.25)

    momentum_s = _avg([(trade.get("momentum") or {}).get("score"),
                       (entry.get("momentum") or {}).get("score")])
    candle_a = trade.get("candles") or {}
    bull_p = sum(p["strength"] for p in candle_a.get("patterns", [])
                 if p["direction"] == "BULL")
    bear_p = sum(p["strength"] for p in candle_a.get("patterns", [])
                 if p["direction"] == "BEAR")
    candles_s = _clamp(0.5 + (bull_p - bear_p) * 0.30
                       + (candle_a.get("pressure", 0.5) - 0.5) * 0.6)
    volume_s = _avg([(trade.get("volume") or {}).get("score"),
                     (entry.get("volume") or {}).get("score")])
    liq = market.get("liquidity") or {}
    liq_s = _f(liq.get("score"), 0.0)
    dq = _f((market.get("data_quality") or {}).get("score"), 0.0)

    trend_d = (trade.get("trend") or {})
    ext = trend_d.get("extension_atr")
    atr_pct = _f(trend_d.get("atr_pct"), 0.01) or 0.01
    # entry location: near EMA20 / just above support is good, far above is
    # the «buying the top» case this engine exists to refuse
    entry_s = 0.5
    if ext is not None:
        if -1.0 <= ext <= 1.0:
            entry_s = 0.92
        elif 1.0 < ext <= 2.0:
            entry_s = 0.7
        elif 2.0 < ext <= 3.0:
            entry_s = 0.42
        elif ext > 3.0:
            entry_s = 0.15
        elif -2.0 <= ext < -1.0:
            entry_s = 0.6
        else:
            entry_s = 0.3
    dr = struct.get("dist_resistance_pct")
    if dr is not None:
        if dr >= 2.5:
            entry_s = min(1.0, entry_s + 0.05)
        elif dr < 0.7:
            entry_s = max(0.0, entry_s - 0.2)   # right under the ceiling
    if mtf.get("entry_pullback"):
        entry_s = min(1.0, entry_s + 0.05)

    # relative strength vs BTC and vs the market median (both are returns
    # over the SAME window, so the comparison is honest)
    chg = _f(market.get("change_24h"))
    btc = _f(context.get("btc_change_24h"))
    med = _f(context.get("market_median_change_24h"))
    rs = 0.5 + _clamp((chg - btc) / 12.0, -0.3, 0.3) \
        + _clamp((chg - med) / 20.0, -0.2, 0.2)
    rs_s = _clamp(rs)

    return {
        "trend": _r(_clamp(trend_s)),
        "structure": _r(_clamp(structure_s)),
        "momentum": _r(_clamp(momentum_s)),
        "candles": _r(candles_s),
        "volume": _r(_clamp(volume_s)),
        "entry": _r(_clamp(entry_s)),
        "liquidity": _r(_clamp(liq_s)),
        "relative_strength": _r(rs_s),
        "data_quality": _r(_clamp(dq)),
        "atr_pct": atr_pct,
        "extension_atr": ext,
    }


def correction_risk(mtf: dict, frames: dict, market: dict,
                    comps: dict) -> tuple:
    """0..1 probability-of-pullback proxy + the reasons behind it."""
    trade = frames.get(TRADE_FRAME) or {}
    trend = trade.get("trend") or {}
    mom = trade.get("momentum") or {}
    vol = trade.get("volume") or {}
    ca = trade.get("candles") or {}
    struct = trade.get("structure") or {}
    reasons = []
    risk = 0.0

    ext = trend.get("extension_atr")
    if ext is not None:
        if ext > 3.0:
            risk += 0.30
            reasons.append("PRICE_FAR_ABOVE_EMA20")
        elif ext > 2.0:
            risk += 0.18
            reasons.append("EXTENDED_FROM_EMA20")
    rsi = mom.get("rsi")
    if rsi is not None and rsi >= 72:
        risk += 0.14
        reasons.append("RSI_OVERBOUGHT")
    if mom.get("divergence") == "BEARISH":
        risk += 0.18
        reasons.append("BEARISH_DIVERGENCE")
    if ca.get("bull_run", 0) >= 4:
        risk += 0.10
        reasons.append("FOUR_CONSECUTIVE_BULL_CANDLES")
    if ca.get("upper_wick_cluster"):
        risk += 0.10
        reasons.append("UPPER_WICK_PRESSURE")
    if ca.get("volume_anomaly") and comps.get("candles", 0.5) < 0.5:
        risk += 0.06
        reasons.append("HIGH_VOLUME_WITHOUT_PROGRESS")
    if vol.get("climax"):
        risk += 0.12
        reasons.append("VOLUME_CLIMAX")
    dr = struct.get("dist_resistance_pct")
    if dr is not None and dr < 0.8 and not struct.get("breakout", {}).get("confirmed"):
        risk += 0.15
        reasons.append("AT_RESISTANCE_WITHOUT_BREAKOUT")
    if struct.get("breakout", {}).get("failed"):
        risk += 0.22
        reasons.append("FAILED_BREAKOUT")
    chg = _f(market.get("change_24h"))
    if chg >= 25:
        risk += 0.14
        reasons.append("ALREADY_UP_MORE_THAN_25PCT")
    elif chg >= 12:
        risk += 0.06
    if not mtf.get("aligned") and mtf.get("conflict"):
        risk += 0.10
        reasons.append("TIMEFRAME_CONFLICT")
    return _r(_clamp(risk), 3), reasons


def market_context_multiplier(context: dict) -> tuple:
    """How much the whole market helps or hurts this trade."""
    btc_dir = str(context.get("btc_trend") or "NEUTRAL").upper()
    breadth = context.get("breadth")
    reasons = []
    mult = 1.0
    if btc_dir in ("DOWN", "DOWNTREND"):
        mult -= 0.15
        reasons.append("BTC_DOWNTREND")
    elif btc_dir in ("UP", "UPTREND"):
        mult += 0.05
    if breadth is not None:
        if breadth < 0.35:
            mult -= 0.12
            reasons.append("MARKET_BREADTH_WEAK")
        elif breadth > 0.6:
            mult += 0.05
    btc_chg = _f(context.get("btc_change_24h"))
    if btc_chg <= -5:
        mult -= 0.10
        reasons.append("BTC_DUMPING")
    return _r(_clamp(mult, 0.6, 1.1), 3), reasons


def classify_setup(mtf: dict, frames: dict, comps: dict) -> str:
    trade = frames.get(TRADE_FRAME) or {}
    struct = trade.get("structure") or {}
    trend = trade.get("trend") or {}
    ca = trade.get("candles") or {}
    bo = struct.get("breakout") or {}
    ext = trend.get("extension_atr")
    high_up = mtf.get("direction") == "UP"
    bull_pattern = any(p["direction"] == "BULL"
                       for p in ca.get("patterns", []))

    if bo.get("detected") and bo.get("direction") == "DOWN":
        return "BREAKDOWN"
    if bo.get("failed"):
        return "FALSE_BREAKOUT"
    if ext is not None and ext > 3.0:
        return "OVEREXTENDED"
    if bo.get("confirmed") and bo.get("direction") == "UP":
        # volume is judged on the move as a whole (OBV + buy share + rel
        # volume), not on the retest bar: a breakout on volume followed by
        # a quiet pullback is the HEALTHY version of the pattern
        vol_score = _f((trade.get("volume") or {}).get("score"), 0.5)
        rel = _f((trade.get("volume") or {}).get("rel_volume"), 1.0)
        if vol_score >= 0.45 or rel >= 1.2:
            return "BREAKOUT_CONFIRMED"
        return "BREAKOUT_NO_VOLUME"
    if (struct.get("tightness") or 0) >= 0.6 and high_up:
        return "CONSOLIDATION"
    if high_up and mtf.get("entry_pullback") and bull_pattern:
        return "PULLBACK_ENTRY"
    if high_up and mtf.get("entry_pullback"):
        return "PULLBACK_FORMING"
    if high_up and struct.get("trend") == "UPTREND":
        return "TREND_CONTINUATION"
    if struct.get("trend") == "DOWNTREND":
        return "DOWNTREND"
    return "NEUTRAL"


def growth_score(comps: dict, weights: dict, corr_risk: float,
                 ctx_mult: float) -> float:
    wsum = sum(max(0.0, _f(v)) for v in weights.values()) or 1.0
    base = sum(max(0.0, _f(weights.get(k))) * _f(comps.get(k), 0.5)
               for k in weights) / wsum
    # correction risk is a PENALTY, never a bonus: the goal is to separate
    # «already ran» from «about to run»
    score = base * (1.0 - 0.55 * _clamp(corr_risk)) * ctx_mult
    return _r(_clamp(score) * 100.0, 2)


def count_confirmations(mtf: dict, frames: dict, comps: dict,
                        corr_risk: float) -> tuple:
    """Independent confirmations — the Safe mode's quorum."""
    trade = frames.get(TRADE_FRAME) or {}
    struct = trade.get("structure") or {}
    confs = []
    if mtf.get("direction") == "UP" and mtf.get("aligned"):
        confs.append("TREND_ALIGNED")
    if struct.get("trend") == "UPTREND":
        confs.append("HIGHER_HIGHS_LOWS")
    if _f((trade.get("momentum") or {}).get("score"), 0.5) >= 0.58:
        confs.append("MOMENTUM_POSITIVE")
    if _f((trade.get("volume") or {}).get("score"), 0.5) >= 0.55 \
            and _f((trade.get("volume") or {}).get("rel_volume"), 1.0) >= 0.9:
        confs.append("VOLUME_SUPPORTS")
    if any(p["direction"] == "BULL"
           for p in (trade.get("candles") or {}).get("patterns", [])):
        confs.append("BULLISH_CANDLE")
    if comps.get("entry", 0) >= 0.6:
        confs.append("GOOD_ENTRY_LOCATION")
    if corr_risk <= 0.35:
        confs.append("LOW_CORRECTION_RISK")
    return confs


# -------------------------------------------------------------- decisions
def _vetoes(mtf: dict, frames: dict, market: dict, corr_risk: float,
            mode: str) -> list:
    """Hard blocks. A veto is never averaged away by a good score."""
    out = []
    if not mtf.get("valid"):
        out.append("NO_VALID_TIMEFRAME")
    if TF_SECONDS and TRADE_FRAME not in (mtf.get("available") or []):
        out.append("TRADE_TIMEFRAME_MISSING")
    if "4h" not in (mtf.get("available") or []) and \
            "1d" not in (mtf.get("available") or []):
        out.append("NO_DIRECTION_TIMEFRAME")
    dq = _f((market.get("data_quality") or {}).get("score"), 0.0)
    if dq < 0.4:
        out.append("LOW_DATA_QUALITY")
    liq = market.get("liquidity") or {}
    if str(liq.get("status") or "").upper() in ("CRITICAL", "UNKNOWN", ""):
        out.append("LIQUIDITY_UNTRADABLE")
    if _f(liq.get("score"), 0.0) < 0.30:
        out.append("LIQUIDITY_SCORE_LOW")
    spread = _f(market.get("spread_percent"))
    atr_pct = _f((frames.get(TRADE_FRAME) or {}).get("trend", {})
                 .get("atr_pct"), 0.01) or 0.01
    spread_cap = max(1.5, atr_pct * 100 * 0.5)
    if spread > spread_cap:
        out.append("WIDE_SPREAD")
    struct = (frames.get(TRADE_FRAME) or {}).get("structure") or {}
    if struct.get("breakout", {}).get("direction") == "DOWN" and \
            struct.get("breakout", {}).get("confirmed"):
        out.append("BREAKDOWN_IN_PROGRESS")
    if corr_risk >= (0.75 if mode == RISK else 0.62):
        out.append("CORRECTION_RISK_HIGH")
    ca = (frames.get(TRADE_FRAME) or {}).get("candles") or {}
    # Four red bars alone are NOT distribution: a quiet retest after a
    # breakout is exactly four or five small red bars. It becomes selling
    # pressure when the floor is lost or the move is already extended.
    if ca.get("bear_run", 0) >= 4 and (struct.get("support_broken")
                                       or corr_risk >= 0.30):
        out.append("SELLING_PRESSURE")
    if (frames.get(TRADE_FRAME) or {}).get("volume", {}).get("drying") and \
            mode == SAFE:
        out.append("VOLUME_DRYING_UP")
    return out


def decide(symbol: str, frames: dict, market: dict,
           context: dict, mode: str = SAFE,
           weights: Optional[dict] = None,
           thresholds: Optional[dict] = None,
           cost_pct: float = 0.5) -> dict:
    """The single entry decision used by BOTH modes and by the backtest.

    cost_pct: round-trip cost (fees + expected slippage) in PERCENT — the
    trade must clear it or it is not a trade.
    """
    mode = RISK if str(mode).lower() == RISK else SAFE
    weights = weights or (RISK_WEIGHTS if mode == RISK else SAFE_WEIGHTS)
    th = {
        "min_growth": 62.0 if mode == SAFE else 55.0,
        "min_confirmations": 3 if mode == SAFE else 2,
        "min_entry": 55.0 if mode == SAFE else 40.0,
        "max_correction": 0.45 if mode == SAFE else 0.62,
        "min_rr": 1.3 if mode == SAFE else 1.0,
    }
    th.update({k: v for k, v in (thresholds or {}).items() if v is not None})

    mtf = analyze_timeframes(frames)
    comps = score_components(mtf, frames, market, context)
    corr, corr_reasons = correction_risk(mtf, frames, market, comps)
    ctx_mult, ctx_reasons = market_context_multiplier(context)
    growth = growth_score(comps, weights, corr, ctx_mult)
    confs = count_confirmations(mtf, frames, comps, corr)
    setup = classify_setup(mtf, frames, comps)
    vetoes = _vetoes(mtf, frames, market, corr, mode)

    # economics: expected move vs the distance to the nearest real obstacle
    trade = frames.get(TRADE_FRAME) or {}
    struct = trade.get("structure") or {}
    atr_pct = _f((trade.get("trend") or {}).get("atr_pct"), 0.01) or 0.01
    # Targets: the nearer of «the next real resistance» and «3.2 ATR of
    # travel» — a ceiling that sits 0.4% away would otherwise make every
    # trade look like negative EV. Costs are charged on both sides so the
    # ratio is a NET ratio, which is the only one worth comparing to 1.
    atr_units = atr_pct * 100.0
    dr = struct.get("dist_resistance_pct")
    target_pct = max(dr if (dr is not None and dr > 0) else 0.0,
                     atr_units * 3.2)
    risk_pct = max(atr_units * 1.2, cost_pct * 2.0)
    rr = (target_pct - cost_pct) / max(risk_pct + cost_pct * 0.5, 1e-9)

    reasons_for, reasons_against = [], []
    if comps["trend"] >= 0.6:
        reasons_for.append("TREND_UP")
    if comps["structure"] >= 0.6:
        reasons_for.append("STRUCTURE_INTACT")
    if comps["momentum"] >= 0.6:
        reasons_for.append("MOMENTUM_UP")
    if comps["volume"] >= 0.6:
        reasons_for.append("VOLUME_CONFIRMS")
    if comps["entry"] >= 0.6:
        reasons_for.append("ENTRY_LOCATION_OK")
    if comps["relative_strength"] >= 0.6:
        reasons_for.append("RELATIVE_STRENGTH")
    if setup in ("BREAKOUT_CONFIRMED", "PULLBACK_ENTRY", "CONSOLIDATION"):
        reasons_for.append("SETUP_" + setup)
    reasons_against.extend(corr_reasons)
    reasons_against.extend(ctx_reasons)
    if comps["candles"] < 0.45:
        reasons_against.append("BEARISH_CANDLES")
    if not mtf.get("aligned"):
        reasons_against.append("TIMEFRAMES_NOT_ALIGNED")

    blocked = list(vetoes)
    if growth < th["min_growth"]:
        blocked.append("GROWTH_SCORE_BELOW_MIN")
    if corr > th["max_correction"]:
        blocked.append("CORRECTION_RISK_ABOVE_MAX")
    if len(confs) < int(th["min_confirmations"]):
        blocked.append("NOT_ENOUGH_CONFIRMATIONS")
    if comps["entry"] * 100 < th["min_entry"]:
        blocked.append("ENTRY_QUALITY_LOW")
    if rr < th["min_rr"]:
        blocked.append("RISK_REWARD_TOO_LOW")
    if setup in ("OVEREXTENDED", "BREAKDOWN", "FALSE_BREAKOUT"):
        blocked.append("SETUP_" + setup)
    if mode == RISK and setup == "DOWNTREND":
        blocked.append("SETUP_DOWNTREND")

    # entry/exit coherence: never buy a chart that the exit policy would
    # already be selling out of on the next bar — such a trade just pays
    # the round trip to prove the engine contradicts itself
    pre = exit_analysis({}, frames, market, context, mode=mode)
    if pre.get("exit"):
        blocked.append("EXIT_CONDITIONS_ALREADY_TRUE")

    allow = not blocked
    confidence = _clamp(0.5 * (growth / 100.0)
                        + 0.3 * (len(confs) / 7.0)
                        + 0.2 * (1.0 - corr))
    if blocked:
        confidence *= 0.55

    return {
        "engine": ENGINE_VERSION,
        "symbol": symbol,
        "mode": mode,
        "action": "BUY" if allow else "AVOID",
        "allow_buy": allow,
        "setup": setup,
        "confidence": _r(_clamp(confidence), 4),
        "growth_score": growth,
        "entry_score": _r(comps["entry"] * 100, 2),
        "correction_risk": _r(corr * 100, 2),
        "risk_reward": _r(rr, 3),
        "target_pct": _r(target_pct, 3),
        "risk_pct": _r(risk_pct, 3),
        "cost_pct": _r(cost_pct, 3),
        "components": {k: v for k, v in comps.items()
                       if k not in ("atr_pct", "extension_atr")},
        "atr_pct": comps.get("atr_pct"),
        "extension_atr": comps.get("extension_atr"),
        "confirmations": confs,
        "confirmations_count": len(confs),
        "reasons_for": reasons_for,
        "reasons_against": reasons_against,
        "vetoes": blocked,
        "timeframes": mtf,
        "direction": mtf.get("direction"),
        "structure_trend": mtf.get("structure_trend"),
        "support": struct.get("support") or [],
        "resistance": struct.get("resistance") or [],
        "market_context": {
            "btc_trend": context.get("btc_trend"),
            "btc_change_24h": context.get("btc_change_24h"),
            "breadth": context.get("breadth"),
            "multiplier": ctx_mult,
        },
        "last_closed_candle": (trade.get("last_candle_time")),
        "data_valid": bool(mtf.get("valid") and not vetoes),
        "thresholds": th,
    }


# ------------------------------------------------------------------ exits
def exit_analysis(position: dict, frames: dict, market: dict,
                  context: dict, mode: str = RISK,
                  entry_price: float = 0.0) -> dict:
    """Should this position be closed NOW? Analysis, not a fixed percent.

    A single red candle is explicitly NOT a reason to sell: the triggers
    below are structural (broken support / lost EMA / momentum flip /
    distribution volume) and each one names itself in the reasons.
    """
    reasons = []
    trade = frames.get(TRADE_FRAME) or {}
    if not trade.get("valid"):
        return {"exit": False, "reasons": ["NO_DATA"], "urgent": False}
    trend = trade.get("trend") or {}
    mom = trade.get("momentum") or {}
    struct = trade.get("structure") or {}
    vol = trade.get("volume") or {}
    ca = trade.get("candles") or {}
    price = _f(market.get("last_price")) or _f(trade.get("last_close"))
    mtf = analyze_timeframes(frames)

    if struct.get("support_broken") or struct.get("trend") == "DOWNTREND" \
            or (struct.get("lower_low") and struct.get("lower_high")):
        reasons.append("STRUCTURE_BROKEN")
    if trend.get("align") == "BEAR":
        reasons.append("EMA_STACK_BEARISH")
    if mom.get("macd_hist") is not None and mom["macd_hist"] < 0 \
            and mom.get("rsi") is not None and mom["rsi"] < 50:
        reasons.append("MOMENTUM_FLIPPED")
    if mom.get("divergence") == "BEARISH":
        reasons.append("BEARISH_DIVERGENCE")
    if vol.get("climax") and ca.get("upper_wick_cluster"):
        reasons.append("DISTRIBUTION_VOLUME")
    if ca.get("bear_run", 0) >= 3 and vol.get("buy_volume_share", 0.5) < 0.4:
        reasons.append("SELLING_PRESSURE")
    if mtf.get("direction") == "DOWN" and mtf.get("conflict"):
        reasons.append("HIGH_FRAME_TURNED_DOWN")
    if str(context.get("btc_trend") or "").upper() in ("DOWN", "DOWNTREND") \
            and _f(context.get("btc_change_24h")) <= -5:
        reasons.append("BTC_MARKET_WIDE_DROP")
    if _f(vol.get("rel_volume"), 1.0) <= 0.5 and trend.get("strength", 0.5) < 0.45:
        reasons.append("MOMENTUM_DEAD")

    # a single weak signal is noise; two independent ones are a decision
    urgent = ("STRUCTURE_BROKEN" in reasons and "MOMENTUM_FLIPPED" in reasons) \
        or "DISTRIBUTION_VOLUME" in reasons or "BEARISH_DIVERGENCE" in reasons
    enough = len(reasons) >= (2 if mode == RISK else 2)
    if mode != RISK:
        # safe mode keeps its bracket exits; analysis-exit only escalates
        enough = urgent or len(reasons) >= 3

    pnl_pct = None
    if entry_price and price:
        pnl_pct = (price / entry_price - 1.0) * 100.0

    return {
        "exit": bool(reasons) and enough,
        "urgent": bool(urgent),
        "reasons": reasons,
        "pnl_pct": _r(pnl_pct, 3) if pnl_pct is not None else None,
        "trend": trend.get("align"),
        "structure": struct.get("trend"),
        "rsi": mom.get("rsi"),
        "divergence": mom.get("divergence"),
        "checked_at": trade.get("last_candle_time"),
    }


# --------------------------------------------------------------- ranking
def rank(scored: list, top_n: int = 5) -> list:
    """Cross-sectional ranking: absolute scores are not comparable across
    markets, percentiles are. This is what stops «+30% today» from
    automatically winning over «+5% and breaking out now»."""
    scored = [s for s in scored or [] if s.get("growth_score") is not None]
    if not scored:
        return []
    ordered = sorted(scored, key=lambda s: -_f(s.get("growth_score")))
    n = len(ordered)
    for i, s in enumerate(ordered, 1):
        s["rank"] = i
        s["percentile"] = _r(1.0 - (i - 1) / n, 3)
    return ordered[:max(1, top_n)]
