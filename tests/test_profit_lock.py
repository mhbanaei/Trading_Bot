"""Live-scenario tests for the ALWAYS-ON hadd ratchet (0→1→2→3…).

Model (no on/off switch — the ratchet always runs):
- The plan's `base_price` (قیمت حد) IS the LAST buy price at stage 0.
- Whenever the price reaches the armed take-profit, the stage counter
  goes up, the hadd MOVES to that price and the next take-profit
  re-arms step% above the new hadd.
- نردبان (risk.ladder_enabled): with every strike the STOP-LOSS moves
  up to the PREVIOUS rung and never falls — rung 1 lands on the buy
  price (break-even), rung 2 on rung 1, rung 3 on rung 2 … so the stop
  percent the row shows against the HADD (قیمت حد) updates rung by
  rung. Stops left behind by older plans are lifted on the next cycle.
- درصد ردیف: هم حد سود و هم حد ضرر نسبت به «قیمت حد» (the strike base)
  خوانده می‌شوند؛ «مبلغ» (قفل سود/ریسک) نسبت به قیمت خرید می‌ماند.
- A NEW buy resets the hadd to the fresh buy price (stage 0); an
  automatic LADDER buy does NOT (it is the same campaign).

Uses the same harness as tests/test_contract.py: a demo engine with a
planted position; the ratchet is driven by synthetic prices so the
stages advance deterministically.
Run:  python -m unittest tests.test_profit_lock -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402


class TestProfitLock(TempCase):
    """Ratchet: reaching the take-profit moves the hadd to that price."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _plant(self, e, market, price):
        """A held position + an armed plan at stage 0 (hadd = buy).

        No `trail` flag: the ratchet is always on.
        """
        from datetime import datetime, timezone
        e.storage.upsert_position(market, Decimal("10"), price,
                                  Decimal("0"),
                                  last_buy_at=datetime.now(
                                      timezone.utc).isoformat(),
                                  last_buy_price=price)
        plan = {"stop": float(price * Decimal("0.98")),
                "stop_pct": -2.0, "auto": True,
                "stage": 0,
                "take_profit_manual": float(price * Decimal("1.20")),
                "take_profit_manual_pct": 20.0}
        plans = e.storage.kv_get("exit_plans", {})
        plans[market] = plan
        e.storage.kv_set("exit_plans", plans)
        return plan

    def _first_market(self, e):
        return next(m["symbol"] for m in e.latest_snapshot["markets"])

    def _price_of(self, e, mkt):
        return Decimal(str(next(m for m in e.latest_snapshot["markets"]
                                if m["symbol"] == mkt)["last_price"]))

    def _step_frac(self, e):
        return (Decimal(str(getattr(e.cfg.risk,
                                    "profit_lock_step_pct", 20.0)))
                / Decimal("100"))

    def test_no_position_rejected_by_set_stop(self):
        e = self._engine()
        r = e.set_stop_loss("NOPE_USDT", 0.0, side="sell", pct=-2.0,
                            kind="stop")
        self.assertFalse(r["ok"])
        self.assertIn("موقعیت", r["error"])

    def test_stage_advances_and_hadd_moves(self):
        e = self._engine()
        e.run_cycle()
        mkt = self._first_market(e)
        price = self._price_of(e, mkt)
        self._plant(e, mkt, price)
        tp0 = Decimal(str(e.storage.kv_get("exit_plans", {})[mkt]
                          ["take_profit_manual"]))
        stop_frac = abs(Decimal(str(getattr(
            e.cfg.risk, "default_stop_loss_pct", -2.0) or -2.0))) / 100
        step = self._step_frac(e)

        # price below the target: nothing moves
        e._advance_profit_locks({mkt: float(price * Decimal("1.1"))})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["stage"]), 0)

        # price REACHES the target: stage 0->1, the HADD moves to the
        # reached price and the next take-profit re-arms +step% above
        # the new hadd. نردبان: the STOP also moves — to the previous
        # rung, which at stage 1 IS the buy price (break-even), so it can
        # only ever go UP (the planted stop was buy*0.98).
        stop_before = Decimal(str(e.storage.kv_get(
            "exit_plans", {})[mkt]["stop"]))
        e._advance_profit_locks({mkt: float(tp0)})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["stage"]), 1)
        self.assertEqual(Decimal(str(plan["base_price"])), tp0)
        self.assertEqual(Decimal(str(plan["locked_stop"])), tp0)
        # the stop rode the ladder up to rung 0 = the buy price
        self.assertEqual(Decimal(str(plan["stop"])),
                         price.quantize(Decimal("0.000001")))
        self.assertGreater(Decimal(str(plan["stop"])), stop_before)
        self.assertEqual(int(plan["ladder_stop_stage"]), 1)
        # and the next rung's capital budget is queued for the same cycle
        self.assertEqual(int(plan["ladder_add_stage"]), 1)
        self.assertEqual(
            Decimal(str(plan["take_profit_manual"])),
            (tp0 * (Decimal("1") + step)).quantize(Decimal("0.000001")))
        # tp percent reads against the NEW hadd
        self.assertEqual(float(plan["take_profit_manual_pct"]),
                         float(step * 100))

        # second stage: reaching the new target moves the hadd again
        # (1->2) and the stop up to rung 1 (the level just struck)
        tp1 = Decimal(str(plan["take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp1 * Decimal("1.05"))})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["stage"]), 2)
        self.assertEqual(Decimal(str(plan["base_price"])), tp1)
        # the stop now sits on rung 1 = the level just struck (tp0),
        # snapped to exchange price precision like every rung
        self.assertEqual(Decimal(str(plan["stop"])),
                         tp0.quantize(Decimal("0.000001")))
        self.assertGreater(Decimal(str(plan["stop"])), price)

    def test_row_percents_read_against_hadd(self):
        """The holdings row shows «قیمت حد (n)»; BOTH the TP percent and
        the STOP percent read against the hadd (قیمت حد) — the user's
        «حد ضرر از قیمت حد محاسبه شود». The buy-anchored percent stays
        available as stop_pct_buy (it drives «قفل سود/ریسک»)."""
        e = self._engine()
        e.run_cycle()
        mkt = self._first_market(e)
        price = self._price_of(e, mkt)
        self._plant(e, mkt, price)
        tp0 = Decimal(str(e.storage.kv_get("exit_plans", {})[mkt]
                          ["take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp0)})
        pos = e.storage.position(mkt)
        row = e._position_row(pos, {mkt: float(price)}, 1_000_000.0)
        # hadd moved to the reached take-profit, stage 1
        self.assertEqual(Decimal(str(row["hadd"])), tp0)
        self.assertEqual(int(row["stage"]), 1)
        # TP % vs the hadd (=tp0): +step%
        self.assertAlmostEqual(row["hadd_tp_pct"],
                               float(step := self._step_frac(e) * 100),
                               places=2)
        # STOP % vs the HADD: the ladder lifted the stop to rung 0 (the
        # buy price) while the hadd sits on the reached target, so the row
        # reads «how far below قیمت حد the stop sits» = (buy/hadd - 1)
        self.assertAlmostEqual(row["hadd_stop_pct"],
                               float((price / tp0 - 1) * 100), places=1)
        # the old buy-anchored percent is still reported (break-even here)
        self.assertAlmostEqual(row["stop_pct_buy"], 0.0, places=2)
        # and the money shown next to it: nothing lost, nothing locked yet
        self.assertAlmostEqual(row["stop_money"], 0.0, places=1)

    def test_row_hadd_column_is_the_live_price(self):
        """نمای کلی: the «قیمت حد» the row PRINTS is the coin's current
        price (user request) and BOTH trigger percents read against that
        same number — while the engine's strike base (`hadd`) keeps its
        own value, so the ladder/ratchet is untouched."""
        e = self._engine()
        e.run_cycle()
        mkt = self._first_market(e)
        price = self._price_of(e, mkt)
        self._plant(e, mkt, price)
        tp0 = Decimal(str(e.storage.kv_get("exit_plans", {})[mkt]
                          ["take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp0)})
        plan_now = e.storage.kv_get("exit_plans", {})[mkt]
        tp_now = Decimal(str(plan_now["take_profit_manual"]))
        stop = Decimal(str(plan_now["stop"]))
        pos = e.storage.position(mkt)
        live = price * Decimal("1.10")
        row = e._position_row(pos, {mkt: float(live)}, 1_000_000.0)
        # the column shows EXACTLY the live price …
        # (the payload is JSON — compare as floats)
        self.assertAlmostEqual(row["hadd_display"], float(live),
                               delta=float(live) * 1e-9)
        # … and the strike behind the ladder is still the reached target
        self.assertEqual(Decimal(str(row["hadd"])), tp0)
        self.assertEqual(int(row["stage"]), 1)
        # both percentages read against the live price
        self.assertAlmostEqual(row["live_tp_pct"],
                               float((tp_now / live - 1) * 100), places=2)
        self.assertAlmostEqual(row["live_stop_pct"],
                               float((stop / live - 1) * 100), places=2)
        # the strike-anchored percents are still reported for the API
        self.assertIsNotNone(row["hadd_stop_pct"])
        self.assertIsNotNone(row["hadd_tp_pct"])

    def test_first_buy_records_hadd(self):
        """_auto_stop_for_buy stores the hadd = buy price, stage 0."""

        e = self._engine()
        e.run_cycle()
        mkt = self._first_market(e)
        px = self._price_of(e, mkt)
        plan = e._auto_stop_for_buy(mkt, px)
        self.assertEqual(Decimal(str(plan["base_price"])), px)
        self.assertEqual(int(plan.get("stage", 0)), 0)

    def test_new_buy_resets_hadd_and_stage(self):
        """EVERY buy re-bases the hadd to the fresh price (stage 0) —
        the strike counter counts strikes since THIS buy."""
        e = self._engine()
        e.run_cycle()
        mkt = self._first_market(e)
        px = self._price_of(e, mkt)
        self._plant(e, mkt, px)
        tp0 = Decimal(str(e.storage.kv_get("exit_plans", {})[mkt]
                          ["take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp0)})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["stage"]), 1)
        # a fresh buy at a new price resets the counter
        new_px = px * Decimal("2")
        e._auto_stop_for_buy(mkt, new_px)
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["stage"]), 0)
        self.assertLess(abs(Decimal(str(plan["base_price"])) - new_px),
                        Decimal("0.000001"))

    def test_stage0_hadd_is_last_buy_price(self):
        """Stage 0: the hadd IS the last buy price — even when an older
        base_price lingers in the plan."""
        e = self._engine()
        e.run_cycle()
        mkt = self._first_market(e)
        price = self._price_of(e, mkt)
        from datetime import datetime, timezone
        e.storage.upsert_position(mkt, Decimal("10"), price,
                                  Decimal("0"),
                                  last_buy_at=datetime.now(
                                      timezone.utc).isoformat(),
                                  last_buy_price=price)
        plan = {"stop": float(price), "stage": 0,
                "base_price": float(price * Decimal("9")),  # stale garbage
                "take_profit_manual": float(price * Decimal("1.2"))}
        plans = e.storage.kv_get("exit_plans", {})
        plans[mkt] = plan
        e.storage.kv_set("exit_plans", plans)
        pos = e.storage.position(mkt)
        row = e._position_row(pos, {mkt: float(price)}, 1_000_000.0)
        self.assertEqual(Decimal(str(row["hadd"])), price)
        base = e.set_stop_loss(mkt, 0.0, side="sell", pct=-5.0,
                               kind="stop")
        self.assertTrue(base["ok"])
        # -5% of the hadd (= the last buy price), not of the stale base
        self.assertEqual(Decimal(str(base["stop"])),
                         (price * Decimal("0.95")).quantize(
                             Decimal("0.000001")))



class TestLadder(TempCase):
    """نردبان پله‌ها: حد ضرر روی پلهٔ قبلی، و سرمایهٔ افزودهٔ هر پله."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _market(self, e):
        snap = e.latest_snapshot or {}
        m = next((m for m in snap.get("markets", [])
                  if (m.get("data_quality") or {}).get("score", 0)
                  >= e.cfg.quality.min_trade_quality), None)
        if m is None:
            m = next(m for m in snap["markets"])
        return m["symbol"], Decimal(str(m["last_price"]))

    def _plant(self, e, market, price, extra=None):
        from datetime import datetime, timezone
        e.storage.upsert_position(
            market, Decimal("10"), price, Decimal("0"),
            last_buy_at=datetime.now(timezone.utc).isoformat(),
            last_buy_price=price)
        plan = {"stop": float(price * Decimal("0.97")), "stop_pct": -3.0,
                "auto": True, "stage": 0,
                "take_profit_manual": float(price * Decimal("1.15")),
                "take_profit_manual_pct": 15.0}
        if extra:
            plan.update(extra)
        plans = e.storage.kv_get("exit_plans", {})
        plans[market] = plan
        e.storage.kv_set("exit_plans", plans)
        return plan

    def test_legacy_stale_stop_is_lifted_to_the_previous_rung(self):
        """پلهٔ ۴ (حد ‎+۷۵٪) با حد ضرری که هنوز ۳٪ زیر قیمت خرید مانده:
        همان چرخه روی پلهٔ ۳ بالا کشیده می‌شود — دقیقاً جایی که کاربر
        می‌گفت «حد ضرر پایین‌تر از پله به‌روز می‌شود و سود بی‌محافظ است»."""
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        hadd = (price * (Decimal("1.15") ** 4)).quantize(Decimal("0.000001"))
        plan = self._plant(e, mkt, price, {
            "stage": 4, "base_price": float(hadd),
            "locked_stop": float(hadd),
            # هدف خیلی دور تا همین تست strike جدیدی نخورد
            "take_profit_manual": float(hadd * Decimal("10")),
            "take_profit_manual_pct": 15.0})
        old_stop = Decimal(str(plan["stop"]))
        e._advance_profit_locks({})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        rung3 = (price * (Decimal("1.15") ** 3)).quantize(Decimal("0.000001"))
        self.assertEqual(Decimal(str(plan["stop"])), rung3)
        self.assertGreater(Decimal(str(plan["stop"])), old_stop)
        self.assertEqual(int(plan["ladder_stop_stage"]), 4)
        self.assertEqual(int(plan["stage"]), 4)

    def test_stop_never_falls_back(self):
        """حد ضرری که دستی بالاتر از پله گذاشته شده پایین نمی‌آید."""
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        hadd = (price * Decimal("1.15")).quantize(Decimal("0.000001"))
        high = (price * Decimal("2")).quantize(Decimal("0.000001"))
        self._plant(e, mkt, price, {
            "stage": 1, "base_price": float(hadd),
            "stop": float(high), "locked_stop": float(hadd),
            "take_profit_manual": float(hadd * Decimal("10"))})
        e._advance_profit_locks({})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(Decimal(str(plan["stop"])), high)

    def test_row_shows_locked_profit_percent_and_money(self):
        """درصد و مبلغ حد ضرر با پله عوض می‌شوند: پله ۲ → حد = پله ۱."""
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        plan = self._plant(e, mkt, price)
        tp0 = Decimal(str(plan["take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp0)})
        e._advance_profit_locks({mkt: float(Decimal(str(
            e.storage.kv_get("exit_plans", {})[mkt][
                "take_profit_manual"])))})
        row = e._position_row(e.storage.position(mkt),
                              {mkt: float(price)}, 1_000_000.0)
        self.assertEqual(int(row["stage"]), 2)
        # حد = پلهٔ قبلی (۱۵٪ بالای خرید) و حد ضرر روی پلهٔ ۱ → درصد
        # نمایش‌داده‌شده نسبت به «قیمت حد» = ۱۵٪ زیر پلهٔ فعلی (منفی)
        self.assertAlmostEqual(row["hadd_stop_pct"],
                               -100 * 15.0 / 115.0, places=1)
        # درصد نسبت به قیمت خرید همان ‎+۱۵٪ قفل‌شده است (پایهٔ مبلغ)
        self.assertAlmostEqual(row["stop_pct_buy"], 15.0, places=1)
        want_money = float((Decimal(str(row["stop"])) - price) *
                           Decimal("10"))
        self.assertAlmostEqual(row["stop_money"], want_money, places=1)        # سرمایهٔ پلهٔ بعد = سودِ همان پله: (حد سودِ مسلح − میانگین خرید) ×
        # تعداد سکه — نه درصدی از ارزش موقعیت
        tp_armed = Decimal(str(row["take_profit"]))
        self.assertAlmostEqual(
            row["ladder_next_add"],
            float((tp_armed - price) * Decimal("10")), places=2)

        self.assertEqual(int(row["ladder_stop_stage"]), 2)

    def test_stop_percent_reads_from_hadd_base(self):
        """حد ضرر از «قیمت حد» محاسبه میشود (base=hadd):
        در پله ۲ با ‎-3٪، حد ضرر = قیمت حد × ۰٫۹۷ (نه ۳٪ زیر قیمت خرید)
        — همان چیزی که ویرایشگر ردیف «حد» می‌فرستد، و «حد ضرر همه» هم
        با همین مبنا کار می‌کند."""
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        plan = self._plant(e, mkt, price)
        tp0 = Decimal(str(plan["take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp0)})
        tp1 = Decimal(str(e.storage.kv_get("exit_plans", {})[mkt][
            "take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp1)})
        hadd = (price * Decimal("1.15") ** 2).quantize(Decimal("0.000001"))
        self.assertEqual(
            Decimal(str(e.storage.kv_get("exit_plans", {})[mkt]
                       ["base_price"])), hadd)
        # قیمت زنده روی خود پله باشد تا گارد «سمت اشتباه قیمت» فعال نشود
        for m in e.latest_snapshot["markets"]:
            if m["symbol"] == mkt:
                m["last_price"] = float(hadd)
        r = e.set_stop_loss(mkt, 0.0, side="sell", pct=-3.0,
                            kind="stop", base_mode="hadd")
        self.assertTrue(r["ok"])
        self.assertEqual(r["base"], "hadd")
        self.assertEqual(Decimal(str(r["stop"])),
                         (hadd * Decimal("0.97")).quantize(
                             Decimal("0.000001")))
        # ردیف همان درصد را نشان میدهد (نسبت به قیمت حد، نه قیمت خرید)
        row = e._position_row(e.storage.position(mkt),
                              {mkt: float(hadd)}, 1_000_000.0)
        self.assertAlmostEqual(row["hadd_stop_pct"], -3.0, places=1)
        self.assertGreater(row["stop_pct_buy"], 0)  # سود قفل‌شده
        # «حد ضرر همه» با مبنای قیمت حد
        allr = e.set_all_stops(-5.0, kind="stop", base="hadd")
        self.assertTrue(allr["ok"])
        self.assertEqual(Decimal(str(
            e.storage.kv_get("exit_plans", {})[mkt]["stop"])),
            (hadd * Decimal("0.95")).quantize(Decimal("0.000001")))

    def test_ladder_buy_does_not_reset_the_ladder(self):
        """خرید خودکار پله ادامهٔ همان کمپین است: پله/حد/حد ضرر عوض نمی‌شود."""
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        plan = self._plant(e, mkt, price)
        tp0 = Decimal(str(plan["take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp0)})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        stop_on_rung = Decimal(str(plan["stop"]))
        self.assertEqual(int(plan["stage"]), 1)
        plan["ladder_buy_pending"] = True
        plans = e.storage.kv_get("exit_plans", {})
        plans[mkt] = plan
        e.storage.kv_set("exit_plans", plans)
        # خرید پله‌ای با قیمت بالاتر پر می‌شود
        out = e._auto_stop_for_buy(mkt, price * Decimal("1.2"))
        self.assertEqual(int(out.get("stage", 0)), 1)
        self.assertEqual(Decimal(str(out["base_price"])), tp0)
        self.assertEqual(Decimal(str(out["stop"])), stop_on_rung)
        self.assertFalse(out.get("ladder_buy_pending"))
        # حد ضرر از قیمت خرید جدید (۳٪ زیر آن) ساخته نشده — همان پله است
        self.assertNotEqual(
            Decimal(str(out["stop"])),
            (price * Decimal("1.2") * Decimal("0.97")).quantize(
                Decimal("0.000001")))

    def test_manual_rung_edit_lifts_the_stop(self):
        """پلهٔ دستی (کلیک روی زوج (n)) هم حد ضرر را بالا می‌برد."""
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        self._plant(e, mkt, price)
        out = e.set_hadd(mkt, 2, float(price * Decimal("1.3")))
        self.assertTrue(out["ok"])
        # لنگر = قیمت خرید، گام = ۱۵٪ → حد = پلهٔ ۱ = خرید × ۱.
        self.assertEqual(Decimal(str(out["stop"])),
                         (price * Decimal("1.15")).quantize(
                             Decimal("0.000001")))

    def test_ladder_topup_buys_one_rung_budget_once(self):
        """هر strike در همان چرخه یک خرید به اندازهٔ سودِ همان پله
        ((قیمت پله − میانگین خرید) × تعداد سکه)، و همان پله دوباره خرید
        نمی‌شود."""
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        self._plant(e, mkt, price)
        snap = e.latest_snapshot or {}
        prices = {m["symbol"]: m["last_price"] for m in snap["markets"]
                  if m.get("last_price")}
        ps = e.portfolio.portfolio_snapshot(prices, e._usdt_rates(prices))
        tp0 = Decimal(str(e.storage.kv_get("exit_plans", {})[mkt][
            "take_profit_manual"]))
        e._advance_profit_locks({mkt: float(tp0)})
        buys = e._ladder_topup_decisions(prices, ps, "cyc_ladder")
        decs = [d for d in e.storage.decisions_recent(400)
                if "LADDER_TOPUP" in ((d.get("payload") or {}).get(
                    "reason_codes") or [])]
        self.assertEqual(len(decs), 1, "exactly one ladder buy for the rung")
        dec = decs[0]["payload"]
        want = ((tp0 - price) * Decimal("10")).quantize(Decimal("0.01"))
        self.assertAlmostEqual(dec["position_size"]["quote_value"],
                               float(want), places=4)
        self.assertIn("LADDER_STAGE_1", dec["reason_codes"])
        # the budget is spent with the rung (buy fired or not)
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["ladder_done_stage"]), 1)
        self.assertEqual(int(plan["ladder_add_stage"]), 0)
        # same rung never fires twice
        self.assertEqual(
            e._ladder_topup_decisions(prices, ps, "cyc_ladder_2"), [])

    def test_ladder_budget_reaches_the_paper_pipeline(self):
        """بودجهٔ پله در همان چرخه واقعاً اجرا می‌شود: موقعیت به‌اندازهٔ
        سودِ همان پله بزرگ می‌شود (خط لولهٔ کامل validation → risk →
        execution)."""
        e = self._engine()
        e.run_cycle()
        snap = e.latest_snapshot or {}
        pool = [x for x in snap.get("markets", [])
                if x["symbol"].endswith("USDT") and x.get("last_price")
                and (x.get("data_quality") or {}).get("score", 0)
                >= e.cfg.quality.min_trade_quality]
        if not pool:
            self.skipTest("no quality USDT market in the demo snapshot")
        # the cheapest market keeps the rung budget above the exchange
        # minimum order, so the approved size is exactly the budget
        m = min(pool, key=lambda x: float(x["last_price"]))
        mkt = m["symbol"]
        price = Decimal(str(m["last_price"]))
        rung1 = (price * Decimal("1.15")).quantize(Decimal("0.000001"))
        self._plant(e, mkt, price, {
            "stage": 1, "base_price": float(rung1),
            "locked_stop": float(rung1),
            "take_profit_manual": float(rung1 * Decimal("1.15")),
            "ladder_anchor": float(price),
            "ladder_rungs": [float(price), float(rung1)],
            "ladder_add_stage": 1, "ladder_done_stage": 0})
        # میانگین خرید پایین‌تر تا سودِ پله از حداقل سفارش صرافی بالاتر بماند
        # (زیر حداقل، خودِ صرافی اندازه را بالا می‌برد و تست را قطعی نمی‌کند)
        anchor = (price / Decimal("3")).quantize(Decimal("0.000001"))
        e.storage.upsert_position(mkt, Decimal("10"), anchor, Decimal("0"))
        budget = (rung1 - anchor) * Decimal("10")
        before = Decimal(str(e.storage.position(mkt)["amount"]))
        e.run_cycle()
        after = Decimal(str(e.storage.position(mkt)["amount"]))
        self.assertGreater(after, before)
        fills = [f for f in e.storage.fills(limit=10)
                 if f.get("market") == mkt]
        self.assertTrue(fills, "the rung budget never reached execution")
        spent = Decimal(str(fills[0].get("gross_value") or 0))
        self.assertAlmostEqual(float(spent), float(budget),
                               delta=float(budget) * 0.03)
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["ladder_done_stage"]), 1)
        self.assertEqual(int(plan["ladder_add_stage"]), 0)

    def test_ladder_rung_without_profit_takes_no_capital(self):
        """پلهٔ زیر میانگین خرید سرمایه نمی‌گیرد: بودجهٔ هر پله همان سودِ
        آن پله است، پس پلهٔ بی‌سود می‌سوزد، پول نقد آزاد دست‌نخورده
        می‌ماند و دلیلش در رویداد LADDER_TOPUP_BLOCKED ثبت می‌شود."""
        import json
        from datetime import datetime, timezone
        e = self._engine()
        e.run_cycle()
        mkt, price = self._market(e)
        self._plant(e, mkt, price, {
            "stage": 1, "base_price": float(price),
            "locked_stop": float(price),
            "take_profit_manual": float(price * Decimal("1.15")),
            "ladder_anchor": float(price),
            "ladder_rungs": [float(price), float(price)],
            "ladder_add_stage": 1, "ladder_done_stage": 0})
        # میانگین خرید دو برابر قیمتِ همان پله: سودِ پله منفی است
        e.storage.upsert_position(
            mkt, Decimal("10"), price * Decimal("2"), Decimal("0"),
            last_buy_at=datetime.now(timezone.utc).isoformat(),
            last_buy_price=price * Decimal("2"))
        snap = e.latest_snapshot or {}
        prices = {m["symbol"]: m["last_price"] for m in snap["markets"]
                  if m.get("last_price")}
        ps = e.portfolio.portfolio_snapshot(prices, e._usdt_rates(prices))
        before = Decimal(str(e.storage.position(mkt)["amount"]))
        self.assertEqual(
            e._ladder_topup_decisions(prices, ps, "cyc_norungprofit"), [])
        self.assertEqual(Decimal(str(e.storage.position(mkt)["amount"])),
                         before)
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(int(plan["ladder_done_stage"]), 1)
        self.assertEqual(int(plan["ladder_add_stage"]), 0)
        self.assertTrue(any("NO_PROFIT_AT_RUNG" in json.dumps(ev)
                            for ev in e.recent_events(50)))

    def test_ladder_disabled_leaves_the_stop_alone(self):
        """ladder_enabled=False → رفتار قبلی: حد ضرر با strike تکون نمی‌خورد."""
        e = self._engine()
        e.cfg.risk.ladder_enabled = False
        e.cfg.risk.ladder_topup_enabled = False
        e.run_cycle()
        mkt, price = self._market(e)
        plan = self._plant(e, mkt, price, {
            "stage": 4,
            "base_price": float(price * Decimal("1.75")),
            "take_profit_manual": float(price * Decimal("20"))})
        old_stop = Decimal(str(plan["stop"]))
        e._advance_profit_locks({})
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertEqual(Decimal(str(plan["stop"])), old_stop)
        self.assertNotIn("ladder_stop_stage", plan)
        self.assertEqual(int(plan.get("ladder_add_stage", 0)), 0)


class TestNetDeposited(TempCase):
    """کل سرمایه واریزی from REAL Nobitex turnover (deposits − withdraws)."""

    def test_exchange_turnover_beats_manual(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False

        class FakeEx:
            def deposits_withdraws(self):
                return {"deposits": [{"amount": "100000000", "currency": "rls",
                               "status": "Succeed", "fee": "0"},
                              {"amount": "1", "currency": "usdt",
                               "status": "Succeed"}],
                        "withdraws": [{"amount": "20000000", "currency": "rls",
                                       "status": "Done"}]}

            def ticker(self, sym):
                if sym == "USDTIRT":
                    return {"last_price": 100000.0}
                raise AssertionError("unexpected ticker " + sym)

        e.exchange = FakeEx()
        e.set_deposited_capital(555555.0)          # manual override present
        out = e._holdings_pnl({})
        self.assertEqual(out["deposited_source"], "exchange")
        # 100M rls /10 = 10,000,000 toman + 1 usdt*100k = 100,000
        # withdraw 20M rls /10 = 2,000,000  → net = 8,100,000
        self.assertEqual(out["deposited_irt"], 8100000.0)

    def test_falls_back_to_manual_on_error(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False

        class BoomEx:
            def deposits_withdraws(self):
                raise RuntimeError("network down")

        e.exchange = BoomEx()
        e.set_deposited_capital(123000.0)
        out = e._holdings_pnl({})
        self.assertEqual(out["deposited_source"], "manual")
        self.assertEqual(out["deposited_irt"], 123000.0)
        self.assertIn("net_irt", out["deposited_detail"])

    def test_a_withdrawal_in_progress_already_left_the_wallet(self):
        """کاربر: «وقتی از کل سرمایه واریزی برداشت می‌کنم سود سرمایه منفی
        می‌خورد» — پول همان لحظهٔ ثبت درخواست از کیف پول کم می‌شود ولی
        وضعیت برداشت ساعت‌ها روی «Bank processing» می‌ماند. پس همین حالا
        باید از «کل سرمایه واریزی» کم شود؛ وگرنه افت ارزش حساب بدون افت
        سرمایه، یک ضرر جعلی به اندازهٔ کل برداشت می‌سازد. فقط برداشت
        شکست‌خورده (که پول را از حساب نبرد) بیرون می‌ماند."""
        e = TradingEngine(self.cfg)
        e.auto_trading = False

        class FakeEx:
            def deposits_withdraws(self):
                return {"deposits": [{"amount": "200000000",
                                       "currency": "rls",
                                       "status": "Succeed", "fee": "0"}],
                        "withdraws": [
                            {"amount": "24193810", "currency": "rls",
                             "status": "Bank processing"},
                            {"amount": "28907241", "currency": "rls",
                             "status": "Failed"}]}

            def ticker(self, sym):
                return {"last_price": 100000.0}

        e.exchange = FakeEx()
        out = e._holdings_pnl({})
        # 20,000,000 − 2,419,381 تومان (برداشت Failed شمرده نمی‌شود)
        self.assertEqual(out["deposited_irt"], 17580619.0)
        d = out["deposited_detail"]
        self.assertEqual(d["withdraws_count"], 1)
        self.assertEqual(d["pending_withdraws_irt"], 2419381.0)
        failed = next(a for a in d["audit"]
                      if a["status"] == "failed")
        self.assertFalse(failed["counted"])
        self.assertEqual(failed["irt"], 2890724.1)

    def test_a_pending_deposit_is_not_capital_yet(self):
        """واریزیِ نرسیده سرمایه نیست (پولش روی کیف پول نیست) پس نباید
        سود را جعلی بالا ببرد؛ واریزی کارت‌به‌کارت که status سطح بالا
        ندارد از statement.status خوانده می‌شود (executed = رسیده)."""
        e = TradingEngine(self.cfg)
        e.auto_trading = False

        class FakeEx:
            def deposits_withdraws(self):
                return {"deposits": [
                    # cobank card deposit: NO top-level status
                    {"amount": "10000000", "fee": "40000",
                     "statement": {"status": "executed"}},
                    {"amount": "50000000", "currency": "rls",
                     "status": "Pending", "fee": "0"},
                    {"amount": "10000000", "fee": "40000",
                     "statement": {"status": "rejected"}}],
                    "withdraws": []}

            def ticker(self, sym):
                return {"last_price": 100000.0}

        e.exchange = FakeEx()
        out = e._holdings_pnl({})
        # تنها واریزیِ رسیده: (10,000,000 − 40,000) /10 = 996,000 تومان
        self.assertEqual(out["deposited_irt"], 996000.0)
        self.assertEqual([a["counted"] for a in
                          out["deposited_detail"]["audit"]],
                         [True, False, False])

    def test_a_wallet_outflow_refreshes_the_turnover_now(self):
        """وقتی سینک کیف پول افت تومانی بدون هیچ فیل را می‌بیند (برداشت
        بانکی)، کش «کل سرمایه واریزی» دیگر معتبر نیست و همان چرخه
        بازخوانی می‌شود — وگرنه تا یک ساعت سود سرمایه به اندازهٔ کل
        برداشت اشتباه می‌ماند."""
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        calls = []

        class CountEx:
            def deposits_withdraws(self):
                calls.append(1)
                return {"deposits": [{"amount": "100000000",
                                       "currency": "rls",
                                       "status": "Succeed", "fee": "0"}],
                        "withdraws": []}

            def ticker(self, sym):
                return {"last_price": 100000.0}

        e.exchange = CountEx()
        e.portfolio.overwrite_balances({
            "USDT": {"available": Decimal("100"), "locked": Decimal("0")},
            "IRT": {"available": Decimal("10000000"),
                    "locked": Decimal("0")}})
        e.portfolio.flow_dirty = False      # the FIRST sync is not a flow
        e._holdings_pnl({})
        e._holdings_pnl({})                 # within TTL → cached
        self.assertEqual(len(calls), 1)
        # کاربر برداشت می‌زند: کیف پول 2.5M تومان کمتر، بدون هیچ فیل در دفتر
        e.portfolio.overwrite_balances({
            "USDT": {"available": Decimal("100"), "locked": Decimal("0")},
            "IRT": {"available": Decimal("7500000"),
                    "locked": Decimal("0")}})
        self.assertTrue(e.portfolio.flow_dirty)
        e._holdings_pnl({})                 # stale cache must not be used
        self.assertEqual(len(calls), 2)
        self.assertFalse(e.portfolio.flow_dirty)

    def test_an_old_cache_without_the_paging_version_is_refreshed(self):
        """کش قدیمی (قبل از صفحه‌بندی کامل) نباید با همان عدد ناقص سرو شود."""
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        calls = []

        class CountEx:
            def deposits_withdraws(self):
                calls.append(1)
                return {"deposits": [{"amount": "60302240",
                                       "currency": "rls",
                                       "status": "Succeed", "fee": "0"}],
                        "withdraws": [], "pages": 2}

            def ticker(self, sym):
                return {"last_price": 100000.0}

        e.exchange = CountEx()
        import time as _time
        e.storage.kv_set("net_deposited_cache", {
            "at": _time.time(), "net_irt": 1536224.0,   # page-1 only
            "deposits_irt": 22740470.0, "withdraws_irt": 21204246.0})
        out = e._holdings_pnl({})
        self.assertEqual(len(calls), 1)                 # re-read NOW
        self.assertEqual(out["deposited_irt"], 6030224.0)
        self.assertEqual(out["deposited_detail"]["pages"], 2)
        e._holdings_pnl({})                             # v2 cache → TTL
        self.assertEqual(len(calls), 1)

    def test_a_deposit_never_shows_up_as_profit(self):
        """کاربر: «واریز کردم ولی جزو سود حساب شد». ارزش حساب و سرمایهٔ
        واریزی باید با هم به همان اندازه بالا بروند → سود ثابت بماند."""
        e = TradingEngine(self.cfg)
        e.auto_trading = False

        class CountEx:
            deposits = [{"amount": "100000000", "currency": "rls",
                         "status": "Succeed", "fee": "0"}]

            def deposits_withdraws(self):
                return {"deposits": list(self.deposits),
                        "withdraws": []}

            def ticker(self, sym):
                return {"last_price": 100000.0}

        ex = CountEx()
        e.exchange = ex
        e.portfolio.overwrite_balances({
            "IRT": {"available": Decimal("5000000"), "locked": Decimal("0")}})
        e.portfolio.flow_dirty = False       # the first sync is not a flow
        ps = e.portfolio.portfolio_snapshot({}, e._usdt_rates({}))
        before = e._holdings_pnl({}, ps)
        # deposit lands: wallet +10M AND the turnover shows the same +10M
        ex.deposits = [{"amount": "200000000", "currency": "rls",
                        "status": "Succeed", "fee": "0"}]
        e.portfolio.overwrite_balances({
            "IRT": {"available": Decimal("15000000"),
                    "locked": Decimal("0")}})
        e.portfolio.flow_dirty = False
        e.storage.kv_set("net_deposited_cache", {})
        ps = e.portfolio.portfolio_snapshot({}, e._usdt_rates({}))
        after = e._holdings_pnl({}, ps)
        self.assertEqual(before["deposited_irt"], 10000000.0)
        self.assertEqual(after["deposited_irt"], 20000000.0)
        self.assertAlmostEqual(after["profit_capital_irt"],
                               before["profit_capital_irt"], places=2)
        # درصد عمداً عوض می‌شود (پایهٔ سرمایه بزرگ‌تر شده) — عدد سود نه
        self.assertAlmostEqual(after["total_irt"], 15000000.0, places=2)

    def test_recent_history_capital_is_repaired_from_the_full_list(self):
        """بعد از صفحه‌بندی، `d` نقاط اخیر تاریخچه از لیست کامل بازسازی
        می‌شود؛ وگرنه باکس ۲۴ساعته با مبنای ناقص، ضرر جعلی می‌سازد."""
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        now = datetime.now(timezone.utc)
        t_new = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_old = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dep_at = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        e.storage.kv_set("equity_history", [
            {"t": t_old, "e": 1000000.0, "d": 111.0, "a": 1, "u": "irt"},
            {"t": t_new, "e": 6000000.0, "d": 111.0, "a": 1,
             "u": "irt"}])

        class CountEx:
            def deposits_withdraws(self):
                return {"deposits": [{"amount": "100000000",
                                       "currency": "rls",
                                       "status": "Succeed", "fee": "0",
                                       "createdAt": dep_at + ".000000+00:00"}],
                        "withdraws": []}

            def ticker(self, sym):
                return {"last_price": 100000.0}

        e.exchange = CountEx()
        e.storage.kv_set("net_deposited_cache", {})
        out = e._net_deposited_from_exchange()
        self.assertEqual(out["net_irt"], 10000000.0)
        hist = {p["t"]: p for p in e.storage.kv_get("equity_history")}
        # before the deposit: net − deposit; after it: the full net
        self.assertEqual(hist[t_old]["d"], 0.0)
        self.assertEqual(hist[t_new]["d"], 10000000.0)

    def test_cache_ttl_and_refresh(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        calls = []

        class CountEx:
            def deposits_withdraws(self):
                calls.append(1)
                return {"deposits": [{"amount": "10000000", "currency": "rls",
                               "status": "Succeed", "fee": "0"}],
                        "withdraws": []}

            def ticker(self, sym):
                return {"last_price": 100000.0}

        e.exchange = CountEx()
        e._holdings_pnl({})
        e._holdings_pnl({})          # within TTL → cached, no new call
        self.assertEqual(len(calls), 1)
        out = e.refresh_net_deposited()   # forced → new call
        self.assertEqual(len(calls), 2)
        self.assertTrue(out["fresh"])
        self.assertEqual(out["net_irt"], 1000000.0)


if __name__ == "__main__":
    unittest.main()

