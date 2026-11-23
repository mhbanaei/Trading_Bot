"""Snapshot collector — contract sections 7-10, 54-57.

Each cycle collects one independent snapshot: per-market ticker + orderbook +
recent trades + candles, all validated before storage. Files are written
atomically (tmp -> fsync -> rename, SS56) with a SHA-256 integrity block
(SS57) and full lineage (SS9).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .enums import SnapshotStatus
from .events import EventBus
from .storage import Storage
from .utils import sha256_bytes, split_symbol, utc_now_iso
from .validation import (
    ValidationResult, data_quality_from_issues, validate_iso_timestamp,
    validate_orderbook, validate_positive,
)

SNAPSHOT_FORMAT = "snapshot_%06d.json"


def aligned_candles(rows: list, step_seconds: int) -> list:
    """One bar per timeframe bucket, first seen wins.

    Exchange candles already sit on the timeframe boundary. The demo fixture
    regenerates its walk from `now` on every call, so storing its rows as-is
    would add ~60 near-duplicate rows per market per cycle; flooring to the
    bucket keeps the table at one row per real period.
    """
    seen, out = set(), []
    for row in rows or []:
        try:
            ts = datetime.fromisoformat(
                str(row["timestamp"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        bucket = (int(ts.replace(tzinfo=timezone.utc).timestamp())
                  // step_seconds * step_seconds)
        if bucket in seen:
            continue
        seen.add(bucket)
        item = dict(row)
        item["timestamp"] = datetime.fromtimestamp(
            bucket, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(item)
    return out


class MarketRegistry:
    """Append-compatible registry (SS6): removed markets get tradable=false."""

    def __init__(self, data_dir: Path, source: str):
        self.path = Path(data_dir) / "markets" / "market_registry.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.source = source
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.doc = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.doc = None
        if not getattr(self, "doc", None):
            self.doc = {"schema_version": "1.0", "registry_version": 1,
                        "updated_at": utc_now_iso(), "source": self.source,
                        "markets": {}}
        self.doc.setdefault("markets", {})

    def update(self, discovered: list) -> dict:
        with self._lock:
            now = utc_now_iso()
            known = set(self.doc["markets"].keys())
            seen = set()
            new_markets = []
            for m in discovered:
                sym = m["symbol"]
                seen.add(sym)
                if sym in known:
                    entry = self.doc["markets"][sym]
                    entry["status"] = m.get("status", entry.get("status"))
                    entry["tradable"] = m.get("tradable", entry.get("tradable"))
                    entry["last_seen_at"] = now
                    entry["updated_at"] = now
                else:
                    entry = dict(m)
                    entry["created_at"] = now
                    entry["first_seen_at"] = now
                    entry["last_seen_at"] = now
                    self.doc["markets"][sym] = entry
                    new_markets.append(sym)
            for sym in known - seen:
                # SS5: never delete — disable instead
                self.doc["markets"][sym]["tradable"] = False
                self.doc["markets"][sym]["status"] = "INACTIVE"
                self.doc["markets"][sym]["updated_at"] = now
            self.doc["registry_version"] = int(self.doc.get("registry_version", 1)) + 1
            self.doc["updated_at"] = now
            self._save()
            return {"new": new_markets, "disabled": sorted(known - seen)}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.doc, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def tradable_symbols(self) -> list:
        return [s for s, m in self.doc["markets"].items()
                if m.get("tradable") and m.get("status") == "ACTIVE"]

    def all_symbols(self) -> set:
        return set(self.doc["markets"].keys())

    def market(self, symbol: str) -> dict | None:
        return self.doc["markets"].get(symbol)


class SnapshotCollector:
    def __init__(self, cfg: Config, exchange, events: EventBus, storage: Storage):
        self.cfg = cfg
        self.exchange = exchange
        self.events = events
        self.storage = storage
        self.snapshots_dir = Path(cfg.engine.data_dir) / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.registry = MarketRegistry(Path(cfg.engine.data_dir), exchange.name)
        self._seq = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ sequence
    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def discover_markets(self) -> dict:
        try:
            discovered = self.exchange.markets()
        except Exception as exc:
            self.events.error("DATA_COLLECTOR", "MARKET_DISCOVERY_FAILED",
                              "HIGH", str(exc), retryable=True)
            return {"new": [], "disabled": []}
        result = self.registry.update(discovered)
        for sym in result["new"]:
            self.events.publish("MARKET_DISCOVERED", "data_collector", "",
                                {"symbol": sym})
        for sym in result["disabled"]:
            self.events.publish("MARKET_DISABLED", "data_collector", "",
                                {"symbol": sym})
        return result

    # -------------------------------------------------------------- collect
    def collect(self, cycle_id: str, extra_symbols: list | None = None) -> dict:
        seq = self._next_seq()
        snapshot_id = "snap_%s_%06d" % (
            datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"), seq)
        started = utc_now_iso()

        symbols = self.registry.tradable_symbols()
        # whole-catalog movers that the volume/top-N universe dropped ride
        # along this cycle (bounded by the engine): so a promising coin
        # ANYWHERE on the exchange can reach the deep scanner — with no
        # permanent registry change (next cycle re-decides the list)
        seen = set(symbols)
        for sym in (extra_symbols or []):
            sym = str(sym or "").strip().upper()
            if sym and sym not in seen:
                try:
                    if self.exchange.ticker(sym).get("last_price"):
                        symbols.append(sym)
                        seen.add(sym)
                except Exception:
                    continue
        markets = []
        for sym in symbols:
            try:
                markets.append(self._collect_market(sym))
            except Exception as exc:
                self.events.error("DATA_COLLECTOR", "MARKET_COLLECTION_FAILED",
                                  "WARNING", "%s: %s" % (sym, exc),
                                  retryable=True, cycle_id=cycle_id)

        valid = [m for m in markets
                 if m["data_quality"]["status"] in ("GOOD", "WARNING")]
        total_score = sum(m["data_quality"]["score"] for m in markets) or 1.0
        quality = sum(m["data_quality"]["score"] for m in valid) / total_score \
            if markets else 0.0

        portfolio = self._collect_portfolio()
        status = SnapshotStatus.COMPLETE if valid \
            else SnapshotStatus.INVALID

        snapshot = {
            "schema_version": "1.0",
            "snapshot_id": snapshot_id,
            "cycle_id": cycle_id,
            "created_at": started,
            "collection_started_at": started,
            "collection_finished_at": utc_now_iso(),
            "status": status.value,
            "source": self.exchange.name,
            "market_count": len(markets),
            "valid_market_count": len(valid),
            "data_quality_score": round(quality, 4),
            "markets": markets,
            "portfolio": portfolio,
            "open_orders": [],
            "recent_trades": [],
            "system_state": {"engine": "idle", "safe_mode": False},
        }
        self._finalize(snapshot)
        return snapshot

    def _collect_market(self, symbol: str) -> dict:
        issues = ValidationResult()
        ticker = self.exchange.ticker(symbol)
        book = self.exchange.orderbook(symbol, self.cfg.exchange.book_levels)
        # BANDWIDTH: recent-trades fetches dropped — nothing in the engine,
        # scanner, features or AI ever reads m["trades"]; it was 20 extra
        # HTTP calls + payload per market per cycle for data nobody used.
        # Candles are bounded by the SAME budget the AI context honors
        # (ai.max_candles_per_market) instead of a hard-coded 64.
        trades: list = []
        try:
            candle_limit = max(21, int(getattr(
                self.cfg.ai, "max_candles_per_market", 60) or 60))
        except Exception:
            candle_limit = 60
        candles_30m = self.exchange.candles(symbol, "30m",
                                            limit=candle_limit)
        # Persist what was just fetched: this table is what
        # `python -m nobitex_bot.backtest --db ...` replays. The primary key
        # makes each cycle's overlap a no-op instead of growth.
        try:
            self.storage.upsert_candles(
                symbol, "30m", aligned_candles(candles_30m, 1800))
        except Exception as exc:
            self.events.error("DATA_COLLECTOR", "CANDLE_STORE_FAILED",
                              "WARNING", "%s: %s" % (symbol, exc),
                              retryable=True)

        validate_positive(ticker.get("last_price"), issues, "last_price")
        validate_orderbook(book, issues)
        if ticker.get("timestamp"):
            validate_iso_timestamp(ticker["timestamp"], issues, "timestamp")

        ohlc = {"timeframe": "30m", "candles": candles_30m}
        dq = data_quality_from_issues(issues.issues)
        base, quote = split_symbol(symbol)
        return {
            "symbol": symbol,
            "base_asset": base,
            "quote_asset": quote,
            "timestamp": ticker.get("timestamp"),
            "received_at": utc_now_iso(),
            "last_price": ticker.get("last_price"),
            "best_bid": ticker.get("best_bid"),
            "best_ask": ticker.get("best_ask"),
            "spread": ticker.get("spread"),
            "spread_percent": ticker.get("spread_percent"),
            "volume_24h": ticker.get("volume_24h"),
            "quote_volume_24h": ticker.get("quote_volume_24h"),
            "change_24h": ticker.get("change_24h"),
            "high_24h": ticker.get("high_24h"),
            "low_24h": ticker.get("low_24h"),
            "orderbook": book,
            "trades": trades,
            "ohlc": ohlc,
            "liquidity": {},
            "volatility": {},
            "data_quality": dq,
        }

    def _collect_portfolio(self) -> dict:
        try:
            return self.exchange.portfolio()
        except Exception as exc:
            self.events.error("DATA_COLLECTOR", "PORTFOLIO_FAILED", "WARNING",
                              str(exc), retryable=True)
            return {"timestamp": utc_now_iso(), "account": {}, "assets": [],
                    "collection_error": str(exc)}

    # -------------------------------------------------------------- write
    def _persist_enabled(self) -> bool:
        """Snapshot files are OFF by default (engine.persist_snapshots).

        The data is collected from the exchange API every cycle and kept in
        memory (`engine.latest_snapshot`); writing a ~400KB JSON per cycle
        only filled the data folder (~200MB/day) and was never read except
        for debugging.
        """
        return bool(getattr(self.cfg.engine, "persist_snapshots", False))

    def _finalize(self, snapshot: dict) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False, default=str)
        snapshot["integrity"] = {
            "algorithm": "SHA-256",
            "checksum": sha256_bytes(payload.encode("utf-8")).replace(
                "sha256:", ""),
        }
        if self._persist_enabled():
            name = SNAPSHOT_FORMAT % self._seq
            final = self.snapshots_dir / name
            tmp = self.snapshots_dir / (name + ".tmp")
            payload = json.dumps(snapshot, ensure_ascii=False, default=str)
            tmp.write_text(payload, encoding="utf-8")
            with open(tmp, "ab") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
            self._prune_snapshots()

        self.events.publish("SNAPSHOT_CREATED", "data_collector",
                            snapshot["cycle_id"],
                            {"snapshot_id": snapshot["snapshot_id"],
                             "status": snapshot["status"]})
        if snapshot["status"] == "COMPLETE":
            self.events.publish("SNAPSHOT_COMPLETED", "data_collector",
                                snapshot["cycle_id"],
                                {"snapshot_id": snapshot["snapshot_id"],
                                 "market_count": snapshot["market_count"],
                                 "valid_market_count": snapshot["valid_market_count"]})
        else:
            self.events.publish("SNAPSHOT_INVALID", "data_collector",
                                snapshot["cycle_id"],
                                {"snapshot_id": snapshot["snapshot_id"]})

    def _prune_snapshots(self, max_keep: int | None = None) -> int:
        """Keep only the newest `max_keep` snapshot files (SS-retention).

        Snapshots are large (~400KB each); without pruning the data folder
        grows ~200MB/day. When snapshot files are disabled every file is
        removed (temp data must not accumulate), otherwise the cap comes
        from engine.max_snapshots. Returns removed count.
        """
        try:
            max_keep = int(getattr(self.cfg.engine, "max_snapshots", 0))
        except Exception:
            max_keep = 0
        files = sorted(self.snapshots_dir.glob("snapshot_*.json"))
        if max_keep <= 0:
            removed = 0
            for f in files:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            return removed
        excess = len(files) - max_keep
        removed = 0
        for f in files[:max(0, excess)]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def latest_snapshot(self) -> dict | None:
        files = sorted(self.snapshots_dir.glob("snapshot_*.json"))
        if not files:
            return None
        try:
            return json.loads(files[-1].read_text(encoding="utf-8"))
        except Exception:
            return None
