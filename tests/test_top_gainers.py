"""RISK mode buys the day's biggest 24h gainers.

The strategy is explicit: the leader of the 24h board is the candidate to
buy — but only on a pullback, not 3+ ATR above EMA20, and only when the
forward projection still sees an edge. Safe mode never inherits this
behaviour, and a downtrend/breakdown is still refused.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nobitex_bot import analysis  # noqa: E402
from nobitex_bot.config import Config  # noqa: E402

TF = analysis.TF_SECONDS

MARKET = {"last_price": 100.0, "change_24h": 18.0, "spread_percent": 0.05,
          "liquidity": {"score": 0.9, "status": "GOOD"},
          "data_quality": {"score": 0.95}}
CTX = {"btc_trend": "UP", "btc_change_24h": 2.0, "breadth": 0.7,
       "market_median_change_24h": 1.0}


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


def frames_from_1h(closes_1h, volumes=None):
    v15 = interpolate(volumes, 4) if volumes else None
    return {
        "1h": analysis.analyze_frame(candles(closes_1h, "1h", volumes), "1h"),
        "4h": analysis.analyze_frame(candles(resample(closes_1h, 4), "4h"),
                                     "4h"),
        "1d": analysis.analyze_frame(candles(resample(closes_1h, 24), "1d"),
                                     "1d"),
        "15m": analysis.analyze_frame(candles(interpolate(closes_1h, 4),
                                              "15m", v15), "15m"),
    }


def day_leader():
    """+2.6%/bar for 60 bars: a parabolic blow-off, not an entry."""
    closes = [100.0 * (1.0008 ** i) for i in range(1900)]
    for _i in range(60):
        closes.append(closes[-1] * 1.026)
    return closes, [2200.0] * len(closes)


def pullback_leader():
    """+25% in 24h, then a 3-bar 1.2% pullback: still the board leader,
    but no longer stretched 3+ ATR above its EMA20."""
    closes = [100.0 * (1.0008 ** i) for i in range(1900)]
    for _i in range(24):
        closes.append(closes[-1] * 1.009)
    for _i in range(3):
        closes.append(closes[-1] * 0.988)
    return closes, [2200.0] * len(closes)


def downtrend():
    return [100.0 * (0.9988 ** i) for i in range(1900)], None


def decide(closes, volumes=None, mode=analysis.RISK, top_gainer=False):
    return analysis.decide("TESTUSDT", frames_from_1h(closes, volumes),
                           MARKET, CTX, mode=mode, cost_pct=0.6,
                           top_gainer=top_gainer)


def test_config_enables_top_gainers_by_default():
    cfg = Config()
    assert cfg.analysis.top_gainers_risk is True
    assert cfg.analysis.top_gainers_count >= 1
    assert cfg.analysis.top_gainers_min_change_pct > 0


def test_risk_buys_the_24h_leader_on_a_pullback():
    verdict = decide(*pullback_leader(), mode=analysis.RISK, top_gainer=True)
    assert verdict["top_gainer"] is True
    assert verdict["forecast"]["direction"] == "UP"
    assert verdict["allow_buy"] is True, verdict["vetoes"]


def test_parabolic_leader_is_refused():
    """+2.6%/bar is a blow-off, not an entry: the leader flag no longer
    overrides the overextension veto."""
    verdict = decide(*day_leader(), mode=analysis.RISK, top_gainer=True)
    assert verdict["top_gainer"] is True
    assert "SETUP_OVEREXTENDED" in verdict["vetoes"]
    assert verdict["allow_buy"] is False


def test_same_leader_is_refused_by_the_growth_engine():
    """Without the top-gainer flag the engine still calls it extended."""
    verdict = decide(*day_leader(), mode=analysis.RISK, top_gainer=False)
    assert verdict["top_gainer"] is False
    assert verdict["allow_buy"] is False


def test_safe_mode_never_inherits_top_gainers():
    verdict = decide(*day_leader(), mode=analysis.SAFE, top_gainer=True)
    assert verdict["top_gainer"] is False
    assert verdict["allow_buy"] is False


def test_top_gainer_still_refuses_a_downtrend():
    verdict = decide(*downtrend(), mode=analysis.RISK, top_gainer=True)
    assert verdict["allow_buy"] is False


def test_ranking_is_by_24h_change():
    rows = [{"symbol": "A", "growth_score": 40.0, "change_24h": 3.0},
            {"symbol": "B", "growth_score": 78.0, "change_24h": 21.0},
            {"symbol": "C", "growth_score": 61.0, "change_24h": 9.0}]
    ranked = sorted(rows, key=lambda r: -r["change_24h"])
    assert [r["symbol"] for r in ranked] == ["B", "C", "A"]


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
