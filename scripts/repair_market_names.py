"""One-shot repair: migrate exchange-echoed market names to canonical ones.

Nobitex echoes full coin names ("PYTH NETWORK", "Defi App") in
/market/orders/list and several other endpoints. Fills/orders recorded
under those raw names were invisible to positions/exit plans/UI (which key
on the canonical registry symbol, PYTHIRT). This script rewrites:
  - fills.market
  - orders.market
  - exit_plans keys (kv_state)
to canonical_symbol(raw). Idempotent: canonical names pass through.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nobitex_bot.utils import canonical_symbol  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "trading_bot.db"


def main() -> None:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    changed_fills = changed_orders = changed_plans = 0

    rows = conn.execute("SELECT trade_id, market FROM fills").fetchall()
    for r in rows:
        c = canonical_symbol(r["market"])
        if c and c != r["market"]:
            conn.execute("UPDATE fills SET market=? WHERE trade_id=?",
                         (c, r["trade_id"]))
            changed_fills += 1

    rows = conn.execute("SELECT order_id, market FROM orders").fetchall()
    for r in rows:
        c = canonical_symbol(r["market"])
        if c and c != r["market"]:
            conn.execute("UPDATE orders SET market=? WHERE order_id=?",
                         (c, r["order_id"]))
            changed_orders += 1

    row = conn.execute(
        "SELECT value FROM kv_state WHERE key='exit_plans'").fetchone()
    if row:
        try:
            plans = json.loads(row[0])
        except Exception:
            plans = None
        if isinstance(plans, dict):
            for k in list(plans.keys()):
                c = canonical_symbol(k)
                if c and c != k:
                    # merge if the canonical key already exists
                    if c in plans and isinstance(plans[c], dict):
                        merged = dict(plans[k])
                        merged.update({k2: v2 for k2, v2 in plans[c].items()
                                       if k2 not in ("stop", "tps",
                                                     "take_profit_manual",
                                                     "stop_pct",
                                                     "take_profit_manual_pct")})
                        plans[c] = merged
                    else:
                        plans[c] = plans[k]
                    del plans[k]
                    changed_plans += 1
            conn.execute("UPDATE kv_state SET value=? WHERE key='exit_plans'",
                         (json.dumps(plans, ensure_ascii=False),))
    conn.commit()

    # report leftovers that still look non-canonical
    left = conn.execute(
        "SELECT DISTINCT market FROM fills WHERE market LIKE '% %'").fetchall()
    print("fills migrated:", changed_fills)
    print("orders migrated:", changed_orders)
    print("exit_plans migrated:", changed_plans)
    print("fills still spacey:", [r["market"] for r in left])
    conn.close()


if __name__ == "__main__":
    main()
