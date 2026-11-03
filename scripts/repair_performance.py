"""Repair historical realized PnL on a COPY of the bot database.

The live ledger recorded 1,543 `live_` fills, but the exchange-reconciliation
path never applied them to the portfolio, so `positions.realized_pnl` stayed
at zero and every closed trade looked like a loss.

Attribution uses the CLOSING SELL event, not a fully closed buy/sell cycle:
the bot frequently sells all but a dust remainder, so requiring quantity to
reach exactly zero would leave every row unmatched. The PnL of a sell is
path-independent: replay the market's fills up to that sell with average
cost, and the last sell's `proceeds - average_cost * amount` is the number
the performance row is about.

Safety rules:
  * the source database is opened READ-ONLY; nothing is ever written to it;
  * --apply is required, and the output must be a different file;
  * markets where a SELL is not covered by prior BUYs (manual coins,
    airdrops, renamed markets) are skipped and reported, never guessed;
  * a position is updated only when the replay's remaining quantity matches
    the wallet's amount, so an incomplete fill history cannot invent a PnL;
  * a performance row is updated only when exactly one sell ended shortly
    before the row was written; ambiguous rows are left untouched.

Usage (dry run):
    python scripts/repair_performance.py --db data/trading_bot.db
Usage (apply to a copy):
    python scripts/repair_performance.py --db data/trading_bot.db \
        --out data/trading_bot.repaired.db --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nobitex_bot.engine import realized_pnl_from_fills  # noqa: E402

D = Decimal
FEE_STEP = D("0.00000001")


def _connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load(con: sqlite3.Connection) -> tuple:
    fills = [dict(r) for r in con.execute(
        "SELECT * FROM fills WHERE trade_id LIKE 'live_%' "
        "ORDER BY timestamp ASC, rowid ASC")]
    positions = {r["market"]: dict(r) for r in con.execute(
        "SELECT * FROM positions")}
    perf = [dict(r) for r in con.execute(
        "SELECT * FROM performance ORDER BY created_at ASC")]
    return fills, positions, perf


def _fee(fill: dict, rate: D) -> D:
    fee = D(str(fill.get("fee_amount") or 0))
    if fee <= 0 and rate > 0:
        fee = (D(str(fill.get("price") or 0))
               * D(str(fill.get("amount") or 0)) * rate).quantize(FEE_STEP)
    return fee


def _by_market(fills: list) -> dict:
    out = {}
    for f in fills:
        out.setdefault(f.get("market") or "", []).append(f)
    return out


def _sell_events(market: str, rows: list, rate: D) -> tuple:
    """Every sell and its own realized PnL; ([], False) if unbalanced."""
    qty = D(0)
    cost = D(0)
    events = []
    for f in rows:
        price = D(str(f.get("price") or 0))
        amount = D(str(f.get("amount") or 0))
        if price <= 0 or amount <= 0:
            continue
        fee = _fee(f, rate)
        if str(f.get("side") or "").upper() == "BUY":
            qty += amount
            cost += price * amount + fee
            continue
        if amount > qty:
            return [], False
        avg = (cost / qty) if qty > 0 else D(0)
        basis = avg * amount
        events.append({"market": market, "end": f["timestamp"],
                       "realized": (price * amount - fee) - basis,
                       "basis": basis})
        qty -= amount
        cost -= basis
        if qty <= 0:
            qty = D(0)
            cost = D(0)
    return events, True


def _all_sell_events(fills: list, rate: D) -> tuple:
    events, skipped = [], []
    for market, rows in _by_market(fills).items():
        found, usable = _sell_events(market, rows, rate)
        if not usable:
            skipped.append(market)
            continue
        events.extend(found)
    return events, sorted(skipped)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _match_performance(perf: list, events: list,
                       window: timedelta) -> tuple:
    """Latest sell within `window` before each row was written."""
    by_market = {}
    for e in events:
        by_market.setdefault(e["market"], []).append(e)
    for rows in by_market.values():
        rows.sort(key=lambda e: e["end"])
    matches, skipped = [], 0
    for row in perf:
        created = _parse_ts(row.get("created_at"))
        market = row.get("symbol") or ""
        best = None
        tie = False
        if created is not None:
            for e in by_market.get(market, []):
                ended = _parse_ts(e.get("end"))
                if ended is None:
                    continue
                age = (created - ended).total_seconds()
                if 0 <= age <= window.total_seconds():
                    if best is not None and ended == best[0]:
                        tie = True
                    if best is None or ended > best[0]:
                        best = (ended, e)
                        tie = False
        if best is None or tie:
            skipped += 1
            continue
        matches.append((row, best[1]))
    return matches, skipped


def _positions_report(fills: list, positions: dict, rate: D) -> tuple:
    updates, skipped = [], []
    for market, rows in _by_market(fills).items():
        pos = positions.get(market)
        if not pos:
            continue
        realized, balanced, qty = realized_pnl_from_fills(rows, rate)
        wallet = D(str(pos.get("amount") or 0))
        tol = max(FEE_STEP, abs(wallet) * D("0.01"))
        if not balanced or abs(qty - wallet) > tol:
            skipped.append(market)
            continue
        updates.append((market, realized))
    return updates, sorted(skipped)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "trading_bot.db"))
    ap.add_argument("--out", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--fee-rate", default="0.0025")
    ap.add_argument("--window-hours", type=float, default=2.0)
    args = ap.parse_args()

    source = Path(args.db)
    if not source.exists():
        print("source not found:", source)
        return 2
    rate = D(args.fee_rate)
    window = timedelta(hours=max(0.1, args.window_hours))

    if not args.apply:
        con = _connect_ro(source)
        fills, positions, perf = _load(con)
        con.close()
        events, bad_markets = _all_sell_events(fills, rate)
        matches, unmatched = _match_performance(perf, events, window)
        updates, pos_skipped = _positions_report(fills, positions, rate)
        print(json.dumps({
            "mode": "dry-run", "source": str(source),
            "live_fills": len(fills), "sell_events": len(events),
            "performance_rows": len(perf),
            "performance_matched": len(matches),
            "performance_skipped": unmatched,
            "positions_would_update": len(updates),
            "skip_markets": bad_markets,
            "position_skip_markets": pos_skipped,
            "note": "no file was written",
        }, ensure_ascii=False, indent=1))
        return 0

    out = Path(args.out) if args.out else source.with_suffix(".repaired.db")
    if out.resolve() == source.resolve():
        print("refusing to write over the live database:", source)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    shutil.copy2(source, out)

    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    fills, positions, perf = _load(con)
    events, bad_markets = _all_sell_events(fills, rate)
    matches, unmatched = _match_performance(perf, events, window)
    updated = 0
    for row, event in matches:
        if event["basis"] <= 0:
            continue
        con.execute(
            "UPDATE performance SET actual_return=?, actual_pnl=?, "
            "profitable=? WHERE decision_id=?",
            (round(float(event["realized"] / event["basis"]), 6),
             str(event["realized"]),
             1 if event["realized"] > 0 else 0, row["decision_id"]))
        updated += 1

    updates, pos_skipped = _positions_report(fills, positions, rate)
    for market, realized in updates:
        con.execute(
            "UPDATE positions SET realized_pnl=?, updated_at=datetime('now') "
            "WHERE market=?", (str(realized), market))
    con.commit()
    con.close()
    print(json.dumps({
        "mode": "apply", "source": str(source), "output": str(out),
        "live_fills": len(fills), "sell_events": len(events),
        "performance_rows": len(perf),
        "performance_updated": updated,
        "performance_skipped": unmatched,
        "positions_updated": len(updates),
        "skip_markets": bad_markets,
        "position_skip_markets": pos_skipped,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
