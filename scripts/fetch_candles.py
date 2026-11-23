"""Backfill real Nobitex candles into the SQLite ``candles`` table.

The backtest replays ``--db`` rows, but until now nothing wrote them: the
collector fetched candles for the AI and dropped them. This script fills the
table from the PUBLIC OHLC endpoint only — no login, no wallet call, no order.
It is safe to run while the bot is running (writes are additive and the
``(symbol, timeframe, timestamp)`` key makes a re-fetch a no-op).

The public endpoint returns at most 500 bars per request, so ``--limit`` is
capped at 500 by the exchange, not by this script.

Usage
-----
    python scripts/fetch_candles.py                      # top 12 by volume, 1h+4h
    python scripts/fetch_candles.py --top 20 --timeframes 1h,4h,1d
    python scripts/fetch_candles.py --symbols BTCIRT,ETHIRT,USDTIRT

The ledger lives at ``<data-dir>/trading_bot.db`` (that path is fixed by
``Storage``), so ``--data-dir`` — not a bare sqlite filename — selects it.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI  # noqa: E402
from nobitex_bot.storage import Storage  # noqa: E402

API_CANDLE_CAP = 500


def liquid_symbols(api: NobitexAPI, top: int, min_quote_usdt: float) -> list:
    """Most-traded markets, quote volume normalised to USDT-equivalents."""
    tickers = api.all_tickers()
    usd_irt = float((tickers.get("USDTIRT") or {}).get("last_price") or 0)
    if usd_irt <= 0:
        raise SystemExit("[candles] USDTIRT price unavailable; cannot rank "
                         "markets by volume")
    ranked = []
    for symbol, ticker in tickers.items():
        if not ticker.get("tradable") or not ticker.get("last_price"):
            continue
        quote_volume = float(ticker.get("quote_volume_24h") or 0)
        usdt_value = (quote_volume / usd_irt if symbol.endswith("IRT")
                      else quote_volume)
        if usdt_value >= min_quote_usdt:
            ranked.append((usdt_value, symbol))
    ranked.sort(reverse=True)
    return [symbol for _value, symbol in ranked[:max(1, top)]]


def fetch(api: NobitexAPI, symbol: str, timeframe: str, limit: int,
          attempts: int = 3) -> list:
    for attempt in range(attempts):
        try:
            return api.candles(symbol, timeframe, limit=limit)
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    print("  %-12s %-3s fetch failed: %s" % (symbol, timeframe, last))
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python scripts/fetch_candles.py")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--symbols", default="",
                    help="comma separated markets; empty = rank by volume")
    ap.add_argument("--top", type=int, default=12,
                    help="how many volume-ranked markets to fetch")
    ap.add_argument("--min-quote-usdt", type=float, default=50000.0)
    ap.add_argument("--timeframes", default="1h,4h")
    ap.add_argument("--limit", type=int, default=API_CANDLE_CAP)
    args = ap.parse_args(argv)

    limit = max(70, min(args.limit, API_CANDLE_CAP))
    timeframes = [tf.strip() for tf in args.timeframes.split(",") if tf.strip()]
    wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    cfg = Config()
    cfg.exchange.mode = "nobitex"
    api = NobitexAPI(cfg.exchange)
    storage = Storage(args.data_dir)

    symbols = wanted or liquid_symbols(api, args.top, args.min_quote_usdt)
    print("[candles] %d markets x %s x %d bars -> %s"
          % (len(symbols), ",".join(timeframes), limit, storage.db_path))

    total_new = total_seen = 0
    stored = []
    for symbol in symbols:
        for timeframe in timeframes:
            bars = fetch(api, symbol, timeframe, limit)
            if not bars:
                continue
            new = storage.upsert_candles(symbol, timeframe, bars)
            total_new += new
            total_seen += len(bars)
            stored.append((symbol, timeframe, len(bars), new))
            print("  %-12s %-3s %4d bars (%3d new) %s .. %s"
                  % (symbol, timeframe, len(bars), new,
                     bars[0]["timestamp"], bars[-1]["timestamp"]))

    if not stored:
        print("[candles] nothing stored — the public endpoint returned no "
              "usable candles", file=sys.stderr)
        return 2
    markets = sorted({row[0] for row in stored})
    print("[candles] stored %d markets / %d rows (%d new)"
          % (len(markets), total_seen, total_new))
    print("[candles] backtest: python -m nobitex_bot.backtest --db %s "
          "--timeframe %s --symbols %s"
          % (storage.db_path, timeframes[0], ",".join(markets)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
