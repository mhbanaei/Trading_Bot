"""Execution engine — contract sections 38, 39, 41, 43, 44, 25.

Paper broker: submits limit orders, simulates fills against the live
market price with slippage, applies fills to the paper portfolio,
records fills/orders/executions with full audit lineage (SS83).
"""
from __future__ import annotations

import threading
from decimal import Decimal

from .config import Config
from .diagnostics import (classify_failure, record_error,
                          record_failed_trade)
from .enums import ExecutionStatus, OrderStatus
from .events import EventBus
from .nobitex_api import NobitexAPI, NobitexError
from .portfolio import PaperPortfolio
from .storage import Storage
from .utils import canonical_symbol, d, new_id, utc_now_iso


class ExecutionEngine:
    def __init__(self, cfg: Config, storage: Storage, events: EventBus,
                 portfolio: PaperPortfolio, exchange=None):
        self.cfg = cfg
        self.storage = storage
        self.events = events
        self.portfolio = portfolio
        self.exchange = exchange
        self._lock = threading.Lock()

    def _portfolio_state(self) -> dict:
        try:
            return {"balances": {k: str(v) for k, v in
                                  dict(getattr(self.portfolio, "balances",
                                               {}) or {}).items()}}
        except Exception:
            return {}

    def _note_failed_attempt(self, decision: dict, risk_decision: dict,
                             side: str, *, reason: str, category: str = "",
                             price=None, amount=None, execution=None,
                             status: str = "", outcome: str = "rejected",
                             exchange_response=None, executed_amount=None,
                             fees=None) -> None:
        """Record one unsuccessful execution attempt (never a normal loss)."""
        try:
            record_failed_trade(
                symbol=decision.get("symbol", ""),
                intended_action=side,
                failure_category=category or classify_failure(reason),
                failure_reason=reason,
                execution_mode="live" if self._live() else "paper",
                strategy=",".join(map(str, decision.get("reason_codes")
                                      or []))[:200],
                ai_recommendation=decision.get("action"),
                decision_id=decision.get("decision_id", ""),
                risk_decision_id=risk_decision.get("risk_decision_id", ""),
                order_id=(execution or {}).get("order_id", ""),
                order_status=status or (execution or {}).get("status", ""),
                exchange_response=exchange_response,
                expected_price=price,
                actual_price=(execution or {}).get("fill_price"),
                intended_amount=amount,
                executed_amount=executed_amount,
                fees=fees,
                portfolio_before=self._portfolio_state(),
                outcome=outcome)
        except Exception:
            pass

    def _live(self) -> bool:
        """Real orders only when the real adapter is attached AND the user
        explicitly enabled live trading (SS101 default: paper)."""
        return (isinstance(self.exchange, NobitexAPI)
                and bool(getattr(self.cfg.exchange, "live_trading", False)))

    def _crossing_sell_price(self, market: str, last: Decimal,
                             amount: Decimal | None = None,
                             aggression: float = 0.0) -> Decimal:
        """Price a limit SELL below the bids so the order actually fills.

        Two user reports drive this: «فروش می‌رود در لیست فروش‌ها ولی
        فروخته نمی‌شود». A SELL resting ABOVE the best bid never matches,
        and a SELL at the best bid only fills the top level — a position
        larger than that level rests forever.

        So: walk the bid side of the book and take the price at which the
        WHOLE amount fits (market-impact price). When the visible bids are
        thinner than the order, use the lowest visible bid and lean a
        little further down. The ticker's `best_bid` is the fallback and
        the last price is the final fallback.
        """
        want = d(str(amount)) if amount is not None else Decimal("0")
        base = Decimal("0")
        try:
            book = self.exchange.orderbook(market, depth=20) or {}
            bids = [b for b in (book.get("bids") or []) if b]
            bids = sorted(
                ({**b, "price": d(str(b.get("price"))),
                  "amount": d(str(b.get("amount") or 0))}
                 if isinstance(b, dict) else
                 {"price": d(str(b)), "amount": Decimal("0")}
                 for b in bids),
                key=lambda x: -x["price"])
            if bids and want <= 0:
                base = bids[0]["price"]           # unknown size: best bid
            elif bids:
                filled = Decimal("0")
                for lvl in bids:
                    filled += lvl["amount"]
                    base = lvl["price"]           # deepest needed level
                    if filled >= want:
                        break
                if filled < want:
                    # book thinner than the order: lean below the last bid
                    base = bids[-1]["price"]
        except Exception:
            base = Decimal("0")
        if base <= 0:
            try:
                tick = self.exchange.ticker(market) or {}
                base = d(str(tick.get("best_bid") or 0))
            except Exception:
                base = Decimal("0")
        if base <= 0:
            base = d(str(last or 0))
        if base <= 0:
            return last
        slip = min(d("0.003"), d(str(getattr(self.cfg.risk,
                                            "max_slippage", 0) or 0)))
        if slip <= 0:
            slip = d("0.001")      # always cross, never rest exactly on it
        if aggression > 0:
            # A RETRY means the first price did not match (stale book, a
            # moved market): cross deeper so the sale completes. A limit
            # SELL below the bid executes at the best bid, so the deeper
            # price never gives the market more than it already asks.
            slip = max(slip, min(d("0.03"), d(str(aggression))))
        px = base * (Decimal("1") - slip)
        return px if px > 0 else base

    def _crossing_buy_price(self, market: str, last: Decimal,
                            amount: Decimal | None = None) -> Decimal:
        """Price a limit BUY above the asks so a manual click fills now.

        The automatic path deliberately rests a BUY at a pullback price
        (the AI's entry). A HUMAN click on «خرید» means «buy it now», so
        the order is priced at the ask level that covers the whole size
        (+ a hair) instead of waiting for the market to come down.
        """
        want = d(str(amount)) if amount is not None else Decimal("0")
        base = Decimal("0")
        try:
            book = self.exchange.orderbook(market, depth=20) or {}
            asks = [a for a in (book.get("asks") or []) if a]
            asks = sorted(
                ({**a, "price": d(str(a.get("price"))),
                  "amount": d(str(a.get("amount") or 0))}
                 if isinstance(a, dict) else
                 {"price": d(str(a)), "amount": Decimal("0")}
                 for a in asks),
                key=lambda x: x["price"])
            if asks and want <= 0:
                base = asks[0]["price"]
            elif asks:
                filled = Decimal("0")
                for lvl in asks:
                    filled += lvl["amount"]
                    base = lvl["price"]
                    if filled >= want:
                        break
                if filled < want:
                    base = asks[-1]["price"]
        except Exception:
            base = Decimal("0")
        if base <= 0:
            try:
                tick = self.exchange.ticker(market) or {}
                base = d(str(tick.get("best_ask") or 0))
            except Exception:
                base = Decimal("0")
        if base <= 0:
            base = d(str(last or 0))
        if base <= 0:
            return last
        slip = min(d("0.003"), d(str(getattr(self.cfg.risk,
                                            "max_slippage", 0) or 0)))
        if slip <= 0:
            slip = d("0.001")
        px = base * (Decimal("1") + slip)
        return px if px > 0 else base

    def _client_order_id(self, cycle_id: str, symbol: str) -> str:
        # SS40: bot_<cycle>_<symbol>_<seq>  — Nobitex REJECTS '_' in
        # clientOrderId (verified live: 'Invalid clientOrderId'), so use
        # hyphens: bot-<cycle>-<symbol>-<seq>
        seq = len(self.storage.orders(limit=10000)) + 1
        return "bot-%s-%s-%03d" % (cycle_id.replace("cycle_", "")[:15],
                                   symbol, seq)

    def execute(self, decision: dict, risk_decision: dict,
                fresh_prices: dict, cycle_id: str,
                manual: bool = False) -> dict:
        """Submit + fill a risk-approved decision. Returns execution record.

        manual=True is a HUMAN click (holdings-box «خرید»): the order is
        priced to cross the book and fill immediately instead of resting
        at the AI's pullback price.
        """
        if risk_decision.get("status") != "APPROVED":
            return {"status": "SKIPPED", "reason": "RISK_NOT_APPROVED"}
        if decision.get("action") not in ("BUY", "SELL"):
            return {"status": "SKIPPED", "reason": "NO_ACTION_REQUIRED"}

        sym = decision["symbol"]
        side = decision["action"]
        price = d(str(fresh_prices.get(sym) or 0))
        if price <= 0:
            # data-quality failure: the decision exists but has no live price
            self._note_failed_attempt(
                decision, risk_decision, side, reason="PRICE_UNAVAILABLE",
                category="DATA_QUALITY",
                amount=risk_decision.get("approved_position_size"))
            record_error(
                "EXECUTION", "WARNING",
                "cannot execute %s %s: no fresh price" % (side, sym),
                module="execution.execute", symbol=sym,
                context={"decision_id": decision.get("decision_id")},
                recovered=True,
                recovery_action="attempt skipped before any order was sent")
            return {"status": "SKIPPED", "reason": "PRICE_UNAVAILABLE"}

        amount = d(str(risk_decision.get("approved_position_size") or 0))
        if amount <= 0:
            self._note_failed_attempt(
                decision, risk_decision, side, reason="NO_APPROVED_SIZE",
                category="STRATEGY", price=price)
            return {"status": "SKIPPED", "reason": "NO_APPROVED_SIZE"}

        # LIVE SELLS MUST CROSS THE BOOK. Sending a SELL limit at the last
        # traded price leaves it resting above the best bid whenever the
        # market ticks down; the stale-order reaper then cancels it and the
        # coin is never sold (user report: «دکمهٔ فروش را می‌زنم، نمی‌فروشد»).
        # Price the sell at the best bid (minus a small buffer) so it
        # matches immediately; the exchange still enforces its own limits.
        if side == "SELL" and self._live():
            price = self._crossing_sell_price(sym, price, amount)
        elif side == "BUY" and manual and self._live():
            price = self._crossing_buy_price(sym, price, amount)

        # paper-broker safety clamp: never promise more than the wallet can
        # cover (price + fee + slippage) — the risk check used the older price
        fee_rate = d(str(self.cfg.fee.taker_rate))
        if side == "BUY":
            quote_asset = "USDT" if sym.endswith("USDT") else "IRT"
            per_unit = price * (Decimal("1") + fee_rate + d(str(self.cfg.risk.max_slippage)))
            affordable = self.portfolio.available(quote_asset) / per_unit
            if amount > affordable:
                amount = affordable
            # minimum order value in the market's quote asset (same
            # conversion as the risk engine): min_order_usdt is USDT;
            # IRT-quoted markets price in RIALS so convert at USDTIRT.
            min_notional = d(str(getattr(self.cfg.exchange,
                                         "min_order_usdt", 0.44)))
            if not sym.endswith("USDT"):
                usdtirt = fresh_prices.get("USDTIRT") or 0
                if usdtirt:
                    min_notional = min_notional * d(str(usdtirt))
            if amount * price < min_notional:
                self._note_failed_attempt(
                    decision, risk_decision, side, reason="INSUFFICIENT_CASH",
                    category="INSUFFICIENT_BALANCE", price=price, amount=amount,
                    outcome="rejected")
                return {"status": "SKIPPED", "reason": "INSUFFICIENT_CASH"}

        now = utc_now_iso()
        execution_id = new_id("exec")
        client_order_id = self._client_order_id(cycle_id, sym)

        execution = {
            "execution_id": execution_id,
            "decision_id": decision["decision_id"],
            "risk_decision_id": risk_decision["risk_decision_id"],
            "market": sym,
            "side": side,
            "order_type": "LIMIT",
            "execution_type": "LIMIT",
            "price": float(price),
            "amount": float(amount),
            "client_order_id": client_order_id,
            "max_slippage": self.cfg.risk.max_slippage,
            "status": ExecutionStatus.SUBMITTING.value,
            "created_at": now,
        }

        order_id = "ord_" + new_id("o")[2:]
        order = {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "decision_id": decision["decision_id"],
            "market": sym,
            "side": side,
            "order_type": "LIMIT",
            "price": str(price),
            "amount": str(amount),
            "matched_amount": "0",
            "unmatched_amount": str(amount),
            "status": OrderStatus.PENDING.value,
            "fee_asset": "USDT" if sym.endswith("USDT") else "IRT",
            "created_at": now,
            "updated_at": now,
        }
        self.storage.save_order(order)
        self.events.publish("ORDER_SUBMITTED", "execution_engine", cycle_id,
                            {"order_id": order_id, "market": sym, "side": side,
                             "price": float(price), "amount": float(amount)})

        # ---- real order path (live trading enabled + real adapter attached)
        if self._live():
            return self._submit_real(execution, order, cycle_id)

        # ---- simulate fill (paper): marketable limit fills at limit price
        # with slippage worse for us, bounded by max_slippage
        slippage = min(d(str(self.cfg.risk.max_slippage)),
                       Decimal("0.0005") + amount * price / Decimal("10000000"))
        fill_price = price * (1 + slippage if side == "BUY"
                              else 1 - slippage)
        gross = fill_price * amount
        fee_rate = d(str(self.cfg.fee.taker_rate))
        fee = gross * fee_rate

        order["status"] = OrderStatus.FILLED.value
        order["matched_amount"] = str(amount)
        order["unmatched_amount"] = "0"
        order["updated_at"] = utc_now_iso()
        self.storage.save_order(order)
        self.events.publish("ORDER_OPENED", "execution_engine", cycle_id,
                            {"order_id": order_id})
        self.events.publish("ORDER_FILLED", "execution_engine", cycle_id,
                            {"order_id": order_id, "price": float(fill_price),
                             "amount": float(amount)})

        fill = {
            "trade_id": new_id("trd"),
            "order_id": order_id,
            "decision_id": decision["decision_id"],
            "market": sym,
            "side": side,
            "price": str(fill_price),
            "amount": str(amount),
            "gross_value": str(gross),
            "fee_amount": str(fee),
            "fee_asset": order["fee_asset"],
            "timestamp": utc_now_iso(),
        }
        dup = self.storage.save_fill(fill)
        if dup:
            try:
                applied = self.portfolio.apply_fill(fill, fee_rate)
            except ValueError as exc:
                # SS101: balance problem -> record failure, no partial state
                self.storage.update_order_status(order_id, "REJECTED")
                self.storage.update_execution_status(
                    execution_id, ExecutionStatus.FAILED.value)
                self.events.error("EXECUTION_ENGINE", "FILL_APPLY_FAILED",
                                  "HIGH", str(exc), cycle_id=cycle_id)
                err_id = record_error(
                    "EXECUTION", "HIGH",
                    "fill could not be applied (order %s): %s" % (order_id, exc),
                    module="execution.execute", symbol=sym, order_id=order_id,
                    cycle_id=cycle_id, exc=exc, recovered=False)
                self._note_failed_attempt(
                    decision, risk_decision, side,
                    reason="FILL_APPLY_FAILED: %s" % exc,
                    category="EXCHANGE_BEHAVIOR", price=price, amount=amount,
                    status="REJECTED", outcome="failed")
                execution["status"] = ExecutionStatus.FAILED.value
                execution["error"] = str(exc)
                execution["error_id"] = err_id
                self.storage.save_execution(execution)
                return execution

            self.events.publish(
                "POSITION_OPENED" if applied["position_amount"] and
                applied["avg_entry"] == d(str(fill["price"])) else
                "POSITION_UPDATED",
                "execution_engine", cycle_id,
                {"market": sym, "amount": float(applied["position_amount"])})
            if applied.get("closed"):
                self.events.publish("POSITION_CLOSED", "execution_engine",
                                    cycle_id, {"market": sym,
                                               "realized_pnl":
                                               float(applied["realized_pnl"])})
        else:
            # SS91 duplicate fill — must not double-apply
            self.events.error("EXECUTION_ENGINE", "DUPLICATE_FILL_SUPPRESSED",
                              "WARNING", "trade_id already applied",
                              cycle_id=cycle_id)

        execution["status"] = ExecutionStatus.FILLED.value
        execution["fill_price"] = float(fill_price)
        execution["fee"] = float(fee)
        self.storage.save_execution(execution)

        # SS36/37: register the exit plan from the decision's stop/TP plan
        try:
            sl = decision.get("stop_loss") or {}
            tps = [t for t in (decision.get("take_profit") or [])
                   if t.get("price") and float(t.get("close_percent",
                       t.get("percentage", 0)) or 0) > 0]
            if sl.get("price") and tps:
                plans = self.storage.kv_get("exit_plans", {})
                if not isinstance(plans, dict):
                    plans = {}
                if side == "BUY":
                    existing = plans.get(sym, {"stage": 0, "tps": []})
                    plans[sym] = {
                        "stop": str(sl["price"]),
                        "stage": existing.get("stage", 0),
                        "tps": [{"price": str(t["price"]),
                                 "close_percent": t.get("close_percent",
                                     t.get("percentage", 0))} for t in tps],
                    }
                    self.storage.kv_set("exit_plans", plans)
                else:
                    # SELL filled: the position shrank/closed — drop the
                    # plan so stale stop/TP levels never linger (the next
                    # BUY re-arms fresh levels from the new fill price)
                    plans.pop(sym, None)
                    self.storage.kv_set("exit_plans", plans)
        except Exception:
            pass  # exit-plan bookkeeping must never break settlement
        return execution

    # ------------------------------------------------------------ live path
    def _submit_real(self, execution: dict, order: dict,
                     cycle_id: str) -> dict:
        """Submit the order to the real exchange; NO paper fill is applied.

        Positions/balances for live mode come from wallet sync (engine) and
        order reconciliation, never from the simulated ledger.
        """
        sym = execution["market"]
        try:
            result = self.exchange.place_limit_order(
                sym, execution["side"], execution["price"],
                execution["amount"], execution["client_order_id"])
        except NobitexError as exc:
            return self._fail_real(execution, order,
                                   "%s: %s" % (exc.code, exc), cycle_id)
        except Exception as exc:
            return self._fail_real(execution, order, str(exc), cycle_id)

        order["exchange_order_id"] = result.get("order_id", "")
        order["status"] = result.get("status", OrderStatus.OPEN.value)
        order["matched_amount"] = str(result.get("matched_amount") or "0")
        order["unmatched_amount"] = str(
            result.get("unmatched_amount") or execution["amount"])
        order["updated_at"] = utc_now_iso()
        # record the real fill in the ledger so trades/positions/PnL display
        # correctly everywhere (fills, portfolio, history)
        applied = self._live_fill_from_order(execution, order, cycle_id)
        if applied:
            # `applied` is the fill RECORD (dict), not a status string: the
            # old assignment stored the whole dict in orders.status and
            # sqlite raised «type 'dict' is not supported», so every live
            # order that matched immediately crashed _submit_real AFTER
            # the exchange had accepted it (the dashboard showed an error
            # and the ledger never recorded the fill).
            order["status"] = applied.get("status") or order["status"]
        self.storage.save_order(order)

        # a PARTIAL fill is an unsuccessful execution outcome worth studying
        # (the exchange matched only part of the size). Missing data stays
        # NULL — nothing here is inferred from a submission reply.
        try:
            _matched = d(str(order.get("matched_amount") or 0))
            _want = d(str(execution.get("amount") or 0))
            if _matched > 0 and _want > 0 and _matched < _want:
                record_failed_trade(
                    symbol=sym, intended_action=execution.get("side", ""),
                    failure_category="PARTIAL_FILL",
                    failure_reason="order matched %s of %s" % (_matched, _want),
                    execution_mode="live",
                    decision_id=execution.get("decision_id", ""),
                    risk_decision_id=execution.get("risk_decision_id", ""),
                    order_id=order.get("order_id", ""),
                    order_status=order.get("status", ""),
                    exchange_response=result,
                    expected_price=order.get("price"),
                    actual_price=execution.get("fill_price"),
                    intended_amount=_want, executed_amount=_matched,
                    fees=execution.get("fee"),
                    portfolio_before=self._portfolio_state(),
                    outcome="partially_filled")
        except Exception:
            pass

        execution["status"] = (ExecutionStatus.FILLED.value
                               if order["status"] == OrderStatus.FILLED.value
                               else ExecutionStatus.SUBMITTED.value)
        execution["exchange_order_id"] = result.get("order_id", "")
        # the caller must be able to tell HOW MUCH actually sold: a live
        # limit order can rest unmatched, and «سفارش ثبت شد» for a resting
        # order is exactly the bug the user reported
        execution["matched_amount"] = str(order.get("matched_amount") or "0")
        execution["unmatched_amount"] = str(order.get("unmatched_amount") or
                                          execution["amount"])
        if applied:
            execution["fill_price"] = float(applied["fill_price"])
            execution["fee"] = float(applied["fee"])
        self.storage.save_execution(execution)
        self.events.publish("ORDER_OPENED", "execution_engine", cycle_id,
                            {"order_id": order["order_id"],
                             "exchange_order_id": order["exchange_order_id"],
                             "live": True, "market": sym,
                             "side": execution["side"]})
        return execution

    def _live_fill_from_order(self, execution: dict, order: dict,
                              cycle_id: str):
        """Record a real exchange fill in the ledger (fills/positions/PnL).

        Uses the exchange's matched amount and average price when the order
        filled immediately; marketable limit orders on Nobitex typically
        report matched > 0 right away. Idempotent per order. The fill is
        recorded under the CANONICAL market symbol — the exchange echoes
        full coin names in some fields, and exit plans/positions/UI all
        key on the registry symbol (PYTH NETWORKIRT -> PYTHIRT).
        """
        try:
            matched = d(str(order.get("matched_amount") or 0))
            if matched <= 0:
                return None
            already = [f for f in self.storage.fills(limit=200)
                       if f.get("order_id") == order["order_id"]]
            if already:
                return None
            avg_price = execution.get("fill_price")
            fill_price = d(str(avg_price)) if avg_price else \
                d(str(order.get("price") or execution["price"]))
            amount = matched
            gross = fill_price * amount
            fee_rate = d(str(self.cfg.fee.taker_rate))
            fee = (gross * fee_rate).quantize(Decimal("0.00000001"))
            fill = {
                "trade_id": "live_" + str(order.get("exchange_order_id") or
                                          new_id("trd")),
                "order_id": order["order_id"],
                "decision_id": order.get("decision_id", ""),
                "market": canonical_symbol(order["market"]),
                "side": order["side"],
                "price": str(fill_price),
                "amount": str(amount),
                "gross_value": str(gross),
                "fee_amount": str(fee),
                "fee_asset": order.get("fee_asset") or (
                    "USDT" if order["market"].endswith("USDT") else "IRT"),
                "timestamp": utc_now_iso(),
            }
            dup = self.storage.save_fill(fill)
            if not dup:
                self.events.error("EXECUTION_ENGINE", "DUPLICATE_FILL_SUPPRESSED",
                                  "WARNING", "live fill already applied",
                                  cycle_id=cycle_id)
                return None
            applied = self.portfolio.apply_fill(fill, fee_rate)
            self.events.publish("ORDER_FILLED", "execution_engine", cycle_id,
                                {"order_id": order["order_id"],
                                 "live": True,
                                 "price": float(fill_price),
                                 "amount": float(amount)})
            self.events.publish(
                "POSITION_OPENED" if applied["position_amount"] and
                applied["avg_entry"] == fill_price else "POSITION_UPDATED",
                "execution_engine", cycle_id,
                {"market": fill["market"],
                 "amount": float(applied["position_amount"])})
            if applied.get("closed"):
                self.events.publish("POSITION_CLOSED", "execution_engine",
                                    cycle_id, {"market": fill["market"],
                                               "realized_pnl":
                                               float(applied["realized_pnl"])})
            return {"status": OrderStatus.FILLED.value,
                    "fill_price": fill_price, "fee": fee}
        except ValueError as exc:
            self.events.error("EXECUTION_ENGINE", "LIVE_FILL_APPLY_FAILED",
                              "HIGH", str(exc), cycle_id=cycle_id)
            record_error("EXECUTION", "HIGH",
                         "live fill could not be applied: %s" % exc,
                         module="execution._live_fill_from_order",
                         symbol=order.get("market", ""),
                         order_id=order.get("order_id", ""),
                         cycle_id=cycle_id, exc=exc, recovered=False)
        except Exception as exc:
            self.events.error("EXECUTION_ENGINE", "LIVE_FILL_RECORD_FAILED",
                              "HIGH", str(exc), cycle_id=cycle_id)
            record_error("EXECUTION", "HIGH",
                         "live fill could not be recorded: %s" % exc,
                         module="execution._live_fill_from_order",
                         symbol=order.get("market", ""),
                         order_id=order.get("order_id", ""),
                         cycle_id=cycle_id, exc=exc, recovered=False)
        return None

    def _fail_real(self, execution: dict, order: dict, error: str,
                   cycle_id: str) -> dict:
        order["status"] = OrderStatus.REJECTED.value
        order["updated_at"] = utc_now_iso()
        self.storage.save_order(order)
        self.storage.update_execution_status(
            execution["execution_id"], ExecutionStatus.FAILED.value)
        execution["status"] = ExecutionStatus.FAILED.value
        execution["error"] = error
        self.storage.save_execution(execution)
        self.events.error("EXECUTION_ENGINE", "REAL_ORDER_FAILED", "HIGH",
                          error, cycle_id=cycle_id, retryable=False)
        category = classify_failure(error)
        err_id = record_error(
            "EXECUTION", "HIGH",
            "real %s order rejected for %s: %s"
            % (execution.get("side"), execution.get("market"), error),
            module="execution._fail_real",
            symbol=execution.get("market", ""),
            order_id=order.get("order_id", ""),
            trade_id="", cycle_id=cycle_id, exc=None,
            context={"client_order_id": order.get("client_order_id"),
                     "category": category},
            recovered=False)
        execution["error_id"] = err_id
        try:
            record_failed_trade(
                symbol=execution.get("market", ""),
                intended_action=execution.get("side", ""),
                failure_category=category, failure_reason=error,
                execution_mode="live",
                strategy=",".join(map(str, (execution.get("reason_codes")
                                            or []))),
                decision_id=execution.get("decision_id", ""),
                risk_decision_id=execution.get("risk_decision_id", ""),
                order_id=order.get("order_id", ""),
                order_status=OrderStatus.REJECTED.value,
                exchange_response=error, error_id=err_id,
                expected_price=order.get("price"),
                intended_amount=order.get("amount"),
                executed_amount="0",
                portfolio_before=self._portfolio_state(),
                outcome="rejected")
        except Exception:
            pass
        return execution
