"""«بازار معاملات» gate — the quote selector also stops the OFF side.

User report: with the setting on «فقط تتری» the bot still bought and sold
IRT (Toman) markets. Two leaks were reproduced against the real report:

* the DEMO universe keeps its own fixture list (BTCIRT/SHIBIRT/USDTIRT)
  and only the REAL registry was ever filtered by the selector, so every
  demo cycle kept trading Toman fixtures;
* the automatic paths (exit plans, profit top-up, ladder rungs, automatic
  dust conversion, risk-mode sweep) acted on old Toman positions without
  consulting the selector at all.

These tests pin the gate: while a section is switched OFF no candidate,
decision, execution or conversion may touch that side, and switching it
back ON restores the old behaviour (a gate that blocks everything would
be just as wrong as one that blocks nothing).

Run:  python -m unittest tests.test_quote_gate -v
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.config import PaperCfg  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402

TS = "2026-10-13T10:00:00.000Z"


class QuoteCase(TempCase):
    """Shared helpers: a demo engine with both paper wallets funded."""

    def _engine(self, quote: str = "usdt") -> TradingEngine:
        self.cfg.exchange.mode = "demo"
        self.cfg.exchange.trade_quote_markets = quote
        self.cfg.exchange.live_trading = False
        self.cfg.paper = PaperCfg(starting_usdt=10000.0,
                                  starting_irt=500000000.0)
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()          # one snapshot so prices / quality exist
        return e

    def _prices(self, e) -> dict:
        snap = e.latest_snapshot or {}
        return {m["symbol"]: m["last_price"] for m in snap.get("markets", [])
                if m.get("last_price")}

    def _price(self, e, market: str) -> Decimal:
        return Decimal(str(self._prices(e)[market]))

    def _plant_position(self, e, market: str, px: Decimal,
                        amount: str = "0.001") -> None:
        e.storage.upsert_position(market, Decimal(amount), px, Decimal("0"))

    def _plant_plan(self, e, market: str, plan: dict) -> None:
        plans = e.storage.kv_get("exit_plans", {}) or {}
        plans[market] = plan
        e.storage.kv_set("exit_plans", plans)

    def _save_fill(self, e, market: str, trade_id: str = "live_qg1") -> None:
        e.storage.save_fill({
            "trade_id": trade_id, "order_id": "qg_ord1", "decision_id": "",
            "market": market, "side": "BUY", "price": "100", "amount": "1",
            "gross_value": "100", "fee_amount": "0", "fee_asset": "IRT",
            "timestamp": TS})


class TestDemoUniverseHonoursSelector(QuoteCase):
    """Only the real registry used to apply the selector — the demo fixture
    list kept buying and selling its Toman pairs every cycle."""

    def _run(self, e, cycles: int = 6):
        candidates, decisions, executions = set(), set(), []
        for _ in range(cycles):
            e.run_cycle()
            for c in (e.latest_candidates or []):
                candidates.add(c.get("symbol"))
            for d in (e.latest_decisions or []):
                decisions.add((d.get("symbol"), d.get("action")))
            for x in (e.latest_executions or []):
                executions.append((x.get("market"), x.get("side"),
                                   str(x.get("status"))))
        return candidates, decisions, executions

    def test_usdt_only_never_touches_toman_in_demo(self):
        e = self._engine("usdt")
        candidates, decisions, executions = self._run(e)
        self.assertTrue([s for s in candidates if s and s.endswith("USDT")],
                        "the Tether side must keep trading: %r" % (candidates,))
        self.assertEqual([s for s in candidates
                          if s and s.endswith("IRT")], [])
        self.assertEqual([d for d in decisions
                          if d[0] and d[0].endswith("IRT")], [])
        self.assertEqual([x for x in executions
                          if x[0] and x[0].endswith("IRT")], [])

    def test_irt_only_never_touches_tether_in_demo(self):
        e = self._engine("irt")
        candidates, decisions, executions = self._run(e)
        self.assertTrue([s for s in candidates if s and s.endswith("IRT")],
                        "the Toman side must keep trading: %r" % (candidates,))
        self.assertEqual([s for s in candidates
                          if s and s.endswith("USDT")], [])
        self.assertEqual([d for d in decisions
                          if d[0] and d[0].endswith("USDT")], [])


class TestAutoExitsHonourSelector(QuoteCase):
    """An old exit plan on the switched-off side must not fire."""

    def _stop_hit(self, e, market: str):
        px = self._price(e, market)
        self._plant_position(e, market, px)
        self._plant_plan(e, market, {"stop": float(px * Decimal("0.985")),
                                     "stop_pct": -1.5, "auto": True,
                                     "stage": 0})
        prices = self._prices(e)
        prices[market] = float(px * Decimal("0.97"))     # below the stop
        return e._manage_exits(prices, "cyc_gate")

    def test_stop_on_the_off_section_is_not_sold(self):
        e = self._engine("usdt")
        out = self._stop_hit(e, "BTCIRT")                # Toman side, off
        self.assertEqual(out, [])
        # the plan is untouched, so it resumes when the section returns
        self.assertIn("BTCIRT", e.storage.kv_get("exit_plans", {}))

    def test_stop_on_the_enabled_section_still_sells(self):
        e = self._engine("irt")
        out = self._stop_hit(e, "BTCIRT")
        self.assertEqual(len(out), 1)
        self.assertIn("EXIT_STOP", out[0]["reason_codes"])
        self.assertTrue(out[0].get("exit"))

    def test_tether_stop_still_sells_with_usdt_only(self):
        e = self._engine("usdt")
        out = self._stop_hit(e, "BTCUSDT")
        self.assertEqual(len(out), 1)
        self.assertIn("EXIT_STOP", out[0]["reason_codes"])


class TestAutoBuysHonourSelector(QuoteCase):
    """«buy more» paths (profit top-up, ladder rung) followed positions
    directly, so they could still spend money on the off side."""

    def test_ladder_rung_does_not_buy_the_off_section(self):
        e = self._engine("usdt")
        px = self._price(e, "SHIBIRT")
        self._plant_position(e, "SHIBIRT", px * Decimal("0.9"),
                             amount="1000")
        self._plant_plan(e, "SHIBIRT", {
            "base_price": float(px), "ladder_rungs": [float(px)],
            "ladder_add_stage": 1, "ladder_done_stage": 0})
        prices = self._prices(e)
        ps = e.portfolio.portfolio_snapshot(prices, e._usdt_rates(prices))
        self.assertEqual(e._ladder_topup_decisions(prices, ps, "cyc_gate"), [])
        saved = [d for d in e.storage.decisions_recent(200)
                 if "LADDER_TOPUP" in ((d.get("payload") or {}).get(
                     "reason_codes") or [])]
        self.assertEqual(saved, [])

    def test_ladder_rung_still_buys_when_the_section_is_on(self):
        e = self._engine("irt")
        px = self._price(e, "SHIBIRT")
        self._plant_position(e, "SHIBIRT", px * Decimal("0.9"),
                             amount="1000")
        self._plant_plan(e, "SHIBIRT", {
            "base_price": float(px), "ladder_rungs": [float(px)],
            "ladder_add_stage": 1, "ladder_done_stage": 0})
        prices = self._prices(e)
        ps = e.portfolio.portfolio_snapshot(prices, e._usdt_rates(prices))
        e._ladder_topup_decisions(prices, ps, "cyc_gate")
        # the decision is what the gate controls; whether the risk engine
        # then approves the tiny demo budget is its own business
        saved = [d for d in e.storage.decisions_recent(200)
                 if "LADDER_TOPUP" in ((d.get("payload") or {}).get(
                     "reason_codes") or [])]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["symbol"], "SHIBIRT")

    def test_profit_topup_does_not_buy_the_off_section(self):
        e = self._engine("usdt")
        px = self._price(e, "SHIBIRT")
        entry = px * Decimal("0.5")            # +100% since the last buy
        e.storage.upsert_position("SHIBIRT", Decimal("1000"), entry,
                                  Decimal("0"), last_buy_price=entry)
        prices = self._prices(e)
        ps = e.portfolio.portfolio_snapshot(prices, e._usdt_rates(prices))
        self.assertEqual(e._profit_topup_decisions(prices, ps, "cyc_gate"), [])
        saved = [d for d in e.storage.decisions_recent(200)
                 if "PROFIT_TOPUP" in ((d.get("payload") or {}).get(
                     "reason_codes") or [])]
        self.assertEqual(saved, [])


class TestDustConvertHonoursSelector(QuoteCase):
    """Automatic dust conversion paid out in Toman — i.e. it was the bot
    selling into the switched-off section."""

    def _convertible_engine(self, quote: str, calls: list,
                            dust: str = "1000000"):
        e = self._engine(quote)
        e._real_attached = True
        e.exchange = SimpleNamespace(
            portfolio=lambda: {"assets": [
                {"asset": "SHIB", "available": dust}]},
            convert_wallet_balance=lambda coin: (
                calls.append(coin) or {"converted_amount": dust}))
        self._save_fill(e, "SHIBIRT")          # really bought in Toman
        return e

    def test_auto_convert_skips_the_off_section(self):
        calls: list = []
        e = self._convertible_engine("usdt", calls)
        out = e._convert_dust_holdings()
        self.assertEqual(calls, [])
        self.assertEqual(out["converted"], 0)
        blocked = [b for b in out["blocked"] if b.get("coin") == "SHIB"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("خاموش", blocked[0]["reason"])

    def test_manual_convert_button_still_converts(self):
        # force=True is the user's own click on the confirmed list
        calls: list = []
        e = self._convertible_engine("usdt", calls)
        out = e._convert_dust_holdings(force=True)
        self.assertEqual(calls, ["SHIB"])
        self.assertEqual(out["converted"], 1)

    def test_auto_convert_runs_when_the_section_is_on(self):
        calls: list = []
        e = self._convertible_engine("irt", calls)
        out = e._convert_dust_holdings()
        self.assertEqual(calls, ["SHIB"])
        self.assertEqual(out["converted"], 1)


if __name__ == "__main__":
    unittest.main()
