"""Execution engine — contract sections 38, 39, 41, 43, 44, 25.

Paper broker: submits limit orders, simulates fills against the live
market price with slippage, applies fills to the paper portfolio,
records fills/orders/executions with full audit lineage (SS83).
"""
from __future__ import annotations

import threading
from decimal import Decimal

from .config import Config
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

    def _live(self) -> bool:
        """Real orders only when the real adapter is attached AND the user
        explicitly enabled live trading (SS101 default: paper)."""
        return (isinstance(self.exchange, NobitexAPI)
                and bool(getattr(self.cfg.exchange, "live_trading", False)))

    def _client_order_id(self, cycle_id: str, symbol: str) -> str:
        # SS40: bot_<cycle>_<symbol>_<seq>  — Nobitex REJECTS '_' in
        # clientOrderId (verified live: 'Invalid clientOrderId'), so use
        # hyphens: bot-<cycle>-<symbol>-<seq>
        seq = len(self.storage.orders(limit=10000)) + 1
        return "bot-%s-%s-%03d" % (cycle_id.replace("cycle_", "")[:15],
                                   symbol, seq)

    def execute(self, decision: dict, risk_decision: dict,
                fresh_prices: dict, cycle_id: str) -> dict:
        """Submit + fill a risk-approved decision. Returns execution record."""
        if risk_decision.get("status") != "APPROVED":
            return {"status": "SKIPPED", "reason": "RISK_NOT_APPROVED"}
        if decision.get("action") not in ("BUY", "SELL"):
            return {"status": "SKIPPED", "reason": "NO_ACTION_REQUIRED"}

        sym = decision["symbol"]
        price = d(str(fresh_prices.get(sym) or 0))
        if price <= 0:
            return {"status": "SKIPPED", "reason": "PRICE_UNAVAILABLE"}

        amount = d(str(risk_decision.get("approved_position_size") or 0))
        if amount <= 0:
            return {"status": "SKIPPED", "reason": "NO_APPROVED_SIZE"}

        side = decision["action"]

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
                execution["status"] = ExecutionStatus.FAILED.value
                execution["error"] = str(exc)
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
            order["status"] = applied
        self.storage.save_order(order)

        execution["status"] = (ExecutionStatus.FILLED.value
                               if order["status"] == OrderStatus.FILLED.value
                               else ExecutionStatus.SUBMITTED.value)
        execution["exchange_order_id"] = result.get("order_id", "")
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
        except Exception as exc:
            self.events.error("EXECUTION_ENGINE", "LIVE_FILL_RECORD_FAILED",
                              "HIGH", str(exc), cycle_id=cycle_id)
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
        return execution
