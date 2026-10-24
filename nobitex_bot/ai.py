"""AI layer — contract sections 29, 30, 31, 32, 69-74.

Two engines:
- heuristic (default): deterministic scorer over features/candidates. No LLM.
- llm (optional): OpenAI-compatible endpoint; builds a compressed SS29
  context, requests JSON, then validates the output exactly like the
  heuristic output (SS73) — invalid output -> NO_TRADE (SS74).

The AI never executes: it only recommends (SS32, SS102).
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from . import tls
from .ai_committee import AICommittee, CommitteeScoreboard
from .config import Config
from .utils import new_id, sha256_json, utc_iso, utc_now_iso


def d(x) -> Decimal:
    """Decimal coercion helper (lenient, never raises)."""
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(0)

PROMPT_VERSION = "1.0"


class HeuristicAI:
    """Deterministic decision layer — same inputs always give same output."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def decide(self, candidate: dict, market_snap: dict,
               portfolio_snapshot: dict, open_positions: dict,
               prices: dict | None = None,
               topup_allowed: bool = False) -> dict:
        sym = candidate["symbol"]
        signals = candidate["signals"]
        score = candidate["score"]
        liq = market_snap.get("liquidity", {})
        liq_status = liq.get("status", "UNKNOWN")
        vol = market_snap.get("volatility", {})
        regime = vol.get("volatility_regime", "UNKNOWN")
        structure = market_snap.get("structure", {})
        trend = structure.get("trend", "UNKNOWN")
        breakout = structure.get("breakout", {})
        vol_suit = signals.get("volatility", 0)

        # --- action. Default: no pyramiding (a held coin is HOLD, not BUY).
        # With risk.allow_topup the AI may re-BUY its best-ranked held coin
        # so the bot can ADD free cash to a winning position — the risk
        # engine still caps size/cash (SS33/34).
        held = float(open_positions.get(sym, {}).get("amount", 0) or 0) > 0
        allow_topup = bool(topup_allowed and held)
        action = "HOLD"
        if liq_status in ("CRITICAL", "UNKNOWN") or vol_suit < 0.25:
            action = "AVOID"
        elif (signals.get("trend", 0) > 0.45 and
              signals.get("momentum", 0) > 0.4 and
              signals.get("liquidity", 0) > 0.35):
            action = "BUY" if (not held or allow_topup) else "HOLD"
        elif (breakout.get("detected") and
              breakout.get("direction") == "UP" and
              signals.get("liquidity", 0) > 0.35 and not held):
            action = "BUY"
        elif held:
            if (signals.get("trend", 1) < 0.45 and
                    signals.get("momentum", 1) < 0.5):
                action = "SELL"
        elif breakout.get("detected") and breakout.get("direction") == "DOWN":
            action = "AVOID"

        # --- confidence
        conf = 0.5
        if action == "BUY":
            conf = 0.55 + 0.4 * score
        elif action == "SELL":
            conf = 0.55 + 0.3 * (1.0 - score)
        elif action == "AVOID":
            conf = 0.7
        dq = market_snap.get("data_quality", {}).get("score", 0)
        conf *= (0.5 + 0.5 * dq)
        conf = round(max(0.0, min(0.97, conf)), 4)

        # --- exit (SS42: every decision carries created_at/expires_at TTL)
        now_dt = datetime.now(timezone.utc)
        now_iso = utc_iso(now_dt)
        expires_iso = utc_iso(now_dt + timedelta(
            seconds=self.cfg.risk.decision_ttl_s))
        decision = {
            "decision_id": "dec_%s_%s" % (
                utc_now_iso().replace("-", "").replace(":", "")[:15], new_id("x")[2:10]),
            "snapshot_id": market_snap.get("snapshot_id", ""),
            "cycle_id": market_snap.get("cycle_id", ""),
            "symbol": sym,
            "action": action,
            "confidence": conf,
            "time_horizon": "INTRADAY",
            "reason_codes": ([r for r in candidate.get("reasons", [])][:5]
                             + (["REGIME_" + candidate["regime"]]
                                if candidate.get("regime") else [])),
            "created_at": now_iso,
            "expires_at": expires_iso,
        }

        if action in ("BUY", "SELL"):
            last = market_snap.get("last_price") or 0
            atr = (market_snap.get("volatility", {}) or {}).get("atr")
            if not atr or atr <= 0:
                atr = last * 0.012
            # --- realistic percentage-based entry/stop (configurable):
            # BUY entries rest entry_pullback_pct BELOW the last price so
            # the limit order is actually reachable (the touch pays spread
            # + slippage); stops are at least min_stop_distance_pct away so
            # spread/noise doesn't trigger them instantly.
            entry_pct = float(getattr(self.cfg.ai,
                                      "entry_pullback_pct", 0.0025) or 0)
            # حالت ریسک: ورود فوری روی پرسودترین — سفارش در همان قیمت
            # لحظه‌ای می‌نشیند (pullback منفی یعنی جا ماندن از موج روز)
            if getattr(self.cfg.risk, "risk_mode", False):
                entry_pct = float(getattr(
                    self.cfg.risk, "risk_mode_chase_pct", 0.0) or 0)
            stop_pct = float(getattr(self.cfg.risk,
                                     "min_stop_distance_pct", 0.012) or 0.012)
            if action == "BUY":
                entry = last * (1 - entry_pct)
                stop = min(last - 1.5 * atr, entry * (1 - stop_pct))
                t1, t2, t3 = (entry + 2 * atr, entry + 3.2 * atr,
                              entry + 4.5 * atr)
                method = "ATR_SUPPORT"
                reason = "ATR_SUPPORT"
            else:
                entry = last * (1 + entry_pct)
                stop = max(last + 1.5 * atr, entry * (1 + stop_pct))
                t1, t2, t3 = (entry - 2 * atr, entry - 3.2 * atr,
                              entry - 4.5 * atr)
                method = "ATR_RESISTANCE"
                reason = "ATR_RESISTANCE"
            price = entry

            risk_frac = self.cfg.risk.max_risk_per_trade
            equity = portfolio_snapshot.get("risk", {}).get(
                "total_equity", 0.0) or 0.0
            # sizing floor so tiny/small accounts still produce tradable
            # sizes (the risk engine re-clamps to real cash anyway)
            if equity <= 0:
                equity = 1000.0
            # the market trades in its quote asset: convert the USDT risk
            # budget into quote units so the size is real money (SS35).
            # IRT-market prices are TOMAN (adapter-normalized) and USDTIRT
            # reports Toman per USDT, so the conversion is direct.
            quote_asset = sym[-4:] if sym.endswith("USDT") else "IRT"
            conv = 1.0   # quote units per USDT
            if quote_asset == "IRT":
                usdtirt = float((prices or {}).get("USDTIRT") or 0)
                conv = usdtirt if usdtirt > 0 else 0.0
            max_loss = equity * risk_frac * conv
            stop_dist = abs(entry - stop)
            amount = (max_loss / stop_dist) if stop_dist > 0 else 0.0
            max_amount = (equity * self.cfg.risk.max_position_fraction
                          * conv) / price
            amount = min(amount, max_amount)
            # cash cap: never propose more than the free quote balance
            assets = {a.get("asset"): float(a.get("available") or 0)
                      for a in (portfolio_snapshot.get("assets") or [])}
            cash = assets.get(quote_asset, 0.0)
            if action == "SELL":
                # sell the position we actually hold (risk engine clamps
                # to holdings again) — never the dust-cash fallback
                held = d(str(open_positions.get(sym, {}).get(
                    "amount", 0) or 0))
                amount = float(held) if held > 0 else 0.0
            elif cash > 0:
                amount = min(amount, cash * 0.95 / price)
            if amount <= 0 and action != "SELL":
                amount = ((equity * self.cfg.risk.max_position_fraction
                           * conv) * 0.05) / price
            quote_value = amount * price

            decision["entry"] = {
                "price": round(entry, 8),
                "min": round(entry * 0.998, 8),
                "max": round(entry * 1.002, 8),
            }
            decision["stop_loss"] = {
                "price": round(stop, 8),
                "method": method,
                "reason": reason,
                "atr": round(atr, 8),
                "distance_percent": round(abs(entry - stop) / entry, 6),
            }
            decision["take_profit"] = [
                {"price": round(t1, 8), "close_percent": 50},
                {"price": round(t2, 8), "close_percent": 30},
                {"price": round(t3, 8), "close_percent": 20},
            ]
            decision["position_size"] = {
                "amount": round(amount, 8),
                "quote_value": round(quote_value, 2),
            }
            rr = abs(t1 - entry) / stop_dist if stop_dist else 0
            decision["risk"] = {
                "max_loss": round(max_loss, 2),
                "risk_reward": round(rr, 2),
            }
            fees = (self.cfg.fee.taker_rate * 2)
            costs = fees + (liq.get("estimated_buy_slippage", 0.001) or 0.001)
            # Expected gross return of the WHOLE plan, not just TP1. The
            # ladder closes 50% at +2×ATR, 30% at +3.2×ATR and 20% at
            # +4.5×ATR, so the position's expected exit is the weighted
            # distance (2.86×ATR). Using only TP1 (2×ATR) understated it:
            # combined with the honest 0.6% net floor in risk.py it rejected
            # EVERY buy in quiet markets, which is why the bot stopped
            # trading after the fee-aware gate landed.
            expected = (abs(t1 - entry) * 0.5 + abs(t2 - entry) * 0.3
                        + abs(t3 - entry) * 0.2) / entry
            decision["cost"] = {
                "estimated_total_cost": round(costs, 6),
                "expected_gross_return": round(expected, 6),
                # SS28 net-expected test is the honest edge check: gross
                # MINUS (fees both ways + slippage). The old 0.003 floor
                # faked a positive edge on trades whose real expectation
                # was below cost — a slow bleed at 0.25%+0.25% per
                # round-trip. If the math says negative, risk must see it
                # negative (risk.py rejects < min_net_expected_return).
                "expected_net_return": round(expected - costs, 6),
            }
            decision["invalidation_conditions"] = [
                "BREAK_SUPPORT", "SPREAD_EXPANSION", "LIQUIDITY_COLLAPSE"]

        return decision


class LLMClient:
    """Optional OpenAI-compatible chat client. Fails soft to heuristic."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def available(self) -> bool:
        return bool(self.cfg.ai.llm_base_url and self.cfg.ai.llm_model)

    def ready(self) -> bool:
        """Endpoint configured — enough for the committee's multiple models
        (a local OpenAI-compatible server needs no key)."""
        return bool(self.cfg.ai.llm_base_url)

    def complete_json(self, system: str, user: str,
                      model: str | None = None) -> Optional[dict]:
        if not self.cfg.ai.llm_base_url:
            return None
        model = model or self.cfg.ai.llm_model
        if not model:
            return None
        url = self.cfg.ai.llm_base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": self.cfg.ai.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + (self.cfg.ai.llm_api_key or "")})
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=tls.context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception:
            return None


def build_ai_context(candidate: dict, market_snap: dict,
                     portfolio_snapshot: dict, cfg: Config) -> dict:
    """SS29 compressed context — never raw orderbooks or thousands of trades."""
    tech = market_snap.get("technicals", {})
    return {
        "context_version": "1.0",
        "snapshot_id": market_snap.get("snapshot_id", ""),
        "cycle_id": market_snap.get("cycle_id", ""),
        "account_summary": {
            "equity_usdt": portfolio_snapshot.get("risk", {}).get("total_equity"),
            "cash_ratio": portfolio_snapshot.get("risk", {}).get("cash_ratio"),
            "risk_status": portfolio_snapshot.get("risk", {}).get("risk_status"),
        },
        "market_candidates": [candidate],
        "portfolio_risk": portfolio_snapshot.get("risk", {}),
        "active_orders": [],
        "recent_decisions": [],
        "historical_summary": {},
        "global_market_context": {},
        "system_constraints": {
            "max_risk_per_trade": cfg.risk.max_risk_per_trade,
            "max_position_fraction": cfg.risk.max_position_fraction,
            "max_total_exposure": cfg.risk.max_total_exposure,
            "min_net_expected_return": cfg.risk.min_net_expected_return,
            "min_confidence": cfg.ai.min_confidence,
            "fee_taker": cfg.fee.taker_rate,
            "allowed_actions": ["BUY", "SELL", "HOLD", "AVOID"],
        },
        "market_state": {
            "symbol": market_snap.get("symbol"),
            "last_price": market_snap.get("last_price"),
            "spread_percent": market_snap.get("spread_percent"),
            "change_24h": market_snap.get("change_24h"),
            "liquidity": {k: market_snap.get("liquidity", {}).get(k)
                          for k in ("score", "status")},
            "volatility": {k: market_snap.get("volatility", {}).get(k)
                           for k in ("atr", "atr_percent", "volatility_regime")},
            "technicals": {
                "rsi": tech.get("rsi"),
                "macd_histogram": (tech.get("macd") or {}).get("histogram"),
                "ema": {k: (tech.get("ema") or {}).get(k) for k in ("9", "20", "50")},
            },
            "structure": market_snap.get("structure"),
            "data_quality": market_snap.get("data_quality", {}).get("score"),
        },
    }


def validate_llm_output(raw: dict, candidate: dict, market_snap: dict,
                        cfg: Config) -> Optional[dict]:
    """SS73/74 — LLM output must be a strict subset of the contract; else None
    (which maps to NO_TRADE)."""
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if action not in ("BUY", "SELL", "HOLD", "AVOID"):
        return None
    if raw.get("symbol") != market_snap.get("symbol"):
        return None
    conf = raw.get("confidence")
    if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
        return None
    heuristic = HeuristicAI(cfg)
    decision = heuristic.decide(candidate, market_snap, {}, {})
    decision["action"] = action
    decision["confidence"] = round(float(conf), 4)
    decision["reason_codes"] = [
        str(x)[:40] for x in (raw.get("reason_codes") or [])][:8]
    if action in ("BUY", "SELL"):
        if "entry" not in decision:
            return None
    return decision


class AILayer:
    """Dispatches to heuristic or LLM engine; produces SS31 decisions."""

    def __init__(self, cfg: Config, events=None):
        self.cfg = cfg
        self.events = events
        self.heuristic = HeuristicAI(cfg)
        self.llm = LLMClient(cfg)
        # کمیتهٔ چندمدلی (ai_committee.py): رأی مدل‌های مستقل + گیتِ ارزش
        # انتظاری مثبت. verdict هر نماد برای ثبت در دفترچهٔ پیش‌بینی می‌ماند.
        self.committee = AICommittee(cfg, llm=self.llm)
        self.scoreboard = CommitteeScoreboard(cfg)
        self._committee_verdicts: dict = {}

    # --------------------------------------------------- committee (SS32)
    def _committee_plan(self, cand: dict, snap: dict,
                        portfolio_snapshot: dict, open_positions: dict,
                        prices: dict | None) -> Optional[dict]:
        """کامل‌ترین نقشهٔ ورود (entry/stop/TP/size) از موتور تکنیکال.

        «decide» mode: تصمیم BUY با کمیته است ولی نقشهٔ اجرا باید کامل
        باشد؛ HeuristicAI با سیگنال‌های تأییدشده همان نقشه را با اندازه و
        حد ضرر/سود ATR-محور می‌سازد (همان کاری که «خرید بهترین گزینه»
        می‌کند). موتور ریسک بعد از این دوباره اندازه/نقدینگی را می‌بندد.
        """
        boosted = {**cand, "signals": {
            **cand.get("signals", {}),
            "trend": max(cand.get("signals", {}).get("trend", 0), 0.9),
            "momentum": max(cand.get("signals", {}).get("momentum", 0), 0.9),
        }}
        plan = self.heuristic.decide(boosted, snap, portfolio_snapshot,
                                     open_positions, prices)
        if (plan.get("action") not in ("BUY", "SELL") or
                not (plan.get("entry") or {}).get("price")):
            return None
        return plan

    def _committee_apply(self, dec: dict, verdict: dict, held: bool,
                         cand: dict, snap: dict,
                         portfolio_snapshot: dict, open_positions: dict,
                         prices: dict | None) -> dict:
        """اعمال حکم کمیته روی تصمیم موتور.

        - gate   : کمیته فقط می‌تواند BUY موتور را رد کند.
        - decide : اگر کمیته BUY بگوید و موتور مردد باشد، تصمیم BUY از
                   کمیته می‌آید و نقشهٔ ورود کامل از موتور.
        در هر دو حالت، بدون تأیید کمیته هیچ BUY تازه‌ای صادر نمی‌شود
        (اگر بستهٔ بدون ورود باشد AVOID، برای سکهٔ نگه‌داری‌شده HOLD).
        """
        if not verdict.get("models"):
            return dec
        allow = bool(verdict.get("allow_buy"))
        action = dec.get("action")
        reasons = list(dec.get("reason_codes") or [])

        if allow and self.committee.decide_mode() and action not in ("BUY",
                                                                    "SELL"):
            if held:
                return dec        # افزودن به سکهٔ موجود تصمیم جداست (topup)
            plan = self._committee_plan(cand, snap, portfolio_snapshot,
                                        open_positions, prices)
            if plan is None:
                return dec
            for k in ("entry", "stop_loss", "take_profit", "position_size",
                      "risk", "cost", "invalidation_conditions"):
                if k in plan:
                    dec[k] = plan[k]
            dec["action"] = "BUY"
            dec["confidence"] = round(max(float(verdict.get("p_up") or 0),
                                          float(dec.get("confidence") or 0)),
                                      4)
            dec["reason_codes"] = (["COMMITTEE_BUY"] + reasons)[:8]
            return dec

        if action == "BUY" and not allow:
            blocked = verdict.get("blocked_by") or []
            dec["action"] = "HOLD" if held else (
                "AVOID" if verdict.get("consensus") == "AVOID" else "HOLD")
            dec["confidence"] = round(min(float(dec.get("confidence") or 0),
                                          max(float(verdict.get("p_up") or 0),
                                              0.3)), 4)
            dec["reason_codes"] = ([("COMMITTEE_BLOCK:" + str(blocked[0]))
                                    if blocked else "COMMITTEE_BLOCK"]
                                   + reasons)[:8]
            for k in ("entry", "stop_loss", "take_profit", "position_size",
                      "risk", "cost", "invalidation_conditions"):
                dec.pop(k, None)
        return dec

    def register_verdict(self, symbol: str, verdict: dict) -> None:
        """حکم کمیته را برای تصمیمی که بیرون از analyze() ساخته شده نگه می‌دارد
        (مثلاً «خرید بهترین گزینه») تا دفترچهٔ پیش‌بینی آن را هم ثبت کند."""
        if symbol and verdict:
            self._committee_verdicts[symbol] = verdict

    def scoreboard_tick(self, storage, prices: dict,
                        executions: list | None = None) -> dict:
        """هر BUY تأییدشده را با پیش‌بینی‌اش ثبت و بعد از افق ارزیابی کن."""
        if not self.committee.enabled():
            return {}
        for ex in executions or []:
            if str(ex.get("side") or "").upper() != "BUY":
                continue
            if str(ex.get("status") or "").upper() not in ("FILLED",
                                                            "SUBMITTED"):
                continue
            sym = ex.get("market") or ex.get("symbol")
            verdict = self._committee_verdicts.get(sym)
            if not verdict or not verdict.get("allow_buy"):
                continue
            price = ex.get("fill_price") or ex.get("price") or 0
            try:
                self.scoreboard.record(storage, sym, float(price), verdict)
            except Exception:
                pass
        try:
            return self.scoreboard.update(storage, prices)
        except Exception:
            return {}

    def analyze(self, candidates: list, market_by_symbol: dict,
                portfolio_snapshot: dict, open_positions: dict,
                cycle_id: str, prices: dict | None = None,
                topup_allowed: bool = False) -> list:
        decisions = []
        use_llm = self.cfg.ai.engine == "llm" and self.llm.available()
        # LLM voters cost one request per model per candidate — only the
        # best few candidates get them (ai.committee_llm_top_n)
        llm_top_n = int(getattr(self.cfg.ai, "committee_llm_top_n", 2) or 0)
        for idx, cand in enumerate(candidates):
            sym = cand["symbol"]
            snap = market_by_symbol.get(sym)
            if not snap:
                continue
            if use_llm:
                ctx = build_ai_context(cand, snap, portfolio_snapshot, self.cfg)
                ctx_hash = sha256_json(ctx)
                system = (
                    "You are a trading decision module. Respond ONLY with a JSON "
                    "object matching the AI Decision schema: action in "
                    "BUY|SELL|HOLD|AVOID, confidence 0..1, symbol must match input. "
                    "You may NOT bypass risk limits. If uncertain, choose AVOID.")
                user = json.dumps(ctx, default=str)
                raw = self.llm.complete_json(system, user)
                dec = (validate_llm_output(raw, cand, snap, self.cfg)
                       if raw else None)
                if dec is None:
                    dec = self.heuristic.decide(
                        cand, snap, portfolio_snapshot, open_positions, prices,
                        topup_allowed=topup_allowed)
                    dec["reason_codes"] = ["LLM_OUTPUT_INVALID", "FALLBACK_HEURISTIC"]
                dec["prompt_version"] = PROMPT_VERSION
                dec["context_hash"] = ctx_hash
            else:
                dec = self.heuristic.decide(
                    cand, snap, portfolio_snapshot, open_positions, prices,
                    topup_allowed=topup_allowed)
            # کمیتهٔ چندمدلی: رأی همهٔ مدل‌ها + گیت ارزش انتظاری مثبت
            if self.committee.enabled():
                try:
                    verdict = self.committee.evaluate(
                        cand, snap, allow_llm=(idx < llm_top_n))
                except Exception:
                    verdict = {}
                if verdict:
                    self._committee_verdicts[sym] = verdict
                    held = float((open_positions.get(sym) or {}).get(
                        "amount", 0) or 0) > 0
                    dec = self._committee_apply(
                        dec, verdict, held, cand, snap, portfolio_snapshot,
                        open_positions, prices)
                    dec["committee"] = verdict
            dec["cycle_id"] = cycle_id
            decisions.append(dec)
        return decisions
