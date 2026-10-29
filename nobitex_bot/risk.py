"""Risk engine — contract sections 33, 34, 35, 41, 42, 43.

Every AI decision must pass here before execution. The AI can never
override risk (SS34). Fail-closed: uncertain -> reject (SS101/102).
"""
from __future__ import annotations

from decimal import Decimal

from .config import Config
from .enums import ExecutionStatus
from .events import EventBus
from .storage import Storage
from .utils import d, new_id, parse_iso, utc_now_iso

# Fallback Toman-per-USDT when USDTIRT is missing from the fresh prices
# (e.g. it is not part of the trading universe that cycle). An IRT-quoted
# BUY decision whose quote cannot be converted to USDT would otherwise
# collide with a USDT-denominated equity and read as ~150x over-exposure,
# silently blocking every buy while Toman cash sits idle.
FALLBACK_USDTIRT = Decimal("105000")

class RiskEngine:
    def __init__(self, cfg: Config, storage: Storage, events: EventBus):
        self.cfg = cfg
        self.storage = storage
        self.events = events

    def evaluate(self, decision: dict, market_snap: dict,
                 portfolio_snapshot: dict, fresh_prices: dict,
                 topup: tuple | None = None,
                 size_cap: Decimal | None = None,
                 manual: bool = False) -> dict:
        """Evaluate one decision (SS33/34).

        topup: optional (symbol, amount_quote) for a buy into an already-held
        coin (the holdings-box manual buy, the ladder's per-rung top-up).
        A top-up buys MORE of an already-held coin, so:
        - MAX_OPEN_POSITIONS_REACHED does not apply (the position exists);
        - `amount_quote` acts as a size floor: the approved order is at least
          the requested amount, still capped by cash and fraction limits.
          It is in the market's QUOTE unit (Toman for *IRT markets, USDT for
          *USDT ones) — the same unit the SS35 sizing below works in.
        size_cap: optional ceiling in COINS on the risk-based size. The
        ladder uses floor = cap = one rung's budget so each rung adds
        exactly the planned amount (the exchange minimum order can still
        raise it, and cash/fraction caps can still lower it).
        manual: a HUMAN click on the dashboard (خرید/فروش دستی). Manual
        decisions take priority over the bot's own soft gates (quality,
        liquidity, cooldown, open-position/exposure/risk caps, drawdown,
        revalidation) so a click never dies with «رد توسط موتور ریسک».
        Hard reality is still enforced — a balance that cannot pay, a
        missing price and the exchange's minimum order still reject — and
        every skipped gate is recorded in `rd["overridden"]`.
        """
        now_iso = utc_now_iso()
        rd = {
            "risk_decision_id": new_id("risk"),
            "decision_id": decision.get("decision_id", ""),
            "status": "APPROVED",
            "checks": {},
            "approved_position_size": None,
            "approved_max_loss": None,
            "approved_entry_range": None,
            "rejection_reasons": [],
            "created_at": now_iso,
        }
        cfg = self.cfg.risk
        action = decision.get("action")
        checks = rd["checks"]
        manual = bool(manual)
        overridden: list = []
        rd["manual_override"] = manual

        def fail(code: str) -> None:
            rd["status"] = "REJECTED"
            if code not in rd["rejection_reasons"]:
                rd["rejection_reasons"].append(code)

        def gate(name: str, ok: bool, code: str) -> None:
            """One soft gate: a manual click OVERRIDES it (and says so),
            an automatic decision is rejected by it."""
            checks[name] = ok or manual
            if ok:
                return
            if manual:
                if code not in overridden:
                    overridden.append(code)
            else:
                fail(code)

        # ---- HOLD/AVOID need no execution sizing, just freshness/quality
        if action not in ("BUY", "SELL"):
            checks["data_quality"] = True
            checks["liquidity"] = True
            rd["checks"] = checks
            return rd

        sym = decision.get("symbol")
        snap = market_snap or {}
        is_exit = bool(decision.get("exit"))
        dq = (snap.get("data_quality") or {}).get("score", 0)
        liq_status = (snap.get("liquidity") or {}).get("status", "UNKNOWN")

        # SS20/SS93 quality gate (closing risk must never be blocked by it)
        gate("data_quality",
             is_exit or dq >= self.cfg.quality.min_trade_quality,
             "DATA_QUALITY_BELOW_TRADE_THRESHOLD")

        # SS15: CRITICAL/UNKNOWN liquidity -> not tradable by default
        # (exits close exposure, so degraded liquidity must not trap them)
        gate("liquidity",
             is_exit or liq_status in ("EXCELLENT", "GOOD", "MEDIUM"),
             "LIQUIDITY_NOT_TRADABLE")

        # SS42 decision freshness
        try:
            exp = parse_iso(decision["expires_at"])
            fresh = parse_iso(now_iso) <= exp
        except Exception:
            fresh = False
        checks["decision_freshness"] = fresh
        if not fresh:
            fail("DECISION_EXPIRED")

        # SS28 net expected return (exit decisions bypass the edge test)
        cost = (decision.get("cost") or {})
        net = cost.get("expected_net_return") or 0
        checks["fee_check"] = True
        checks["slippage_check"] = True
        gate("net_expected_return",
             bool(decision.get("exit")) or
             net >= cfg.min_net_expected_return,
             "INSUFFICIENT_NET_EXPECTED_RETURN")

        # ---- portfolio limits (SS22)
        risk_doc = portfolio_snapshot.get("risk", {})
        equity = d(str(risk_doc.get("total_equity", 0)))
        exposure_ratio = d(str(risk_doc.get("exposure_ratio", 0)))
        cash_ratio = d(str(risk_doc.get("cash_ratio", 0)))
        drawdown = d(str(risk_doc.get("drawdown", {}).get("current", 0)))
        price = d(str(fresh_prices.get(sym) or snap.get("last_price") or 0))
        if price <= 0:
            fail("PRICE_UNAVAILABLE")
            rd["checks"] = checks
            self._finalize(rd, decision)
            return rd

        topup_floor = None
        if topup and topup[0] == sym and topup[1] > 0 and \
                not decision.get("exit"):
            # the requested amount arrives in the market's QUOTE unit
            # (Toman for *IRT markets, USDT for *USDT ones) while SS35
            # sizes the order in COINS — convert with the live price. The
            # old code compared the money amount against a coin amount, so
            # a manual/ladder top-up's requested size never really applied.
            try:
                floor_price = d(str(fresh_prices.get(sym) or
                                    snap.get("last_price") or 0))
            except Exception:
                floor_price = Decimal(0)
            if floor_price > 0:
                topup_floor = d(str(topup[1])) / floor_price

        # quote-consistent equity + exchange minimum order value. IRT-market
        # prices are in Toman (adapter-converted) so equity converts at the
        # live USDTIRT (Toman per USDT) for those markets.
        equity_quote = equity
        min_notional_quote = d(str(getattr(self.cfg.exchange,
                                           "min_order_usdt", 0.44)))
        if not sym.endswith("USDT"):
            usdtirt = d(str(fresh_prices.get("USDTIRT") or 0))
            if usdtirt <= 0:
                usdtirt = FALLBACK_USDTIRT
            equity_quote = equity * usdtirt
            min_notional_quote = min_notional_quote * usdtirt
        # small-account bootstrap: the exchange's absolute minimum order
        # value can exceed the configured fraction caps on tiny accounts
        # (min order ~100k Toman vs a 185k-Toman account). Relax the caps
        # just enough for ONE minimum-size order, never above ~95% equity.
        frac_floor = (min_notional_quote / equity_quote *
                      Decimal("1.02")) if equity_quote > 0 else Decimal(0)
        pos_frac_eff = max(d(str(cfg.max_position_fraction)),
                           min(Decimal("0.95"), frac_floor))
        expo_eff = max(d(str(cfg.max_total_exposure)),
                       min(Decimal("0.98"), exposure_ratio + frac_floor))

        # exposure limit (closing a position never adds exposure)
        if is_exit:
            checks["exposure_limit"] = True
        else:
            checks["exposure_limit"] = True
            # exposure_limit is evaluated AFTER SS35 sizing (below), where
            # the real order notional (cash/limit-capped) is known — the
            # AI's raw wish is unsellable on a small account and would
            # freeze all trading with idle cash. USDTIRT missing from the
            # fresh prices must not block every IRT buy either — the
            # fallback rate below keeps Toman cash deployable.

        # drawdown limiter protects entries; closing risk is always allowed
        gate("drawdown_limit",
             is_exit or drawdown < d(str(cfg.max_drawdown)),
             "DRAWDOWN_LIMIT_BREACHED")

        usdtirt = d(str(fresh_prices.get("USDTIRT") or 0))
        if usdtirt <= 0:
            usdtirt = FALLBACK_USDTIRT

        # SS35 position sizing, recomputed independently of AI
        sl = decision.get("stop_loss") or {}
        entry = d(str((decision.get("entry") or {}).get("price", 0)))
        stop = d(str(sl.get("price", 0)))
        stop_dist = abs(entry - stop)

        approved_amount = None
        approved_loss = None
        if is_exit:
            # SS36/37 exit: close what the plan asked, capped by holdings.
            # Loss-based sizing and notional limits do not apply to closing.
            checks["max_risk"] = True
            checks["position_limit"] = True
            checks["exposure_limit"] = True
            if rd["status"] == "APPROVED":
                pos = self.storage.position(sym)
                pos_amount = d(pos["amount"]) if pos else Decimal(0)
                requested = d(str(decision.get("position_size", {}).get(
                    "amount", 0)))
                amount = min(requested, pos_amount)
                if amount <= 0:
                    fail("NO_POSITION_TO_SELL")
                else:
                    approved_amount = amount
                    approved_loss = Decimal(0)
        else:
            if entry <= 0 or stop <= 0 or stop_dist <= 0:
                fail("INVALID_STOP_LOSS")
            else:
                # validate stop direction
                if decision.get("action") == "BUY" and stop >= entry:
                    fail("INVALID_STOP_LOSS")
                if decision.get("action") == "SELL" and stop <= entry:
                    fail("INVALID_STOP_LOSS")

            checks["max_risk"] = True
            checks["position_limit"] = True
            checks["exposure_limit"] = True
            if rd["status"] == "APPROVED":
                max_loss = equity_quote * d(str(cfg.max_risk_per_trade))
                amount = max_loss / stop_dist
                # small-account bootstrap: when the exchange's minimum order
                # is larger than the loss-based size, size UP to one minimum
                # order — but never beyond the fraction cap (pos_frac_eff is
                # already relaxed for tiny accounts) and never beyond cash.
                min_amount_quote = min_notional_quote / price
                if getattr(self.cfg.risk, "risk_mode", False):
                    # خردهٔ فروش‌نشدنی نساز: خرید کمی بزرگ‌تر از حداقل
                    # صرافی تا بعد از افت (تا ‎-3٪) هنوز قابل فروش بماند
                    min_amount_quote *= d(str(getattr(
                        self.cfg.risk, "risk_mode_size_floor_mult", 1.2)
                        or 1.0))
                # requested ceiling first (the ladder asks for exactly one
                # rung's budget): the exchange minimum below may still
                # raise it, the fraction/cash caps may still lower it
                if size_cap is not None:
                    try:
                        cap_d = d(str(size_cap))
                    except Exception:
                        cap_d = Decimal(0)
                    if cap_d > 0 and amount > cap_d:
                        amount = cap_d
                if topup_floor and topup_floor > min_amount_quote:
                    min_amount_quote = topup_floor
                if amount < min_amount_quote:
                    amount = min_amount_quote
                # liquidity-aware cap (SS35) — bootstrap-aware for tiny
                # accounts whose minimum order exceeds the default fraction.
                # A MANUAL click keeps the size the user typed (only cash
                # and the exchange minimum bound it); the fraction cap is
                # recorded as overridden instead of silently shrinking it.
                max_frac_amount = (equity_quote * pos_frac_eff) / price
                if amount > max_frac_amount and not manual:
                    amount = max_frac_amount
                # cash constraint: BUY needs the market's quote-asset
                # balance (USDT markets pay USDT, IRT markets pay IRT)
                if decision.get("action") == "BUY":
                    quote_asset = "USDT" if sym.endswith("USDT") else "IRT"
                    assets = {a.get("asset"): d(str(a.get("available", "0")))
                              for a in (portfolio_snapshot.get("assets") or [])}
                    cash = assets.get(quote_asset, Decimal(0))
                    # safety guard: a USDT-valued IRT balance is a stale
                    # valuation artifact, not real cash (real Toman is
                    # ~100k per USDT, not ~2.3). Keep the wallet's true
                    # Toman so sizing/deposits are never silently shrunk.
                    if quote_asset == "IRT" and 0 < cash < Decimal("1000"):
                        cash = d(str((portfolio_snapshot.get("account") or
                                      {}).get("irt_available", 0)))
                    per_unit = price * (Decimal("1") + d(str(self.cfg.fee.taker_rate))
                                        + d(str(cfg.max_slippage)))
                    affordable = (cash * Decimal("0.995")) / per_unit
                    if amount > affordable:
                        amount = affordable
                    # honest dust check: if even the whole quote balance
                    # cannot reach the exchange minimum order value, reject
                    # with the real reason instead of approving a dust order
                    min_order_usdt = d(str(getattr(self.cfg.exchange,
                                                   "min_order_usdt", 0.087)))
                    if affordable * price < min_order_usdt:
                        fail("BELOW_MIN_NOTIONAL")
                # SELL needs base balance (SS77)
                if decision.get("action") == "SELL":
                    pos = self.storage.position(sym)
                    pos_amount = d(pos["amount"]) if pos else Decimal(0)
                    if pos_amount <= 0:
                        fail("NO_POSITION_TO_SELL")
                    elif amount > pos_amount:
                        amount = pos_amount
                if amount <= 0:
                    fail("POSITION_SIZE_TOO_SMALL")
                else:
                    notional = amount * price
                    # per-order notional floor (SS27): the exchange's
                    # minimum order value in this market's quote asset
                    if notional < min_notional_quote:
                        fail("BELOW_MIN_NOTIONAL")
                    else:
                        approved_amount = amount
                        approved_loss = amount * stop_dist
                        # bootstrap order: when the size was raised to the
                        # exchange minimum, the loss budget is raised with it
                        # (capped by the fraction — the position itself is
                        # capped, the LOSS % is the price of doing business
                        # on a small account)
                        loss_budget = max_loss
                        if min_amount_quote > max_loss / stop_dist:
                            loss_budget = (max_frac_amount * stop_dist
                                           if amount >= max_frac_amount
                                           else min_amount_quote * stop_dist)
                        gate("max_risk",
                             approved_loss <= loss_budget * Decimal("1.05"),
                             "MAX_RISK_EXCEEDED")
                        gate("position_limit",
                             (notional / equity_quote if equity_quote > 0
                              else Decimal(1)) <= pos_frac_eff,
                             "POSITION_LIMIT_EXCEEDED")

                # SS22 exposure limit — evaluated on the FINAL sized order
                # notional (that is the exposure this order actually adds),
                # converted to USDT for unit consistency with equity. When
                # the sized order would overshoot the cap, DOWN-SIZE it to
                # the remaining headroom instead of rejecting — otherwise a
                # portfolio whose positions already sit near the cap can
                # never deploy its idle cash again. Reject only when even
                # one exchange-minimum order does not fit the headroom.
                if (rd["status"] == "APPROVED" and approved_amount
                        and decision.get("action") == "BUY"):
                    headroom_usdt = max(Decimal("0"), expo_eff
                                        - exposure_ratio) * equity
                    headroom_quote = headroom_usdt * (
                        Decimal("1") if sym.endswith("USDT") else usdtirt)
                    headroom_amount = (headroom_quote / price
                                       if price > 0 else Decimal("0"))
                    min_units = (min_notional_quote / price
                                 if price > 0 else Decimal("0"))
                    if headroom_amount >= min_units:
                        if approved_amount > headroom_amount and not manual:
                            approved_amount = headroom_amount
                            if approved_loss is not None:
                                approved_loss = approved_amount * stop_dist
                        checks["exposure_limit"] = True
                    elif manual:
                        # the user's click wins; say that the exposure cap
                        # was overridden so the audit trail stays honest
                        checks["exposure_limit"] = True
                        if "EXCESSIVE_PORTFOLIO_EXPOSURE" not in overridden:
                            overridden.append("EXCESSIVE_PORTFOLIO_EXPOSURE")
                    else:
                        checks["exposure_limit"] = False
                        fail("EXCESSIVE_PORTFOLIO_EXPOSURE")

        # SS41 idempotency: same decision already executed?
        # A manual click is the user's explicit intent: the duplicate/cooldown
        # /stop-lock gates protect AUTOMATIC trading from itself and must not
        # swallow a human order (they also blocked the second click of the
        # same day with BUY_COOLDOWN_ACTIVE).
        checks["duplicate_order_check"] = True
        recent_execs = self.storage.executions_recent(100)
        if manual:
            recent_execs = []
        for ex in recent_execs:
            if (ex.get("decision_id") == decision.get("decision_id") and
                    ex.get("status") in ("READY", "SUBMITTING", "SUBMITTED",
                                         "OPEN", "PARTIALLY_FILLED", "FILLED")):
                checks["duplicate_order_check"] = False
                fail("DO_NOT_DUPLICATE")
                break
        # per-market BUY cooldown: each cycle mints a fresh decision_id, so
        # idempotency alone cannot stop three near-identical limit orders
        # from firing in 20 seconds when the market keeps ranking #1. A
        # market that already has a live BUY order (or got one within the
        # cooldown window) is not re-bought until it fills or is cancelled.
        if (decision.get("action") == "BUY" and
                rd["status"] == "APPROVED"):
            # حالت ریسک: فاصله خرید تکراری کوتاه‌تر (چرخش سریع سرمایه)
            if getattr(self.cfg.risk, "risk_mode", False):
                cooldown_s = float(getattr(
                    self.cfg.risk, "risk_mode_buy_cooldown_s", 300) or 0)
            else:
                cooldown_s = float(getattr(self.cfg.risk, "buy_cooldown_s",
                                           900) or 0)
            now_dt = parse_iso(now_iso)
            for ex in recent_execs:
                if ex.get("market") != decision.get("symbol"):
                    continue
                if (ex.get("side") or "").upper() != "BUY":
                    continue
                st = (ex.get("status") or "").upper()
                if st in ("OPEN", "SUBMITTED", "SUBMITTING", "READY",
                          "PARTIALLY_FILLED"):
                    checks["duplicate_order_check"] = False
                    fail("BUY_ORDER_ALREADY_ACTIVE")
                    break
                if cooldown_s > 0 and st in ("FILLED", "FAILED"):
                    # FAILED هم مشمول cooldown است: خطای سمت صرافی
                    # (مثل OverValueOrder روی بازارهای کوچک) نباید هر
                    # چرخه تکرار شود — همان فاصله، فقط یک تلاش
                    ts = ex.get("created_at") or ""
                    try:
                        age = (now_dt - parse_iso(ts)).total_seconds() \
                            if ts and now_dt else 0
                    except Exception:
                        age = 0
                    if 0 <= age < cooldown_s:
                        checks["duplicate_order_check"] = False
                        fail("BUY_COOLDOWN_ACTIVE")
                        break
            # stop-exit lock-out: a market just exited by its حد ضرر must
            # NOT be instantly re-bought — re-entering the same loser
            # doubles the loss. Blocked until the price reaches the NEXT
            # strike (the hadd the ratchet had armed) or the rotate
            # cooldown elapses, whichever comes first.
            if (rd["status"] == "APPROVED" and
                    decision.get("reason_codes") and
                    "STOP_EXIT_ROTATION" not in decision["reason_codes"]):
                try:
                    lock_plans = self.storage.kv_get("exit_plans", {}) or {}
                    lk_pl = lock_plans.get(decision.get("symbol")) or {}
                    if lk_pl.get("stop_lock_until_ts"):
                        lk_until = parse_iso(str(
                            lk_pl["stop_lock_until_ts"]))
                        # the exchange-side level the ratchet had armed when
                        # the stop fired (`stop_lock_hadd`) is the real
                        # re-entry level; `locked_stop` is only the legacy
                        # fallback (older plans never wrote the former)
                        lk_hadd = d(str(lk_pl.get("stop_lock_hadd")
                                        or lk_pl.get("locked_stop")
                                        or 0))
                        reached = (lk_hadd > 0 and price > 0 and
                                   price >= lk_hadd)
                        if not reached and now_dt and \
                                now_dt < lk_until:
                            checks["stop_lock"] = False
                            fail("STOP_EXIT_LOCKOUT")
                except Exception:
                    pass

        # SS43 revalidation: price moved too far since decision?
        checks["revalidation_price"] = True
        try:
            dec_price = d(str((decision.get("entry") or {}).get("price", 0)))
            if dec_price > 0 and not manual:
                move = abs(price - dec_price) / dec_price
                if move > d(str(cfg.revalidate_price_tolerance)):
                    checks["revalidation_price"] = False
                    fail("PRICE_MOVED_TOO_FAR")
        except Exception:
            pass

        # open positions cap — counts only BOT-OPENED trades (real avg
        # entry). Pre-held wallet coins synced with entry 0 are the user's
        # existing holdings, not bot positions, and must not exhaust the cap.
        open_pos = [p for p in self.storage.positions()
                    if d(p["amount"]) > 0 and d(p["avg_entry"]) > 0]
        if (decision.get("action") == "BUY" and not manual and
                len(open_pos) >= cfg.max_open_positions and
                topup_floor is None and not getattr(
                    self.cfg.risk, "allow_topup", False)):
            fail("MAX_OPEN_POSITIONS_REACHED")


        if rd["status"] == "APPROVED" and approved_amount:
            # 12 decimals, not 8: rounding a small-but-real exit size to 8
            # decimals turned dust positions into 0.0 and the SELL was
            # skipped with NO_APPROVED_SIZE (the click did nothing).
            rd["approved_position_size"] = float(
                round(approved_amount, 12) if is_exit else
                round(approved_amount, 8))
            rd["approved_max_loss"] = float(round(approved_loss, 4))
            rd["approved_entry_range"] = {
                "min": float(entry * Decimal("0.998")),
                "max": float(entry * Decimal("1.002")),
            }
        rd["checks"] = checks
        if overridden:
            rd["overridden"] = overridden
            self.events.publish("MANUAL_OVERRIDE", "risk_engine",
                                decision.get("cycle_id", ""),
                                {"decision_id": rd["decision_id"],
                                 "overridden": overridden})
        self._finalize(rd, decision)
        return rd

    def _finalize(self, rd: dict, decision: dict) -> None:
        if rd["status"] == "APPROVED":
            self.events.publish("RISK_APPROVED", "risk_engine",
                                decision.get("cycle_id", ""),
                                {"decision_id": rd["decision_id"]})
        else:
            self.events.publish("RISK_REJECTED", "risk_engine",
                                decision.get("cycle_id", ""),
                                {"decision_id": rd["decision_id"],
                                 "reasons": rd["rejection_reasons"]})
