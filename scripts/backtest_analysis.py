"""Walk-forward backtest for the analysis engine — Safe vs Risk.

Design rules (the ones that make a number mean something):
  * NO LOOK-AHEAD: at decision time t only candles whose CLOSE is <= t are
    visible; entries fill at the NEXT bar's open, never at the signal bar.
  * Same code path as live: analysis.decide / analysis.exit_analysis are
    called exactly as the engine calls them, not a re-implementation.
  * Real costs: taker fee both sides + slippage on both fills.
  * Walk-forward: the timeline is split into in-sample and out-of-sample
    halves; the summary reports both, so a good in-sample half cannot hide
    a broken second half.

Usage:
    python scripts/backtest_analysis.py --mode both --bars 500
    python scripts/backtest_analysis.py --mode risk --markets BTCUSDT,ETHUSDT
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nobitex_bot import analysis  # noqa: E402

BASE = "https://apiv2.nobitex.ir"
RESOLUTION = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}
SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
CACHE = ROOT / "data" / "backtest"

DEFAULT_MARKETS = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "SOLUSDT", "DOGEUSDT"]


# ------------------------------------------------------------------ data
def _opener():
    try:
        from nobitex_bot import tls
        return urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=tls.context()))
    except Exception:
        return urllib.request.build_opener()


def fetch(symbol: str, tf: str, bars: int = 500, offline: bool = False):
    """Closed candles oldest-first, cached on disk (no credentials needed)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / ("%s_%s.json" % (symbol, tf))
    if offline:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() \
            else []
    now = int(time.time())
    span = SECONDS[tf] * min(bars, 500)
    url = ("%s/market/udf/history?symbol=%s&resolution=%s&from=%d&to=%d"
           % (BASE, symbol.upper(), RESOLUTION[tf], now - span, now))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "NobitexBotBacktest/1.0"})
        with _opener().open(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print("  ! fetch failed %s %s: %s" % (symbol, tf, exc))
        return json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else []
    if data.get("s") != "ok":
        return json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else []
    out = []
    for i, t in enumerate(data.get("t") or []):
        try:
            out.append({"time": int(t),
                        "open": float(data["o"][i]),
                        "high": float(data["h"][i]),
                        "low": float(data["l"][i]),
                        "close": float(data["c"][i]),
                        "volume": float(data["v"][i])})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    out = analysis.sanitize(out)
    path.write_text(json.dumps(out), encoding="utf-8")
    return out


# ------------------------------------------------------------- simulation
def btc_context(btc_frames_by_time):
    return btc_frames_by_time


def simulate(symbol: str, data: dict, mode: str, cfg: dict,
             btc_series: list | None = None) -> dict:
    """One market, one mode, strictly sequential."""
    t15 = data.get("15m") or []
    if len(t15) < 100:
        return {"trades": [], "skipped": "NO_15M_DATA"}
    fee = float(cfg.get("fee", 0.0025))
    slip = float(cfg.get("slippage", 0.001))
    cost_pct = (fee * 2.0 + slip * 2.0) * 100.0
    disaster_stop = float(cfg.get("disaster_stop", 0.15))
    trades, equity_curve = [], []
    pos = None
    start = 0

    # warmup: the 1d frame alone needs ~80 bars, so skip the early timeline
    timeline = [c["time"] + SECONDS["15m"] for c in t15]
    for i, t in enumerate(timeline):
        price = t15[i]["close"]
        if i + 1 < len(t15):
            next_open = t15[i + 1]["open"]
        else:
            next_open = price
        frames = {}
        for tf in ("15m", "1h", "4h", "1d"):
            closed = analysis.closed_candles(data.get(tf) or [], tf, t)
            frames[tf] = analysis.analyze_frame(closed, tf)
        market = {"symbol": symbol, "last_price": price,
                  "change_24h": (price / t15[max(0, i - 96)]["close"] - 1) * 100
                  if i >= 96 else 0.0,
                  "spread_percent": 0.05,
                  "liquidity": {"score": 0.9, "status": "GOOD"},
                  "data_quality": {"score": 0.95}}
        ctx = {"btc_trend": btc_trend_at(btc_series, t),
               "btc_change_24h": btc_change_at(btc_series, t),
               "breadth": 0.5, "market_median_change_24h": 0.0}

        if pos is None:
            verdict = analysis.decide(symbol, frames, market, ctx, mode=mode,
                                      thresholds=cfg.get("thresholds"),
                                      cost_pct=cost_pct)
            if not verdict.get("allow_buy"):
                continue
            pos = {"entry": next_open * (1 + slip), "bar": i,
                   "time": t, "setup": verdict.get("setup"),
                   "growth": verdict.get("growth_score"),
                   "target_pct": verdict.get("target_pct"),
                   "risk_pct": verdict.get("risk_pct"),
                   "mfe": 0.0, "mae": 0.0}
            continue

        pnl = (price / pos["entry"] - 1.0) * 100.0
        pos["mfe"] = max(pos["mfe"], pnl)
        pos["mae"] = min(pos["mae"], pnl)
        exit_reason = None

        # RISK: analysis only (plus a disaster circuit breaker, which is
        # risk management, not a strategy stop)
        if mode == analysis.RISK:
            res = analysis.exit_analysis({}, frames, market, ctx, mode=mode,
                                         entry_price=pos["entry"])
            if res.get("exit"):
                exit_reason = "ANALYSIS"
            elif pnl <= -disaster_stop * 100.0:
                exit_reason = "DISASTER_STOP"
        else:
            # SAFE: the bracket the plan would arm, plus an analysis exit
            if pnl <= -abs(float(pos.get("risk_pct") or 1.5)):
                exit_reason = "STOP"
            elif pnl >= float(pos.get("target_pct") or 3.0):
                exit_reason = "TARGET"
            else:
                res = analysis.exit_analysis({}, frames, market, ctx,
                                             mode=mode,
                                             entry_price=pos["entry"])
                if res.get("urgent"):
                    exit_reason = "ANALYSIS"

        if exit_reason:
            fill = price * (1 - slip)
            net = (fill / pos["entry"] - 1.0) * 100.0 - cost_pct
            trades.append({"symbol": symbol, "mode": mode,
                           "setup": pos["setup"], "entry": pos["entry"],
                           "exit": fill, "pnl_pct": net,
                           "gross_pct": (fill / pos["entry"] - 1.0) * 100.0,
                           "bars": i - pos["bar"], "reason": exit_reason,
                           "mfe": pos["mfe"], "mae": pos["mae"],
                           "growth": pos["growth"], "time": pos["time"]})
            pos = None
        equity_curve.append(price)

    return {"trades": trades, "bars": len(timeline)}


def btc_trend_at(series, t):
    if not series:
        return "NEUTRAL"
    closes = [c["close"] for c in series if c["time"] + SECONDS["4h"] <= t]
    if len(closes) < 30:
        return "NEUTRAL"
    e9, e20 = analysis.ema(closes, 9), analysis.ema(closes, 20)
    if not e9 or not e20:
        return "NEUTRAL"
    if e9 > e20 * 1.001:
        return "UP"
    if e9 < e20 * 0.999:
        return "DOWN"
    return "NEUTRAL"


def btc_change_at(series, t):
    if not series:
        return 0.0
    closes = [c["close"] for c in series if c["time"] <= t]
    if len(closes) < 2:
        return 0.0
    return (closes[-1] / closes[-2] - 1.0) * 100.0


# ---------------------------------------------------------------- metrics
def metrics(trades: list, start_equity: float = 1000.0) -> dict:
    if not trades:
        return {"trades": 0, "net_pct": 0.0, "win_rate": None,
                "profit_factor": None, "max_drawdown_pct": 0.0,
                "avg_win": None, "avg_loss": None, "expectancy": None}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    equity = start_equity
    peak = start_equity
    max_dd = 0.0
    for p in pnls:
        equity *= (1 + p / 100.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(pnls),
        "net_pct": round((equity / start_equity - 1) * 100, 2),
        "win_rate": round(len(wins) / len(pnls), 3),
        "profit_factor": (round(gross_win / gross_loss, 2)
                          if gross_loss else None),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_win": round(statistics.mean(wins), 2) if wins else None,
        "avg_loss": round(statistics.mean(losses), 2) if losses else None,
        "expectancy": round(statistics.mean(pnls), 3),
        "avg_bars": round(statistics.mean([t["bars"] for t in trades]), 1),
        "avg_mfe": round(statistics.mean([t["mfe"] for t in trades]), 2),
        "avg_mae": round(statistics.mean([t["mae"] for t in trades]), 2),
    }


def by_setup(trades: list) -> dict:
    out = {}
    for t in trades:
        key = t.get("setup") or "?"
        out.setdefault(key, []).append(t["pnl_pct"])
    return {k: {"n": len(v), "avg_pct": round(statistics.mean(v), 2),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v), 2)}
            for k, v in sorted(out.items())}


def walk_forward(trades: list) -> dict:
    if len(trades) < 4:
        return {}
    trades = sorted(trades, key=lambda t: t["time"])
    mid = len(trades) // 2
    return {"in_sample": metrics(trades[:mid]),
            "out_of_sample": metrics(trades[mid:])}


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="both", choices=["safe", "risk", "both"])
    ap.add_argument("--markets", default=",".join(DEFAULT_MARKETS))
    ap.add_argument("--bars", type=int, default=500)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    modes = [analysis.SAFE, analysis.RISK] if args.mode == "both" \
        else [args.mode]

    btc = fetch("BTCUSDT", "4h", 500, args.offline)
    result = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
              "costs": {"fee_per_side": 0.0025, "slippage": 0.001,
                        "round_trip_pct": 0.7},
              "markets": markets, "modes": {}}

    for mode in modes:
        all_trades = []
        per_market = {}
        for sym in markets:
            data = {tf: fetch(sym, tf, args.bars, args.offline)
                    for tf in ("15m", "1h", "4h", "1d")}
            sim = simulate(sym, data, mode, {}, btc)
            per_market[sym] = metrics(sim["trades"])
            all_trades.extend(sim["trades"])
            print("  [%s] %s -> %d trades (bars=%d)" % (
                mode, sym, len(sim["trades"]), sim.get("bars") or 0),
                flush=True)
        result["modes"][mode] = {
            "overall": metrics(all_trades),
            "walk_forward": walk_forward(all_trades),
            "by_setup": by_setup(all_trades),
            "per_market": per_market,
        }
        m = result["modes"][mode]["overall"]
        print("\n== %s ==" % mode.upper())
        print("  trades=%s net=%s%% win=%s pf=%s maxDD=%s%% expectancy=%s%%"
              % (m["trades"], m["net_pct"], m["win_rate"],
                 m["profit_factor"], m["max_drawdown_pct"],
                 m["expectancy"]))
        for k, v in (result["modes"][mode]["by_setup"] or {}).items():
            print("    %-22s n=%-3s avg=%s%% win=%s" %
                  (k, v["n"], v["avg_pct"], v["win_rate"]))

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1),
                                  encoding="utf-8")
        print("\nsaved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
