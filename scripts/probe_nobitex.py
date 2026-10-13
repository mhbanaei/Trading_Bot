"""Probe real Nobitex API connectivity using the project's own adapter.

Public market data + login endpoint reachability (no real credentials).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI, NobitexError  # noqa: E402


def main() -> None:
    cfg = Config()
    cfg.exchange.mode = "nobitex"
    api = NobitexAPI(cfg.exchange)

    print("== 1. public /market/stats (whole market) ==")
    t0 = time.time()
    try:
        stats = api._stats_all()
        pairs = list(stats.keys())
        print("ok in %.1fs, %d pairs, sample: %s" % (
            time.time() - t0, len(pairs), pairs[:5]))
        btc = stats.get("btc-rls") or stats.get("btc-usdt") or {}
        print("btc latest:", btc.get("latest"))
    except Exception as exc:
        print("FAILED in %.1fs: %r" % (time.time() - t0, exc))

    print("== 2. login endpoint reachability (dummy creds) ==")
    t0 = time.time()
    try:
        api.session.login(username="probe@example.com",
                          password="wrongpass", totp_code="000000")
        print("unexpected success")
    except NobitexError as exc:
        print("API answered in %.1fs -> code=%s retryable=%s msg=%s" % (
            time.time() - t0, exc.code, exc.retryable, str(exc)[:120]))
    except Exception as exc:
        print("NETWORK/other failure in %.1fs: %r" % (time.time() - t0, exc))

    print("== 3. orderbook public ==")
    t0 = time.time()
    try:
        book = api.orderbook("BTCUSDT", depth=5)
        print("ok in %.1fs, bids=%d asks=%d top=%s" % (
            time.time() - t0, len(book["bids"]), len(book["asks"]),
            (book["bids"] or [{}])[0].get("price")))
    except Exception as exc:
        print("FAILED in %.1fs: %r" % (time.time() - t0, exc))

    print("== 4. candles public ==")
    t0 = time.time()
    try:
        c = api.candles("BTCUSDT", "30m", 5)
        print("ok in %.1fs, %d candles, last close=%s" % (
            time.time() - t0, len(c), c[-1]["close"] if c else None))
    except Exception as exc:
        print("FAILED in %.1fs: %r" % (time.time() - t0, exc))


if __name__ == "__main__":
    main()
