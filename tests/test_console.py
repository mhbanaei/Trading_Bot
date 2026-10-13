"""Live console view tests — nobitex_bot/console.py (start.bat's window).

Run:  python -m unittest tests.test_console -v

The window start.bat opens IS the live view now: the user watches it to see
what the bot is doing and closes it to stop. So three things are checked
here, all without touching a real account:

  * the per-market noise stays out (MARKET_DISABLED is ~270 lines a cycle)
  * money events keep their numbers, and every line stays ONE line even
    when the payload carries a traceback
  * a broken engine (status() raising, subscribe() raising, closed stdout)
    can silence the view but must never raise into the engine's publish
    loop and take the bot down with it
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.console import LiveConsole, render_event
from nobitex_bot.events import EventBus


def ev(etype: str, payload: dict | None = None, ts: str = "2026-10-12T17:23:45.500Z"):
    return {"event_type": etype, "timestamp": ts, "source": "test",
            "cycle_id": "cycle_x", "payload": payload or {}}


# realistic sizes: the heartbeat has to fit ONE 120-column console line,
# so the stub uses the account the live bot actually reports (millions of
# Toman, a two-digit dollar value) rather than round demo numbers.
STUB_STATUS = {
    "cycles_run": 219,
    "snapshot": {"market_count": 28, "valid_market_count": 28},
    "portfolio": {
        "account": {"equity_usdt": 14.03, "equity_irt": 3654347.0,
                    "irt_total": 1.0, "available_usdt": 1.2},
        "risk": {"total_profit_pct": -12463.59, "exposure_ratio": 0.511,
                 "risk_status": "ELEVATED"},
        "positions": [{"market": "BTCUSDT"}, {"market": "ETHUSDT"},
                      {"market": "FILUSDT"}],
    },
    "holdings_pnl": {"growth_pct": 19.98, "pnl_irt": 120000},
    "open_orders": [],
}


class StubEngine:
    def __init__(self, status=None, status_raises=False, subscribe_raises=False):
        import tempfile
        self.events = EventBus(Path(tempfile.mkdtemp()))
        self._status = status if status is not None else STUB_STATUS
        self._status_raises = status_raises
        self._cycles_run = 219
        self.cfg = type("C", (), {"engine": type("E", (), {"cycle_seconds": 5})()})()
        if subscribe_raises:
            self.events = type("Bus", (), {
                "subscribe": staticmethod(self._boom)})()

    @staticmethod
    def _boom(fn):
        raise RuntimeError("bus closed")

    def status(self):
        if self._status_raises:
            raise RuntimeError("database is locked")
        return self._status


class TestEventLines(unittest.TestCase):
    def test_per_market_noise_is_not_printed(self):
        for etype in ("MARKET_DISABLED", "SNAPSHOT_CREATED",
                      "AI_ANALYSIS_STARTED"):
            self.assertIsNone(render_event(ev(etype, {"symbol": "ZRXRLS"})),
                              etype)

    def test_decision_line_keeps_symbol_action_and_confidence(self):
        line = render_event(ev("AI_DECISION_CREATED", {
            "symbol": "TRXUSDT", "action": "BUY", "confidence": 0.8089}))
        self.assertIn("TRXUSDT", line)
        self.assertIn("BUY", line)
        self.assertIn("0.81", line)

    def test_risk_rejection_line_names_the_reason(self):
        line = render_event(ev("RISK_REJECTED", {
            "decision_id": "dec_20261012T134129_d5064bbe",
            "reasons": ["INSUFFICIENT_NET_EXPECTED_RETURN"]}))
        self.assertIn("INSUFFICIENT_NET_EXPECTED_RETURN", line)
        self.assertIn("BLOCKED", line)
        self.assertIn("d5064bbe", line)   # which decision was refused

    def test_order_line_keeps_market_side_size_and_price(self):
        line = render_event(ev("ORDER_SUBMITTED", {
            "order_id": "ord_1", "market": "FILUSDT", "side": "BUY",
            "price": 1.222, "amount": 0.36726678}))
        for token in ("FILUSDT", "BUY", "1.222", "0.3673"):
            self.assertIn(token, line)

    def test_snapshot_line_reports_the_market_count(self):
        line = render_event(ev("SNAPSHOT_COMPLETED", {
            "snapshot_id": "snap_20261012_134123_001407", "market_count": 28,
            "valid_market_count": 28}))
        self.assertIn("28 markets", line)

    def test_status_payloads_filed_as_system_error_are_decoded(self):
        """The engine publishes WALLET_SYNCED / the cross-market gate under
        names EVENT_TYPES does not know, so the bus files them as
        SYSTEM_ERROR with a data payload and no code. They must not print
        «? ERROR:»."""
        line = render_event(ev("SYSTEM_ERROR", {
            "assets": ["ARB", "USDT", "IRT"], "positions_seeded": 2,
            "positions_stale_removed": 0}))
        self.assertIn("wallet synced: 3 assets", line)
        self.assertIn("2 positions seeded", line)
        self.assertIn("info", line)
        self.assertNotIn("ERROR", line)

        line = render_event(ev("SYSTEM_ERROR", {
            "coins": ["ARB", "FIL"],
            "reason": "CROSS_MARKET_TRANSFER_NOT_ALLOWED"}))
        self.assertIn("CROSS_MARKET_TRANSFER_NOT_ALLOWED", line)
        self.assertIn("ARB", line)

    def test_error_line_collapses_a_traceback_to_one_line(self):
        line = render_event(ev("SYSTEM_ERROR", {
            "component": "ENGINE", "code": "CYCLE_FAILED",
            "message": "database disk image is malformed\n  File \"x.py\"\n"
                       "    SELECT decision_id FROM ai_decisions"}))
        self.assertNotIn("\n", line)
        self.assertIn("CYCLE_FAILED", line)
        self.assertIn("malformed", line)
        self.assertLessEqual(len(line), 160)

    def test_every_line_starts_with_a_clock_and_fits_one_line(self):
        for etype, payload in (
                ("SNAPSHOT_COMPLETED", {"market_count": 5}),
                ("ORDER_FILLED", {"market": "BTCUSDT", "price": 1, "amount": 2}),
                ("DUST_CONVERTED", {"symbol": "SHIBIRT", "amount": 12000}),
                ("AUTO_STOP_SET", {"market": "BTCUSDT", "stop_loss_pct": -3}),
                ("MADE_UP_TYPE", {"anything": "x" * 500})):
            line = render_event(ev(etype, payload))
            self.assertRegex(line, r"^\d\d:\d\d:\d\d ")
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), 160)


class TestHeartbeat(unittest.TestCase):
    def test_heartbeat_shows_the_account_the_dashboard_shows(self):
        con = LiveConsole(StubEngine(), out=io.StringIO())
        line = con.heartbeat()
        self.assertIn("cycle 219", line)
        # «ارزش کل حساب»: Toman headline + its dollar equivalent
        self.assertIn("equity 3,654,347 toman", line)
        self.assertIn("~$14.03", line)
        self.assertIn("exp 51.1% ELEVATED", line)
        self.assertIn("pos 3", line)
        self.assertIn("mkt 28", line)
        self.assertLessEqual(len(line), 118, "must fit a 120-column console")

    def test_heartbeat_hides_an_empty_order_book(self):
        line = LiveConsole(StubEngine(), out=io.StringIO()).heartbeat()
        self.assertNotIn("orders", line)
        self.assertLessEqual(len(line), 118)

    def test_a_busy_window_drops_the_dollar_not_the_toman_number(self):
        """One 120-column line is the contract: when an open order makes it
        too long, the dollar equivalent goes and the Toman headline stays."""
        status = json.loads(json.dumps(STUB_STATUS))
        status["open_orders"] = [{"order_id": "ord_1"}]
        line = LiveConsole(StubEngine(status=status), out=io.StringIO()).heartbeat()
        self.assertIn("orders 1", line)
        self.assertIn("equity 3,654,347 toman", line)
        self.assertNotIn("~$", line)
        self.assertLessEqual(len(line), 118)

    def test_a_huge_equity_drops_the_dollar_not_the_toman_number(self):
        status = json.loads(json.dumps(STUB_STATUS))
        status["portfolio"]["account"]["equity_irt"] = 999999999999.0
        status["open_orders"] = [{"order_id": "ord_1"}]
        line = LiveConsole(StubEngine(status=status), out=io.StringIO()).heartbeat()
        self.assertIn("equity 999,999,999,999 toman", line)
        self.assertIn("orders 1", line)
        self.assertNotIn("~$", line)
        self.assertLessEqual(len(line), 118)

    def test_heartbeat_uses_the_growth_kpi_not_the_broken_ratio(self):
        """risk.total_profit_pct has printed -12463% on a real account
        (it divides by a stale starting_equity); the dashboard headline is
        holdings_pnl.growth_pct, so the console must show that one."""
        line = LiveConsole(StubEngine(), out=io.StringIO()).heartbeat()
        self.assertIn("growth +19.98%", line)
        self.assertNotIn("12463", line)

    def test_heartbeat_survives_a_failing_status(self):
        con = LiveConsole(StubEngine(status_raises=True), out=io.StringIO())
        line = con.heartbeat()
        self.assertIn("status unavailable", line)
        self.assertIn("cycle 219", line)


class TestConsoleWiring(unittest.TestCase):
    def test_attach_prints_a_hint_and_live_events(self):
        out = io.StringIO()
        engine = StubEngine()
        con = LiveConsole(engine, out=out).attach()
        engine.events.publish("ORDER_FILLED", "execution_engine",
                              payload={"market": "BTCUSDT", "price": 99500,
                                       "amount": 0.001})
        engine.events.publish("MARKET_DISABLED", "data_collector",
                              payload={"symbol": "ZRXRLS"})
        text = out.getvalue()
        self.assertIn("live view on", text)
        self.assertIn("BTCUSDT", text)
        self.assertNotIn("ZRXRLS", text)
        con.close()
        self.assertIn("stopped", out.getvalue())

    def test_close_stops_the_heartbeat_thread(self):
        con = LiveConsole(StubEngine(), out=io.StringIO()).attach()
        thread = con._thread
        con.close()
        self.assertFalse(thread.is_alive())

    def test_a_broken_bus_or_closed_stdout_never_raises(self):
        con = LiveConsole(StubEngine(subscribe_raises=True), out=io.StringIO())
        con.attach()                      # subscribe raised -> still attached
        con.close()

        class Boom:
            def write(self, text):
                raise ValueError("console closed")

            def flush(self):
                raise ValueError("console closed")

        con = LiveConsole(StubEngine(), out=Boom())
        con.attach()
        con._on_event(ev("ORDER_FILLED", {"market": "BTCUSDT"}))
        con.close()                       # must not raise


class TestWindowCloseHandler(unittest.TestCase):
    def test_installing_the_console_handler_is_safe_anywhere(self):
        import run

        class Engine:
            def stop(self):
                self.stopped = True

        engine = Engine()
        result = run.install_console_stop(engine)
        self.assertIsInstance(result, bool)
        # the handler must only touch the engine on real close/logoff events
        self.assertFalse(getattr(engine, "stopped", False))


if __name__ == "__main__":
    unittest.main()
