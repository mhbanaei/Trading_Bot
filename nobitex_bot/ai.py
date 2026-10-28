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
        dq = market_snap.get("data_quality", {}).get("score", 0)
        tech = market_snap.get("technicals", {}) or {}
        rsi = tech.get("rsi")
        spread_pct = market_snap.get("spread_percent")
        change_24h = market_snap.get("change_24h")
        macd_hist = (tech.get("macd") or {}).get("histogram")
        ema = tech.get("ema") or {}

        def rf(v):
            """Lenient float — bad exchange data must never raise."""
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        rsi_v = rf(rsi)
        # --- RSI gate (تحلیل مکمل روی ستون گیت‌های قبلی): خرید وقتی RSI
        # بالای ۷۲ است یعنی «خرید سقف» — قیمت تازه بمب افته و پول شرکتی
        # در حال خارج‌شدن است؛ و خرید در RSI زیر ۳۵ یعنی چاقوی در حال
        # سقوط — مومنتوم منفی است و هیچ سیگنالی نتوانسته قیمت را نگه کند.
        # دادهٔ غایب گیت را قفل نمی‌کند (None -> pass).
        RSI_OVERBOUGHT = 72.0
        RSI_knife = 35.0
        if rsi_v is None:
            rsi_ok, rsi_code = True, ""
        elif rsi_v >= RSI_OVERBOUGHT:
            rsi_ok, rsi_code = False, "RSI_OVERBOUGHT"
        elif rsi_v <= RSI_knife:
            rsi_ok, rsi_code = False, "RSI_FALLING_KNIFE"
        else:
            rsi_ok, rsi_code = True, ""
        # --- ساختار کل: شکست صعودی وقتی EMA ترتیب نزولی دارد فقط یک برق
        # از سرِ فشار فروش است؛ بدون ترتیب EMA صعودی، breakout_up به‌عنوان
        # قبل خرید نیست (تحلیلی ضد چترِ «شبیه‌سازی خرید ارزانه»).
        e9, e20, e50 = rf(ema.get("9")), rf(ema.get("20")), rf(ema.get("50"))
        ema_bull = bool(e9 and e20 and e50 and e9 > e20 > e50)
        ema_bear = bool(e9 and e20 and e50 and e9 < e20 < e50)

        # --- «درجهٔ اطمینان» (conviction): شاهدهای مستقل، نه یک امتیاز -----
        # اسکنر یک عدد ترکیبی می‌دهد؛ تصمیم دقیق یعنی هر شاهد مستقل (روند،
        # مومنتوم/مکدی، حجم، موقعیت نسبت به EMA20، کیفیت اجرا و تازگی داده)
        # جدا وزن شود و خرید فقط وقتی انجام شود که مجموع از آستانه بگذرد.
        # دادهٔ غایب هر شاهد را خنثی (۰٫۵) می‌کند، پس نبود یک فیلد هیچ‌وقت
        # خرید سالم را قفل نمی‌کند.
        risk_mode_on = bool(getattr(self.cfg.risk, "risk_mode", False))
        vr = rf(tech.get("volume_ratio"))
        roc = rf(tech.get("roc"))
        last_px = rf(market_snap.get("last_price")) or 0.0
        atr_abs = rf((vol or {}).get("atr")) or 0.0
        atr_frac = ((atr_abs / last_px) if (last_px > 0 and atr_abs > 0)
                    else (rf((vol or {}).get("atr_percent")) or 0.012))
        # فاصلهٔ قیمت از EMA20 بر حسب ATR — «چقدر از حمایت دور افتاده»
        ext_atr = ((last_px / e20 - 1.0) / atr_frac
                   if (e20 and e20 > 0 and last_px > 0 and atr_frac > 0)
                   else None)

        def _cl(v, lo=0.0, hi=1.0):
            return max(lo, min(hi, v))

        _struct = str(trend).upper()
        ev_trend = _cl(
            0.55 * float(signals.get("trend", 0) or 0)
            + 0.25 * (1.0 if ema_bull else 0.15 if ema_bear else 0.5)
            + 0.20 * (1.0 if _struct in ("UP", "UPTREND", "BULLISH")
                      else 0.0 if _struct in ("DOWN", "DOWNTREND", "BEARISH")
                      else 0.5))
        macd_ev = 0.5 if macd_hist is None else (1.0 if macd_hist > 0 else 0.0)
        roc_ev = 0.5 if roc is None else _cl(0.5 + roc / 10.0)
        ev_mom = _cl(0.50 * float(signals.get("momentum", 0) or 0)
                     + 0.25 * macd_ev + 0.25 * roc_ev)
        if risk_mode_on:
            # حالت ریسک عامدانه دنبال پرسودترین‌های ۲۴ساعته است
            ev_mom = _cl(0.6 * ev_mom + 0.4 * float(
                signals.get("momentum_24h", 0) or 0))
        ev_part = 0.5 if vr is None else _cl((float(vr) - 0.4) / 1.2)
        if rsi_v is None:
            rsi_ev = 0.5
        elif rsi_v >= RSI_OVERBOUGHT:
            rsi_ev = 0.10
        elif rsi_v >= 65:
            rsi_ev = 0.42
        elif rsi_v >= 45:
            rsi_ev = 0.85
        elif rsi_v >= RSI_knife:
            rsi_ev = 0.48
        else:
            rsi_ev = 0.15
        if ext_atr is None:
            pos_ev = 0.5
        elif ext_atr > 4.0:
            pos_ev = 0.10
        elif ext_atr > 2.0:
            pos_ev = 0.35
        elif ext_atr > 0.6:
            pos_ev = 0.62
        elif ext_atr >= -1.5:
            pos_ev = 0.90
        else:
            pos_ev = 0.35      # زیر حمایت: چاقوی در حال سقوط
        ev_loc = _cl(0.5 * rsi_ev + 0.5 * pos_ev)
        spread_atr = (rf(spread_pct) / (atr_frac * 100.0)
                      if (spread_pct is not None and atr_frac > 0) else None)
        if spread_atr is None:
            spr_ev = 0.5
        elif spread_atr <= 0.15:
            spr_ev = 1.0
        elif spread_atr <= 0.40:
            spr_ev = 0.70
        elif spread_atr <= 0.75:
            spr_ev = 0.40
        else:
            spr_ev = 0.10
        ev_exec = _cl(0.6 * float(signals.get("liquidity", 0) or 0)
                      + 0.4 * spr_ev)
        ev_data = _cl(float(dq or 0))
        conviction = round(_cl(0.26 * ev_trend + 0.20 * ev_mom
                               + 0.12 * ev_part + 0.17 * ev_loc
                               + 0.15 * ev_exec + 0.10 * ev_data), 4)
        evidence = {"trend": round(ev_trend, 3),
                    "momentum": round(ev_mom, 3),
                    "volume": round(ev_part, 3),
                    "location": round(ev_loc, 3),
                    "execution": round(ev_exec, 3),
                    "data": round(ev_data, 3)}
        # آستانهٔ اطمینان: در حالت ریسک خاموش است (آن حالت خودش اولویت‌های
        # جدا دارد) و در حالت عادی خریدِ «نه‌ضعیف نه‌قوی» رد می‌شود.
        min_conv = float(getattr(self.cfg.ai, "min_conviction", 0.60) or 0.0)
        conv_ok = risk_mode_on or conviction >= min_conv
        # سه چک کیفیِ دیگر — هر کدام با دادهٔ غایب «عبور» است
        part_ok = (vr is None or float(vr) >= 0.55)
        macd_ok = (macd_hist is None or float(macd_hist) >= 0)
        chase_ok = bool(risk_mode_on or ext_atr is None
                        or (ext_atr <= 3.0 and (last_px / e20 - 1.0) <= 0.06))

        # --- rationality gates (every BUY must pass the WHOLE stack):
        # trend + momentum + liquidity + data fresh enough + enough
        # volatility + RSI در محدودهٔ منطقی + EMA ترتیب راست + حجم/مکدی +
        # «دور از سقف» + درجهٔ اطمینان
        # A lone upward spike (breakout) is only purchasable WITH trend
        # confirmation — counter-trend chasing a spike was exactly the
        # least rational trade this layer used to make.
        DQ_FLOOR = 0.45
        trend_ok = signals.get("trend", 0) > 0.45
        mom_ok = signals.get("momentum", 0) > 0.4
        liq_ok = signals.get("liquidity", 0) > 0.35
        data_ok = float(dq) >= DQ_FLOOR
        vol_ok = vol_suit >= 0.30
        breakout_down = (breakout.get("detected") and
                         breakout.get("direction") == "DOWN")
        breakout_up = (breakout.get("detected") and
                       breakout.get("direction") == "UP")
        action = "HOLD"
        if liq_status in ("CRITICAL", "UNKNOWN") or vol_suit < 0.25:
            action = "AVOID"
        elif breakout_down and not held:
            # a confirmed DOWN breakout is never a BUY setup: same market
            # signal would have to be ANTI-trend to buy there
            action = "AVOID"
        elif (trend_ok and mom_ok and liq_ok and data_ok and vol_ok
              and rsi_ok and not ema_bear and macd_ok and part_ok
              and chase_ok and conv_ok):
            action = "BUY" if (not held or allow_topup) else "HOLD"
        elif (breakout_up and signals.get("trend", 0) > 0.30 and
              liq_ok and data_ok and vol_ok and rsi_ok
              and ema_bull and not held and macd_ok and part_ok
              and chase_ok and conv_ok):
            action = "BUY"
        elif held:
            if (signals.get("trend", 1) < 0.45 and
                    signals.get("momentum", 1) < 0.5):
                action = "SELL"
        # explanation for the dashboard: exactly which gate decided.
        # HOLD while the price stack looks BUY-ish but data/volatility
        # fails is THE case users could never see before — now the card
        # says WHY (e.g. «کیفیت داده ۰٫۳۸ از ۰٫۴۵ لازم»).
        gate_notes = []
        if trend_ok and mom_ok and not data_ok:
            gate_notes.append("DATA_QUALITY_LOW")
        if trend_ok and mom_ok and not liq_ok:
            gate_notes.append("LIQUIDITY_GATE")
        if trend_ok and mom_ok and liq_ok and data_ok and not vol_ok:
            gate_notes.append("VOLATILITY_TOO_LOW")
        if (trend_ok and mom_ok and liq_ok and data_ok and vol_ok
                and not rsi_ok):
            gate_notes.append(rsi_code)
        if (trend_ok and mom_ok and liq_ok and data_ok and vol_ok
                and rsi_ok and ema_bear):
            gate_notes.append("EMA_ORDER_BEARISH")
        if (trend_ok and mom_ok and liq_ok and data_ok and vol_ok
                and rsi_ok and not ema_bear and not macd_ok):
            gate_notes.append("MACD_BEARISH")
        if (trend_ok and mom_ok and liq_ok and data_ok and vol_ok
                and rsi_ok and not ema_bear and macd_ok and not part_ok):
            gate_notes.append("VOLUME_DRY")
        if (trend_ok and mom_ok and liq_ok and data_ok and vol_ok
                and rsi_ok and not ema_bear and macd_ok and part_ok
                and not chase_ok):
            gate_notes.append("CHASING_EXTENSION")
        if (trend_ok and mom_ok and liq_ok and data_ok and vol_ok
                and rsi_ok and not ema_bear and macd_ok and part_ok
                and chase_ok and not conv_ok):
            gate_notes.append("LOW_CONVICTION")
        if breakout_up and not trend_ok and not held:
            gate_notes.append("BREAKOUT_NO_TREND")

        def r3(v):
            try:
                return round(float(v), 3)
            except (TypeError, ValueError):
                return None

        why = {
            "trend": r3(signals.get("trend", 0)),
            "momentum": r3(signals.get("momentum", 0)),
            "momentum_24h": r3(signals.get("momentum_24h", 0)),
            "orderbook": r3(signals.get("orderbook", 0)),
            "risk": r3(signals.get("risk", 0)),
            "liquidity": r3(signals.get("liquidity", 0)),
            "volatility": r3(vol_suit),
            "data_quality": r3(dq),
            "trend_label": str(trend),
            "regime": str(regime),
            "rsi": r3(rsi) if rsi is not None else None,
            "macd_histogram": r3(macd_hist) if macd_hist is not None else None,
            "spread_percent": r3(spread_pct) if spread_pct is not None else None,
            "change_24h": r3(change_24h) if change_24h is not None else None,
            "volume_ratio": r3(vr) if vr is not None else None,
            "roc": r3(roc) if roc is not None else None,
            "extension_atr": r3(ext_atr) if ext_atr is not None else None,
            # درجهٔ اطمینان کلی + سهم هر شاهد مستقل (۰..۱)
            "conviction": conviction,
            "evidence": evidence,
            "ema_order": ("BULL" if ema_bull else
                          "BEAR" if ema_bear else "NEUTRAL"),
            "breakout": {"detected": bool(breakout.get("detected")),
                         "direction": str(breakout.get("direction") or "")
                                     or "NONE"},
            "gates": {"trend": 0.45, "momentum": 0.40, "liquidity": 0.35,
                      "data_quality": DQ_FLOOR, "volatility": 0.30,
                      "rsi_overbought": RSI_OVERBOUGHT,
                      "rsi_falling_knife": RSI_knife,
                      "conviction": min_conv},
            # نتیجهٔ هر گیت: کارت تصمیم «همهٔ چک‌ها» را یک نگاه نشان می‌دهد
            "checks": {"trend": trend_ok, "momentum": mom_ok,
                       "liquidity": liq_ok, "data_quality": data_ok,
                       "volatility": vol_ok, "rsi": rsi_ok,
                       "ema_order": not ema_bear,
                       "macd": macd_ok, "volume": part_ok,
                       "not_chasing": chase_ok, "conviction": conv_ok,
                       "breakout_down": breakout_down,
                       "breakout_up": breakout_up},
            "gate_notes": gate_notes,
        }

        # --- confidence: نیمی امتیاز اسکنر، نیمی شاهدهای مستقل
        # (اطمینان باید از «چند شاهد مستقل تأیید می‌کنند» بیاید، نه از یک
        # عدد ترکیبی که ممکن است با یک سیگنال تکی بالا رفته باشد.)
        basis = 0.5 * float(score or 0) + 0.5 * conviction
        conf = 0.5
        if action == "BUY":
            conf = 0.55 + 0.4 * basis
        elif action == "SELL":
            conf = 0.55 + 0.3 * (1.0 - basis)
        elif action == "AVOID":
            conf = 0.7
        conf *= (0.5 + 0.5 * dq)   # dq already read for the gates above
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
            "conviction": conviction,
            "time_horizon": "INTRADAY",
            "reason_codes": ([r for r in candidate.get("reasons", [])][:5]
                             + (["REGIME_" + candidate["regime"]]
                                if candidate.get("regime") else [])
                             + gate_notes),
            "created_at": now_iso,
            "expires_at": expires_iso,
            # تحلیل کامل این تصمیم: همهٔ سیگنال‌ها + نتیجهٔ هر گیت + notِ‌های
            # گیت — کارت «آخرین تصمیم‌ها» همین را به‌صورت خوانا نشان می‌دهد
            "why": why,
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
