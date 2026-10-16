"""Debug: real Nobitex wallet vs the bot's own position ledger.

Run:  python scripts/dbg_wallet_vs_ledger.py

Read-only (one GET /users/wallets/list + one GET /market/stats). Shows, per
held coin: the REAL wallet balance, the amount/dates the bot's ledger
believes it holds, and the armed exit plan (stop / take-profit). Any row
where the two disagree is exactly why a stop-loss can be synthesized,
approved by the risk engine, and then REJECTED by the exchange
("OverValueOrder" = selling more than the wallet has, "SmallOrder" = order
value below the exchange minimum).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import types  # noqa: E402

from nobitex_bot.config import load_config  # noqa: E402
from nobitex_bot.storage import Storage  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI  # noqa: E402
from nobitex_bot.utils import d  # noqa: E402


def base_of(market: str) -> str:
    m = (market or "").upper()
    if m.endswith("USDT"):
        return m[:-4]
    if m.endswith("IRT"):
        return m[:-3]
    return m


def main() -> int:
    cfg = load_config()
    cfg.exchange.mode = "nobitex"          # force the real adapter
    cfg.exchange.live_trading = False      # never place anything
    # the adapter reads its login/token from <data_dir>/real and needs the
    # engine's data_dir alongside the exchange block
    ex_cfg = types.SimpleNamespace(
        **vars(cfg.exchange),
        data_dir=str(Path(cfg.engine.data_dir) / "real"))
    api = NobitexAPI(ex_cfg)
    try:
        wallet = api.portfolio()
    except Exception as exc:
        print("WALLET READ FAILED:", exc)
        return 1
    real = {}
    for a in wallet.get("assets") or []:
        coin = (a.get("asset") or "").upper()
        total = d(str(a.get("total") or 0))
        if total > 0 and coin not in ("USDT", "IRT"):
            real[coin] = total
    print("REAL WALLET: %d coins" % len(real))
    for k in sorted(real):
        print("   %-8s %s" % (k, real[k]))

    st = Storage(cfg.engine.data_dir)
    plans = st.kv_get("exit_plans", {}) or {}
    print("\nLEDGER vs WALLET:")
    bad = 0
    for p in st.positions():
        market = p["market"]
        amt = d(str(p.get("amount") or 0))
        if amt <= 0:
            continue
        base = base_of(market)
        r = real.get(base, d("0"))
        flag = ""
        if r <= 0:
            flag = "  <-- wallet has NONE (stale ledger row)"
            bad += 1
        elif r < amt:
            flag = "  <-- ledger %.3f%% of the real balance" % (
                float(amt / r * 100))
            bad += 1
        plan = plans.get(market) or {}
        print("   %-10s ledger=%-18s wallet=%-18s stop=%s tp=%s%s" % (
            market, amt, r, plan.get("stop"), plan.get("take_profit_manual"),
            flag))

    print("\nPLANS without a live position: %s" % (
        [m for m in plans if not st.position(m)]))
    print("\nSUMMARY: %d mismatching rows" % bad)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
