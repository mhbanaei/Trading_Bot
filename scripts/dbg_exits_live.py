#!/usr/bin/env python3
"""Preview the exit engine against the REAL positions — NOTHING is written.

«حد ضرر مشخص شده اما فروش نمی‌شود» is the hardest failure to see in
production, so this prints the whole decision for every position in the
real ledger, per market:

* the LIVE catalog price (ONE public exchange call — no snapshot file, no
  cycle, no login required for prices),
* the armed stop and how far the price is below/above it,
* the branch the engine takes: SELL order, BELOW-MINIMUM CONVERT, parked
  (with the reason) or no trigger,
* whether the resulting decision survives the validation gate that used to
  throw held markets away as UNKNOWN_MARKET.

SAFETY (three independent layers, all verified by tests):

1. ``NOBITEX_READ_ONLY=1`` is exported before ANY object is built, so the
   real adapter itself refuses every place/cancel/convert call no matter
   which engine path reaches it (see NobitexAPI.is_read_only);
2. the real ledger is opened through a READ-ONLY sqlite URI — positions and
   exit plans are copied out, never modified;
3. all engine state lives in a throwaway temp data dir, and live trading
   stays OFF, so the running bot's ledger, wallet and credentials are
   untouched.

Usage:  python scripts/dbg_exits_live.py [--data DIR] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# first statement on purpose: every later line is structurally unable to
# move real money, whatever this script is edited into
os.environ["NOBITEX_READ_ONLY"] = "1"

from nobitex_bot.config import load_config  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI  # noqa: E402
from nobitex_bot.universe import MarketUniverse  # noqa: E402
from nobitex_bot.utils import d  # noqa: E402

DB_NAME = "trading_bot.db"


def _read_only_ledger(data_dir: Path) -> tuple:
    """(positions, exit_plans) copied out of the live ledger, read-only."""
    path = (data_dir / DB_NAME).resolve()
    if not path.exists():
        raise SystemExit("no ledger at %s" % path)
    uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT market, amount, avg_entry FROM positions").fetchall()
        kv = conn.execute(
            "SELECT value FROM kv_state WHERE key='exit_plans'").fetchone()
    finally:
        conn.close()
    positions = [(m, str(a), str(e or 0)) for m, a, e in rows
                 if d(str(a or 0)) > 0]
    plans = {}
    if kv:
        try:
            plans = json.loads(kv[0]) or {}
        except Exception:
            plans = {}
    return positions, plans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="", help="ledger dir (default: config)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config()
    live_dir = Path(args.data) if args.data else Path(cfg.engine.data_dir)
    positions, plans = _read_only_ledger(live_dir)
    if args.limit:
        positions = positions[:args.limit]
    print("[ledger] %s -> %d open position(s), %d armed plan(s) (read-only)"
          % (live_dir, len(positions), len(plans)))

    # ---- throwaway engine: real PUBLIC catalog attached, live trading OFF
    cfg.engine.data_dir = tempfile.mkdtemp(prefix="nobot_exits_")
    cfg.engine.server_port = 0
    cfg.exchange.live_trading = False
    cfg.exchange.live_by_default = False
    engine = TradingEngine(cfg)
    engine.auto_trading = False
    engine._set_exchange(NobitexAPI(cfg.exchange, events=engine.events))
    for market, amount, entry in positions:
        engine.storage.upsert_position(market, d(amount), d(entry),
                                       Decimal("0"))
    engine.storage.kv_set("exit_plans", plans)
    print("[engine] live_trading=%s | adapter read_only=%s | data=%s"
          % (cfg.exchange.live_trading, NobitexAPI.is_read_only(),
             cfg.engine.data_dir))

    uni = engine.market_universe(force=True)
    st = uni.status()
    print("[catalog] %s markets (tradable %s, source %s) error=%r"
          % (st["count"], st["tradable"], st["source"], st["error"]))
    if not st["count"]:
        print("[catalog] EMPTY — check the network/IP (Nobitex needs an "
              "Iranian IP)")
        return 1

    # ---- the real thing: what would the exit engine do RIGHT NOW?
    decisions = engine._manage_exits({}, "probe_exits")
    by_symbol = {x.get("symbol"): x for x in decisions}
    allowed = engine._allowed_symbols({})

    print("\n[exits] per-market decision (snapshot = NONE on purpose)")
    counts = {"SELL": 0, "CONVERT": 0, "PARKED": 0, "NONE": 0, "OFF": 0}
    for market, amount, entry in positions:
        plan = plans.get(market) or {}
        stop = d(str(plan.get("stop") or 0))
        px = engine._exit_price(market, {})
        price_s = ("%.10g" % float(px)) if px else "—"
        gap = ""
        if px and stop > 0:
            gap = "%+.2f%% vs stop" % (
                float((d(str(px)) / stop - 1) * 100))
        if uni.row(market) is None:
            alts = [a for a in MarketUniverse.alternates(market)
                    if uni.row(a)]
            gap = (gap + " " if gap else "") + (
                "same coin listed as " + ",".join(alts) if alts
                else "not listed by the exchange")
        if not engine._quote_market_enabled(market):
            counts["OFF"] += 1
            state = "section OFF — plan stays armed, sell it by hand"
        elif market in by_symbol:
            dec = by_symbol[market]
            ok = market in allowed
            notional = d(str(dec["position_size"]["quote_value"]))
            counts["SELL"] += 1
            state = ("SELL %s of %s (~%s) | validation: %s"
                     % (dec["reason_codes"][0],
                        dec["position_size"]["amount"], "%.4g" % float(
                            notional),
                        "PASS" if ok else "REJECTED (UNKNOWN_MARKET)"))
        elif stop > 0 and px and d(str(px)) <= stop:
            notional = d(amount) * d(str(px))
            mn = d(str(engine._min_notional_for(market, {}) or 0))
            if mn > 0 and notional < mn:
                counts["CONVERT"] += 1
                state = ("stop HIT and the order book refuses %s < min %s "
                         "-> CONVERT path (closes it, pays out in Toman)"
                         % ("%.4g" % float(notional), "%.4g" % float(mn)))
            else:
                counts["PARKED"] += 1
                state = "stop HIT but the exit is parked: %s" % (
                    plan.get("below_min_until_ts") and
                    "retry window still open" or "no free amount / no price")
        else:
            counts["NONE"] += 1
            state = "no trigger (stop %s)" % ("%.10g" % float(stop)
                                              if stop else "not armed")
        print("  %-11s amt=%-14s price=%-12s stop=%-12s %-22s %s"
              % (market, amount, price_s,
                 ("%.10g" % float(stop)) if stop else "—", gap, state))

    print("\n[summary] sell-order=%d convert=%d parked=%d no-trigger=%d "
          "section-off=%d | exits built this cycle=%d"
          % (counts["SELL"], counts["CONVERT"], counts["PARKED"],
             counts["NONE"], counts["OFF"], len(decisions)))
    print("[safety] real ledger untouched (read-only URI); adapter writes "
          "refused by NOBITEX_READ_ONLY=%s"
          % os.environ.get("NOBITEX_READ_ONLY"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
