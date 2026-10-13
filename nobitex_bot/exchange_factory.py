"""Exchange factory — selects the adapter from config (demo | nobitex)."""
from __future__ import annotations

from .config import Config
from .demo_market import DemoMarket
from .nobitex_api import NobitexAPI


def make_exchange(cfg: Config):
    mode = (cfg.exchange.mode or "demo").lower()
    if mode == "nobitex":
        has_login = bool(cfg.exchange.nobitex_username and
                         cfg.exchange.nobitex_password)
        has_key = bool(cfg.exchange.nobitex_api_key)
        if not (has_login or has_key):
            # SS101 fail-safe: never block startup on unreachable/empty
            # credentials — fall back to the demo simulator and say why.
            print("[warn] exchange.mode=nobitex but no credentials/"
                  "api_key configured — running DEMO simulator instead "
                  "(login from the dashboard to attach the real account).")
            cfg.exchange.mode = "demo"   # honest status: we ARE on the simulator
            return DemoMarket(cfg.exchange, events=None)
        return NobitexAPI(cfg.exchange, events=None)
    if mode == "demo":
        return DemoMarket(cfg.exchange, events=None)
    raise ValueError("unknown exchange mode: %s" % mode)
