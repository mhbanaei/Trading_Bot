#!/usr/bin/env python3
"""Serve a SANDBOX dashboard for UI checks — public market data, no account.

`mode=nobitex` + `live_trading=False`: the exchange adapter is only ever
asked for PUBLIC data (the whole catalog, orderbooks, candles), while every
order stays in the paper broker — `ExecutionEngine._live()` needs the real
adapter AND `live_trading` before anything is sent to the exchange. Auto
trading is switched off as well, and the data dir is a throwaway temp dir,
so the running bot's ledger and credentials are never touched.

Belt AND braces: this script exports NOBITEX_READ_ONLY=1 before it builds
anything, so the real adapter refuses EVERY wallet-moving call (place,
cancel, convert) no matter which engine path reaches it.

Optional `--holdings` plants the exact live failure the dashboard must
survive: 1.667996 ATOM bought at 1.942 with a stop at 1.88374 while the
price is far below it — a position the cycle's snapshot does not cover.

Usage:  python scripts/dbg_serve_sandbox.py [--port 8799] [--holdings]
                                           [--data DIR]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import load_config  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.web import serve  # noqa: E402
from nobitex_bot.webauth import DashboardAuth  # noqa: E402


def main() -> int:
    # first statement on purpose: the adapter reads this at call time, so it
    # is impossible for any later line of this script to move real money
    os.environ["NOBITEX_READ_ONLY"] = "1"
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--data", default="")
    ap.add_argument("--holdings", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    cfg.engine.data_dir = args.data or tempfile.mkdtemp(
        prefix="nobot_sandbox_")
    cfg.engine.server_host = "127.0.0.1"      # loopback: password gate off
    cfg.engine.server_port = args.port
    cfg.web.password = ""
    cfg.exchange.mode = "nobitex"             # real PUBLIC catalog
    cfg.exchange.live_trading = False         # paper broker only
    engine = TradingEngine(cfg)
    engine.auto_trading = False
    # without credentials the engine falls back to the DEMO simulator; this
    # sandbox is about the REAL public catalog, and Nobitex serves it
    # without a login — attach the real adapter read-only (live_trading
    # stays False, so no order can leave the paper broker)
    from nobitex_bot.nobitex_api import NobitexAPI  # noqa: E402
    engine._set_exchange(NobitexAPI(cfg.exchange, events=engine.events))
    if args.holdings:
        engine.storage.upsert_position("ATOMUSDT", Decimal("1.667996"),
                                       Decimal("1.942"), Decimal("0"))
        plans = engine.storage.kv_get("exit_plans", {}) or {}
        plans["ATOMUSDT"] = {"stop": 1.88374, "stop_pct": -3.0,
                             "auto": True, "stage": 0}
        engine.storage.kv_set("exit_plans", plans)
    engine.start()
    # the gate must stay OFF for this sandbox: point it at a throwaway
    # config file with an empty password instead of the user's config.json
    # (which is password-protected on purpose)
    gate_cfg = Path(cfg.engine.data_dir) / "sandbox_config.json"
    gate_cfg.parent.mkdir(parents=True, exist_ok=True)
    gate_cfg.write_text('{"web": {"password": ""}}', encoding="utf-8")
    auth = DashboardAuth(cfg, config_path=gate_cfg)
    print("[sandbox] dashboard http://127.0.0.1:%d | data=%s | live=%s "
          "auto=%s | read_only=%s | gate=%s"
          % (args.port, cfg.engine.data_dir, cfg.exchange.live_trading,
             engine.auto_trading, NobitexAPI.is_read_only(), auth.enabled()),
          flush=True)
    serve(engine, cfg.engine.server_host, args.port, auth=auth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
