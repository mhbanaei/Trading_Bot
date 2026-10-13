#!/usr/bin/env python3
"""Nobitex Smart Trading Bot — CLI entry point.

Usage:
  python run.py                 # start engine + dashboard (demo mode)
  python run.py --port 9000     # custom dashboard port
  python run.py --cycles 5      # run N cycles then exit (no server)
  python run.py --once          # single cycle, print summary, exit
  python run.py --live          # live view (trades + account) for a debug run
  python run.py --doctor        # check THIS host (VPS) before starting

The live view is OFF by default: it costs one engine.status() per cycle on
top of the trading work, and this bot runs for weeks on a small VPS.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nobitex_bot.config import load_config
from nobitex_bot.engine import TradingEngine

# windows console-control handlers must stay referenced or the callback
# object is garbage collected and the handler silently stops firing
_CTRL_HANDLER = []

# CTRL_CLOSE_EVENT (X on the console window), CTRL_LOGOFF_EVENT,
# CTRL_SHUTDOWN_EVENT: all three end with the process being killed, so the
# only chance to stop the engine cleanly is inside the handler.
_CTRL_STOP_EVENTS = (2, 5, 6)


def install_console_stop(engine, console=None) -> bool:
    """Stop the engine BEFORE Windows kills this process.

    Closing the cmd window (or logging off / shutting down) does not give
    Python a KeyboardInterrupt: the OS runs the console control handlers
    and then terminates the process. Without a handler the trading thread
    is cut off wherever it happens to be — including in the middle of a
    SQLite commit, which is how a large trading_bot.db ends up malformed.
    Stopping first means the database is never left half-written.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _handler(event):
            if event in _CTRL_STOP_EVENTS:
                try:
                    engine.stop()
                except Exception:
                    pass
                if console is not None:
                    try:
                        console.close()
                    except Exception:
                        pass
            return False  # let Windows finish its own shutdown path

        cb = proto(_handler)
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True):
            return False
        _CTRL_HANDLER.append(cb)
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Nobitex Smart Trading Bot")
    ap.add_argument("--port", type=int, default=None,
                    help="dashboard port (default from config.json)")
    ap.add_argument("--host", default=None, help="bind host")
    ap.add_argument("--cycles", type=int, default=0,
                    help="stop after N cycles (0 = unlimited)")
    ap.add_argument("--once", action="store_true",
                    help="run a single cycle and exit")
    ap.add_argument("--no-server", action="store_true",
                    help="do not start the web dashboard")
    ap.add_argument("--reset", action="store_true",
                    help="reset paper portfolio before starting")
    ap.add_argument("--doctor", action="store_true",
                    help="pre-flight check of this host: TLS trust store, "
                         "clock, dashboard bind/port, egress IP "
                         "(exit 2 when a check FAILs)")
    ap.add_argument("--live", action="store_true",
                    help="OPT-IN live view: one line per event plus a "
                         "per-cycle account line (costs an extra "
                         "engine.status() every cycle, so it is off by "
                         "default)")
    args = ap.parse_args()

    # OFF by default on purpose: the live view prints every event and asks
    # the engine for a full status() once per cycle, and the bot has to run
    # for weeks on a small VPS. --live turns it on for a debugging run.
    live_view = bool(args.live)

    cfg = load_config()
    if args.port:
        cfg.engine.server_port = args.port
    if args.host:
        cfg.engine.server_host = args.host
    if args.cycles:
        cfg.engine.max_cycles = args.cycles

    if args.doctor:
        # first thing to run on a new machine/VPS: it must not touch the
        # account, so it happens before the engine is even built
        from nobitex_bot.doctor import exit_code, render, run_doctor
        checks = run_doctor(cfg)
        print(render(checks))
        return exit_code(checks)

    engine = TradingEngine(cfg)
    print("=" * 62)
    print(" Nobitex Smart Trading Bot v%s" % "1.0.0")
    print(" mode: %s | data: %s" % (cfg.exchange.mode, cfg.engine.data_dir))
    print(" dashboard: http://127.0.0.1:%d" % cfg.engine.server_port)
    print("=" * 62)
    sys.stdout.flush()

    console = None
    if live_view:
        from nobitex_bot.console import LiveConsole
        console = LiveConsole(engine).attach()

    # closing the window must stop the bot WITHOUT cutting a database
    # write in half (see install_console_stop)
    install_console_stop(engine, console)

    if args.reset:
        engine.portfolio.reset()
        print("[cli] paper portfolio reset to %s" % cfg.exchange_starting_balance
              if hasattr(cfg, "exchange_starting_balance") else
              "[cli] paper portfolio reset")

    engine.start()  # background cycle loop

    if args.once:
        summary = engine.run_cycle()
        engine.stop()
        print(json.dumps(summary, indent=2, default=str))
        if console:
            console.close()
        return 0

    if not args.no_server:
        from nobitex_bot.web import serve
        try:
            serve(engine, cfg.engine.server_host, cfg.engine.server_port)
        except KeyboardInterrupt:
            pass
    else:
        try:
            while engine.running():
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    engine.stop()
    if console:
        console.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
