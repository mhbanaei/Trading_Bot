"""SQLite storage — contract section 87 (DB is for trades/orders/fills/
features/candles/events/performance) and section 91 (unique constraints
prevent duplicate immutable entities)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'nobitex',
    PRIMARY KEY (symbol, timeframe, timestamp)          -- SS90/SS91 unique key
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT UNIQUE,
    decision_id TEXT,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL DEFAULT 'LIMIT',
    price TEXT NOT NULL,
    amount TEXT NOT NULL,
    matched_amount TEXT NOT NULL DEFAULT '0',
    unmatched_amount TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL,
    fee_asset TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    exchange_order_id TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    trade_id TEXT PRIMARY KEY,                          -- SS91 duplicate prevention
    order_id TEXT NOT NULL,
    decision_id TEXT,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    amount TEXT NOT NULL,
    gross_value TEXT NOT NULL,
    fee_amount TEXT NOT NULL,
    fee_asset TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    decision_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    payload TEXT NOT NULL,
    validation_ok INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    risk_decision_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_position_size TEXT,
    approved_max_loss TEXT,
    rejection_reasons TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    risk_decision_id TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    amount TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance (
    decision_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    expected_return REAL,
    actual_return REAL,
    actual_pnl TEXT,
    direction_correct INTEGER,
    profitable INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    market TEXT PRIMARY KEY,
    side TEXT NOT NULL DEFAULT 'LONG',
    amount TEXT NOT NULL DEFAULT '0',
    avg_entry TEXT NOT NULL DEFAULT '0',
    realized_pnl TEXT NOT NULL DEFAULT '0',
    opened_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(timestamp);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


class Storage:
    """Thread-safe SQLite wrapper. All money columns are TEXT (exact Decimal)."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "trading_bot.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        # lightweight migration for DBs created before live-order support
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(orders)").fetchall()}
        if "exchange_order_id" not in cols:
            self._conn.execute(
                "ALTER TABLE orders ADD COLUMN exchange_order_id TEXT")
        # positions: track the LAST buy time + cumulative invested Toman
        pcols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(positions)").fetchall()}
        if "last_buy_at" not in pcols:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN last_buy_at TEXT")
        if "total_buys_irt" not in pcols:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN total_buys_irt TEXT")
        # SS100d: price of the MOST RECENT buy — the UI «قیمت خرید» shows
        # this after a new purchase (reset view) while avg_entry stays the
        # weighted mean for correct P&L
        if "last_buy_price" not in pcols:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN last_buy_price TEXT")
        self._conn.commit()

    # ------------------------------------------------------------- candles
    def upsert_candle(self, symbol: str, timeframe: str, ts: str,
                      o, h, l, c, volume, source: str = "nobitex") -> bool:
        """Returns False if the candle already existed (duplicate prevented)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?,?,?)",
                (symbol, timeframe, ts, str(o), str(h), str(l), str(c), str(volume), source))
            self._conn.commit()
            return cur.rowcount > 0

    def candles(self, symbol: str, timeframe: str, limit: int = 200) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT timestamp, open, high, low, close, volume FROM candles "
                "WHERE symbol=? AND timeframe=? ORDER BY timestamp DESC LIMIT ?",
                (symbol, timeframe, limit)).fetchall()
        out = []
        for ts, o, h, l, c, v in reversed(rows):
            out.append({"timestamp": ts, "open": o, "high": h, "low": l,
                        "close": c, "volume": v})
        return out

    # -------------------------------------------------------------- orders
    def save_order(self, order: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO orders (order_id, client_order_id, "
                "decision_id, market, side, order_type, price, amount, "
                "matched_amount, unmatched_amount, status, fee_asset, "
                "created_at, updated_at, exchange_order_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order["order_id"], order.get("client_order_id"),
                 order.get("decision_id"), order["market"], order["side"],
                 order.get("order_type", "LIMIT"), str(order["price"]),
                 str(order["amount"]), str(order.get("matched_amount", "0")),
                 str(order.get("unmatched_amount", "0")), order["status"],
                 order.get("fee_asset"), order["created_at"],
                 order.get("updated_at", order["created_at"]),
                 order.get("exchange_order_id")))
            self._conn.commit()

    def update_order_status(self, order_id: str, status: str,
                            matched=None) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT amount, matched_amount FROM orders WHERE order_id=?",
                (order_id,)).fetchone()
            if not row:
                return
            amount_s, matched_s = row
            if matched is not None:
                matched_s = str(matched)
            from .utils import d
            unmatched = d(amount_s) - d(matched_s)
            self._conn.execute(
                "UPDATE orders SET status=?, matched_amount=?, unmatched_amount=?, "
                "updated_at=? WHERE order_id=?",
                (status, matched_s, str(unmatched), utc_now(), order_id))
            self._conn.commit()

    def orders(self, status: str | None = None, limit: int = 200) -> list:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
                    (limit,)).fetchall()
        cols = ["order_id", "client_order_id", "decision_id", "market", "side",
                "order_type", "price", "amount", "matched_amount",
                "unmatched_amount", "status", "fee_asset", "created_at",
                "updated_at", "exchange_order_id"]
        return [dict(zip(cols, r)) for r in rows]

    # --------------------------------------------------------------- fills
    def save_fill(self, fill: dict) -> bool:
        """Returns False if duplicate trade_id (SS91)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (fill["trade_id"], fill["order_id"], fill.get("decision_id"),
                 fill["market"], fill["side"], str(fill["price"]),
                 str(fill["amount"]), str(fill["gross_value"]),
                 str(fill["fee_amount"]), fill["fee_asset"], fill["timestamp"]))
            self._conn.commit()
            return cur.rowcount > 0

    def fills_by_base(self, base: str, limit: int = 200) -> list:
        """All fills whose market starts with the base asset (ARB*) —
        covers the same coin traded under two market names
        (ARBIRT + ARBITRUMIRT)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fills WHERE market LIKE ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (base + "%", limit)).fetchall()
        cols = ["trade_id", "order_id", "decision_id", "market", "side",
                "price", "amount", "gross_value", "fee_amount", "fee_asset",
                "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    def fills(self, market: str | None = None, limit: int = 200) -> list:
        with self._lock:
            if market:
                rows = self._conn.execute(
                    "SELECT * FROM fills WHERE market=? ORDER BY timestamp DESC LIMIT ?",
                    (market, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM fills ORDER BY timestamp DESC LIMIT ?",
                    (limit,)).fetchall()
        cols = ["trade_id", "order_id", "decision_id", "market", "side",
                "price", "amount", "gross_value", "fee_amount", "fee_asset",
                "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------------------------------------------------------- decisions
    def save_ai_decision(self, decision: dict, validation_ok: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ai_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                (decision["decision_id"], decision.get("cycle_id", ""),
                 decision.get("snapshot_id", ""), decision.get("symbol", ""),
                 decision.get("action", ""), float(decision.get("confidence", 0)),
                 json.dumps(decision, default=str), 1 if validation_ok else 0,
                 decision.get("created_at", utc_now())))
            self._conn.commit()

    def ai_decision(self, decision_id: str) -> dict | None:
        """Full AI decision payload by id (or None)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM ai_decisions WHERE decision_id=?",
                (decision_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def save_risk_decision(self, rd: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO risk_decisions VALUES (?,?,?,?,?,?,?,?)",
                (rd["risk_decision_id"], rd.get("decision_id", ""),
                 rd["status"], str(rd.get("approved_position_size", "")),
                 str(rd.get("approved_max_loss", "")),
                 json.dumps(rd.get("rejection_reasons", [])),
                 json.dumps(rd, default=str), rd.get("created_at", utc_now())))
            self._conn.commit()

    def save_execution(self, ex: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ex["execution_id"], ex["decision_id"], ex["risk_decision_id"],
                 ex["market"], ex["side"], str(ex["price"]), str(ex["amount"]),
                 ex["status"], json.dumps(ex, default=str),
                 ex.get("created_at", utc_now())))
            self._conn.commit()

    def update_execution_status(self, execution_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE executions SET status=? WHERE execution_id=?",
                (status, execution_id))
            self._conn.commit()

    def decisions_recent(self, limit: int = 50) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT decision_id, cycle_id, snapshot_id, symbol, action, "
                "confidence, validation_ok, created_at, payload FROM ai_decisions "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            item = dict(zip(["decision_id", "cycle_id", "snapshot_id", "symbol",
                             "action", "confidence", "validation_ok",
                             "created_at", "payload"], r))
            try:
                item["payload"] = json.loads(item["payload"])
            except Exception:
                pass
            out.append(item)
        return out

    def risk_recent(self, limit: int = 50) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM risk_decisions ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except Exception:
                pass
        return out

    def executions_recent(self, limit: int = 50) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM executions ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except Exception:
                pass
        return out

    # ------------------------------------------------------- retention
    def prune_history(self, keep_decisions: int = 3000,
                      keep_risk: int | None = None,
                      keep_executions: int = 2000) -> dict:
        """Drop old decision/risk/execution rows — the DB growth driver.

        A live bot writes one ai_decision (+ one risk_decision) per analysed
        market per cycle; after weeks those two tables held 250k+ rows with
        full JSON payloads each (~465MB for one account). They are audit
        history, not financial records: orders/fills/positions/performance
        are NEVER touched, and the newest rows (what the UI shows) stay.

        Returns the number of deleted rows per table.
        """
        keep_decisions = max(0, int(keep_decisions))
        keep_risk = keep_decisions if keep_risk is None else max(0, int(keep_risk))
        removed = {}
        with self._lock:
            for table, keep in (("ai_decisions", keep_decisions),
                                ("risk_decisions", keep_risk),
                                ("executions", max(0, int(keep_executions)))):
                try:
                    total = self._conn.execute(
                        "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                except sqlite3.Error:
                    removed[table] = 0
                    continue
                excess = total - keep
                if excess <= 0:
                    removed[table] = 0
                    continue
                self._conn.execute(
                    "DELETE FROM %s WHERE rowid IN (SELECT rowid FROM %s "
                    "ORDER BY created_at ASC LIMIT ?)" % (table, table),
                    (excess,))
                removed[table] = excess
            self._conn.commit()
        return removed

    def db_size_mb(self) -> float:
        try:
            return self.db_path.stat().st_size / (1024 * 1024)
        except OSError:
            return 0.0

    def vacuum(self) -> None:
        """Reclaim the space of deleted rows (SQLite keeps it otherwise)."""
        with self._lock:
            try:
                self._conn.execute("VACUUM")
                self._conn.commit()
            except sqlite3.Error:
                pass

    # -------------------------------------------------------- performance
    def save_performance(self, perf: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO performance VALUES (?,?,?,?,?,?,?,?,?)",
                (perf["decision_id"], perf["symbol"], perf["action"],
                 perf.get("expected_return"),
                 perf.get("actual_return"),
                 str(perf.get("actual_pnl", "0")),
                 1 if perf.get("direction_correct") else 0,
                 1 if perf.get("profitable") else 0,
                 perf.get("created_at", utc_now())))
            self._conn.commit()

    def performance_summary(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), "
                "AVG(direction_correct), AVG(profitable), "
                "SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) "
                "FROM performance WHERE actual_return IS NOT NULL").fetchone()
            sum_win = self._conn.execute(
                "SELECT SUM(actual_pnl) FROM performance WHERE actual_pnl > 0"
            ).fetchone()[0] or 0
            sum_loss = self._conn.execute(
                "SELECT SUM(actual_pnl) FROM performance WHERE actual_pnl < 0"
            ).fetchone()[0] or 0
        n, dir_acc, win, n_win = row
        n = n or 0
        win = win or 0
        loss_sum = abs(float(sum_loss or 0))
        return {
            "measured_decisions": n,
            "direction_accuracy": round(float(dir_acc), 4) if dir_acc is not None and n else None,
            "win_rate": round(float(win), 4) if win is not None and n else None,
            "profit_factor": (round(float(sum_win) / loss_sum, 4)
                              if loss_sum > 0 else (None if not sum_win else 999.0)),
            "sum_profit": float(sum_win),
            "sum_loss": -loss_sum,
        }

    # ---------------------------------------------------------- positions
    def upsert_position(self, market: str, amount, avg_entry, realized_pnl,
                        side: str = "LONG", last_buy_at: str | None = None,
                        total_buys_irt=None, last_buy_price=None) -> None:
        """Insert/update a position row.

        last_buy_at: timestamp of the most recent BUY fill — the UI "holding
        timer" restarts from it on every new purchase. total_buys_irt:
        cumulative Toman invested in this position (grows with each top-up).
        last_buy_price: price of the most recent BUY (SS100d — UI «قیمت
        خرید» shows this; None preserves the existing value).
        When last_buy_at is None the existing value is preserved.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT opened_at, last_buy_at, total_buys_irt, "
                "last_buy_price FROM positions "
                "WHERE market=?", (market,)).fetchone()
            opened_at = (row[0] if row and row[0] else utc_now())
            if last_buy_at is not None:
                lb = last_buy_at
            elif row:
                lb = row[1]
            else:
                lb = None
            tb = row[2] if row and row[2] else None
            if total_buys_irt is not None:
                tb = str(total_buys_irt)
            lbp = row[3] if row and row[3] else None
            if last_buy_price is not None:
                lbp = str(last_buy_price)
            self._conn.execute(
                "INSERT INTO positions (market, side, amount, avg_entry, "
                "realized_pnl, opened_at, last_buy_at, total_buys_irt, "
                "last_buy_price, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(market) DO UPDATE SET amount=excluded.amount, "
                "avg_entry=excluded.avg_entry, realized_pnl=excluded.realized_pnl, "
                "side=excluded.side, last_buy_at=excluded.last_buy_at, "
                "total_buys_irt=excluded.total_buys_irt, "
                "last_buy_price=excluded.last_buy_price, "
                "updated_at=excluded.updated_at",
                (market, side, str(amount), str(avg_entry), str(realized_pnl),
                 opened_at, lb, tb, lbp, utc_now()))
            self._conn.commit()

    def positions(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT market, side, amount, avg_entry, realized_pnl, "
                "opened_at, last_buy_at, total_buys_irt, last_buy_price, "
                "updated_at FROM positions").fetchall()
        cols = ["market", "side", "amount", "avg_entry", "realized_pnl",
                "opened_at", "last_buy_at", "total_buys_irt",
                "last_buy_price", "updated_at"]
        return [dict(zip(cols, r)) for r in rows]

    def position(self, market: str) -> dict | None:
        for p in self.positions():
            if p["market"] == market:
                return p
        return None

    # ------------------------------------------------------------ kv
    def kv_get(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv_state VALUES (?,?)",
                (key, json.dumps(value, default=str)))
            self._conn.commit()


def utc_now() -> str:
    from .utils import utc_now_iso
    return utc_now_iso()
