"""Committee tests — multi-model votes, the expected-value gate, scoreboard.

The user's requirement: "the AI must decide logically, so that after the buy
the price goes UP — use several AI models and build a structure for it."
These tests pin that structure down:

* every model votes with p_up + expected_move_pct;
* a BUY needs a quorum, p_up, enough upside AND positive expected value
  after fees/slippage — a decent-but-not-enough setup is blocked, not bought;
* veto models (liquidity / volatility) can block but never approve;
* an LLM voter can never approve an inconsistent forecast (BUY with a
  negative expected move) and a broken reply is an ABSTAIN;
* the AI layer downgrades an engine BUY the committee rejects, and in
  "decide" mode promotes a committee BUY with a complete entry plan;
* the scoreboard records the forecast and later scores the real move.

Run:  python -m unittest tests.test_ai_committee -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.ai import AILayer, HeuristicAI  # noqa: E402
from nobitex_bot.storage import Storage  # noqa: E402
from nobitex_bot.ai_committee import (  # noqa: E402
    ABSTAIN, AVOID, BUY, HOLD, AICommittee, CommitteeScoreboard, combine,
    vote_breakout, vote_extension, vote_liquidity, vote_llm, vote_meanrev,
    vote_momentum, vote_trend, vote_volatility, vote_volume,
)


def candidate(symbol="BTCUSDT", trend=0.8, momentum=0.8, mom24=0.8,
              liquidity=0.9, vol_suit=0.6, breakout="UP") -> dict:
    return {
        "symbol": symbol,
        "score": 0.85,
        "regime": "TREND_UP",
        "reasons": ["TREND_ALIGNMENT", "POSITIVE_MOMENTUM"],
        "signals": {"trend": trend, "momentum": momentum,
                    "momentum_24h": mom24, "liquidity": liquidity,
                    "volatility": vol_suit, "orderbook": 0.8, "risk": 0.8},
        "structure": {"breakout": {"detected": breakout != "NONE",
                                   "direction": breakout}},
    }


def snapshot(symbol="BTCUSDT", price=100.0, atr_pct=0.04, rsi=55.0,
             e20=None, liq_status="OK", liq_score=0.85, spread=0.2,
             dq=0.9, trend="UP", breakout="UP", macd=1.0) -> dict:
    e20 = price if e20 is None else e20
    return {
        "symbol": symbol,
        "last_price": price,
        "spread_percent": spread,
        "change_24h": 6.0,
        "liquidity": {"score": liq_score, "status": liq_status,
                      "estimated_buy_slippage": 0.001},
        "volatility": {"atr": price * atr_pct, "atr_percent": atr_pct,
                       "volatility_regime": "NORMAL"},
        "technicals": {"rsi": rsi, "macd": {"histogram": macd},
                       "ema": {"9": price * 1.01, "20": e20,
                               "50": price * 0.97}},
        "structure": {"trend": trend,
                      "breakout": {"detected": breakout != "NONE",
                                   "direction": breakout}},
        "data_quality": {"score": dq},
    }


def ctx(price=100.0, atr_pct=0.04, cost_pct=0.6) -> dict:
    return {"price": price, "atr_pct": atr_pct, "cost_pct": cost_pct,
            "horizon_s": 14400, "atr_crash_pct": 8.0}


def vote(verdict, p=0.7, move=1.5, weight=1.0, veto=False, model="m") -> dict:
    return {"model": model, "weight": weight, "p_up": p,
            "expected_move_pct": move, "verdict": verdict, "veto": veto,
            "reason": verdict}


class TestCombine(TempCase):
    """The gate math: only a positive expected value may buy."""

    def test_positive_edge_allows_buy(self):
        v = combine([vote(BUY, 0.7, 3.0, 1.0, model="a"),
                     vote(BUY, 0.7, 3.0, 1.0, model="b"),
                     vote(BUY, 0.7, 3.0, 1.0, model="c")],
                    cost_pct=0.6, atr_pct=0.04, min_votes=3)
        self.assertTrue(v["allow_buy"])
        self.assertEqual(v["consensus"], BUY)
        self.assertEqual(v["votes_buy"], 3)
        # .7*3 - .3*2 - .6 = 0.9
        self.assertAlmostEqual(v["expected_net_edge_pct"], 0.9, places=3)

    def test_small_upside_cannot_pay_the_fees(self):
        """1.5% expected with 70% odds does NOT cover 0.6% costs + 2% risk."""
        v = combine([vote(BUY, 0.7, 1.5, 1.0, model=m) for m in "abc"],
                    cost_pct=0.6, atr_pct=0.04, min_votes=3)
        self.assertFalse(v["allow_buy"])
        self.assertIn("NEGATIVE_EXPECTED_VALUE", v["blocked_by"])
        self.assertLess(v["expected_net_edge_pct"], 0)

    def test_quorum_needed(self):
        v = combine([vote(BUY, 0.8, 4.0, 1.0, model="a"),
                     vote(HOLD, 0.5, 0.0, 1.0, model="b"),
                     vote(HOLD, 0.5, 0.0, 1.0, model="c")],
                    cost_pct=0.6, atr_pct=0.04, min_votes=3)
        self.assertFalse(v["allow_buy"])
        self.assertIn("NO_QUORUM(1/3)", v["blocked_by"])
        self.assertEqual(v["consensus"], HOLD)

    def test_veto_blocks_even_with_full_quorum(self):
        veto = dict(vote(AVOID, 0.05, -1.0, 0.0, veto=True, model="liq"),
                    reason="AVOID:LIQUIDITY_CRITICAL")
        v = combine([vote(BUY, 0.85, 5.0, 1.0, model=m) for m in "abc"]
                    + [veto], cost_pct=0.6, atr_pct=0.04, min_votes=3)
        self.assertFalse(v["allow_buy"])
        self.assertEqual(v["consensus"], AVOID)
        self.assertIn("LIQUIDITY_CRITICAL", " ".join(v["blocked_by"]))

    def test_probability_gate(self):
        v = combine([vote(BUY, 0.5, 6.0, 1.0, model=m) for m in "abc"],
                    cost_pct=0.6, atr_pct=0.04, min_votes=3)
        self.assertFalse(v["allow_buy"])
        self.assertIn("P_UP_TOO_LOW", v["blocked_by"])

    def test_configured_upside_floor_is_enforced(self):
        v = combine([vote(BUY, 0.9, 4.0, 1.0, model=m) for m in "abc"],
                    cost_pct=0.6, atr_pct=0.04, min_upside_pct=6.0,
                    min_votes=3)
        self.assertAlmostEqual(v["min_upside_pct"], 6.0, places=3)
        self.assertIn("UPSIDE_TOO_SMALL", v["blocked_by"])
        self.assertFalse(v["allow_buy"])

    def test_round_trip_cost_is_charged_against_the_plan(self):
        """A 5% cost eats a 2% target: no buy, whatever the forecast says."""
        v = combine([vote(BUY, 0.9, 4.0, 1.0, model=m) for m in "abc"],
                    cost_pct=5.0, atr_pct=0.04, min_votes=3,
                    plan_tp_pct=2.0, plan_stop_pct=-2.0)
        self.assertFalse(v["allow_buy"])
        self.assertIn("NEGATIVE_EXPECTED_VALUE", v["blocked_by"])
        self.assertAlmostEqual(v["plan_ev_pct"], -3.4, places=3)

    def test_wide_plan_target_pays_the_fee(self):
        """The bot's real +25%/−3% plan: 60% odds are clearly profitable."""
        v = combine([vote(BUY, 0.6, 1.0, 1.0, model=m) for m in "abc"],
                    cost_pct=0.55, atr_pct=0.005, min_votes=3,
                    plan_tp_pct=25.0, plan_stop_pct=-3.0, hold_s=86400,
                    bar_s=1800)
        self.assertTrue(v["allow_buy"], v["blocked_by"])
        self.assertAlmostEqual(v["plan_ev_pct"], 13.25, places=2)
        self.assertEqual(v["plan_tp_pct"], 25.0)
        self.assertEqual(v["hold_s"], 86400)

    def test_abstain_models_do_not_vote(self):
        v = combine([vote(BUY, 0.8, 4.0, 1.0, model=m) for m in "abc"]
                    + [vote(ABSTAIN, 0.5, 0.0, 1.0, model="liq")],
                    cost_pct=0.6, atr_pct=0.04, min_votes=3)
        self.assertEqual(v["votes_active"], 3)
        abstained = [m for m in v["models"] if m["model"] == "liq"][0]
        self.assertEqual(abstained["verdict"], ABSTAIN)
        self.assertTrue(v["allow_buy"])

    def test_downside_falls_back_to_atr(self):
        v = combine([vote(BUY, 0.7, 3.0, 1.0, model=m) for m in "abc"],
                    cost_pct=0.6, atr_pct=0.04, min_votes=3)
        self.assertAlmostEqual(v["expected_down_pct"], 2.0, places=3)


class TestVoters(TempCase):
    def test_veto_models_never_buy(self):
        bad_liq = snapshot(liq_status="CRITICAL")
        v = vote_liquidity(candidate(), bad_liq, ctx())
        self.assertEqual(v["verdict"], AVOID)
        self.assertTrue(v["veto"])
        good = vote_liquidity(candidate(), snapshot(), ctx())
        self.assertEqual(good["verdict"], ABSTAIN)
        self.assertFalse(good.get("veto"))
        self.assertTrue(vote_volatility(candidate(),
                                        snapshot(atr_pct=0.09),
                                        ctx(atr_pct=0.09))["veto"])
        self.assertTrue(vote_volatility(candidate(),
                                        snapshot(atr_pct=0.001),
                                        ctx(atr_pct=0.001))["veto"])
        self.assertEqual(vote_volatility(candidate(), snapshot(),
                                         ctx())["verdict"], ABSTAIN)

    def test_overbought_is_not_a_buy(self):
        v = vote_meanrev(candidate(), snapshot(rsi=80.0), ctx())
        self.assertNotEqual(v["verdict"], BUY)
        self.assertLess(v["expected_move_pct"], 0)
        self.assertEqual(vote_meanrev(candidate(), snapshot(rsi=None),
                                      ctx())["verdict"], ABSTAIN)

    def test_directional_voters_vote_buy_on_strong_evidence(self):
        snap = snapshot()
        for fn in (vote_trend, vote_momentum, vote_breakout, vote_meanrev):
            v = fn(candidate(), snap, ctx())
            self.assertEqual(v["verdict"], BUY, fn.__name__)
            self.assertGreater(v["p_up"], 0.5, fn.__name__)
            self.assertGreater(v["expected_move_pct"], 0, fn.__name__)

    def test_breakout_down_is_avoid(self):
        v = vote_breakout(candidate(breakout="DOWN"),
                          snapshot(breakout="DOWN"), ctx())
        self.assertEqual(v["verdict"], AVOID)


class _Store:
    """Minimal kv store — same shape Storage exposes to the scoreboard."""

    def __init__(self):
        self.data = {}

    def kv_get(self, key, default=None):
        return self.data.get(key, default)

    def kv_set(self, key, value):
        self.data[key] = value


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def ready(self) -> bool:
        return True

    def available(self) -> bool:
        return True

    def complete_json(self, system, user, model=None):
        self.calls.append(model)
        return self.replies.pop(0) if self.replies else None


class TestLLMVoter(TempCase):
    def test_valid_forecast_is_used(self):
        llm = _FakeLLM([{"p_up": 0.72, "expected_move_pct": 2.4,
                         "verdict": "BUY", "reason": "trend up"}])
        v = vote_llm("gpt-x", candidate(), snapshot(), ctx(), llm)
        self.assertEqual(v["verdict"], BUY)
        self.assertAlmostEqual(v["p_up"], 0.72)
        self.assertEqual(llm.calls, ["gpt-x"])

    def test_broken_reply_is_only_an_abstain(self):
        for reply in (None, {"p_up": 2, "verdict": "BUY"},
                      {"p_up": 0.7, "expected_move_pct": 1,
                       "verdict": "MAYBE"}):
            v = vote_llm("m", candidate(), snapshot(), ctx(),
                         _FakeLLM([reply]))
            self.assertEqual(v["verdict"], ABSTAIN)
            self.assertIn("LLM_INVALID", v["reason"])

    def test_inconsistent_buy_is_neutralised(self):
        """«خرید» با حرکت انتظاری منفی تناقض است — به HOLD تبدیل می‌شود."""
        v = vote_llm("m", candidate(), snapshot(), ctx(),
                     _FakeLLM([{"p_up": 0.8, "expected_move_pct": -1.5,
                                "verdict": "BUY", "reason": "x"}]))
        self.assertEqual(v["verdict"], HOLD)


class TestCommittee(TempCase):
    def test_strong_setup_is_approved_after_costs(self):
        cfg = self.cfg
        cfg.ai.committee_enabled = True
        cfg.ai.committee_mode = "decide"
        committee = AICommittee(cfg)
        verdict = committee.evaluate(candidate(), snapshot())
        # trend, momentum, breakout, meanrev, volume, pump_guard,
        # extension, liquidity, volatility
        self.assertEqual(len(verdict["models"]), 9)
        self.assertTrue(verdict["allow_buy"], verdict["blocked_by"])
        self.assertGreater(verdict["expected_net_edge_pct"],
                           cfg.ai.committee_min_net_edge_pct)
        self.assertGreaterEqual(verdict["votes_buy"],
                                cfg.ai.committee_min_votes)
        self.assertEqual(verdict["horizon_s"], cfg.ai.committee_horizon_s)

    def test_quiet_market_can_still_buy(self):
        """ATR 0.5% must not lock the bot out of every trade.

        Regression: the committee compared a single 30-minute ATR against
        the full round-trip fee, so in quiet markets it blocked 100% of
        buys and the bot never traded at all.
        """
        cfg = self.cfg
        cfg.ai.committee_enabled = True
        cfg.ai.committee_mode = "decide"
        cfg.ai.committee_min_upside_pct = 0.0
        verdict = AICommittee(cfg).evaluate(candidate(),
                                            snapshot(atr_pct=0.005))
        self.assertTrue(verdict["allow_buy"], verdict["blocked_by"])
        self.assertGreater(verdict["plan_ev_pct"], 0)

    def test_a_thin_target_is_rejected(self):
        cfg = self.cfg
        cfg.risk.default_take_profit_pct = 0.8
        cfg.risk.default_stop_loss_pct = -2.0
        verdict = AICommittee(cfg).evaluate(candidate(),
                                            snapshot(atr_pct=0.005))
        self.assertFalse(verdict["allow_buy"])
        self.assertIn("NEGATIVE_EXPECTED_VALUE", verdict["blocked_by"])

    def test_weak_setup_is_rejected(self):
        cfg = self.cfg
        committee = AICommittee(cfg)
        verdict = committee.evaluate(
            candidate(trend=0.45, momentum=0.4, mom24=0.4, liquidity=0.4,
                      vol_suit=0.3, breakout="NONE"),
            snapshot(atr_pct=0.02, rsi=68.0, e20=95.0))
        self.assertFalse(verdict["allow_buy"])
        self.assertTrue(verdict["blocked_by"])

    def test_illiquid_market_is_vetoed(self):
        committee = AICommittee(self.cfg)
        verdict = committee.evaluate(candidate(),
                                     snapshot(liq_status="CRITICAL"))
        self.assertFalse(verdict["allow_buy"])
        self.assertEqual(verdict["consensus"], AVOID)

    def test_multiple_llm_models_each_vote_once(self):
        cfg = self.cfg
        cfg.ai.llm_base_url = "http://127.0.0.1:9/v1"
        cfg.ai.llm_models = "model-a, model-b"
        llm = _FakeLLM([
            {"p_up": 0.9, "expected_move_pct": 4.0, "verdict": "BUY",
             "reason": "a"},
            {"p_up": 0.8, "expected_move_pct": 3.0, "verdict": "BUY",
             "reason": "b"}])
        verdict = AICommittee(cfg, llm=llm).evaluate(candidate(), snapshot())
        self.assertEqual(sorted(llm.calls), ["model-a", "model-b"])
        self.assertEqual(len([m for m in verdict["models"]
                              if m["model"].startswith("model-")]), 2)


class TestAILayerGate(TempCase):
    """The engine's BUY must obey the committee; decide mode may promote."""

    def _layer(self):
        layer = AILayer(self.cfg)
        return layer

    def test_engine_buy_is_downgraded_without_approval(self):
        layer = self._layer()
        dec = HeuristicAI(self.cfg).decide(
            candidate(), snapshot(), {"risk": {"total_equity": 1000},
                                      "assets": [{"asset": "USDT",
                                                  "available": 5000}]},
            {}, {"USDTIRT": 100000})
        self.assertEqual(dec["action"], BUY)
        verdict = {"allow_buy": False, "consensus": HOLD, "p_up": 0.52,
                   "blocked_by": ["NO_QUORUM(1/3)"],
                   "models": [{"model": "trend", "verdict": HOLD}]}
        out = layer._committee_apply(dec, verdict, False, candidate(),
                                     snapshot(),
                                     {"risk": {"total_equity": 1000}}, {}, {})
        self.assertEqual(out["action"], HOLD)
        self.assertNotIn("entry", out)
        self.assertIn("COMMITTEE_BLOCK:NO_QUORUM(1/3)", out["reason_codes"])

    def test_decide_mode_promotes_with_a_complete_plan(self):
        self.cfg.ai.committee_mode = "decide"
        layer = self._layer()
        neutral = HeuristicAI(self.cfg).decide(
            candidate(trend=0.45, momentum=0.45, breakout="NONE"),
            snapshot(breakout="NONE"),
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})
        self.assertEqual(neutral["action"], HOLD)
        verdict = {"allow_buy": True, "consensus": BUY, "p_up": 0.81,
                   "expected_up_pct": 2.4, "blocked_by": [],
                   "models": [{"model": "trend", "verdict": BUY}]}
        out = layer._committee_apply(
            neutral, verdict, False, candidate(), snapshot(),
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})
        self.assertEqual(out["action"], BUY)
        for key in ("entry", "stop_loss", "take_profit", "position_size",
                    "risk", "cost"):
            self.assertIn(key, out)
        self.assertGreaterEqual(out["confidence"], 0.81)
        self.assertIn("COMMITTEE_BUY", out["reason_codes"])

    def test_gate_mode_never_promotes(self):
        self.cfg.ai.committee_mode = "gate"
        layer = self._layer()
        neutral = HeuristicAI(self.cfg).decide(
            candidate(trend=0.45, momentum=0.45, breakout="NONE"),
            snapshot(breakout="NONE"),
            {"risk": {"total_equity": 1000}}, {}, {"USDTIRT": 100000})
        verdict = {"allow_buy": True, "consensus": BUY, "p_up": 0.81,
                   "blocked_by": [], "models": [{"model": "trend",
                                                 "verdict": BUY}]}
        out = layer._committee_apply(
            neutral, verdict, False, candidate(), snapshot(),
            {"risk": {"total_equity": 1000}}, {}, {"USDTIRT": 100000})
        self.assertEqual(out["action"], neutral["action"])

    def test_held_position_is_only_downgraded_to_hold(self):
        layer = self._layer()
        dec = HeuristicAI(self.cfg).decide(
            candidate(), snapshot(), {"risk": {"total_equity": 1000}},
            {"BTCUSDT": {"amount": 1}}, {"USDTIRT": 100000})
        verdict = {"allow_buy": False, "consensus": AVOID, "p_up": 0.1,
                   "blocked_by": ["AVOID:LIQUIDITY_CRITICAL"],
                   "models": [{"model": "liquidity", "veto": True,
                               "verdict": AVOID}]}
        out = layer._committee_apply(dec, verdict, True, candidate(),
                                     snapshot(),
                                     {"risk": {"total_equity": 1000}},
                                     {"BTCUSDT": {"amount": 1}},
                                     {"USDTIRT": 100000})
        self.assertEqual(out["action"], HOLD)


class TestScoreboardTick(TempCase):
    """The engine hook: an approved BUY is recorded, then scored later."""

    def test_approved_buy_is_recorded_and_then_scored(self):
        self.cfg.ai.committee_enabled = True
        layer = AILayer(self.cfg)
        layer.register_verdict("BTCUSDT", {"allow_buy": True, "p_up": 0.8,
                                           "expected_up_pct": 1.0})
        storage = Storage(Path(self.cfg.engine.data_dir))
        out = layer.scoreboard_tick(
            storage, {"BTCUSDT": 101.0},
            [{"market": "BTCUSDT", "side": "BUY", "status": "FILLED",
              "fill_price": 100.0}])
        self.assertEqual(out["attempts"], 1)
        # a later tick scores the open forecast (still inside the horizon)
        out = layer.scoreboard_tick(storage, {"BTCUSDT": 102.0}, [])
        self.assertEqual(out["resolved"], 0)

    def test_blocked_verdict_is_not_recorded(self):
        self.cfg.ai.committee_enabled = True
        layer = AILayer(self.cfg)
        layer.register_verdict("BTCUSDT", {"allow_buy": False,
                                           "p_up": 0.4})
        storage = Storage(Path(self.cfg.engine.data_dir))
        out = layer.scoreboard_tick(
            storage, {"BTCUSDT": 101.0},
            [{"market": "BTCUSDT", "side": "BUY", "status": "FILLED",
              "fill_price": 100.0}])
        self.assertEqual(out.get("attempts", 0), 0)


class TestRationalityGates(TempCase):
    """مقیّم‌های تحلیلی روی گیت‌های خرید: RSI + ترتیب EMA + وتوی پامپ."""

    def _decide(self, **snap_kwargs):
        return HeuristicAI(self.cfg).decide(
            candidate(), snapshot(**snap_kwargs),
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})

    def test_healthy_setup_still_buys(self):
        dec = self._decide()
        self.assertEqual(dec["action"], BUY)
        self.assertTrue((dec["why"].get("checks") or {}).get("rsi"))

    def test_overbought_rsi_is_never_a_buy(self):
        dec = self._decide(rsi=78.0)
        self.assertNotEqual(dec["action"], BUY)
        self.assertIn("RSI_OVERBOUGHT", dec["why"]["gate_notes"])
        self.assertFalse(dec["why"]["checks"]["rsi"])

    def test_falling_knife_rsi_blocks_even_strong_signals(self):
        dec = self._decide(rsi=28.0)
        self.assertNotEqual(dec["action"], BUY)
        self.assertIn("RSI_FALLING_KNIFE", dec["why"]["gate_notes"])

    def test_missing_rsi_does_not_lock_the_buy(self):
        dec = self._decide(rsi=None)
        self.assertEqual(dec["action"], BUY)

    def test_bearish_ema_order_blocks_the_buy(self):
        snap = snapshot()
        snap["technicals"]["ema"] = {"9": 97.0, "20": 101.0, "50": 103.0}
        dec = HeuristicAI(self.cfg).decide(
            candidate(), snap,
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})
        self.assertNotEqual(dec["action"], BUY)
        self.assertIn("EMA_ORDER_BEARISH", dec["why"]["gate_notes"])
        self.assertIn("BEAR", str(dec["why"]["ema_order"]))

    def test_breakout_up_needs_a_bullish_ema_stack(self):
        snap = snapshot()
        snap["technicals"]["ema"] = {"9": 97.0, "20": 101.0, "50": 103.0}
        dec = HeuristicAI(self.cfg).decide(
            candidate(trend=0.35), snap,          # مسیر breakout-up
            {"risk": {"total_equity": 1000}}, {}, {"USDTIRT": 100000})
        self.assertNotEqual(dec["action"], BUY)


class TestCommitteeHardVetoes(TempCase):
    """وتوی پامپِ کم‌حجم + سقف اسپردِ نسبی به نوسان."""

    def test_low_volume_pump_is_blocked_by_default_weights(self):
        c = AICommittee(self.cfg)
        v = c.evaluate({**candidate(), "reasons": ["LOW_VOLUME_PUMP"]},
                       snapshot())
        self.assertFalse(v["allow_buy"])
        self.assertIn("AVOID:LOW_VOLUME_PUMP", v["blocked_by"])
        self.assertTrue(any(
            m["model"] == "pump_guard" and m.get("veto")
            for m in v["models"]))

    def test_momentum_liquidity_mismatch_is_vetoed(self):
        c = AICommittee(self.cfg)
        v = c.evaluate(candidate(mom24=0.95, liquidity=0.2), snapshot())
        self.assertFalse(v["allow_buy"])
        self.assertIn("AVOID:MOM24_LIQUIDITY_MISMATCH", v["blocked_by"])

    def test_spread_cap_relaxes_with_real_volatility(self):
        # بازار پرنوسان (ATR ۴٪): اسپرد ۱٫۸٪ عادی است — وتو نمی‌شود
        v = vote_liquidity(candidate(), snapshot(spread=1.8),
                           ctx(atr_pct=0.04))
        self.assertNotIn("veto", v)
        # بازار مرده (ATR ۰٫۲٪): همان اسپرد سم است
        v = vote_liquidity(candidate(), snapshot(spread=1.8),
                           ctx(atr_pct=0.002))
        self.assertTrue(v.get("veto"))
        self.assertIn("WIDE_SPREAD", v["reason"])


class TestScoreboard(TempCase):
    def test_records_and_scores_the_real_move(self):
        sb = CommitteeScoreboard(self.cfg)
        storage = _Store()
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        sb.record(storage, "BTCUSDT", 100.0,
                  {"p_up": 0.8, "expected_up_pct": 1.5,
                   "expected_net_edge_pct": 0.5}, now=now)
        summary = sb.update(storage, {"BTCUSDT": 100.4},
                            now=now + timedelta(minutes=10))
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["resolved"], 0)
        summary = sb.update(storage, {"BTCUSDT": 102.0},
                            now=now + timedelta(hours=5))
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["win_rate"], 1.0)
        self.assertEqual(summary["target_rate"], 1.0)
        self.assertAlmostEqual(summary["avg_pct"], 2.0, places=3)

    def test_loss_is_scored_as_a_miss(self):
        sb = CommitteeScoreboard(self.cfg)
        storage = _Store()
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        sb.record(storage, "ETHUSDT", 50.0,
                  {"p_up": 0.75, "expected_up_pct": 1.0}, now=now)
        summary = sb.update(storage, {"ETHUSDT": 49.0},
                            now=now + timedelta(hours=5))
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["win_rate"], 0.0)
        self.assertEqual(summary["target_rate"], 0.0)


class TestDecisionPrecision(TempCase):
    """دقت تصمیم: شاهدهای مستقل، گیت‌های کیفی و رأی اکثریت کمیته."""

    def _decide(self, **snap_kwargs):
        return HeuristicAI(self.cfg).decide(
            candidate(), snapshot(**snap_kwargs),
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})

    def test_conviction_is_recorded_with_every_evidence_slice(self):
        dec = self._decide()
        self.assertIn("conviction", dec)
        self.assertGreaterEqual(dec["conviction"], self.cfg.ai.min_conviction)
        ev = dec["why"]["evidence"]
        for key in ("trend", "momentum", "volume", "location",
                    "execution", "data"):
            self.assertIn(key, ev)
            self.assertGreaterEqual(ev[key], 0.0)
            self.assertLessEqual(ev[key], 1.0)

    def test_dry_volume_blocks_the_buy(self):
        snap = snapshot()
        snap["technicals"]["volume_ratio"] = 0.3
        dec = HeuristicAI(self.cfg).decide(
            candidate(), snap,
            {"risk": {"total_equity": 1000},
             "assets": [{"asset": "USDT", "available": 5000}]},
            {}, {"USDTIRT": 100000})
        self.assertNotEqual(dec["action"], "BUY")
        self.assertIn("VOLUME_DRY", dec["why"]["gate_notes"])
        self.assertFalse(dec["why"]["checks"]["volume"])

    def test_negative_macd_blocks_the_buy(self):
        dec = self._decide(macd=-1.0)
        self.assertNotEqual(dec["action"], "BUY")
        self.assertIn("MACD_BEARISH", dec["why"]["gate_notes"])

    def test_buying_at_the_top_of_an_extension_is_blocked(self):
        dec = self._decide(e20=92.0, atr_pct=0.02)
        self.assertNotEqual(dec["action"], "BUY")
        self.assertIn("CHASING_EXTENSION", dec["why"]["gate_notes"])
        self.assertFalse(dec["why"]["checks"]["not_chasing"])

    def test_volume_model_abstains_without_data_and_votes_with_it(self):
        self.assertEqual(vote_volume(candidate(), snapshot(), ctx())["verdict"],
                         ABSTAIN)
        snap = snapshot()
        snap["technicals"]["volume_ratio"] = 2.2
        v = vote_volume(candidate(), snap, ctx())
        self.assertEqual(v["verdict"], BUY)
        self.assertGreater(v["expected_move_pct"], 0)

    def test_parabolic_extension_is_vetoed(self):
        v = vote_extension(candidate(), snapshot(e20=85.0, atr_pct=0.02),
                           ctx(atr_pct=0.02))
        self.assertTrue(v.get("veto"))
        self.assertEqual(v["verdict"], AVOID)
        self.assertIn("PARABOLIC", v["reason"])
        self.assertNotIn("veto", vote_extension(candidate(), snapshot(), ctx()))

    def test_buy_needs_a_majority_not_just_the_floor(self):
        votes = [vote(BUY, 0.8, 4.0, 1.0, model="a"),
                 vote(BUY, 0.8, 4.0, 1.0, model="b")]
        votes += [vote(HOLD, 0.5, 0.0, 1.0, model=m) for m in ("c", "d", "e")]
        v = combine(votes, cost_pct=0.6, atr_pct=0.04, min_votes=2)
        self.assertFalse(v["allow_buy"])
        self.assertIn("NO_QUORUM(2/3)", v["blocked_by"])

    def test_a_split_committee_is_rejected(self):
        votes = [vote(BUY, 0.9, 3.0, 1.0, model=m) for m in "abc"]
        votes += [vote(HOLD, 0.2, 0.0, 1.0, model=m) for m in ("d", "e", "f")]
        v = combine(votes, cost_pct=0.6, atr_pct=0.04, min_votes=3,
                    min_agreement=0.6)
        self.assertFalse(v["allow_buy"])
        self.assertIn("MODELS_DISAGREE", v["blocked_by"])
        self.assertLess(v["agreement"], 0.6)


if __name__ == "__main__":
    unittest.main()
