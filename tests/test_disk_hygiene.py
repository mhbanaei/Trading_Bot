"""Disk-footprint tests — the program must stay small (≤200MB).

The live account had grown to 1.7GB:

* `data/events/events.jsonl` — 843MB because the configured cap was 1GB
  (and the old pruner loaded the whole file into memory to trim it);
* `data/trading_bot.db` — 465MB, almost all of it `ai_decisions` +
  `risk_decisions` rows (250k+ full JSON payloads of audit history);
* `data/snapshots/` — a ~400KB market snapshot written every cycle even
  though the bot reads everything from the exchange API anyway.

These tests pin the new behaviour: no snapshot files by default, a hard
byte cap on the event log that is O(cap) to enforce, and row retention that
never touches orders/fills/positions.

Run:  python -m unittest tests.test_disk_hygiene -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.events import EventBus  # noqa: E402
from nobitex_bot.storage import Storage  # noqa: E402


class TestNoSnapshotFiles(TempCase):
    def test_cycle_keeps_market_data_in_memory_only(self):
        cfg = self.cfg
        self.assertFalse(cfg.engine.persist_snapshots)
        self.assertEqual(cfg.engine.max_snapshots, 0)
        e = TradingEngine(cfg)
        e.run_cycle()
        snap_dir = Path(cfg.engine.data_dir) / "snapshots"
        self.assertEqual(list(snap_dir.glob("snapshot_*.json")), [])
        self.assertIsNotNone(e.latest_snapshot)
        self.assertEqual(e.latest_snapshot["status"], "COMPLETE")

    def test_leftover_snapshot_files_are_deleted(self):
        """A restart must clean files written by the old build."""
        e = TradingEngine(self.cfg)
        snap_dir = Path(self.cfg.engine.data_dir) / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (snap_dir / ("snapshot_%06d.json" % i)).write_text("{}",
                                                               encoding="utf-8")
        removed = e.collector._prune_snapshots()
        self.assertEqual(removed, 3)
        self.assertEqual(list(snap_dir.glob("snapshot_*.json")), [])


class TestEventLogCap(TempCase):
    def test_prune_keeps_only_recent_complete_lines(self):
        bus = EventBus(Path(self.cfg.engine.data_dir))
        bus.set_retention(0.01)          # 10KB
        for i in range(400):
            bus.publish("SNAPSHOT_CREATED", "test", "c%d" % i,
                        {"i": i, "pad": "x" * 60})
        size = bus._jsonl.stat().st_size
        self.assertLessEqual(size, 10 * 1024)
        for line in bus._jsonl.read_text(encoding="utf-8").splitlines():
            json.loads(line)             # every kept line is complete JSON

    def test_set_retention_trims_an_existing_oversized_log(self):
        data_dir = Path(self.cfg.engine.data_dir)
        bus = EventBus(data_dir)
        bus.max_bytes = 0
        with open(bus._jsonl, "w", encoding="utf-8") as fh:
            for i in range(500):
                fh.write(json.dumps({"i": i, "pad": "y" * 80}) + "\n")
        bus.set_retention(0.02)          # 20KB cap -> immediate trim
        self.assertLessEqual(bus._jsonl.stat().st_size, 20 * 1024)


class TestDatabaseRetention(TempCase):
    def _storage(self) -> Storage:
        return Storage(Path(self.cfg.engine.data_dir))

    def test_old_decisions_are_pruned_but_trades_are_kept(self):
        st = self._storage()
        for i in range(40):
            st.save_ai_decision({
                "decision_id": "d%03d" % i, "symbol": "BTCUSDT",
                "action": "BUY", "confidence": 0.6,
                "created_at": "2026-10-%02dT00:00:00.000Z" % (i % 28 + 1),
                "payload": "z" * 200}, True)
            st.save_risk_decision({
                "risk_decision_id": "r%03d" % i, "decision_id": "d%03d" % i,
                "status": "APPROVED",
                "created_at": "2026-10-%02dT00:00:00.000Z" % (i % 28 + 1)})
        st.save_order({"order_id": "o1", "client_order_id": "c1",
                       "decision_id": "d1", "market": "BTCUSDT",
                       "side": "BUY", "price": "100", "amount": "1",
                       "status": "FILLED", "created_at": "2026-10-01T00:00:00Z"})
        st.save_fill({"trade_id": "t1", "order_id": "o1",
                      "market": "BTCUSDT", "side": "BUY", "price": "100",
                      "amount": "1", "gross_value": "100",
                      "fee_amount": "0.25", "fee_asset": "USDT",
                      "timestamp": "2026-10-01T00:00:00Z"})
        removed = st.prune_history(keep_decisions=10)
        self.assertEqual(removed["ai_decisions"], 30)
        self.assertEqual(removed["risk_decisions"], 30)
        self.assertEqual(len(st.decisions_recent(100)), 10)
        self.assertEqual(len(st.fills(limit=10)), 1)
        self.assertEqual(len(st.orders(limit=10)), 1)
        st.vacuum()
        self.assertGreater(st.db_size_mb(), 0.0)

    def test_hygiene_runs_on_a_cycle_and_keeps_the_db_small(self):
        cfg = self.cfg
        cfg.engine.decisions_keep = 2
        cfg.engine.db_maintenance_every = 1
        e = TradingEngine(cfg)
        for _ in range(3):
            e.run_cycle()
        # the sweep runs at the START of a cycle; one more check after the
        # last cycle leaves the DB at the configured cap (no rows added)
        e._maybe_housekeeping()
        self.assertLessEqual(len(e.storage.decisions_recent(500)), 2)


if __name__ == "__main__":
    unittest.main()
