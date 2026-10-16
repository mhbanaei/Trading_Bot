"""Live market universe — کاتالوگ زندهٔ همهٔ بازارهای نوبیتکس.

WHY THIS EXISTS (the snapshot problem)
--------------------------------------
The disk SnapshotCollector only ever covers the CYCLE's trading universe:
the top-N markets by 24h volume (engine.max_markets) that are switched on
in «بازار معاملات». A coin you HOLD can easily sit outside that set —
wallet-synced older coins, a market whose volume dropped, a section
switched off. For those markets the snapshot carries no price, and:

* `_manage_exits` had nothing to compare the stop-loss against, so the
  stop silently never fired even though the dashboard (which used a
  ticker fallback) showed the price far below it;
* «فروش» on a holding answered «بازار در اسنپ‌شات فعلی نیست»;
* the whole «همه بازارها» table could only ever show the snapshot subset.

The universe fixes that class of bug by fetching the exchange's OWN
catalog instead of a file: Nobitex returns every pair in ONE public call
(`GET /market/stats`), which is cached here for a few seconds. Prices,
volumes, 24h change, spreads and the tradable flag for ~all listed coins
are therefore available with NO snapshot, no cycle, and no per-market
round trip — and the same index feeds the whole-exchange scanner.

The disk copy (`data/markets/market_universe.json`) is only a warm-start
cache so a freshly restarted process can price holdings before its first
network call; it is never authoritative.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .utils import utc_now_iso

DEFAULT_TTL = 20.0
CACHE_NAME = "market_universe.json"
#: sort tokens accepted by ranked()
SORTS = ("volume", "change", "gainers", "losers", "price", "spread", "symbol")


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class MarketUniverse:
    """Thread-safe, TTL-cached index of every market the exchange lists."""

    def __init__(self, exchange, data_dir=None, ttl: float = DEFAULT_TTL,
                 events=None):
        self.exchange = exchange
        self.ttl = max(1.0, _f(ttl, DEFAULT_TTL))
        self.events = events
        self.data_dir = Path(data_dir) if data_dir else None
        self._lock = threading.Lock()
        self._rows: dict = {}
        self._fetched_at = 0.0
        self._updated_at = ""
        self._error = ""
        self._source = getattr(exchange, "name", "unknown")
        if self.data_dir is not None:
            self._load_cache()

    # ------------------------------------------------------------ helpers
    @property
    def cache_path(self) -> Path | None:
        if self.data_dir is None:
            return None
        return Path(self.data_dir) / "markets" / CACHE_NAME

    def age_seconds(self) -> float:
        if not self._fetched_at:
            return float("inf")
        return max(0.0, time.time() - self._fetched_at)

    def is_fresh(self) -> bool:
        return bool(self._rows) and self.age_seconds() <= self.ttl

    # ------------------------------------------------------------ fetching
    def _fetch(self) -> dict:
        """One shot at the exchange: its own whole-catalog call when the
        adapter has one, else a bounded markets()+ticker() sweep."""
        fn = getattr(self.exchange, "all_tickers", None)
        if callable(fn):
            try:
                doc = fn() or {}
                if doc:
                    return doc
            except Exception as exc:
                self._error = "all_tickers: %s" % exc
        return self._sweep()

    def _sweep(self) -> dict:
        """Fallback: ask the adapter for every market's ticker one by one."""
        out: dict = {}
        try:
            markets = self.exchange.markets() or []
        except Exception as exc:
            self._error = "markets: %s" % exc
            return out
        for m in markets:
            sym = (m or {}).get("symbol")
            if not sym:
                continue
            try:
                t = self.exchange.ticker(sym) or {}
            except Exception:
                continue
            if not t.get("last_price"):
                continue
            out[sym] = {
                **t,
                "base_asset": (m or {}).get("base_asset"),
                "quote_asset": (m or {}).get("quote_asset"),
                "tradable": bool((m or {}).get("tradable", True)),
            }
        return out

    def refresh(self, force: bool = False) -> dict:
        """(Re)load the catalog unless the cache is still fresh.

        Never raises: a failing exchange returns the previous rows (or the
        disk warm-start) so the engine can keep pricing what it already
        knows instead of trading blind.
        """
        with self._lock:
            if not force and self._rows and self.age_seconds() <= self.ttl:
                return self._rows
            try:
                rows = self._fetch()
            except Exception as exc:            # defensive: adapters vary
                rows = {}
                self._error = str(exc)
            if rows:
                self._rows = rows
                self._fetched_at = time.time()
                self._updated_at = utc_now_iso()
                self._error = ""
                self._save_cache(rows)
            elif not self._rows:
                self._error = self._error or "empty catalog"
            return self._rows

    # ------------------------------------------------------------ lookups
    def rows(self) -> dict:
        with self._lock:
            return self._rows

    def row(self, symbol: str) -> dict | None:
        return self.rows().get((symbol or "").strip().upper())

    def price(self, symbol: str) -> float | None:
        r = self.row(symbol)
        if not r:
            return None
        px = _f(r.get("last_price"))
        return px if px > 0 else None

    def price_any(self, symbol: str) -> float | None:
        """Price of `symbol`, else of the SAME coin in the other quote.

        A wallet coin can be listed in both markets while only one of them
        is the one we hold; for valuation/exits either quote is a valid
        measure of the coin's value.
        """
        px = self.price(symbol)
        if px is not None:
            return px
        sym = (symbol or "").strip().upper()
        for other in self.alternates(sym):
            px = self.price(other)
            if px is not None:
                return px
        return None

    @staticmethod
    def alternates(symbol: str) -> list:
        sym = (symbol or "").strip().upper()
        if sym.endswith("USDT"):
            return [sym[:-4] + "IRT"]
        if sym.endswith("IRT"):
            return [sym[:-3] + "USDT"]
        return []

    def symbols(self) -> set:
        return set(self.rows().keys())

    def tradable_symbols(self) -> set:
        return {s for s, r in self.rows().items() if r.get("tradable", True)}

    def status(self) -> dict:
        """Small, JSON-safe health block for /api/status."""
        with self._lock:
            return {
                "count": len(self._rows),
                "tradable": sum(1 for r in self._rows.values()
                                if r.get("tradable", True)),
                "updated_at": self._updated_at or None,
                "age_seconds": (round(self.age_seconds(), 1)
                                if self._fetched_at else None),
                "ttl_seconds": self.ttl,
                "source": self._source,
                "error": self._error or "",
                "from_disk": bool(self._rows) and not self._fetched_at,
            }

    # ----------------------------------------------------------- analysis
    def ranked(self, sort: str = "volume", quote: str | None = None,
               limit: int | None = None, search: str = "",
               exclude=(), min_quote_volume: float = 0.0,
               only_tradable: bool = True, extras: dict | None = None) -> list:
        """Whole-exchange rows, ranked — the snapshot-free scanner.

        sort: volume | change | gainers | losers | price | spread | symbol
        extras: optional {symbol: {...}} merged into each row (the engine
        hangs holding/candidate flags there).
        """
        key = (sort or "volume").strip().lower()
        if key not in SORTS:
            key = "volume"
        needle = (search or "").strip().upper()
        q = (quote or "").strip().upper() or None
        drop = {str(x).strip().upper() for x in (exclude or ())}
        out = []
        for sym, r in self.rows().items():
            if only_tradable and not r.get("tradable", True):
                continue
            if q and (r.get("quote_asset") or "").upper() != q:
                continue
            if needle and needle not in sym:
                continue
            if sym in drop:
                continue
            if min_quote_volume and \
                    _f(r.get("quote_volume_24h")) < min_quote_volume:
                continue
            row = dict(r)
            row["symbol"] = sym
            if extras and sym in extras:
                row.update(extras[sym] or {})
            out.append(row)
        if key in ("change", "gainers"):
            out.sort(key=lambda x: -_f(x.get("change_24h")))
        elif key == "losers":
            out.sort(key=lambda x: _f(x.get("change_24h")))
        elif key == "price":
            out.sort(key=lambda x: -_f(x.get("last_price")))
        elif key == "spread":
            out.sort(key=lambda x: _f(x.get("spread_percent")))
        elif key == "symbol":
            out.sort(key=lambda x: x["symbol"])
        else:  # volume
            out.sort(key=lambda x: -_f(x.get("quote_volume_24h")))
        if limit and limit > 0:
            return out[:int(limit)]
        return out

    def movers(self, kind: str = "gainers", limit: int = 20,
               min_quote_volume: float = 0.0, quote: str | None = None) -> list:
        """Top 24h movers over the WHOLE exchange (no snapshot needed)."""
        return self.ranked(sort=kind, limit=limit, quote=quote,
                           min_quote_volume=min_quote_volume)

    # ------------------------------------------------------- disk warm-up
    def _load_cache(self) -> None:
        path = self.cache_path
        if not path or not path.exists():
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            rows = doc.get("rows") if isinstance(doc, dict) else None
            if isinstance(rows, dict) and rows:
                self._rows = rows
                self._updated_at = str(doc.get("updated_at") or "")
                self._source = str(doc.get("source") or self._source)
        except Exception:
            pass

    def _save_cache(self, rows: dict) -> None:
        path = self.cache_path
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "schema_version": "1.0",
                "source": self._source,
                "updated_at": self._updated_at,
                "count": len(rows),
                "rows": rows,
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass
