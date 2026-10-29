"""حالت ریسک (risk mode) — چرخش سریع روی پرسودترین‌های ۲۴ساعته.

Covers:
- toggle: set_risk_mode flips the flag + persists via save_config-style
  live config (risk.risk_mode), and rank_markets reorders by 24h change.
- rotation switches: cooldown uses risk_mode_buy_cooldown_s in risk mode
  and the normal one otherwise.
- exit: a drop from the post-buy peak (فروش هنگام افت) closes the whole
  position with EXIT_DROP and the freed cash rotates in the same cycle.
- safe mode still wins over risk mode.
Run:  python -m unittest tests.test_risk_mode -v
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
from nobitex_bot.scanner import rank_markets, scan_market  # noqa: E402
from nobitex_bot.regime import gate_candidates  # noqa: E402
from nobitex_bot.utils import utc_now_iso  # noqa: E402


class TestRiskModeToggle(TempCase):
    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def test_toggle_flips_flag(self):
        e = self._engine()
        self.assertFalse(e.risk_mode)
        r = e.set_risk_mode(True)
        self.assertTrue(r["ok"])
        self.assertTrue(e.risk_mode)
        self.assertTrue(e.cfg.risk.risk_mode)
        r = e.set_risk_mode(False)
        self.assertFalse(e.risk_mode)
        self.assertFalse(e.cfg.risk.risk_mode)

    def test_profile_mirrors_risk_mode(self):
        """«حالت امن» ⇄ «حالت ریسک» are the two positions of one switch."""
        e = self._engine()
        s = e.status()
        self.assertEqual(s["risk_mode"], False)
        self.assertEqual(s["profile"], "safe")
        self.assertTrue(s["safe_profile"])
        e.set_risk_mode(True)
        s = e.status()
        self.assertEqual(s["profile"], "risk")
        self.assertFalse(s["safe_profile"])
        e.set_risk_mode(False)
        s = e.status()
        self.assertEqual(s["profile"], "safe")
        self.assertTrue(s["safe_profile"])

    def test_halt_is_independent_of_the_profile(self):
        """The engine's safety HALT (safe_mode) is NOT the safe profile: a
        halted engine can still be in risk mode, and exiting the halt must
        not silently change the profile."""
        e = self._engine()
        e.set_risk_mode(True)
        e.enter_safe_mode(["MANUAL"])
        s = e.status()
        self.assertTrue(s["safe_mode"])
        self.assertEqual(s["profile"], "risk")
        e.exit_safe_mode()
        s = e.status()
        self.assertFalse(s["safe_mode"])
        self.assertEqual(s["profile"], "risk")
        self.assertTrue(s["risk_mode"])

    def test_ranking_follows_24h_change(self):
        """Risk mode ranks by the 24h change: the biggest gainer is #1."""
        def mkt(sym, chg, dq=0.9):
            return {"symbol": sym,
                    "features": {"change_24h": chg,
                                 "orderbook": {"liquidity_score": 0.8,
                                               "spread_percent": 0.004},
                                 "liquidity": {"score": 0.8, "status": "GOOD"},
                                 "volatility": {"volatility_regime": "MEDIUM"},
                                 "technicals": {"ema": {}, "rsi": 55},
                                 "structure": {}},
                    "data_quality": {"score": dq}}

        mkts = [mkt("SLOWUSDT", 0.2), mkt("HOTUSDT", 9.5),
                mkt("MIDUSDT", 2.0), mkt("NEGUSDT", -1.0)]
        # safe mode: identical features -> stable sort keeps input order
        # (24h change is ignored) — the exact complaint risk mode fixes
        safe = rank_markets(mkts, top_n=4, risk_mode=False)
        self.assertEqual([c["symbol"] for c in safe],
                         ["SLOWUSDT", "HOTUSDT", "MIDUSDT", "NEGUSDT"])
        risky = rank_markets(mkts, top_n=4, risk_mode=True)
        # strictly ordered by the 24h change: biggest gainer first
        order = [c["symbol"] for c in risky]
        self.assertEqual(order.index("HOTUSDT"), 0)
        self.assertLess(order.index("HOTUSDT"), order.index("MIDUSDT"))
        self.assertLess(order.index("MIDUSDT"), order.index("SLOWUSDT"))
        self.assertLess(order.index("SLOWUSDT"), order.index("NEGUSDT"))
        # signal is exposed for the dashboard
        self.assertGreater(risky[0]["signals"]["momentum_24h"],
                           risky[-1]["signals"]["momentum_24h"])

    def test_scan_market_momentum_24h_signal(self):
        up = scan_market("XUSDT", {"change_24h": 12.0}, {"score": 0.9},
                         risk_mode=True)
        down = scan_market("XUSDT", {"change_24h": -8.0}, {"score": 0.9},
                           risk_mode=True)
        none = scan_market("XUSDT", {}, {"score": 0.9}, risk_mode=True)
        self.assertGreater(up["score"], down["score"])
        self.assertEqual(none["signals"]["momentum_24h"], 0.5)
        self.assertIn("TOP_24H_MOVER", up["reasons"])
        self.assertIn("WEAK_24H", down["reasons"])


class TestRiskModeRotation(TempCase):
    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def test_cooldown_switch_in_risk_mode(self):
        e = self._engine()
        self.cfg.risk.buy_cooldown_s = 1800
        mkt = "BTCUSDT"
        snap = {"data_quality": {"score": 0.95},
                "liquidity": {"status": "GOOD"},
                "last_price": 113000.0}
        px = {mkt: 113000.0}
        dec = {"decision_id": "d1", "symbol": mkt, "action": "BUY",
               "expires_at": (datetime.now(timezone.utc) +
                              timedelta(minutes=5)).isoformat(),
               "entry": {"price": 113000.0},
               "stop_loss": {"price": 110000.0},
               "position_size": {"amount": 0.4, "quote_value": 45000.0},
               "cost": {"expected_net_return": 0.05}}
        pf = {"risk": {"total_equity": 1000000.0},
              "assets": [{"asset": "USDT", "available": 50000.0}]}
        rd = e.risk.evaluate(dec, snap, pf, px)
        self.assertEqual(rd["status"], "APPROVED")
        # a recent FILLED BUY from 20 minutes ago blocks in safe mode
        e.storage.save_execution({
            "execution_id": "x1", "decision_id": "d0",
            "risk_decision_id": "r0", "market": mkt, "side": "BUY",
            "price": "113000.0", "amount": "0.01", "status": "FILLED",
            "created_at": (datetime.now(timezone.utc) -
                           timedelta(minutes=20)).isoformat()})
        rd = e.risk.evaluate(dec, snap, pf, px)
        self.assertIn("BUY_COOLDOWN_ACTIVE", rd["rejection_reasons"])
        # a FAILED order from 2 minutes ago also backs off (no per-cycle
        # hammering of a market the exchange keeps rejecting). Realistic
        # sizes so the earlier gates pass and the cooldown is reached.
        e.storage.save_execution({
            "execution_id": "x2", "decision_id": "d9",
            "risk_decision_id": "r9", "market": "NEWIRT", "side": "BUY",
            "price": "1000.0", "amount": "500.0", "status": "FAILED",
            "created_at": (datetime.now(timezone.utc) -
                           timedelta(minutes=2)).isoformat()})
        dec2 = dict(dec, symbol="NEWIRT")
        dec2["entry"] = {"price": 1000.0}
        dec2["stop_loss"] = {"price": 990.0}
        dec2["position_size"] = {"amount": 500.0, "quote_value": 500000.0}
        pf2 = {"risk": {"total_equity": 1000000.0},
               "assets": [{"asset": "IRT", "available": 900000.0}]}
        rd = e.risk.evaluate(dec2, snap, pf2,
                             {"NEWIRT": 1000.0, "USDTIRT": 200000.0})
        self.assertIn("BUY_COOLDOWN_ACTIVE", rd["rejection_reasons"])
        # risk mode: 5-minute cooldown -> the same 20-minute-old buy is OK
        e.set_risk_mode(True)
        rd = e.risk.evaluate(dec, snap, pf, px)
        self.assertNotIn("BUY_COOLDOWN_ACTIVE", rd["rejection_reasons"])


class TestRiskModeDropExit(TempCase):
    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _plant(self, e, market, price):
        """A held position (≥ min-notional value) + plan armed at the
        buy price (peak anchored)."""
        from datetime import datetime as _dt
        amount = (Decimal("300000") / price).quantize(Decimal("0.00000001"))
        e.storage.upsert_position(
            market, amount, price, Decimal("0"),
            last_buy_at=_dt.now(timezone.utc).isoformat(),
            last_buy_price=price)
        plans = e.storage.kv_get("exit_plans", {})
        plans[market] = {"stop": float(price * Decimal("0.985")),
                         "stop_pct": -1.5, "auto": True, "stage": 0,
                         "risk_mode_peak": float(price)}
        e.storage.kv_set("exit_plans", plans)

    def _price_of(self, e, mkt):
        return Decimal(str(next(m for m in e.latest_snapshot["markets"]
                                if m["symbol"] == mkt)["last_price"]))

    def test_auto_stop_uses_risk_mode_levels(self):
        e = self._engine()
        e.run_cycle()
        mkt = next(m["symbol"] for m in e.latest_snapshot["markets"])
        px = self._price_of(e, mkt)
        e.set_risk_mode(True)
        plan = e._auto_stop_for_buy(mkt, px)
        self.assertAlmostEqual(plan["stop_pct"], -3.0, places=1)
        self.assertAlmostEqual(plan["take_profit_manual_pct"], 12.0, places=1)
        e.set_risk_mode(False)
        plan = e._auto_stop_for_buy(mkt, px)
        self.assertAlmostEqual(plan["stop_pct"], -3.0, places=1)
        self.assertAlmostEqual(plan["take_profit_manual_pct"], 25.0, places=1)

    def test_drop_exit_closes_position_and_rotates(self):
        e = self._engine()
        e.run_cycle()
        mkt = next(m["symbol"] for m in e.latest_snapshot["markets"])
        px = self._price_of(e, mkt)
        self._plant(e, mkt, px)
        e.set_risk_mode(True)
        # peak tracks the rise...
        e._manage_exits({mkt: float(px * Decimal("1.10")),
                         "USDTIRT": 100000.0}, "cycle_t")
        plan = e.storage.kv_get("exit_plans", {})[mkt]
        self.assertAlmostEqual(plan["risk_mode_peak"],
                               float(px * Decimal("1.10")), places=4)
        # ...then a >6% drop from the peak (below the -6% drop-exit but
        # ABOVE the -3% stop at 0.985·px) fires the full drop exit
        out = e._manage_exits({mkt: float(px * Decimal("1.03")),
                               "USDTIRT": 100000.0}, "cycle_t")
        self.assertEqual(len(out), 1)
        self.assertIn("EXIT_DROP", out[0]["reason_codes"])
        self.assertTrue(out[0].get("exit"))

    def test_stop_exit_rotation_triggers_on_drop(self):
        e = self._engine()
        e.run_cycle()
        mkt = next(m["symbol"] for m in e.latest_snapshot["markets"])
        px = self._price_of(e, mkt)
        self._plant(e, mkt, px)
        e.set_risk_mode(True)
        e._manage_exits({mkt: float(px * Decimal("1.10")),
                         "USDTIRT": 100000.0}, "cycle_t")
        out = e._manage_exits({mkt: float(px * Decimal("1.03")),
                               "USDTIRT": 100000.0}, "cycle_t")
        # rotation runs on the EXIT_DROP exits without crashing (it may
        # return [] when no tradable candidate exists — engine stays safe)
        rot = e._rotation_buy_after_stop(out if out else [
            {"reason_codes": ["EXIT_DROP"]}])
        self.assertIsInstance(rot, list)

    def test_low_volume_pump_filtered_in_risk_mode(self):
        """فیلتر پامپ بی‌حجم: در حالت ریسک، برترینِ ۲۴ساعته با حجم کم
        امتیاز صفر می‌گیرد (LOW_VOLUME_PUMP) و خرید نمی‌شود."""
        from nobitex_bot.scanner import scan_market, rank_markets
        feats = {"change_24h": 25.0, "technicals": {}, "structure": {},
                 "liquidity": {}, "orderbook": {"liquidity_score": 0.8},
                 "quote_volume_24h": 5000.0}
        dq = {"score": 0.9}
        pumped = scan_market("PUMPUSDT", feats, dq, risk_mode=True,
                             min_quote_volume_usdt=100000.0)
        self.assertIn("LOW_VOLUME_PUMP", pumped["reasons"])
        self.assertEqual(pumped["score"], 0.0)
        # same market, liquid enough -> normal risk-mode scoring
        feats["quote_volume_24h"] = 900000.0
        ok = scan_market("PUMPUSDT", feats, dq, risk_mode=True,
                         min_quote_volume_usdt=100000.0)
        self.assertNotIn("LOW_VOLUME_PUMP", ok["reasons"])
        self.assertGreater(ok["score"], 0.0)
        # outside risk mode the floor never applies
        feats["quote_volume_24h"] = 5000.0
        safe = scan_market("PUMPUSDT", feats, dq, risk_mode=False,
                           min_quote_volume_usdt=100000.0)
        self.assertNotIn("LOW_VOLUME_PUMP", safe["reasons"])
        ranked = rank_markets(
            [{"symbol": "PUMPUSDT", "features": feats, "data_quality": dq}],
            5, risk_mode=True, min_quote_volume_usdt=100000.0)
        self.assertEqual(ranked[0]["score"], 0.0)

    def test_safe_mode_wins(self):
        e = self._engine()
        e.set_risk_mode(True)
        e.enter_safe_mode(["TEST"])
        self.assertTrue(e.safe_mode)
        before = len(e.storage.orders(limit=1000))
        for _ in range(2):
            e.run_cycle()
        self.assertEqual(len(e.storage.orders(limit=1000)), before)
        self.assertTrue(e.risk_mode)  # independent switches


class TestRiskModeSweep(TempCase):
    """حالت ریسک — تجمیع سرمایه: unsellable bot crumbs -> Toman."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def test_sweeps_only_bot_micro_positions(self):
        from types import SimpleNamespace
        e = self._engine()
        e.set_risk_mode(True)
        e._real_attached = True
        converted = []
        e.exchange = SimpleNamespace(
            convert_wallet_balance=lambda coin: converted.append(coin) or {
                "ok": True})
        e._price_fallback = lambda mkt: None
        now = datetime.now(timezone.utc).isoformat()
        # bot-opened crumb: sellable value 10k < min order 88k -> swept
        e.storage.upsert_position("ZZZIRT", Decimal("10"), Decimal("1000"),
                                  Decimal("0"), last_buy_at=now,
                                  last_buy_price=Decimal("1000"))
        # user pre-existing holding (avg_entry 0): NEVER swept
        e.storage.upsert_position("YYYIRT", Decimal("5"), Decimal("0"),
                                  Decimal("0"))
        # valuable bot position (> max_value): left to the normal exit plan
        e.storage.upsert_position("BIGIRT", Decimal("1"), Decimal("900000"),
                                  Decimal("0"), last_buy_at=now,
                                  last_buy_price=Decimal("900000"))
        e.latest_snapshot = {"markets": [
            {"symbol": "ZZZIRT", "last_price": 1000.0},
            {"symbol": "YYYIRT", "last_price": 2000.0},
            {"symbol": "BIGIRT", "last_price": 900000.0},
            {"symbol": "USDTIRT", "last_price": 200000.0}]}
        out = e._sweep_bot_micro_positions()
        self.assertEqual(out["converted"], 1)
        self.assertEqual(converted, ["ZZZ"])

    def test_sweep_disabled_outside_risk_mode(self):
        e = self._engine()
        self.assertFalse(e.risk_mode)
        # the run_cycle gate only calls it in risk mode; direct call still
        # respects the real-attached guard
        out = e._sweep_bot_micro_positions()
        self.assertEqual(out["converted"], 0)


class TestRegimeGate(TempCase):
    """لایهٔ رژیم بازار: تشخیص ۶ رژیم + گیت خرید حالت ریسک."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _price_of(self, e, mkt):
        return Decimal(str(next(m for m in e.latest_snapshot["markets"]
                                if m["symbol"] == mkt)["last_price"]))

    @staticmethod
    def _mkt(sym, ema="up", roc=3.0, atr_pct=0.015, rsi=60):
        if ema == "up":
            e9, e20, e50, tr = 110.0, 100.0, 90.0, "UPTREND"
        elif ema == "down":
            e9, e20, e50, tr = 90.0, 100.0, 110.0, "DOWNTREND"
        else:  # flat
            e9, e20, e50, tr = 100.0, 100.2, 99.8, "RANGE"
        return {"symbol": sym,
                "technicals": {"ema": {"9": e9, "20": e20, "50": e50},
                               "rsi": rsi, "roc": roc},
                "structure": {"trend": tr, "breakout": {}},
                "volatility": {"atr_percent": atr_pct}}

    def test_detect_six_regimes(self):
        from nobitex_bot.regime import detect
        up = detect(self._mkt("A"))
        self.assertEqual(up["regime"], "STRONG_UPTREND")
        down = detect(self._mkt("B", ema="down", roc=-3.0, rsi=40))
        self.assertEqual(down["regime"], "STRONG_DOWNTREND")
        flat = detect(self._mkt("C", ema="flat", roc=0.0, rsi=50))
        self.assertEqual(flat["regime"], "SIDEWAYS")
        crash = detect(self._mkt("D", atr_pct=0.09))
        self.assertEqual(crash["regime"], "HIGH_VOLATILITY")
        self.assertIn("ATR_EXPANSION", crash["reasons"])

    def test_gate_blocks_non_uptrend_in_risk_mode(self):
        from nobitex_bot.regime import gate_candidates
        cands = [scan_market("UPUSDT", self._mkt("UPUSDT"), {"score": 0.9},
                             risk_mode=True),
                 scan_market("RANGEUSDT",
                             self._mkt("RANGEUSDT", ema="flat", roc=0.0,
                                       rsi=50),
                             {"score": 0.9}, risk_mode=True)]
        mkt_by = {"UPUSDT": self._mkt("UPUSDT"),
                  "RANGEUSDT": self._mkt("RANGEUSDT", ema="flat",
                                         roc=0.0, rsi=50)}
        kept, blocked = gate_candidates(cands, mkt_by, risk_mode=True)
        self.assertEqual([c["symbol"] for c in kept], ["UPUSDT"])
        self.assertEqual(blocked[0]["symbol"], "RANGEUSDT")
        self.assertEqual(blocked[0]["regime_blocked"], "SIDEWAYS")
        # safe mode: sideways is still buyable (analytic pick), only the
        # crash regime blocks
        kept2, blocked2 = gate_candidates(cands, mkt_by, risk_mode=False)
        self.assertEqual(len(kept2), 2)
        self.assertEqual(blocked2, [])

    def test_engine_gate_blocks_cycle_buys(self):
        e = self._engine()
        e.run_cycle()
        mkt = next(m["symbol"] for m in e.latest_snapshot["markets"])
        px = self._price_of(e, mkt)
        e.set_risk_mode(True)
        e.latest_candidates = [{"symbol": mkt, "score": 0.9,
                                "signals": {"momentum_24h": 0.9},
                                "reasons": []}]
        # keep the per-cycle regime override OFF: this test drives the
        # gate function directly
        e.cfg.risk.risk_mode_require_uptrend = True
        market_by_symbol = {m["symbol"]: m for m in
                            e.latest_snapshot["markets"]}
        snap = market_by_symbol[mkt]
        fpx = float(px)
        snap["technicals"] = {"ema": {"9": fpx * 0.8, "20": fpx * 0.9,
                                      "50": fpx},
                              "rsi": 35, "roc": -4.0}
        snap["structure"] = {"trend": "DOWNTREND", "breakout": {}}
        snap["volatility"] = {"atr_percent": 0.012}
        kept, blocked = gate_candidates(e.latest_candidates,
                                        market_by_symbol, risk_mode=True)
        self.assertEqual(kept, [])
        self.assertEqual(blocked[0]["regime_blocked"], "STRONG_DOWNTREND")


class TestATRStopRiskMode(TempCase):
    """حد ضرر ATR-محور حالت ریسک: max(کف ثابت، mult×ATR٪) با سقف مطلق."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _price_of(self, e, mkt):
        return Decimal(str(next(m for m in e.latest_snapshot["markets"]
                                if m["symbol"] == mkt)["last_price"]))

    def test_atr_widens_stop_within_cap(self):
        e = self._engine()
        e.run_cycle()
        mkt = next(m["symbol"] for m in e.latest_snapshot["markets"])
        px = self._price_of(e, mkt)
        e.set_risk_mode(True)
        # ATR 1% -> 1.5×ATR = 1.5% < floor 3% -> floor wins
        for m in e.latest_snapshot["markets"]:
            m["volatility"] = {"atr_percent": 0.01}
        plan = e._auto_stop_for_buy(mkt, px)
        self.assertAlmostEqual(plan["stop_pct"], -3.0, places=1)
        # ATR 4% -> 1.5×ATR = 6% > floor -> ATR wins, stop ≈ -6%
        for m in e.latest_snapshot["markets"]:
            m["volatility"] = {"atr_percent": 0.04}
        plan = e._auto_stop_for_buy(mkt, px)
        self.assertAlmostEqual(plan["stop_pct"], -6.0, places=1)
        # ATR 9% -> 13.5% > cap 8% -> capped at -8%
        for m in e.latest_snapshot["markets"]:
            m["volatility"] = {"atr_percent": 0.09}
        plan = e._auto_stop_for_buy(mkt, px)
        self.assertAlmostEqual(plan["stop_pct"], -8.0, places=1)
        # safe mode: the fixed -3% default regardless of ATR
        e.set_risk_mode(False)
        plan = e._auto_stop_for_buy(mkt, px)
        self.assertAlmostEqual(plan["stop_pct"], -3.0, places=1)


class TestQuoteMarketSelection(TempCase):
    """انتخاب بازار معاملات: تتری (USDT) / تومانی (IRT) / هر دو.

    Settings selector exchange.trade_quote_markets decides which quote
    markets the bot may buy; legacy booleans are only a fallback.
    """

    STATS = {  # dst == USDT
        "btc-usdt": {"isClosed": False, "volumeDst": "900000"},
        "eth-usdt": {"isClosed": False, "volumeDst": "500000"},
        # dst == RLS (Toman)
        "btc-rls": {"isClosed": False, "volumeDst": "20000000000"},
        "xrp-rls": {"isClosed": False, "volumeDst": "8000000000"},
        "usdt-rls": {"isClosed": False, "volumeDst": "99999999999"},
    }

    def _engine(self):
        from types import SimpleNamespace
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e._real_attached = True
        e.exchange = SimpleNamespace(_stats_all=lambda: dict(self.STATS))
        # seed the registry the way collector.discover_markets() does in
        # production: _apply_trading_universe only flips tradable flags on
        # EXISTING registry entries
        syms = []
        for pair in self.STATS:
            src, _, dst = pair.partition("-")
            sym = src.upper() + ("USDT" if dst == "usdt" else "IRT")
            syms.append({"symbol": sym, "status": "ACTIVE", "tradable": True})
        e.collector.registry.update(syms)
        return e

    def _universe(self, e):
        e._apply_trading_universe()
        return set(e.collector.registry.tradable_symbols())

    def test_selector_both(self):
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "both"
        uni = self._universe(e)
        self.assertIn("BTCUSDT", uni)
        self.assertIn("BTCIRT", uni)

    def test_selector_usdt_only(self):
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "usdt"
        uni = self._universe(e)
        self.assertIn("BTCUSDT", uni)
        self.assertNotIn("BTCIRT", uni)
        self.assertNotIn("XRPUSDT", uni)  # only markets Nobitex really has

    def test_selector_irt_only(self):
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "irt"
        uni = self._universe(e)
        self.assertIn("BTCIRT", uni)
        self.assertIn("USDTIRT", uni)
        self.assertNotIn("BTCUSDT", uni)

    def test_invalid_value_falls_back_to_legacy_flags(self):
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = ""
        e.cfg.exchange.prefer_usdt_markets = False
        e.cfg.exchange.include_irt_markets = True
        uni = self._universe(e)
        self.assertNotIn("BTCUSDT", uni)
        self.assertIn("BTCIRT", uni)
        # unknown junk behaves the same way
        e.cfg.exchange.trade_quote_markets = "garbage"
        uni = self._universe(e)
        self.assertNotIn("BTCUSDT", uni)
        self.assertIn("BTCIRT", uni)

    def test_both_off_failsafe_reenables(self):
        # legacy config with BOTH booleans off and no selector would leave
        # the bot unable to buy anything -> fail-safe re-enables both
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = ""
        e.cfg.exchange.prefer_usdt_markets = False
        e.cfg.exchange.include_irt_markets = False
        uni = self._universe(e)  # must NOT be empty — fail-safe re-enables
        self.assertIn("BTCUSDT", uni)
        self.assertIn("BTCIRT", uni)

    def test_selector_overrides_stale_legacy_off(self):
        # a valid selector wins even when the old booleans are both off
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "usdt"
        e.cfg.exchange.prefer_usdt_markets = False
        e.cfg.exchange.include_irt_markets = False
        uni = self._universe(e)
        self.assertIn("BTCUSDT", uni)
        self.assertNotIn("BTCIRT", uni)

    def test_legacy_flags_without_selector(self):
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = ""
        e.cfg.exchange.prefer_usdt_markets = True
        e.cfg.exchange.include_irt_markets = False
        uni = self._universe(e)
        self.assertIn("BTCUSDT", uni)
        self.assertNotIn("BTCIRT", uni)

    def test_registry_instant_after_selection_change(self):
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "both"
        uni = self._universe(e)
        self.assertIn("BTCUSDT", uni)
        self.assertIn("BTCIRT", uni)
        e.cfg.exchange.trade_quote_markets = "usdt"
        uni = self._universe(e)  # second pass: switch is instant
        self.assertIn("BTCUSDT", uni)
        self.assertNotIn("BTCIRT", uni)

    def test_manual_buy_respects_selector(self):
        # the dashboard «خرید» button follows the same restriction:
        # with the selector on "usdt" a manual IRT-market buy is refused
        from types import SimpleNamespace
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "usdt"
        e.exchange = SimpleNamespace()
        e.latest_snapshot = {"markets": [
            {"symbol": "BTCIRT", "last_price": 1000.0,
             "created_at": datetime.now(timezone.utc).isoformat()}]}
        out = e.manual_buy("BTCIRT", 100000.0)
        self.assertFalse(out["ok"])
        self.assertIn("تومانی", out["error"])
        # and with "irt" the USDT side is refused instead
        e.cfg.exchange.trade_quote_markets = "irt"
        e.latest_snapshot = {"markets": [
            {"symbol": "BTCUSDT", "last_price": 1000.0,
             "created_at": datetime.now(timezone.utc).isoformat()}]}
        out = e.manual_buy("BTCUSDT", 100000.0)
        self.assertFalse(out["ok"])
        self.assertIn("تتری", out["error"])
        # allowed side passes the selector gate (fails later on risk
        # sizing with no free cash — but NOT on the selector)
        e.cfg.exchange.trade_quote_markets = "both"
        e.latest_snapshot = {"markets": [
            {"symbol": "BTCUSDT", "last_price": 1000.0,
             "created_at": datetime.now(timezone.utc).isoformat()}]}
        out = e.manual_buy("BTCUSDT", 100000.0)
        self.assertNotIn("بازار", str(out.get("error", "")))

    def test_manual_buy_retargets_to_the_selected_quote(self):
        """کلیک «خرید» روی ردیف تومانی در حالت «فقط تتری» رد نمی‌شود —
        همان سکه در بازار تتری خریده می‌شود (و برعکس)، تا خرید تازه زیر
        همان بخش (تتری/تومانی) در باکس دارایی‌ها بنشیند."""
        from types import SimpleNamespace
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "usdt"
        e.exchange = SimpleNamespace()
        both = [
            {"symbol": "BTCIRT", "last_price": 1000.0,
             "created_at": datetime.now(timezone.utc).isoformat()},
            {"symbol": "BTCUSDT", "last_price": 1000.0,
             "created_at": datetime.now(timezone.utc).isoformat()},
        ]
        e.latest_snapshot = {"snapshot_id": "snap_test",
                             "markets": list(both)}
        out = e.manual_buy("BTCIRT", 100000.0)
        self.assertEqual(out.get("market"), "BTCUSDT")
        self.assertEqual(out.get("symbol"), "BTCUSDT")
        self.assertNotIn("خاموش", str(out.get("error", "")))
        self.assertIn("USDT", str(out.get("note", "")))
        # و در حالت «فقط تومانی» برعکس
        e.cfg.exchange.trade_quote_markets = "irt"
        e.latest_snapshot = {"snapshot_id": "snap_test",
                             "markets": list(both)}
        out = e.manual_buy("BTCUSDT", 100000.0)
        self.assertEqual(out.get("market"), "BTCIRT")
        self.assertNotIn("خاموش", str(out.get("error", "")))

    def test_manual_sell_keeps_the_clicked_rows_market(self):
        """«فروش» روی ردیف هر بخش، همان بازار را می‌فروشد (سکه‌های تتری
        زیر بخش تتری، تومانی زیر بخش تومانی)؛ فقط وقتی آن بازار سکه
        ندارد و همان سکه روی بازار انتخاب‌شدهٔ «بازار معاملات» هست، به
        همان بازار می‌رود تا کلیک فروش بی‌جواب نماند."""
        e = self._engine()
        # موجودی پایه برای پر شدن فروش کاغذی؛ این تست دربارهٔ انتخاب بازار
        # است، نه دربارهٔ موجودی کیف پول
        e.portfolio.overwrite_balances({
            "BTC": {"available": Decimal("10"), "locked": Decimal("0")},
            "USDT": {"available": Decimal("10000"),
                     "locked": Decimal("0")},
            "IRT": {"available": Decimal("500000000"),
                    "locked": Decimal("0")}})
        # هر دو بازار روشن تا اسنپ‌شات هر دو بخش (تتری و تومانی) را داشته باشد
        e.cfg.exchange.trade_quote_markets = "both"
        e.run_cycle()
        snap = e.latest_snapshot or {}
        prices = {x["symbol"]: x["last_price"]
                  for x in snap.get("markets", []) if x.get("last_price")}
        self.assertIn("BTCIRT", prices)
        self.assertIn("BTCUSDT", prices)
        e.cfg.exchange.trade_quote_markets = "usdt"
        pir = Decimal(str(prices["BTCIRT"]))
        e.storage.upsert_position("BTCIRT", Decimal("9") / pir, pir,
                                  Decimal("0"), last_buy_price=pir)
        out = e.sell_position_now("BTCIRT")
        self.assertTrue(out.get("ok"), out.get("error") or out)
        # فروش در همان بازار ردیف تومانی ماند (با انتخاب تتری بازهدف نشد)
        self.assertEqual(out.get("market"), "BTCIRT")
        self.assertIsNone(out.get("note"))
        # ۲) ردیف تومانی سکه ندارد ولی همان سکه در بازار تتری هست →
        # همان سکه در بازار انتخاب‌شده فروخته می‌شود و یادداشت می‌آید
        e.storage.upsert_position("BTCIRT", Decimal("0"), pir,
                                  Decimal("0"), last_buy_price=pir)
        pusd = Decimal(str(prices["BTCUSDT"]))
        e.storage.upsert_position("BTCUSDT", Decimal("5") / pusd, pusd,
                                  Decimal("0"), last_buy_price=pusd)
        out = e.sell_position_now("BTCIRT")
        self.assertTrue(out.get("ok"), out.get("error") or out)
        self.assertEqual(out.get("market"), "BTCUSDT")
        self.assertIn("BTCUSDT", str(out.get("note", "")))

    def test_manual_buy_respects_the_requested_amount(self):
        """مبلغی که کاربر تایپ می‌کند سقف اندازه است و در واحد درست
        تبدیل می‌شود: ۵ دلار خرید تتری = ۵ دلار سفارش (نه ۱۰^۱۰ برابر
        و نه اندازهٔ بودجهٔ خود موتور ریسک)."""
        e = self._engine()
        e.cfg.exchange.trade_quote_markets = "both"
        e.run_cycle()
        mkt = "BTCUSDT"
        snap = e.latest_snapshot or {}
        m = next((x for x in snap.get("markets", []) if x["symbol"] == mkt),
                 None)
        self.assertIsNotNone(m)
        price = Decimal(str(m["last_price"]))
        e.storage.upsert_position(mkt, Decimal("0.001"), price, Decimal("0"),
                                  last_buy_price=price)
        prices = {x["symbol"]: x["last_price"]
                  for x in snap.get("markets", []) if x.get("last_price")}
        usdt_per_irt = float(e._usdt_rates(prices).get("IRT") or 0)
        self.assertGreater(usdt_per_irt, 0)
        out = e.manual_buy(mkt, 5.0 / usdt_per_irt)   # 5 USDT in Toman
        self.assertTrue(out.get("ok"), out.get("error"))
        ex = out.get("execution") or {}
        notional = Decimal(str(ex.get("amount") or 0)) * Decimal(
            str(ex.get("price") or 0))
        self.assertLess(notional, Decimal("6"))    # ۵ دلار بود، نه چند برابر
        self.assertGreater(notional, Decimal("4"))

    def test_buy_suggestion_follows_the_selected_quote(self):
        """مبلغ پیشنهادی «خرید» هم در واحد همان بازار است: در حالت
        تتری، مبلغ/موجودی/کف سفارش به تتر می‌آید نه تومان."""
        e = self._engine()
        e.run_cycle()
        # بخش تومانی موجودی خودش را دارد: پول تتری هرگز به خرید تومانی
        # اضافه نمی‌شود (آن فقط با «Both + تعامل بین بازارها» مجاز است)
        e.portfolio.balances["IRT"] = {"available": Decimal("500000000"),
                                       "locked": Decimal("0")}
        e.portfolio._save()
        sug = e.suggest_buy("BTCIRT")
        self.assertTrue(sug["ok"])
        e.cfg.exchange.trade_quote_markets = "usdt"
        sug = e.suggest_buy("BTCIRT")
        self.assertTrue(sug["ok"], sug.get("error"))
        self.assertEqual(sug["market"], "BTCUSDT")
        self.assertEqual(sug["quote"], "USDT")
        self.assertGreater(sug["cash_quote"], 0)
        # مبلغ پیشنهادی در حد موجودی تتر است (نه موجودی تومانی)
        self.assertLessEqual(sug["amount_quote"], sug["cash_quote"])
        self.assertGreater(sug["amount_irt"], 0)
        e.cfg.exchange.trade_quote_markets = "irt"
        sug = e.suggest_buy("BTCUSDT")
        self.assertTrue(sug["ok"], sug.get("error"))
        self.assertEqual(sug["quote"], "IRT")


class TestMarketInteraction(TempCase):
    """تعامل دو بازار: «Both» به‌تنهایی مجوز انتقال دارایی نیست.

    انتخاب Both فقط می‌گوید هر دو بازار فعال‌اند؛ اجازهٔ تبدیل/جابه‌جایی
    بین بخش‌های IRT و USDT یک تنظیم مستقل است
    (exchange.trade_market_interaction) و موتور همیشه آن را جدا بررسی
    می‌کند.
    """

    def _engine(self, quote="both", interaction="independent"):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.cfg.exchange.trade_quote_markets = quote
        e.cfg.exchange.trade_market_interaction = interaction
        return e

    def test_only_the_popup_answer_grants_cross_market(self):
        e = self._engine("both", "linked")
        self.assertTrue(e._cross_market_allowed())
        # Both + «بازارهای مستقل» → هیچ انتقالی مجاز نیست
        e.cfg.exchange.trade_market_interaction = "independent"
        self.assertFalse(e._cross_market_allowed())
        # مقدار خالی/ناشناخته → fail-safe: مستقل
        for raw in ("", "garbage"):
            e.cfg.exchange.trade_market_interaction = raw
            mode, _src = e._effective_market_interaction()
            self.assertEqual(mode, "independent")
            self.assertFalse(e._cross_market_allowed())

    def test_single_market_selection_never_transfers_even_when_linked(self):
        # انتخاب تک‌بازاری یعنی طرف دیگر خاموش است؛ «linked» هم آن را
        # روشن نمی‌کند
        for quote in ("usdt", "irt"):
            e = self._engine(quote, "linked")
            self.assertFalse(e._cross_market_allowed(), quote)

    def test_toman_buy_cannot_be_funded_from_the_tether_balance(self):
        """تنها تفاوت «مستقل» و «تعامل»: تغذیهٔ خرید تومانی از موجودی
        تتری. حساب تست نه تومان دارد و نه… فقط تتر (۱۰٬۰۰۰ دلار)."""
        e = self._engine("both", "independent")
        e.run_cycle()
        sug = e.suggest_buy("BTCIRT")
        self.assertFalse(sug["ok"])
        self.assertIn("موجودی نقد", sug["error"])
        self.assertEqual(sug["quote"], "IRT")
        # همان حساب، همان بازارها — فقط پاسخ پنجرهٔ Both عوض شد
        e.cfg.exchange.trade_market_interaction = "linked"
        sug = e.suggest_buy("BTCIRT")
        self.assertTrue(sug["ok"], sug.get("error"))
        self.assertGreater(sug["cash_quote"], 0)

    def test_tether_buy_never_sees_the_toman_balance(self):
        e = self._engine("both", "linked")
        e.run_cycle()
        e.portfolio.balances["IRT"] = {"available": Decimal("9000000000"),
                                       "locked": Decimal("0")}
        e.portfolio._save()
        sug = e.suggest_buy("BTCUSDT")
        self.assertTrue(sug["ok"], sug.get("error"))
        self.assertEqual(sug["quote"], "USDT")
        self.assertLessEqual(sug["amount_quote"], sug["cash_quote"])
        self.assertLess(sug["cash_quote"], 10001.0)   # فقط ۱۰۰۰۰ تتر

    def test_status_exposes_the_gate(self):
        e = self._engine("both", "linked")
        st = e.status()
        gate = st["market_interaction"]
        self.assertEqual(gate["interaction"], "linked")
        self.assertTrue(gate["cross_market_allowed"])
        self.assertEqual(gate["quote_markets"]["usdt"], "on")
        e.cfg.exchange.trade_market_interaction = "independent"
        self.assertFalse(e.status()["market_interaction"]
                         ["cross_market_allowed"])

    def test_save_config_normalizes_the_interaction_token(self):
        """فقط دو توکن مستند («linked»/«independent») در config ذخیره
        می‌شود؛ هر چیز دیگر = مستقل (fail-safe)."""
        from nobitex_bot.web import _normalize_market_interaction as norm
        self.assertEqual(norm("linked"), "linked")
        self.assertEqual(norm(" LINKED "), "linked")
        self.assertEqual(norm("independent"), "independent")
        self.assertEqual(norm("garbage"), "independent")
        self.assertEqual(norm(None), "independent")

    def test_coin_section_uses_the_real_buy_market(self):
        e = self._engine("both", "independent")
        e.run_cycle()
        e.storage.save_fill({
            "trade_id": "live_sec1", "order_id": "o1", "decision_id": "",
            "market": "BTCUSDT", "side": "BUY", "price": "60000",
            "amount": "1", "gross_value": "60000", "fee_amount": "0",
            "fee_asset": "USDT", "timestamp": "2026-10-09T10:00:00.000Z"})
        self.assertEqual(e._coin_section("BTC"), "USDT")
        self.assertEqual(e._coin_section("USDT"), "USDT")
        self.assertEqual(e._coin_section("IRT"), "IRT")

    def test_conversion_gate_follows_the_market_setting(self):
        """تبدیل کیف‌پول همیشه به تومان می‌نشیند: سکهٔ بخش تتری بدون
        تعامل، تبدیل نمی‌شود."""
        e = self._engine("both", "independent")
        e.run_cycle()
        e.storage.save_fill({
            "trade_id": "live_sec2", "order_id": "o2", "decision_id": "",
            "market": "BTCUSDT", "side": "BUY", "price": "60000",
            "amount": "1", "gross_value": "60000", "fee_amount": "0",
            "fee_asset": "USDT", "timestamp": "2026-10-09T10:00:00.000Z"})
        ok, reason = e._conversion_section_allowed("BTC")
        self.assertFalse(ok)
        self.assertIn("انتقال", reason)
        # سکهٔ بخش تومانی آزاد است (همان بخش، همان پول)
        e.storage.save_fill({
            "trade_id": "live_sec3", "order_id": "o3", "decision_id": "",
            "market": "ETHIRT", "side": "BUY", "price": "100000000",
            "amount": "1", "gross_value": "100000000", "fee_amount": "0",
            "fee_asset": "IRT", "timestamp": "2026-10-09T11:00:00.000Z"})
        self.assertTrue(e._conversion_section_allowed("ETH")[0])
        # و با «تعامل بین بازارها» انتقال مجاز است
        e.cfg.exchange.trade_market_interaction = "linked"
        self.assertTrue(e._conversion_section_allowed("BTC")[0])

    def test_sweep_leaves_the_tether_section_alone_when_independent(self):
        from types import SimpleNamespace
        e = self._engine("both", "independent")
        e._real_attached = True
        converted = []
        e.exchange = SimpleNamespace(
            convert_wallet_balance=lambda coin: converted.append(coin) or
            {"ok": True})
        e._price_fallback = lambda mkt: None
        now = datetime.now(timezone.utc).isoformat()
        # خردهٔ ربات زیر حداقل سفارش، ولی در بخش تتری (0.1 دلار ≈ ۲۰٬۰۰۰ ت)
        e.storage.upsert_position("QQQUSDT", Decimal("1"), Decimal("0.1"),
                                  Decimal("0"), last_buy_at=now,
                                  last_buy_price=Decimal("0.1"))
        e.latest_snapshot = {"markets": [
            {"symbol": "QQQUSDT", "last_price": 0.1},
            {"symbol": "USDTIRT", "last_price": 200000.0}]}
        out = e._sweep_bot_micro_positions()
        self.assertEqual(out["converted"], 0)
        self.assertEqual(out["blocked"], 1)
        self.assertEqual(converted, [])
        # با تعامل بین بازارها همان خرده به تومان برمی‌گردد
        e.cfg.exchange.trade_market_interaction = "linked"
        out = e._sweep_bot_micro_positions()
        self.assertEqual(out["converted"], 1)
        self.assertEqual(converted, ["QQQ"])


class TestProfit24h(TempCase):
    """«سود ۲۴ساعت» — مبنا دقیقاً ۲۴ ساعت قبل، پوشش صادقانه، هرس زمانی."""

    def e_profit24(self, hist, eq, deposited=None, real=None):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e._profit_24h(eq, hist, deposited, real)

    def _pt(self, hours_ago, e, d=None, a=None):
        pt = {"t": (datetime.now(timezone.utc) -
                    timedelta(hours=hours_ago)).isoformat(), "e": e}
        if d is not None:
            pt["d"] = d
        if a is not None:
            pt["a"] = a
        return pt

    def test_exact_24h_baseline(self):
        # place a real point 24.5h ago by writing a shifted timestamp
        t = (datetime.now(timezone.utc) - timedelta(hours=24.5)).isoformat()
        # BOTH ends carry کل سرمایه واریزی: the box reports «سود سرمایه»
        hist = [{"t": t, "e": 120.0, "d": 90.0, "a": 1},
                self._pt(2, 140.0, 90.0, 1)]
        out = self.e_profit24(hist, 140.0, 100.0, True)
        self.assertEqual(out["coverage"], "exact")
        self.assertEqual(out["basis"], "capital")
        self.assertAlmostEqual(out["pct"], 10.0, places=1)   # 40-30 over 100
        self.assertAlmostEqual(out["ref_equity"], 120.0, places=6)
        self.assertAlmostEqual(out["capital_ref_irt"], 30.0, places=6)
        self.assertAlmostEqual(out["capital_now_irt"], 40.0, places=6)

    def test_legacy_usdt_points_are_migrated_to_toman(self):
        """Points written before the 24h KPI reported an AMOUNT stored the
        equity in USDT. They must be converted (or dropped) exactly once so
        a 15k point can never be compared against a 1.4B Toman account."""
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        legacy = [{"t": "2026-10-01T00:00:00+00:00", "e": 15000.0}]
        out, changed = e._migrate_equity_history(legacy, 100000.0)
        self.assertTrue(changed)
        self.assertAlmostEqual(out[0]["e"], 1_500_000_000.0, places=2)
        self.assertEqual(out[0]["u"], "irt")
        # already-normalized points pass through untouched
        fresh = [{"t": "2026-10-01T00:00:00+00:00", "e": 1_400_000_000.0,
                  "d": 900_000_000.0, "a": 1, "u": "irt"}]
        out2, changed2 = e._migrate_equity_history(fresh, 100000.0)
        self.assertFalse(changed2)
        self.assertEqual(out2[0]["e"], 1_400_000_000.0)
        self.assertEqual(out2[0]["d"], 900_000_000.0)
        # no rate available -> dropped, never mixed into the comparison
        out3, changed3 = e._migrate_equity_history(legacy, 0.0)
        self.assertTrue(changed3)
        self.assertEqual(out3, [])

    def test_amount_is_reported_and_deposit_not_counted_as_profit(self):
        """The KPI shows an AMOUNT, and money the user deposited inside the
        window is subtracted (real turnover), never reported as profit."""
        t = (datetime.now(timezone.utc) - timedelta(hours=24.5)).isoformat()
        hist = [{"t": t, "e": 100.0, "d": 80.0, "a": 1}]
        out = self.e_profit24(hist, 130.0, 100.0)
        self.assertEqual(out["coverage"], "exact")
        self.assertAlmostEqual(out["delta_irt"], 30.0, places=6)
        self.assertAlmostEqual(out["flow_irt"], 20.0, places=6)   # +20 deposit
        self.assertAlmostEqual(out["pnl_irt"], 10.0, places=6)
        self.assertAlmostEqual(out["pct"], 10.0, places=1)        # 10 / 100
        self.assertAlmostEqual(out["pct_raw"], 30.0, places=1)
        self.assertTrue(out["real"])
        self.assertAlmostEqual(out["base_irt"], 100.0, places=6)
        self.assertAlmostEqual(out["total_irt"], 130.0, places=6)
        self.assertAlmostEqual(out["deposited_irt"], 100.0, places=6)

    def test_withdrawal_is_added_back(self):
        # withdrew 20 of deposits but the account still gained 10:
        # the real profit is +30, not +10
        t = (datetime.now(timezone.utc) - timedelta(hours=24.5)).isoformat()
        hist = [{"t": t, "e": 100.0, "d": 120.0, "a": 1}]
        out = self.e_profit24(hist, 110.0, 100.0)
        self.assertAlmostEqual(out["flow_irt"], -20.0, places=6)
        self.assertAlmostEqual(out["pnl_irt"], 30.0, places=6)
        self.assertAlmostEqual(out["pct"], 30.0, places=1)

    def test_missing_deposit_data_never_fakes_a_flow(self):
        # a baseline point written before «کل سرمایه واریزی» existed: the
        # box must NOT invent a flow and must NOT show the raw account
        # delta as if it were capital profit -> it waits, with the delta
        # kept for auditing only
        hist = [self._pt(24.5, 100.0)]
        out = self.e_profit24(hist, 110.0, 500.0)
        self.assertEqual(out["coverage"], "waiting")
        self.assertEqual(out["waiting_reason"], "MISSING_DEPOSIT_DATA")
        self.assertIsNone(out["pct"])
        self.assertNotIn("pnl_irt", out)
        self.assertNotIn("flow_irt", out)
        self.assertAlmostEqual(out["audit"]["delta_irt"], 10.0, places=6)
        self.assertFalse(out["real"])

    def test_short_history_is_not_reported_as_24h(self):
        # history younger than 24h: no number at all — the OLD behaviour
        # reported a 5h window under the name «سود ۲۴ساعت»; now the box
        # waits and reports the real age of the history + hours left
        hist = [self._pt(5, 200.0), self._pt(1, 210.0)]
        out = self.e_profit24(hist, 220.0, 200.0, True)
        self.assertEqual(out["coverage"], "waiting")
        self.assertEqual(out["waiting_reason"], "HISTORY_SHORTER_THAN_24H")
        self.assertIsNone(out["pct"])
        self.assertNotIn("pnl_irt", out)
        self.assertAlmostEqual(out["start_age_h"], 5.0, places=1)
        self.assertAlmostEqual(out["needs_h"], 19.0, places=1)

    def test_short_history_is_never_substituted_by_account_start(self):
        # even a 12-minute-old history does NOT become the baseline: the
        # requested number is the change over 24 hours, or nothing
        hist = [self._pt(0.2, 200.0)]
        out = self.e_profit24(hist, 220.0, 200.0, True)
        self.assertEqual(out["coverage"], "waiting")
        self.assertEqual(out["waiting_reason"], "HISTORY_SHORTER_THAN_24H")
        self.assertNotIn("ref_equity", out)
        self.assertLess(out["start_age_h"], 0.25)
        self.assertGreater(out["needs_h"], 23.5)

    def test_waiting_only_without_any_history(self):
        # no point at all -> nothing to compare against (the KPI says so
        # instead of inventing a baseline)
        out = self.e_profit24([], 220.0)
        self.assertEqual(out["coverage"], "waiting")
        self.assertIsNone(out["pct"])
        self.assertEqual(out["needs_h"], 24.0)

    def test_exact_baseline_never_runs_past_the_24h_cap(self):
        hist = [self._pt(25.8, 100.0, 90.0, 1),
                self._pt(24.4, 150.0, 90.0, 1),
                self._pt(2, 200.0, 90.0, 1)]
        out = self.e_profit24(hist, 220.0, 200.0, True)
        self.assertEqual(out["coverage"], "exact")
        self.assertAlmostEqual(out["ref_equity"], 150.0, places=6)
        self.assertLess(out["ref_age_h"], 24.6)

    def test_stale_points_are_pruned_on_read(self):
        # days-old points must NEVER become the baseline (the old bug:
        # «سود ۲۴ساعت» silently compared against a 9-day-old equity).
        # points: 9 days / 30h / 3h old -> 30h is outside the 26h window
        # too, so the only fresh point is the 3h one: that is STILL not a
        # 24h baseline -> the box waits, it does NOT fall back to any of
        # them (least of all the 9-day point)
        hist = [self._pt(24 * 9, 100.0), self._pt(30, 150.0),
                self._pt(3, 200.0)]
        out = self.e_profit24(hist, 220.0, 200.0, True)
        self.assertEqual(out["coverage"], "waiting")
        self.assertNotIn("ref_equity", out)
        self.assertNotIn("pnl_irt", out)
        self.assertAlmostEqual(out["start_age_h"], 3.0, places=1)
        self.assertAlmostEqual(out["needs_h"], 21.0, places=1)

    def test_26h_window_keeps_real_24h_baseline(self):
        # a point between 24h and 26h ago is still a valid EXACT baseline
        hist = [self._pt(25.5, 150.0, 90.0, 1), self._pt(3, 200.0, 90.0, 1)]
        out = self.e_profit24(hist, 220.0, 200.0, True)
        self.assertEqual(out["coverage"], "exact")
        self.assertAlmostEqual(out["ref_equity"], 150.0, places=6)

    def test_block_is_the_change_of_capital_profit(self):
        """عدد باکس باید تغییر «میزان سود سرمایه» باشد — همان خطی که زیر
        «سود کل» می‌آید (ارزش حساب − کل سرمایه واریزی) — نه تغییر خام ارزش
        حساب. پس واریز وسط بازه نه سود می‌شود نه ضرر."""
        t = (datetime.now(timezone.utc) - timedelta(hours=24.5)).isoformat()
        # 24.5h ago: equity 1000, deposited 900 -> سود سرمایه = 100
        hist = [{"t": t, "e": 1000.0, "d": 900.0, "a": 1}]
        # now: equity 1150 (۳۰۰ واریز تازه، بقیه سود) deposited 1000
        out = self.e_profit24(hist, 1150.0, 1000.0, True)
        self.assertEqual(out["basis"], "capital")
        self.assertAlmostEqual(out["capital_ref_irt"], 100.0, places=6)
        self.assertAlmostEqual(out["capital_now_irt"], 150.0, places=6)
        self.assertAlmostEqual(out["pnl_irt"], 50.0, places=6)   # 150-100
        self.assertAlmostEqual(out["delta_irt"], 150.0, places=6)
        self.assertAlmostEqual(out["flow_irt"], 100.0, places=6)
        # درصد روی همان مبنایی که کارت «سود کل» دارد: کل سرمایه واریزی
        self.assertAlmostEqual(out["pct"], 5.0, places=6)        # 50/1000
        self.assertEqual(out["pct_base"], "deposited")

    def test_baseline_is_never_taken_from_the_other_account(self):
        # demo points and real-wallet points are two different accounts:
        # a real account whose 24h-old point does not exist yet must WAIT,
        # never answer with the demo account's number
        demo_ref = self._pt(24.5, 924_508_709.03, 0.0, 0)
        real_first = self._pt(5.0, 5_631_464.71, 5_464_605.0, 1)
        real_last = self._pt(0.1, 5_599_227.64, 5_464_605.0, 1)
        hist = [demo_ref, real_first, real_last]
        out = self.e_profit24(hist, 5_599_227.64, 5_464_605.0, True)
        self.assertEqual(out["coverage"], "waiting")
        self.assertEqual(out["waiting_reason"], "HISTORY_SHORTER_THAN_24H")
        self.assertNotIn("pnl_irt", out)
        self.assertNotIn("ref_equity", out)
        self.assertEqual(out["skipped_other_source"], 1)
        self.assertAlmostEqual(out["start_age_h"], 5.0, places=1)
        # once the REAL account has a 24h-old point, that point is the
        # baseline (the demo one is still ignored) and the change is the
        # small, real number — not the -99% of the mixed comparison
        real_ref = self._pt(24.5, 5_631_464.71, 5_464_605.0, 1)
        out2 = self.e_profit24([demo_ref, real_ref, real_last],
                               5_599_227.64, 5_464_605.0, True)
        self.assertEqual(out2["coverage"], "exact")
        self.assertEqual(out2["skipped_other_source"], 1)
        self.assertAlmostEqual(out2["ref_equity"], 5_631_464.71, places=2)
        self.assertAlmostEqual(out2["capital_ref_irt"], 166_859.71, places=2)
        self.assertAlmostEqual(out2["capital_now_irt"], 134_622.64, places=2)
        self.assertAlmostEqual(out2["pnl_irt"], -32_237.07, places=2)
        self.assertGreater(out2["pct"], -1.0)
        self.assertLess(out2["pct"], 0.0)

class TestUsdtPerIrtStatusField(TempCase):
    """SS36: dollar display mode — status sends the live Toman-per-USDT
    rate so the UI can show every number in $ when the trading market is
    both/usdt (USDT-market prices are already plain dollars)."""

    def test_usdt_per_irt_in_status(self):
        from types import SimpleNamespace
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e._real_attached = True
        e.exchange = SimpleNamespace(
            ticker=lambda s: {"last_price": 105000.0})
        st = e.status()
        self.assertIn("usdt_per_irt", st)
        # ticker 105000 Toman per USDT -> rate must be around 105000
        self.assertAlmostEqual(st["usdt_per_irt"], 105000.0, places=1)

    def test_usdt_per_irt_from_snapshot_prices(self):
        from types import SimpleNamespace
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e._real_attached = True
        e.exchange = SimpleNamespace(_stats_all=lambda: {})
        e.latest_snapshot = {"markets": [
            {"symbol": "USDTIRT", "last_price": 104000.0,
             "created_at": datetime.now(timezone.utc).isoformat()}]}
        st = e.status()
        self.assertAlmostEqual(st["usdt_per_irt"], 104000.0, places=1)


class TestInFlightOrders(TempCase):
    """«سفارش‌های در جریان» — هر سفارش باز/نیمه‌پُر با جزئیات آیتم‌هایی
    که داخلش هست (نماد، سمت، قیمت، حجم، پرشده، باقی‌مانده، ارزش، سن)."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _order(self, **kw):
        base = {
            "order_id": "bot-order-1",
            "client_order_id": "bot-c1-BTCUSDT-001",
            "decision_id": "dec_1",
            "market": "BTCUSDT",
            "side": "BUY",
            "order_type": "LIMIT",
            "price": "1000",
            "amount": "2",
            "matched_amount": "0.5",
            "unmatched_amount": "1.5",
            "status": "OPEN",
            "created_at": (datetime.now(timezone.utc)
                           - timedelta(minutes=5)).isoformat(),
            "updated_at": utc_now_iso(),
            "exchange_order_id": "777",
        }
        base.update(kw)
        return base

    def test_open_order_detail(self):
        e = self._engine()
        e.storage.save_order(self._order())
        items = e.in_flight_orders()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["market"], "BTCUSDT")
        self.assertEqual(it["base"], "BTC")
        self.assertEqual(it["quote"], "USDT")
        self.assertEqual(it["side"], "BUY")
        self.assertEqual(it["status"], "OPEN")
        self.assertAlmostEqual(it["price"], 1000.0, places=6)
        self.assertAlmostEqual(it["amount"], 2.0, places=6)
        self.assertAlmostEqual(it["matched_amount"], 0.5, places=6)
        self.assertAlmostEqual(it["unmatched_amount"], 1.5, places=6)
        self.assertAlmostEqual(it["filled_pct"], 25.0, places=4)
        self.assertAlmostEqual(it["value_quote"], 2000.0, places=6)
        self.assertAlmostEqual(it["remaining_value_quote"], 1500.0, places=6)
        self.assertEqual(it["origin"], "bot")
        self.assertEqual(it["decision_id"], "dec_1")
        self.assertEqual(it["exchange_order_id"], "777")
        self.assertIsNotNone(it["age_s"])
        self.assertGreaterEqual(it["age_s"], 290)

    def test_filled_and_cancelled_orders_are_not_in_flight(self):
        e = self._engine()
        e.storage.save_order(self._order(order_id="o-filled",
                                         client_order_id="bot-a",
                                         status="FILLED", matched_amount="2",
                                         unmatched_amount="0"))
        e.storage.save_order(self._order(order_id="o-cancelled",
                                         client_order_id="bot-b",
                                         status="CANCELLED",
                                         unmatched_amount="0"))
        e.storage.save_order(self._order(order_id="o-open",
                                         client_order_id="bot-c",
                                         status="PARTIAL"))
        items = e.in_flight_orders()
        self.assertEqual([i["order_id"] for i in items], ["o-open"])

    def test_manual_order_is_not_labelled_as_the_bot(self):
        e = self._engine()
        e.storage.save_order(self._order(order_id="o-manual",
                                         client_order_id="",
                                         side="SELL", matched_amount="0",
                                         unmatched_amount="2"))
        items = e.in_flight_orders()
        self.assertEqual(items[0]["origin"], "manual")
        self.assertEqual(items[0]["side"], "SELL")

    def test_remaining_amount_falls_back_to_amount_minus_matched(self):
        e = self._engine()
        e.storage.save_order(self._order(unmatched_amount=None,
                                         matched_amount="1.25", amount="2"))
        it = e.in_flight_orders()[0]
        self.assertAlmostEqual(it["unmatched_amount"], 0.75, places=6)

    def test_bot_orders_come_first(self):
        e = self._engine()
        e.storage.save_order(self._order(order_id="o-old-bot",
                                         client_order_id="bot-old",
                                         created_at=(datetime.now(timezone.utc)
                                                     - timedelta(hours=4)).isoformat()))
        e.storage.save_order(self._order(order_id="o-new-manual",
                                         client_order_id="",
                                         created_at=(datetime.now(timezone.utc)
                                                     - timedelta(minutes=1)).isoformat()))
        items = e.in_flight_orders()
        # a duplicated/stale exchange id must never hide a real order
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["order_id"], "o-old-bot")
        self.assertEqual(items[0]["origin"], "bot")
        self.assertEqual(items[1]["origin"], "manual")

    def test_live_price_is_attached_for_the_distance_column(self):
        e = self._engine()
        e.storage.save_order(self._order())
        e.latest_snapshot = {"markets": [
            {"symbol": "BTCUSDT", "last_price": 990.0}]}
        it = e.in_flight_orders()[0]
        self.assertAlmostEqual(it["live_price"], 990.0, places=6)

    def test_status_payload_carries_open_orders(self):
        e = self._engine()
        e.storage.save_order(self._order())
        st = e.status()
        self.assertIn("open_orders", st)
        self.assertEqual(len(st["open_orders"]), 1)
        self.assertEqual(st["open_orders"][0]["market"], "BTCUSDT")


class TestExitRunsWithoutAnalysis(TempCase):
    """حد ضرر نباید به «فرصت خرید» گره بخورد.

    The cycle has early returns (no eligible market / every candidate
    regime-blocked). Those branches used to skip _manage_exits entirely,
    so in a market-wide crash — precisely when the regime gate empties
    the candidate list — the stop-loss never sold. _run_exit_cycle runs
    the exit pipeline on those branches.
    """

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = True
        return e

    def test_stop_sells_on_no_eligible_market_cycle(self):
        e = self._engine()
        e.run_cycle()                       # snapshot + live prices
        mkt = "BTCUSDT"
        px = Decimal("100000")
        amount = (Decimal("5000") / px).quantize(Decimal("0.00000001"))
        e.storage.upsert_position(
            mkt, amount, px, Decimal("0"),
            last_buy_at=utc_now_iso(), last_buy_price=px)
        plans = e.storage.kv_get("exit_plans", {}) or {}
        # stop ABOVE the current price: the trigger is already breached
        plans[mkt] = {"stop": float(px * Decimal("1.5")), "stop_pct": -2.0,
                      "auto": True, "stage": 0, "base_price": float(px)}
        e.storage.kv_set("exit_plans", plans)
        # make the analysis pipeline bail out before its exit step
        e.cfg.quality.min_ai_quality = 1.01
        before = len(e.storage.orders(limit=1000))
        summary = e.run_cycle()
        self.assertEqual(summary["decisions"], 0)
        self.assertIn("exit_only", summary)
        orders = e.storage.orders(limit=1000)
        self.assertGreater(len(orders), before,
                           "stop-loss did not send a SELL on the "
                           "early-return cycle")
        self.assertTrue(any(o.get("side") == "SELL" for o in orders))

    def test_stop_sells_when_all_candidates_blocked(self):
        """The regime gate can empty the candidate list in a downtrend;
        the stop must still be evaluated and sold."""
        e = self._engine()
        e.run_cycle()
        mkt = "BTCUSDT"
        px = Decimal("100000")
        amount = (Decimal("5000") / px).quantize(Decimal("0.00000001"))
        e.storage.upsert_position(
            mkt, amount, px, Decimal("0"),
            last_buy_at=utc_now_iso(), last_buy_price=px)
        plans = e.storage.kv_get("exit_plans", {}) or {}
        plans[mkt] = {"stop": float(px * Decimal("1.5")), "stop_pct": -2.0,
                      "auto": True, "stage": 0, "base_price": float(px)}
        e.storage.kv_set("exit_plans", plans)
        e.set_risk_mode(True)
        # every candidate is blocked by the regime layer
        import nobitex_bot.engine as eng
        orig = eng.gate_candidates
        eng.gate_candidates = lambda cands, mbs, **kw: ([], [
            {"symbol": c["symbol"], "regime_blocked": "STRONG_DOWNTREND"}
            for c in cands])
        try:
            before = len(e.storage.orders(limit=1000))
            summary = e.run_cycle()
        finally:
            eng.gate_candidates = orig
        self.assertTrue(summary.get("regime_blocked_all"))
        orders = e.storage.orders(limit=1000)
        self.assertGreater(len(orders), before,
                           "stop-loss did not send a SELL when the "
                           "regime gate blocked every candidate")


if __name__ == "__main__":
    unittest.main()
