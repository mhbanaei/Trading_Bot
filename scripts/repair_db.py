"""Rebuild a corrupted data/trading_bot.db, salvaging every readable row.

Run:  python scripts/repair_db.py                 # → data/trading_bot_repaired.db
      python scripts/repair_db.py --in-place      # archive the broken file, then swap the rebuilt one in
      python scripts/repair_db.py --src a.db --dst b.db

Why this exists: SQLite answers a damaged page with «database disk image is
malformed» and then EVERY cycle fails — the dashboard still loads, the bot
silently stops trading (22 hours of failed cycles on 2026-10-12 came from
this). The damage is normally one window of pages, not the whole file, so
this script copies the schema, copies every table it can read, rescues the
rest rowid-window by rowid-window and reports what was lost. The finance
tables are copied WHOLE and the script exits non-zero if any of them lost
a row, because a partial orders/fills/positions copy is not acceptable.

STOP THE BOT FIRST (Ctrl+C in its window): repairing a file another
process is writing to is pointless. --in-place keeps the broken original in
data/archive/ with a timestamp.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = PROJECT / "data" / "trading_bot.db"

# copied as one statement: a missing row here changes money/state
MONEY_TABLES = ("orders", "fills", "positions", "executions", "kv_state",
                "performance", "candles")
# high-volume diagnostic logs: rescued window by window when damaged
LOG_TABLES = ("ai_decisions", "risk_decisions")


def _schema(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return con.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex%' "
        "AND name NOT LIKE 'sqlite_sequence' "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END").fetchall()


def salvage(con: sqlite3.Connection, table: str, step: int = 500) -> tuple[int, int]:
    """Copy `table` from the attached `src` schema, skipping unreadable
    rowid windows. Returns (rows_copied, rowids_lost)."""
    top = con.execute("SELECT coalesce(max(rowid), 0) FROM src.%s" % table).fetchone()[0]
    copied = lost = 0
    lo = 1
    while lo <= top:
        hi = lo + step - 1
        try:
            cur = con.execute(
                "INSERT OR REPLACE INTO %s SELECT * FROM src.%s "
                "WHERE rowid BETWEEN ? AND ?" % (table, table), (lo, hi))
            copied += cur.rowcount
            con.commit()
        except sqlite3.DatabaseError:
            lost += hi - lo + 1
            con.rollback()
        lo = hi + 1
    return copied, lost


def repair(src: Path, dst: Path, step: int = 500, verbose: bool = True) -> dict:
    """Rebuild `src` into a fresh, integrity-checked database at `dst`."""
    if dst.exists():
        dst.unlink()
    src_con = sqlite3.connect("file:%s?mode=ro" % Path(src).as_posix(),
                              uri=True, timeout=30)
    dst_con = sqlite3.connect(str(dst))
    for _type, _name, sql in _schema(src_con):
        dst_con.execute(sql)
    dst_con.commit()
    dst_con.execute("ATTACH DATABASE ? AS src", (str(src),))

    tables = [r[0] for r in dst_con.execute(
        "SELECT name FROM src.sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]

    report = {"copied": {}, "lost": {}, "seconds": 0.0}
    t0 = time.time()
    for table in tables:
        if table in LOG_TABLES:
            copied, lost = salvage(dst_con, table, step)
        else:
            cur = dst_con.execute(
                "INSERT OR REPLACE INTO %s SELECT * FROM src.%s" % (table, table))
            dst_con.commit()
            copied, lost = cur.rowcount, 0
        report["copied"][table] = copied
        report["lost"][table] = lost
        if verbose:
            print("  %-16s copied %8d%s" % (
                table, copied, "   LOST %d" % lost if lost else ""))

    dst_con.execute("DETACH DATABASE src")
    dst_con.commit()
    report["integrity"] = dst_con.execute("PRAGMA integrity_check").fetchone()[0]
    dst_con.execute("PRAGMA journal_mode=DELETE")
    dst_con.execute("VACUUM")
    dst_con.close()
    src_con.close()
    report["seconds"] = time.time() - t0
    report["size_mb"] = dst.stat().st_size / 1e6
    report["money_tables_complete"] = all(
        report["lost"].get(t, 0) == 0 for t in MONEY_TABLES if t in report["lost"])
    return report


def archive_path(folder: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return folder / ("trading_bot_%s_corrupt.db" % stamp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild a corrupted trading_bot.db")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC,
                    help="the damaged database (default: data/trading_bot.db)")
    ap.add_argument("--dst", type=Path, default=None,
                    help="where to write the rebuilt file "
                         "(default: <src directory>/trading_bot_repaired.db)")
    ap.add_argument("--in-place", action="store_true",
                    help="archive the damaged file into data/archive/ and put "
                         "the rebuilt one in its place (bot must be stopped)")
    ap.add_argument("--step", type=int, default=500,
                    help="rowid-window size for rescuing damaged log tables")
    args = ap.parse_args(argv)

    src = args.src
    if not src.exists():
        print("[x] no such database: %s" % src)
        return 2
    dst = args.dst or (src.with_name("trading_bot_repaired.db"))

    print("[i] rebuilding %s -> %s" % (src, dst))
    report = repair(src, dst, step=max(50, args.step))

    print("\nintegrity_check: %s | %.1f MB | %.0fs"
          % (report["integrity"], report["size_mb"], report["seconds"]))
    if report["integrity"] != "ok" or not report["money_tables_complete"]:
        print("[x] the rebuilt file is NOT good enough to use — keep the "
              "original and inspect data/archive/")
        return 1

    if args.in_place:
        backup = archive_path(src.parent / "archive")
        backup.parent.mkdir(parents=True, exist_ok=True)
        src.replace(backup)
        shutil.copy2(dst, src)
        dst.unlink()
        print("[ok] %s swapped in | damaged original kept at %s" % (src, backup))
    else:
        print("[ok] rebuilt database is at %s — swap it in while the bot is "
              "stopped (or re-run with --in-place)" % dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
