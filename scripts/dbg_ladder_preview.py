#!/usr/bin/env python3
"""Sandbox preview for the ladder (نردبان) dashboard cell — DEMO DATA ONLY.

Builds a demo engine in a throwaway data dir, plants three ladder states
(stage 0 / 1 / 3) with the rungs the ratchet would have written, then
serves the dashboard on 127.0.0.1:8799 so the row can be checked in a
browser. No real account, no real orders.

Run:  python -u scripts/dbg_ladder_preview.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config, PaperCfg      # noqa: E402
from nobitex_bot.engine import TradingEngine         # noqa: E402
from nobitex_bot.web import serve                    # noqa: E402
from nobitex_bot.webauth import DashboardAuth        # noqa: E402

PORT = 8801   # 8799 kept for older sandboxes — avoid clashes


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ladder_preview_"))
    cfg = Config()
    cfg.engine.data_dir = str(tmp / "data")
    cfg.engine.cycle_seconds = 30
    cfg.exchange.demo_seed = 7
    cfg.exchange.live_trading = False
    cfg.exchange.live_by_default = False
    cfg.paper = PaperCfg(starting_usdt=5000.0, starting_irt=500_000_000.0)
    # پیش‌فرض هر دو بازار روشن تا هر دو بخش باکس دارایی‌ها (تتری/تومانی)
    # دیده شود؛ با PREVIEW_QUOTE=usdt|irt می‌توان «بازار معاملات» را برای
    # آزمون بازهدف‌گیری کلیک‌ها (خرید/فروش) عوض کرد
    cfg.exchange.trade_quote_markets = os.environ.get("PREVIEW_QUOTE", "both")
    engine = TradingEngine(cfg)
    engine.auto_trading = False          # no automatic decisions in the preview
    engine.run_cycle()

    snap = engine.latest_snapshot or {}
    by_sym = {m["symbol"]: m for m in snap.get("markets", [])
              if m.get("last_price")}
    # یکی تتری پله‌دار، یکی تتری و یکی تومانی: هر دو بخش باکس دارایی‌ها
    # PREVIEW_SECTIONS=usdt → فقط خریدهای تتری کاشته می‌شود تا دیده شود
    # بخش تومانی بدون فعالیت واقعی IRT غیرفعال می‌ماند (یک معاملهٔ کاغذی
    # تومانی هم آن را روشن نمی‌کند)
    sections = (os.environ.get("PREVIEW_SECTIONS", "both") or "both").lower()
    plans_by_symbol = [("BTCUSDT", 3), ("ETHUSDT", 1)] if sections == "usdt" \
        else [("BTCUSDT", 3), ("ETHUSDT", 1), ("BTCIRT", 0)]
    wanted = [(by_sym[s], st) for s, st in plans_by_symbol if s in by_sym]
    step = Decimal(str(cfg.risk.profit_lock_step_pct or 15.0)) / Decimal("100")

    plans = engine.storage.kv_get("exit_plans", {}) or {}
    now = datetime.now(timezone.utc).isoformat()
    # اندازهٔ موقعیت‌ها کوچک تا سقف exposure جا برای خرید آزمایشی بماند
    amounts = {"BTCUSDT": Decimal("0.02"), "ETHUSDT": Decimal("0.2"),
               "BTCIRT": Decimal("0.000005")}
    for m, stage in wanted:
        mkt = m["symbol"]
        price = Decimal(str(m["last_price"]))
        amount = amounts.get(mkt, Decimal("1"))
        anchor = (price / ((Decimal("1") + step) ** stage)
                  if stage else price)
        rungs = [(anchor * (Decimal("1") + step) ** k)
                 .quantize(Decimal("0.000001")) for k in range(stage + 1)]
        rungs[-1] = price.quantize(Decimal("0.000001"))
        prev_rung = rungs[-2] if len(rungs) >= 2 else anchor
        engine.storage.upsert_position(
            mkt, amount, anchor.quantize(Decimal("0.000001")),
            Decimal("0"), last_buy_at=now, last_buy_price=anchor)
        plans[mkt] = {
            "stop": float(prev_rung if stage else
                          (anchor * Decimal("0.97"))),
            "stop_pct": -3.0,
            "auto": True,
            "stage": stage,
            "base_price": float(rungs[-1]),
            "locked_stop": float(rungs[-1]),
            "ladder_anchor": float(anchor),
            "ladder_rungs": [float(x) for x in rungs],
            "ladder_add_stage": 0,
            "ladder_done_stage": 0,
            "take_profit_manual": float(
                (rungs[-1] * (Decimal("1") + step))
                .quantize(Decimal("0.000001"))),
            "take_profit_manual_pct": float(step * 100),
            "risk_mode_peak": float(price),
        }
        # سرمایهٔ پلهٔ بعد = سودِ همان پله: (حد سود مسلح − میانگین خرید) ×
        # تعداد سکه — همان عددی که موتور در آن strike می‌خرد
        tp = Decimal(str(plans[mkt]["take_profit_manual"]))
        rung_budget = ((tp - anchor) * amount).quantize(Decimal("0.01"))
        print("[preview] %s stage=%s amount=%s anchor=%s stop=%s rung_add=%s"
              % (mkt, stage, amount, anchor, plans[mkt]["stop"],
                 rung_budget))
    engine.storage.kv_set("exit_plans", plans)
    if sections == "usdt":
        # a PAPER Toman fill: not real money, so it must NOT switch the
        # Toman section on (the engine counts only `live_…` fills/orders)
        engine.storage.save_fill({
            "trade_id": "trd_preview_paper_irt", "order_id": "prev-paper",
            "decision_id": "", "market": "ETHIRT", "side": "BUY",
            "price": "100000000", "amount": "0.01",
            "gross_value": "1000000", "fee_amount": "0",
            "fee_asset": "IRT",
            "timestamp": datetime.now(timezone.utc).isoformat()})
    engine.run_cycle()          # refresh snapshot/rows with the planted plans

    # loopback sandbox: no password gate (the real config.json stays
    # untouched — a non-existent path falls back to this cfg's empty
    # web.password, which is exactly the documented fallback)
    auth = DashboardAuth(cfg, config_path=tmp / "no_config.json")
    print("[preview] serving http://127.0.0.1:%d (gate: %s)"
          % (PORT, "ON" if auth.enabled() else "OFF"))
    serve(engine, "127.0.0.1", PORT, auth=auth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
