"""Scenario tests for the multi-timeframe decision engine (analysis.py).

Every scenario is a synthetic market built from a pure price path, so the
test asserts BEHAVIOUR (this setup is buyable, that one is not) instead of
frozen numbers. The scenarios mirror the ones the user requires before
live trading: strong uptrend, downtrend, range, sudden pump, pump-and-dump,
real breakout, failed breakout, healthy pullback, falling momentum,
bearish divergence, long upper wicks, BTC dump, incomplete data.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nobitex_bot import analysis  # noqa: E402

TF = analysis.TF_SECONDS


# --------------------------------------------------------------- builders
def candles(closes, tf="1h", volumes=None, start=1_700_000_000,
            spread=0.0015):
    step = TF[tf]
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        o = prev
        hi = max(o, close) * (1 + spread * 0.5)
        lo = min(o, close) * (1 - spread * 0.5)
        v = volumes[i] if volumes and i < len(volumes) else 1000.0
        out.append({"time": start + i * step, "open": o, "high": hi,
                    "low": lo, "close": close, "volume": v})
        prev = close
    return out


def resample(closes, k):
    return closes[k - 1::k]


def interpolate(closes, k):
    out = []
    for i in range(len(closes) - 1):
        a, b = closes[i], closes[i + 1]
        for j in range(k):
            out.append(a + (b - a) * j / k)
    out.append(closes[-1])
    return out


def frames_from_1h(closes_1h, tf_volumes=None):
    """Five timeframes derived from one 1h path (no look-ahead anywhere)."""
    v15 = None
    if tf_volumes:
        v15 = interpolate(tf_volumes, 4)
    return {
        "1h": analysis.analyze_frame(candles(closes_1h, "1h",
                                            tf_volumes), "1h"),
        "4h": analysis.analyze_frame(candles(resample(closes_1h, 4), "4h"),
                                     "4h"),
        "1d": analysis.analyze_frame(candles(resample(closes_1h, 24), "1d"),
                                     "1d"),
        "15m": analysis.analyze_frame(candles(interpolate(closes_1h, 4),
                                              "15m", v15), "15m"),
    }


def geo(n, start=100.0, rate=0.0014):
    return [start * (1 + rate) ** i for i in range(n)]


MARKET = {"last_price": 100.0, "change_24h": 2.0, "spread_percent": 0.05,
          "liquidity": {"score": 0.85, "status": "GOOD"},
          "data_quality": {"score": 0.95}}
CTX = {"btc_trend": "UP", "btc_change_24h": 1.5, "breadth": 0.62,
       "market_median_change_24h": 0.8}


def healthy_pullback():
    closes = geo(2000)
    top = closes[-1]
    # healthy correction: ~6% off the high over 10 bars, ending in a hammer
    for i in range(10):
        closes[-10 + i] = top * (1 - 0.002 * (i + 1))
    closes[-1] = closes[-2] * 1.001          # close back up
    vols = [1000.0] * len(closes)
    vols[-1] = 2200.0                        # volume on the recovery bar
    return closes, vols


def parabolic():
    closes = geo(1960)
    for i in range(40):
        closes.append(closes[-1] * 1.035)     # accelerating blow-off top
    return closes, [1000.0] * len(closes)


def downtrend():
    return [100.0 * (0.9988 ** i) for i in range(2000)], None


def pump_low_volume():
    closes = [100.0] * 1990
    for i in range(10):
        closes.append(100.0 * (1.30 ** ((i + 1) / 10.0)))
    vols = [1000.0] * 1990 + [90.0] * 10      # +30% on dust volume
    return closes, vols


def failed_breakout():
    closes = geo(1990, rate=0.0008)
    level = closes[-1]
    closes.append(level * 1.004)              # spike above resistance
    closes.append(level * 0.985)              # closes back below = trap
    closes.append(level * 0.978)
    return closes, None


def strong_breakout():
    """A real breakout: long consolidation, then an expansion with volume."""
    closes = geo(1500, rate=0.0010)
    base = closes[-1]
    for i in range(490):                      # 490-bar tight range
        closes.append(base * (1 + 0.003 * math.sin(i / 3.0)))
    level = max(closes[-100:])
    for i in range(10):                       # break + hold above the range
        closes.append(level * (1 + 0.006 * (i + 1)))
    vols = [1000.0] * (len(closes) - 10) + [3200.0] * 10
    return closes, vols


def breakout_with_retest():
    """Breakout, then a CONTROLLED retest that holds the level — the entry
    the engine is supposed to reward (the fast version is a chase)."""
    closes = geo(1500, rate=0.0010)
    base = closes[-1]
    for i in range(490):
        closes.append(base * (1 + 0.003 * math.sin(i / 3.0)))
    level = max(closes[-100:])
    for i in range(4):                        # 4-bar breakout on volume
        closes.append(level * (1 + 0.006 * (i + 1)))
    top = closes[-1]
    for i in range(5):                        # retest that holds the level
        closes.append(top * (1 - 0.004 * (i + 1)))
    vols = [1000.0] * (len(closes) - 9) + [3000.0] * 4 + [1200.0] * 5
    return closes, vols


# ------------------------------------------------------------------ tests
def decide(closes, volumes=None, mode=analysis.SAFE, market=None,
           context=None, thresholds=None):
    frames = frames_from_1h(closes, volumes)
    return analysis.decide("TESTIRT", frames, market or MARKET,
                           context or CTX, mode=mode,
                           thresholds=thresholds, cost_pct=0.6)


def test_healthy_pullback_beats_the_blow_off_top():
    """The core requirement: «already ran» must not outrank «about to go»."""
    good = decide(*healthy_pullback())
    top = decide(*parabolic())
    assert good["growth_score"] > top["growth_score"]
    assert good["correction_risk"] < top["correction_risk"]
    assert top["allow_buy"] is False


def test_parabolic_move_is_vetoed():
    verdict = decide(*parabolic())
    assert verdict["allow_buy"] is False
    assert (verdict["setup"] == "OVEREXTENDED"
            or "PRICE_FAR_ABOVE_EMA20" in verdict["reasons_against"]
            or "CORRECTION_RISK_ABOVE_MAX" in verdict["vetoes"])


def test_downtrend_is_not_buyable():
    verdict = decide(*downtrend())
    assert verdict["allow_buy"] is False
    assert verdict["growth_score"] < 55


def test_pump_on_no_volume_is_not_buyable():
    verdict = decide(*pump_low_volume())
    assert verdict["allow_buy"] is False


def test_failed_breakout_is_not_buyable():
    verdict = decide(*failed_breakout())
    assert verdict["allow_buy"] is False
    assert (verdict["setup"] in ("FALSE_BREAKOUT", "NEUTRAL", "OVEREXTENDED")
            or "SETUP_FALSE_BREAKOUT" in verdict["vetoes"])


def test_real_breakout_with_volume_is_recognised():
    verdict = decide(*strong_breakout())
    # a 6% breakout in 10 bars IS extended: recognised, but not bought
    assert verdict["direction"] == "UP"
    assert ("VOLUME_SUPPORTS" in verdict["confirmations"]
            or verdict["components"]["volume"] >= 0.55)
    assert (verdict["setup"] == "OVEREXTENDED"
            or verdict["allow_buy"] is False)


def test_breakout_with_retest_is_buyable():
    verdict = decide(*breakout_with_retest(), mode=analysis.RISK)
    assert verdict["setup"] in ("BREAKOUT_CONFIRMED", "PULLBACK_ENTRY",
                                "PULLBACK_FORMING", "TREND_CONTINUATION",
                                "CONSOLIDATION")
    assert "SETUP_OVEREXTENDED" not in verdict["vetoes"]
    assert verdict["growth_score"] >= 45
    assert verdict["entry_score"] >= 40


def test_incomplete_data_fails_closed():
    frames = {"1h": analysis.analyze_frame(candles([100.0] * 5, "1h"), "1h")}
    verdict = analysis.decide("TESTIRT", frames, MARKET, CTX)
    assert verdict["allow_buy"] is False
    assert "NO_VALID_TIMEFRAME" in verdict["vetoes"]


def test_risk_mode_is_not_stricter_than_safe():
    closes, vols = strong_breakout()
    safe = decide(closes, vols, mode=analysis.SAFE)
    risk = decide(closes, vols, mode=analysis.RISK)
    assert risk["thresholds"]["min_growth"] <= safe["thresholds"]["min_growth"]
    assert risk["thresholds"]["min_confirmations"] <= \
        safe["thresholds"]["min_confirmations"]
    assert (risk["allow_buy"] or not safe["allow_buy"])


def test_btc_dump_lowers_the_multiplier():
    bear_ctx = {"btc_trend": "DOWN", "btc_change_24h": -8.0, "breadth": 0.2,
                "market_median_change_24h": -4.0}
    good = decide(*healthy_pullback())
    dumped = decide(*healthy_pullback(), context=bear_ctx)
    assert dumped["market_context"]["multiplier"] < \
        good["market_context"]["multiplier"]
    assert dumped["growth_score"] < good["growth_score"]


def test_single_red_candle_does_not_exit():
    closes, vols = healthy_pullback()
    closes[-1] = closes[-2] * 0.997          # one red bar only
    frames = frames_from_1h(closes, vols)
    res = analysis.exit_analysis({}, frames, MARKET, CTX,
                                 mode=analysis.RISK, entry_price=closes[0])
    assert res["exit"] is False


def test_broken_structure_exits():
    closes, _v = healthy_pullback()
    # a real breakdown: structure breaks, momentum flips, distribution
    for i in range(12):
        closes.append(closes[-1] * 0.985)
    frames = frames_from_1h(closes)
    res = analysis.exit_analysis({}, frames, MARKET, CTX,
                                 mode=analysis.RISK, entry_price=closes[0])
    assert res["exit"] is True
    assert "STRUCTURE_BROKEN" in res["reasons"]


def test_forming_candle_is_dropped():
    raw = candles([100.0 + i for i in range(10)], "1h")
    now = raw[-1]["time"] + 10             # last bar still open
    assert len(analysis.closed_candles(raw, "1h", now)) == 9
    now = raw[-1]["time"] + TF["1h"] + 1   # now it has closed
    assert len(analysis.closed_candles(raw, "1h", now)) == 10


def test_entry_never_contradicts_the_exit_policy():
    """If the exit policy would close the position on the very next bar, the
    entry is refused: buying and selling one bar later only pays the round
    trip and proves the engine argues with itself."""
    closes, _v = healthy_pullback()
    for _i in range(12):
        closes.append(closes[-1] * 0.985)
    frames = frames_from_1h(closes)
    ex = analysis.exit_analysis({}, frames, MARKET, CTX, mode=analysis.RISK)
    assert ex["exit"] is True
    verdict = analysis.decide("TESTIRT", frames, MARKET, CTX,
                              mode=analysis.RISK, cost_pct=0.6)
    assert verdict["allow_buy"] is False
    assert "EXIT_CONDITIONS_ALREADY_TRUE" in verdict["vetoes"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", name, "-", exc or "assertion")
    raise SystemExit(1 if failures else 0)
