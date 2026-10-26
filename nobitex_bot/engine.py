"""Engine orchestrator — contract sections 54, 80, 83, 93, 100.

Runs the cycle loop on a background thread:

  collect snapshot -> features -> quality gate -> quant scanner ->
  AI decisions -> AI output validation -> risk engine -> execution ->
  PnL/performance

Analysis and collection are decoupled (SS54): a single worker alternates,
but a failed analysis never blocks the next snapshot. Safe Mode (SS80)
blocks all new orders while staying cancellable.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import re

from .ai import AILayer
from .collector import SnapshotCollector
from .config import Config
from .events import EventBus
from .execution import ExecutionEngine
from .features import (liquidity_score, market_regime, market_structure,
                       orderbook_features, technical_features,
                       volatility_features)
from .demo_market import DemoMarket
from .nobitex_api import NobitexAPI, NobitexError
from .portfolio import PaperPortfolio, ZERO
from .risk import RiskEngine
from .scanner import pre_rank_from_catalog, rank_markets
from .regime import gate_candidates
from .storage import Storage
from .nobitex_api import NobitexSession
from .universe import MarketUniverse
from .utils import canonical_symbol, d, new_id, parse_iso, quote_of_market, \
    split_symbol, utc_iso, utc_now_iso


def fmt_price(x) -> str:
    """Compact price text for messages (toman prices are big)."""
    try:
        v = Decimal(str(x))
    except Exception:
        return str(x)
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return str(v.quantize(Decimal("0.01")))
    return str(v.quantize(Decimal("0.000001")))
from pathlib import Path
from .validation import (ValidationResult, validate_ai_decision,
                         validate_snapshot_header)


# Bump when the net-deposited ALGORITHM changes: a cache written by an older
# build is ignored (and immediately refreshed) instead of being served with
# a wrong capital figure. v2 = the turnover list is read across ALL pages
# (page 1 alone truncated older deposits and the missing money read as
# profit).
NET_DEPOSIT_CACHE_VERSION = 2


class TradingEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data_dir = cfg.engine.data_dir
        self.events = EventBus(self.data_dir)
        self.storage = Storage(self.data_dir)
        from .exchange_factory import make_exchange
        self.exchange = make_exchange(cfg)
        # real-account session: ALWAYS available so the dashboard can log in
        # from any mode (SS81); the adapter itself may still be the simulator
        self.nobitex_session = NobitexSession(
            Path(cfg.engine.data_dir) / "real",
            username=cfg.exchange.nobitex_username,
            password=cfg.exchange.nobitex_password,
            totp_secret=cfg.exchange.nobitex_totp_secret,
            timeout=cfg.exchange.rest_timeout)
        if isinstance(self.exchange, NobitexAPI):
            # single shared session (token cache) for the real adapter
            self.nobitex_session = self.exchange.session
            self._real_attached = True
        else:
            self._real_attached = False
        self.collector = SnapshotCollector(cfg, self.exchange, self.events,
                                           self.storage)
        starting = {"USDT": cfg.paper.starting_usdt}
        if cfg.exchange.mode == "demo" and cfg.paper.starting_irt > 0:
            starting["IRT"] = cfg.paper.starting_irt
        self.portfolio = PaperPortfolio(self.storage, self.events,
                                        starting_balances=starting)
        self.risk = RiskEngine(cfg, self.storage, self.events)
        self.executor = ExecutionEngine(cfg, self.storage, self.events,
                                        self.portfolio, self.exchange)
        self.ai = AILayer(cfg, self.events)
        # SS100d: EVERY buy fill (paper or live, manual or automatic)
        # re-arms the automatic sell-stop at a realistic 2%~6% below the
        # new fill price — listen to the position events the executor and
        # the portfolio publish so no path can skip it
        self.events.subscribe(self._on_position_event)
        # disk retention: snapshot cap + events.jsonl cap (configurable;
        # 0 disables the cap)
        try:
            self.events.set_retention(
                getattr(cfg.engine, "events_retention_mb", 20.0))
        except Exception:
            pass
        try:
            if hasattr(self.collector, "_prune_snapshots"):
                self.collector._prune_snapshots()
        except Exception:
            pass
        # disk footprint: decision/risk history is the DB growth driver.
        # Trim once at startup so a long-running account shrinks on restart
        # (VACUUM only at startup — it rewrites the file and would stall a
        # cycle if it ran mid-trading).
        self._last_db_check = 0
        try:
            self.storage.prune_history(int(getattr(
                cfg.engine, "decisions_keep", 3000)))
            if self.storage.db_size_mb() > float(getattr(
                    cfg.engine, "db_max_mb", 120.0)):
                self.storage.vacuum()
        except Exception:
            pass
        # one-shot backfill: positions bought before the holding-timer
        # feature have an empty last_buy_at — recover it from the latest
        # recorded BUY fill of that coin (both market-name spellings)
        try:
            self._backfill_last_buy_from_fills()
        except Exception:
            pass
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()
        self._cycles_run = 0
        self._last_error = None
        self.safe_mode = False
        self.safe_mode_reasons = []
        # ---- حالت ریسک (چرخش سریع روی برترین‌های ۲۴ساعته) ----
        # یک فلگ ساده: scanner/risk/ai/exit در نقطه استفاده آن را
        # می‌خوانند (هیچ مقداری جهش نمی‌کند تا ذخیره تنظیمات یا
        # ری‌استارت چیزی را خراب کند)
        self.risk_mode = bool(getattr(self.cfg.risk, "risk_mode", False))
        self.latest_snapshot = None
        self.latest_candidates = []
        self.latest_decisions = []
        self.latest_risk = []
        self.latest_executions = []
        self.started_at = utc_now_iso()
        self.auto_trading = True
        self._ticker_cache: dict = {}   # market -> (price, fetched_at)
        # exact exchange wallet valuations: ASSET -> {amount, value_irt,
        # price_irt}. Filled by sync_real_wallet() from the exchange's own
        # rialBalance, so the holdings box and portfolio use the SAME Toman
        # numbers the Nobitex wallet shows (and coins with no live market,
        # e.g. XIRT, are never valued at zero).
        self._wallet_valuations: dict = {}
        # wallet currency -> the market its balance is really held in.
        # Nobitex wallet tickers do not always equal the market base:
        # wallet «x» is X Empire, traded as XEMPIREIRT. The sync records
        # this mapping so the row can use the wallet's exact rialBalance
        # instead of a same-named (but different) ticker.
        self._wallet_market_assets: dict = {}
        # live market universe (universe.py): EVERY market the exchange
        # lists, in one cached public call — the snapshot-free price/catalog
        # source for holdings, exits, manual orders and the scanner. It is
        # REBOUND automatically when the exchange adapter is swapped
        # (demo -> real hot attach).
        self._universe: MarketUniverse | None = None
        self._universe_exchange = None
        # «سفارشهای در جریان»: the exchange open-order read is TTL-cached
        # so the few-second dashboard poll cannot hammer the API
        self._open_orders_cache = (0.0, [])
        # seamless restart: a persisted login token hot-attaches the real
        # Nobitex adapter at startup, so the bot goes straight back to
        # trading the real account (no manual re-login, no demo detour)
        if (self.nobitex_session.token and not self._real_attached and
                getattr(self.cfg.exchange, "live_by_default", True)):
            attach = self._attach_real_exchange()
            if attach.get("attached"):
                self.sync_real_wallet()
                self.cfg.exchange.live_trading = True

    # ------------------------------------------------------------ safe mode
    def _on_position_event(self, event: dict) -> dict | None:
        """EventBus callback: POSITION_OPENED/POSITION_UPDATED on a BUY fill
        -> (re)set the automatic sell-stop from the fill's price. Runs in
        every path: paper execution, live execution, wallet-sync top-ups
        (the reconciler also calls _auto_stop_for_buy directly)."""
        try:
            et = event.get("event_type") or ""
            if et not in ("POSITION_OPENED", "POSITION_UPDATED"):
                return None
            payload = event.get("payload") or {}
            market = payload.get("market") or payload.get("symbol") or ""
            pos = self.storage.position(market) if market else None
            if not pos or d(pos["amount"]) <= 0:
                return None
            buy_px = pos.get("last_buy_price") or pos.get("avg_entry")
            buy_px_d = d(str(buy_px or 0))
            if buy_px_d <= 0:
                return None
            return self._auto_stop_for_buy(market, buy_px_d)
        except Exception:
            return None

    def _backfill_last_buy_from_fills(self) -> None:
        """Fill empty positions.last_buy_at from recorded BUY fills.

        Scoped to the SAME coin + quote asset: `fills_by_base` is a
        prefix query, so the one-letter base of XIRT also matched
        XRPIRT/XLMIRT/XAUTIRT fills. On the next restart the newest XRP
        buy was written into XIRT's «قیمت خرید» (346,584 for a ~1.37
        coin), turning the row into a fake «ـ۱۰۰٪ / ‎-۳۵ میلیارد تومان»
        loss. Alias spellings of the SAME coin (ARBIRT/ARBITRUMIRT) still
        match because both sides pass through canonical_symbol+
        split_symbol.
        """
        for p in self.storage.positions():
            if p.get("last_buy_at") or d(p["amount"]) <= 0:
                continue
            market = p["market"]
            want = split_symbol(canonical_symbol(market))
            base = want[0]
            latest = None
            latest_price = None
            for f in self.storage.fills_by_base(base, limit=200):
                if split_symbol(canonical_symbol(
                        f.get("market") or "")) != want:
                    continue
                if (f.get("side") or "").upper() == "BUY" and f.get(
                        "timestamp"):
                    if latest is None or f["timestamp"] > latest:
                        latest = f["timestamp"]
                        latest_price = f.get("price")
            if latest:
                self.storage.upsert_position(
                    market, p["amount"], p.get("avg_entry") or ZERO,
                    p.get("realized_pnl") or ZERO, last_buy_at=latest,
                    last_buy_price=latest_price)

    def _auto_stop_for_buy(self, symbol: str, buy_price: Decimal) -> dict:
        """SS100d: the exit levels that go live automatically after EVERY
        buy fill (manual or engine):
        - stop-loss at a FIXED configurable percent below the buy price
          (risk.default_stop_loss_pct, default -2%); when set to 0 the
          old volatility-adaptive ATR band (auto_stop_min/max_pct) is
          used instead.
        - take-profit at a FIXED configurable percent above the buy price
          (risk.default_take_profit_pct, default +20%) — 0 disables.
        """
        plans = self.storage.kv_get("exit_plans", {})
        if not isinstance(plans, dict):
            plans = {}
        plan = dict(plans.get(symbol) or {})
        # نردبان: خریدی که خودِ موتور برای «سرمایهٔ پله» فرستاده ادامهٔ همان
        # کمپین است — نباید نردبان (پله/قیمت حد/حد ضرر) را ریست کند.
        # پرچم اینجا مصرف می‌شود و پلن دست‌نخورده می‌ماند (حد ضرر روی پلهٔ
        # قبلی نشسته و همان‌جا می‌ماند).
        if plan.pop("ladder_buy_pending", None):
            plans[symbol] = plan
            self.storage.kv_set("exit_plans", plans)
            self.events.publish("LADDER_BUY_KEPT_LADDER", "engine", "",
                                {"symbol": symbol,
                                 "buy_price": float(buy_price),
                                 "stage": int(plan.get("stage", 0) or 0),
                                 "stop": plan.get("stop"),
                                 "take_profit": plan.get(
                                     "take_profit_manual")})
            return plan
        # ---- stop-loss: fixed % (default) or ATR band (opt-in via 0)
        # حالت ریسک: حد ضرر ATR-محور — max(کف ثابت ‎-3٪، 1.5×ATR٪) با سقف
        # ‎-8٪: حد ثابت تنگ‌تر از نوسان واقعی بازار = نویز (هر کوین حدِ
        # خودش را از ATR می‌گیرد)؛ بازتر از سقف = ریسک بی‌مهار.
        if self.risk_mode:
            fixed = d(str(getattr(self.cfg.risk,
                                  "risk_mode_stop_loss_pct", -3.0) or 0))
            atr_mult = d(str(getattr(self.cfg.risk,
                                     "risk_mode_atr_stop_mult", 1.5) or 0))
            cap = d(str(getattr(self.cfg.risk,
                                "risk_mode_atr_stop_cap_pct", -8.0) or 0))
            if atr_mult > 0:
                atr_pct = None
                snap = self.latest_snapshot or {}
                for m in snap.get("markets", []):
                    if m.get("symbol") == symbol:
                        atr_pct = ((m.get("volatility") or {}).get(
                            "atr_percent"))
                        break
                try:
                    if atr_pct and float(atr_pct) > 0:
                        # everything in PERCENT here (fixed=-3 means 3%,
                        # atr_percent is a fraction: 0.04 = 4%): the wider
                        # of the fixed floor and mult×ATR wins, capped at
                        # the absolute maximum.
                        atr_pct_units = atr_mult * d(str(atr_pct)) * d("100")
                        frac_pct = max(-fixed, atr_pct_units)
                        cap_pct = -cap
                        if cap_pct > 0 and frac_pct > cap_pct:
                            frac_pct = cap_pct
                        fixed = -frac_pct
                except Exception:
                    pass
        else:
            fixed = d(str(getattr(self.cfg.risk, "default_stop_loss_pct",
                                  -2.0) or 0))
        if fixed != 0:
            frac = -fixed / d("100")
            if frac <= 0:
                frac = d("0.02")
        else:
            lo = d(str(getattr(self.cfg.risk, "auto_stop_min_pct", 0.02)))
            hi = d(str(getattr(self.cfg.risk, "auto_stop_max_pct", 0.06)))
            if hi < lo:
                lo, hi = hi, lo
            frac = lo
            atr_pct = None
            snap = self.latest_snapshot or {}
            for m in snap.get("markets", []):
                if m.get("symbol") == symbol:
                    atr_pct = ((m.get("volatility") or {}).get(
                        "atr_percent"))
                    break
            try:
                if atr_pct:
                    # 1×ATR% -> the lower bound, 3×ATR% and above -> the
                    # upper bound, linear in between — clamped into band
                    a = d(str(atr_pct))
                    span = ((a - lo) / ((hi - lo) / d("2"))
                            if hi > lo else ZERO)
                    frac = min(max(lo + span * (hi - lo), lo), hi)
            except Exception:
                frac = lo
        stop = (buy_price * (1 - frac)).quantize(Decimal("0.000001"))
        plan["stop"] = float(stop)
        plan["stop_pct"] = float((-frac * 100).quantize(Decimal("0.1")))
        plan["auto"] = True
        # a fresh buy revives the position — clear any below-min holdout
        plan.pop("below_min_until_ts", None)
        # ---- take-profit: fixed % above the buy price (0 disables)
        # حالت ریسک: حد سود (پیش‌فرض ‎+12٪) تا پول سریع‌تر آزاد و
        # به پرسودترین بعدی منتقل شود (چرخش)
        if self.risk_mode:
            tp_pct = d(str(getattr(self.cfg.risk,
                                   "risk_mode_take_profit_pct", 12.0) or 0))
        else:
            tp_pct = d(str(getattr(self.cfg.risk, "default_take_profit_pct",
                                   20.0) or 0))
        if tp_pct > 0:
            tp = (buy_price * (1 + tp_pct / d("100"))).quantize(
                Decimal("0.000001"))
            plan["take_profit_manual"] = float(tp)
            plan["take_profit_manual_pct"] = float(tp_pct)
        # HADD (strike base): the reference price the TAKE-PROFIT is
        # measured from. EVERY buy (re-entry included) resets it to the
        # fresh buy price with stage 0 — the strike counter counts how
        # many times the price has REACHED the armed take-profit since
        # this buy. A stop-lock from a previous stop-exit is cleared:
        # the position is held again, so the lock no longer applies.
        plan["base_price"] = float(buy_price)
        plan["stage"] = 0
        # نردبان: لنگر (پله ۰) = قیمت همین خرید — هر خرید تازه نردبان را
        # از همین‌جا شروع می‌کند، پس پله‌ها و «حد ضرر پلهٔ قبلی» از این
        # قیمت شمرده می‌شوند (خرید خودکار پله بالا این را ریست نمی‌کند).
        plan["ladder_anchor"] = float(buy_price)
        plan["ladder_rungs"] = [float(buy_price)]
        plan["ladder_add_stage"] = 0
        plan["ladder_done_stage"] = 0
        plan.pop("ladder_stop_stage", None)
        # anchor the risk-mode drop-exit (فروش هنگام افت) at the fresh buy
        plan["risk_mode_peak"] = float(buy_price)
        plan.pop("stop_lock_until_ts", None)
        plan.pop("stop_lock_hadd", None)
        plans[symbol] = plan
        self.storage.kv_set("exit_plans", plans)
        self.events.publish("AUTO_STOP_SET", "engine", "",
                            {"symbol": symbol, "stop": float(stop),
                             "pct": plan["stop_pct"],
                             "take_profit": plan.get("take_profit_manual"),
                             "tp_pct": plan.get("take_profit_manual_pct")})
        return plan

    @staticmethod
    def _hadd_base(plan: dict, last_buy_price=0, avg_entry=0):
        """The plan's hadd (strike base) price — the reference the
        TAKE-PROFIT percent is measured from.

        Stage 0 (no strike yet): the hadd IS the LAST buy price — the
        number the user just paid (fallback: average entry for imported
        coins with no recorded buy; stale stored bases are ignored so a
        fresh position never shows an old hadd).
        Stage >= 1: the hadd is the last strike price the market REACHED
        (stored in base_price by the ratchet).
        """
        try:
            stage = int(plan.get("stage", 0) or 0)
        except Exception:
            stage = 0
        if stage <= 0:
            cands = (last_buy_price, avg_entry)
        else:
            cands = (plan.get("base_price"), plan.get("locked_stop"),
                     last_buy_price, avg_entry)
        for cand in cands:
            try:
                v = d(str(cand or 0))
            except Exception:
                continue
            if v > 0:
                return v
        return ZERO

    def _hadd_stage_of(self, plan: dict, last_buy_price=0, avg_entry=0):
        """Consistent (stage, hadd) pair — enforces the invariant: a
        hadd EQUAL to the BUY price means stage 0, nothing else. Legacy
        plans with a stale counter (stage>0 while the hadd has fallen
        back to the buy price) read as stage 0."""
        try:
            stage = int(plan.get("stage", 0) or 0)
        except Exception:
            stage = 0
        hadd = self._hadd_base(plan, last_buy_price, avg_entry)
        try:
            anchor = d(str(last_buy_price or 0))
        except Exception:
            anchor = ZERO
        if anchor <= 0:
            try:
                anchor = d(str(avg_entry or 0))
            except Exception:
                anchor = ZERO
        if (hadd > 0 and anchor > 0 and
                abs(hadd - anchor) <= max(Decimal("0.000001"),
                                          anchor * Decimal("0.000000001"))):
            stage = 0
        return stage, hadd

    def set_hadd(self, symbol: str, stage: int,
                 hadd: float | None = None) -> dict:
        """Manually set the strike counter (the «(n)» bracket) and/or the
        hadd price from the holdings row.

        stage=0            -> the hadd resets to the BUY price (the
                              take-profit re-arms from it); the stop-loss
                              stays where it is.
        stage>0 + hadd     -> the hadd is that price; the take-profit
                              re-arms hadd x (1 + tp_pct/100) at the
                              percent it already had AND the ladder moves
                              the stop-loss up to the previous rung
                              (پله ۱ ← قیمت خرید، پله ۲ ← پله ۱ …) — it
                              only ever moves UP. The manually edited rung
                              does NOT spend the per-rung capital on its
                              own (the box's «خرید» is for that).
        Invariant: a hadd equal to the buy price IS stage 0 — requesting
        stage>0 with such a hadd auto-corrects to 0 with a Persian notice.
        """
        symbol = (symbol or "").strip().upper()
        pos = self.storage.position(symbol)
        if not pos or d(pos["amount"]) <= 0:
            return {"ok": False, "error": "موقعیتی برای " + symbol + " نیست"}
        try:
            stage = int(stage)
        except Exception:
            return {"ok": False, "error": "پله نامعتبر است"}
        if stage < 0:
            stage = 0
        plans = self.storage.kv_get("exit_plans", {})
        if not isinstance(plans, dict):
            plans = {}
        plan = dict(plans.get(symbol) or {})
        lbp = d(str(pos.get("last_buy_price") or 0))
        ae = d(str(pos.get("avg_entry") or 0))
        _cur_stage, cur_hadd = self._hadd_stage_of(plan, lbp, ae)
        anchor = lbp if lbp > 0 else ae
        try:
            hadd_d = d(str(hadd)) if hadd is not None else ZERO
        except Exception:
            return {"ok": False, "error": "قیمت حد نامعتبر است"}
        notice = None
        if stage <= 0:
            stage = 0
            base = anchor if anchor > 0 else (hadd_d if hadd_d > 0
                                              else cur_hadd)
            if base <= 0:
                return {"ok": False,
                        "error": "قیمت خرید برای پله ۰ موجود نیست — قیمت حد را دستی بدهید"}
        else:
            base = hadd_d if hadd_d > 0 else cur_hadd
            if base <= 0:
                return {"ok": False,
                        "error": "برای پله بالاتر از صفر، قیمت حد را هم بدهید"}
            if anchor > 0 and abs(base - anchor) <= max(
                    Decimal("0.000001"),
                    anchor * Decimal("0.000000001")):
                # a hadd equal to the buy price IS stage 0 — auto-correct
                stage = 0
                notice = "قیمت حد برابر قیمت خرید است؛ پله روی ۰ تنظیم شد"
        plan["stage"] = stage
        plan["base_price"] = float(base)
        plan["locked_stop"] = float(base)
        if stage <= 0:
            # پله ۰ = نردبان از همین قیمت شروع می‌شود: لنگر پله‌ها و بودجهٔ
            # سرمایهٔ پله‌ها از صفر شمرده می‌شوند
            plan["ladder_anchor"] = float(base)
            plan["ladder_rungs"] = [float(base)]
            plan["ladder_add_stage"] = 0
            plan["ladder_done_stage"] = 0
            plan.pop("ladder_stop_stage", None)
        # re-arm the take-profit from the new hadd at its current percent
        tp_pct = plan.get("take_profit_manual_pct")
        try:
            tp_pct_d = d(str(tp_pct)) if tp_pct is not None else ZERO
        except Exception:
            tp_pct_d = ZERO
        if tp_pct_d <= 0:
            tp_pct_d = d(str(getattr(self.cfg.risk,
                                     "default_take_profit_pct", 25.0) or 0))
        if tp_pct_d > 0:
            tp = (base * (1 + tp_pct_d / d("100"))).quantize(
                Decimal("0.000001"))
            plan["take_profit_manual"] = float(tp)
            plan["take_profit_manual_pct"] = float(tp_pct_d)
        # نردبان: پلهٔ دستی هم حد ضرر را روی پلهٔ قبلی می‌برد (فقط بالا)
        if stage > 0 and getattr(self.cfg.risk, "ladder_enabled", True):
            try:
                step_default = float(getattr(
                    self.cfg.risk, "profit_lock_step_pct", 15.0) or 15.0)
            except Exception:
                step_default = 15.0
            self._raise_ladder_stop(symbol, plan, base,
                                    anchor if anchor > 0 else lbp,
                                    stage, step_default)
        plans[symbol] = plan
        self.storage.kv_set("exit_plans", plans)
        self.events.publish("HADD_SET", "engine", "",
                            {"symbol": symbol, "stage": stage,
                             "base_price": float(base),
                             "stop": plan.get("stop"),
                             "take_profit": plan.get("take_profit_manual")})
        out = {"ok": True, "symbol": symbol, "stage": stage,
               "hadd": float(base),
               "stop": plan.get("stop"),
               "take_profit": plan.get("take_profit_manual")}
        if notice:
            out["notice"] = notice
        return out

    @staticmethod
    def _ladder_step_pct(plan: dict, default: float = 15.0) -> Decimal:
        """ارتفاع یک پله، در درصد: درصد حد سود همین پلن (هر پلن با گام
        خودش مسلح شده)، وگرنه risk.profit_lock_step_pct."""
        for cand in (plan.get("ladder_step_pct"),
                     plan.get("take_profit_manual_pct")):
            try:
                v = d(str(cand))
            except Exception:
                continue
            if v > 0:
                return v
        try:
            v = d(str(default))
        except Exception:
            v = ZERO
        return v if v > 0 else Decimal("15")

    def _ladder_rungs(self, plan: dict, hadd: Decimal, anchor: Decimal,
                      stage: int, default_step: float = 15.0) -> list:
        """قیمت پله‌ها: پله ۰ = لنگر نردبان (قیمت شروعی که پله‌ها از آن
        شمرده می‌شوند)، پله n = پلهٔ قبلی × (۱ + گام)؛ پلهٔ آخر همیشه
        همان «قیمت حد» فعلی است (پله‌ای که قیمت واقعاً به آن رسیده).
        پلن‌های قدیمی که لنگر نردبان ندارند از همین‌جا ساخته می‌شوند.
        """
        step = self._ladder_step_pct(plan, default_step)
        try:
            anchor_d = d(str(anchor or 0))
        except Exception:
            anchor_d = ZERO
        if anchor_d <= 0:
            anchor_d = hadd
        if anchor_d <= 0:
            return []
        # پله‌های واقعی که قیمت به آن‌ها رسیده (ratchet همین‌ها را ثبت
        # می‌کند) — دقیق‌تر از بازسازی هندسی، چون گام پله‌ها می‌تواند
        # بین پله‌ها عوض شود (مثلاً پلهٔ اول ‎+۲۰٪ و بقیه ‎+۱۵٪)
        rungs = []
        stored = plan.get("ladder_rungs")
        if isinstance(stored, list) and len(stored) == max(0, int(stage)) + 1:
            for x in stored:
                try:
                    v = d(str(x))
                except Exception:
                    v = ZERO
                if v <= 0:
                    rungs = []
                    break
                rungs.append(v.quantize(Decimal("0.000001")))
        if len(rungs) < 2:
            rungs = []
            if step > 0:
                for k in range(max(0, int(stage)) + 1):
                    rungs.append((anchor_d * (d("1") + step / d("100")) ** k)
                                 .quantize(Decimal("0.000001")))
        if not rungs:
            rungs = [anchor_d.quantize(Decimal("0.000001"))]
        if hadd > 0:
            rungs[-1] = hadd.quantize(Decimal("0.000001"))
        return rungs

    def _ladder_prev_rung(self, plan: dict, hadd: Decimal, anchor: Decimal,
                          stage: int, default_step: float = 15.0) -> Decimal:
        """حد ضرر نردبانی: روی «پلهٔ قبلی» می‌نشیند — پله ۱ ← پله ۰ =
        قیمت خرید (سربه‌سر)، پله ۲ ← پله ۱، پله ۳ ← پله ۲ … و از آن پله
        پایین‌تر نمی‌آید. پله ۰ (هنوز هیچ strike ی نخورده) = دست‌نزدن.

        چرا «پلهٔ قبلی» و نه چند درصد زیر پلهٔ جدید: با این قاعده یک پلهٔ
        کامل فضای نفس می‌ماند؛ حد تنگ (مثلاً ۳٪ زیر پلهٔ تازه) روی تکان
        معمولی بازار می‌فروخت و سود را قبل از ادامهٔ روند می‌بست.
        """
        if stage <= 0:
            return ZERO
        rungs = self._ladder_rungs(plan, hadd, anchor, stage, default_step)
        if len(rungs) < 2:
            return ZERO
        return rungs[-2]

    def _raise_ladder_stop(self, symbol: str, plan: dict, hadd: Decimal,
                           fallback_anchor: Decimal, stage: int,
                           default_step: float = 15.0) -> bool:
        """حد ضرر پلن را روی پلهٔ قبلی می‌برد (فقط بالا، هرگز پایین).
        درصد و مبلغ حد ضرر همین لحظه از قیمت جدید درمی‌آید، چون
        _position_row آن‌ها را از خودِ stop می‌سازد. اگر نه، هیچ تغییری
        نمی‌دهد و False برمی‌گرداند.
        """
        if stage <= 0:
            return False
        try:
            anchor = d(str(plan.get("ladder_anchor") or 0))
        except Exception:
            anchor = ZERO
        if anchor <= 0:
            anchor = fallback_anchor
        rung_stop = self._ladder_prev_rung(plan, hadd, anchor, stage,
                                           default_step)
        if rung_stop <= 0:
            return False
        try:
            cur_stop = d(str(plan.get("stop", 0) or 0))
        except Exception:
            cur_stop = ZERO
        if rung_stop <= cur_stop:
            return False
        plan["stop"] = float(rung_stop)
        plan["ladder_stop"] = float(rung_stop)
        plan["ladder_stop_stage"] = int(stage)
        self.events.publish("LADDER_STOP_RAISED", "engine", "",
                            {"symbol": symbol, "stage": int(stage),
                             "stop": float(rung_stop),
                             "stop_before": float(cur_stop),
                             "hadd": float(hadd),
                             "anchor": float(anchor)})
        return True

    def _advance_profit_locks(self, prices: dict) -> int:
        """Always-on HADD ratchet + نردبان پله‌ها (run once per cycle).

        The moment the live price reaches the armed take-profit, the
        stage counter goes up (0→1→2→3…), the HADD moves to that price
        and the next take-profit re-arms step% above the new hadd.

        نردبان (ladder, risk.ladder_enabled): با هر strike حد ضرر روی
        «پلهٔ قبلی» بالا می‌رود و هرگز پایین نمی‌آید — پله ۱ ← قیمت خرید
        (سربه‌سر)، پله ۲ ← پله ۱ … پس هر پله که رد می‌شود سود پلهٔ قبل
        قفل می‌شود و درصد/مبلغ حد ضرر هم با همان پله به‌روز می‌شود.
        همین‌جا اگر حد ضرر یک پلن قدیمی عقب مانده باشد (زیر پلهٔ قبلی)
        هر چرخه بالا کشیده می‌شود.
        """
        advanced = 0
        try:
            step = d(str(getattr(self.cfg.risk,
                                 "profit_lock_step_pct", 20.0) or 0))
            if step <= 0:
                step = d("20")
            max_stage = int(getattr(self.cfg.risk,
                                    "profit_lock_max_stage", 0) or 0)
            ladder_on = bool(getattr(self.cfg.risk, "ladder_enabled", True))
            ladder_add_on = ladder_on and bool(getattr(
                self.cfg.risk, "ladder_topup_enabled", True))
            plans = self.storage.kv_get("exit_plans", {})
            if not isinstance(plans, dict):
                return 0
            for symbol, plan in list(plans.items()):
                if not isinstance(plan, dict):
                    continue
                pos = self.storage.position(symbol)
                if not pos or d(pos["amount"]) <= 0:
                    continue
                lbp = d(str(pos.get("last_buy_price") or 0))
                ae = d(str(pos.get("avg_entry") or 0))
                nstage, nhadd = self._hadd_stage_of(plan, lbp, ae)
                stage = int(plan.get("stage", 0) or 0)
                if nstage != stage:
                    # invariant: hadd == buy price ⇒ stage 0 — normalize
                    # legacy plans that still carry a stale counter
                    plan["stage"] = nstage
                    stage = nstage
                    plans[symbol] = plan
                    self.storage.kv_set("exit_plans", plans)
                # نردبان: حد ضرر هر پلن عقب‌مانده را همین چرخه بالا می‌برد
                if ladder_on and self._raise_ladder_stop(
                        symbol, plan, nhadd,
                        lbp if lbp > 0 else ae, stage, float(step)):
                    plans[symbol] = plan
                    self.storage.kv_set("exit_plans", plans)
                if max_stage > 0 and stage >= max_stage:
                    continue
                cur = prices.get(symbol)
                try:
                    cur_d = d(str(cur)) if cur else None
                except Exception:
                    cur_d = None
                if not cur_d or cur_d <= 0:
                    cur_d = self._live_price_for(symbol)
                if not cur_d or cur_d <= 0:
                    continue
                tp = d(str(plan.get("take_profit_manual", 0) or 0))
                if tp <= 0:
                    # plan without a level: re-arm one from the hadd
                    floor0 = self._hadd_base(plan, pos.get("last_buy_price"),
                                             pos.get("avg_entry"))
                    if floor0 > 0:
                        tp = (floor0 * (1 + step / 100)).quantize(
                            Decimal("0.000001"))
                        plan["take_profit_manual"] = float(tp)
                        plan["take_profit_manual_pct"] = float(step)
                        plans[symbol] = plan
                        self.storage.kv_set("exit_plans", plans)
                    continue
                if cur_d < tp:
                    continue
                # the live price REACHED the armed take-profit: the HADD
                # moves to that price (strike #N is registered) and the
                # next take-profit re-arms step% above the new hadd. The
                # ladder moves the STOP up to the previous rung with it.
                new_base = tp
                new_stage = stage + 1
                old_base = d(str(plan.get("base_price", 0) or 0))
                plan["base_price"] = float(new_base)
                plan["locked_stop"] = float(new_base)
                plan["stage"] = new_stage
                nxt = (new_base * (1 + step / 100)).quantize(
                    Decimal("0.000001"))
                plan["take_profit_manual"] = float(nxt)
                plan["take_profit_manual_pct"] = float(
                    ((nxt / new_base - 1) * 100)
                    .quantize(Decimal("0.1")))
                # نردبان: همین strike حد ضرر را روی پلهٔ قبلی می‌برد
                # (فقط بالا) و بودجهٔ سرمایهٔ پلهٔ جدید را در صف می‌گذارد
                # — _ladder_topup_decisions در همان چرخه آن را می‌خرد
                if ladder_on:
                    # پله‌های واقعی رد‌شده را نگه دار تا «پلهٔ قبلی» دقیق
                    # باشد — حتی وقتی گام پله‌ها بین پله‌ها عوض می‌شود
                    rungs = plan.get("ladder_rungs")
                    if (not isinstance(rungs, list) or
                            len(rungs) != max(0, int(stage)) + 1):
                        anchor_v = d(str(plan.get("ladder_anchor") or 0))
                        if anchor_v <= 0:
                            anchor_v = lbp if lbp > 0 else ae
                        rungs = [float(x) for x in self._ladder_rungs(
                            plan, old_base if old_base > 0 else anchor_v,
                            anchor_v, stage, float(step))]
                    rungs.append(float(new_base))
                    plan["ladder_rungs"] = rungs[-50:]
                    self._raise_ladder_stop(
                        symbol, plan, new_base,
                        lbp if lbp > 0 else ae, new_stage, float(step))
                if ladder_add_on:
                    plan["ladder_add_stage"] = new_stage
                plans[symbol] = plan
                self.storage.kv_set("exit_plans", plans)
                advanced += 1
                self.events.publish("PROFIT_LOCK_ADVANCED", "engine", "",
                                    {"symbol": symbol, "stage": new_stage,
                                     "base_price": float(new_base),
                                     "take_profit": float(nxt)})
        except Exception:
            pass
        return advanced

    # ------------------------------------------------------------ safe mode
    def enter_safe_mode(self, reasons: list) -> None:
        if self.safe_mode:
            return
        self.safe_mode = True
        self.safe_mode_reasons = reasons
        self.events.publish("SAFE_MODE_ENTERED", "engine", "",
                            {"reasons": reasons})
        # SS80: cancel-only mode — leave open orders alone (they're paper)

    def exit_safe_mode(self) -> None:
        if not self.safe_mode:
            return
        self.safe_mode = False
        self.safe_mode_reasons = []
        self.events.publish("SAFE_MODE_EXITED", "engine", "", {})

    # ------------------------------------------------------------ main loop
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="trading-engine")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            # interval re-read every iteration: editing cycle_seconds in
            # the settings tab takes effect WITHOUT a restart
            try:
                interval = max(5, int(self.cfg.engine.cycle_seconds))
            except (TypeError, ValueError):
                interval = 30
            t0 = time.time()
            try:
                self.run_cycle()
            except Exception as exc:
                self._last_error = str(exc)
                self.events.error("ENGINE", "CYCLE_FAILED", "HIGH",
                                  "%s\\n%s" % (exc, traceback.format_exc()[-500:]),
                                  retryable=True)
            elapsed = time.time() - t0
            self._stop.wait(max(1.0, interval - elapsed))
            if self.cfg.engine.max_cycles and \
                    self._cycles_run >= self.cfg.engine.max_cycles:
                break

    def _usdt_rates(self, prices: dict) -> dict:
        """USDT value of one unit of each asset (SS21 valuation).

        XXXUSDT markets give asset rates directly; USDTIRT gives the IRT
        rate. The account base (USDT) is exactly 1.0 by definition.
        If no USDTIRT price exists in the collected snapshot (it is not
        always part of the trading universe), fall back to the exchange
        adapter so Toman cash is never valued at zero.
        Coins held against IRT markets (e.g. ARBIRT) have no *USDT market
        in the snapshot — they MUST still be valued or the wallet coins
        vanish from equity and the exposure ratio explodes. Their rate is
        their IRT-market price divided by the Toman-per-USDT rate.
        """
        rates = {"USDT": 1.0}
        usdtirt = 0.0
        for sym, price in prices.items():
            if sym.endswith("USDT") and sym != "USDTIRT":
                rates[sym[:-4]] = price
            elif sym == "USDTIRT" and price:
                usdtirt = float(price)
        if not usdtirt:
            try:
                t = self.exchange.ticker("USDTIRT") \
                    if hasattr(self.exchange, "ticker") else None
                if t and t.get("last_price"):
                    usdtirt = float(t["last_price"])
            except Exception:
                pass
        if usdtirt:
            # sanity guard: real Toman-per-USDT is ~70k-150k. A stale or
            # adapter-glitch price (~2.3 = a Riali leftover) would value the
            # wallet's real Toman cash at ~$8 instead of ~$18, inflate the
            # exposure ratio and silently block every buy while cash sits
            # idle. Refresh from the live ticker before trusting it.
            if not (30000 <= usdtirt <= 10000000):
                try:
                    t = self.exchange.ticker("USDTIRT") \
                        if hasattr(self.exchange, "ticker") else None
                    if t and t.get("last_price"):
                        usdtirt = float(t["last_price"])
                except Exception:
                    pass
                if not (30000 <= usdtirt <= 10000000):
                    usdtirt = 0.0
        if usdtirt:
            rates["IRT"] = 1.0 / usdtirt
            # every asset the wallet holds must be valued: IRT-quoted
            # markets first (coin Toman price / Toman-per-USDT), then the
            # adapter's stats cache as a fallback so valuation works even
            # before the first full snapshot exists.
            try:
                held = set(self.portfolio.balances.keys())
            except Exception:
                held = set()
            for base in held:
                if base in rates or base in ("USDT", "IRT", ""):
                    continue
                p = prices.get(base + "IRT")
                if p:
                    rates[base] = float(p) / usdtirt
                    continue
                # Snapshot-free fallback: the LIVE catalog knows this coin's
                # dollar price even when neither the cycle's top-N snapshot
                # nor the adapter's stats cache carries it. Without this a
                # wallet coin bought outside the snapshot was valued at
                # ZERO, so the account equity quietly under-reported it —
                # and «ارزش کل حساب» disagreed with the sum of the
                # portfolio's own rows (the same rows the user can see).
                try:
                    px = self.market_universe().price(base + "USDT")
                except Exception:
                    px = None
                if px:
                    rates[base] = float(px)
                    continue
                try:
                    stats = self.exchange._stats_all()
                    st = stats.get(base.lower() + "-rls") or {}
                    div = float(getattr(self.cfg.exchange,
                                        "rls_to_irt_divisor", 10) or 10)
                    toman = float(st.get("latest") or 0) / div
                    if toman > 0:
                        rates[base] = toman / usdtirt
                except Exception:
                    pass
        return rates

    def _maybe_housekeeping(self) -> None:
        """Cheap disk check every N cycles (see engine.db_* config).

        Deleted rows keep their SQLite pages for reuse, so the file does not
        grow again after the startup VACUUM; pruning here just keeps the
        row count (and the size) flat without stalling a cycle with VACUUM.
        """
        try:
            every = max(1, int(getattr(self.cfg.engine,
                                       "db_maintenance_every", 30)))
        except Exception:
            every = 30
        self._last_db_check = getattr(self, "_last_db_check", 0) + 1
        if self._last_db_check % every:
            return
        try:
            self.storage.prune_history(
                int(getattr(self.cfg.engine, "decisions_keep", 3000)))
        except Exception:
            pass
        try:
            if not self.collector._persist_enabled():
                self.collector._prune_snapshots()
        except Exception:
            pass
        try:
            self.events._prune_events()
        except Exception:
            pass

    def run_cycle(self) -> dict:
        """One full SS100 pipeline pass. Returns a cycle summary."""
        with self._cycle_lock:
            cycle_id = "cycle_" + utc_now_iso().replace(
                "-", "").replace(":", "")[:15]
            summary = {"cycle_id": cycle_id, "started_at": utc_now_iso()}
            self._maybe_housekeeping()

            # 0 ---- real-account upkeep (SS67 token resync + SS44 recon)
            self.maybe_refresh_token()
            if self.cfg.exchange.wallet_sync:
                sync = self.sync_real_wallet()
                if not sync.get("ok") and sync.get("error"):
                    summary["wallet_sync"] = sync["error"]
            # 0 ---- یک سکه = یک ردیف (دمو و واقعی): موجودی همین دفتر
            # (حالت واقعی: آینهٔ کیف پول صرافی؛ دمو: کیف پول کاغذی) منبع
            # حقیقت است. یک دارایی هرگز نباید همزمان در بخش تتری و تومانی
            # دیده شود؛ ردیف بازار اشتباه صفر می‌شود (رویداد
            # POSITION_MARKET_REFILED). در حالت دمو که sync_real_wallet
            # اجرا نمی‌شود، همین چرخه داشبورد را خودش تمیز می‌کند.
            # Only when the ledger IS a wallet: in demo the paper wallet is
            # the truth; with a real account it is the mirrored exchange
            # wallet (and that mirror only exists with wallet_sync on).
            # Otherwise portfolio.balances is stale paper money and must
            # never be used to clamp real positions.
            try:
                ledger_is_wallet = (not self._real_attached) or bool(
                    self.cfg.exchange.wallet_sync)
                if ledger_is_wallet:
                    deduped = self._reconcile_wallet_totals(
                        dict(getattr(self.portfolio, "balances", {})
                             or {}), {})
                    if deduped:
                        summary["position_market_deduped"] = deduped
            except Exception:
                pass
            summary["real_orders_reconciled"] = self.reconcile_real_orders()
            try:
                summary["real_fills_backfilled"] = \
                    self.backfill_real_fills()
            except Exception:
                summary["real_fills_backfilled"] = 0
            # SS100f: resting BUY orders lock IRT out of the wallet —
            # cancel stale ones after the timeout, and "limit-chase" fresh
            # ones priced below the live price so entries never lag the
            # market and capital never sits locked waiting for the timeout.
            try:
                stale = self._cancel_stale_orders(chase=True)
                if stale:
                    summary["stale_orders_cancelled"] = len(stale)
            except Exception:
                pass
            # SS101: convert tiny coin balances into IRT via the exchange's
            # own convert market so the cash is usable again for buys.
            try:
                dust = self._convert_dust_holdings()
                if dust.get("converted"):
                    summary["dust_converted"] = dust["converted"]
                if dust.get("failed"):
                    summary["dust_convert_failed"] = dust["failed"]
            except Exception:
                pass
            # حالت ریسک — تجمیع سرمایه: پوزیشن‌های خردِ خریدِ خودِ ربات
            # (زیر حداقل سفارش صرافی، فروش‌نشدنی) به تومان برمی‌گردند تا
            # نقدِ آزاد برای چرخش روی پرسودترین‌ها ساخته شود
            if self.risk_mode:
                try:
                    swept = self._sweep_bot_micro_positions()
                    if swept.get("converted"):
                        summary["risk_mode_swept"] = swept["converted"]
                    if swept.get("failed"):
                        summary["risk_mode_sweep_failed"] = swept["failed"]
                except Exception:
                    pass

            # 0 ---- dynamic market discovery + trading-universe selection
            # (SS5/SS6): when the real account is attached, restrict to real
            # tradable markets with a liquidity floor, preferring USDT pairs.
            disc = self.collector.discover_markets()
            if disc["new"]:
                summary["markets_discovered"] = len(disc["new"])
            if self._real_attached:
                self._apply_trading_universe()
            summary["universe_size"] = len(self.collector.registry
                                            .tradable_symbols())

            # 0.5 ---- whole-catalog pre-rank (ALL listed coins, one cached
            # request): every market the exchange lists is scored with its
            # 24h change from the LIVE catalog — not just the volume top-N
            # the trading universe selects. The best movers OUTSIDE the
            # snapshot universe join the deep-analysis pool of THIS cycle,
            # so a new listing or a rising small-cap can be chosen for a
            # buy without ever needing its own candles in the snapshot.
            try:
                uni = self.market_universe()
                pool_cap = max(1, int(getattr(self.cfg.ai,
                                              "top_n_markets", 5) or 5))
                starts = self.collector.registry.tradable_symbols()
                pre = pre_rank_from_catalog(
                    uni.rows(),
                    min_quote_volume_usdt=float(getattr(
                        self.cfg.risk,
                        "risk_mode_min_quote_volume_usdt", 100000.0)
                        if self.risk_mode else float(getattr(
                            self.cfg.exchange,
                            "min_quote_volume_usdt", 50000.0) or 0)),
                    extra_starts=max(4, pool_cap))
                # outside-the-universe movers that beat the pool size join
                for sym, mom24, qv, chg in pre:
                    if sym in starts:
                        continue
                    if len(starts) >= max(pool_cap * 3, 60):
                        break
                    starts.append(sym)
                self._catalog_pre_rank = [
                    {"symbol": s, "momentum_24h": round(mom24, 4),
                     "quote_volume_24h": qv, "change_24h": chg}
                    for s, mom24, qv, chg in pre]
                # the snapshot collects the pre-ranked movers of the WHOLE
                # catalog too (bounded: pool*2 extra markets max), so their
                # candle features are ready for the deep scanner this cycle
                known = set(starts)
                extra = [s for s, _m, _q, _c in pre
                         if s not in known][:max(4, pool_cap * 2)]
            except Exception:
                self._catalog_pre_rank = []
                extra = []

            # 1 ---- collect snapshot (SS7); the whole-catalog movers that
            # sit OUTSIDE the trading universe ride along (bounded list),
            # so every listed coin CAN be chosen for a buy
            snap = self.collector.collect(cycle_id,
                                          extra_symbols=(extra if isinstance(
                                              extra, list) else []))
            self.latest_snapshot = snap
            try:
                sres = ValidationResult()
                validate_snapshot_header(snap, sres)
                if not sres.ok:
                    self.events.error("ENGINE", "SNAPSHOT_HEADER_INVALID",
                                      "HIGH", sres.first_code())
            except Exception as exc:
                self.events.error("ENGINE", "SNAPSHOT_VALIDATE_FAILED",
                                  "WARNING", str(exc))

            # 2 ---- features + registry prices (SS12, 15-19)
            market_by_symbol = {}
            for m in snap["markets"]:
                sym = m["symbol"]
                candles = m.get("ohlc", {}).get("candles", [])
                book_feat = orderbook_features(m.get("orderbook", {}),
                                               m.get("last_price") or 0)
                m["orderbook_features"] = book_feat
                m["liquidity"] = liquidity_score(
                    book_feat, m.get("quote_volume_24h") or 0)
                m["volatility"] = volatility_features(
                    candles, m.get("last_price") or 0)
                m["technicals"] = technical_features(candles)
                m["structure"] = market_structure(candles)
                m["regime"] = market_regime(candles, m["volatility"])
                m["snapshot_id"] = snap["snapshot_id"]
                m["cycle_id"] = cycle_id
                market_by_symbol[sym] = m

            # 3 ---- quality gate (SS93)
            min_q = self.cfg.quality.min_ai_quality
            eligible = [m for m in snap["markets"]
                        if m["data_quality"]["score"] >= min_q and
                        m["data_quality"]["status"] in ("GOOD", "WARNING") and
                        # «بازار معاملات»: a market whose quote is switched
                        # OFF is not analyzed at all. Only the real registry
                        # used to apply the selector, so the DEMO fixture
                        # list kept trading its IRT pairs (BTCIRT/SHIBIRT)
                        # while the setting said «فقط تتری».
                        self._quote_market_enabled(m["symbol"])]
            if not eligible:
                self.events.publish("AI_ANALYSIS_STARTED", "engine", cycle_id,
                                    {"candidates": 0, "reason": "NO_ELIGIBLE_MARKETS"})
                # خروج‌ها هرگز نباید به تحلیل گره بخورند: وقتی هیچ بازار
                # واجدشرطی نیست (ریزش کل بازار، کیفیت داده پایین، بخش
                # معاملات خاموش) قبلاً از همین‌جا برمی‌گشتیم و حد ضرر
                # هیچ‌وقت بررسی نمی‌شد — همان «زیر حد ضرر رسید ولی
                # نفروخت». مسیر خروج مستقل از تحلیل اجرا می‌شود.
                summary["exit_only"] = len(self._run_exit_cycle(cycle_id))
                summary["decisions"] = 0
                self._cycles_run += 1
                summary["finished_at"] = utc_now_iso()
                self._cycles_run_summary = summary
                return summary

            # 4 ---- quant scanner (SS30)
            # حالت ریسک: رتبه‌بندی بر اساس پرسودترین‌های ۲۴ساعته
            candidates = rank_markets(
                [{"symbol": m["symbol"], "features": m,
                  "data_quality": m["data_quality"]} for m in eligible],
                self.cfg.ai.top_n_markets,
                risk_mode=self.risk_mode,
                min_quote_volume_usdt=(float(getattr(
                    self.cfg.risk, "risk_mode_min_quote_volume_usdt", 0)
                    or 0) if self.risk_mode else 0.0))
            self.latest_candidates = candidates

            # 4.5 ---- market regime gate: 24h-momentum rotation (risk
            # mode) degenerates in ranges/downtrends — it buys tops. The
            # regime layer classifies every candidate market and blocks
            # buys the current regime forbids. Never blocks EXITS.
            try:
                if (self.risk_mode and getattr(
                        self.cfg.risk, "risk_mode_require_uptrend", True)):
                    kept, blocked = gate_candidates(
                        candidates, market_by_symbol, risk_mode=True,
                        atr_crash_pct=float(getattr(
                            self.cfg.risk,
                            "risk_mode_regime_atr_crash_pct", 8.0) or 8.0))
                    if blocked:
                        candidates = kept
                        self.latest_regime_blocked = blocked
                        self.events.publish(
                            "RISK_MODE_REGIME_BLOCKED", "engine", cycle_id,
                            {"blocked": [b.get("symbol") for b in blocked],
                             "regimes": [b.get("regime_blocked")
                                         for b in blocked]})
                    else:
                        self.latest_regime_blocked = []
                else:
                    self.latest_regime_blocked = []
            except Exception as ex:
                self.latest_regime_blocked = []
                self.events.publish("SYSTEM_ERROR", "engine", cycle_id,
                                    {"where": "regime_gate",
                                     "error": str(ex)[:200]})
            if not candidates:
                # گیت رژیم در حالت ریسک همهٔ کاندیدها را در بازار نزولی
                # بلاک می‌کند؛ همان لحظه دقیقاً وقتی است که حد ضرر باید
                # بفروشد. قبلاً این return کل مدیریت خروج را رد می‌کرد.
                summary["exit_only"] = len(self._run_exit_cycle(cycle_id))
                summary["decisions"] = 0
                summary["regime_blocked_all"] = True
                self._cycles_run += 1
                summary["finished_at"] = utc_now_iso()
                self._cycles_run_summary = summary
                return summary

            # 5 ---- portfolio snapshot (SS21/22)
            prices = {m["symbol"]: m["last_price"] for m in snap["markets"]
                      if m.get("last_price")}
            # held coins are priced from the LIVE catalog too: a stop-loss
            # must be able to fire for a coin the snapshot did not include
            try:
                self._augment_prices_with_holdings(prices)
            except Exception:
                pass
            portfolio_snapshot = self.portfolio.portfolio_snapshot(
                prices, self._usdt_rates(prices))

            # 5.35 ---- profit-lock backstop: every held coin MUST carry a
            # sell stop. Wallet-sync/manual imports used to stay naked —
            # 292k Toman across 3 real coins had NO stop at all.
            try:
                self._backstop_exit_plans(prices)
            except Exception:
                pass

            # 5.4 ---- profit lock ratchet: lock floors reached this cycle
            # BEFORE the exit check reads the plans
            self._advance_profit_locks(prices)

            # 5.5 ---- exit management: stop-loss / take-profit (SS36/37)
            exit_decisions = self._manage_exits(prices, cycle_id)

            # 5.6 ---- SS100e profit top-up: a held coin whose since-buy
            # profit reached the configured percent gets bought MORE (the
            # amount is decided by the risk engine from free cash) — let
            # winners run with fresh money instead of only taking exits
            profit_buys = self._profit_topup_decisions(
                prices, portfolio_snapshot, cycle_id) if getattr(
                self.cfg.risk, "topup_profit_pct", 0) > 0 else []
            if profit_buys:
                exit_decisions.extend(profit_buys)
            # نردبان: strike همین چرخه -> سرمایهٔ افزوده روی همان سکه
            # (بودجهٔ پله از پول نقد آزاد، در همان چرخه)
            ladder_buys = self._ladder_topup_decisions(
                prices, portfolio_snapshot, cycle_id)
            if ladder_buys:
                exit_decisions.extend(ladder_buys)
                summary["ladder_topups"] = len(ladder_buys)
            # حد ضرر خورد -> rotate the freed cash into the best-ranked
            # opportunity in the SAME cycle (total-value growth goal)
            rotation_buys = self._rotation_buy_after_stop(exit_decisions)
            if rotation_buys:
                exit_decisions.extend(rotation_buys)
                summary["rotation_buys"] = len(rotation_buys)

            # 6 ---- AI decisions (SS31/32) with output validation (SS73)
            open_positions = {
                p["market"]: {"amount": float(d(p["amount"]))}
                for p in self.storage.positions() if d(p["amount"]) > 0}
            self.events.publish("AI_ANALYSIS_STARTED", "engine", cycle_id,
                                {"candidates": len(candidates)})
            decisions = self.ai.analyze(
                candidates, market_by_symbol, portfolio_snapshot,
                open_positions, cycle_id, prices,
                topup_allowed=bool(getattr(self.cfg.risk,
                                           "allow_topup", False)))
            decisions.extend(exit_decisions)
            # buy-the-best option (ai.buy_best): when the cycle produced no
            # BUY, synthesize one for the top-ranked market so the bot always
            # acts on its best opportunity. The risk engine still gates size,
            # real balance and min-notional (SS33/34) — this only guarantees
            # the AI proposes it. Held symbols are never force-bought.
            if (self.cfg.ai.buy_best and candidates and
                    not any(x.get("action") == "BUY" for x in decisions)):
                top = candidates[0]
                snap_top = market_by_symbol.get(top["symbol"])
                held_top = float(open_positions.get(
                    top["symbol"], {}).get("amount", 0) or 0) > 0
                # کمیتهٔ چندمدلی: «خرید بهترین گزینه» هم بدون تأیید کمیته
                # (ارزش انتظاری مثبت بعد از کارمزد) اجرا نمی‌شود؛ وگرنه این
                # مسیر، گیت کمیته را کامل دور می‌زد.
                verdict_top = {}
                committee_ok = True
                if getattr(self.cfg.ai, "committee_enabled", False):
                    try:
                        verdict_top = self.ai.committee.evaluate(
                            top, snap_top or {}, allow_llm=False)
                        committee_ok = bool(verdict_top.get("allow_buy"))
                    except Exception:
                        verdict_top, committee_ok = {}, True
                # allow_topup: a still-best-ranked HELD coin may be topped
                # up with free cash (the risk engine caps it) instead of
                # only ever buying a first position
                if snap_top and committee_ok and (not held_top or getattr(
                        self.cfg.risk, "allow_topup", False)):
                    boosted = {**top, "signals": {
                        **top["signals"],
                        "trend": max(top["signals"].get("trend", 0), 0.9),
                        "momentum": max(top["signals"].get("momentum", 0), 0.9),
                    }}
                    best = self.ai.heuristic.decide(
                        boosted, snap_top, portfolio_snapshot, open_positions,
                        prices)
                    if (best is None or best.get("action") not in
                            ("BUY", "SELL") or
                            not (best.get("entry") or {}).get("price")):
                        # the heuristic is unconvinced (HOLD/AVOID => no entry
                        # plan). Flipping the action on a plan-less decision
                        # produced a BUY with no entry/stop/TP/size, which
                        # the SS73 validation rejects EVERY cycle — so
                        # «خرید بهترین گزینه» never actually bought anything
                        # and the account sat on idle USDT. Build the same
                        # complete percentage plan the manual button uses;
                        # risk still re-sizes and re-clamps it (SS33/34).
                        best = self._synthesize_buy_plan(
                            top, snap_top, portfolio_snapshot, prices)
                    best["action"] = "BUY"
                    best["confidence"] = max(
                        float(best.get("confidence", 0) or 0), 0.75)
                    best["reason_codes"] = ["BUY_BEST_OPTION"] + list(
                        best.get("reason_codes", []))[:5]
                    if verdict_top:
                        best["committee"] = verdict_top
                        self.ai.register_verdict(top["symbol"], verdict_top)
                    decisions.append(best)
            valid_decisions = []
            for dec in decisions:
                vres = ValidationResult(ok=True)
                validate_ai_decision(dec, self._allowed_symbols(market_by_symbol), vres)
                ok = vres.ok
                self.storage.save_ai_decision(dec, ok)
                if ok:
                    valid_decisions.append(dec)
                else:
                    self.events.publish("AI_DECISION_CREATED", "engine",
                                        cycle_id,
                                        {"decision_id": dec.get("decision_id"),
                                         "action": dec.get("action"),
                                         "validation": "REJECTED"})
                    continue
                self.events.publish("AI_DECISION_CREATED", "engine", cycle_id,
                                    {"decision_id": dec.get("decision_id"),
                                     "symbol": dec.get("symbol"),
                                     "action": dec.get("action"),
                                     "confidence": dec.get("confidence")})
            self.latest_decisions = valid_decisions

            # 7 ---- risk engine (SS33/34)
            self.latest_risk = []
            self.latest_executions = []
            if self.safe_mode or not self.auto_trading:
                self.events.publish("RISK_REJECTED", "engine", cycle_id,
                                    {"reason": "SAFE_MODE" if self.safe_mode
                                     else "AUTO_TRADING_DISABLED"})
            else:
                for dec in valid_decisions:
                    if dec["action"] not in ("BUY", "SELL"):
                        continue
                    rd = self.risk.evaluate(dec, market_by_symbol.get(
                        dec["symbol"], {}), portfolio_snapshot, prices)
                    self.storage.save_risk_decision(rd)
                    self.latest_risk.append(rd)
                    if rd["status"] == "APPROVED":
                        ex = self.executor.execute(
                            dec, rd, prices, cycle_id)
                        self.latest_executions.append(ex)

            # 7.5 ---- committee scoreboard: هر BUY تأییدشده با پیش‌بینی‌اش
            # ثبت می‌شود و بعد از افق، حرکت واقعی قیمت با آن مقایسه می‌شود
            # («آیا تحلیل AI درست بود؟»). خطا هرگز چرخه را نمی‌شکند.
            try:
                self.latest_committee_score = self.ai.scoreboard_tick(
                    self.storage, prices, self.latest_executions)
            except Exception:
                pass

            # 8 ---- measure performance on closed positions (SS46)
            self._sync_exit_plans()
            self._measure_performance(prices)
            self._record_equity_point(prices)

            summary["decisions"] = len(valid_decisions)
            summary["executions"] = len(self.latest_executions)
            summary["quality_score"] = snap.get("data_quality_score")
            summary["finished_at"] = utc_now_iso()
            self._cycles_run += 1
            self._cycles_run_summary = summary
            return summary

    # ------------------------------------------------------ equity history
    def _live_price_for(self, symbol: str):
        """Live price for a market: latest snapshot first, then the
        exchange ticker (60s cache). Returns Decimal or None."""
        snap = self.latest_snapshot or {}
        for m in snap.get("markets", []):
            if m.get("symbol") == symbol and m.get("last_price"):
                try:
                    return d(str(m["last_price"]))
                except Exception:
                    pass
        px = self._price_fallback(symbol)
        if px:
            try:
                return d(str(px))
            except Exception:
                return None
        return None

    def market_universe(self, force: bool = False) -> MarketUniverse:
        """The snapshot-free catalog of EVERY market the exchange lists.

        One cached public call (Nobitex /market/stats returns all pairs) so
        holdings, stop-loss checks, manual orders and the whole-exchange
        table never depend on a snapshot file (or on the coin being inside
        the cycle's top-N trading universe). Rebound automatically when the
        exchange adapter is replaced (demo -> real hot attach).
        """
        ex = self.exchange
        uni = getattr(self, "_universe", None)
        if uni is None or self._universe_exchange is not ex:
            uni = MarketUniverse(
                ex, data_dir=self.data_dir, events=self.events,
                ttl=float(getattr(self.cfg.engine, "universe_ttl_s", 20)
                          or 20))
            self._universe = uni
            self._universe_exchange = ex
        if getattr(self.cfg.engine, "live_universe", True):
            try:
                uni.refresh(force=force)
            except Exception:
                pass
        return uni

    def live_universe_status(self) -> dict:
        """Small health block for /api/status (no exception escapes)."""
        try:
            return self.market_universe().status()
        except Exception as exc:
            return {"count": 0, "error": str(exc)}

    def _price_fallback(self, market: str):
        """Best-effort live price for a held market outside the snapshot.

        Order: the WHOLE-EXCHANGE universe (one cached call covers ~all
        markets), then the single-market ticker. A failure is only cached
        for 15 seconds so one hiccup cannot park a position for a minute.
        """
        now = datetime.now(timezone.utc)
        hit = self._ticker_cache.get(market)
        if hit and hit[0] and (now - hit[1]).total_seconds() < 60:
            return hit[0]
        px = None
        try:
            px = self.market_universe().price(market)
        except Exception:
            px = None
        if px is None:
            try:
                t = self.exchange.ticker(market) \
                    if hasattr(self.exchange, "ticker") else None
                px = t.get("last_price") if t else None
            except Exception:
                px = None
        if px:
            self._ticker_cache[market] = (px, now)
        else:
            self._ticker_cache[market] = (None, now - timedelta(seconds=45))
        return px

    def _exit_price(self, market: str, prices: dict | None = None):
        """Price to judge an exit trigger with — snapshot NOT required.

        A held coin can sit outside the cycle's snapshot universe (held
        wallets carry coins the top-N volume filter dropped, sections can
        be switched off, a fresh listing has no history). Judging the stop
        only against `prices` silently skipped those positions, so a stop
        that the dashboard showed as hit never sold anything.
        """
        if prices:
            px = prices.get(market)
            if px:
                return px
        return self._price_fallback(market)

    def _min_notional_for(self, market: str, prices: dict | None = None):
        """Exchange minimum order value expressed in the MARKET'S quote
        unit (USDT for Tether markets, Toman for Toman markets)."""
        min_usdt = d(str(getattr(self.cfg.exchange, "min_order_usdt", 0.5)))
        if market.endswith("USDT"):
            return min_usdt
        usdtirt = ZERO
        if prices:
            usdtirt = d(str(prices.get("USDTIRT") or 0))
        if usdtirt <= 0:
            try:
                usdtirt = d(str(self._price_fallback("USDTIRT") or 0))
            except Exception:
                usdtirt = ZERO
        return (min_usdt * usdtirt) if usdtirt > 0 else ZERO

    def _free_sell_amount(self, market: str, wanted):
        """How much of `market` the EXCHANGE says is free to sell right now.

        The wallet is the source of truth: the ledger can be bigger (a
        convert/sweep or a partial buy between syncs) and our own resting
        SELL orders LOCK coins — an order for more than the free balance
        comes back as ``OverValueOrder: Order Validation Failed`` and the
        stop-loss silently never executes. Cancels our own resting SELL
        orders for that market (an exit wants the position closed anyway)
        and returns the free amount. None = paper mode (no exchange truth),
        the caller keeps its ledger amount.
        """
        if not self._real_attached:
            return None
        base = (market[:-4] if market.endswith("USDT")
                else (market[:-3] if market.endswith("IRT") else market))
        if not base:
            return None

        def _read():
            try:
                doc = self.exchange.portfolio()
            except Exception:
                return None
            for a in (doc.get("assets") or []):
                if (a.get("asset") or "").upper() == base.upper():
                    return d(str(a.get("available") or 0))
            return ZERO

        free = _read()
        if free is None:
            return None          # wallet read failed: leave the ledger alone
        if free < d(str(wanted)):
            freed = False
            try:
                for o in self.exchange.open_orders(market):
                    if (o.get("side") or "").upper() != "SELL":
                        continue
                    try:
                        self.exchange.cancel_order(o["order_id"])
                        freed = True
                    except Exception:
                        pass
            except Exception:
                freed = False
            if freed:
                try:
                    self._sync_after_cancel()
                except Exception:
                    pass
                again = _read()
                if again is not None:
                    free = again
        if free <= 0:
            return ZERO
        return min(free, d(str(wanted)))

    def _convert_exit_below_min(self, market: str, reason: str,
                                notional, min_notional) -> bool:
        """Close a position the ORDER BOOK cannot accept.

        Nobitex rejects any order below 0.5 USDT (``SmallOrder: Order value
        is limited to above 0.5.``), so a micro position's stop-loss could
        never execute — it parked for 30 minutes, forever, while the loss
        kept growing.        The exchange's own convert market (the «تبدیل
        موجودی‌های خرد» path) has NO minimum, so the TRIGGERED exit is
        executed there and the loss is actually cut. Turn it off with
        risk.exit_convert_below_min=false. Note the proceeds land in Toman
        (the convert market pays out in Toman only) — that is the price of
        being able to close a position the order book refuses.

        SAFETY: this is a REAL trade (it moves real coins out of the
        wallet), so it obeys the SAME gate as every other live order —
        the real adapter must be attached AND ``exchange.live_trading``
        must be on. Without that gate a diagnostic script that merely
        CONSTRUCTED the engine (a saved login token hot-attaches the real
        adapter) could convert the user's wallet while the dashboard said
        «معامله واقعی: خاموش». A non-live engine leaves the plan parked
        for the normal 30-minute retry instead.
        """
        if not bool(getattr(self.cfg.risk, "exit_convert_below_min", True)):
            return False
        if not self._real_attached:
            return False
        if not bool(getattr(self.cfg.exchange, "live_trading", False)):
            return False

        base = (market[:-4] if market.endswith("USDT")
                else (market[:-3] if market.endswith("IRT") else ""))
        if not base or base in ("USDT", "IRT"):
            return False
        try:
            conv = self.exchange.convert_wallet_balance(base)
        except NobitexError as exc:
            self.events.error("ENGINE", "EXIT_CONVERT_FAILED", "WARNING",
                              "%s: %s" % (market, exc.code),
                              retryable=True)
            return False
        except Exception as exc:
            self.events.error("ENGINE", "EXIT_CONVERT_FAILED", "WARNING",
                              "%s: %s" % (market, str(exc)[:120]),
                              retryable=True)
            return False
        # the coins are gone from the wallet: drop the plan and the row so
        # the stop cannot fire again on an empty position
        plans = self.storage.kv_get("exit_plans", {})
        if isinstance(plans, dict):
            plans.pop(market, None)
            self.storage.kv_set("exit_plans", plans)
        pos = self.storage.position(market)
        try:
            self.storage.upsert_position(
                market, ZERO, (pos or {}).get("avg_entry") or ZERO,
                (pos or {}).get("realized_pnl") or ZERO,
                (pos or {}).get("side") or "LONG")
        except Exception:
            pass
        try:
            self.sync_real_wallet()
        except Exception:
            pass
        self.events.publish("EXIT_CONVERTED_BELOW_MIN", "engine", "", {
            "symbol": market, "base": base, "reason": reason,
            "value_quote": float(d(str(notional or 0))),
            "min_quote": float(d(str(min_notional or 0))),
            "converted": (conv or {}).get("converted_amount")})
        return True

    def _market_row(self, symbol: str) -> dict | None:
        """Market data for `symbol` WITHOUT requiring a snapshot.

        The cycle's snapshot row wins when it exists (it carries the
        collected orderbook and quality metrics); otherwise the row is
        built from the LIVE catalog so a buy/sell/exit on a held coin never
        fails with «بازار در اسنپ‌شات فعلی نیست». The row is only a carrier
        for price/quality fields — validation and the risk engine still
        gate every order (SS100).
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return None
        for m in (self.latest_snapshot or {}).get("markets", []):
            if m.get("symbol") == symbol and m.get("last_price"):
                return m
        try:
            row = self.market_universe().row(symbol)
        except Exception:
            row = None
        if not row or not row.get("last_price"):
            return None
        now = utc_now_iso()
        return {
            "symbol": symbol,
            "base_asset": row.get("base_asset"),
            "quote_asset": row.get("quote_asset"),
            "timestamp": row.get("timestamp") or now,
            "received_at": now,
            "source": "live_universe",
            "last_price": row.get("last_price"),
            "best_bid": row.get("best_bid"),
            "best_ask": row.get("best_ask"),
            "spread": row.get("spread"),
            "spread_percent": row.get("spread_percent"),
            "volume_24h": row.get("volume_24h"),
            "quote_volume_24h": row.get("quote_volume_24h"),
            "change_24h": row.get("change_24h"),
            "high_24h": row.get("high_24h"),
            "low_24h": row.get("low_24h"),
            "tradable": row.get("tradable", True),
            "orderbook": {"bids": [], "asks": [],
                          "spread": row.get("spread"),
                          "imbalance": 0.0, "depth_levels": 0},
            "trades": [],
            "ohlc": {"timeframe": "30m", "candles": []},
            "liquidity": {}, "volatility": {},
            "data_quality": {"status": "WARNING", "score": 0.6,
                             "issues": ["LIVE_UNIVERSE_ROW"]},
        }

    def _allowed_symbols(self, market_by_symbol: dict) -> set:
        """Symbols a decision may reference (SS75): the cycle's snapshot
        PLUS the exchange's live catalog, the registry and every held
        market.

        Validating against the snapshot alone marked perfectly valid exit
        decisions on HELD coins UNKNOWN_MARKET and dropped them before the
        risk engine ever saw them — one of the reasons «حد ضرر» did not
        sell (documented live: 24 UNIUSDT EXIT_STOP decisions created and
        silently discarded).
        """
        allowed = set(k for k in (market_by_symbol or {}).keys() if k)
        try:
            allowed |= set(self.collector.registry.all_symbols())
        except Exception:
            pass
        try:
            allowed |= self.market_universe().symbols()
        except Exception:
            pass
        try:
            for p in self.storage.positions():
                mkt = p.get("market") or ""
                if mkt and d(p.get("amount") or 0) > 0:
                    allowed.add(mkt)
                    allowed |= set(MarketUniverse.alternates(mkt))
        except Exception:
            pass
        return {s for s in allowed if s}

    def _augment_prices_with_holdings(self, prices: dict) -> dict:
        """Add a live price for EVERY held market the snapshot missed.

        Called on the cycle's price map: exits, valuation and the AI all
        then see the coins the wallet actually holds, whether or not the
        snapshot universe included them this cycle (one cached whole-
        exchange call, no per-market request).
        """
        try:
            held = [p["market"] for p in self.storage.positions()
                    if d(p["amount"]) > 0 and p.get("market")]
        except Exception:
            return prices
        for market in held:
            if prices.get(market):
                continue
            px = self._price_fallback(market)
            if px:
                prices[market] = px
        return prices

    def _known_deposited_irt(self) -> float:
        """کل سرمایه واریزی, preferring the REAL Nobitex turnover.

        Σdeposits − Σwithdraws from the exchange (hourly-cached, so the
        common call makes no extra network request) with the stored/manual
        value as fallback. Used to separate real 24h profit from the user
        moving money in/out: a deposit is NOT profit.
        """
        val = 0.0
        try:
            nd = self._net_deposited_from_exchange()
            net = nd.get("net_irt")
            # any non-zero live read counts, INCLUDING a negative net:
            # after more money was taken out than put in, «سود سرمایه» =
            # ارزش حساب − کل سرمایه واریزی is still the right number (and
            # the 24h box would rather wait than use a stale manual value).
            if net is not None and float(net) != 0.0:
                val = float(net)
        except Exception:
            val = 0.0
        if val <= 0:
            val = self.get_deposited_capital()
        return float(val)

    def _migrate_equity_history(self, hist: list,
                                irt_per_usdt: float) -> tuple[list, bool]:
        """Normalize the equity history to TOMAN (unit marker u="irt").

        Points written before the 24h KPI reported an AMOUNT stored the
        snapshot's equity in USDT (no "u" field). They are converted once
        with the live rate so the history can never mix units (a 15k USDT
        point next to a 1.4B Toman one would fake a huge loss). A point we
        cannot convert (no rate available) is DROPPED — showing no number
        is better than a wrong one.
        """
        changed = False
        out: list = []
        for pt in hist if isinstance(hist, list) else []:
            if not isinstance(pt, dict):
                continue
            if pt.get("u") == "irt":
                out.append(pt)
                continue
            try:
                e_usdt = float(pt.get("e") or 0)
                e_irt = e_usdt * irt_per_usdt if irt_per_usdt > 0 else 0.0
            except Exception:
                e_irt = 0.0
            changed = True
            if e_irt <= 0:
                continue
            out.append({"t": pt.get("t"), "e": round(e_irt, 2),
                        "d": float(pt.get("d") or 0),
                        "a": int(pt.get("a") or 0), "u": "irt"})
        return out, changed

    def _record_equity_point(self, prices: dict) -> None:
        """Compact rolling equity history (per cycle) for the 24h profit.

        Stored in kv_state as [{t, e, d, a, u}] — t = time, e = account
        equity in TOMAN (from the REAL wallet whenever the bot is attached
        to the account), d = کل سرمایه واریزی at that moment (real exchange
        turnover), a = 1 when the point came from the real account,
        u = unit (always "irt"; legacy USDT points are migrated here).
        Throttled to one point per minute so 24h needs no pruning beyond
        the cap, and pruned by TIME (26h) so a days-old point can never
        become the «سود ۲۴ساعت» baseline.
        """
        try:
            prices_all = dict(prices)
            rates = self._usdt_rates(prices_all)
            ps = self.portfolio.portfolio_snapshot(prices_all, rates)
            irt_rate = float(rates.get("IRT") or 0)
            # Toman equity (the KPI reports Toman amounts)
            equity = float((ps.get("account") or {}).get("equity_irt") or 0)
            if equity <= 0 and irt_rate > 0:
                equity = float((ps.get("risk") or {})
                               .get("total_equity") or 0) * irt_rate
            if equity <= 0:
                return
            deposited = self._known_deposited_irt()
            real = 1 if self._real_attached else 0
            hist = self.storage.kv_get("equity_history", [])
            if not isinstance(hist, list):
                hist = []
            hist, _migrated = self._migrate_equity_history(
                hist, 1.0 / irt_rate if irt_rate > 0 else 0.0)
            now_s = utc_now_iso()
            point = {"t": now_s, "e": round(equity, 2),
                     "d": round(deposited, 2), "a": real, "u": "irt"}
            # throttle: at most one point per minute
            if hist and (hist[-1].get("t", "")[:16] == now_s[:16]):
                hist[-1] = point
            else:
                hist.append(point)
            # TIME-based prune: keep only the last 26h so a sparse history
            # (engine off for days) can never offer a days-old point as
            # the «سود ۲۴ساعت» baseline — the count cap alone allowed
            # that when points were 1-per-cycle instead of 1-per-minute.
            cutoff = datetime.now(timezone.utc) - timedelta(hours=26)
            def _fresh(pt):
                try:
                    return parse_iso(str(pt.get("t", ""))) >= cutoff
                except Exception:
                    return False
            hist = [pt for pt in hist if isinstance(pt, dict) and
                    _fresh(pt)]
            # 1 point/minute (throttled) x 26h keeps a REAL 24h-ago
            # reference point available for the «سود ۲۴ساعت» KPI
            self.storage.kv_set("equity_history", hist[-1800:])
        except Exception:
            pass

    def _profit_24h(self, eq_irt: float, hist: list,
                    deposited_now: float | None = None,
                    real_now: bool | None = None) -> dict:
        """«سود ۲۴ساعت» — the CHANGE of «میزان سود سرمایه» over the last
        24h — and nothing else — computed SERVER-SIDE so the dashboard
        never invents a baseline.

        «میزان سود سرمایه» is the very line the «سود کل» card shows below
        its amount: `ارزش حساب − کل سرمایه واریزی`. So this box compares
        that number with its own value at the window's baseline point —
        NOT the raw account-value difference (moving money in/out used to
        look like profit).

        Reports the AMOUNT, not just a percent:
        • capital_now_irt / capital_ref_irt — سود سرمایه now / at the
          baseline point (the two lines the KPI shows)
        • flow_irt  — net money the user moved in/out inside the window
          (real Nobitex turnover: Σdeposits − Σwithdraws). Capital, not
          profit.
        • pnl_irt   — capital_now − capital_ref = the real window profit
        • delta_irt — raw account-value change, kept for auditing
        • pct       — pnl_irt over کل سرمایه واریزی (the SAME denominator
          the «سود کل» card uses) when both ends know the deposits, else
          over the baseline account value
        • basis — always "capital" in a reported payload: a missing
          deposit figure makes the box wait (MISSING_DEPOSIT_DATA) rather
          than report the raw account delta as if it were capital profit
        • base_irt / total_irt / deposited_irt — account value at the
          baseline, now, and deposited capital now

        A point recorded while the engine was attached to the REAL wallet
        and one recorded on the paper/demo ledger are two different
        accounts: `real_now` filters the history so only points of the
        SAME account can become the baseline (a demo point next to a real
        balance used to fake a −99% loss).

        STRICTLY 24 HOURS (user request): the box reports a number ONLY
        when a real point of this account exists ≥24h ago — the change of
        «میزان سود سرمایه» over exactly 24 hours. A shorter window is
        NEVER substituted (the old fallback reported the change “from the
        start of the account”, which is not a 24-hour number); when the
        baseline is missing the box says so and waits.

        coverage:
        • "exact"   — a real point exists within [24h, 26h) ago (the one
          CLOSEST to the 24h mark, so the window never runs past the cap);
          `ref_age_h` reports its real age
        • "waiting" — no 24h-old point of this account yet. `needs_h`
          estimates how many hours until a real 24h baseline exists,
          waiting_reason says WHY:
            · "NO_HISTORY"          — no equity point recorded at all
            · "HISTORY_SHORTER_THAN_24H" — history exists (`start_age_h`)
              but it is younger than 24h
            · "MISSING_DEPOSIT_DATA" — a 24h-old point exists but one end
              lacks کل سرمایه واریزی, so «سود سرمایه» cannot be formed
              (the raw account delta is kept in `audit` only — never
              shown as if it were capital profit)
          `skipped_other_source` counts points dropped because they belong
          to the other account (demo vs real)
        """
        out = {"pct": None, "coverage": "waiting"}
        if not isinstance(hist, list):
            hist = []
        now = datetime.now(timezone.utc)

        def _age(pt: dict):
            try:
                return (now - parse_iso(str(pt.get("t", "")))).total_seconds() / 3600.0
            except Exception:
                return None

        # fresh points of THIS account only (self-heal a sparse old file
        # on read too). A point with no "a" field (legacy) is accepted —
        # we cannot tell its source, and refusing it would blind the KPI.
        fresh = []
        skipped_other_source = 0
        for pt in hist:
            if not isinstance(pt, dict):
                continue
            a = _age(pt)
            if a is None or not (0 <= a <= 26):
                continue
            if real_now is not None and "a" in pt \
                    and bool(pt.get("a")) != bool(real_now):
                skipped_other_source += 1
                continue
            fresh.append((a, pt))
        fresh.sort(key=lambda x: x[0])
        ref = None
        ref_age = None
        coverage = "waiting"
        for a, pt in fresh:          # chronological: first point ≥24h old
            if a >= 24:
                ref, ref_age, coverage = pt, a, "exact"
                break
        # only the exact 24h baseline: no “since account start” substitute
        # (the window must be 24 hours long or the box waits instead)
        if ref is not None and eq_irt > 0:
            ref_e = float(ref.get("e") or 0)
            if ref_e > 0:
                delta = eq_irt - ref_e
                ref_d = float(ref.get("d") or 0)
                dep = float(deposited_now or 0)
                # سود سرمایه = ارزش حساب − کل سرمایه واریزی, the same number
                # the «سود کل» card prints under its amount. Both ends are
                # compared ONLY when BOTH know the deposited capital: a
                # missing figure must never be read as 0 (that would fake
                # profit/loss), and the raw delta is used instead.
                cap_now = (eq_irt - dep) if dep > 0 else None
                cap_ref = (ref_e - ref_d) if (ref_d > 0 and dep > 0) else None
                basis = ("capital" if (cap_now is not None
                                       and cap_ref is not None)
                         else "account")
                if basis != "capital":
                    # بدون «سود سرمایه» در یکی از دو سر بازه، این باکس
                    # عدد نمی‌سازد: تغییر خام ارزش حساب با واریز/برداشت
                    # قاطی می‌شود و سود سرمایه نیست. فقط برای حسابرسی
                    # نگه داشته می‌شود و کاربر «در انتظار» می‌بیند.
                    out["waiting_reason"] = "MISSING_DEPOSIT_DATA"
                    out["needs_h"] = 0.0
                    out["real"] = bool(ref.get("a"))
                    out["ref_age_h"] = round(ref_age, 2)
                    out["ref_at"] = ref.get("t")
                    out["audit"] = {"delta_irt": round(delta, 2),
                                    "base_irt": round(ref_e, 2),
                                    "total_irt": round(eq_irt, 2),
                                    "deposited_irt": round(dep, 2),
                                    "ref_deposited_irt": round(ref_d, 2)}
                    out["skipped_other_source"] = skipped_other_source
                    return out
                flow = dep - ref_d
                pnl = cap_now - cap_ref
                # denominator: کل سرمایه واریزی (same basis as «سود کل»)
                denom = dep if dep > 0 else ref_e
                out = {
                    "pct": round(pnl / denom * 100, 2),
                    "pct_raw": round(delta / ref_e * 100, 2),
                    "pct_base": ("deposited" if denom == dep and dep > 0
                                 else "equity"),
                    "basis": basis,
                    "pnl_irt": round(pnl, 2),
                    "delta_irt": round(delta, 2),
                    "flow_irt": round(flow, 2),
                    "capital_now_irt": (round(cap_now, 2)
                                        if cap_now is not None else None),
                    "capital_ref_irt": (round(cap_ref, 2)
                                        if cap_ref is not None else None),
                    "base_irt": round(ref_e, 2),
                    "total_irt": round(eq_irt, 2),
                    "deposited_irt": round(dep, 2),
                    "ref_deposited_irt": round(ref_d, 2),
                    "real": bool(ref.get("a")),
                    "ref_equity": ref_e,
                    "ref_age_h": round(ref_age, 2),
                    "ref_at": ref.get("t"),
                    "coverage": coverage,
                    "skipped_other_source": skipped_other_source,
                }
                return out
        if ref is None:
            # چرا عددی نیست؟ تا کاربر بداند «منتظر» یعنی چه — چه تاریخچه
            # وجود ندارد، چه کوتاه‌تر از ۲۴ ساعت است. `fresh` صعودی بر
            # اساس سن مرتب شده، پس OLDEST (شروع تاریخچهٔ همین حساب) آخرین
            # عضو است — نه اولین (که تازه‌ترین نقطه است). منتظرِ ماندنِ
            # باکس به سن **قدیمی‌ترین** نقطه بستگی دارد: همان لحظه که به
            # ۲۴ ساعت برسد، مبنای واقعی وجود دارد.
            oldest = fresh[-1][0] if fresh else None
            out["waiting_reason"] = ("HISTORY_SHORTER_THAN_24H" if fresh
                                    else "NO_HISTORY")
            if oldest is not None:
                out["start_age_h"] = round(oldest, 2)
        out["needs_h"] = round(max(0.0, 24.0 - oldest), 1) \
            if oldest is not None else 24.0
        out["skipped_other_source"] = skipped_other_source
        return out

    # ------------------------------------------------------------- exits
    def _run_exit_cycle(self, cycle_id: str) -> list:
        """Run ONLY the exit pipeline (backstop + ratchet + stop/TP) and
        execute the resulting SELLs through validation -> risk -> executor.

        The analysis pipeline has early returns (no eligible market, every
        candidate regime-blocked). Those branches used to skip step 5.5
        entirely, so a market-wide crash — exactly when the regime gate
        empties the candidate list — left stop-losses unevaluated. Exits
        are risk-reducing and must never depend on a buy opportunity.
        Returns the executions that were actually sent.
        """
        snap = self.latest_snapshot or {}
        prices = {m["symbol"]: m["last_price"]
                  for m in (snap.get("markets") or [])
                  if m.get("last_price")}
        try:
            self._augment_prices_with_holdings(prices)
        except Exception:
            pass
        try:
            self._backstop_exit_plans(prices)
        except Exception:
            pass
        try:
            self._advance_profit_locks(prices)
        except Exception:
            pass
        exit_decisions = self._manage_exits(prices, cycle_id)
        if not exit_decisions:
            return []
        market_by_symbol = {m["symbol"]: m
                            for m in (snap.get("markets") or [])}
        try:
            portfolio_snapshot = self.portfolio.portfolio_snapshot(
                prices, self._usdt_rates(prices))
        except Exception:
            portfolio_snapshot = {}
        executed = []
        for dec in exit_decisions:
            vres = ValidationResult(ok=True)
            validate_ai_decision(dec, self._allowed_symbols(market_by_symbol),
                                 vres)
            self.storage.save_ai_decision(dec, vres.ok)
            if not vres.ok:
                self.events.publish("EXIT_VALIDATION_FAILED", "engine",
                                    cycle_id,
                                    {"symbol": dec.get("symbol"),
                                     "code": vres.first_code()})
                continue
            if self.safe_mode or not self.auto_trading:
                continue
            rd = self.risk.evaluate(dec, market_by_symbol.get(
                dec["symbol"], {}), portfolio_snapshot, prices)
            self.storage.save_risk_decision(rd)
            self.latest_risk.append(rd)
            if rd["status"] == "APPROVED":
                executed.append(self.executor.execute(
                    dec, rd, prices, cycle_id))
        self._sync_exit_plans()
        return executed

    def _manage_exits(self, prices: dict, cycle_id: str) -> list:
        """Monitor open positions against their stop/TP plan (SS36/37).

        Stop hit -> close the whole remaining position.
        TP stage hit -> close that stage's close_percent of the remainder.
        Returns synthesized SELL decisions that flow through the normal
        validation -> risk -> execution pipeline (nothing bypasses SS100).
        """
        plans = self.storage.kv_get("exit_plans", {})
        if not isinstance(plans, dict):
            plans = {}
        out = []
        for market, plan in list(plans.items()):
            if not self._quote_market_enabled(market):
                # «بازار معاملات»: this section is switched OFF, so the bot
                # must not trade there — not even to close an old position.
                # The plan stays armed and resumes the moment the section
                # is switched back on; the user can always sell the row by
                # hand from the dashboard.
                continue
            pos = self.storage.position(market)
            if not pos or d(pos["amount"]) <= 0:
                # triggers are holding-based now; nothing to do without coins
                continue
            # حد ضرر/حد سود با قیمتِ زندهٔ خود سکه سنجیده می‌شود، نه فقط با
            # اسنپ‌شات: کوینِ نگه‌داشته‌شده‌ای که در سبد چرخه نیست (فیلتر
            # حجم، بخش خاموش، لیست‌شدن تازه) قبلاً بی‌قیمت می‌ماند و حد
            # ضررش هیچ‌وقت اجرا نمی‌شد.
            price = prices.get(market) or self._exit_price(market, prices)
            if not price:
                continue   # no price anywhere: leave the plan armed
            # a position worth less than the exchange minimum can never be
            # sold — retry no sooner than 30 min instead of spamming the
            # adapter with REJECTEDs every cycle
            bmu = plan.get("below_min_until_ts")
            if bmu:
                try:
                    if parse_iso(bmu) > datetime.now(timezone.utc):
                        continue
                except Exception:
                    pass
            amount = d(pos["amount"])
            price_d = d(str(price))
            tps = plan.get("tps") or []
            stage = int(plan.get("stage", 0))
            triggered_pct = 0
            while stage < len(tps) and price_d >= d(str(tps[stage]["price"])):
                triggered_pct += int(tps[stage].get("close_percent", 0))
                stage += 1
            stop = d(str(plan.get("stop", 0)))
            stop_hit = stop > 0 and price_d <= stop
            # manual take-profit: ride the candle to its top, sell on the rise
            tp_manual = d(str(plan.get("take_profit_manual", 0)))
            tp_hit = tp_manual > 0 and price_d >= tp_manual
            # حالت ریسک — فروش هنگام افت: از سقفِ قیمت از لحظه خرید،
            # افت بیشتر از risk_mode_drop_exit_pct (پیش‌فرض ‎-6٪) یعنی
            # روند روزه برگشته: خروج کامل همان چرخه (trailing از سقف)
            drop_hit = False
            if self.risk_mode:
                drop_pct = d(str(getattr(self.cfg.risk,
                                         "risk_mode_drop_exit_pct", -6.0)
                                  or 0))
                if drop_pct < 0:
                    peak = d(str(plan.get("risk_mode_peak") or 0))
                    if price_d > peak:
                        plan["risk_mode_peak"] = float(price_d)
                        peak = price_d
                    drop_hit = peak > 0 and \
                        price_d <= peak * (d("1") + drop_pct / d("100"))
            if not triggered_pct and not stop_hit and not tp_hit \
                    and not drop_hit:
                # سقف (peak) ممکن است همین چرخه به‌روز شده باشد — ذخیره
                # تا ردگیری افت از سقف واقعی ادامه یابد
                if "risk_mode_peak" in plan:
                    self.storage.kv_set("exit_plans", plans)
                continue
            sell_pct = 100 if (stop_hit or tp_hit or drop_hit) \
                else min(100, triggered_pct)
            sell_amount = amount * Decimal(sell_pct) / Decimal(100)
            reason = ("EXIT_STOP" if stop_hit else
                      "EXIT_DROP" if drop_hit else
                      "EXIT_TAKE_PROFIT" if triggered_pct else
                      "EXIT_PROFIT_TARGET")
            # the EXCHANGE is the source of truth for how much is free to
            # sell right now: our own resting SELL orders lock coins and the
            # ledger can be bigger than the wallet, so an order for the
            # whole position came back as «OverValueOrder: Order Validation
            # Failed» every cycle while the price kept falling.
            try:
                free_amt = self._free_sell_amount(market, sell_amount)
            except Exception:
                free_amt = None
            if free_amt is not None:
                if free_amt <= 0:
                    # کیف پول واقعی این سکه را ندارد: ردیف دفتر کهنه است —
                    # صفر و پلن پاک می‌شود تا هر چرخه بی‌فایده تلاش نکند
                    plans.pop(market, None)
                    self.storage.kv_set("exit_plans", plans)
                    try:
                        self.storage.upsert_position(
                            market, ZERO, pos.get("avg_entry") or ZERO,
                            pos.get("realized_pnl") or ZERO,
                            pos.get("side") or "LONG")
                    except Exception:
                        pass
                    self.events.publish("EXIT_STALE_POSITION_CLEARED",
                                        "engine", cycle_id,
                                        {"symbol": market,
                                         "reason": reason})
                    continue
                sell_amount = free_amt
            notional = sell_amount * price_d
            # حداقل سفارش صرافی (زنده: «SmallOrder: Order value is limited
            # to above 0.5») در واحد خودِ بازار: سفارش زیر آن فقط REJECTED
            # می‌گیرد. اما موقعیتِ کوچک هم نباید بی‌پناه بماند — از مسیر
            # «تبدیل» صرافی (بدون کمینه) فروخته می‌شود تا حد ضرر واقعاً
            # اجرا شود؛ وگرنه به‌مدت ۳۰ دقیقه پارک می‌شد و هیچ‌وقت نمی‌فروخت.
            min_notional = self._min_notional_for(market, prices)
            if (sell_amount <= 0 or notional < Decimal("1")
                    or (min_notional > 0 and notional < min_notional)):
                if self._convert_exit_below_min(market, reason, notional,
                                                min_notional):
                    # the conversion closed the position on the exchange:
                    # drop the plan from THIS loop's copy too, or the next
                    # decision written from it would resurrect the plan of
                    # a market we no longer hold
                    plans.pop(market, None)
                    continue
                until = datetime.now(timezone.utc) + timedelta(seconds=1800)
                plans[market]["below_min_until_ts"] = utc_iso(until)
                self.storage.kv_set("exit_plans", plans)
                continue
            now_dt = datetime.now(timezone.utc)
            dec = {
                "decision_id": new_id("dec"),
                "snapshot_id": (self.latest_snapshot or {}).get(
                    "snapshot_id", ""),
                "cycle_id": cycle_id,
                "symbol": market,
                "action": "SELL",
                "confidence": 0.9,
                "time_horizon": "INTRADAY",
                "entry": {"price": float(price_d), "min": float(price_d),
                          "max": float(price_d)},
                "stop_loss": {"price": float(stop) if stop else float(price_d),
                              "method": "MARKET_STRUCTURE",
                              "reason": reason},
                "take_profit": [{"price": float(price_d),
                                 "close_percent": 100}],
                "position_size": {"amount": float(sell_amount),
                                  "quote_value": float(sell_amount * price_d)},
                "risk": {"max_loss": 0.0, "risk_reward": 0.0},
                "cost": {"estimated_total_cost": self.cfg.fee.taker_rate,
                         "expected_gross_return": 0.0,
                         "expected_net_return": 0.0},
                "reason_codes": [reason],
                "exit": True,
                "created_at": utc_iso(now_dt),
                "expires_at": utc_iso(now_dt + timedelta(
                    seconds=self.cfg.risk.decision_ttl_s)),
            }
            plans[market]["stage"] = stage
            if stop_hit:
                # حد ضرر خورد: lock THIS market against an instant re-buy
                # (the loss must not be doubled by re-entering the same
                # dip) and remember the hadd the ratchet had armed — the
                # market becomes buyable again once the price REACHES that
                # next strike (or the rotate cooldown elapses). The freed
                # cash rotates into the current best opportunity in this
                # same cycle (see _rotation_buy_after_stop).
                until = datetime.now(timezone.utc) + timedelta(
                    seconds=max(300, int(getattr(
                        self.cfg.risk, "rotate_cooldown_s", 120) or 0)))
                plans[market]["stop_lock_until_ts"] = utc_iso(until)
                plans[market]["stop_lock_hadd"] = plan.get("locked_stop")
            self.storage.kv_set("exit_plans", plans)
            out.append(dec)
        return out

    def _rotation_buy_after_stop(self, exit_decisions: list) -> list:
        """حد ضرر خورد -> rotate the freed cash into the BEST ranked
        opportunity NOW (same cycle), so total account value keeps
        growing instead of the money sleeping in IRT after a losing
        exit. One rotation BUY per cycle, only when the exit list actually
        contains a stop-loss SELL; the risk engine still gates size, cash
        and the STOP_EXIT_LOCKOUT of the exited market itself."""
        try:
            if not getattr(self.cfg.risk, "rotate_after_stop", True):
                return []
            # حد ضرر (EXIT_STOP) یا فروش هنگام افت در حالت ریسک (EXIT_DROP)
            # -> آزادشده سرمایه در همین چرخه به بهترین فرصت فعلی می‌رود
            if not any("EXIT_STOP" in (x.get("reason_codes") or []) or
                       "EXIT_DROP" in (x.get("reason_codes") or [])
                       for x in exit_decisions):
                return []
            if not (self.cfg.ai.buy_best and self.latest_candidates and
                    self.latest_snapshot):
                return []
            snap = self.latest_snapshot
            market_by_symbol = {
                m["symbol"]: m for m in snap.get("markets", [])
                if (m.get("data_quality") or {}).get("score", 0)
                >= self.cfg.quality.min_trade_quality}
            prices = {m["symbol"]: m["last_price"]
                      for m in snap.get("markets", []) if m.get("last_price")}
            portfolio_snapshot = self.portfolio.portfolio_snapshot(
                prices, self._usdt_rates(prices))
            open_positions = {
                p["market"]: {"amount": float(d(p["amount"]))}
                for p in self.storage.positions() if d(p["amount"]) > 0}
            for top in self.latest_candidates[:5]:
                sym = top["symbol"]
                snap_top = market_by_symbol.get(sym)
                if not snap_top or sym in open_positions:
                    continue
                boosted = {**top, "signals": {
                    **top["signals"],
                    "trend": max(top["signals"].get("trend", 0), 0.9),
                    "momentum": max(top["signals"].get("momentum", 0),
                                    0.9),
                }}
                dec = self.ai.heuristic.decide(
                    boosted, snap_top, portfolio_snapshot, open_positions,
                    prices)
                dec["action"] = "BUY"
                dec["confidence"] = max(float(dec.get("confidence", 0)
                                              or 0), 0.75)
                dec["reason_codes"] = ["STOP_EXIT_ROTATION"] + list(
                    dec.get("reason_codes", []))[:5]
                return [dec]
            return []
        except Exception:
            return []

    def _sync_exit_plans(self) -> None:
        """Drop exit plans whose position is fully closed."""
        plans = self.storage.kv_get("exit_plans", {})
        if not isinstance(plans, dict) or not plans:
            return
        changed = False
        for market in list(plans.keys()):
            pos = self.storage.position(market)
            if not pos or d(pos["amount"]) <= 0:
                plans.pop(market)
                changed = True
        if changed:
            self.storage.kv_set("exit_plans", plans)

    def _backstop_exit_plans(self, prices: dict) -> int:
        """Profit-protection backstop: NO held coin may stay without a
        sell stop. Coins reaching the ledger outside a tracked buy (wallet
        sync, manual exchange buys, imported dust) never went through
        _auto_stop_for_buy and sat unprotected — in the live DB three
        real coins (~292k Toman) had no stop. Arms the fixed default
        stop from the position's average entry once, marks it `backstop`
        so a later manual edit always wins, and gives the plan its HADD
        (strike base) so the row reads «قیمت حد (0)»."""
        armed = 0
        plans = self.storage.kv_get("exit_plans", {})
        if not isinstance(plans, dict):
            plans = {}
        changed = False
        for pos in self.storage.positions():
            market = pos["market"]
            if d(pos["amount"]) <= 0:
                continue
            plan = plans.get(market)
            entry = d(str(pos.get("avg_entry") or 0))
            if entry <= 0:
                # wallet-imported coins (bought on the exchange BEFORE the
                # bot ever ran) have no recorded entry — arm from the LIVE
                # price instead of leaving them naked forever
                cur = prices.get(market) or self._price_fallback(market)
                try:
                    cur_d = d(str(cur)) if cur else None
                except Exception:
                    cur_d = None
                if not cur_d or cur_d <= 0:
                    continue
                entry = cur_d
            if isinstance(plan, dict) and (plan.get("stop") or
                                           plan.get("backstop")):
                # already protected: only give it a HADD when it has none
                # (plans armed by older versions read «قیمت حد —» and
                # their percents had no strike base to measure against)
                if d(str(plan.get("base_price", 0) or 0)) <= 0 and \
                        d(str(plan.get("locked_stop", 0) or 0)) <= 0:
                    plan["base_price"] = float(entry)
                    plans[market] = plan
                    changed = True
                # legacy plans (armed before risk mode) get their drop-exit
                # anchor lazily from the entry the first time risk mode runs
                if self.risk_mode and "risk_mode_peak" not in plan:
                    plan["risk_mode_peak"] = float(entry)
                    plans[market] = plan
                    changed = True
                continue
            plan = dict(plan or {})
            fixed = d(str(getattr(self.cfg.risk,
                                  "default_stop_loss_pct", -2.0) or 0))
            frac = abs(fixed) / d("100") if fixed < 0 else d("0.02")
            stop = (entry * (1 - frac)).quantize(Decimal("0.000001"))
            plan["stop"] = float(stop)
            plan["stop_pct"] = float((-frac * 100).quantize(Decimal("0.1")))
            plan["backstop"] = True
            plan["base_price"] = float(entry)
            plan.setdefault("stage", 0)
            # risk-mode drop-exit needs a peak anchor too (from the entry)
            if self.risk_mode:
                plan.setdefault("risk_mode_peak", float(entry))
            plans[market] = plan
            changed = True
            armed += 1
            self.events.publish("BACKSTOP_ARMED", "engine", "",
                                {"symbol": market, "stop": float(stop),
                                 "entry": float(entry)})
        if changed:
            self.storage.kv_set("exit_plans", plans)
        return armed

    # ------------------------------------------------------ performance
    def _measure_performance(self, prices: dict) -> None:
        """SS46: when a position closes, attribute return to its decisions."""
        rows = self.storage.kv_get("perf_pending", {})
        if not isinstance(rows, dict):
            rows = {}
        changed = False
        for pos in self.storage.positions():
            market = pos["market"]
            if d(pos["amount"]) == 0 and market in rows:
                info = rows.pop(market)
                changed = True
                entry = d(info["entry_price"])
                exit_p = d(str(prices.get(market) or info.get("last_price") or 0))
                actual = float((exit_p - entry) / entry) if entry else 0
                pnl = info.get("realized_pnl", 0)
                self.storage.save_performance({
                    "decision_id": info["decision_id"],
                    "symbol": market,
                    "action": info.get("action", "BUY"),
                    "expected_return": info.get("expected_return"),
                    "actual_return": round(actual, 6),
                    "actual_pnl": pnl,
                    "direction_correct": (actual > 0) ==
                                         (info.get("action") == "BUY"),
                    "profitable": float(pnl) > 0,
                })
        for market in rows:
            if d(str(prices.get(market, 0))) > 0 and rows[market].get("entry_price"):
                rows[market]["last_price"] = prices[market]
                pos = self.storage.position(market)
                if pos:
                    rows[market]["realized_pnl"] = float(d(pos["realized_pnl"]))
                changed = True
        # register newly opened positions for future measurement
        for pos in self.storage.positions():
            market = pos["market"]
            if d(pos["amount"]) > 0 and market not in rows:
                decs = [x for x in self.storage.decisions_recent(30)
                        if x["symbol"] == market and
                        x["action"] in ("BUY", "SELL")]
                if decs:
                    dec = decs[0]
                    rows[market] = {
                        "decision_id": dec["decision_id"],
                        "entry_price": d(pos["avg_entry"]),
                        "action": dec["action"],
                        "expected_return": (dec.get("payload", {}).get(
                            "cost", {}) or {}).get("expected_net_return"),
                        "realized_pnl": 0,
                    }
                    changed = True
        if changed or rows:
            self.storage.kv_set("perf_pending", rows)

    # ------------------------------------------------------------ UI data
    def status(self) -> dict:
        snap = self.latest_snapshot or {}
        prices = {}
        for m in snap.get("markets", []):
            if m.get("last_price"):
                prices[m["symbol"]] = m["last_price"]
        portfolio_snapshot = self.portfolio.portfolio_snapshot(
            prices, self._usdt_rates(prices))
        # holdings box: live price even for coins outside the snapshot
        # universe (top-N by volume) — without this their profit/value
        # would show «—» although the coins are real money
        for p in self.storage.positions():
            mk = p["market"]
            if d(p["amount"]) > 0 and mk not in prices and \
                    mk not in ("USDT", "IRT"):
                px = self._price_fallback(mk)
                if px:
                    prices[mk] = px
        snap_dir = Path(self.data_dir) / "snapshots"
        try:
            snap_count = len(list(snap_dir.glob("snapshot_*.json"))) \
                if snap_dir.exists() else 0
        except OSError:
            snap_count = 0
        # Toman-per-USDT for the UI's dollar display mode: USDT-market
        # prices arrive already in plain dollars, so only IRT-market Toman
        # prices and Toman cash need dividing by this rate.
        irt_rate = float(self._usdt_rates(prices).get("IRT") or 0)
        usdt_per_irt = (1.0 / irt_rate) if irt_rate > 0 else 0.0
        # «سود ۲۴ساعت» input: the WHOLE account (کل سرمایه واریزی + سود)
        # in Toman, plus its history normalized to the same unit (legacy
        # USDT points migrated once here so numbers can never mix units)
        equity_24h_irt = float((portfolio_snapshot.get("account") or {})
                               .get("equity_irt") or 0)
        if equity_24h_irt <= 0 and irt_rate > 0:
            equity_24h_irt = float((portfolio_snapshot.get("risk") or {})
                                   .get("total_equity") or 0) * irt_rate
        # the deposit-cache migration + recent-history repair must run
        # BEFORE the 24h history is read: the first poll after this upgrade
        # would otherwise compare the new correct capital with a baseline
        # built from the old truncated turnover list (a fake one-day loss)
        try:
            self._net_deposited_from_exchange()
        except Exception:
            pass
        hist_24h = self.storage.kv_get("equity_history", [])[-1800:]
        hist_24h, migrated_24h = self._migrate_equity_history(
            hist_24h, 1.0 / irt_rate if irt_rate > 0 else 0.0)
        if migrated_24h:
            self.storage.kv_set("equity_history", hist_24h)
        # «سود کل» inputs are computed BEFORE the payload is assembled so the
        # «سود ۲۴ساعت» box compares the VERY SAME numbers: both cards print
        # «میزان سود سرمایه», and two independent valuations inside one
        # payload made them disagree by a cent while the market moved.
        hp_now = self._holdings_pnl(prices, portfolio_snapshot)
        return {
            "schema_version": "1.0",
            "usdt_per_irt": usdt_per_irt,
            "generated_at": utc_now_iso(),
            "mode": self.cfg.exchange.mode,
            "running": self.running(),
            "auto_trading": self.auto_trading,
            "buy_best": bool(getattr(self.cfg.ai, "buy_best", False)),
            "live_trading": bool(getattr(self.cfg.exchange, "live_trading",
                                         False)),
            "executions": self.latest_executions,
            "real_attached": self._real_attached,
            "trading_real": (self._real_attached and
                             bool(getattr(self.cfg.exchange,
                                          "live_trading", False))),
            "real_account": {
                "attached": self._real_attached,
                "login": self.login_status(),
            },
            "safe_mode": self.safe_mode,
            "safe_mode_reasons": self.safe_mode_reasons,
            "risk_mode": self.risk_mode,
            # Two OPERATING PROFILES, mutually exclusive by definition:
            #   safe = risk_mode OFF (the conservative profile the dashboard
            #          shows as «حالت امن»), risk = risk_mode ON.
            # NOTE this is NOT the safety halt above — `safe_mode` stays the
            # engine's own fail-safe (errors/drawdown) and is reported
            # separately so the two can never be confused in the UI.
            "profile": "risk" if self.risk_mode else "safe",
            "safe_profile": not self.risk_mode,
            "risk_mode_switches": {
                "stop_loss_pct": (getattr(self.cfg.risk,
                                          "risk_mode_stop_loss_pct", -3.0)
                                  if self.risk_mode else
                                  getattr(self.cfg.risk,
                                          "default_stop_loss_pct", -3.0)),
                "take_profit_pct": (getattr(self.cfg.risk,
                                            "risk_mode_take_profit_pct", 12.0)
                                    if self.risk_mode else
                                    getattr(self.cfg.risk,
                                            "default_take_profit_pct", 25.0)),
                "drop_exit_pct": getattr(self.cfg.risk,
                                         "risk_mode_drop_exit_pct", -6.0),
                "buy_cooldown_s": (getattr(self.cfg.risk,
                                           "risk_mode_buy_cooldown_s", 300)
                                   if self.risk_mode else
                                   self.cfg.risk.buy_cooldown_s),
                "entry_pullback_pct": (getattr(self.cfg.risk,
                                               "risk_mode_chase_pct", 0.0)
                                       if self.risk_mode else
                                       self.cfg.ai.entry_pullback_pct),
            },
            "cycles_run": self._cycles_run,
            "started_at": self.started_at,
            "cycle_seconds": self.cfg.engine.cycle_seconds,
            "exchange_health": self._safe_health(),
            "snapshot": {
                "snapshot_id": snap.get("snapshot_id"),
                "created_at": snap.get("created_at"),
                "status": snap.get("status"),
                "market_count": snap.get("market_count"),
                "valid_market_count": snap.get("valid_market_count"),
                "data_quality_score": snap.get("data_quality_score"),
            },
            # کاتالوگ زندهٔ همهٔ بازارهای صرافی (بدون نیاز به اسنپ‌شات):
            # قیمت/حجم/تغییر ۲۴س همهٔ جفت‌ها با یک درخواست کش‌شده
            "universe": self.live_universe_status(),
            "portfolio": portfolio_snapshot,
            "candidates": self.latest_candidates,
            # «سفارشهای در جریان» — every order still working at the
            # exchange / ledger with the detail of what it holds
            "open_orders": self.in_flight_orders(),
            "regime_blocked": getattr(self, "latest_regime_blocked", []),
            "decisions": [self._decision_brief(x) for x in self.latest_decisions],
            "risk": self.latest_risk,
            "executions": self.latest_executions,
            "performance": self.storage.performance_summary(),
            "positions": self.storage.positions(),
            "equity_history": hist_24h,
            # «سود ۲۴ساعت» — how much «میزان سود سرمایه» (ارزش حساب −
            # کل سرمایه واریزی, the line under the «سود کل» amount) moved
            # over the last 24h, from the equity history of THIS account
            # only: `real_now` keeps demo-ledger points apart from the real
            # wallet, so switching accounts can never be read as profit or
            # a −99% loss. coverage tells the UI honest/waiting.
            "profit_24h": self._profit_24h_payload(
                (hp_now.get("total_irt") or equity_24h_irt), hist_24h,
                (hp_now.get("deposited_irt") or
                 self._known_deposited_irt()), self._real_attached),
            # real-account connection truth for the header badge: token
            # present NOW, adapter attached, and last cycle's wallet sync
            "nobitex_connected": bool(
                getattr(self.nobitex_session, "token", ""))
            and self._real_attached,
            "nobitex_username": (self.nobitex_session.username
                                 if self.nobitex_session else ""),
            # live totals for the سود کل KPI: sum of (current value −
            # purchase amount) across ALL holdings — profit since MY buy,
            # revalued every status call (computed above so «سود ۲۴ساعت»
            # reads the same numbers)
            "holdings_pnl": hp_now,
            # پورتفوی نوبیتکسی (نمای کلی + تب «پرتفوی نوبیتکس»): یک ردیف
            # برای هر دارایی با ارزش/سود/سهم — همه به تومان، بدون نیاز به
            # اسنپ‌شات. با همان اعدادِ holdings_pnl ساخته می‌شود تا کارت
            # «سود کل» و این بخش هرگز با هم اختلاف نداشته باشند.
            "portfolio_v2": self._portfolio_v2(prices, portfolio_snapshot,
                                               hp_now, irt_rate),
            # holdings-box rows: per-coin profit since MY purchase, holding
            # timer since the last buy, and the dual stop/TP triggers
            "holdings": [
                self._position_row(p, prices,
                                   float(self._usdt_rates(prices)
                                         .get("IRT") or 0))
                for p in self.storage.positions()
                if d(p["amount"]) > 0 and
                p["market"] not in ("USDT", "IRT", "USDTIRT")],
            "snapshot_count": snap_count,
            "exit_plans": self.storage.kv_get("exit_plans", {}),
            "markets_view": self._markets_view(snap),
            # which quote market the user REALLY traded in — the dashboard
            # activates the Toman (IRT) section of the holdings box only
            # when this says so (see _market_activity)
            "market_sections": self._market_activity(),
            # نحوهٔ تعامل دو بازار (پنجرهٔ Both) + اینکه آیا انتقال/تبدیل
            # بین بخش‌های IRT و USDT مجاز است — داشبورد همان چیزی را
            # نشان می‌دهد که موتور واقعاً اجرا می‌کند
            "market_interaction": self._market_transfer_gate(),
        }

    def _market_activity(self) -> dict:
        """Per-quote REAL activity: IRT (Toman) and USDT (Tether) separately.

        The holdings box shows the Toman section ONLY when the user has
        actually invested Toman (a held Toman-market coin, a real
        Toman-market order/fill, or a stop/take-profit plan on one) — a
        coin bought with Tether must never light it up, and the reverse.
        Counts come from the LEDGER's own markets (the market an order was
        actually placed in), never from the configured market preference.

        Demo/paper records are ignored for orders/fills (an order without
        an exchange id, a fill whose trade id is not `live_…`): they are
        not real Toman/Tether spent. Paper POSITIONS still count, so the
        sections work in demo mode too.
        """
        act = {q: {"holdings": 0, "invested": 0, "orders": 0, "fills": 0,
                   "plans": 0, "markets": [], "active": False}
               for q in ("USDT", "IRT")}
        seen = {q: set() for q in act}
        for p in self.storage.positions():
            mk = (p.get("market") or "").upper()
            q = quote_of_market(mk)
            if not q:
                continue
            amount = d(str(p.get("amount") or 0))
            buys = d(str(p.get("total_buys_irt") or 0))
            if amount > 0:
                act[q]["holdings"] += 1
                seen[q].add(mk)
            # «سرمایه‌گذاری» = هنوز سکه داریم یا پولی خرج شده. یک ردیف
            # صفرشده (amount=0 و total_buys=0) هیچ‌وقت به‌تنهایی بخش را
            # فعال نمی‌کند — وگرنه بازار اشتباهِ صفرشده تا ابد بخش را
            # روشن نگه می‌داشت (بخش خالی با بازار خارج از آن).
            if amount > 0 or buys > 0:
                act[q]["invested"] += 1
                seen[q].add(mk)
        try:
            for mk, plan in (self.storage.kv_get("exit_plans", {}) or {}).items():
                q = quote_of_market(mk)
                if q and plan:
                    act[q]["plans"] += 1
                    seen[q].add(str(mk).upper())
        except Exception:
            pass
        try:
            for o in self.storage.orders(limit=500):
                if not o.get("exchange_order_id"):
                    continue        # paper order — not real money
                q = quote_of_market(o.get("market") or "")
                if q:
                    act[q]["orders"] += 1
                    seen[q].add((o.get("market") or "").upper())
        except Exception:
            pass
        try:
            for f in self.storage.fills(limit=500):
                if not str(f.get("trade_id") or "").startswith("live_"):
                    continue        # paper fill — not real money
                q = quote_of_market(f.get("market") or "")
                if q:
                    act[q]["fills"] += 1
                    seen[q].add((f.get("market") or "").upper())
        except Exception:
            pass
        for q, a in act.items():
            a["active"] = bool(a["holdings"] or a["invested"] or
                               a["orders"] or a["fills"] or a["plans"])
            a["markets"] = sorted(seen[q])
        return act

    def _real_buy_activity(self, base: str) -> dict:
        """Our OWN real BUYs of `base`, grouped by quote market.

        Answers exactly one question for the holdings box and the wallet
        sync: in which market did the user really spend money on this
        coin? Demo/paper records (a trade id that is not `live_…`, an
        order without an exchange id) are ignored — they are not real
        Toman/Tether spent. Returns
        {"USDT": {"count": n, "last": iso, "market": m}, "IRT": {...}}
        with the market being the most recent one bought in.
        """
        base = (base or "").strip().upper()
        cands = {base + "USDT": "USDT", base + "IRT": "IRT"}
        out: dict = {}

        def _bump(q: str, market: str, ts: str) -> None:
            cur = out.setdefault(q, {"count": 0, "last": "", "market": market})
            cur["count"] += 1
            if (ts or "") >= cur["last"]:
                cur["last"] = ts or cur["last"]
                cur["market"] = market

        try:
            for f in self.storage.fills_by_base(base, limit=200):
                if (f.get("side") or "").upper() != "BUY":
                    continue
                if not str(f.get("trade_id") or "").startswith("live_"):
                    continue
                mk = canonical_symbol(f.get("market") or "")
                q = cands.get(mk)
                if q:
                    _bump(q, mk, f.get("timestamp") or "")
        except Exception:
            pass
        try:
            for o in self.storage.orders(limit=500):
                if not o.get("exchange_order_id"):
                    continue
                if (o.get("side") or "").upper() != "BUY":
                    continue
                mk = canonical_symbol(o.get("market") or "")
                q = cands.get(mk)
                if q:
                    _bump(q, mk, o.get("created_at") or "")
        except Exception:
            pass
        return out

    def _market_for_base(self, base: str, markets) -> str | None:
        """The ONE market a wallet-held coin belongs to.

        «نمایش بر اساس بازار واقعی خرید»: the section a coin appears in is
        decided by where its money was REALLY spent (rule 1), never by the
        configured «بازار معاملات» preference (rule 3 breaks ties only).
        A single wallet balance can only live in ONE market, so a coin
        bought with Tether never shows up in the Toman box and the wallet
        sync can no longer seed the same coin into both sections.
        """
        base = (base or "").strip().upper()
        cands = [base + "USDT", base + "IRT"]
        votes = self._real_buy_activity(base)
        best = None
        for q, v in sorted(votes.items(),
                           key=lambda kv: (kv[1]["last"], kv[1]["count"]),
                           reverse=True):
            if v["market"] in cands:
                best = v["market"]
                break
        held = []
        for mk in cands:
            p = self.storage.position(mk)
            if p and d(str(p.get("amount") or 0)) > 0:
                held.append(mk)
        if held:
            if best and best in held:
                return best
            if len(held) == 1:
                # re-file a row sitting in a market the user never bought
                # in, but only with the other side's real buys as proof
                if best and best != held[0] \
                        and quote_of_market(held[0]) not in votes:
                    return best
                return held[0]
            # both markets hold coins: the real (most recent) buy wins,
            # else the first tracked one
            return held[0]
        if best:
            return best
        # no real trading history for this coin: the configured market
        # preference decides, exactly like the old wallet sync did
        usdt_on, irt_on, _src = self._effective_quote_markets()
        if usdt_on == "on" and (base + "USDT") in markets:
            return base + "USDT"
        if irt_on == "on" and (base + "IRT") in markets:
            return base + "IRT"
        if (base + "USDT") in markets:
            return base + "USDT"
        return base + "IRT"

    def _live_fill_quantities(self) -> dict:
        """Net live quantity of every market, from the bot's own fills.

        One read serves the whole wallet sync: the wallet reports ONE
        balance per coin, and for a wallet ticker that is not the market
        base (wallet «x» = XEMPIREIRT) the matching quantity is the only
        honest link between the two. Demo fills (trade ids without the
        `live_` prefix) never enter: they are not exchange balances.
        """
        out: dict = {}
        try:
            fills = self.storage.fills(limit=5000)
        except Exception:
            return out
        for f in fills:
            if not str(f.get("trade_id") or "").startswith("live_"):
                continue
            side = (f.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue
            mk = canonical_symbol(f.get("market") or "")
            if not mk:
                continue
            try:
                amt = d(str(f.get("amount") or 0))
            except Exception:
                continue
            cur = out.setdefault(mk, {"net": ZERO, "last": ""})
            cur["net"] += amt if side == "BUY" else -amt
            ts = f.get("timestamp") or ""
            if ts > cur["last"]:
                cur["last"] = ts
        return out

    def _wallet_market_for(self, asset: str, amount, markets,
                           live: dict | None = None,
                           hist: dict | None = None) -> str | None:
        """The ONE market a wallet balance is really held in.

        Exact ticker markets win when the bot has actually bought there.
        Otherwise match the wallet quantity against the NET live quantity
        of markets whose base starts with the wallet ticker — that is how
        the wallet's `x` (397k X Empire) is recognised as XEMPIREIRT even
        though no XIRT market exists. The prefix match is deliberately
        restricted to a close quantity ratio so a one-letter ticker can
        never bind to an unrelated coin (XRP/LTC/…) that merely shares the
        first letter.
        """
        asset = (asset or "").strip().upper()
        if not asset:
            return None
        try:
            amount = d(str(amount or 0))
        except Exception:
            amount = ZERO
        if live is None:
            live = self._live_fill_quantities()
        # The exchange's own transaction history is the strongest evidence
        # of the quote side the REMAINING quantity was bought in — stronger
        # than a local fill that may since have been sold, and stronger
        # than the configured preference. For wallet «x» this also maps
        # XEMPIRE/X to the real XIRT market.
        hist_quote = str((hist or {}).get("quote") or "").upper()
        if hist_quote in ("IRT", "USDT"):
            cand = canonical_symbol(asset + hist_quote)
            if cand and (cand in markets or self._known_market(cand)):
                return cand
        exact = (asset + "USDT", asset + "IRT")
        # If BOTH sides have real buys, the row that is already tracked
        # keeps the wallet balance (the alternative would move a live
        # position between boxes on every sync). The exchange-history
        # quote above is still authoritative when it exists.
        tracked_live = [mk for mk in exact
                        if d(str((live.get(mk) or {}).get("net") or 0)) > 0
                        and (self.storage.position(mk) or {}).get("amount")
                        and d(str(self.storage.position(mk).get("amount")
                                 or 0)) > 0]
        if tracked_live:
            tracked_live.sort(
                key=lambda mk: str((live.get(mk) or {}).get("last") or ""),
                reverse=True)
            return tracked_live[0]
        exact_live = [(str((live.get(mk) or {}).get("last") or ""), mk)
                      for mk in exact
                      if d(str((live.get(mk) or {}).get("net") or 0)) > 0]
        if exact_live:
            # most recent real purchase decides the quote side
            exact_live.sort(reverse=True)
            return exact_live[0][1]
        if amount > 0:
            best = None
            for mk, val in live.items():
                q = d(str((val or {}).get("net") or 0))
                if q <= 0:
                    continue
                mk_base = split_symbol(canonical_symbol(mk))[0]
                if not mk_base.startswith(asset):
                    continue
                ratio = float(q / amount)
                if ratio < 0.5 or ratio > 2.0:
                    continue
                key = (abs(1.0 - ratio), str((val or {}).get("last") or ""))
                if (best is None or key[0] < best[0][0] or
                        (key[0] == best[0][0] and key[1] > best[0][1])):
                    best = (key, mk)
            if best:
                return best[1]
        # no live history: only a market that really exists may be used;
        # the old fallback invented «XIRT» for wallet x and valued 397k X
        # at the price of a same-named token that was never held.
        for mk in exact:
            if mk in markets:
                return mk
        return None

    def _known_market(self, symbol: str) -> bool:
        try:
            return bool(self.market_universe().row(symbol))
        except Exception:
            return False

    def _decision_brief(self, d_: dict) -> dict:
        """خلاصهٔ تصمیم برای داشبورد — کمیتهٔ چندمدلی هم دیده می‌شود.

        `committee` همان حکم ai_committee است: هر مدل چه رأیی داد، احتمال
        صعود، حرکت انتظاری و ارزش انتظاری خالص، و دلیل مسدودشدن. چون پاسخ
        وضعیت هر چند ثانیه خوانده می‌شود، فهرست مدل‌ها کوتاه (نام/رأی/عدد)
        فرستاده می‌شود نه کل پیلود.
        """
        brief = {k: d_.get(k) for k in (
            "decision_id", "symbol", "action", "confidence",
            "reason_codes", "created_at", "expires_at", "entry",
            "stop_loss", "take_profit", "position_size", "cost", "risk")}
        c = d_.get("committee")
        if isinstance(c, dict) and c:
            brief["committee"] = {
                "consensus": c.get("consensus"),
                "allow_buy": c.get("allow_buy"),
                "p_up": c.get("p_up"),
                "expected_up_pct": c.get("expected_up_pct"),
                "expected_down_pct": c.get("expected_down_pct"),
                "expected_net_edge_pct": c.get("expected_net_edge_pct"),
                "plan_ev_pct": c.get("plan_ev_pct"),
                "plan_tp_pct": c.get("plan_tp_pct"),
                "plan_stop_pct": c.get("plan_stop_pct"),
                "forecast_net_edge_pct": c.get("forecast_net_edge_pct"),
                "cost_pct": c.get("cost_pct"),
                "votes_buy": c.get("votes_buy"),
                "votes_active": c.get("votes_active"),
                "votes_needed": c.get("votes_needed"),
                "hold_s": c.get("hold_s"),
                "horizon_s": c.get("horizon_s"),
                "blocked_by": c.get("blocked_by") or [],
                "models": [{"model": m.get("model"),
                            "verdict": m.get("verdict"),
                            "p_up": m.get("p_up"),
                            "expected_move_pct": m.get("expected_move_pct")}
                           for m in (c.get("models") or [])],
            }
        return brief

    def _markets_view(self, snap: dict) -> list:
        """«همه بازارها» — the WHOLE exchange, snapshot or not.

        Rows come from the live catalog (every market the exchange lists,
        one cached public call) merged with the cycle's snapshot metrics
        (liquidity, RSI, trend, regime, candidate score) for the markets
        that happen to be inside the trading universe. The old version
        could only ever show the snapshot subset, so a coin the user holds
        — or a brand-new listing — simply did not exist in the table.
        """
        snap_rows = {m["symbol"]: m for m in snap.get("markets", [])
                     if m.get("symbol")}
        cand_by_symbol = {c["symbol"]: c for c in self.latest_candidates}
        held = {}
        try:
            for p in self.storage.positions():
                if d(p["amount"]) > 0 and p.get("market"):
                    held[p["market"]] = d(p["amount"])
        except Exception:
            pass
        try:
            uni_rows = self.market_universe().rows()
        except Exception:
            uni_rows = {}
        symbols = list(uni_rows.keys())
        for s in snap_rows:
            if s not in symbols:
                symbols.append(s)
        out = []
        for sym in symbols:
            m = snap_rows.get(sym) or {}
            u = uni_rows.get(sym) or {}
            cand = cand_by_symbol.get(sym)
            out.append({
                "symbol": sym,
                "last_price": m.get("last_price", u.get("last_price")),
                "change_24h": (m.get("change_24h") if m
                               else u.get("change_24h")),
                "spread_percent": (m.get("spread_percent") if m
                                   else u.get("spread_percent")),
                "volume_24h": (m.get("quote_volume_24h") if m
                               else u.get("quote_volume_24h")),
                "liquidity": m.get("liquidity", {}).get("status"),
                "liquidity_score": m.get("liquidity", {}).get("score"),
                "volatility_regime": m.get("volatility", {}).get(
                    "volatility_regime"),
                "rsi": (m.get("technicals") or {}).get("rsi"),
                "trend": (m.get("structure") or {}).get("trend"),
                "regime": (m.get("regime") or {}).get("regime"),
                "score": cand["score"] if cand else None,
                "rank": cand["rank"] if cand else None,
                "data_quality": m.get("data_quality", {}).get("score"),
                "data_quality_status": m.get("data_quality", {}).get("status"),
                # whole-catalog flags (the dashboard's «وضعیت» column)
                "source": ("snapshot" if m else
                           ("universe" if u else "none")),
                "in_snapshot": bool(m),
                "tradable": bool(u.get("tradable", True)),
                "enabled": self._quote_market_enabled(sym),
                "held": sym in held,
                "held_amount": (float(held[sym]) if sym in held else None),
            })
        out.sort(key=lambda x: (x["rank"] is None,
                                x["rank"] if x["rank"] is not None else 999,
                                -(float(x.get("volume_24h") or 0))))
        return out

    def markets_universe(self, sort: str = "volume", quote: str | None = None,
                         limit: int | None = None, search: str = "",
                         refresh: bool = False,
                         only_enabled: bool = False) -> dict:
        """Whole-exchange catalog + analysis flags — the snapshot-free
        scanner behind «همه بازارهای نوبیتکس».

        Every market the exchange lists, ranked (volume / 24h change /
        gainers / losers / price / spread), searchable by symbol, with the
        engine's own flags per row: whether the coin is HELD, inside the
        cycle's snapshot, a ranked candidate, and whether its section is
        enabled in «بازار معاملات».
        """
        try:
            uni = self.market_universe(force=refresh)
        except Exception as exc:
            return {"rows": [], "count": 0, "error": str(exc),
                    "universe": {"count": 0, "error": str(exc)}}
        held = {}
        try:
            for p in self.storage.positions():
                if d(p["amount"]) > 0 and p.get("market"):
                    held[p["market"]] = d(p["amount"])
        except Exception:
            pass
        snap_syms = {m["symbol"] for m in (self.latest_snapshot or {}).get(
            "markets", []) if m.get("symbol")}
        cand = {c["symbol"]: c for c in self.latest_candidates}
        prices = uni.rows()
        rates = {}
        try:
            rates = self._usdt_rates({s: r.get("last_price")
                                      for s, r in prices.items()
                                      if r.get("last_price")})
        except Exception:
            rates = {}
        usdt_per_irt = d(str(rates.get("IRT") or 0))

        def _usdt_value(sym: str, amount: Decimal):
            """Value of `amount` units of `sym` in USDT (unit-aware)."""
            row = prices.get(sym) or {}
            px = d(str(row.get("last_price") or 0))
            if px <= 0:
                return None
            if sym.endswith("USDT"):
                return float(px * amount)
            if sym.endswith("IRT") and usdt_per_irt > 0:
                return float(px * amount * usdt_per_irt)
            return float(px * amount)

        extras = {}
        for sym in list(prices.keys()) + [s for s in held
                                          if s not in prices]:
            amt = held.get(sym)
            extras[sym] = {
                "held": sym in held,
                "held_amount": float(amt) if amt is not None else None,
                "value_usdt": (_usdt_value(sym, amt)
                               if amt is not None else None),
                "in_snapshot": sym in snap_syms,
                # same token as «همه بازارها» (src/analysis source)
                "source": "snapshot" if sym in snap_syms else "universe",
                "candidate_rank": (cand.get(sym) or {}).get("rank"),
                "candidate_score": (cand.get(sym) or {}).get("score"),
                "enabled": self._quote_market_enabled(sym),
            }
        rows = uni.ranked(sort=sort, quote=quote, limit=limit, search=search,
                          extras=extras)
        if only_enabled:
            rows = [r for r in rows if r.get("enabled")]
        return {"rows": rows, "count": len(rows),
                "universe": uni.status(),
                "transfer": self._market_transfer_gate(),
                "sort": sort, "search": search, "quote": quote,
                "generated_at": utc_now_iso()}

    def _safe_health(self) -> dict:
        try:
            return self.exchange.health()
        except Exception as exc:
            return {"api": {"status": "UNKNOWN", "error": str(exc)}}

    def login_status(self) -> dict:
        """Real-account login state for the dashboard (SS81-safe: no secrets)."""
        st = self.nobitex_session.status() if self.nobitex_session else {
            "connected": False}
        # the login session is ALWAYS available now (hot-attach on login)
        st["available"] = True
        st["mode"] = self.cfg.exchange.mode
        st["real_attached"] = self._real_attached
        return st

    def account_login(self, username: str, password: str, otp: str = "",
                      totp_secret: str = "", remember: str = "yes") -> dict:
        """Dashboard-triggered full login flow -> token stored (SS81).

        On success the real Nobitex adapter is hot-attached (even from demo
        mode) and the real spot wallet is synced into the engine so the risk
        engine trades against actual balances.
        """
        try:
            result = self.nobitex_session.login(
                username=username, password=password, totp_secret=totp_secret,
                totp_code=otp, remember=remember)
        except NobitexError as exc:
            self.events.error("WEB", "LOGIN_FAILED", "WARNING",
                              "%s: %s" % (exc.code, exc))
            return {"ok": False, "error": "%s: %s" % (exc.code, exc)}
        except Exception as exc:
            self.events.error("WEB", "LOGIN_FAILED", "WARNING", str(exc))
            return {"ok": False, "error": str(exc)}
        self.events.publish("LOGIN_SUCCESS", "web", "",
                            {"username": username,
                             "device": result.get("device", "")})
        # hot-attach the real adapter without restarting the process
        attach = self._attach_real_exchange()
        sync = {"ok": False}
        if attach.get("attached"):
            sync = self.sync_real_wallet()
            # product spec: once logged into the real account, the bot
            # trades the real account immediately (no sandbox stop).
            if getattr(self.cfg.exchange, "live_by_default", True):
                self.cfg.exchange.live_trading = True
                self.events.publish("LIVE_TRADING_CHANGED", "engine", "",
                                    {"enabled": True, "reason": "LIVE_BY_DEFAULT"})
        return {"ok": True, "device": result.get("device", ""),
                "logged_in_at": result.get("logged_in_at", ""),
                "live_trading": self.cfg.exchange.live_trading,
                "exchange": attach, "wallet_sync": sync}

    # ---------------------------------------------------- real-account glue
    def _attach_real_exchange(self) -> dict:
        """Swap the simulator for the real Nobitex adapter after login."""
        if isinstance(self.exchange, NobitexAPI):
            return {"attached": True, "note": "already real"}
        try:
            cfg = self.cfg
            cfg.exchange.nobitex_username = self.nobitex_session.username
            cfg.exchange.nobitex_password = self.nobitex_session.password
            cfg.exchange.nobitex_totp_secret = self.nobitex_session.totp_secret
            real = NobitexAPI(cfg.exchange, events=self.events)
            real.session = self.nobitex_session  # share the logged-in session
            real.set_irt_divisor(float(getattr(
                cfg.exchange, "rls_to_irt_divisor", 10) or 10))
            self._set_exchange(real)
            self.events.publish("EXCHANGE_ATTACHED", "engine", "",
                                {"adapter": "nobitex"})
            return {"attached": True}
        except Exception as exc:
            self.events.error("ENGINE", "EXCHANGE_ATTACH_FAILED", "HIGH",
                              str(exc), retryable=True)
            return {"attached": False, "error": str(exc)}

    def _effective_quote_markets(self) -> tuple[str, str, str]:
        """Resolve which quote markets are BUYABLE.

        Returns (usdt_state, irt_state, source) where each state is "on"
        or "off" and source is "trade_quote_markets" (the 3-way Settings
        selector) or "legacy_flags" (fallback to the old boolean pair).

        The 3-way selector wins whenever it holds a recognized value
        ("both"/"usdt"/"irt" — case-insensitive). An empty or unknown
        value falls back to the legacy booleans prefer_usdt_markets /
        include_irt_markets so pre-existing user configs keep their old
        behavior. Fail-safe: a resolution that would disable BOTH groups
        never happens — the engine warns and re-enables both instead of
        silently leaving the bot unable to buy anything.
        """
        ex = self.cfg.exchange
        raw = str(getattr(ex, "trade_quote_markets", "both") or "").strip().lower()
        if raw in ("both", "usdt", "irt"):
            usdt_on = raw in ("both", "usdt")
            irt_on = raw in ("both", "irt")
            src = "trade_quote_markets"
        else:
            # legacy/unknown -> old boolean pair decides
            usdt_on = bool(ex.prefer_usdt_markets)
            irt_on = bool(ex.include_irt_markets)
            src = "legacy_flags"
        if not (usdt_on or irt_on):
            self.events.publish(
                "QUOTE_MARKETS_BOTH_OFF", "engine", "",
                {"trade_quote_markets": raw,
                 "resolved": "both", "source": src})
            return ("on", "on", src)
        return ("on" if usdt_on else "off", "on" if irt_on else "off", src)

    def _quote_market_enabled(self, market: str) -> bool:
        """Is the QUOTE currency of this market switched ON in «بازار معاملات»?

        The selector is the single source of truth in BOTH directions: a
        market whose quote is off is never BOUGHT (the trading universe
        already drops it from the real registry) *and* is never touched by
        the engine's automatic paths either. Without this gate «فقط تتری»
        still answered old exit plans with Toman sell orders, and the demo
        universe — which deliberately keeps its own fixture list — kept
        buying and selling BTCIRT/SHIBIRT/USDTIRT every cycle.

        Manual dashboard actions (خرید/فروش/تبدیل clicked by the user) are
        explicit orders and stay available.
        """
        sym = (market or "").strip().upper()
        usdt_state, irt_state, _src = self._effective_quote_markets()
        if sym.endswith("USDT"):
            return usdt_state == "on"
        if sym.endswith("IRT"):
            return irt_state == "on"
        return True

    def _effective_market_interaction(self) -> tuple[str, str]:
        """Resolve HOW the two markets may interact (Both-mode popup).

        The popup records its answer in its OWN field
        (exchange.trade_market_interaction) instead of being inferred from
        trade_quote_markets == "both": being in Both mode must never imply
        that moving money between the IRT and the USDT section is allowed.

        Returns (mode, source) where mode is "linked" (رعایت تعامل: transfer
        / convert between the two sections allowed) or "independent"
        (بازارهای مستقل: no transfer at all). An empty or unknown value
        falls back to "independent" — the fail-safe that cannot move the
        user's money on its own.
        """
        ex = self.cfg.exchange
        raw = str(getattr(ex, "trade_market_interaction", "") or "").strip().lower()
        if raw in ("linked", "link", "interact", "interaction", "connected",
                   "transfer", "transferable", "both_linked", "on"):
            return "linked", "trade_market_interaction"
        if raw in ("independent", "separate", "isolated", "split", "off",
                   "no_transfer"):
            return "independent", "trade_market_interaction"
        return "independent", "default"

    def _cross_market_allowed(self) -> bool:
        """May assets/balances move BETWEEN the IRT and the USDT section?

        Only when BOTH markets are on AND the Both-popup answer was
        «تعامل بین بازارها» (linked). A single-market selection therefore
        behaves exactly like «Both + بازارهای مستقل»: no funding of one
        market from the other, no conversion between them.
        """
        usdt_state, irt_state, _src = self._effective_quote_markets()
        if usdt_state != "on" or irt_state != "on":
            return False
        mode, _msrc = self._effective_market_interaction()
        return mode == "linked"

    def _market_transfer_gate(self) -> dict:
        """Read-only view of both settings (status/dashboard payload)."""
        usdt_state, irt_state, src = self._effective_quote_markets()
        mode, msrc = self._effective_market_interaction()
        return {
            "quote_markets": {"usdt": usdt_state, "irt": irt_state,
                              "source": src},
            "interaction": mode,
            "interaction_source": msrc,
            "cross_market_allowed": self._cross_market_allowed(),
        }

    def _coin_section(self, coin: str) -> str | None:
        """Which section (IRT/USDT) a held coin belongs to.

        Same rule as the holdings box (_market_for_base): the market the
        user REALLY bought the coin in wins, else the configured quote
        preference. Used to decide whether a wallet conversion would be a
        cross-market transfer (a Tether-side coin converted to Toman).
        """
        coin = (coin or "").strip().upper()
        if not coin:
            return None
        if coin == "USDT":
            return "USDT"
        if coin == "IRT":
            return "IRT"
        snap = self.latest_snapshot or {}
        markets = {m.get("symbol") for m in snap.get("markets", [])
                   if m.get("symbol")}
        # make sure both candidates exist so the preference fallback inside
        # _market_for_base can answer for coins missing from the cycle
        markets.add(coin + "USDT")
        markets.add(coin + "IRT")
        market = self._market_for_base(coin, markets)
        if not market:
            return None
        return "USDT" if market.endswith("USDT") else "IRT"

    def _conversion_section_allowed(self, coin: str) -> tuple[bool, str]:
        """Can this coin be converted (wallet convert pays out in Toman)?

        The exchange's wallet conversion always lands in the Toman wallet,
        so it may only run inside the IRT section. A coin filed under the
        USDT section would be a cross-market transfer and is refused unless
        the two markets are explicitly linked (Both + تعامل بین بازارها).
        """
        section = self._coin_section(coin)
        if section == "USDT" and not self._cross_market_allowed():
            return False, ("تبدیل به تومان یک انتقال بین دو بازار است — "
                           "«بازار معاملات» روی تتری/مستقل است")
        return True, ""

    def _apply_trading_universe(self) -> None:
        """Real-market trading universe (SS5/SS6): USDT pairs preferred,
        optional IRT pairs, with a 24h quote-volume floor. Only applied when
        the real account is attached; demo keeps its fixture list.

        The USDT/IRT split follows the Settings selector
        exchange.trade_quote_markets (both|usdt|irt) when it is set, else
        the legacy prefer_usdt_markets / include_irt_markets booleans."""
        ex = self.cfg.exchange
        usdt_state, irt_state, _src = self._effective_quote_markets()
        registry = self.collector.registry
        try:
            stats = self.exchange._stats_all()
        except Exception:
            stats = {}
        div = float(ex.rls_to_irt_divisor or 10)
        floor = float(ex.min_quote_volume_usdt or 0)
        keep = []
        for pair, st in stats.items():
            src, _, dst = pair.partition("-")
            dst_u = dst.upper()
            if dst_u == "USDT":
                if usdt_state != "on":
                    continue
                sym = src.upper() + "USDT"
                vol_usdt = float(st.get("volumeDst") or 0)
            elif dst_u in ("RLS", "IRT"):
                if irt_state != "on":
                    continue
                sym = src.upper() + "IRT"
                vol_usdt = float(st.get("volumeDst") or 0) / div
            else:
                continue
            if st.get("isClosed", False):
                continue
            if vol_usdt < floor:
                continue
            keep.append((sym, vol_usdt))
        if not keep:
            return  # discovery update alone (fail-safe: keep registry state)
        keep.sort(key=lambda x: -x[1])
        keep = keep[: max(1, self.cfg.engine.max_markets)]
        keep_syms = {s for s, _ in keep}
        now = utc_now_iso()
        for sym, entry in registry.doc["markets"].items():
            active = sym in keep_syms
            if entry.get("tradable") != active:
                entry["tradable"] = active
                entry["status"] = "ACTIVE" if active else "INACTIVE"
                entry["updated_at"] = now
        registry.doc["updated_at"] = now
        registry.doc["universe"] = {
            "selected": [s for s, _ in keep],
            "volume_floor_usdt": floor,
            "updated_at": now,
        }
        registry._save()

    def _set_exchange(self, new_exchange) -> None:
        """Atomically swap the exchange adapter used by collector/executor."""
        self.exchange = new_exchange
        self.cfg.exchange.mode = "nobitex"
        self.collector.exchange = new_exchange
        self.executor.exchange = new_exchange
        self._real_attached = isinstance(new_exchange, NobitexAPI)
        # never let a previous account's wallet valuations leak into a new
        # adapter (demo or another login); the next sync fills them again.
        self._wallet_valuations = {}
        self._wallet_market_assets = {}
        try:
            self.portfolio.exchange_valuations_irt = {}
        except Exception:
            pass

    #: the exchange transaction history is rate-limited (60 calls/hour), so
    #: its per-coin cost basis is cached. Positions repaired from it stay
    #: exact; only a new trade can move the average, and that fill updates
    #: the ledger itself in the normal path.
    WALLET_COST_TTL_S = 6 * 3600

    def _wallet_cost_bases_cached(self, usdt_toman: float) -> dict:
        """Exchange cost basis, cached; never raises into the wallet sync."""
        cached = self.storage.kv_get("wallet_cost_bases", {}) or {}
        if not isinstance(cached, dict):
            cached = {}
        at = cached.get("at")
        if cached.get("assets") and at:
            try:
                age = (datetime.now(timezone.utc) - parse_iso(at))
                if 0 <= age.total_seconds() < self.WALLET_COST_TTL_S:
                    return cached
            except Exception:
                pass
        fn = getattr(self.exchange, "wallet_cost_bases", None)
        if not callable(fn):
            return cached
        try:
            assets = fn(usdt_toman=usdt_toman) or {}
        except Exception as exc:
            self.events.error("ENGINE", "WALLET_COST_BASIS_FAILED",
                              "WARNING", str(exc))
            return cached
        if not assets:
            return cached
        doc = {"at": utc_now_iso(), "usdt_toman": float(usdt_toman or 0),
               "assets": assets}
        self.storage.kv_set("wallet_cost_bases", doc)
        self.events.publish("WALLET_COST_BASIS_READ", "engine", "",
                            {"assets": sorted(assets.keys())})
        return doc

    #: the portfolio daily report changes once a day; a short cache keeps
    #: the dashboard poll from hitting the exchange on every status read.
    EXCHANGE_DAILY_PNL_TTL_S = 15 * 60

    def _profit_24h_payload(self, total_irt, hist, deposited, real) -> dict:
        """The «سود ۲۴ساعت» card: exchange daily report when available.

        The local equity-history calculation is kept as the fallback, but
        on the real account the wallet's own daily P&L is authoritative —
        it already accounts for every manual trade, fee and transfer, so
        it is exactly the number the Nobitex portfolio view prints. Using
        it also removes the fake jumps caused by a deposit landing between
        two local history points.
        """
        local = self._profit_24h(total_irt, hist, deposited, real)
        if not real:
            return local
        daily = self._exchange_daily_pnl_cached()
        pnl = daily.get("profit_irt")
        if pnl is None:
            return local
        pct = daily.get("pct")
        bal = daily.get("balance_irt")
        if pct is None and bal and float(bal) != 0:
            pct = round(float(pnl) / float(bal) * 100, 2)
        return {
            "coverage": "exact", "basis": "exchange_daily",
            "real": True, "source": daily.get("source"),
            "report_date": daily.get("report_date"),
            "pnl_irt": float(pnl), "pct": pct,
            "pct_raw": pct, "pct_base": "balance",
            "total_irt": float(bal) if bal is not None else None,
            "waiting_reason": None,
        }

    def _exchange_daily_pnl_cached(self) -> dict:
        """Latest exchange daily P&L (wallet/portfolio panel), cached."""
        cached = self.storage.kv_get("exchange_daily_pnl", {}) or {}
        if not isinstance(cached, dict):
            cached = {}
        at = cached.get("at")
        if cached.get("data") and at:
            try:
                age = (datetime.now(timezone.utc) - parse_iso(at))
                if 0 <= age.total_seconds() < self.EXCHANGE_DAILY_PNL_TTL_S:
                    return cached.get("data") or {}
            except Exception:
                pass
        fn = getattr(self.exchange, "portfolio_daily_profit", None)
        if not callable(fn):
            return cached.get("data") or {}
        try:
            data = fn() or {}
        except Exception as exc:
            self.events.error("ENGINE", "EXCHANGE_DAILY_PNL_FAILED",
                              "WARNING", str(exc))
            return cached.get("data") or {}
        if data:
            self.storage.kv_set("exchange_daily_pnl",
                                {"at": utc_now_iso(), "data": data})
        return data

    def _apply_wallet_cost_bases(self, costs: dict, usdt_toman: float,
                                 as_of: str | None = None) -> int:
        """Rewrite the ledger's average entry from the exchange ledger.

        The exchange knows every BUY/SELL — including manual ones the bot
        never recorded — and its wallet P&L is computed from that same
        history. Copying the remaining cost into the position makes the
        row's «ارزش − مبلغ خرید» (Toman money and percent) match the
        wallet instead of a local weighted average that survived a partial
        sell or a re-buy.

        Guards: only when the history's remaining quantity equals the
        position amount and the position has no NEWER local buy (a fresh
        fill that the cached history does not contain yet).
        """
        applied = 0
        if not costs:
            return 0
        inverse = {str(a).upper(): mk for mk, a in
                   (getattr(self, "_wallet_market_assets", None) or {}).items()}
        try:
            as_of_dt = parse_iso(as_of) if as_of else None
        except Exception:
            as_of_dt = None
        for asset, c in costs.items():
            if not isinstance(c, dict):
                continue
            # A partial history (page cap, older rows not read, or a price
            # missing from a description) gives an AVERAGE OF ONLY THE
            # NEWEST TRADES. Using it would move the row away from the
            # exchange's own P&L, so keep the existing book instead.
            if c.get("complete") is False:
                continue
            asset_u = str(asset).upper()
            market = inverse.get(asset_u)
            if not market:
                q = str(c.get("quote") or "").upper()
                if q in ("IRT", "USDT"):
                    market = canonical_symbol(asset_u + q)
            pos = self.storage.position(market) if market else None
            if not pos:
                continue
            try:
                amount = d(str(pos.get("amount") or 0))
                hist_qty = d(str(c.get("amount") or 0))
                cost_irt = d(str(c.get("cost_irt") or 0))
            except Exception:
                continue
            if amount <= 0 or hist_qty <= 0 or cost_irt <= 0:
                continue
            # one wallet balance is ONE lot: a mismatch means deposits/
            # withdrawals or a fill missing from the cached history — keep
            # the local book rather than copying a cost for another size.
            diff = abs(amount - hist_qty)
            if diff > max(Decimal("0.00000001"), amount * Decimal("0.000001")):
                continue
            # a local BUY newer than the cached history is not in it yet
            lb = pos.get("last_buy_at") or ""
            if as_of_dt and lb:
                try:
                    if parse_iso(lb) > as_of_dt + timedelta(seconds=60):
                        continue
                except Exception:
                    pass
            quote = quote_of_market(market) or str(c.get("quote") or "IRT")
            if quote == "USDT":
                if not usdt_toman or usdt_toman <= 0:
                    continue
                avg = cost_irt / amount / d(str(usdt_toman))
                lbp = d(str(c.get("last_buy_price") or 0))
                if str(c.get("last_quote") or "").upper() != "USDT" or lbp <= 0:
                    lbp = avg
            else:
                avg = cost_irt / amount
                lbp = d(str(c.get("last_buy_price_irt") or 0))
                if str(c.get("last_quote") or "").upper() != "IRT" or lbp <= 0:
                    lbp = avg
            if avg <= 0:
                continue
            old = d(str(pos.get("avg_entry") or 0))
            if old > 0 and abs(old - avg) / old < Decimal("0.0000001"):
                continue
            self.storage.upsert_position(
                market, str(amount), str(avg),
                pos.get("realized_pnl") or "0",
                pos.get("side") or "LONG",
                last_buy_at=c.get("last_buy_at") or None,
                total_buys_irt=avg * amount,
                last_buy_price=lbp)
            applied += 1
            self.events.publish(
                "POSITION_COST_BASIS_FIXED", "engine", market,
                {"from": float(old), "to": float(avg),
                 "amount": float(amount),
                 "source": "nobitex-wallet-history"})
        return applied

    def sync_real_wallet(self) -> dict:
        """Pull the real spot wallet and mirror it into the paper ledger.

        This makes the risk engine size orders against actual free balances
        (SS21/SS101): if the real USDT/IRT balance is 0, nothing is approved.
        """
        if not self._real_attached:
            return {"ok": False, "error": "not logged in to real account"}
        try:
            doc = self.exchange.portfolio()
        except NobitexError as exc:
            return {"ok": False, "error": "%s: %s" % (exc.code, exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if doc.get("error"):
            return {"ok": False, "error": doc["error"]}
        assets = doc.get("assets") or []
        mapped = {}
        valuations = {}
        # live Toman value of one USDT, taken from the same exchange wallet
        # read — used to translate Tether-priced BOYs in the cost history
        # into the Toman basis the wallet P&L is compared against.
        usdt_toman = 0.0
        for a in assets:
            if (a.get("asset") or "").upper() == "USDT":
                try:
                    total = float(a.get("total") or 0)
                    val = float(a.get("value_irt") or 0)
                except (TypeError, ValueError):
                    total = val = 0.0
                if total > 0 and val > 0:
                    usdt_toman = val / total
                break
        for a in assets:
            try:
                amt = d(str(a.get("available") or 0))
                locked = d(str(a.get("locked") or 0))
                total = d(str(a.get("total") or 0))
                val = d(str(a.get("value_irt") or 0))
            except Exception:
                continue
            asset = (a.get("asset") or "").upper()
            if amt + locked > 0:
                mapped[asset] = {"available": amt, "locked": locked}
            # rialBalance / divisor — the exact number the exchange wallet
            # page shows; zero/absent fields fall back to the ticker path.
            if asset and total > 0 and val > 0:
                valuations[asset] = {
                    "amount": float(total), "value_irt": float(val),
                    "price_irt": float(val / total),
                    "source": a.get("value_source") or "wallet",
                }
        self._wallet_valuations = valuations
        try:
            self.portfolio.exchange_valuations_irt = {
                k: v["value_irt"] for k, v in valuations.items()}
        except Exception:
            pass
        if not mapped:
            return {"ok": False,
                    "error": "wallet returned no balances (empty account?)"}
        self.portfolio.overwrite_balances(mapped)
        # mirror positions to the real wallet: coins still held keep their
        # position row; coins the wallet no longer has are zeroed so stale
        # demo positions can never block the risk engine (exposure/position
        # caps) with holdings the real account does not have.
        mirrored = 0
        for pos in self.storage.positions():
            mkt = pos["market"]
            base = (mkt[:-4] if mkt.endswith("USDT") else mkt[:-3]).upper()
            if base in ("USDT", "IRT", "", None):
                continue  # quote assets are balances, not positions
            real_amt = mapped.get(base, {}).get("available", ZERO)
            cur = d(str(pos["amount"] or 0))
            if cur > 0 and real_amt <= 0:
                self.storage.upsert_position(mkt, ZERO, pos.get("avg_entry") or ZERO,
                                             pos.get("realized_pnl") or ZERO,
                                             pos.get("side") or "LONG")
                mirrored += 1
            elif cur > 0 and real_amt > 0 and real_amt < cur:
                # keep the position but clamp to the real holding
                self.storage.upsert_position(mkt, real_amt, pos.get("avg_entry") or ZERO,
                                             pos.get("avg_entry") and
                                             pos.get("realized_pnl") or ZERO,
                                             pos.get("side") or "LONG")
                mirrored += 1
        # reconcile live orders -> ledger fills (avg entry source), then
        # seed positions for coins the account holds. Bot-bought coins get
        # their real avg entry from our own fills and an exit plan from the
        # last AI decision, so stop-loss/take-profit works after restarts.
        try:
            self.backfill_real_fills()
        except Exception:
            pass
        # The exchange's OWN transaction ledger is the authority for
        # «مبلغ خرید» / «سود از خرید»: local average entries drifted after
        # partial sells and re-buys (ATOM: local 433,564 vs the exchange's
        # actual 447,284 Toman for the coins still held), so both the Toman
        # profit and its percentage disagreed with the wallet. Read it
        # (cached for a few hours) and repair the ledger from it.
        cost_doc = self._wallet_cost_bases_cached(usdt_toman)
        costs = cost_doc.get("assets") or {}
        seeded = self._seed_positions_from_wallet(mapped, costs=costs)
        cost_fixed = self._apply_wallet_cost_bases(
            costs, usdt_toman, cost_doc.get("at"))
        self.events.publish("WALLET_SYNCED", "engine", "",
                            {"assets": sorted(mapped.keys()),
                             "positions_seeded": seeded,
                             "positions_cost_fixed": cost_fixed,
                             "positions_stale_removed": mirrored})
        return {"ok": True, "assets": sorted(mapped.keys()),
                "positions_seeded": seeded,
                "positions_cost_fixed": cost_fixed,
                "positions_stale_removed": mirrored,
                "timestamp": doc.get("timestamp")}

    def _seed_positions_from_wallet(self, balances: dict,
                                    costs: dict | None = None) -> int:
        """Register held coins as positions with real avg entries.

        Driven by the WALLET holdings (not the snapshot), so it works even
        before the first full cycle has collected market data. Entry price
        source of truth: our own recorded fills for that market (bot-bought
        coins), else unknown (0). A bot-bought coin also gets an exit plan
        derived from the AI decision that bought it so stop-loss/take-
        profit resumes after restarts (SS36/37).
        """
        seeded = 0
        snap = self.latest_snapshot or {}
        market_prices = {}
        for m in snap.get("markets", []):
            if m.get("last_price"):
                market_prices[m["symbol"]] = m["last_price"]
        markets = set(market_prices.keys())
        try:                             # the whole live catalog, not just
            markets |= self.market_universe().symbols()  # the cycle snapshot
        except Exception:
            pass
        # wallet-driven: map every held coin to its ONE canonical market —
        # the market the user REALLY bought it in (our own real fills),
        # never both. The old loop walked every market of the snapshot and
        # paired each one with the same wallet balance, so a single coin
        # was seeded into the Tether AND the Toman box at once and showed
        # up in a section the user never bought in.
        targets = {}
        wallet_market_assets = {}
        live = self._live_fill_quantities()
        for base, b in balances.items():
            try:
                total_amt = d(str(b.get("available", 0))) + d(str(
                    b.get("locked", 0)))
            except Exception:
                total_amt = ZERO
            if base in ("USDT", "IRT", "") or total_amt <= 0:
                continue
            market = self._wallet_market_for(
                base, total_amt, markets, live,
                hist=(costs or {}).get(str(base).upper()))
            if not market:
                continue
            targets[market] = base
            wallet_market_assets[market] = base
            markets.add(market)
        # the row's exact wallet valuation is looked up by this mapping
        self._wallet_market_assets = wallet_market_assets
        # A wallet ticker can have been seeded earlier under a made-up
        # market (wallet x -> XIRT). Now that its real market is known,
        # zero those rows — but only when that market has NO live buys of
        # its own, so a genuine dual holding is never destroyed.
        stale = 0
        target_of_base = {base: mk for mk, base in targets.items()}
        plans = None
        for pos in list(self.storage.positions()):
            mk = (pos.get("market") or "").upper()
            if not mk or mk in targets:
                continue
            try:
                cur = d(str(pos.get("amount") or 0))
            except Exception:
                cur = ZERO
            if cur <= 0:
                continue
            pos_base = split_symbol(canonical_symbol(mk))[0]
            target = target_of_base.get(pos_base)
            if not target or target == mk:
                continue
            if d(str((live.get(mk) or {}).get("net") or 0)) > 0:
                continue        # this quote side has real buys — keep it
            self.storage.upsert_position(
                mk, ZERO, pos.get("avg_entry") or ZERO,
                pos.get("realized_pnl") or ZERO, pos.get("side") or "LONG",
                total_buys_irt=0, last_buy_price=0)
            if plans is None:
                plans = self.storage.kv_get("exit_plans", {}) or {}
            if isinstance(plans, dict) and mk in plans:
                plans.pop(mk, None)
                self.storage.kv_set("exit_plans", plans)
            stale += 1
        for market in sorted(targets):
            base = targets[market]
            b = balances.get((base or "").upper(), {}) or {}
            amt = d(str(b.get("available", 0))) + d(str(b.get("locked", 0)))
            if amt <= 0:
                continue
            pos = self.storage.position(market)
            if pos and d(pos["amount"]) > 0:
                # دفتر خودمان ملاکِ «مبلغ خرید» است: اگر میانگین خریدِ
                # ذخیره‌شده با فیل‌های همین سکه زمین تا آسمان فرق داشته
                # باشد (SUSDT: ۱۶٫۵$ در برابر خرید‌های ‎~۰٫۰۴$)، همان
                # ردیف «ارزش منهاي مبلغ خرید» را بی‌معنا می‌کند و در
                # فروش هم زیان جعلی می‌سازد — پس از فیل‌ها تعمیر می‌شود.
                self._repair_position_entry(market, amt)
                pos = self.storage.position(market) or pos
                # already tracked: fill in a missing avg entry + exit plan
                # (pre-fix positions were seeded with entry 0)
                if d(pos["avg_entry"]) <= 0:
                    avg, plan, lb_at, b_irt, lbp = \
                        self._reconstruct_entry(market, d(pos["amount"]))
                    if avg > 0:
                        self.storage.upsert_position(
                            market, str(pos["amount"]), str(avg),
                            pos.get("realized_pnl") or "0",
                            last_buy_at=lb_at, total_buys_irt=b_irt,
                            last_buy_price=lbp)
                        if plan:
                            plans = self.storage.kv_get("exit_plans", {})
                            if isinstance(plans, dict):
                                plans[market] = plan
                                self.storage.kv_set("exit_plans", plans)
                        seeded += 1
                else:
                    # wallet grew beyond the tracked amount: a TOP-UP buy
                    # filled — rebuild avg entry / holding timer / invested
                    # total from the fills (which now include the new BUY).
                    # The user's exit plan is never overwritten here.
                    real_amt = d(str(balances.get(base, {}).get(
                        "available", 0))) + d(str(balances.get(base, {})
                                                  .get("locked", 0)))
                    cur = d(pos["amount"])
                    if real_amt > cur * Decimal("1.0000001"):
                        avg, _plan, lb_at, b_irt, lbp = \
                            self._reconstruct_entry(market, real_amt)
                        if avg > 0:
                            prev_irt = d(pos.get("total_buys_irt") or 0)
                            self.storage.upsert_position(
                                market, str(real_amt), str(avg),
                                pos.get("realized_pnl") or "0",
                                last_buy_at=lb_at,
                                total_buys_irt=max(prev_irt, b_irt),
                                last_buy_price=lbp)
                            # SS100d: the wallet grew because a new BUY
                            # filled — re-arm the auto sell-stop under the
                            # new last-buy price and restart the profit
                            # clock for the UI
                            try:
                                self._auto_stop_for_buy(market, lbp
                                                        if lbp and lbp > 0
                                                        else avg)
                            except Exception:
                                pass
                            seeded += 1
                continue
            avg, plan, last_buy_at, buys_irt, lbp = \
                self._reconstruct_entry(market, d(str(amt)))
            self.storage.upsert_position(
                market, str(amt), str(avg), "0",
                last_buy_at=last_buy_at, total_buys_irt=buys_irt,
                last_buy_price=lbp)
            if plan:
                plans = self.storage.kv_get("exit_plans", {})
                if isinstance(plans, dict):
                    plans[market] = plan
                    self.storage.kv_set("exit_plans", plans)
            seeded += 1
        refiled = self._refile_misplaced_positions(targets, costs=costs)
        # هر سکه = یک ردیف: مجموع ردیف‌های یک سکه هرگز نمی‌تواند از موجودی
        # کیف پول بیشتر باشد — وگرنه همان دارایی هم در بخش تتری و هم در بخش
        # تومانی دیده می‌شود (با دو قیمت مختلف).
        refiled += self._reconcile_wallet_totals(balances, targets)
        return seeded + refiled + stale

    def _reconcile_wallet_totals(self, balances: dict, targets: dict) -> int:
        """یک سکه فقط در یک بازار می‌نشیند: جمع ردیف‌ها ≤ موجودی کیف پول.

        The wallet reports ONE balance per coin, so the coin's position
        rows across quote markets must not ADD UP to more than that
        balance. When they do (an old row seeded while the market
        preference was Toman, plus a later row seeded in Tether), the very
        same coins are being shown twice — and the Toman copy of a
        Tether-bought coin is exactly the «duplicate با قیمت USDT» the user
        sees.

        `targets` (base -> the market the user really trades that coin in)
        decides which row survives, with `_market_for_base` as fallback
        (real BUY fills/orders first, configured preference last). The
        surviving row is clamped to the wallet balance, every other market's
        row is zeroed, and its stale exit plan is dropped so it cannot
        light up that section either. A coin whose rows DO fit inside the
        wallet balance is left alone: a genuine split across markets is
        real money and is never destroyed.
        """
        out = 0
        plans = None
        for base, b in (balances or {}).items():
            base = (base or "").strip().upper()
            if not base or base in ("USDT", "IRT"):
                continue
            if not isinstance(b, dict):
                continue
            try:
                total = d(str(b.get("available", 0))) + \
                    d(str(b.get("locked", 0)))
            except Exception:
                continue
            if total <= 0:
                continue        # not held at all — the wallet mirror owns this
            held = []
            for mk in (base + "USDT", base + "IRT"):
                p = self.storage.position(mk)
                if p and d(str(p.get("amount") or 0)) > 0:
                    held.append((mk, p))
            if len(held) <= 1:
                continue
            ssum = ZERO
            for _mk, p in held:
                ssum += d(str(p.get("amount")))
            if ssum <= total * Decimal("1.0000001"):
                continue        # each market's coins fit: a real dual holding
            held_mk = [mk for mk, _p in held]
            keep = (targets or {}).get(base)
            if keep not in held_mk:
                keep = self._market_for_base(base, set(held_mk))
            if keep not in held_mk:
                keep = held_mk[0]
            # سهم بازارِ برنده اول از کیف پول، بقیهٔ موجودی بین ردیف‌های
            # دیگر بازار تقسیم می‌شود — جمع هیچ‌وقت از کیف پول بیشتر نمی‌شود
            order = sorted(held, key=lambda kv: kv[0] != keep)
            remaining = total
            for mk, p in order:
                amt = d(str(p.get("amount")))
                take = min(amt, remaining) if remaining > 0 else ZERO
                remaining -= take
                if take >= amt:
                    continue            # fits inside the wallet — leave it
                # a FULL zero clears the row's money columns too: a leftover
                # total_buys_irt/last_buy_price would keep the section of the
                # dead market looking «invested» forever
                self.storage.upsert_position(
                    mk, take, p.get("avg_entry") or ZERO,
                    p.get("realized_pnl") or ZERO, p.get("side") or "LONG",
                    total_buys_irt=(0 if take <= 0 else None),
                    last_buy_price=(0 if take <= 0 else None))
                if take <= 0:
                    # ردیف صفر شد: پلن حدش هم می‌رود تا تنهایی بخش را
                    # فعال نکند (market_sections روی پلن‌ها هم حساب می‌کند)
                    if plans is None:
                        plans = self.storage.kv_get("exit_plans", {}) or {}
                    if isinstance(plans, dict) and mk in plans:
                        plans.pop(mk, None)
                        self.storage.kv_set("exit_plans", plans)
                out += 1
                self.events.publish(
                    "POSITION_MARKET_REFILED", "engine", "",
                    {"base": base, "from": mk, "to": keep,
                     "amount": str(amt), "kept": str(take),
                     "wallet_total": str(total),
                     "reason": "the wallet holds %s %s in total — its "
                               "rows may never add up to more" %
                               (total, base)})
        return out

    def _refile_misplaced_positions(self, targets: dict,
                                    costs: dict | None = None) -> int:
        """Zero stale rows of a coin tracked in the WRONG quote market.

        The wallet reports ONE balance per coin, so the same coin cannot
        really sit in both the Tether and the Toman box. A row is only
        dropped when the market the user really bought in has been found
        (targets = base -> that market) and the other row's own market has
        NO real buys of its quote: genuine dual holdings (real buys on
        both sides) are always left untouched.
        """
        refiled = 0
        for market, base in sorted(targets.items()):
            hist = (costs or {}).get(str(base).upper()) or {}
            hist_quote = str(hist.get("quote") or "").upper()
            q_by = hist.get("qty_by_quote") or {}
            for other in (base + "USDT", base + "IRT"):
                if other == market:
                    continue
                p = self.storage.position(other)
                if not p or d(str(p.get("amount") or 0)) <= 0:
                    continue
                other_q = str(quote_of_market(other) or "").upper()
                # the exchange ledger says the coins still held came from
                # the TARGET quote and none remain on this side — that is
                # stronger than a local fill which has already been sold
                # (ZEC was bought in Toman but a stale local row filed it
                # under ZECUSDT, so the 24h change was the wrong market's).
                try:
                    left = float(q_by.get(other_q) or 0)
                except (TypeError, ValueError):
                    left = 0.0
                force_other_empty = (hist_quote and
                                     hist_quote != other_q and left <= 0.0)
                if not force_other_empty and \
                        quote_of_market(other) in self._real_buy_activity(base):
                    continue        # real buys exist there too — keep it
                self.storage.upsert_position(
                    other, ZERO, p.get("avg_entry") or ZERO,
                    p.get("realized_pnl") or ZERO, p.get("side") or "LONG",
                    total_buys_irt=0, last_buy_price=0)
                refiled += 1
                self.events.publish(
                    "POSITION_MARKET_REFILED", "engine", "",
                    {"base": base, "from": other, "to": market,
                     "amount": str(p.get("amount")),
                     "reason": "real buys are on the %s side" % market})
        return refiled

    def _repair_position_entry(self, market: str, held=None) -> dict | None:
        """تعمیر «میانگین خرید»ی که با فیل‌های خودمان نمی‌خواند.

        زندهٔ واقعی: SUSDT با میانگین خرید ۱۶٫۵$ ذخیره شده بود در حالی
        که همهٔ خریدهایش حدود ۰٫۰۴$ بوده‌اند. اثرش فقط نمایشِ خراب نبود:
        «مبلغ خرید» ۳۹۰ برابر واقعیت می‌شد (و «سود و زیان این بازار»
        تتری ‎-۲۳۳$ از هیچ در می‌آمد) و در فروشِ همان سکه، دفتر با
        realized = فروش − میانگین×تعداد یک «زیان محقق‌شدهٔ» جعلی می‌ساخت.
        پس نه نمایش، بلکه خودِ دفتر از فیل‌های خودِ ربات تعمیر می‌شود.

        فقط عددهای دور از واقعیت (بیش از ۵ برابر یا کمتر از یک‌پنجمِ
        میانگینِ فیل‌ها) دست‌کاری می‌شوند؛ میانگینِ سالم، توقفِ حد ضرر و
        پلهٔ کاربر هرگز لمس نمی‌شوند (نقشهٔ خروج عوض نمی‌شود).
        """
        pos = self.storage.position(market)
        if not pos:
            return None
        cur = d(str(pos.get("avg_entry") or 0))
        if cur <= 0:
            return None            # ناشناخته — چیزی برای مقایسه نیست
        amount = d(str(pos.get("amount") or 0)) if held is None \
            else d(str(held))
        if amount <= 0:
            return None
        avg, _plan, lb_at, cost, lbp = self._reconstruct_entry(market, amount)
        if avg <= 0:
            return None            # فیل‌ی از این سکه نداریم؛ دست نمی‌زنیم
        ratio = cur / avg
        if Decimal("0.2") <= ratio <= Decimal("5"):
            return None            # اختلاف طبیعی (کارمزد/تاپ‌آپ) — سالم است
        total = d(str(pos.get("total_buys_irt") or 0))
        new_total = None
        if cost > 0 and (total <= 0
                         or total / cost > Decimal("5")):
            new_total = cost
        self.storage.upsert_position(
            market, str(pos.get("amount") or amount), str(avg),
            pos.get("realized_pnl") or "0", pos.get("side") or "LONG",
            last_buy_at=None,          # تایمر نگه‌داری و قیمت آخرین خرید
            total_buys_irt=new_total,  # و نقشهٔ خروج کاربر دست‌نخورده
            last_buy_price=None)
        out = {"market": market, "from": float(cur), "to": float(avg),
               "fills_avg": float(avg), "total_buys_irt":
               float(new_total) if new_total is not None else None}
        self.events.publish("POSITION_ENTRY_REPAIRED", "engine", market,
                            out)
        return out

    def _reconstruct_entry(self, market: str, held: Decimal):
        """(avg_entry, exit_plan, last_buy_at, total_buys_irt) for a held
        coin from our own fills.

        The bot records every live fill with its decision id, so a restart
        can rebuild the position's avg entry, the stop/TP plan, the holding
        timer (most recent BUY) and the cumulative invested Toman exactly.
        """
        base = market[:-3] if market.endswith("IRT") else (
            market[:-4] if market.endswith("USDT") else market)
        # every fill of this base across BOTH market NAMES (bot symbol
        # ARBIRT vs exchange-facing ARBITRUMIRT, GIRT vs GRAVITYIRT) —
        # the wallet coin is ONE holding, so its entry rebuild must see
        # every real BUY/SELL of that base (scoped query: the global
        # LIMIT window would be flooded by old demo fills).
        # BUT only within the SAME quote asset: Rial/Toman fills and
        # tether fills are different money. Weighting a 0.0745 USDT buy
        # together with 10,000-Toman IRT-era buys of the same coin mixed
        # two units and produced a fake ~-205,000 USDT «loss» the moment
        # the first USDT buy of a previously IRT-traded coin filled.
        quote = "USDT" if market.upper().endswith("USDT") else "IRT"
        # همان بازاری که LIKE-Prefix می‌آورد باید **همان سکه** باشد: برای
        # بازار یک‌حرفی مثل SUSDT، `base` = "S" است و LIKE 'S%' خرید‌های
        # SOLUSDT/STRKUSDT/... را هم می‌آورد؛ اگر وارد میانگین شوند،
        # «میانگین خرید» و «سود محقق‌شدهٔ» همان سکه بیگانه می‌شود (و
        # تعمیرِ خودکارِ همین تابع داده‌ی سالم را هم خراب می‌کند). پس
        # برابری بر پایهٔ (سکه، واحد پول) پس از یکسان‌سازی نام‌ها.
        want = split_symbol(canonical_symbol(market))
        fills = [f for f in self.storage.fills_by_base(base, limit=200)
                 if split_symbol(canonical_symbol(
                     f.get("market") or "")) == want]
        if not fills:
            return ZERO, None, None, ZERO, ZERO
        # avg entry = weighted average of the BUYs (sells must NOT inflate
        # the entry of the coins still held — subtracting sold qty while
        # keeping full cost tripled ARB's entry once).
        qty = ZERO
        cost = ZERO
        last_buy_fill = None
        last_buy_ts = None
        last_buy_px = None
        for f in fills:
            side = (f.get("side") or "").upper()
            amount = d(str(f.get("amount") or 0))
            price = d(str(f.get("price") or 0))
            if side == "BUY" and amount > 0 and price > 0:
                qty += amount
                cost += amount * price
                if last_buy_fill is None:
                    last_buy_fill = f
                ts = f.get("timestamp") or ""
                if ts and (last_buy_ts is None or ts > last_buy_ts):
                    last_buy_ts = ts
                    last_buy_px = price
        if qty <= 0 or cost <= 0:
            return ZERO, None, None, ZERO, ZERO
        avg = cost / qty
        # exit plan from the decision that produced the most recent BUY
        plan = None
        dec_id = (last_buy_fill or {}).get("decision_id") or ""
        if dec_id:
            dec = self.storage.ai_decision(dec_id)
            if dec:
                sl = dec.get("stop_loss") or {}
                tps = dec.get("take_profit") or []
                if d(str(sl.get("price", 0))) > 0 and tps:
                    plan = {
                        "stop": str(sl["price"]),
                        "stage": 0,
                        "tps": [{"price": str(t.get("price")),
                                 "close_percent": int(t.get(
                                     "close_percent", 50))} for t in tps],
                        "rebuilt_from": dec_id,
                    }
        return avg, plan, last_buy_ts, cost, last_buy_px

    def set_buy_best(self, enabled: bool) -> dict:
        """Toggle the buy-the-best-option switch (ai.buy_best)."""
        self.cfg.ai.buy_best = bool(enabled)
        self.events.publish("BUY_BEST_CHANGED", "engine", "",
                            {"enabled": bool(enabled)})
        return {"ok": True, "buy_best": self.cfg.ai.buy_best}

    # ------------------------------------------------------ risk mode
    def set_risk_mode(self, enabled: bool) -> dict:
        """حالت ریسک: خرید پرسودترین‌های ۲۴ساعته، فروش سریع هنگام افت
        و چرخش سریع سرمایه به پرسودترین بعدی.

        فقط فلگ سوییچ می‌شود؛ scanner (رتبه‌بندی ۲۴ساعته)، خروج‌ها
        (حد ضرر/سود و drop-exit)، cooldown خرید (risk.py) و ورود
        (ai.py) در نقطه استفاده حالت را می‌خوانند. مثل بقیه سوییچ‌ها
        (buy_best و…) مقدار با ذخیره تنظیمات ماندگار می‌شود."""
        on = bool(enabled)
        self.cfg.risk.risk_mode = on
        self.risk_mode = on
        self.events.publish("RISK_MODE_CHANGED", "engine", "",
                            {"enabled": on})
        return {"ok": True, "risk_mode": self.risk_mode}

    def execute_best_trade(self) -> dict:
        """One-shot "execute the best trade now" button.

        Picks the top-ranked market(s) and pushes a synthesized BUY through
        the FULL SS100 pipeline (validation -> risk -> execution). Only the
        AI's action choice is overridden — the risk engine stays sovereign
        (SS33/34): real balance, min-notional and all limits still apply.
        Tries up to 5 candidates in rank order and reports exactly why each
        attempt failed so the dashboard can show the real reason.
        """
        if self.safe_mode:
            return {"ok": False, "error": "SAFE_MODE — سفارش جدید مسدود است"}
        if not self.auto_trading:
            self.auto_trading = True
        if not self.latest_snapshot or not self.latest_candidates:
            self.run_cycle()
        snap = self.latest_snapshot or {}
        market_by_symbol = {
            m["symbol"]: m for m in snap.get("markets", [])
            if (m.get("data_quality") or {}).get("score", 0)
            >= self.cfg.quality.min_trade_quality}
        candidates = [c for c in (self.latest_candidates or [])
                      if c["symbol"] in market_by_symbol]
        if not candidates:
            return {"ok": False, "error": "بازار مجازی برای خرید نیست"}
        prices = {m["symbol"]: m["last_price"] for m in snap["markets"]
                  if m.get("last_price")}
        portfolio_snapshot = self.portfolio.portfolio_snapshot(
            prices, self._usdt_rates(prices))
        open_positions = {
            p["market"]: {"amount": float(d(p["amount"]))}
            for p in self.storage.positions() if d(p["amount"]) > 0}
        attempted = []
        for cand in candidates[:5]:
            m = market_by_symbol.get(cand["symbol"])
            if not m:
                continue
            if float(open_positions.get(cand["symbol"], {}).get(
                    "amount", 0) or 0) > 0:
                continue  # no pyramiding (SS32)
            boosted = {**cand, "signals": {
                **cand["signals"],
                "trend": max(cand["signals"].get("trend", 0), 0.9),
                "momentum": max(cand["signals"].get("momentum", 0), 0.9),
            }}
            dec = self.ai.heuristic.decide(
                boosted, m, portfolio_snapshot, open_positions, prices)
            if dec is None or dec.get("action") not in ("BUY", "SELL") \
                    or not (dec.get("entry") or {}).get("price"):
                # heuristic returned HOLD/AVOID (no entry plan): synthesize a
                # complete percentage-based BUY plan around the live price
                dec = self._synthesize_buy_plan(cand, m, portfolio_snapshot,
                                                prices)
            # freshness: the synthesized decision must not inherit a stale
            # market snapshot timestamp or validation/risk rejects it
            dec["snapshot_id"] = m.get("snapshot_id", "")
            dec["cycle_id"] = m.get("cycle_id", "best_trade")
            dec["created_at"] = utc_now_iso()
            dec["expires_at"] = (datetime.now(timezone.utc) +
                                 timedelta(seconds=self.cfg.risk.decision_ttl_s)
                                 ).isoformat().replace("+00:00", "Z")
            dec["reason_codes"] = ["MANUAL_BEST_TRADE"] + list(
                dec.get("reason_codes", []))[:5]
            vres = ValidationResult(ok=True)
            validate_ai_decision(dec, self._allowed_symbols(market_by_symbol), vres)
            if not vres.ok:
                attempted.append({"symbol": cand["symbol"], "ok": False,
                                  "error": "AI_VALIDATION: " + "; ".join(
                                      "%s:%s" % (i.field, i.code)
                                      for i in vres.issues[:3])})
                continue
            self.storage.save_ai_decision(dec, True)
            self.latest_decisions.append(dec)
            rd = self.risk.evaluate(dec, m, portfolio_snapshot, prices)
            self.storage.save_risk_decision(rd)
            self.latest_risk.append(rd)
            if rd["status"] != "APPROVED":
                attempted.append({"symbol": cand["symbol"], "ok": False,
                                  "rejected": rd["rejection_reasons"],
                                  "size": rd.get("approved_position_size")})
                continue
            ex = self.executor.execute(
                dec, rd, prices, "manual_" +
                utc_now_iso().replace("-", "").replace(":", "")[:12])
            self.latest_executions.append(ex)
            attempted.append({"symbol": cand["symbol"], "ok": True,
                              "execution": ex})
            self.events.publish("BEST_TRADE_EXECUTED", "engine", "",
                                {"symbol": cand["symbol"],
                                 "status": ex.get("status")})
            return {"ok": True, "symbol": cand["symbol"],
                    "execution": ex, "attempted": attempted}
        return {"ok": False, "error": "هیچ بازاری قابل اجرا نبود",
                "attempted": attempted}

    def _synthesize_buy_plan(self, cand: dict, m: dict,
                             portfolio_snapshot: dict, prices: dict) -> dict:
        """Complete percentage-based BUY plan around the live price.

        Used by the "execute best trade" button when the heuristic returned
        HOLD/AVOID (no entry plan). Realistic pullback entry, min-stop-distance
        stop and a 3-step TP ladder; the risk engine re-sizes/re-clamps anyway.
        """
        sym = cand["symbol"]
        last = float(m.get("last_price") or prices.get(sym) or 0)
        entry_pct = float(getattr(self.cfg.ai, "entry_pullback_pct", 0.0025))
        stop_pct = float(getattr(self.cfg.risk,
                                 "min_stop_distance_pct", 0.012))
        entry = last * (1 - entry_pct)
        stop = entry * (1 - stop_pct)
        now_dt = datetime.now(timezone.utc)
        return {
            "decision_id": "dec_%s_%s" % (
                utc_now_iso().replace("-", "").replace(":", "")[:15],
                new_id("x")[2:10]),
            "snapshot_id": m.get("snapshot_id", ""),
            "cycle_id": m.get("cycle_id", "best_trade"),
            "symbol": sym,
            "action": "BUY",
            "confidence": 0.9,
            "time_horizon": "INTRADAY",
            "reason_codes": ["MANUAL_BEST_TRADE", "PERCENT_PLAN"],
            "created_at": utc_now_iso(),
            "expires_at": (now_dt + timedelta(
                seconds=self.cfg.risk.decision_ttl_s)
            ).isoformat().replace("+00:00", "Z"),
            "entry": {"price": round(entry, 8),
                      "min": round(entry * 0.998, 8),
                      "max": round(entry * 1.002, 8)},
            "stop_loss": {"price": round(stop, 8),
                          "method": "PERCENT_STOP",
                          "reason": "MANUAL_BEST_TRADE",
                          "distance_percent": stop_pct},
            "take_profit": [
                {"price": round(entry * (1 + stop_pct * 1.5), 8),
                 "close_percent": 50},
                {"price": round(entry * (1 + stop_pct * 2.2), 8),
                 "close_percent": 30},
                {"price": round(entry * (1 + stop_pct * 3.0), 8),
                 "close_percent": 20}],
            "position_size": {"amount": 1.0, "quote_value": float(entry)},
            "risk": {"max_loss": 0.0},
            "cost": {"expected_net_return": 0.01},
            "invalidation_conditions": ["BREAK_SUPPORT"],
        }

    def execute_decision(self, decision_id: str) -> dict:
        """Re-run a specific AI decision through risk + execution on demand.

        Nothing bypasses SS100: the decision is re-validated, re-priced
        against the fresh snapshot, and re-evaluated by the risk engine.
        Only the automatic trigger is replaced by the user's click.
        """
        row = None
        for x in self.storage.decisions_recent(300):
            if x["decision_id"] == decision_id:
                row = x
                break
        if not row:
            return {"ok": False, "error": "تصمیم یافت نشد"}
        dec = row.get("payload") or {}
        if not dec or dec.get("action") not in ("BUY", "SELL"):
            return {"ok": False, "error": "این تصمیم قابل اجرا نیست (خرید/فروش نیست)"}
        snap = self.latest_snapshot or {}
        market_by_symbol = {m["symbol"]: m for m in snap.get("markets", [])}
        m = self._market_row(dec.get("symbol") or "")
        if not m:
            return {"ok": False,
                    "error": "قیمت زنده برای " + str(dec.get("symbol")) +
                    " پیدا نشد — نماد را بررسی کنید یا چند لحظه بعد "
                    "دوباره تلاش کنید"}
        market_by_symbol.setdefault(m["symbol"], m)
        prices = {mm["symbol"]: mm["last_price"] for mm in snap["markets"]
                  if mm.get("last_price")}
        prices.setdefault(m["symbol"], m.get("last_price"))
        portfolio_snapshot = self.portfolio.portfolio_snapshot(
            prices, self._usdt_rates(prices))
        # fresh id + TTL so the freshness/duplicate checks judge the
        # re-triggered decision, not the stale original
        now_dt = datetime.now(timezone.utc)
        dec["decision_id"] = dec["decision_id"] + "_r" + new_id("x")[2:8]
        dec["created_at"] = utc_iso(now_dt)
        dec["expires_at"] = utc_iso(now_dt + timedelta(
            seconds=self.cfg.risk.decision_ttl_s))
        vres = ValidationResult(ok=True)
        validate_ai_decision(dec, set(market_by_symbol.keys()), vres)
        if not vres.ok:
            return {"ok": False, "error": "AI_VALIDATION: " + vres.first_code()}
        self.storage.save_ai_decision(dec, True)
        self.latest_decisions.append(dec)
        rd = self.risk.evaluate(dec, m, portfolio_snapshot, prices)
        self.storage.save_risk_decision(rd)
        self.latest_risk.append(rd)
        if rd["status"] != "APPROVED":
            return {"ok": False,
                    "error": "رد توسط موتور ریسک: " +
                    "، ".join(rd["rejection_reasons"]),
                    "rejection_reasons": rd["rejection_reasons"]}
        ex = self.executor.execute(
            dec, rd, prices, "manual_" +
            utc_now_iso().replace("-", "").replace(":", "")[:12])
        self.latest_executions.append(ex)
        return {"ok": True, "execution": ex}

    # ------------------------------------------------- manual buy (holdings)
    def set_deposited_capital(self, amount_irt: float) -> dict:
        """Set the total Toman the user has DEPOSITED into the bot
        (کل سرمایه واریزی) — the «سود کل» KPI splits the live since-buy
        profit against this number. Stored in kv_state; persists."""
        try:
            amt = d(str(amount_irt))
        except Exception:
            return {"ok": False, "error": "مبلغ نامعتبر است"}
        if amt < 0:
            return {"ok": False, "error": "مبلغ نمی‌تواند منفی باشد"}
        self.storage.kv_set("deposited_capital_irt", float(amt))
        self.events.publish("DEPOSITED_CAPITAL_SET", "engine", "",
                            {"amount_irt": float(amt)})
        return {"ok": True, "deposited_irt": float(amt)}

    def get_deposited_capital(self) -> float:
        v = self.storage.kv_get("deposited_capital_irt", 0) or 0
        try:
            return float(v)
        except Exception:
            return 0.0

    # --------------------------------------- real net-deposited capital
    def _net_deposited_from_exchange(self) -> dict:
        """کل سرمایه واریزی from REAL Nobitex turnover data:
        Σdeposits − Σwithdraws (the exact numbers the dashboard pages
        /panel/turnover/deposit and /panel/turnover/withdraw show).

        Currencies are valued into Toman with live market rates (Rial
        wallets ÷ divisor, USDT/coins via their USDT market). Cached in
        kv_state and refreshed at most once per hour so the engine cycle
        never hammers the endpoint; 'refresh' forces a live re-fetch, and
        a wallet-level flow seen by the ledger sync (portfolio.flow_dirty)
        forces one too — the cached capital must never lag behind a real
        withdrawal, because that lag reads as a fake loss.

        What counts (user report: a withdrawal made «سود سرمایه» negative):
        • a withdrawal is counted as soon as the exchange ACCEPTS it — the
          wallet is debited then, while the status can stay "Bank
          processing" for hours. Excluding it dropped the capital only
          once it reached "Done", so in between the account value fell
          while «کل سرمایه واریزی» did not → −2.42M Toman of phantom loss.
          Only failed/rejected/canceled/returned items are left out.
        • a deposit counts only once it LANDED (Succeed/executed/…): a
          pending or rejected deposit is not on the wallet, so counting it
          would fake profit. Items with no status at all (older bank
          deposits) are counted, which is what they always did.
        """
        now = time.time()
        cached = self.storage.kv_get("net_deposited_cache") or {}
        try:
            cached_at = float(cached.get("at") or 0)
            cache_ttl = float(getattr(self.cfg.exchange,
                                      "net_deposit_refresh_s", 3600) or 3600)
        except Exception:
            cached_at, cache_ttl = 0.0, 3600.0
        # a wallet-level transfer was just seen by the ledger sync (money
        # entered or left the account and no fill explains it) -> the cached
        # turnover is STALE by definition. Serving it made «کل سرمایه واریزی»
        # lag behind the account value, and the gap showed up as a fake
        # profit/loss in «سود سرمایه» (a 2.42M Toman withdrawal read as a
        # −2.43M loss until the cache expired an hour later).
        force = False
        if getattr(self.portfolio, "flow_dirty", False):
            force = True
        cache_current = (int(cached.get("v") or 0) ==
                         NET_DEPOSIT_CACHE_VERSION)
        if not force and cache_current and cached.get("net_irt") is not None \
                and now - cached_at < cache_ttl:
            return {**cached, "fresh": False}
        try:
            data = self.exchange.deposits_withdraws()
        except Exception as exc:
            # keep the flag so the next read tries again (the balance and
            # the capital must not stay out of sync for a whole TTL)
            if cached.get("net_irt") is not None:
                return {**cached, "fresh": False,
                        "stale": True, "error": str(exc)}
            return {"net_irt": None, "error": str(exc), "fresh": False}
        div = float(getattr(self.cfg.exchange, "rls_to_irt_divisor", 10) or 10)
        prices = {}
        try:
            snap = self.collector.latest_snapshot()
            for row in ((snap or {}).get("markets") or []):
                if row.get("symbol") and row.get("last_price"):
                    prices[row["symbol"]] = float(row["last_price"])
        except Exception:
            prices = {}
        rates = self._usdt_rates(prices) if prices else {}
        usdtirt = 0.0
        try:
            t = self.exchange.ticker("USDTIRT")
            usdtirt = float(t.get("last_price") or 0)
        except Exception:
            usdtirt = float(rates.get("IRT") or 0)

        def _irt(amount: float, cur) -> float:
            # IRT bank deposits ship with NO currency key — Nobitex's
            # internal unit for them is Rial (same as withdrawals' "rls").
            cur = (cur or "rls").strip().lower()
            if cur == "rls":
                return amount / div
            if cur == "irt":
                return amount
            coin = cur.upper()
            if coin == "USDT":
                return amount * (usdtirt or 0)
            r = float(rates.get(coin) or 0)          # 1 unit in USDT
            if not r:
                return 0.0
            return amount * r * (usdtirt or 0)

        def _item_date(item: dict) -> str:
            """Timestamp of a turnover item: bank withdrawals/deposits expose
            `createdAt`, the older statement-based rows expose `date`."""
            for key in ("createdAt", "created_at", "date"):
                v = item.get(key)
                if isinstance(v, str) and v:
                    return v[:19]
            return ""

        def _status_of(item: dict) -> str:
            """Status text of a turnover item: the top-level field, else the
            bank statement's own status (cobank card deposits ship NO
            top-level status, only statement.status = executed/rejected)."""
            st = str(item.get("status") or "").strip().lower()
            if st:
                return st
            stmt = item.get("statement")
            if isinstance(stmt, dict):
                return str(stmt.get("status") or "").strip().lower()
            return ""

        # What "still exists" means on each side of the ledger (user
        # report: «وقتی برداشت می‌کنم سود سرمایه منفی می‌خورد» — the
        # withdrawal had already LEFT the wallet while still sitting at
        # "Bank processing", so equity dropped and capital did not):
        # • a WITHDRAWAL debits the wallet when the request is accepted;
        #   only a failed/rejected/canceled/returned transfer never took
        #   the money out — so those, and only those, are left out.
        # • a DEPOSIT exists in the wallet only once the exchange says it
        #   landed — a pending/rejected one must NOT count (its money is
        #   not on the wallet yet, so counting it would fake profit).
        DEAD = ("fail", "reject", "cancel", "return", "refund", "expire")
        NOT_LANDED = DEAD + ("pending", "processing", "waiting", "progress",
                             "queue", "hold")

        def _withdraw_committed(item: dict) -> bool:
            st = _status_of(item)
            return not any(k in st for k in DEAD)

        def _deposit_landed(item: dict) -> bool:
            st = _status_of(item)
            if not st:                 # legacy bank deposits: no status at
                return True            # all -> the money IS on the wallet
            return not any(k in st for k in NOT_LANDED)

        dep_sum = wdr_sum = ZERO
        dep_rls = wdr_rls = 0
        # audit trail: every item the exchange reported with the Toman
        # value it was counted at, so a unit/currency/status surprise in
        # the raw data is inspectable instead of silently shifting
        # «سود سرمایه» (the UI tooltip shows this list on demand).
        audit: list = []
        for item in (data.get("deposits") or []):
            try:
                st = _status_of(item)
                amt = float(item.get("amount") or 0)
                fee = float(item.get("fee") or 0)   # credited = gross − fee
                cur = item.get("currency")          # may be None → Rial
                counted = _deposit_landed(item)
            except Exception:
                continue
            irt_v = d(str(_irt(amt - fee, cur)))
            if counted:
                dep_sum += irt_v
                dep_rls += 1
            audit.append({"kind": "deposit", "status": st,
                          "currency": cur, "amount": amt, "fee": fee,
                          "irt": float(round(irt_v, 2)), "counted": counted,
                          "date": _item_date(item)})
        for item in (data.get("withdraws") or []):
            try:
                st = _status_of(item)
                amt = float(item.get("amount") or 0)
                cur = item.get("currency")
                counted = _withdraw_committed(item)
            except Exception:
                continue
            irt_v = d(str(_irt(amt, cur)))
            if counted:
                wdr_sum += irt_v
                wdr_rls += 1
            audit.append({"kind": "withdraw", "status": st,
                          "currency": cur, "amount": amt,
                          "irt": float(round(irt_v, 2)), "counted": counted,
                          "date": _item_date(item)})
        net = dep_sum - wdr_sum
        out = {"v": NET_DEPOSIT_CACHE_VERSION,
               "at": now, "net_irt": float(round(net, 2)),
               "deposits_irt": float(round(dep_sum, 2)),
               "withdraws_irt": float(round(wdr_sum, 2)),
               "deposits_count": dep_rls, "withdraws_count": wdr_rls,
               # pagination audit: how many pages the FULL turnover needed
               # and whether a cap stopped us before `hasNext` ended
               "pages": int(data.get("pages") or 1),
               "truncated": bool(data.get("truncated")),
               # «در جریان»: a withdrawal that already left the wallet but
               # has not settled at the bank yet (counted in withdraws_irt)
               "pending_withdraws_irt": float(round(sum(
                   d(str(a["irt"])) for a in audit
                   if a["kind"] == "withdraw" and a["counted"] and
                   not any(k in a["status"] for k in ("done", "sent",
                                                      "paid"))), 2)),
               "audit": audit[-60:]}
        self.storage.kv_set("net_deposited_cache", out)
        # the FULL list is in hand now: rewrite the recent equity points'
        # «کل سرمایه واریزی» so the 24h box does not compare today's correct
        # number with a baseline built from the truncated list (that reads
        # as a one-day fake loss after the paging fix)
        self._repair_equity_deposits(out)
        try:                     # cache is live again: the ledger flow that
            self.portfolio.flow_dirty = False   # forced this has been read
        except Exception:
            pass
        return {**out, "fresh": True}

    def _repair_equity_deposits(self, nd: dict) -> None:
        """Rewrite recent `d` (کل سرمایه واریزی) values in the equity history.

        Each history point stores the capital at that minute. Points written
        before the turnover list was paginated carry a truncated capital, so
        once the current net is correct the 24h box would subtract a wrong
        baseline and report a fake loss for a day.

        The transfer audit has every deposit/withdraw with its date, so the
        capital at any past time is reconstructable:
            d(t) = net_now − Σ(transfers AFTER t)
        Only the last 26h matter (that is the 24h box's window). If the audit
        was capped (60 rows) completeness is not guaranteed, so we skip
        rather than write a guess.
        """
        try:
            audit = nd.get("audit") or []
            if not audit or len(audit) >= 60:
                return
            flows = []
            for a in audit:
                if not a.get("counted"):
                    continue
                ts = str(a.get("date") or "")[:19]
                if not ts:
                    return
                sign = 1 if a.get("kind") == "deposit" else -1
                flows.append((ts, sign * float(a.get("irt") or 0)))
            net = float(nd.get("net_irt") or 0)
            hist = self.storage.kv_get("equity_history", [])
            if not isinstance(hist, list) or not hist:
                return
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=26)) \
                .strftime("%Y-%m-%dT%H:%M:%S")
            changed = False
            for pt in hist:
                if not isinstance(pt, dict):
                    continue
                t = str(pt.get("t") or "")[:19]
                if not t or t < cutoff:
                    continue
                after = sum(v for ts, v in flows if ts > t)
                d_new = round(net - after, 2)
                try:
                    same = abs(float(pt.get("d") or 0) - d_new) <= 1
                except Exception:
                    same = False
                if not same:
                    pt["d"] = d_new
                    changed = True
            if changed:
                self.storage.kv_set("equity_history", hist)
        except Exception:
            pass

    def refresh_net_deposited(self) -> dict:
        """Force a live re-fetch of the exchange turnover (UI button)."""
        self.storage.kv_set("net_deposited_cache", {})
        return self._net_deposited_from_exchange()

    def _seed_deposited_capital(self, invested_irt: float) -> None:
        """First run only: if the user never set a deposited amount,
        seed it with the current capital-at-work so the split shows
        something sensible instead of zeros."""
        try:
            if invested_irt > 0 and \
                    self.storage.kv_get("deposited_capital_irt") is None:
                self.storage.kv_set("deposited_capital_irt",
                                    float(invested_irt))
        except Exception:
            pass

    @staticmethod
    def _display_step(quote: str) -> Decimal:
        """Display precision of one market's money.

        Dollars are printed with cents (2dp), Toman with no fraction
        (0dp). The holdings box sums the VERY numbers its rows print —
        «سود و زیان این بازار» = Σ(ارزش − مبلغ خرید) per coin — so the
        per-row quantization has to happen at the same precision the UI
        prints, not on the grand total.
        """
        return (Decimal("0.01") if str(quote).upper() == "USDT"
                else Decimal("1"))

    def _holdings_pnl(self, prices: dict, ps: dict | None = None) -> dict:
        """LIVE سود کل over the holdings box — built from exactly the
        same per-row «سود از خرید» numbers the holdings table shows:
        for every held coin, profit = value − invested (invested at the
        row's weighted average cost — the same basis as its percent).
        Recomputed on every status() call, so the KPI moves with the rows
        in real time.

        PER-MARKET SPLIT (`by_market`): Toman (IRT) and Tether (USDT)
        are two different moneys and are NEVER summed into one number.
        `invested_irt`/`value_irt`/`pnl_irt`/`pnl_pct` at the top level
        are the TOMAN markets only (their own name says تومان) — a
        Tether row's invested = amount × entry is dollars, and adding it
        to a Toman row produced a meaningless mixed sum. Each market's
        numbers are in that market's OWN quote unit: Toman for *IRT,
        dollars for *USDT.

        TWO distinct percents, clearly separated:
        • growth_pct  → headline of the «سود کل» KPI: REAL growth of the
          deposited capital (equity − deposited) / deposited — includes
          realized PnL, fees and cash.
        • pnl_pct     → the open rows' aggregate «سود از خرید» percent
          (Σpnl / Σinvested) — informational only, NO realized PnL/fees.

        Also reports the DEPOSITED-capital split (کل سرمایه واریزی):
        میزان سود سرمایه = live profit; کل سرمایه واریزی = what the user
        deposited; جمع = the two summed (the account's total money)."""
        usdtirt = float(self._usdt_rates(prices).get("IRT") or 0)
        # one bucket per quote market, each in its own currency — and every
        # row enters its bucket with the VERY numbers the UI prints for it
        # (value_disp/invested_disp: cents for dollars, whole Toman for
        # تومان). «سود و زیان این بازار» = Σ(ارزش − مبلغ خرید) per coin,
        # so the header is literally the sum of the visible rows; summing
        # raw values and rounding the total instead made them disagree by
        # one cent / one Toman (rows $1.26 while the header said $1.25).
        by = {q: {"invested": ZERO, "value": ZERO, "pnl": ZERO, "rows": 0}
              for q in ("USDT", "IRT")}
        rows_all = 0
        for p in self.storage.positions():
            mk = p["market"]
            if d(p["amount"]) <= 0 or mk in ("USDT", "IRT", "USDTIRT"):
                continue
            q = quote_of_market(mk) or "USDT"
            if mk not in prices:
                px = self._price_fallback(mk)
                if px:
                    prices[mk] = px
            row = self._position_row(p, prices, usdtirt)
            agg = by.setdefault(q, {"invested": ZERO, "value": ZERO,
                                    "pnl": ZERO, "rows": 0})
            # a row whose خرید basis is unknown has no (ارزش − مبلغ خرید)
            # to add — counting its whole value as profit is how the
            # phantom ‎-۲۳۳$ / +$235 numbers used to appear.
            if (row.get("invested_disp") is not None and
                    row.get("value_disp") is not None):
                agg["invested"] += d(str(row["invested_disp"]))
                agg["value"] += d(str(row["value_disp"]))
                if row.get("pnl_money") is not None:
                    agg["pnl"] += d(str(row["pnl_money"]))
            agg["rows"] += 1
            rows_all += 1
        by_market = {}
        for q, a in by.items():
            by_market[q] = {
                # already at display precision, so these sums are exactly
                # the numbers printed on the rows (no second rounding)
                "invested": float(a["invested"]),
                "value": float(a["value"]),
                "pnl": float(a["pnl"]),
                "pnl_pct": (float((a["pnl"] / a["invested"] * 100)
                                  .quantize(Decimal("0.01")))
                            if a["invested"] > 0 else None),
                # display precision of this market's money (for the UI)
                "display_step": float(self._display_step(q)),
                "rows": a["rows"],
            }
        # top-level `_irt` numbers = the TOMAN market only; the Tether
        # market is reported separately under by_market.USDT and is never
        # added into them (mixed-unit sums used to be reported as Toman).
        invested = d(str(by_market["IRT"]["invested"]))
        value = d(str(by_market["IRT"]["value"]))
        pnl = value - invested
        pct = by_market["IRT"]["pnl_pct"]
        deposited = d(str(self.get_deposited_capital()))
        self._seed_deposited_capital(float(invested))
        # NOTE: «کل سرمایه واریزی» is a Toman concept, so it is seeded/read
        # from the Toman side only — a Tether position's dollars are never
        # counted as if they were Toman deposited.
        # Prefer the REAL exchange turnover (Σdeposits − Σwithdraws from
        # /users/wallets/deposits/list) over the manual override — it is
        # checked fresh at most hourly and cached in kv_state.
        nd = self._net_deposited_from_exchange()
        nd_net = nd.get("net_irt")
        used_exchange = bool(nd_net is not None and float(nd_net) != 0.0)
        if used_exchange:
            # a live read is authoritative — INCLUDING a negative net (the
            # user has taken out more than they put in): «سود سرمایه» =
            # ارزش حساب − کل سرمایه واریزی stays right with a negative
            # capital, so it must not be overwritten by the book value.
            deposited = d(str(nd_net))
        if deposited <= 0 and not (used_exchange and float(nd_net) < 0):
            # nothing usable (no read, or an account with no turnover yet)
            # -> the book value at work is the best proxy we have
            deposited = invested
        # میزان سود سرمایه must be the growth of the WHOLE account
        # (cash + positions + realized PnL + fees) over the deposited
        # capital — NOT just the open rows' unrealized sum. Otherwise
        # سود سرمایه + سرمایه واریزی could exceed ارزش کل حساب: realized
        # losses/fees of closed trades live only in the equity number.
        # With the equity-based split: جمع == ارزش کل exactly.
        eq_irt = 0.0
        try:
            acc = (ps or {}).get("account") or {}
            eq_irt = float(acc.get("equity_irt") or 0)
        except Exception:
            eq_irt = 0.0
        growth_pct = None
        if eq_irt > 0:
            profit_capital = d(str(eq_irt)) - deposited
            total_irt = d(str(eq_irt))
            # headline percent of the «سود کل» KPI: the REAL growth of
            # the deposited capital = profit_capital / deposited. The old
            # headline (pct above = Σ of the rows' «سود از خرید» percents)
            # ignores realized losses/fees and weighs coins by invested
            # size inconsistently — it could show +31% while the account
            # actually grew +0.8%. The UI headline now reads growth_pct;
            # the rows-sum percent stays available as "pnl_pct" (باز) for
            # anyone who wants the open-positions-only view.
            if deposited > 0:
                growth_pct = float((profit_capital / deposited * 100)
                                   .quantize(Decimal("0.01")))
        else:
            # no equity available (rare) — fall back to the row sum
            profit_capital = pnl
            total_irt = pnl + deposited
            growth_pct = pct
        return {
            "invested_irt": float(round(invested, 2)),
            "value_irt": float(round(value, 2)),
            "pnl_irt": float(round(pnl, 2)),
            "pnl_pct": pct,
            # REAL headline percent for the «سود کل» KPI: growth of the
            # whole account over the deposited capital (toman-consistent
            # with profit_capital_irt / deposited_irt below).
            "growth_pct": growth_pct,
            "rows": rows_all,
            # تفکیک کامل دو بازار: هر بازار موجودی/ارزش/سود و زیان خودش
            "by_market": by_market,
            # convenience mirrors of the Tether side, always in dollars
            "usdt_invested": by_market["USDT"]["invested"],
            "usdt_value": by_market["USDT"]["value"],
            "usdt_pnl": by_market["USDT"]["pnl"],
            "usdt_pnl_pct": by_market["USDT"]["pnl_pct"],
            # the deposited-capital split shown under the سود کل KPI
            "profit_capital_irt": float(round(profit_capital, 2)),
            "deposited_irt": float(round(deposited, 2)),
            "total_irt": float(round(total_irt, 2)),
            # provenance of the deposited number (exchange turnover vs manual)
            "deposited_source": "exchange" if used_exchange else "manual",
            "deposited_detail": nd,
        }

    # ------------------------------------------------------- portfolio v2
    def _market_change_24h(self, market: str):
        """24h change (percent) of a market from the LIVE catalog.

        Snapshot-free by construction: the whole-exchange catalog is one
        cached public call, so a held wallet coin that sits outside the
        cycle's top-N universe still gets an honest 24h number instead of
        an empty cell. When the exact market has no row, the SAME coin's
        other quote (BTCIRT for BTCUSDT) is a valid measure of the coin's
        24h move.
        """
        try:
            uni = self.market_universe()
        except Exception:
            return None
        # A stale/alias row name (XEMPIREIRT for the XIRT market) must still
        # read the coin's real 24h move — otherwise the column is blank or
        # shows another market's change.
        market = canonical_symbol(market)
        for sym in [market] + MarketUniverse.alternates(market):
            try:
                row = uni.row(sym)
            except Exception:
                row = None
            if not row:
                continue
            try:
                chg = float(row.get("change_24h"))
            except (TypeError, ValueError):
                continue
            if chg == chg:                      # skip NaN
                return chg
        return None

    def _portfolio_v2(self, prices: dict, ps: dict, hp: dict,
                      irt_rate: float | None = None) -> dict:
        """پورتفوی نوبیتکسی — the whole account, every number in TOMAN.

        One row per held coin: مقدار، میانگین خرید، قیمت لحظه‌ای، ارزش،
        سود/زیان خرید، سود/زیان ۲۴ساعت و سهم از پرتفوی — plus the
        account-level totals the dashboard's hero cards print.

        Toman is the REPORTING unit (it is the user's own money), so a
        dollar-quoted (*USDT) row is converted with the live
        Toman-per-USDT rate while keeping its own market numbers on the
        row (`value_quote` / `invested_quote`).

        NO SNAPSHOT AND NO CYCLE IS NEEDED: prices come from the same live
        catalog fallback the holdings box uses and the 24h changes from
        that catalog too, so a coin the snapshot dropped is still valued
        and still gets its 24h move.

        Every aggregate is the plain SUM of the very row numbers the UI
        prints, so «سود/زیان دارایی‌ها» on the page is exactly Σ of its
        column — nothing is re-rounded behind the user's back.
        """
        if irt_rate is None:
            irt_rate = float(self._usdt_rates(prices).get("IRT") or 0)
        toman_per_usdt = (1.0 / irt_rate) if irt_rate > 0 else 0.0
        acc = (ps or {}).get("account") or {}
        total_irt = float(acc.get("equity_irt") or 0)
        cash_irt = float(acc.get("irt_total") or 0)
        cash_avail_irt = float(acc.get("irt_available") or 0)
        rows: list = []
        for p in self.storage.positions():
            mk = p["market"]
            if d(p["amount"]) <= 0 or mk in ("USDT", "IRT", "USDTIRT"):
                continue
            if mk not in prices:
                px = self._price_fallback(mk)
                if px:
                    prices[mk] = px
            r = self._position_row(p, prices, irt_rate)
            quote = r.get("quote") or (quote_of_market(mk) or "IRT")
            mul = 1.0 if quote == "IRT" else toman_per_usdt
            # display numbers (2dp dollars / whole Toman) for the rendered
            # row, but CONVERT FROM THE RAW valuation: rounding $3.85 to
            # $3.85 then multiplying by ~۲۶۹٬۵۰۰ can shift the Toman value by
            # thousands. The raw numbers are the exchange rialBalance and
            # the weighted cost, so use them for money/percent/24h.
            value_q = r.get("value_disp")
            invested_q = r.get("invested_disp")
            value_raw = r.get("value_irt")
            invested_raw = r.get("invested_irt")
            value_irt = (float(d(str(value_raw)) * d(str(mul)))
                         if (value_raw is not None and mul > 0) else None)
            invested_irt = (float(d(str(invested_raw)) * d(str(mul)))
                            if (invested_raw is not None and mul > 0) else None)
            pnl_irt = (value_irt - invested_irt
                       if (value_irt is not None and invested_irt is not None)
                       else None)
            pnl_pct = (round(pnl_irt / invested_irt * 100, 2)
                       if (pnl_irt is not None and invested_irt) else None)
            # 24h move of THIS coin applied to MY amount (the Nobitex
            # panel's «سود/زیان ۲۴ساعته»): value 24h ago = value/(1+chg).
            chg = self._market_change_24h(mk)
            ch24_irt = None
            if (chg is not None and value_irt is not None
                    and (1.0 + chg / 100.0) > 0):
                ch24_irt = value_irt - (value_irt / (1.0 + chg / 100.0))
            value_usdt = None
            if quote == "USDT":
                value_usdt = value_raw
            elif value_irt is not None and toman_per_usdt > 0:
                value_usdt = value_irt / toman_per_usdt
            rows.append({
                "symbol": mk, "coin": r.get("coin"), "quote": quote,
                "amount": r.get("amount"),
                "avg_entry": r.get("avg_entry"),
                "last_buy_price": r.get("last_buy_price"),
                "price": r.get("price"), "price_usdt": r.get("price_usdt"),
                "value_quote": value_q, "invested_quote": invested_q,
                "value_irt": (round(value_irt, 2)
                              if value_irt is not None else None),
                "value_usdt": (round(value_usdt, 2)
                               if value_usdt is not None else None),
                "invested_irt": (round(invested_irt, 2)
                                 if invested_irt is not None else None),
                "pnl_irt": (round(pnl_irt, 2)
                            if pnl_irt is not None else None),
                "pnl_pct": pnl_pct,
                "ch24_pct": (round(chg, 2) if chg is not None else None),
                "ch24_irt": (round(ch24_irt, 2)
                             if ch24_irt is not None else None),
                "last_buy_at": r.get("last_buy_at"),
                "holding_fa": r.get("holding_fa"),
                "price_source": r.get("price_source"),
                "ch24_quote": r.get("ch24_money"),
                "stop": r.get("stop"), "take_profit": r.get("take_profit"),
            })
        coins_irt = sum(r["value_irt"] for r in rows
                        if r["value_irt"] is not None)
        invested_sum = sum(r["invested_irt"] for r in rows
                           if r["invested_irt"] is not None)
        open_pnl = sum(r["pnl_irt"] for r in rows
                       if r["pnl_irt"] is not None)
        ch24_rows = [r for r in rows if r["ch24_irt"] is not None]
        ch24_sum = sum(r["ch24_irt"] for r in ch24_rows)
        ch24_base = sum(r["value_irt"] - r["ch24_irt"] for r in ch24_rows)
        open_pct = (round(open_pnl / invested_sum * 100, 2)
                    if invested_sum > 0 else None)
        ch24_pct = round(ch24_sum / ch24_base * 100, 2) if ch24_base > 0 else None
        base_irt = total_irt if total_irt > 0 else coins_irt + cash_irt
        for r in rows:
            r["weight"] = (round(r["value_irt"] / base_irt, 6)
                           if (r["value_irt"] is not None and base_irt > 0)
                           else None)
        rows.sort(key=lambda r: (-(r["value_irt"] or 0.0), r["coin"] or ""))
        return {
            "unit": "IRT",
            "generated_at": utc_now_iso(),
            "toman_per_usdt": round(toman_per_usdt, 2),
            "toman_rate_ok": bool(toman_per_usdt > 0),
            "total_irt": round(total_irt, 2),
            "cash_irt": round(cash_irt, 2),
            "cash_available_irt": round(cash_avail_irt, 2),
            # Tether cash is real money too — reported separately (never
            # added into the Toman number) so the UI can show both.
            "cash_usdt": round(float(acc.get("available_usdt") or 0), 2),
            "cash_usdt_irt": round(float(acc.get("available_usdt") or 0)
                                   * toman_per_usdt, 2),
            "coins_value_irt": round(coins_irt, 2),
            "coins_value_pct": (round(coins_irt / base_irt * 100, 2)
                                if base_irt > 0 else None),
            "cash_weight": (round(cash_irt / base_irt, 6)
                            if base_irt > 0 else None),
            "invested_irt": round(invested_sum, 2),
            "open_pnl_irt": round(open_pnl, 2),
            "open_pnl_pct": open_pct,
            # account-level profit (growth of the deposited capital) — the
            # SAME number the «سود کل» box prints, taken from holdings_pnl
            "total_pnl_irt": hp.get("profit_capital_irt"),
            "total_pnl_pct": hp.get("growth_pct"),
            "deposited_irt": hp.get("deposited_irt"),
            "pnl_24h_irt": round(ch24_sum, 2),
            "pnl_24h_pct": ch24_pct,
            "rows_24h": len(ch24_rows),
            "row_count": len(rows),
            "rows": rows,
        }

    def _position_row(self, p: dict, prices: dict, usdtirt: float) -> dict:
        """One holdings-box row: amounts, values and profit since MY buys.

        سود از خرید is computed against the position's average entry —
        NOT the market's 24h change. The holding timer restarts from the
        most recent BUY fill (last_buy_at), so every top-up restarts it.
        """
        market = p["market"]
        wallet_vals = getattr(self, "_wallet_valuations", None) or {}
        # A wallet-only coin can itself end in IRT/USDT (XIRT is the whole
        # ticker, not X + IRT). When the exchange wallet reports that exact
        # asset name, keep it as the base; otherwise strip the real market
        # quote (XRPIRT -> XRP, ATOMUSDT -> ATOM). The old blind rstrip
        # turned XIRT into «X», so its wallet valuation could never match
        # and the row showed price/value «—».
        if market.upper() in wallet_vals:
            coin = market.upper()
        else:
            coin = re.sub(r"(USDT|IRT)$", "", market)
        amount = d(p["amount"])
        price = d(str(prices.get(market) or 0))
        price_source = "market"
        # EXACT exchange valuation first (Nobitex wallet rialBalance). The
        # ticker is only a fallback: for a coin with no live market (XIRT)
        # it used to value the holding at ~۱٫۳۸ T while the wallet showed
        # ~۵٫۴ T, so every Toman profit/24h number built on it was wrong.
        # use the wallet's own ticker when it differs from the market base
        # (wallet «x» = XEMPIREIRT): the value/price is keyed by the wallet
        # asset there, not by the market name.
        wallet_asset = (getattr(self, "_wallet_market_assets", None) or {}).get(
            market.upper())
        wv = wallet_vals.get((wallet_asset or coin).upper())
        if wv:
            unit_irt = d(str(wv.get("price_irt") or 0))
            if unit_irt > 0:
                if market.endswith("USDT"):
                    # row values for USDT markets are in dollars; convert
                    # the wallet's Toman-per-unit with the live USDT rate
                    # (usdtirt here = USDT per Toman).
                    if usdtirt and usdtirt > 0:
                        price = unit_irt * d(str(usdtirt))
                        price_source = "wallet"
                else:
                    price = unit_irt
                    price_source = "wallet"
        entry = d(p["avg_entry"])
        # SS100d: after a NEW buy the UI «قیمت خرید» shows the LAST buy
        # price (the number the user just paid). P&L — both the money and
        # the percent — always reads the WEIGHTED average cost (avg_entry),
        # which is what the coins actually cost; using the last buy price
        # for the percent made it disagree with the money (a loss showed
        # «+۰٫۳۷٪» and a profit showed «−۴٫۵۵٪»).
        last_buy_price = d(str(p.get("last_buy_price") or 0))
        display_entry = last_buy_price if last_buy_price > 0 else entry
        # مبلغ خرید = پولی که واقعاً برای همین تعداد سکه داده‌ایم:
        # تعداد × میانگین خرید — همان عددی که دفتر با هر فیل خرید
        # می‌سازد و در فروش هم مبنای «سود محقق‌شده» است. «قیمت خرید» ردیف
        # (طبق SS100d) آخرین خرید را نشان می‌دهد، ولی «مبلغ خرید» و
        # «سود از خرید» (هم مبلغ، هم درصد) هرگز از میانگین خرید جدا
        # نمی‌شوند؛ وگرنه جمع «ارزش منهای مبلغ خرید» روی ردیف‌ها هزینهٔ
        # واقعی نمی‌شود.
        # (ریشهٔ باگ ‎-۲۳۳٫۵۹$: میانگین خریدِ SUSDT در دفتر ۱۶٫۵$ مانده
        # بود در حالی که همهٔ خریدهایش ‎~۰٫۰۴$ بود — عدد خراب از خودِ
        # دفتر تعمیر می‌شود (`_repair_position_entry`)، نه اینکه نمایش
        # عوض شود و خرابی زیرش بماند.)
        entry_basis = entry if entry > 0 else display_entry
        buys_irt = amount * entry_basis if entry_basis > 0 else ZERO
        value_irt = amount * price
        # 24h move of THIS coin applied to MY amount — the SAME definition
        # the Nobitex wallet uses, and independent of when I bought the
        # coin. The UI used to show «profit since purchase» for positions
        # younger than 24h, so the ۲۴ساعت column silently switched meaning
        # (and jumped at the 24h mark). That fallback is gone.
        chg24 = self._market_change_24h(market)
        ch24_money = None
        if chg24 is not None and value_irt > 0:
            chg_d = d(str(chg24))
            if (1 + chg_d / 100) > 0:
                ch24_money = value_irt - value_irt / (1 + chg_d / 100)
        last_buy = p.get("last_buy_at")
        age_h = None
        if last_buy:
            try:
                lb = datetime.fromisoformat(last_buy.replace("Z", "+00:00"))
                age_h = round((datetime.now(timezone.utc) - lb)
                              .total_seconds() / 3600, 1)
            except Exception:
                age_h = None
        plan = (self.storage.kv_get("exit_plans", {}) or {}).get(market, {})
        stop_v = d(str(plan.get("stop", 0) or 0))
        tp_v = d(str(plan.get("take_profit_manual", 0) or 0))
        # HADD (strike base): the reference the TAKE-PROFIT reads
        # against. Stage 0 = the LAST buy price; every reached
        # take-profit moves the hadd to that price (strike) and the next
        # TP re-derives from it. The STOP-LOSS percent reads against the
        # SAME حد (user request: «حد ضرر از قیمت حد محاسبه شود») — so a
        # stop sitting under the strike always reads as a small negative
        # percent instead of a meaningless one against an old buy price.
        row_stage, hadd = self._hadd_stage_of(plan, last_buy_price, entry)
        stop_anchor = entry if entry > 0 else (last_buy_price
                                               if last_buy_price > 0
                                               else hadd)
        # buy-anchored percent (what the STOP used to display): kept in
        # the payload for reference/back-compat — «مبلغ» still uses it
        buy_stop_pct = None
        if stop_anchor > 0 and stop_v > 0:
            buy_stop_pct = float(((stop_v - stop_anchor) / stop_anchor
                                  * 100).quantize(Decimal("0.01")))
        # the percent the row SHOWS (and the editor opens with): vs the
        # حد (falling back to the buy anchor when no hadd exists yet)
        hadd_stop_pct = None
        if stop_v > 0:
            pct_base = hadd if hadd > 0 else stop_anchor
            if pct_base > 0:
                hadd_stop_pct = float(((stop_v - pct_base) / pct_base * 100)
                                      .quantize(Decimal("0.01")))
        hadd_tp_pct = None
        if hadd > 0 and tp_v > 0:
            hadd_tp_pct = float(((tp_v - hadd) / hadd * 100)
                                .quantize(Decimal("0.01")))
        # «قیمت حد» نمایشی ردیف نمای کلی = قیمت لحظه‌ای همین سکه (درخواست
        # کاربر: «قیمت حد باید دقیقاً قیمت فعلی توکن باشد»). مرجع پله/
        # نردبان همان `hadd` بالا می‌ماند و دست نمی‌خورد؛ فقط عددی که ردیف
        # چاپ می‌کند — و پایهٔ درصدهایی که ویرایشگر حد با آن باز می‌شود —
        # قیمت لحظه‌ای است، پس درصدها فاصلهٔ سطح تا «همین‌جا» را می‌گویند.
        hadd_display = price if price > 0 else (hadd if hadd > 0 else None)
        live_stop_pct = None
        if stop_v > 0 and price > 0:
            live_stop_pct = float(((stop_v - price) / price * 100)
                                  .quantize(Decimal("0.01")))
        live_tp_pct = None
        if tp_v > 0 and price > 0:
            live_tp_pct = float(((tp_v - price) / price * 100)
                                .quantize(Decimal("0.01")))
        # مبلغ حد ضرر (در واحد پول همان بازار): مثبت = سودی که با فعال شدن
        # حد ضرر قفل می‌شود، منفی = پولی که هنوز در ریسک است. با هر پله که
        # حد ضرر بالا می‌رود این عدد و درصدش همان لحظه عوض می‌شوند.
        stop_money = None
        if stop_v > 0 and stop_anchor > 0 and amount > 0:
            stop_money = float(((stop_v - stop_anchor) * amount)
                               .quantize(Decimal("0.01")))
        # بودجهٔ سرمایهٔ پلهٔ بعد = سودِ همان پله (نه درصدی از ارزش موقعیت):
        # (حد سودِ مسلح − میانگین خرید) × تعداد سکه. همان سودی که با رسیدن
        # قیمت به آن پله تازه به دست می‌آید، سرمایهٔ پلهٔ بعد می‌شود؛ اگر آن
        # قیمت بالای میانگین خرید نباشد پله سرمایه‌ای نمی‌گیرد.
        ladder_on = bool(getattr(self.cfg.risk, "ladder_enabled", True))
        ladder_next_add = None
        if ladder_on and amount > 0 and tp_v > 0 and stop_anchor > 0:
            rung_profit = (tp_v - stop_anchor) * amount
            if rung_profit > 0:
                ladder_next_add = float(rung_profit.quantize(
                    Decimal("0.01")))

        def _fa_h(h):
            if h is None:
                return ""
            if h < 1:
                return "(" + str(max(1, int(h * 60))) + " دقیقه)"
            if h < 48:
                return "(" + str(int(h)) + " ساعت)"
            return "(" + str(round(h / 24, 1)) + " روز)"

        # اعدادِ نمایشی همین ردیف: همان‌هایی که ردیف چاپ می‌کند و همان‌هایی
        # که سرِ بخش جمع می‌زند (دلار ۲ رقم اعشار، تومان بدون اعشار). اگر
        # جمعِ خام را جدا گرد کنیم، سرِ بخش با جمعِ ردیف‌ها یک‌سنت/یک‌تومان
        # اختلاف پیدا می‌کند (زنده: ردیف‌ها $1.26 ولی سرِ بخش $1.25 بود).
        row_quote = quote_of_market(market) or "IRT"
        row_step = self._display_step(row_quote)
        value_disp = (d(str(value_irt)).quantize(row_step,
                                                 rounding=ROUND_HALF_UP)
                      if price > 0 else None)
        invested_disp = (d(str(buys_irt)).quantize(row_step,
                                                   rounding=ROUND_HALF_UP)
                         if buys_irt > 0 else None)
        pnl_disp = (value_disp - invested_disp
                    if (value_disp is not None and
                        invested_disp is not None) else None)
        ch24_disp = (d(str(ch24_money)).quantize(row_step,
                                                rounding=ROUND_HALF_UP)
                     if (ch24_money is not None and row_step) else None)
        # «سود از خرید» در درصد = (ارزش − مبلغ خرید) ÷ مبلغ خرید روی
        # اعدادِ دقیق (نه دو عدد گِردشدهٔ نمایش): درصد باید همان درصدی
        # باشد که کیف پول نوبیتکس از ارزش واقعی حساب می‌دهد. علامتش هم
        # همیشه با علامت همان پول یکی است (قبلاً درصد نسبت به «آخرین
        # قیمت خرید» بود و زیان را «+» نشان می‌داد).
        profit_pct = None
        if entry_basis > 0 and buys_irt > 0:
            profit_pct = float(((value_irt - buys_irt) / buys_irt * 100)
                               .quantize(Decimal("0.01")))
            if profit_pct == 0:
                profit_pct = 0.0      # no «-0.00» / «+0.00» from a zero

        return {
            "symbol": market, "coin": coin,
            "amount": float(amount),
            "price": float(price) if price else None,
            # قیمت به دلار: بازار تتری همان عدد است، بازار تومانی ×
            # نرخ دلار-تومان (usdtirt = ارزش یک تومان به دلار). تقسیم
            # قبلی قیمت را ~۱۰^۵ برابر بزرگ نشان می‌داد.
            "price_usdt": (float(price) if market.endswith("USDT")
                           else float(price * d(str(usdtirt or 0))))
            if price and usdtirt else None,
            "value_irt": float(value_irt) if price else None,
            "avg_entry": float(entry) if entry > 0 else None,
            "last_buy_price": float(last_buy_price)
            if last_buy_price > 0 else None,
            "invested_irt": float(buys_irt) if buys_irt > 0 else None,
            # واحد پول همین ردیف: بازار تتری $، بازار تومانی تومان — هیچ
            # عددی بین دو بخش قرض گرفته نمی‌شود (by_market هم روی همین است)
            "quote": row_quote,
            # همان اعداد با دقتِ نمایش (چیزی که ردیف چاپ می‌کند و سرِ بخش
            # جمع می‌زند) — «value_irt»/«invested_irt» خام و دقیق می‌مانند
            # برای محاسبات داخلی.
            "value_disp": (float(value_disp) if value_disp is not None
                           else None),
            "invested_disp": (float(invested_disp)
                              if invested_disp is not None else None),
            # سود/زیان پولی همین سکه به پول همان بازار = ارزش − مبلغ خرید
            # (هر دو بر یک مبنا: همان entry_basis بالا و با دقتِ همان نمایش)
            "pnl_money": (float(pnl_disp) if pnl_disp is not None else None),
            "profit_pct": profit_pct,
            # اعدادِ خام (بدون گِردکردن نمایش) برای تولتیپ/بررسی دقیق:
            # همان چیزی که از rialBalance صرافی و میانگین خرید آمده است.
            "value_exact": (float(value_irt) if price > 0 else None),
            "invested_exact": (float(buys_irt) if buys_irt > 0 else None),
            "pnl_exact": (float(value_irt - buys_irt) if price > 0 else None),
            "price_source": price_source,
            # سود/زیان ۲۴ساعت همین سکه روی مقدار فعلی — هم درصد و هم مبلغ
            # (مبلغ در واحد پول همین بازار: تومان یا دلار).
            "ch24_pct": (round(chg24, 2) if chg24 is not None else None),
            "ch24_money": (float(ch24_disp) if ch24_disp is not None
                           else None),
            "last_buy_at": last_buy,
            "holding_fa": _fa_h(age_h),
            "stop": float(stop_v) if stop_v > 0 else None,
            "stop_pct": plan.get("stop_pct"),
            "take_profit": float(tp_v) if tp_v > 0 else None,
            "take_profit_pct": plan.get("take_profit_manual_pct"),
            # profit-lock ratchet state for the «حد» column: on/off, the
            # stage counter shown in brackets (0/1/2/3…) and the locked
            # floor (the stop level that can never fall again)
            "trail": bool(plan.get("trail", False)),
            "stage": row_stage,
            "locked_stop": plan.get("locked_stop"),
            # «قیمت حد (n)» — the strike base the TP percent reads against
            "hadd": float(hadd) if hadd > 0 else None,
            # the number the overview row PRINTS as «قیمت حد»: the coin's
            # live price (the engine strike above is kept for the ladder)
            "hadd_display": (float(hadd_display)
                             if hadd_display is not None else None),
            # درصد حد ضرر/حد سود نسبت به «قیمت حد» = قیمت لحظه‌ای
            "live_stop_pct": live_stop_pct,
            "live_tp_pct": live_tp_pct,
            # درصد حد ضرر نسبت به «قیمت حد» (همان حدی که کاربر می‌بیند)
            # + مبلغ حد ضرر نسبت به قیمت خرید (قفل سود/ریسک)
            "hadd_stop_pct": hadd_stop_pct,
            "stop_pct_buy": buy_stop_pct,
            "stop_base": float(stop_anchor) if stop_anchor > 0 else None,
            "hadd_stop_base": float(hadd) if hadd > 0 else None,
            "stop_money": stop_money,
            "hadd_tp_pct": hadd_tp_pct,
            # نردبان پله‌ها: پله‌ای که حد ضرر روی آن نشسته + بودجهٔ
            # سرمایهٔ پلهٔ بعد
            "ladder": ladder_on,
            "ladder_stop_stage": plan.get("ladder_stop_stage"),
            "ladder_anchor": plan.get("ladder_anchor"),
            "ladder_next_add": ladder_next_add,
            "ladder_add_stage": plan.get("ladder_add_stage"),
        }

    def _profit_topup_decisions(self, prices: dict,
                                portfolio_snapshot: dict,
                                cycle_id: str) -> list:
        """SS100e: for every held coin whose since-buy profit reached
        risk.topup_profit_pct (default 20%), mint a BUY decision so the
        bot adds free cash to the winning position. The decision goes
        through the FULL risk pipeline — sizing, cash cap, min-notional
        and cooldowns all still apply; only MAX_OPEN_POSITIONS is waived
        (the coin is already a position, same as manual top-ups)."""
        out = []
        threshold = float(getattr(self.cfg.risk, "topup_profit_pct", 0))
        if threshold <= 0:
            return out
        snap = self.latest_snapshot or {}
        market_by_symbol = {
            m["symbol"]: m for m in snap.get("markets", [])
            if (m.get("data_quality") or {}).get("score", 0)
            >= self.cfg.quality.min_trade_quality}
        usdtirt = d(str(self._usdt_rates(prices).get("IRT") or 0))
        # one top-up per coin per day: remember what we already fired
        fired = self.storage.kv_get("profit_topups", {}) or {}
        today = utc_now_iso()[:10]
        for p in self.storage.positions():
            market = p["market"]
            if d(p["amount"]) <= 0 or market not in prices:
                continue
            if not self._quote_market_enabled(market):
                continue  # بخش خاموش «بازار معاملات» — هیچ خریدی در آن
            # base = the LAST buy price (the «قیمت خرید» the UI shows),
            # NOT avg_entry: after dips, avg_entry sits above the last buy
            # and the coin is LOSING money from the user's own cost —
            # pyramiding into that is doubling down on a loser, not
            # pressing a winner.
            entry = d(str(p.get("last_buy_price") or 0)) \
                or d(p["avg_entry"])
            cur = d(str(prices.get(market) or 0))
            if entry <= 0 or cur <= 0:
                continue
            profit_pct = float((cur - entry) / entry * 100)
            if profit_pct < threshold:
                continue
            if market not in market_by_symbol:
                continue  # only top up markets we can still analyze
            if (fired.get(market) or "") >= today:
                continue  # already topped this coin up today
            m = market_by_symbol[market]
            dec = self._synthesize_buy_plan(
                {"symbol": market}, m, portfolio_snapshot, prices)
            dec["decision_id"] = "dec_%s_%s" % (
                utc_now_iso().replace("-", "").replace(":", "")[:15],
                new_id("x")[2:10])
            dec["created_at"] = utc_now_iso()
            dec["expires_at"] = (datetime.now(timezone.utc) + timedelta(
                seconds=self.cfg.risk.decision_ttl_s)).isoformat().replace(
                "+00:00", "Z")
            dec["reason_codes"] = [
                "PROFIT_TOPUP", "PROFIT_%.1f%%" % profit_pct]
            vres = ValidationResult(ok=True)
            validate_ai_decision(
                dec, set(market_by_symbol.keys()), vres)
            if not vres.ok:
                continue
            self.storage.save_ai_decision(dec, True)
            self.latest_decisions.append(dec)
            rd = self.risk.evaluate(dec, m, portfolio_snapshot, prices,
                                    topup=(market, 0.0))
            self.storage.save_risk_decision(rd)
            self.latest_risk.append(rd)
            if rd["status"] != "APPROVED":
                continue
            ex = self.executor.execute(dec, rd, prices,
                                       "profit_topup")
            self.latest_executions.append(ex)
            if ex.get("status") in ("FILLED", "OPEN", "SUBMITTED",
                                    "PARTIALLY_FILLED", "READY"):
                fired[market] = today
                self.storage.kv_set("profit_topups", fired)
                self.events.publish("PROFIT_TOPUP_FIRED", "engine",
                                    cycle_id,
                                    {"symbol": market,
                                     "profit_pct": profit_pct,
                                     "status": ex.get("status")})
                out.append(dec)
        return out

    def _ladder_topup_decisions(self, prices: dict,
                                portfolio_snapshot: dict,
                                cycle_id: str) -> list:
        """نردبان — سرمایهٔ هر پله (risk.ladder_*).

        هر strike که رد می‌شود (`ladder_add_stage` توسط ratchet ثبت
        می‌شود) در همان چرخه سرمایهٔ افزوده روی همان سکه می‌رود. سرمایهٔ
        هر پله همان **سودِ آن پله** است: (قیمتی که پله به آن رسید −
        میانگین خرید) × تعداد سکه، از پول نقد آزاد — پله فقط از سود خودش
        سرمایه می‌گیرد، نه درصدی از ارزش موقعیت. مبلغ هم کف و هم سقف
        اندازهٔ سفارش است، یعنی خرید دقیقاً همان بودجهٔ پله است (حداقل
        سفارش صرافی می‌تواند کمی بالا ببرد و سقف سهم/نقد پایین). تصمیم
        از خط لولهٔ کامل SS100 می‌گذرد: validation → risk →
        execution. هر پله حداکثر یک خرید دارد؛ اگر پول نقد یا سقف اجازه
        نداد همان پله می‌سوزد و در چرخه‌های بعد تکرار نمی‌شود (دلیلش در
        رویداد LADDER_TOPUP_BLOCKED ثبت می‌شود).
        """
        out = []
        if not (getattr(self.cfg.risk, "ladder_enabled", True) and
                getattr(self.cfg.risk, "ladder_topup_enabled", True)):
            return out
        if self.safe_mode:
            return out
        plans = self.storage.kv_get("exit_plans", {})
        if not isinstance(plans, dict):
            return out
        snap = self.latest_snapshot or {}
        market_by_symbol = {
            m["symbol"]: m for m in snap.get("markets", [])
            if (m.get("data_quality") or {}).get("score", 0)
            >= self.cfg.quality.min_trade_quality}
        rates = self._usdt_rates(prices)
        usdt_per_irt = d(str(rates.get("IRT") or 0))
        irt_per_usdt = (d("1") / usdt_per_irt) if usdt_per_irt > 0 else ZERO
        dirty = False
        for p in self.storage.positions():
            market = p["market"]
            plan = plans.get(market)
            if not isinstance(plan, dict):
                continue
            if not self._quote_market_enabled(market):
                continue  # بخش خاموش «بازار معاملات» — پله سرمایه نمی‌گیرد
            amount = d(str(p["amount"] or 0))
            if amount <= 0 or market not in prices:
                continue
            try:
                due = int(plan.get("ladder_add_stage", 0) or 0)
                done = int(plan.get("ladder_done_stage", 0) or 0)
            except Exception:
                continue
            if due <= 0 or due <= done:
                continue
            price = d(str(prices.get(market) or 0))
            if price <= 0:
                continue
            # سرمایهٔ همین پله = سودِ همین پله: (قیمتی که پله به آن رسید −
            # میانگین خرید) × تعداد سکه — نه درصدی از ارزش موقعیت
            rung = d(str(plan.get("base_price") or 0))
            if rung <= 0:
                rungs = plan.get("ladder_rungs")
                if isinstance(rungs, list) and rungs:
                    rung = d(str(rungs[-1] or 0))
            basis = d(str(p.get("avg_entry") or 0))
            if basis <= 0:
                basis = d(str(p.get("last_buy_price") or 0))
            rung_profit = ((rung - basis) * amount
                           if (rung > 0 and basis > 0) else ZERO)
            # همین پله مصرف می‌شود؛ موفق یا ناموفق، تکرار نمی‌شود
            plan["ladder_done_stage"] = due
            plan["ladder_add_stage"] = 0
            plans[market] = plan
            dirty = True
            if rung_profit <= 0:
                # پله‌ای که بالای میانگین خرید نیست سرمایه نمی‌گیرد: پول نقد
                # آزاد دست‌نخورده می‌ماند و همان پله (مثل وقتی پول کم است)
                # می‌سوزد — دلیلش در رویداد ثبت می‌شود.
                self.events.publish("LADDER_TOPUP_BLOCKED", "engine",
                                    cycle_id,
                                    {"symbol": market, "stage": due,
                                     "rung": float(rung),
                                     "avg_entry": float(basis),
                                     "reason": "NO_PROFIT_AT_RUNG"})
                continue
            add_quote = rung_profit.quantize(Decimal("0.01"))
            add_coins = add_quote / price
            m = market_by_symbol.get(market)
            if not m:
                self.events.publish("LADDER_TOPUP_BLOCKED", "engine",
                                    cycle_id,
                                    {"symbol": market, "stage": due,
                                     "reason": "NO_MARKET_DATA"})
                continue
            dec = self._synthesize_buy_plan(
                {"symbol": market}, m, portfolio_snapshot, prices)
            dec["decision_id"] = "dec_%s_%s" % (
                utc_now_iso().replace("-", "").replace(":", "")[:15],
                new_id("x")[2:10])
            dec["created_at"] = utc_now_iso()
            dec["expires_at"] = (datetime.now(timezone.utc) + timedelta(
                seconds=self.cfg.risk.decision_ttl_s)).isoformat().replace(
                "+00:00", "Z")
            dec["cycle_id"] = cycle_id
            dec["reason_codes"] = ["LADDER_TOPUP",
                                   "LADDER_STAGE_%d" % due,
                                   "LADDER_PROFIT_BUDGET"]
            dec["position_size"] = {
                "amount": float(add_coins),
                "quote_value": float(add_quote),
                "requested_quote_value": float(add_quote),
            }
            vres = ValidationResult(ok=True)
            validate_ai_decision(
                dec, set(market_by_symbol.keys()), vres)
            if not vres.ok:
                self.events.publish("LADDER_TOPUP_BLOCKED", "engine",
                                    cycle_id,
                                    {"symbol": market, "stage": due,
                                     "reason": "VALIDATION_" +
                                     str(vres.first_code())})
                continue
            self.storage.save_ai_decision(dec, True)
            self.latest_decisions.append(dec)
            rd = self.risk.evaluate(dec, m, portfolio_snapshot, prices,
                                    topup=(market, float(add_quote)),
                                    size_cap=add_coins)
            self.storage.save_risk_decision(rd)
            self.latest_risk.append(rd)
            if rd["status"] != "APPROVED":
                self.events.publish("LADDER_TOPUP_BLOCKED", "engine",
                                    cycle_id,
                                    {"symbol": market, "stage": due,
                                     "reason": "RiskEngineGate: " +
                                     "، ".join(rd.get(
                                         "rejection_reasons") or [])})
                continue
            # این خرید ادامهٔ همان کمپین است: _auto_stop_for_buy نباید
            # نردبان (پله/قیمت حد/حد ضرر) را از صفر شروع کند
            plan["ladder_buy_pending"] = True
            self.storage.kv_set("exit_plans", plans)
            ex = self.executor.execute(dec, rd, prices, "ladder_topup")
            self.latest_executions.append(ex)
            st = (ex.get("status") or "").upper()
            if st not in ("FILLED", "OPEN", "SUBMITTED", "PARTIALLY_FILLED",
                          "READY"):
                plan.pop("ladder_buy_pending", None)
                self.storage.kv_set("exit_plans", plans)
                self.events.publish("LADDER_TOPUP_BLOCKED", "engine",
                                    cycle_id,
                                    {"symbol": market, "stage": due,
                                     "reason": "EXECUTION_" + (st or "?")})
                continue
            out.append(dec)
            self.events.publish("LADDER_TOPUP_FIRED", "engine", cycle_id,
                                {"symbol": market, "stage": due,
                                 "status": st,
                                 "rung": float(rung),
                                 "profit_budget": float(add_quote),
                                 "amount_quote": float(add_quote),
                                 "amount_irt": float(
                                     add_quote if not market.endswith("USDT")
                                     else add_quote * irt_per_usdt)})
        if dirty:
            self.storage.kv_set("exit_plans", plans)
        return out

    def _click_market_for(self, symbol: str) -> tuple[str | None, str | None]:
        """Market a manual «خرید»/«فروش» click must use.

        The Settings «بازار معاملات» selector (USDT/IRT/both) decides the
        QUOTE currency of dashboard trades: with it on «فقط تتری»,
        clicking «خرید» on a Toman row (BTCIRT) buys the same coin on its
        Tether market (BTCUSDT) — and vice versa — so the click is never
        answered with a bare refusal. Returns (market, error): error is a
        Persian explanation when the other side has no market in the
        current snapshot (which is also what keeps the selector
        meaningful).
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return None, "نماد لازم است"
        usdt_state, irt_state, _ = self._effective_quote_markets()
        if symbol.endswith("USDT"):
            if usdt_state == "on":
                return symbol, None
            other = ((symbol[:-4] + "IRT") if irt_state == "on" else None)
            off_fa, on_fa = "تومانی", "تتری (USDT)"
        elif symbol.endswith("IRT"):
            if irt_state == "on":
                return symbol, None
            other = ((symbol[:-3] + "USDT") if usdt_state == "on" else None)
            off_fa, on_fa = "تتری", "تومانی (IRT)"
        else:
            return symbol, None
        if other:
            snap = self.latest_snapshot or {}
            if any(m.get("symbol") == other
                   for m in snap.get("markets", [])):
                return other, None
            return None, ("بازار %s خاموش است — «بازار معاملات» در تنظیمات "
                          "روی %s است و بازار %s (%s) هم در این چرخه موجود "
                          "نیست؛ یک چرخه اجرا کنید یا تنظیمات را عوض کنید"
                          % (off_fa, on_fa, other,
                             "تتری" if other.endswith("USDT") else "تومانی"))
        return None, ("بازار %s خاموش است — «بازار معاملات» در تنظیمات "
                      "روی %s است" % (off_fa, on_fa))

    def manual_buy(self, symbol: str, amount_irt: float | None = None) -> dict:
        """Dashboard «خرید» button in the holdings box (SS100c).

        Buys `symbol` with the given Toman amount (or a suggestion from
        free cash when None). A dedicated top-up decision is minted and
        pushed through the FULL SS100 pipeline: validation -> risk ->
        execution. The risk engine's normal caps still apply, but:
        - MAX_OPEN_POSITIONS_REACHED does not block topping up a held coin;
        - when the requested amount is below the exchange minimum order,
          the size is raised to one minimum order (small-account bootstrap,
          same as the auto path).
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return {"ok": False, "error": "نماد لازم است"}
        if self.safe_mode:
            return {"ok": False, "error": "حالت امن فعال است — خرید مسدود"}
        # quote-market selector (Settings): manual buys follow the same
        # USDT/IRT restriction as automatic ones. A click on a row whose
        # quote is switched OFF is re-targeted to the SAME COIN's market
        # in the enabled quote (the selector decides the currency of
        # dashboard trades): with it on «فقط تتری», clicking «خرید» on a
        # Toman row buys the Tether market — and the new holding then
        # shows under the USDT section of the holdings box.
        click_symbol = symbol
        market, merr = self._click_market_for(symbol)
        if not market:
            return {"ok": False, "error": merr}
        symbol = market
        note = ("خرید در بازار %s ثبت شد (انتخاب «بازار معاملات»: %s)"
                % (symbol, "تتری (USDT)" if symbol.endswith("USDT")
                   else "تومانی (IRT)")) if symbol != click_symbol else None
        snap = self.latest_snapshot or {}
        m = self._market_row(symbol)
        if not m or not m.get("last_price"):
            return {"ok": False,
                    "error": "قیمت زنده برای " + symbol +
                    " پیدا نشد — نماد را بررسی کنید یا چند لحظه بعد "
                    "دوباره تلاش کنید"}
        if amount_irt is not None and amount_irt <= 0:
            return {"ok": False, "error": "مبلغ باید مثبت باشد"}

        # run a fresh cycle when the snapshot is stale so limits/prices are
        # current (best-effort; ignore failures)
        try:
            created = m.get("created_at") or ""
            if created:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(created.replace("Z", "+00:00"))
                       ).total_seconds()
                if age > 180:
                    self.run_cycle()
                    snap = self.latest_snapshot or {}
                    m = next((x for x in snap.get("markets", [])
                              if x.get("symbol") == symbol), m)
        except Exception:
            pass

        prices = {x["symbol"]: x["last_price"] for x in snap.get("markets", [])
                  if x.get("last_price")}
        prices.setdefault(symbol, m.get("last_price"))
        cur = d(str(prices.get(symbol) or 0))
        if cur <= 0:
            return {"ok": False, "error": "قیمت موجود نیست"}
        # rates["IRT"] is the USDT value of ONE Toman (SS21 valuation), so
        # Toman-per-USDT is its inverse. The old code divided the Toman
        # amount by THIS number — i.e. multiplied it by ~۱۰۰٬۰۰۰ — so a
        # «۵ دلاری» click reached the risk engine as millions of dollars
        # and only the fraction/cash caps kept the order sane.
        usdt_per_irt = d(str(self._usdt_rates(prices).get("IRT") or 0))
        irt_per_usdt = (d("1") / usdt_per_irt) if usdt_per_irt > 0 else ZERO
        portfolio_snapshot = self.portfolio.portfolio_snapshot(
            prices, self._usdt_rates(prices))

        dec = self._synthesize_buy_plan(
            {"symbol": symbol}, m, portfolio_snapshot, prices)
        now_dt = datetime.now(timezone.utc)
        dec["decision_id"] = "dec_%s_%s" % (
            utc_now_iso().replace("-", "").replace(":", "")[:15],
            new_id("x")[2:10])
        dec["created_at"] = utc_now_iso()
        dec["expires_at"] = (now_dt + timedelta(
            seconds=self.cfg.risk.decision_ttl_s)).isoformat().replace(
            "+00:00", "Z")
        dec["reason_codes"] = ["MANUAL_BUY"] + list(
            dec.get("reason_codes", []))[:5]

        # requested amount -> the market's QUOTE unit (Toman for *IRT
        # markets, Tether for *USDT ones). The old code handed a Toman
        # number to a USDT-unit field, so the typed amount never really
        # applied (and the risk engine compared money against coins).
        want_irt = d(str(amount_irt or 0))
        want_quote = want_irt
        if symbol.endswith("USDT") and irt_per_usdt > 0:
            want_quote = want_irt / irt_per_usdt
        want_coins = (want_quote / cur) if cur > 0 else ZERO
        if amount_irt:
            dec["position_size"] = {
                "amount": float(want_coins),
                "quote_value": float(want_quote),
                "requested_quote_value": float(want_quote),
            }

        # the allowed set is the WHOLE tradable catalog (snapshot + registry
        # + live universe), not just this cycle's snapshot: a manual buy on
        # a market the cycle never collected used to die with UNKNOWN_MARKET
        if not dec.get("snapshot_id"):
            # this market may not be in any snapshot at all (that is the
            # whole point): tag the lineage instead of refusing the click
            dec["snapshot_id"] = (snap.get("snapshot_id") or
                                  "manual_live_universe")
        vres = ValidationResult(ok=True)
        validate_ai_decision(dec, self._allowed_symbols(
            {x.get("symbol"): x for x in snap.get("markets", [])}), vres)
        if not vres.ok:
            out = {"ok": False, "symbol": symbol, "market": symbol,
                   "error": "اعتبارسنجی تصمیم: " + "; ".join(
                       "%s:%s" % (i.field, i.code) for i in vres.issues[:3])}
            if note:
                out["note"] = note
            return out
        self.storage.save_ai_decision(dec, True)
        self.latest_decisions.append(dec)

        rd = self.risk.evaluate(dec, m, portfolio_snapshot, prices,
                                topup=(symbol, float(want_quote)),
                                # مبلغ تایپ‌شدهٔ کاربر سقف اندازه هم هست: بدون
                                # این سقف فقط کف می‌شد و موتور ریسک به اندازهٔ
                                # بودجهٔ خودش (بزرگ‌تر از درخواست) می‌خرید
                                size_cap=(want_coins if amount_irt and
                                          want_coins > 0 else None))
        self.storage.save_risk_decision(rd)
        self.latest_risk.append(rd)
        if rd["status"] != "APPROVED":
            out = {"ok": False, "symbol": symbol, "market": symbol,
                   "rejected": rd.get("rejection_reasons"),
                   "error": "رد توسط موتور ریسک: " + ", ".join(
                       rd.get("rejection_reasons") or [])}
            if note:
                out["note"] = note
            return out
        ex = self.executor.execute(dec, rd, prices, "manual_buy")
        self.latest_executions.append(ex)
        ok = ex.get("status") in ("FILLED", "OPEN", "SUBMITTED",
                                  "PARTIALLY_FILLED", "READY")
        if ok:
            self.events.publish("MANUAL_BUY_EXECUTED", "engine", "",
                                {"symbol": symbol,
                                 "click_symbol": click_symbol,
                                 "amount_irt": amount_irt,
                                 "status": ex.get("status")})
        out = {"ok": bool(ok), "symbol": symbol, "market": symbol,
               "execution": ex}
        if note:
            out["note"] = note
        if click_symbol != symbol:
            out["click_symbol"] = click_symbol
        return out

    def suggest_buy(self, symbol: str = "") -> dict:
        """Suggested amount for the holdings-box buy button: the engine's
        per-trade budget (risk fraction of equity), capped by free cash,
        floored at the exchange minimum order.

        The symbol is first resolved through the «بازار معاملات» selector
        (see _click_market_for) so the suggestion arrives in the currency
        the buy will actually use — with it on تتری, a click on a Toman
        row suggests a USDT amount from the USDT balance. Returns the
        resolved market + quote ("quote"/"amount_quote"/"cash_quote") and
        keeps the Toman fields for the API/UI that still convert.
        """
        market = (symbol or "").strip().upper()
        merr = None
        if market:
            resolved, merr = self._click_market_for(market)
            if resolved:
                market = resolved
        snap = self.latest_snapshot or {}
        prices = {x["symbol"]: x["last_price"] for x in snap.get("markets", [])
                  if x.get("last_price")}
        # a suggested amount must exist for EVERY market the user clicks —
        # held coins outside the snapshot universe included (the click may
        # target a market the cycle never collected)
        try:
            self._augment_prices_with_holdings(prices)
            m_row = self._market_row(market) if market else None
            if m_row and m_row.get("last_price"):
                prices.setdefault(market, m_row.get("last_price"))
        except Exception:
            m_row = None
        rates = self._usdt_rates(prices)
        # rates["IRT"] = USDT value of ONE Toman; invert it to get the
        # Toman-per-USDT conversion factor used below
        usdt_per_irt = d(str(rates.get("IRT") or 0))
        irt_per_usdt = (Decimal(1) / usdt_per_irt) \
            if usdt_per_irt > 0 else ZERO
        portfolio_snapshot = self.portfolio.portfolio_snapshot(prices, rates)
        equity = d(str((portfolio_snapshot.get("risk") or {}).get(
            "total_equity", 0)))   # USDT
        min_notional_usdt = d(str(getattr(self.cfg.exchange,
                                          "min_order_usdt", 0.44)))
        usdtirt_d = irt_per_usdt
        # the engine's own per-trade budget, in USDT
        budget_usdt = equity * d(str(self.cfg.risk.max_risk_per_trade))
        for f in {self.cfg.risk.max_position_fraction,
                  getattr(self.cfg.risk, "per_trade_fraction", None) or
                  self.cfg.risk.max_position_fraction}:
            budget_usdt = min(budget_usdt, equity * d(str(f))) \
                if equity > 0 else budget_usdt
        assets = {a.get("asset"): d(str(a.get("available", "0")))
                  for a in portfolio_snapshot.get("assets", [])}
        cash_usdt = assets.get("USDT", ZERO)
        # موجودی انحصاری هر بخش: خرید تومانی فقط از موجودی تومانی تغذیه
        # می‌شود. موجودی تتری فقط وقتی به آن اضافه می‌شود که دو بازار
        # صریحاً «تعامل بین بازارها» داشته باشند (Both + linked) — با
        # انتخاب تک‌بازاری یا «Both + بازارهای مستقل» این پول مال بخش
        # دیگری است و نباید به‌عنوان پول قابل خرج خرید تومانی دیده شود.
        cash_irt = assets.get("IRT", ZERO)
        if usdtirt_d and self._cross_market_allowed():
            cash_irt = cash_irt + cash_usdt * usdtirt_d
        min_irt = min_notional_usdt * usdtirt_d if usdtirt_d else ZERO
        # QUOTE OF THE RESOLVED MARKET: a Tether buy can only spend the
        # USDT balance (its minimum order is quoted in USDT), a Toman buy
        # spends Toman (plus the USDT balance valued in Toman, as before).
        is_usdt_market = market.endswith("USDT")
        if is_usdt_market:
            cash_quote = cash_usdt
            min_quote = min_notional_usdt
            budget_quote = budget_usdt
        else:
            cash_quote = cash_irt
            min_quote = min_irt
            budget_quote = budget_usdt * usdtirt_d if usdtirt_d else ZERO
        # budget -> Toman, then clamp: [min order, 95% of free cash]
        amount_hint = min(budget_quote, cash_quote * Decimal("0.95"))
        amount_hint = max(amount_hint, min_quote)
        amount_hint = min(amount_hint, cash_quote * Decimal("0.995"))
        if amount_hint <= 0 or cash_quote < min_quote:
            err = {"ok": False,
                   "error": "موجودی نقد برای حداقل سفارش کافی نیست",
                   "cash_irt": float(cash_irt),
                   "min_order_irt": float(min_irt),
                   "cash_quote": float(cash_quote),
                   "min_order_quote": float(min_quote),
                   "quote": "USDT" if is_usdt_market else "IRT"}
            if market:
                err["market"] = market
            if merr:
                err["note"] = merr
            return err
        if is_usdt_market:
            amount_hint = amount_hint.quantize(Decimal("0.01"))
            amount_irt = (amount_hint * usdtirt_d).quantize(Decimal("1")) \
                if usdtirt_d else ZERO
        else:
            amount_hint = amount_hint.quantize(Decimal("1"))
            amount_irt = amount_hint
        out = {"ok": True, "amount_irt": float(amount_irt),
               "cash_irt": float(cash_irt),
               "min_order_irt": float(min_irt),
               "amount_quote": float(amount_hint),
               "cash_quote": float(cash_quote),
               "min_order_quote": float(min_quote),
               "quote": "USDT" if is_usdt_market else "IRT"}
        if market:
            out["symbol"] = market
            out["market"] = market
        return out

    def set_stop_loss(self, symbol: str, stop: float,
                      side: str = "sell", pct: float | None = None,
                      kind: str = "stop",
                      base_mode: str = "entry") -> dict:
        """Set/update the exit triggers of a held symbol (dashboard edit).

        side="sell" only; two trigger kinds:
        - kind="stop"   حد ضرر فروش: price falling to *stop* closes the position.
        - kind="profit" حد سود: price RISING to *stop* closes the position
          — lets the candle run to its top and sells before the drop, so the
          absolute number must sit ABOVE the current price.
        (The old buy-side dip-stop was removed: buying is the engine's job.)

        Percent mode base (base_mode):
        - "hadd" (what the dashboard editor sends for the STOP): percent
          is relative to the plan's HADD — «قیمت حد», the strike base
          shown as «قیمت حد (n)» in the holdings row. Stage 0: the hadd
          IS the last buy price, so this equals the old behaviour until
          the first strike; higher stages: -3 means "3% below the level
          the price already reached", i.e. the stop travels with the
          strikes (never a stale percent against an old buy price).
        - "entry": percent is relative to MY invested value in
          the coin (amount x average entry) — pct=-2 on a stop = "sell
          when my holding loses 2%"; pct=+5 on a profit trigger = "sell
          when my holding gains 5%". After every re-buy the «قیمت خرید»
          in the holdings box is updated to the LAST buy price and the
          engine re-arms the stop from it, so the percent travels with
          the coin through re-buys.
        - "current": percent relative to the coin's CURRENT price —
          pct=-3 => trigger = now x 0.97 (kept for API compatibility).
        The absolute trigger is always recomputed and stored from that
        percentage, so editing the percent re-writes the number and the
        stored percent is re-derived when an absolute number is given.
        """
        symbol = (symbol or "").strip().upper()
        side = (side or "sell").strip().lower()
        kind = (kind or "stop").strip().lower()
        base_mode = (base_mode or "entry").strip().lower()
        if base_mode not in ("current", "entry", "hadd"):
            return {"ok": False,
                    "error": "مبنای درصد باید hadd (قیمت حد)، current (قیمت فعلی) یا entry (قیمت خرید) باشد"}
        if side not in ("sell",):
            return {"ok": False,
                    "error": "فقط حد ضرر/حد سود پشتیبانی می‌شود"}
        if kind not in ("stop", "profit"):
            return {"ok": False,
                    "error": "نوع حد باید stop (حد ضرر) یا profit (حد سود) باشد"}
        pos = self.storage.position(symbol)
        if not pos or d(pos["amount"]) <= 0:
            return {"ok": False, "error": "موقعیتی برای " + symbol + " نیست"}
        cur = self._live_price_for(symbol)
        plans = self.storage.kv_get("exit_plans", {})
        if not isinstance(plans, dict):
            plans = {}
        plan = dict(plans.get(symbol) or {})
        entry = d(str(pos.get("avg_entry", "0")))
        amount = d(pos["amount"])
        last_buy = d(str(pos.get("last_buy_price", "0") or 0))
        # TWO bases by kind:
        # - kind="stop"   حد ضرر: percent reads against the BUY price
        #   (avg entry, then the LAST buy price) by default, or against
        #   the HADD (قیمت حد) when base_mode="hadd" — the base the
        #   dashboard editor uses, so «حد» and «قیمت حد» stay in step
        # - kind="profit" حد سود: percent reads against the plan's HADD
        #   (strike base — stage 0 = last buy, then the struck levels)
        hadd = self._hadd_base(plan, pos.get("last_buy_price"), entry)
        base_stop = entry if entry > 0 else (last_buy if last_buy > 0
                                             else hadd)
        base_tp = hadd if hadd > 0 else base_stop
        if kind == "profit" or base_mode == "hadd":
            base = base_tp
        else:
            base = base_stop
        if base <= 0:
            base = entry if entry > 0 else (cur or ZERO)
        key_stop = "stop" if kind == "stop" else "take_profit_manual"
        key_pct = "stop_pct" if kind == "stop" else "take_profit_manual_pct"
        if pct is not None:
            try:
                pct_d = d(str(pct))
            except Exception:
                return {"ok": False, "error": "درصد نامعتبر است"}
            if pct_d == 0 or base <= 0:
                return {"ok": False,
                        "error": "درصد نامعتبر یا قیمت مرجع (قیمت خرید) موجود نیست"}
            if kind == "stop" and pct_d > 0:
                return {"ok": False,
                        "error": "درصد حد ضرر فروش باید منفی باشد (مثلاً ‎-2 = ۲٪ زیر قیمت خرید)"}
            if kind == "profit" and pct_d < 0:
                return {"ok": False,
                        "error": "درصد حد سود باید مثبت باشد (مثلاً ‎+5 = ۵٪ بالاتر از قیمت حد)"}
            # percent base: kind-dependent (buy anchor for the stop, the
            # hadd for the profit trigger) or the coin's current price
            # (base_mode="current").
            if base_mode == "current":
                if cur is None or cur <= 0:
                    return {"ok": False,
                            "error": "قیمت لحظه‌ای " + symbol +
                            " موجود نیست — یک چرخه اجرا کنید یا بعد از واریز دوباره تلاش کنید"}
                base_eff = cur
            else:
                base_eff = base
            stop_d = base_eff * (1 + pct_d / 100)
            stop_d = stop_d.quantize(Decimal("0.000001"))
        else:
            try:
                stop_d = d(str(stop))
            except Exception:
                return {"ok": False, "error": "عدد حد نامعتبر است"}
            if stop_d > 0 and base > 0:
                plan[key_pct] = float(((stop_d - base) / base * 100)
                                      .quantize(Decimal("0.01")))
        if stop_d <= 0:
            if symbol in plans:
                plans[symbol].pop(key_stop, None)
                plans[symbol].pop(key_pct, None)
                # sweep the removed buy-side keys of earlier versions
                plans[symbol].pop("buy_stop", None)
                plans[symbol].pop("buy_stop_pct", None)
                self.storage.kv_set("exit_plans", plans)
            return {"ok": True, "symbol": symbol, "side": side,
                    "kind": kind, "stop": 0,
                    "removed": True, "distance": "—"}
        # Sanity-check the live price: a garbage ticker reading (unit or
        # currency mix-up) used to poison the guard below — e.g. an IRT
        # coin quoted at 2,700 instead of 270,000 made a perfectly valid
        # -2% stop (265,000) look "above current" and get rejected with
        # «حد ضرر فروش باید پایین‌تر از قیمت فعلی باشد». Only trust the
        # price when it is within 5x..1/5x of the entry.
        price_ok = bool(cur and cur > 0 and base > 0
                        and (cur <= base * 5) and (cur >= base / 5))
        clamped = False
        if kind == "stop":
            if price_ok and stop_d >= cur:
                # Clamp instead of rejecting: a position bought higher can
                # already sit below its own -2% line (e.g. entry=100k,
                # now=96k -> -2% stop=98k > now). Refusing it left the
                # position UNPROTECTED; clamping arms it at the current
                # price (≈ instant guard) and the user's percent still
                # arms correctly on the next re-buy.
                stop_d = (cur * Decimal("0.999")).quantize(
                    Decimal("0.000001"))
                if pct is not None:
                    eff_pct = ((stop_d - base) / base * 100).quantize(
                        Decimal("0.01"))
                    plan[key_pct] = float(eff_pct)
                clamped = True
        else:
            if price_ok and stop_d <= cur:
                # Same clamping for the profit side: if the coin already
                # sits ABOVE the requested trigger (e.g. -20% relative to
                # a much higher entry), the take-profit must still sit
                # ABOVE the current price to be reachable — park it just
                # above current instead of refusing.
                stop_d = (cur * Decimal("1.001")).quantize(
                    Decimal("0.000001"))
                if pct is not None:
                    eff_pct = ((stop_d - base) / base * 100).quantize(
                        Decimal("0.01"))
                    plan[key_pct] = float(eff_pct)
                clamped = True
        plan[key_stop] = float(stop_d)
        if pct is not None:
            plan[key_pct] = float(pct_d)
        # persist the HADD only from a PROFIT-side edit (the strike base
        # belongs to the take-profit); a stop edit re-derives its own
        # percent from the BUY anchor and must not seed a stale hadd
        if (kind == "profit" and base > 0 and
                d(str(plan.get("base_price", 0) or 0)) <= 0):
            plan["base_price"] = float(base)
        if not plan.get("tps"):
            plan["tps"] = []
        if not plan.get("stage"):
            plan["stage"] = 0
        plans[symbol] = plan
        self.storage.kv_set("exit_plans", plans)
        dist = "—"
        if cur and cur > 0:
            diff_pct = ((stop_d - cur) / cur * 100).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP)
            dist = ("+" + diff_pct.to_eng_string()
                    if diff_pct > 0 else diff_pct.to_eng_string()) + "%"
        invested = amount * (entry if entry > 0 else (cur or ZERO))
        out = {"ok": True, "symbol": symbol, "side": side, "kind": kind,
               "stop": float(stop_d),
               "entry": float(entry) if entry > 0 else None,
               "hadd": float(base) if base > 0 else None,
               "invested": float(invested),
               "pct": plan.get(key_pct), "distance": dist,
               "base": ("current" if base_mode == "current"
                        else ("buy" if kind == "stop" and
                              base_mode != "hadd" else "hadd"))}
        if clamped:
            # Persian notice: the requested level sat on the wrong side of
            # the current price (position already deeper than the percent,
            # or the % base is an older, higher entry) — armed at the
            # current price instead so the position is never left naked.
            out["notice"] = (
                "چون قیمت فعلی از سطح درخواستی عبور کرده بود، سطح دقیقاً "
                "روی قیمت فعلی (" + fmt_price(cur) + ") مسلح شد تا موقعیت "
                "بدون حفاظ نماند")
        return out

    def set_all_stops(self, pct: float, kind: str = "stop",
                      base: str = "entry") -> dict:
        """Apply ONE stop percent to EVERY held coin at once (the dashboard
        «حد ضرر همه» / «حد سود همه» chips). base="entry" (default): each
        coin's trigger reads against its HADD (strike base — stage 0 =
        the last buy price, moved up by every reached strike) x
        (1 + pct/100). base="current" uses the coin's live price
        instead. Per-coin editing stays possible afterwards. Coins
        without a usable base are skipped with a reason."""
        try:
            pct_d = d(str(pct))
        except Exception:
            return {"ok": False, "error": "درصد نامعتبر است"}
        if pct_d == 0:
            return {"ok": False, "error": "درصد صفر مجاز نیست"}
        if kind == "stop" and pct_d > 0:
            return {"ok": False,
                    "error": "برای حد ضرر، درصد را منفی بده (مثلاً ‎-3)"}
        if kind == "profit" and pct_d < 0:
            return {"ok": False,
                    "error": "برای حد سود، درصد را مثبت بده (مثلاً ‎+5)"}
        snap = self.latest_snapshot or {}
        prices = {}
        for m in snap.get("markets", []):
            if m.get("last_price"):
                prices[m.get("symbol")] = d(str(m["last_price"]))
        applied, skipped = [], []
        for p in self.storage.positions():
            sym = p["market"]
            try:
                if d(p["amount"]) <= 0:
                    continue
            except Exception:
                continue
            if not prices.get(sym):
                px = self._live_price_for(sym)
                if not px:
                    skipped.append({"symbol": sym,
                                    "reason": "قیمت لحظه‌ای موجود نیست"})
                    continue
                prices[sym] = px
            r = self.set_stop_loss(sym, 0.0, side="sell",
                                   pct=float(pct_d), kind=kind, base_mode=base)
            if r.get("ok"):
                applied.append({"symbol": sym, "stop": r.get("stop"),
                                "pct": r.get("pct"),
                                "distance": r.get("distance")})
            else:
                skipped.append({"symbol": sym, "reason": r.get("error")})
        self.events.publish("AUTO_STOP_SET", "engine", "",
                            {"symbol": "ALL", "count": len(applied),
                             "pct": float(pct_d)})
        return {"ok": True, "count": len(applied), "applied": applied,
                "skipped": skipped}

    def _sync_after_cancel(self) -> None:
        """Best-effort refresh of order statuses + wallet after cancelling."""
        try:
            self.reconcile_real_orders()
        except Exception:
            pass
        try:
            self.sync_real_wallet()
        except Exception:
            pass

    @staticmethod
    def _order_age_seconds(created) -> float:
        """Age of an order from its exchange-reported placement time.
        Tolerates ISO strings and epoch numbers (s or ms)."""
        if not created:
            return -1.0
        now = datetime.now(timezone.utc)
        if isinstance(created, (int, float)) or \
                (isinstance(created, str) and created.strip().isdigit()):
            try:
                ts = float(created)
                if ts > 1e12:          # milliseconds
                    ts /= 1000.0
                return (now.timestamp() - ts)
            except Exception:
                return -1.0
        try:
            return (now - parse_iso(str(created))).total_seconds()
        except Exception:
            return -1.0

    # ---------------------------------------------------- SS101 dust convert
    @staticmethod
    def _extract_min_amount(exc=None, default: int = 250000) -> int:
        """Parse the convert-market minimum-notional floor (in RIALS) from
        Nobitex's error text ('Amount should be more than 2.50000E+6')
        — used only as a guard; the real value comes from the API when a
        quote is actually requested. Falls back to 250,000 rial (the
        smallest observed floor across coins)."""
        import re as _re
        txt = str(exc or "")
        m = _re.search(r"more than ([0-9.]+E\+[0-9]+)", txt)
        if m:
            try:
                return int(float(m.group(1)))
            except Exception:
                pass
        return default

    def _dust_exclude_set(self) -> set:
        """Coins never to auto-convert (from config, always incl. quote
        assets USDT/IRT)."""
        excl = set()
        raw = str(getattr(self.cfg.risk, "dust_convert_exclude", "") or "")
        for tok in re.split(r"[,\s]+", raw):
            tok = tok.strip().upper()
            if tok:
                excl.add(tok)
        excl.update({"USDT", "IRT", ""})
        return excl

    def _convert_dust_holdings(self, force: bool = False) -> dict:
        """SS101: convert tiny coin balances into IRT.

        A coin is "dust" when its IRT value is below
        risk.dust_convert_threshold_irt. Conversion goes through the
        exchange's own convert market (panel/balance/convert equivalent):
        get-quote -> create-trade, executed on the real account. Each
        conversion is logged as a DUST_CONVERTED event; failures are
        logged per-coin and never break the cycle.

        force=True is used by the manual dashboard button: converts EVERY
        non-excluded coin below the threshold (no cap) — the UI shows the
        full list with values first and the user confirms.
        """
        out = {"converted": 0, "failed": 0, "details": [], "blocked": []}
        if not self._real_attached:
            return out
        thr = d(str(getattr(self.cfg.risk, "dust_convert_threshold_irt", 0)
                    or 0))
        if thr <= 0 and not force:
            return out
        cap = max(1, int(getattr(self.cfg.risk,
                                 "dust_convert_max_per_cycle", 3) or 3))
        excl = self._dust_exclude_set()
        # current prices: snapshot first, then live ticker fallback
        snap = self.latest_snapshot or {}
        prices = {}
        usdt_irt = None
        for m in snap.get("markets", []):
            if m.get("last_price"):
                prices[m["symbol"]] = d(str(m["last_price"]))
        try:
            usdt_irt = self._usdt_rates(prices).get("IRT")
        except Exception:
            usdt_irt = None
        # evaluate every real holding that is not a quote asset
        balances = {}
        try:
            doc = self.exchange.portfolio()
            for a in (doc.get("assets") or []):
                coin = (a.get("asset") or "").upper()
                if coin in excl:
                    continue
                amt = d(str(a.get("available") or 0))
                if amt > 0:
                    balances[coin] = amt
        except Exception:
            return out
        candidates = []
        blocked = []
        for coin, amt in balances.items():
            # انحصار بخشی: تبدیل کیف‌پول همیشه به تومان می‌نشیند، پس سکهٔ
            # بخش تتری بدون «تعامل بین بازارها» تبدیل نمی‌شود (انتقال بین
            # دو بازار = ممنوع)
            ok_section, section_reason = self._conversion_section_allowed(coin)
            if not ok_section:
                blocked.append({"coin": coin, "reason": section_reason})
                continue
            # «بازار معاملات»: with the coin's section switched OFF the
            # AUTOMATIC pass leaves it alone — converting a Toman-side coin
            # into Toman is still the bot trading that section. The manual
            # «تبدیل کن» button (force=True) is the user's own order and
            # keeps converting exactly the list they confirmed.
            if not force:
                section = self._coin_section(coin)
                if section and not self._quote_market_enabled(coin + section):
                    blocked.append({
                        "coin": coin,
                        "reason": "بخش %s در «بازار معاملات» خاموش است" % (
                            "تومانی" if section == "IRT" else "تتری")})
                    continue
            # IRT value of the holding: prefer the coin's own IRT market,
            # else USDT market × USDT rate
            px = None
            for mkt in (coin + "IRT", coin + "USDT"):
                p = prices.get(mkt)
                if p is None:
                    fp = self._price_fallback(mkt)
                    p = d(str(fp)) if fp is not None else None
                if p is not None:
                    px = p
                    if mkt.endswith("USDT") and usdt_irt:
                        # usdt_irt comes from _usdt_rates() as a float while
                        # the price is Decimal — mixing them raised
                        # TypeError and aborted the whole conversion pass
                        px = p * d(str(usdt_irt))
                    break
            if px is None:
                continue
            value_irt = px * amt
            if value_irt >= thr:
                continue
            candidates.append((coin, amt, value_irt))
        if blocked:
            # ONE event per cycle (not per coin) so the event file cannot be
            # flooded by a Tether-side dust balance we refuse to convert
            out["blocked"] = blocked
            self.events.publish("DUST_CONVERT_BLOCKED", "engine", "", {
                "coins": [b["coin"] for b in blocked],
                "reason": "CROSS_MARKET_TRANSFER_NOT_ALLOWED"})
        # biggest dust first so the cap spends on the most valuable crumbs
        candidates.sort(key=lambda x: -float(x[2]))
        # force mode (manual button) has NO cap — the user saw the full
        # list and confirmed; only auto mode respects the per-cycle cap.
        targets = candidates if force else candidates[:cap]
        for coin, amt, value_irt in targets:
            # «تبدیل موجودی‌های خرد» path (SS101): POST /users/wallets/convert
            # converts the WHOLE available balance with NO min-notional
            # floor — verified live (XLM ~4k toman converted fine while the
            # exchange/get-quote market rejects anything below 25k toman).
            try:
                res = self.exchange.convert_wallet_balance(coin)
                out["converted"] += 1
                detail = {"coin": coin, "value_irt": float(value_irt),
                          "converted_amount": res.get("converted_amount"),
                          "result": res}
                out["details"].append(detail)
                self.events.publish("DUST_CONVERTED", "engine", "", {
                    "coin": coin, "amount": float(amt),
                    "value_irt": float(value_irt),
                    "converted_amount": res.get("converted_amount"),
                })
            except NobitexError as exc:
                out["failed"] += 1
                out["details"].append({"coin": coin, "error": exc.code,
                                       "message": str(exc)})
                self.events.publish("DUST_CONVERT_FAILED", "engine", "", {
                    "coin": coin, "code": exc.code, "message": str(exc)})
            except Exception as exc:
                out["failed"] += 1
                out["details"].append({"coin": coin,
                                       "error": "CONVERT_ERROR",
                                       "message": str(exc)})
                self.events.publish("DUST_CONVERT_FAILED", "engine", "", {
                    "coin": coin, "code": "CONVERT_ERROR",
                    "message": str(exc)})
        if out["converted"] or out["failed"]:
            # funds changed — resync the ledger right away so the freed
            # IRT is visible to the risk engine in the very next step
            try:
                self.sync_real_wallet()
            except Exception:
                pass
        return out

    def _sweep_bot_micro_positions(self) -> dict:
        """حالت ریسک — تجمیع سرمایه.

        Every BOT-OPENED position (real avg entry > 0) whose CURRENT Toman
        value sits below the exchange minimum order value is UNSELLABLE
        (the exit path would just hit below_min) — it only fragments the
        capital. In risk mode those crumbs go back to Toman through the
        exchange's no-minimum wallet convert (the same SS101 path), a few
        per cycle, so the freed cash can rotate into the day's top movers.

        NEVER touched: the user's pre-existing wallet coins (avg_entry 0),
        USDT/IRT quote assets and the dust_convert_exclude list.
        """
        out = {"converted": 0, "failed": 0, "details": [], "blocked": 0}
        if not self._real_attached:
            return out
        max_value = d(str(getattr(self.cfg.risk,
                                  "risk_mode_sweep_max_value_irt", 200000.0)
                          or 0))
        cap = max(1, int(getattr(self.cfg.risk,
                                 "risk_mode_sweep_per_cycle", 4) or 4))
        if max_value <= 0:
            return out
        excl = self._dust_exclude_set()
        # live prices: snapshot first, then the 60s ticker fallback
        snap = self.latest_snapshot or {}
        prices = {}
        usdt_irt = None
        for m in snap.get("markets", []):
            if m.get("last_price"):
                prices[m["symbol"]] = d(str(m["last_price"]))
        try:
            usdt_irt = self._usdt_rates(prices).get("IRT")
        except Exception:
            usdt_irt = None
        min_usdt = d(str(getattr(self.cfg.exchange, "min_order_usdt", 0.44)))
        usdtirt_d = d(str(prices.get("USDTIRT") or 0))
        min_irt = (min_usdt * usdtirt_d) if usdtirt_d else ZERO
        # newest buys first — those are yesterday's rotation picks, not
        # the user's legacy holdings
        rows = sorted(self.storage.positions(),
                      key=lambda p: (p.get("last_buy_at") or ""),
                      reverse=True)
        for pos in rows:
            if out["converted"] + out["failed"] >= cap:
                break
            market = pos["market"]
            base = market[:-4] if market.endswith("USDT") else market[:-3]
            if not base or base in excl or base in ("USDT", "IRT"):
                continue
            if not self._quote_market_enabled(market):
                out["blocked"] += 1
                continue  # بخش خاموش «بازار معاملات» — خرده هم فروخته نمی‌شود
            # انحصار بخشی: تبدیل کیف‌پول به تومان می‌نشیند، پس خردهٔ بخش
            # تتری بدون «تعامل بین بازارها» به تومان منتقل نمی‌شود
            if market.endswith("USDT") and not self._cross_market_allowed():
                out["blocked"] += 1
                continue
            if d(pos["amount"]) <= 0:
                continue
            if d(str(pos.get("avg_entry") or 0)) <= 0:
                continue  # pre-existing user holding — never swept
            # current Toman value: own IRT market, else USDT market x rate
            px = None
            for mkt in (base + "IRT", base + "USDT"):
                p = prices.get(mkt)
                if p is None:
                    fp = self._price_fallback(mkt)
                    p = d(str(fp)) if fp is not None else None
                if p is not None:
                    px = p
                    if mkt.endswith("USDT") and usdt_irt:
                        # float rate × Decimal price would raise TypeError
                        # and kill the sweep half-way
                        px = p * d(str(usdt_irt))
                    break
            if px is None:
                continue
            value = px * d(pos["amount"])
            if value > max_value or (min_irt > 0 and value >= min_irt):
                continue  # sellable or too valuable — leave it to the
                          # normal exit plan
            try:
                res = self.exchange.convert_wallet_balance(base)
                out["converted"] += 1
                out["details"].append({"coin": base,
                                       "value_irt": float(value)})
                self.events.publish("RISK_MODE_SWEPT", "engine", "", {
                    "coin": base, "value_irt": float(value)})
            except NobitexError as exc:
                out["failed"] += 1
                out["details"].append({"coin": base, "error": exc.code})
                self.events.publish("RISK_MODE_SWEEP_FAILED", "engine", "",
                                    {"coin": base, "code": exc.code})
            except Exception as exc:
                out["failed"] += 1
                out["details"].append({"coin": base,
                                       "error": "CONVERT_ERROR"})
                self.events.publish("RISK_MODE_SWEEP_FAILED", "engine", "",
                                    {"coin": base, "code": "CONVERT_ERROR",
                                     "message": str(exc)})
        if out["converted"]:
            try:
                self.sync_real_wallet()
            except Exception:
                pass
        return out

    def dust_preview(self, coins: list | None = None) -> dict:
        """List the dust holdings WOULD be converted — no side effects.

        For the confirmation dialog: each row = coin, amount, IRT value,
        convertible yes/no with the Persian reason when not. Used by the
        dashboard before the user confirms the real conversion.
        """
        out = {"items": [], "total_irt": 0.0, "real_attached":
               self._real_attached, "threshold_irt": float(
                   getattr(self.cfg.risk, "dust_convert_threshold_irt", 0)
                   or 0),
               "cross_market_allowed": self._cross_market_allowed(),
               "interaction": self._effective_market_interaction()[0]}
        if not self._real_attached:
            out["error"] = "ابتدا به حساب واقعی لاگین کنید"
            return out
        thr = d(str(out["threshold_irt"] or 0))
        excl = self._dust_exclude_set()
        snap = self.latest_snapshot or {}
        prices = {}
        for m in snap.get("markets", []):
            if m.get("last_price"):
                prices[m["symbol"]] = d(str(m["last_price"]))
        usdt_irt = None
        try:
            usdt_irt = self._usdt_rates(prices).get("IRT")
        except Exception:
            usdt_irt = None
        balances = {}
        try:
            doc = self.exchange.portfolio()
            for a in (doc.get("assets") or []):
                coin = (a.get("asset") or "").upper()
                if coin in excl:
                    continue
                amt = d(str(a.get("available") or 0))
                if amt > 0:
                    balances[coin] = amt
        except Exception as exc:
            out["error"] = "خواندن موجودی واقعی ناموفق: %s" % exc
            return out
        for coin, amt in sorted(balances.items()):
            if coins and coin not in coins:
                continue
            # a Tether-section coin would be a cross-market transfer: the
            # dialog greys it out with the reason instead of hiding it
            ok_section, section_reason = self._conversion_section_allowed(coin)
            px = None
            for mkt in (coin + "IRT", coin + "USDT"):
                p = prices.get(mkt)
                if p is None:
                    fp = self._price_fallback(mkt)
                    p = d(str(fp)) if fp is not None else None
                if p is not None:
                    px = p
                    if mkt.endswith("USDT") and usdt_irt:
                        px = p * Decimal(str(usdt_irt))
                    break
            value_irt = (px * amt) if px is not None else None
            item = {"coin": coin, "amount": float(amt),
                    "value_irt": float(value_irt) if value_irt is not None
                    else None}
            if value_irt is None:
                item["convertible"] = False
                item["reason"] = "قیمت لحظه‌ای پیدا نشد"
            elif not ok_section:
                item["convertible"] = False
                item["reason"] = section_reason
            elif thr > 0 and value_irt >= thr:
                item["convertible"] = False
                item["reason"] = "دارایی خرد نیست (بالای آستانه)"
            else:
                item["convertible"] = True
            out["items"].append(item)
            if value_irt is not None:
                out["total_irt"] += float(value_irt)
        # convertible first (bigger value first), then the not-convertible
        out["items"].sort(key=lambda x: (not x.get("convertible"),
                                         -(x.get("value_irt") or 0)))
        return out

    def convert_selected(self, coins: list) -> dict:
        """Convert ONLY the coins the user confirmed in the dialog.

        Bypasses the threshold filter (the user already saw and picked the
        rows) but keeps every safety: exclusions, min-notional floor,
        per-coin error capture. The per-cycle cap does not apply — this is
        an explicit user-confirmed batch.
        """
        wanted = {str(c).strip().upper() for c in (coins or []) if str(c).strip()}
        if not wanted:
            return {"converted": 0, "failed": 0,
                    "details": [{"error": "NO_SELECTION",
                                 "message": "سکه‌ای انتخاب نشده بود"}]}
        prev_cap_guard = getattr(self.cfg.risk, "dust_convert_max_per_cycle",
                                 0)
        out = self._convert_selected_impl(wanted)
        return out

    def _convert_selected_impl(self, wanted: set) -> dict:
        """Real conversion of an explicit coin set (user-confirmed)."""
        out = {"converted": 0, "failed": 0, "details": []}
        if not self._real_attached:
            out["failed"] = len(wanted)
            out["details"] = [{"coin": c, "error": "NOT_ATTACHED",
                               "message": "حساب واقعی وصل نیست"}
                              for c in sorted(wanted)]
            return out
        excl = self._dust_exclude_set()
        snap = self.latest_snapshot or {}
        prices = {}
        for m in snap.get("markets", []):
            if m.get("last_price"):
                prices[m["symbol"]] = d(str(m["last_price"]))
        usdt_irt = None
        try:
            usdt_irt = self._usdt_rates(prices).get("IRT")
        except Exception:
            usdt_irt = None
        balances = {}
        try:
            doc = self.exchange.portfolio()
            for a in (doc.get("assets") or []):
                coin = (a.get("asset") or "").upper()
                if coin not in wanted or coin in excl:
                    continue
                amt = d(str(a.get("available") or 0))
                if amt > 0:
                    balances[coin] = amt
        except Exception as exc:
            out["failed"] = len(wanted)
            out["details"] = [{"coin": c, "error": "PORTFOLIO_ERROR",
                               "message": str(exc)} for c in sorted(wanted)]
            return out
        for coin in sorted(wanted):
            ok_section, section_reason = self._conversion_section_allowed(coin)
            if not ok_section:
                out["failed"] += 1
                out["details"].append({"coin": coin,
                                       "error": "TRANSFER_BLOCKED",
                                       "message": section_reason})
                self.events.publish("DUST_CONVERT_FAILED", "engine", "", {
                    "coin": coin, "code": "TRANSFER_BLOCKED",
                    "message": section_reason})
                continue
            amt = balances.get(coin)
            if amt is None:
                out["details"].append({"coin": coin,
                                       "error": "NOT_HELD",
                                       "message": coin in excl and
                                       "سکه مستثنی است" or
                                       "موجودی‌ای از این سکه نیست"})
                out["failed"] += 1
                continue
            px = None
            for mkt in (coin + "IRT", coin + "USDT"):
                p = prices.get(mkt)
                if p is None:
                    fp = self._price_fallback(mkt)
                    p = d(str(fp)) if fp is not None else None
                if p is not None:
                    px = p
                    if mkt.endswith("USDT") and usdt_irt:
                        px = p * Decimal(str(usdt_irt))
                    break
            if px is None:
                out["failed"] += 1
                out["details"].append({"coin": coin,
                                       "error": "NO_PRICE",
                                       "message": "قیمت لحظه‌ای پیدا نشد"})
                continue
            value_irt = px * amt
            # «تبدیل موجودی‌های خرد» path: POST /users/wallets/convert
            # converts the WHOLE available balance with NO min-notional
            # floor (verified live: XLM ~4k toman converted while the
            # get-quote market rejects < 25k toman). The value shown in
            # the dialog is informational only — the exchange takes all.
            try:
                res = self.exchange.convert_wallet_balance(coin)
                out["converted"] += 1
                out["details"].append({"coin": coin,
                                       "value_irt": float(value_irt),
                                       "converted_amount":
                                           res.get("converted_amount"),
                                       "result": res})
                self.events.publish("DUST_CONVERTED", "engine", "", {
                    "coin": coin, "amount": float(amt),
                    "value_irt": float(value_irt),
                    "converted_amount": res.get("converted_amount")})
            except NobitexError as exc:
                out["failed"] += 1
                out["details"].append({"coin": coin, "error": exc.code,
                                       "message": str(exc)})
                self.events.publish("DUST_CONVERT_FAILED", "engine", "", {
                    "coin": coin, "code": exc.code, "message": str(exc)})
            except Exception as exc:
                out["failed"] += 1
                out["details"].append({"coin": coin,
                                       "error": "CONVERT_ERROR",
                                       "message": str(exc)})
                self.events.publish("DUST_CONVERT_FAILED", "engine", "", {
                    "coin": coin, "code": "CONVERT_ERROR",
                    "message": str(exc)})
        if out["converted"]:
            try:
                self.sync_real_wallet()
            except Exception:
                pass
        return out

    def _cancel_stale_orders(self, chase: bool = False) -> list:
        """Cancel OUR live BUY orders still resting (unfilled) so the locked
        IRT returns to the wallet and fresh orders can be sent. The source
        of truth is the EXCHANGE's open-order list with its real placement
        time (the ledger only knows when an order was first discovered).

        chase=True (limit-chase): cancel resting BUYs priced BELOW the
        current price immediately — waiting the full timeout in a rising
        market keeps capital locked forever. The same cycle re-posts the
        order at the new price, so entries never lag the market.
        chase=False cancels only orders older than
        risk.stale_order_cancel_min minutes. 0 disables the reaper.
        """
        mins = int(getattr(self.cfg.risk, "stale_order_cancel_min", 10) or 0)
        if mins <= 0 and not chase:
            return []
        if not (self._real_attached and
                self.cfg.exchange.live_trading):
            return []
        try:
            opens = self.exchange.open_orders()
        except Exception:
            return []
        # local ledger index: exchange id + client id -> local order row
        ledger = {}
        try:
            for row in self.storage.orders(limit=300):
                if row.get("exchange_order_id"):
                    ledger[str(row["exchange_order_id"])] = row
                if row.get("client_order_id"):
                    ledger["c:" + str(row["client_order_id"])] = row
        except Exception:
            pass
        cancelled = []
        chased = []
        for o in opens:
            if (o.get("side") or "").upper() not in ("BUY", "SELL"):
                continue
            age = self._order_age_seconds(o.get("created_at"))
            reason = None
            if chase and (o.get("side") or "").upper() == "BUY" \
                    and o.get("status", "OPEN").upper() == "OPEN" \
                    and (mins > 0 and age < mins * 60):
                # limit-chase: resting BUY under the live price -> repost
                try:
                    cur = self._price_fallback(o.get("market") or "")
                    if not cur:
                        # ticker naming can 400 on some symbols — the
                        # freshest snapshot is the second price source
                        snap = self.latest_snapshot or {}
                        for mk in (snap.get("markets") or []):
                            if mk.get("symbol") == o.get("market"):
                                cur = mk.get("last_price")
                                break
                    opx = d(str(o.get("price") or 0))
                    if cur and opx > 0 and opx < d(str(cur)) * Decimal("0.999"):
                        reason = "limit_chase"
                except Exception:
                    pass
            elif mins > 0 and age >= mins * 60:
                reason = "stale_timeout"
            if not reason:
                continue
            exch_id = str(o.get("order_id") or "")
            coid = str(o.get("client_order_id") or "")
            row = (ledger.get(exch_id)
                   or ledger.get("c:" + coid) or {})
            local_id = row.get("order_id", "")
            try:
                self.exchange.cancel_order(exch_id)
            except Exception:
                continue          # already filled/cancelled — next sync resolves
            if local_id:
                self.storage.update_order_status(local_id, "CANCELLED")
            else:
                # order known to the exchange only — record it so history shows it
                try:
                    self.storage.save_order({
                        "order_id": "exch_" + exch_id,
                        "client_order_id": coid,
                        "decision_id": "",
                        "market": canonical_symbol(
                            o.get("market", "")),
                        "side": o.get("side", ""), "order_type": "LIMIT",
                        "price": str(o.get("price") or 0),
                        "amount": str(o.get("amount") or 0),
                        "matched_amount": str(o.get("matched_amount") or 0),
                        "unmatched_amount": str(o.get("unmatched_amount") or 0),
                        "status": "CANCELLED",
                        "exchange_order_id": exch_id,
                        "created_at": o.get("created_at") or utc_now_iso(),
                        "updated_at": utc_now_iso()})
                except Exception:
                    pass
            # clear the blocking SUBMITTED execution (it reads the JSON
            # payload, so re-save the whole row with the new status)
            try:
                for ex in self.storage.executions_recent(150):
                    if (str(ex.get("exchange_order_id") or "") == exch_id
                            or (coid and str(ex.get("client_order_id") or
                                             "") == coid)):
                        ex["status"] = "CANCELLED"
                        ex["error"] = ((ex.get("error") or "") +
                                       " | " + reason).strip()
                        self.storage.save_execution(ex)
            except Exception:
                pass
            cancelled.append({"order_id": exch_id, "market": o.get("market"),
                              "side": o.get("side"), "reason": reason,
                              "age_min": int(age // 60)})
        if cancelled:
            reasons = sorted({c["reason"] for c in cancelled})
            self.events.publish("ORDER_CANCELLED", "engine", "",
                                {"reason": "+".join(reasons),
                                 "minutes": mins, "cancelled": cancelled})
            self._sync_after_cancel()
        return cancelled

    def _sell_capital_to_irt(self, ex: dict) -> dict:
        """After a real SELL fill, move the freed capital into the wallet.

        The fill application credits IRT paper-side, but the real exchange
        still shows the coins locked until the order closes and the wallet
        is pulled again — without this step the user sees the capital as
        neither sold nor spendable. Cancel leftovers, refresh statuses,
        re-pull the wallet and report the proceeds.
        """
        res = {"freed": 0, "irt_credited": None, "errors": []}
        if not self._real_attached:
            return res
        market = ex.get("market") or ""
        try:
            opens = [o for o in self.exchange.open_orders(market)
                     if (o.get("side") or "").upper() == "SELL"]
            for o in opens:
                try:
                    self.exchange.cancel_order(o["order_id"])
                    res["freed"] += 1
                except Exception as exc:
                    res["errors"].append(str(exc))
        except Exception as exc:
            res["errors"].append("open_orders: " + str(exc))
        try:
            self.backfill_real_fills()
        except Exception:
            pass
        sync = self.sync_real_wallet()
        if not sync.get("ok") and sync.get("error"):
            res["errors"].append(sync["error"])
        irt = self.portfolio.balances.get("IRT") or {}
        avail = self._dec_of(irt.get("available"))
        res["irt_credited"] = float(avail) if avail > 0 else 0.0
        return res

    def sell_position_now(self, symbol: str) -> dict:
        """One-click SELL of an entire held position at market reality.

        Before building the SELL decision any open REAL order on this
        market is cancelled (a standing order locks part of the coins),
        and the wallet is re-synced so the sell sees the full holding —
        coins locked in our own stale orders would otherwise be sold
        short or rejected. Converts everything into IRT (quote). Builds
        a market-referenced SELL decision for the full holding and
        pushes it through the normal pipeline — the risk engine still
        validates balance/limits (SS100), exits bypass entry-specific
        caps but never balance checks.
        """
        symbol = (symbol or "").strip().upper()
        note = None
        pos_hit = self.storage.position(symbol) if symbol else None
        if not pos_hit or d(pos_hit["amount"]) <= 0:
            # the clicked row holds no coins (or the route only has the
            # other quote): fall back to the SAME COIN's market in the
            # quote the «بازار معاملات» selector points at, so a «فروش»
            # click always reaches the real holding.
            alt, _err = self._click_market_for(symbol)
            if alt and alt != symbol:
                alt_pos = self.storage.position(alt)
                if alt_pos and d(alt_pos["amount"]) > 0:
                    note = ("فروش از بازار %s انجام میشود (همان سکه در بازار "
                            "انتخابشدهٔ «بازار معاملات»)" % alt)
                    symbol = alt
        freed: list = []
        freed_errors: list = []
        if self._real_attached:
            try:
                opens = self.exchange.open_orders(symbol)
            except Exception as exc:
                opens = []
                freed_errors.append("open_orders: " + str(exc))
            for o in opens:
                if (o.get("side") or "").upper() != "SELL":
                    continue  # only SELL orders lock coins of THIS position
                try:
                    self.exchange.cancel_order(o["order_id"])
                    freed.append({"order_id": o["order_id"],
                                  "unmatched": o.get("unmatched_amount")})
                except Exception as exc:
                    freed_errors.append("cancel " + str(o.get("order_id")) +
                                        ": " + str(exc))
            if freed:
                # statuses + balances refresh so risk sees the full coins
                self._sync_after_cancel()
        pos = self.storage.position(symbol)
        if not pos or d(pos["amount"]) <= 0:
            out = {"ok": False,
                   "error": "موقعیتی برای " + symbol + " نیست"}
            if note:
                out["note"] = note
            return out
        snap = self.latest_snapshot or {}
        market_by_symbol = {m["symbol"]: m for m in snap.get("markets", [])}
        m = self._market_row(symbol)
        if not m:
            return {"ok": False,
                    "error": "قیمت زنده برای " + symbol +
                    " پیدا نشد — نماد را بررسی کنید یا چند لحظه بعد "
                    "دوباره تلاش کنید"}
        market_by_symbol.setdefault(symbol, m)
        prices = {mm["symbol"]: mm["last_price"] for mm in snap["markets"]
                  if mm.get("last_price")}
        prices.setdefault(symbol, m.get("last_price"))
        price = float(m.get("last_price") or prices.get(symbol) or 0)
        if price <= 0:
            return {"ok": False, "error": "قیمت موجود نیست"}
        amount = float(d(pos["amount"]))
        now_dt = datetime.now(timezone.utc)
        dec = {
            "decision_id": new_id("dec"),
            "snapshot_id": snap.get("snapshot_id", ""),
            "cycle_id": "manual_sell",
            "symbol": symbol,
            "action": "SELL",
            "confidence": 0.9,
            "time_horizon": "INTRADAY",
            "reason_codes": ["MANUAL_SELL_NOW"],
            "created_at": utc_iso(now_dt),
            "expires_at": utc_iso(now_dt + timedelta(
                seconds=self.cfg.risk.decision_ttl_s)),
            "entry": {"price": price, "min": price, "max": price},
            "stop_loss": {"price": price, "method": "MARKET_STRUCTURE",
                          "reason": "MANUAL_SELL_NOW"},
            "take_profit": [{"price": price, "close_percent": 100}],
            "position_size": {"amount": amount,
                              "quote_value": amount * price},
            "risk": {"max_loss": 0.0, "risk_reward": 0.0},
            "cost": {"estimated_total_cost": self.cfg.fee.taker_rate,
                     "expected_gross_return": 0.0,
                     "expected_net_return": 0.0},
            "exit": True,
        }
        vres = ValidationResult(ok=True)
        validate_ai_decision(dec, self._allowed_symbols(market_by_symbol),
                             vres)
        if not vres.ok:
            return {"ok": False, "error": "AI_VALIDATION: " +
                    "; ".join("%s:%s" % (i.field, i.code)
                              for i in vres.issues[:3])}
        self.storage.save_ai_decision(dec, True)
        self.latest_decisions.append(dec)
        portfolio_snapshot = self.portfolio.portfolio_snapshot(
            prices, self._usdt_rates(prices))
        rd = self.risk.evaluate(dec, m, portfolio_snapshot, prices)
        self.storage.save_risk_decision(rd)
        self.latest_risk.append(rd)
        if rd["status"] != "APPROVED":
            return {"ok": False,
                    "error": "رد توسط موتور ریسک: " +
                    "، ".join(rd["rejection_reasons"]),
                    "rejection_reasons": rd["rejection_reasons"]}
        ex = self.executor.execute(
            dec, rd, prices, "manual_sell_" +
            utc_now_iso().replace("-", "").replace(":", "")[:12])
        self.latest_executions.append(ex)
        # sold capital must land in the wallet as spendable IRT: cancel
        # leftovers, refresh order statuses, re-pull the real wallet
        capital = self._sell_capital_to_irt(ex)
        self.events.publish("MANUAL_SELL", "engine", "",
                            {"symbol": symbol, "status": ex.get("status"),
                             "irt_credited": capital.get("irt_credited")})
        out = {"ok": True, "execution": ex, "market": symbol,
               "freed_orders": freed, "freed_errors": freed_errors,
               "capital": capital}
        if note:
            out["note"] = note
        return out

    _NUM_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")

    def _dec_of(self, val) -> Decimal:
        """Best-effort Decimal from int/float/str (floats via repr, never
        via the 17-digit binary expansion str(2.675) traps us in)."""
        if isinstance(val, Decimal):
            return val
        if isinstance(val, bool):
            return ZERO
        if isinstance(val, int):
            return Decimal(val)
        if isinstance(val, float):
            m = self._NUM_RE.fullmatch(repr(val))
            return Decimal(m.group(0)) if m else ZERO
        try:
            return d(str(val))
        except Exception:
            return ZERO

    def set_live_trading(self, enabled: bool) -> dict:
        """Explicit live-order switch (SS101: demo stays paper by default)."""
        if enabled and not self._real_attached:
            return {"ok": False,
                    "error": "login to the real account first"}
        self.cfg.exchange.live_trading = bool(enabled)
        self.events.publish("LIVE_TRADING_CHANGED", "engine", "",
                            {"enabled": bool(enabled)})
        return {"ok": True, "live_trading": self.cfg.exchange.live_trading}

    def maybe_refresh_token(self) -> None:
        """Best-effort token refresh once per cycle (SS67 resync)."""
        if not self._real_attached:
            return
        try:
            self.nobitex_session.ensure_token()
        except NobitexError as exc:
            self.events.error("ENGINE", "REAL_SESSION_LOST", "WARNING",
                              "%s: %s" % (exc.code, exc), retryable=True)
        except Exception:
            pass

    # statuses that mean the exchange is still working the order
    IN_FLIGHT_STATUSES = ("NEW", "PENDING", "QUEUED", "SUBMITTED", "OPEN",
                          "PARTIAL", "PARTIALLY_FILLED", "ACTIVE")

    def in_flight_orders(self, ttl_s: float = 10.0) -> list:
        """«سفارشهای در جریان» — every order that is NOT finished yet,
        with the detail of what it actually holds: item (market + side),
        price, requested size, filled/remaining amount, fill percent, its
        value, its age and whether the bot or the user placed it.

        While a real account is attached the EXCHANGE open-order list is
        the source of truth (the local ledger can keep a row OPEN after an
        external cancel/fill). The dashboard polls the status every few
        seconds, so that read is cached for `ttl_s`. Demo mode has no
        exchange list at all — the local ledger is used instead.
        """
        try:
            rows = self.storage.orders(limit=300)
        except Exception:
            rows = []
        live = bool(self._real_attached and getattr(
            self.cfg.exchange, "live_trading", False))
        opens = []
        if live:
            now = time.time()
            ts, cached = getattr(self, "_open_orders_cache", (0.0, []))
            if now - ts < ttl_s:
                opens = cached
            else:
                try:
                    opens = self.exchange.open_orders() or []
                except Exception:
                    opens = cached          # last good read beats nothing
                self._open_orders_cache = (now, opens)
        # ledger index: exchange id / client id -> local order row, so an
        # exchange row keeps its local identity (decision, origin)
        led = {}
        for r in rows:
            if r.get("exchange_order_id"):
                led[str(r["exchange_order_id"])] = r
            if r.get("client_order_id"):
                led["c:" + str(r["client_order_id"])] = r
        prices = {}
        for m in (self.latest_snapshot or {}).get("markets", []):
            if m.get("last_price"):
                prices[m.get("symbol")] = m["last_price"]
        out, seen = [], set()
        if live:
            for o in opens:
                item = self._in_flight_item(o, led, prices, "exchange")
                if item:
                    out.append(item)
                    if item.get("exchange_order_id"):
                        seen.add(item["exchange_order_id"])
        # ledger rows still working with NO exchange row yet (just
        # submitted, or demo mode) — a brand-new order must show at once.
        # `seen` holds exchange ids already reported by the exchange list,
        # so only those are skipped; two ledger rows sharing a stale
        # exchange id must BOTH stay visible rather than hide a real order.
        for r in rows:
            if str(r.get("status") or "").upper() \
                    not in self.IN_FLIGHT_STATUSES:
                continue
            if str(r.get("exchange_order_id") or "") in seen:
                continue
            item = self._in_flight_item(r, led, prices, "ledger")
            if item:
                out.append(item)
        # bot orders first, then the freshest ones
        out.sort(key=lambda o: (o.get("origin") != "bot",
                                o.get("age_s") if o.get("age_s") is not None
                                else 0.0))
        return out

    @staticmethod
    def _in_flight_item(o: dict, led: dict, prices: dict,
                        source: str) -> Optional[dict]:
        """One «سفارش در جریان» row: which item, at what price and size,
        how much is filled, what is left, its value and its age."""
        market = canonical_symbol(o.get("market") or "")
        side = str(o.get("side") or o.get("type") or "").upper()
        if not market or side not in ("BUY", "SELL"):
            return None
        coid = str(o.get("client_order_id") or "")
        ex = str(o.get("exchange_order_id") or o.get("order_id") or "")
        if source == "ledger":
            # a ledger row IS the order itself; resolving it through the
            # exchange-id index could borrow ANOTHER row's identity (two
            # rows sharing a stale exchange id), so never re-look-up here
            row = o
            ex = str(row.get("exchange_order_id") or "")
        else:
            row = led.get(ex) or (led.get("c:" + coid)
                                  if coid else None) or {}
        try:
            price = float(d(str(o.get("price") or 0)))
            amount = float(d(str(o.get("amount") or 0)))
            matched = float(d(str(o.get("matched_amount") or 0)))
        except Exception:
            return None
        # a missing/garbage unmatched_amount must fall back to
        # amount − matched instead of dropping the whole order
        try:
            left = max(float(d(str(o.get("unmatched_amount")))), 0.0)
        except Exception:
            left = max(amount - matched, 0.0)
        pct = (matched / amount * 100.0) if amount > 0 else 0.0
        created = o.get("created_at") or row.get("created_at") or ""
        age = TradingEngine._order_age_seconds(created)
        live_px = prices.get(market)
        base, quote = split_symbol(market)
        return {
            "order_id": str(o.get("order_id") or row.get("order_id") or ""),
            "exchange_order_id": ex,
            "client_order_id": coid,
            "decision_id": row.get("decision_id") or "",
            "market": market,
            "base": base,
            "quote": quote,
            "side": side,
            "order_type": str(o.get("order_type") or row.get("order_type")
                              or "LIMIT").upper(),
            "price": price,
            "amount": amount,
            "matched_amount": matched,
            "unmatched_amount": left,
            "filled_pct": round(pct, 4),
            "status": str(o.get("status") or row.get("status")
                          or "OPEN").upper(),
            "created_at": created,
            "age_s": round(age, 1) if age is not None and age >= 0 else None,
            "live_price": live_px,
            "value_quote": price * amount,
            "remaining_value_quote": price * left,
            "origin": "bot" if coid.startswith("bot-") else "manual",
            "source": source,
        }

    def reconcile_real_orders(self) -> int:
        """Pull open real orders into the order book (SS44 reconciliation)."""
        if not (self._real_attached and self.cfg.exchange.live_trading):
            return 0
        try:
            opens = self.exchange.open_orders()
        except Exception:
            return 0
        known = {o["order_id"] for o in self.storage.orders(limit=500)
                 if o.get("exchange_order_id")}
        added = 0
        for o in opens:
            if o["order_id"] in known:
                continue
            self.storage.save_order({
                "order_id": "exch_" + o["order_id"],
                "client_order_id": o.get("client_order_id") or "",
                "decision_id": "",
                "market": canonical_symbol(o["market"]),
                "side": o["side"],
                "order_type": o.get("order_type", "LIMIT"),
                "price": str(o.get("price") or 0),
                "amount": str(o.get("amount") or 0),
                "matched_amount": str(o.get("matched_amount") or 0),
                "unmatched_amount": str(o.get("unmatched_amount") or 0),
                "status": o.get("status", "OPEN"),
                "exchange_order_id": o["order_id"],
                "created_at": o.get("created_at", utc_now_iso()),
                "updated_at": utc_now_iso(),
            })
            added += 1
        return added

    def backfill_real_fills(self) -> int:
        """Turn our own live orders into ledger fills once the exchange
        reports them matched (SS44). Coins already sit in the wallet, so
        this records the historical BUY (avg entry + exit plan) instead of
        double-crediting balances — the wallet sync already owns balances.
        """
        if not (self._real_attached and self.cfg.exchange.live_trading):
            return 0
        added = 0
        try:
            my_orders = [o for o in self.storage.orders(limit=200)
                         if o.get("exchange_order_id")]
        except Exception:
            return 0
        # executions whose real order reached a terminal state must never
        # stay SUBMITTED — that blocks future buys of the market with
        # BUY_ORDER_ALREADY_ACTIVE forever (the risk cooldown reads these
        # rows). Sync them even when the ORDER row is already terminal.
        exec_by_exch = {}
        try:
            for ex in self.storage.executions_recent(200):
                if ex.get("exchange_order_id"):
                    exec_by_exch[str(ex["exchange_order_id"])] = ex
        except Exception:
            exec_by_exch = {}
        terminal = ("FILLED", "CANCELLED", "CANCELED", "REJECTED")
        for ex in exec_by_exch.values():
            ex_st = (ex.get("status") or "").upper()
            if ex_st in ("SUBMITTED", "SUBMITTING", "READY", "OPEN"):
                try:
                    doc = self.exchange.order_status(
                        str(ex["exchange_order_id"]),
                        ex.get("market") or "")
                    new_st = (doc.get("status") or "").upper()
                except Exception:
                    continue
                if new_st in terminal and new_st != ex_st:
                    ex["status"] = new_st
                    self.storage.save_execution(ex)

        for o in my_orders:
            st = (o.get("status") or "").upper()
            if st in ("FILLED", "CANCELLED", "CANCELED", "REJECTED"):
                continue
            try:
                st_doc = self.exchange.order_status(
                    o["exchange_order_id"], o.get("market") or "")
            except Exception:
                continue
            matched = d(str(st_doc.get("matched_amount") or 0))
            # canonical market key: the exchange echoes full coin names in
            # several endpoints; fills/plans/UI must share ONE symbol
            market = canonical_symbol(o["market"])
            already = [f for f in self.storage.fills(market, limit=100)
                       if f.get("order_id") == o["order_id"]]
            if matched > 0 and not already:
                price = d(str(st_doc.get("average_price") or 0)) \
                    or d(str(o.get("price") or 0))
                # fee estimation: the exchange statement reconciles later;
                # recording 0 here hid ~0.25% of every round-trip from the
                # سود کل ledger (460+ live fills with fee_amount=0) and the
                # bot «made» money it never made. Estimate with the config
                # taker rate; the statement view stays authoritative.
                fee_rate = d(str(self.cfg.fee.taker_rate))
                fee = ((price * matched) * fee_rate).quantize(
                    Decimal("0.00000001"))
                fill = {
                    "trade_id": "live_" + str(o["exchange_order_id"]),
                    "order_id": o["order_id"],
                    "decision_id": o.get("decision_id") or "",
                    "market": market,
                    "side": o.get("side") or "BUY",
                    "price": str(price),
                    "amount": str(matched),
                    "gross_value": str(price * matched),
                    "fee_amount": str(fee),
                    "fee_asset": "USDT" if market.endswith("USDT")
                                 else "IRT",
                    "timestamp": utc_now_iso(),
                }
                if self.storage.save_fill(fill):
                    added += 1
                    # SS100d: a freshly recorded BUY fill (manual or auto)
                    # ALWAYS re-arms the automatic sell-stop AND sell
                    # take-profit below/above the actual fill price — under
                    # the canonical key the holdings UI displays
                    if (fill["side"] or "").upper() == "BUY":
                        try:
                            self._auto_stop_for_buy(market, price)
                        except Exception:
                            pass
            # keep local order status in step with the exchange
            new_status = (st_doc.get("status") or "").upper()
            if new_status and new_status != st:
                self.storage.update_order_status(
                    o["order_id"], new_status, str(matched))
            # keep the EXECUTION row in step too: a cancelled/filled real
            # order must not leave a SUBMITTED execution that blocks all
            # future buys of the market with BUY_ORDER_ALREADY_ACTIVE
            for ex in self.storage.executions_recent(200):
                if ex.get("exchange_order_id") != o["exchange_order_id"]:
                    continue
                ex_st = (ex.get("status") or "").upper()
                if ex_st == new_status:
                    continue
                if new_status in ("FILLED", "CANCELLED", "CANCELED"):
                    ex["status"] = new_status
                    self.storage.save_execution(ex)
                break
        return added

    def cancel_bot_orders(self) -> dict:
        """Cancel open REAL orders the bot itself placed (clientOrderId
        prefixed "bot-"), freeing the locked quote balance. Manual/user
        orders from the same account are never touched."""
        if not (self._real_attached and self.cfg.exchange.live_trading):
            return {"ok": False, "error": "حساب واقعی وصل نیست"}
        cancelled, failed = [], []
        try:
            my_orders = [o for o in self.storage.orders(limit=200)
                         if (o.get("client_order_id") or "").startswith("bot-")
                         and o.get("exchange_order_id")]
            opens = {o["order_id"]: o for o in self.exchange.open_orders()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        for o in my_orders:
            exch_id = o["exchange_order_id"]
            if exch_id not in opens:
                continue
            try:
                self.exchange.cancel_order(exch_id)
                self.storage.update_order_status(o["order_id"], "CANCELLED")
                cancelled.append({"order_id": exch_id,
                                  "market": o["market"],
                                  "side": o["side"]})
            except Exception as exc:
                failed.append({"order_id": exch_id, "error": str(exc)})
        self.events.publish("BOT_ORDERS_CANCELLED", "engine", "",
                            {"count": len(cancelled), "failed": failed})
        return {"ok": True, "cancelled": cancelled, "failed": failed}

    def account_logout(self) -> dict:
        self.nobitex_session.wipe()
        self.events.publish("LOGOUT", "web", "", {})
        # fall back to the simulator so the engine keeps working; real
        # trading must never continue without a real session
        self.cfg.exchange.live_trading = False
        try:
            if isinstance(self.exchange, NobitexAPI):
                self._set_exchange(DemoMarket(self.cfg.exchange,
                                              events=self.events))
                self.cfg.exchange.mode = "demo"
        except Exception:
            pass
        return {"ok": True}

    def exchange_status(self) -> dict:
        """Live portfolio straight from the exchange wallets (SS21)."""
        try:
            return self.exchange.portfolio()
        except Exception as exc:
            return {"error": str(exc), "assets": [], "timestamp": utc_now_iso()}

    def recent_events(self, limit: int = 100) -> list:
        return self.events.recent(limit)

    def history(self, kind: str, limit: int = 100) -> list:
        if kind == "decisions":
            return self.storage.decisions_recent(limit)
        if kind == "risk":
            return self.storage.risk_recent(limit)
        if kind == "executions":
            return self.storage.executions_recent(limit)
        if kind == "orders":
            return self.storage.orders(limit=limit)
        if kind == "fills":
            return self.storage.fills(limit=limit)
        return []

