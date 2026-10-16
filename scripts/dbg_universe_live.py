#!/usr/bin/env python3
"""Live check of the snapshot-free market index — READ ONLY, no orders.

Pulls the WHOLE Nobitex catalog through the bot's own adapter (the same
code path `MarketUniverse` uses: ONE public `GET /market/stats` call, no
login, no snapshot file, no per-market round trip) and prints:

* coverage (how many markets / how many tradable, per quote currency),
* the exchange's real top movers,
* every ledger position with its live catalog price — the check that used
  to fail with «بازار در اسنپ‌شات فعلی نیست»,
* optional symbol searches.

Nothing is logged in and nothing is traded: this script never touches the
account, only public market data. It still exports NOBITEX_READ_ONLY=1 so
the adapter itself refuses any write, however this script is later edited.

Usage:  python scripts/dbg_universe_live.py [SYMBOL ...]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import load_config  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI  # noqa: E402
from nobitex_bot.storage import Storage  # noqa: E402
from nobitex_bot.universe import MarketUniverse  # noqa: E402


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def main() -> int:
    os.environ["NOBITEX_READ_ONLY"] = "1"   # see the module docstring
    cfg = load_config()
    cfg.exchange.mode = "nobitex"        # real PUBLIC catalog (read-only)
    api = NobitexAPI(cfg.exchange)
    uni = MarketUniverse(api, data_dir=cfg.engine.data_dir, ttl=20)
    rows = uni.refresh(force=True)
    st = uni.status()
    print("[catalog] %s markets | tradable %s | source %s | error %r"
          % (st["count"], st["tradable"], st["source"], st["error"]))
    if not rows:
        print("[catalog] EMPTY — check the network/IP (Nobitex wants an "
              "Iranian IP) and the base URL")
        return 1

    quotes: dict = {}
    for r in rows.values():
        key = str(r.get("quote_asset") or "?")
        quotes[key] = quotes.get(key, 0) + 1
    print("[catalog] by quote: %s" % quotes)

    print("\n[gainers] top 24h movers over the WHOLE exchange (no snapshot)")
    for r in uni.movers("gainers", limit=8):
        print("   %-14s price=%-16s %+7.2f%%  quote_volume=%s"
              % (r["symbol"], r.get("last_price"),
                 _f(r.get("change_24h")), r.get("quote_volume_24h")))

    print("\n[holdings] ledger positions vs the live catalog")
    try:
        stg = Storage(Path(cfg.engine.data_dir))
        held = 0
        missing = 0
        for p in stg.positions():
            sym = str(p.get("market") or "")
            if not sym or _f(p.get("amount")) <= 0:
                continue
            held += 1
            row = rows.get(sym)
            if row is None:
                missing += 1
                alts = [a for a in MarketUniverse.alternates(sym)
                        if a in rows]
                note = ("same coin listed as " + ", ".join(alts)) if alts \
                    else "NOT listed by the exchange at all"
                print("   %-14s amount=%-18s NO DIRECT MARKET (%s)"
                      % (sym, p.get("amount"), note))
                continue
            px = _f(row.get("last_price"))
            print("   %-14s amount=%-18s live price=%-16s change24h=%+.2f%%"
                  % (sym, p.get("amount"), row.get("last_price"),
                     _f(row.get("change_24h"))))
        print("   -> %s held market(s), %s without a direct catalog market"
              % (held, missing))
    except Exception as exc:                       # diagnostics only
        print("   (ledger read skipped: %s)" % exc)

    for needle in sys.argv[1:]:
        hits = uni.ranked(search=needle, limit=10)
        print("\n[search] %r -> %s" % (needle,
                                       [h["symbol"] for h in hits]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
