"""Forward projection of the multi-timeframe state — «where next?».

The decision engine answers «is this a good buy NOW». This module answers
the question asked before every entry: «if I buy, where is this likely to
be in 100/200/500 bars?». It is deliberately NOT a machine-learning model
and makes no promise: it turns the evidence that `analysis.py` already
extracted from CLOSED candles (trend stack, momentum, structure, volume)
into

    * a drift, expressed in ATR per bar,
    * an 80% band that widens with the square root of the horizon,
    * a probability that the horizon return is positive,
    * an expected NET return after the round-trip cost.

Everything is a pure function of the frames, so live and backtest compute
the exact same numbers from the same candles, and no future candle can
leak into the projection (the inputs are already closed-candle snapshots).
"""
from __future__ import annotations

import math
from typing import Optional

# timeframes and how much each is trusted for DIRECTION. Lower frames only
# time entries, so they get a small weight; the trade frame is where the
# position is actually held.
FRAME_WEIGHTS = {"5m": 0.10, "15m": 0.15, "1h": 0.30, "4h": 0.25, "1d": 0.20}
TRADE_FRAME = "1h"
DEFAULT_HORIZONS = (100, 200, 500)
Z80 = 1.2816                 # two-sided 80% normal band
MAX_BAND_PCT = 60.0
MAX_MOVE_PCT = 45.0
DRIFT_ATR_PER_BAR = 0.03     # full-strength trend: 3% of one ATR per bar


def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _align_sign(align) -> float:
    text = str(align or "").upper()
    if text in ("BULL", "UPTREND"):
        return 1.0
    if text == "MIXED_UP":
        return 0.5
    if text == "MIXED_DOWN":
        return -0.5
    if text in ("BEAR", "DOWNTREND"):
        return -1.0
    return 0.0


def _frame_score(frame: dict) -> float:
    """-1..1 lean of ONE timeframe."""
    if not frame or not frame.get("valid"):
        return 0.0
    trend = frame.get("trend") or {}
    mom = frame.get("momentum") or {}
    struct = frame.get("structure") or {}
    vol = frame.get("volume") or {}

    trend_s = _align_sign(trend.get("align")) * _clamp(
        _f(trend.get("strength"), 0.5))
    ext = trend.get("extension_atr")
    if ext is not None:
        far = abs(_f(ext))
        if far > 3.0:
            trend_s *= 0.55     # stretched: continuation is less likely
        elif far > 2.0:
            trend_s *= 0.80

    mom_s = (_clamp(_f(mom.get("score"), 0.5)) - 0.5) * 2.0
    if mom.get("divergence") == "BEARISH":
        mom_s -= 0.25
    elif mom.get("divergence") == "BULLISH":
        mom_s += 0.25

    struct_s = {"UPTREND": 1.0, "DOWNTREND": -1.0,
                "EXPANDING": 0.30}.get(str(struct.get("trend")), 0.0)
    breakout = struct.get("breakout") or {}
    if breakout.get("confirmed"):
        struct_s += 0.35 if breakout.get("direction") == "UP" else -0.35
    if breakout.get("failed"):
        struct_s -= 0.30
    if struct.get("support_broken"):
        struct_s -= 0.40

    vol_s = (_clamp(_f(vol.get("score"), 0.5)) - 0.5) * 2.0
    share = vol.get("buy_volume_share")
    if share is not None:
        vol_s += (_clamp(_f(share, 0.5)) - 0.5) * 0.6
    if vol.get("drying"):
        vol_s *= 0.5

    return _clamp(0.50 * trend_s + 0.25 * mom_s
                  + 0.15 * _clamp(struct_s, -1.0, 1.0)
                  + 0.10 * _clamp(vol_s, -1.0, 1.0), -1.0, 1.0)


def _blend(frames: dict) -> float:
    """Weighted lean of every VALID timeframe, -1..1."""
    total = weight = 0.0
    for tf, w in FRAME_WEIGHTS.items():
        frame = (frames or {}).get(tf) or {}
        if not frame.get("valid"):
            continue
        total += w * _frame_score(frame)
        weight += w
    return (total / weight) if weight else 0.0


def _atr_pct(frames: dict, trade_frame: str = TRADE_FRAME) -> float:
    """Volatility in return units: the trade-frame ATR, else the median."""
    trade = (frames or {}).get(trade_frame) or {}
    atr = _f((trade.get("trend") or {}).get("atr_pct"))
    if atr > 0:
        return atr
    values = [_f((f or {}).get("trend", {}).get("atr_pct"))
              for f in (frames or {}).values() if (f or {}).get("valid")]
    values = sorted(v for v in values if v > 0)
    if not values:
        return 0.01
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def _drivers(frames: dict, trade_frame: str = TRADE_FRAME) -> list:
    """Human-readable evidence behind the projection (for the decision log)."""
    out = []
    trade = (frames or {}).get(trade_frame) or {}
    trend = trade.get("trend") or {}
    mom = trade.get("momentum") or {}
    struct = trade.get("structure") or {}
    vol = trade.get("volume") or {}
    align = str(trend.get("align") or "").upper()
    if align in ("BULL", "MIXED_UP"):
        out.append("TREND_UP")
    elif align in ("BEAR", "MIXED_DOWN"):
        out.append("TREND_DOWN")
    score = _f(mom.get("score"), 0.5)
    if score >= 0.58:
        out.append("MOMENTUM_UP")
    elif score <= 0.42:
        out.append("MOMENTUM_DOWN")
    if mom.get("divergence") == "BEARISH":
        out.append("BEARISH_DIVERGENCE")
    elif mom.get("divergence") == "BULLISH":
        out.append("BULLISH_DIVERGENCE")
    if struct.get("support_broken"):
        out.append("SUPPORT_BROKEN")
    if (struct.get("breakout") or {}).get("confirmed"):
        out.append("BREAKOUT_CONFIRMED")
    if vol.get("drying"):
        out.append("VOLUME_DRYING")
    if vol.get("climax"):
        out.append("VOLUME_CLIMAX")
    return out or ["NO_CLEAR_EDGE"]


def _horizon(score: float, atr: float, bars: int, cost_pct: float) -> dict:
    per_bar = DRIFT_ATR_PER_BAR * atr * score
    raw = (1.0 + per_bar) ** bars - 1.0
    expected = _clamp(raw * 100.0, -MAX_MOVE_PCT, MAX_MOVE_PCT)
    sigma = _clamp(atr * math.sqrt(bars) * 100.0, 0.0, 35.0)
    band = min(MAX_BAND_PCT, Z80 * sigma)
    # a longer horizon gives the same edge more room to play out, so the
    # probability sharpens as sqrt(bars/200) instead of staying flat
    z = 2.0 * score * math.sqrt(bars / 200.0)
    prob = _clamp(1.0 / (1.0 + math.exp(-z)), 0.05, 0.95)
    net = expected - cost_pct
    if prob >= 0.55 and net > 0:
        direction = "UP"
    elif prob <= 0.45:
        direction = "DOWN"
    else:
        direction = "SIDEWAYS"
    return {
        "horizon_bars": int(bars),
        "direction": direction,
        "probability_up": round(prob, 4),
        "expected_return_pct": round(expected, 3),
        "expected_net_pct": round(net, 3),
        "band_pct": round(band, 3),
        "range_low_pct": round(expected - band, 3),
        "range_high_pct": round(expected + band, 3),
    }


def project(frames: dict, horizon_bars: int = 200,
            cost_pct: float = 0.0,
            trade_frame: str = TRADE_FRAME,
            horizons: Optional[tuple] = None) -> dict:
    """Probabilistic projection of this market at 100/200/500 bars.

    Returns the requested horizon at the top level (so `decide` can veto on
    `direction`/`probability_up`) plus the full table under `horizons`.
    """
    frames = frames or {}
    trade_frame = str(trade_frame or TRADE_FRAME)
    try:
        horizon_bars = max(1, int(horizon_bars or 200))
    except (TypeError, ValueError):
        horizon_bars = 200
    wanted = set()
    for h in tuple(horizons or DEFAULT_HORIZONS) + (horizon_bars,):
        try:
            if int(h) > 0:
                wanted.add(int(h))
        except (TypeError, ValueError):
            continue
    wanted = sorted(wanted or {200})
    score = _blend(frames)
    atr = _atr_pct(frames, trade_frame)
    table = {h: _horizon(score, atr, h, cost_pct) for h in wanted}
    main = dict(table.get(horizon_bars)
                or _horizon(score, atr, horizon_bars, cost_pct))
    main["score"] = round(score, 4)
    main["atr_pct"] = round(atr, 6)
    main["drivers"] = _drivers(frames, trade_frame)
    main["horizons"] = {str(h): table[h] for h in wanted}
    main["note"] = ("probabilistic 80% band from closed-candle evidence; "
                    "not a price target and not a guarantee")
    return main
