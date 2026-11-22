"""Phase 3 — backtest harness: 24h-change ranking vs net expected-edge.

What this measures
------------------
The Phase-1 finding was that the live engine ranks candidates by 24h change
and, in risk mode, drops the entry-quality guards for the day's leaders, so it
buys after a large rise. This harness replays stored candles and runs the SAME
exit logic and the SAME costs for three selection arms:

    change_24h        rank by 24h change, no entry veto      <- today's behaviour
    change_24h+veto   rank by 24h change, Phase-2 vetoes on
    net_edge+veto     rank by net expected edge, vetoes on   <- Phase-2 pipeline

Comparing the three separates "the ranking is wrong" from "the entry veto is
missing": if arm 2 and arm 3 both beat arm 1 the veto is what matters; if arm 3
beats arm 2 the ranking matters too.

Costs are real, from the config defaults: taker fee 0.25% per side and 0.1%
slippage per side (fills are moved against the position). Stored candles carry
no order book, so spread is 0 unless passed with ``--spread-pct``; the report
always states which cost components were applied.

What it deliberately does NOT do
--------------------------------
* It never touches the exchange, the engine, ``data.db`` or live trading. The
  engine does not import this module.
* It makes no profitability claim: a result here is evidence about the RULES
  on the sample that was loaded, nothing more.
* ``p_up`` is not the live forecast. It is an empirical first-touch rate: over
  the bars BEFORE the decision, how often did price reach the same target
  before the same stop? That is measurable without lookahead, and it is stated
  as a method — not dressed up as a calibrated probability.

Usage
-----
    python -m nobitex_bot.backtest --synthetic
    python -m nobitex_bot.backtest --db data/trading_bot.db --timeframe 15m \
        --symbols BTCIRT,ETHIRT
    python -m nobitex_bot.backtest --csv a.csv --csv b.csv --timeframe 15m
    python -m nobitex_bot.backtest --synthetic --json reports/bt.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .analysis import atr_series, ema_series
from .pipeline import BUY, DecisionPipeline, Evidence, Policy

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}


# --------------------------------------------------------------------- data
@dataclass(frozen=True)
class Costs:
    """Execution costs, in percent per side."""

    fee_pct: float = 0.25
    slippage_pct: float = 0.10
    spread_pct: float = 0.0

    def charged_pct(self) -> float:
        """Round-trip fee + spread. Slippage is already in the fill prices."""
        return 2.0 * self.fee_pct + self.spread_pct


def _num(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _ts(value) -> float:
    """Unix seconds from a unix number, a numeric string or an ISO string."""
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("empty timestamp")
        try:
            out = float(text)
        except ValueError:
            iso = text.replace("Z", "+00:00")
            out = datetime.fromisoformat(iso).timestamp()
    return out / 1000.0 if out > 1e11 else out      # milliseconds in the wild


def _bar(symbol: str, timestamp, o, h, l, c, v) -> dict:
    return {"symbol": symbol, "timestamp": _ts(timestamp),
            "open": _num(o), "high": _num(h), "low": _num(l),
            "close": _num(c), "volume": _num(v)}


def _normalise(bars: list[dict]) -> list[dict]:
    """Sort by time, drop duplicates and internally inconsistent candles."""
    seen, out = set(), []
    for b in sorted(bars, key=lambda x: x["timestamp"]):
        if not (b["high"] > 0 and b["low"] > 0 and b["close"] > 0):
            continue
        if b["high"] < b["low"]:
            continue
        if not (b["low"] <= b["open"] <= b["high"]
                and b["low"] <= b["close"] <= b["high"]):
            continue
        if b["timestamp"] in seen:
            continue
        seen.add(b["timestamp"])
        out.append(b)
    return out


def load_sqlite(path: str | Path, symbol: str,
                timeframe: str) -> list[dict]:
    """Stored candles for one market. Empty result means nothing was stored."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT timestamp, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND timeframe=? ORDER BY timestamp",
            (symbol, timeframe)).fetchall()
    finally:
        con.close()
    return _normalise([_bar(symbol, *row) for row in rows
                       if len(row) == 6])


def load_csv(path: str | Path, symbol: str | None = None) -> list[dict]:
    """CSV with a header; accepted time columns: timestamp/time/ts/date."""
    path = Path(path)
    name = symbol or path.stem.split("_")[0].upper()
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            low = {str(k).strip().lower(): v for k, v in row.items()}
            t = low.get("timestamp") or low.get("time") or low.get("ts") \
                or low.get("date")
            try:
                out.append(_bar(name, t, low.get("open"), low.get("high"),
                                low.get("low"), low.get("close"),
                                low.get("volume")))
            except (TypeError, ValueError):
                continue
    return _normalise(out)


def synthetic_candles(symbol: str, bars: int = 1500, seed: int = 7,
                      start_price: float = 100.0,
                      timeframe: str = "15m") -> list[dict]:
    """Deterministic random walk — to TEST THE HARNESS, never to judge a rule.

    Reports built from it are labelled ``synthetic`` and must not be quoted as
    evidence about profitability.
    """
    rng = random.Random(f"{seed}:{symbol}")
    step = TF_SECONDS.get(timeframe, 900)
    t0 = 1_700_000_000 - bars * step
    price, out = start_price, []
    for i in range(bars):
        drift = 0.0004 * math.sin(i / 90.0)          # slow regime swings
        ret = drift + rng.gauss(0.0, 0.006)
        o = price
        c = max(1e-6, o * (1.0 + ret))
        wick = abs(rng.gauss(0.0, 0.003)) * o
        h = max(o, c) + wick
        l = max(1e-9, min(o, c) - wick)
        out.append(_bar(symbol, t0 + i * step, o, h, l, c,
                        rng.uniform(500, 5000)))
        price = c
    return _normalise(out)


# ----------------------------------------------------------------- evidence
@dataclass(frozen=True)
class Arm:
    name: str
    rank: str          # "change_24h" | "net_edge"
    veto: bool         # Phase-2 entry vetoes enabled


ARMS = (
    Arm("change_24h", "change_24h", False),        # today's engine behaviour
    Arm("change_24h+veto", "change_24h", True),
    Arm("net_edge", "net_edge", False),            # ranking alone
    Arm("net_edge+veto", "net_edge", True),        # the Phase-2 pipeline
)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    out = ema_series(values, period)
    return float(out[-1]) if out else float(values[-1])


def _hit_rate(bars: list[dict], i: int, target_pct: float, stop_pct: float,
              max_hold: int, samples: int = 24, stride: int = 5) -> float | None:
    """Empirical first-touch rate using ONLY bars before ``i``.

    For each of the last ``samples`` entry points (spaced ``stride`` apart) the
    same target/stop is walked forward up to ``max_hold`` bars. When a bar
    touches both, the stop is assumed first (pessimistic).
    """
    wins = total = 0
    start = max(25, i - samples * stride)
    for j in range(start, i, stride):
        entry = bars[j]["close"]
        if entry <= 0:
            continue
        target = entry * (1.0 + target_pct / 100.0)
        stop = entry * (1.0 - stop_pct / 100.0)
        total += 1
        for k in range(j + 1, min(j + 1 + max_hold, i + 1)):
            if bars[k]["low"] <= stop:
                break
            if bars[k]["high"] >= target:
                wins += 1
                break
    return None if not total else wins / total


def build_evidence(bars: list[dict], i: int, timeframe: str, costs: Costs,
                   policy: Policy, max_hold: int, lookback: int = 30,
                   p_up_stride: int = 5) -> Evidence | None:
    """Evidence at bar ``i`` from bars ``0..i`` only (no lookahead)."""
    if i < max(lookback + 3, 26):
        return None
    close = bars[i]["close"]
    if close <= 0:
        return None
    closes = [b["close"] for b in bars[:i + 1]]
    ema20 = _ema(closes[-120:], 20)
    atr = atr_series(bars[:i + 1], 14)
    atr_abs = atr[-1] if atr else 0.0
    if atr_abs <= 0:
        return None
    atr_pct = atr_abs / close * 100.0
    extension = (close - ema20) / atr_abs

    window = bars[max(0, i - lookback):i - 1]       # never the current bar
    resistance = max((b["high"] for b in window), default=0.0)
    room = ((resistance - close) / close * 100.0
            if resistance > close else None)

    step = TF_SECONDS.get(timeframe, 900)
    day_ago = i
    while day_ago > 0 and bars[i]["timestamp"] - bars[day_ago]["timestamp"] \
            < 86400:
        day_ago -= 1
    change_24h = ((close / bars[day_ago]["close"] - 1.0) * 100.0
                  if bars[day_ago]["close"] > 0 else 0.0)

    volumes = [b["volume"] for b in bars[max(0, i - 50):i + 1]]
    median_volume = sorted(volumes)[len(volumes) // 2] if volumes else 0.0
    volume_ratio = (bars[i]["volume"] / median_volume
                    if median_volume > 0 else 0.0)

    target_pct = atr_pct * policy.atr_target_mult
    if room is not None:
        target_pct = min(target_pct, room)
    stop_pct = max(atr_pct * policy.atr_stop_mult,
                   2.0 * (costs.charged_pct() + 2 * costs.slippage_pct))
    p_up = _hit_rate(bars, i - (i % max(1, p_up_stride)), target_pct,
                     stop_pct, max_hold)

    upward = close > ema20 and ema20 > _ema(closes[-121:-1], 20)
    downward = close < ema20 and ema20 < _ema(closes[-121:-1], 20)
    return Evidence(
        symbol=bars[i]["symbol"],
        price=close,
        atr_pct=round(atr_pct, 6),
        extension_atr=round(extension, 4),
        dist_resistance_pct=None if room is None else round(room, 4),
        p_up=round(0.5 if p_up is None else p_up, 4),
        expected_move_pct=None,
        change_24h=round(change_24h, 4),
        spread_pct=costs.spread_pct,
        fee_pct=costs.fee_pct,
        slippage_pct=costs.slippage_pct,
        data_quality=1.0,                  # stored bars are assumed clean
        liquidity=round(max(0.0, min(1.0, volume_ratio)), 4),
        liquidity_status="GOOD" if volume_ratio >= 0.5 else "THIN",
        structure=("UPTREND" if upward else
                   "DOWNTREND" if downward else "RANGE"),
    )


def rank_candidates(evidences: list[Evidence], arm: Arm,
                    pipeline: DecisionPipeline
                    ) -> list[tuple[Evidence, float, list[str]]]:
    """Published, testable selection step: best candidate first.

    Returns ``(evidence, sort_key, vetoes)``. With ``veto`` off nothing is
    filtered — that is exactly the pre-Phase-2 behaviour being measured.
    """
    scored = []
    for ev in evidences:
        decision = (pipeline.evaluate(ev)
                    if (arm.veto or arm.rank == "net_edge") else None)
        if arm.veto and decision.action != BUY:
            continue
        if decision is not None and arm.rank == "net_edge":
            key = decision.net_edge_pct
        else:
            key = ev.change_24h
        scored.append((ev, key, list(decision.vetoes) if decision else []))
    return sorted(scored, key=lambda row: row[1], reverse=True)


# --------------------------------------------------------------- simulation
@dataclass
class Trade:
    arm: str
    symbol: str
    entry_ts: float
    exit_ts: float
    entry_price: float
    exit_price: float
    target_pct: float
    stop_pct: float
    p_up: float
    net_pct: float
    bars_held: int
    reason: str
    top_gainer: bool


def exit_for(pos: dict, bar: dict, max_hold: int,
             costs: Costs) -> tuple[str | None, float | None]:
    """(reason, exit_price) for one open position on one bar.

    A bar that touches BOTH the target and the stop is scored as a stop: OHLC
    cannot tell which came first, and assuming the win would flatter every
    strategy in the report.
    """
    if bar["low"] <= pos["stop"]:
        return "STOP", pos["stop"] * (1.0 - costs.slippage_pct / 100.0)
    if bar["high"] >= pos["target"]:
        return "TARGET", pos["target"]
    if pos["bars"] >= max_hold:
        return "TIMEOUT", bar["close"] * (1.0 - costs.slippage_pct / 100.0)
    return None, None


def run_arm(arm: Arm, series: dict[str, list[dict]], timeframe: str,
            costs: Costs, policy: Policy, slots: int = 2,
            max_hold: int = 24) -> list[Trade]:
    """Replay the bars for one selection arm. Decisions never see the future."""
    pipeline = DecisionPipeline(policy)
    index = {sym: {b["timestamp"]: i for i, b in enumerate(bars)}
             for sym, bars in series.items()}
    timeline = sorted({ts for m in index.values() for ts in m})
    positions: dict[str, dict] = {}
    pending: list[tuple[str, Evidence]] = []
    trades: list[Trade] = []

    for ts in timeline:
        for sym, ev in pending:                      # fill at THIS bar's open
            bar = series[sym][index[sym][ts]] if ts in index[sym] else None
            if bar is None or bar["open"] <= 0:
                continue
            entry = bar["open"] * (1.0 + costs.slippage_pct / 100.0)
            target_pct = ev.atr_pct * policy.atr_target_mult
            stop_pct = max(ev.atr_pct * policy.atr_stop_mult,
                           2.0 * (costs.charged_pct()
                                  + 2 * costs.slippage_pct))
            positions[sym] = {
                "entry": entry, "entry_ts": ts,
                "target": entry * (1.0 + target_pct / 100.0),
                "stop": entry * (1.0 - stop_pct / 100.0),
                "target_pct": target_pct, "stop_pct": stop_pct,
                "p_up": ev.p_up, "top_gainer": ev.top_gainer, "bars": 0,
            }
        pending = []

        for sym in list(positions):                  # manage exits
            pos = positions[sym]
            if ts not in index[sym]:
                continue
            bar = series[sym][index[sym][ts]]
            pos["bars"] += 1
            reason, exit_price = exit_for(pos, bar, max_hold, costs)
            if reason is None:
                continue
            gross = (exit_price / pos["entry"] - 1.0) * 100.0
            trades.append(Trade(
                arm=arm.name, symbol=sym, entry_ts=pos["entry_ts"], exit_ts=ts,
                entry_price=round(pos["entry"], 8),
                exit_price=round(exit_price, 8),
                target_pct=round(pos["target_pct"], 4),
                stop_pct=round(pos["stop_pct"], 4),
                p_up=pos["p_up"],
                net_pct=round(gross - costs.charged_pct(), 4),
                bars_held=pos["bars"], reason=reason,
                top_gainer=pos["top_gainer"]))
            del positions[sym]

        free = slots - len(positions) - len(pending)
        if free <= 0:
            continue
        evidences = []
        for sym, bars in series.items():
            i = index[sym].get(ts)
            if i is None or sym in positions:
                continue
            ev = build_evidence(bars, i, timeframe, costs, policy, max_hold)
            if ev is not None:
                evidences.append(ev)
        if not evidences:
            continue
        hottest = max(evidences, key=lambda e: e.change_24h)
        evidences = [replace(e, top_gainer=(e.symbol == hottest.symbol))
                     for e in evidences]
        for ev, _key, _vetoes in rank_candidates(evidences, arm, pipeline):
            if free <= 0:
                break
            pending.append((ev.symbol, ev))
            free -= 1
    return trades


# ------------------------------------------------------------------ metrics
def metrics(trades: list[Trade]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_net_pct": 0.0, "total_net_pct": 0.0,
                "profit_factor": 0.0, "max_drawdown_pct": 0.0,
                "avg_bars": 0.0, "stop_rate": 0.0, "target_rate": 0.0,
                "timeout_rate": 0.0}
    returns = [t.net_pct for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    equity, peak, worst = 0.0, 0.0, 0.0
    for r in returns:                      # chronological: trades are appended
        equity += r
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100.0, 2),
        "avg_net_pct": round(sum(returns) / n, 4),
        "total_net_pct": round(sum(returns), 4),
        "profit_factor": (round(gross_win / gross_loss, 3)
                          if gross_loss > 0 else 0.0),
        "max_drawdown_pct": round(worst, 4),
        "avg_bars": round(sum(t.bars_held for t in trades) / n, 2),
        "stop_rate": round(sum(1 for t in trades if t.reason == "STOP")
                           / n * 100.0, 2),
        "target_rate": round(sum(1 for t in trades if t.reason == "TARGET")
                             / n * 100.0, 2),
        "timeout_rate": round(sum(1 for t in trades if t.reason == "TIMEOUT")
                              / n * 100.0, 2),
    }


# ------------------------------------------------------------------- report
def run(sources: dict[str, list[dict]], timeframe: str, costs: Costs,
        policy: Policy, slots: int, max_hold: int, label: str) -> dict:
    series = {sym: bars for sym, bars in sources.items() if len(bars) > 60}
    if not series:
        print("[backtest] no usable data: every source had <=60 candles. "
              "Nothing in this build writes the candles table "
              "(upsert_candle has no caller), so export CSV or use "
              "--synthetic.", file=sys.stderr)
        raise SystemExit(2)
    arms = {}
    for arm in ARMS:
        trades = run_arm(arm, series, timeframe, costs, policy, slots,
                         max_hold)
        arms[arm.name] = {"metrics": metrics(trades),
                          "trades": [asdict(t) for t in trades]}
    return {
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": {"markets": sorted(series),
                 "candles": {s: len(b) for s, b in series.items()},
                 "span": {"from": min(b[0]["timestamp"] for b in series.values()),
                          "to": max(b[-1]["timestamp"] for b in series.values())}},
        "settings": {"timeframe": timeframe, "slots": slots,
                     "max_hold": max_hold,
                     "fee_pct_per_side": costs.fee_pct,
                     "slippage_pct_per_side": costs.slippage_pct,
                     "spread_pct": costs.spread_pct,
                     "spread_note": ("stored candles carry no order book; pass "
                                     "--spread-pct to charge one"),
                     "p_up_method": ("empirical first-touch rate over the "
                                     "previous bars, same target/stop, stop "
                                     "assumed first on a double touch"),
                     "liquidity_method": "volume vs 50-bar median (proxy)",
                     "policy": asdict(policy)},
        "arms": arms,
    }


def _table(report: dict) -> str:
    keys = ("trades", "win_rate", "avg_net_pct", "total_net_pct",
            "profit_factor", "max_drawdown_pct", "stop_rate", "target_rate")
    head = f"{'arm':<17}" + "".join(f"{k[:13]:>15}" for k in keys)
    lines = [head, "-" * len(head)]
    for name, arm in report["arms"].items():
        m = arm["metrics"]
        lines.append(f"{name:<17}" + "".join(f"{m.get(k, 0):>15}"
                                             for k in keys))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m nobitex_bot.backtest",
        description="Replay stored candles: 24h-change ranking vs net edge.")
    ap.add_argument("--db", help="sqlite file with a candles table")
    ap.add_argument("--symbols", default="",
                    help="comma separated markets for --db")
    ap.add_argument("--csv", action="append", default=[],
                    help="OHLCV csv (repeat for several markets)")
    ap.add_argument("--synthetic", action="store_true",
                    help="deterministic random walk — harness self-test only")
    ap.add_argument("--bars", type=int, default=1500)
    ap.add_argument("--synthetic-markets", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--fee-pct", type=float, default=0.25)
    ap.add_argument("--slippage-pct", type=float, default=0.10)
    ap.add_argument("--spread-pct", type=float, default=0.0)
    ap.add_argument("--slots", type=int, default=2)
    ap.add_argument("--max-hold", type=int, default=24)
    ap.add_argument("--max-extension-atr", type=float, default=2.5)
    ap.add_argument("--min-p-up", type=float, default=0.55)
    ap.add_argument("--json", dest="json_path", default="")
    args = ap.parse_args(argv)

    costs = Costs(fee_pct=args.fee_pct, slippage_pct=args.slippage_pct,
                  spread_pct=args.spread_pct)
    policy = Policy(max_extension_atr=args.max_extension_atr,
                    min_p_up=args.min_p_up)
    sources: dict[str, list[dict]] = {}
    label = ""

    if args.db:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if
                   s.strip()]
        if not symbols:
            print("[backtest] --db needs --symbols A,B,C", file=sys.stderr)
            return 2
        for sym in symbols:
            sources[sym] = load_sqlite(args.db, sym, args.timeframe)
        label = f"sqlite:{args.db}"
        empty = [s for s, b in sources.items() if not b]
        if empty:
            print(f"[backtest] no stored candles for {', '.join(empty)} "
                  f"({args.timeframe}); the candles table is filled by nothing "
                  f"in this build — export CSV or use --synthetic",
                  file=sys.stderr)
            return 2
    for path in args.csv:
        bars = load_csv(path)
        if bars:
            sources[bars[0]["symbol"]] = bars
            label = (label + "+" if label else "") + f"csv:{path}"
    if args.synthetic or not sources:
        markets = max(1, args.synthetic_markets)
        for k in range(markets):
            sym = f"SYN{k}IRT"
            sources[sym] = synthetic_candles(
                sym, bars=args.bars, seed=args.seed + k,
                start_price=100.0 * (k + 1), timeframe=args.timeframe)
        label = (label + "+" if label else "") + (
            f"synthetic(seed={args.seed}, bars={args.bars})")
        if not args.synthetic:
            print("[backtest] no data source given; used the synthetic "
                  "self-test walk (NOT evidence about profitability)",
                  file=sys.stderr)

    report = run(sources, args.timeframe, costs, policy, args.slots,
                 args.max_hold, label)
    print(f"[backtest] {report['label']} | {report['settings']['timeframe']}"
          f" fee={costs.fee_pct}%/side slip={costs.slippage_pct}%/side"
          f" spread={costs.spread_pct}% slots={args.slots}"
          f" max_hold={args.max_hold}")
    print(_table(report))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"[backtest] wrote {out}")
    print("[backtest] interpretation: change_24h = today's 24h ranking "
          "without the entry veto; net_edge = ranking alone; +veto adds the "
          "Phase-2 entry vetoes. Results describe THIS sample only — no "
          "profitability claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
