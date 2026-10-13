"""Database repair tests — scripts/repair_db.py.

Run:  python -m unittest tests.test_repair_db -v

On 2026-10-12 the live bot spent 22 hours answering every cycle with
«database disk image is malformed» (one damaged page window in
ai_decisions) while the dashboard kept loading, so the failure was invisible
from the browser. The repair tool exists for that day: rebuild the file,
rescue every readable row, and never hand back a partial finance table.

The damage here is applied the way SQLite reports it in the wild: the page
type byte of one data page is zeroed, which makes reads through that page
raise DatabaseError while the rest of the file is untouched.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import repair_db  # noqa: E402

PAGE = 4096
LOGGED_ROWS = 4000
ORDER_ROWS = 25


# page 10 of this fixture is inside the log table's own data (its payloads
# spill over several pages); an index/internal page would still be readable
# row-by-row, which is not the failure being reproduced here
DAMAGED_PAGE = 10


def build_database(path: Path, break_page: int | None = None) -> None:
    """A small stand-in for trading_bot.db: one finance table, one log table."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ai_decisions ("
                "decision_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    con.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY,"
                " market TEXT NOT NULL)")
    con.executemany("INSERT INTO orders VALUES (?,?)",
                    [("ord_%03d" % i, "BTCUSDT") for i in range(ORDER_ROWS)])
    con.executemany("INSERT INTO ai_decisions VALUES (?,?)",
                    [("dec_%05d" % i, "x" * 200) for i in range(LOGGED_ROWS)])
    con.commit()
    con.close()
    if break_page is not None:
        with open(path, "r+b") as fh:
            fh.seek((break_page - 1) * PAGE)
            fh.write(b"\x00")            # invalid b-tree page type


def counts(path: Path) -> dict:
    con = sqlite3.connect(str(path))
    out = {t: con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
           for t in ("orders", "ai_decisions")}
    con.close()
    return out


class RepairCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="repair_test_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_healthy_database_is_copied_row_for_row(self):
        src = self.tmp / "trading_bot.db"
        dst = self.tmp / "repaired.db"
        build_database(src)
        report = repair_db.repair(src, dst, verbose=False)
        self.assertEqual(report["integrity"], "ok")
        self.assertEqual(report["lost"]["ai_decisions"], 0)
        self.assertTrue(report["money_tables_complete"])
        self.assertEqual(counts(dst), counts(src))

    def test_a_damaged_page_loses_only_its_own_rows(self):
        src = self.tmp / "trading_bot.db"
        dst = self.tmp / "repaired.db"
        build_database(src, break_page=DAMAGED_PAGE)

        # the damage is real: the log table cannot be read at all
        with self.assertRaises(sqlite3.DatabaseError):
            con = sqlite3.connect(str(src))
            try:
                con.execute("SELECT * FROM ai_decisions").fetchall()
            finally:
                con.close()

        report = repair_db.repair(src, dst, verbose=False)
        self.assertEqual(report["integrity"], "ok")
        # the finance table survived complete, the log table kept most rows
        self.assertTrue(report["money_tables_complete"])
        self.assertEqual(report["copied"]["orders"], ORDER_ROWS)
        self.assertGreater(report["lost"]["ai_decisions"], 0)
        self.assertLess(report["lost"]["ai_decisions"], LOGGED_ROWS // 3)
        self.assertEqual(counts(dst)["orders"], ORDER_ROWS)
        self.assertGreater(counts(dst)["ai_decisions"], LOGGED_ROWS // 2)

    def test_repairing_something_that_is_not_a_database_does_not_claim_success(self):
        src = self.tmp / "trading_bot.db"
        src.write_bytes(b"this is not sqlite" * 100)
        dst = self.tmp / "repaired.db"
        with self.assertRaises(sqlite3.DatabaseError):
            repair_db.repair(src, dst, verbose=False)

    def test_cli_reports_a_usable_rebuild(self):
        src = self.tmp / "trading_bot.db"
        dst = self.tmp / "repaired.db"
        build_database(src)
        rc = repair_db.main(["--src", str(src), "--dst", str(dst)])
        self.assertEqual(rc, 0)
        self.assertTrue(dst.exists())

    def test_cli_refuses_a_missing_source(self):
        self.assertEqual(repair_db.main(["--src", str(self.tmp / "nope.db")]), 2)


if __name__ == "__main__":
    unittest.main()
