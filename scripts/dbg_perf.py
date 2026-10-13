"""Temporary diagnostic for test_performance_measured_after_close.

Runs 40 demo cycles in a temp data dir and prints the perf_pending
kv state plus the performance table after each position close event.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config, PaperCfg  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="nobot_dbg_"))
    try:
        cfg = Config()
        cfg.engine.data_dir = str(tmp / "data")
        cfg.exchange.demo_seed = 7

        from nobitex_bot.engine import TradingEngine
        eng = TradingEngine(cfg)

        for i in range(40):
            s = eng.run_cycle()
            rows = eng.storage.kv_get("perf_pending", {})
            fills = eng.storage.fills(limit=5)
            if i % 5 == 0 or s.get("executions"):
                print(f"c{i:02d} exec={s.get('executions')} "
                      f"fills={len(fills)} pending={len(rows)} "
                      f"measured={eng.storage.performance_summary()['measured_decisions']}")
            if rows:
                for m, info in rows.items():
                    pos = eng.storage.position(m)
                    amt = pos["amount"] if pos else "NO_POS"
                    print(f"      pending {m}: entry={info.get('entry_price')} "
                          f"pos_amount={amt} action={info.get('action')}")
        print("FINAL summary:", eng.storage.performance_summary())
        print("positions:", [(p["market"], p["amount"]) for p in eng.storage.positions()])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
