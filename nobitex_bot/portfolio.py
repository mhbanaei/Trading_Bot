"""Paper portfolio — contract sections 21, 22, 25, 45.

Tracks balances, positions, realized/unrealized PnL and drawdown.
All money math is Decimal-exact internally; floats only for UI rounding.
State persists in the SQLite kv_state table (survives restarts).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .events import EventBus
from .storage import Storage
from .utils import d, utc_now_iso

ZERO = Decimal("0")


class PaperPortfolio:
    """Spot paper-trading account in USDT (configurable starting balance)."""

    def __init__(self, storage: Storage, events: EventBus,
                 base: str = "USDT", starting_balance: float = 10000.0,
                 starting_balances: Optional[dict] = None):
        self.storage = storage
        self.events = events
        self.base = base
        self.starting_balance = d(str(starting_balance))
        if starting_balances is None:
            starting_balances = {base: float(starting_balance)}
        self.starting_balances = {
            asset.upper(): d(str(v)) for asset, v in starting_balances.items()}
        self.starting_equity = ZERO
        # set when a wallet sync sees money enter/leave the account with no
        # fill to explain it: the exchange-turnover cache (کل سرمایه
        # واریزی) is stale at that instant and must be re-read, otherwise
        # the value gap shows up as fake profit/loss (SS: برداشت)
        self.flow_dirty = False
        self._load()

    # ------------------------------------------------------------- state
    def _load(self) -> None:
        state = self.storage.kv_get("paper_portfolio")
        if not state or not state.get("balances"):
            self.balances = {asset: {"available": amt, "locked": ZERO}
                             for asset, amt in self.starting_balances.items()}
            # peak starts at 0; the first USDT-valued snapshot sets it
            self.peak_equity = ZERO
            self.realized_pnl_total = ZERO
            self._save()
        else:
            self.balances = {
                asset: {"available": d(v["available"]), "locked": d(v["locked"])}
                for asset, v in state["balances"].items()
            }
            self.peak_equity = d(state.get("peak_equity", "0"))
            self.realized_pnl_total = d(state.get("realized_pnl_total", "0"))
        self.starting_equity = d(state.get("starting_equity", "0")) \
            if state else ZERO
        self.deposits_irt = d(state.get("deposits_irt", "0")) \
            if state else ZERO

    def _save(self) -> None:
        self.storage.kv_set("paper_portfolio", {
            "balances": {a: {"available": str(b["available"]),
                             "locked": str(b["locked"])}
                         for a, b in self.balances.items()},
            "peak_equity": str(self.peak_equity),
            "realized_pnl_total": str(self.realized_pnl_total),
            "starting_equity": str(self.starting_equity),
            "deposits_irt": str(getattr(self, "deposits_irt", ZERO)),
        })

    def reset(self, starting_balance: Optional[float] = None) -> None:
        if starting_balance:
            self.starting_balances = {
                self.base: d(str(starting_balance))}
        self.balances = {asset: {"available": amt, "locked": ZERO}
                         for asset, amt in self.starting_balances.items()}
        self.peak_equity = ZERO
        self.realized_pnl_total = ZERO
        self.starting_equity = ZERO
        self.storage.kv_set("perf_pending", {})
        self._save()

    def overwrite_balances(self, balances: dict) -> None:
        """Mirror a real exchange wallet into the ledger (wallet sync).

        balances: {asset: {"available": Decimal, "locked": Decimal}}.
        Positions are untouched (they live in the positions table); the
        equity peak re-anchors so drawdown reflects the real account.
        """
        # deposit guard FIRST (before the ledger is replaced): a manual IRT
        # transfer into the Nobitex wallet is not profit. Whatever the
        # incoming IRT total exceeds the current ledger by is a top-up —
        # accumulated as deposits and excluded from the سود کل KPI so it
        # measures TRADING growth only. The IRT->USDT conversion happens in
        # portfolio_snapshot where the live rate is available.
        prev_irt = self.total("IRT")
        new_irt = d(str(balances.get("IRT", {}).get("available", 0))) + \
            d(str(balances.get("IRT", {}).get("locked", 0)))
        excess = new_irt - prev_irt
        self.balances = {
            asset.upper(): {"available": d(str(v["available"])),
                            "locked": d(str(v.get("locked", 0)))}
            for asset, v in balances.items()}
        self.peak_equity = ZERO
        # re-anchor realized PnL too: after wallet sync the ledger mirrors the
        # real account, so stale paper-era profit must not be shown
        self.realized_pnl_total = ZERO
        # the FIRST wallet sync anchors the account's starting value: the
        # total-profit KPI measures growth from this anchor (not from every
        # sync, which would always read 0)
        if self.starting_equity <= 0:
            self.starting_equity = ZERO  # set on the next snapshot
        if excess > Decimal("1"):
            self.deposits_irt = getattr(self, "deposits_irt", ZERO) + excess
            # fresh money is on the wallet: the exchange-turnover read that
            # feeds «کل سرمایه واریزی» is older than this transfer
            self.flow_dirty = True
        # An UNEXPLAINED IRT drop — every fill updates this ledger itself,
        # so a difference against the wallet is an outside transfer (bank
        # withdrawal). This matters most: the wallet is debited the moment
        # the user submits the withdrawal request, so the turnover cache
        # must be re-read NOW or «سود سرمایه» drops by the whole withdrawn
        # amount until the cache TTL expires (user report: برداشت از کل
        # سرمایه واریزی → سود سرمایه منفی).
        elif excess < -Decimal("1000"):
            self.flow_dirty = True
        self._save()

    # ----------------------------------------------------------- balances
    def available(self, asset: str) -> Decimal:
        return self.balances.get(asset, {}).get("available", ZERO)

    def total(self, asset: str) -> Decimal:
        b = self.balances.get(asset, {"available": ZERO, "locked": ZERO})
        return b["available"] + b["locked"]

    def can_afford(self, quote: str, notional: Decimal, fee: Decimal) -> bool:
        return self.available(quote) >= notional + fee

    # -------------------------------------------------------------- fills
    def apply_fill(self, fill: dict, fee_rate: Decimal) -> dict:
        """Apply a SS25 fill to balances/positions. Returns applied result."""
        market = fill["market"]
        side = fill["side"].upper()
        price = d(fill["price"])
        amount = d(fill["amount"])
        gross = price * amount
        fee = (gross * fee_rate).quantize(Decimal("0.00000001"))
        base_asset = market[:-4] if market.endswith("USDT") else market[:-3]
        quote_asset = "USDT" if market.endswith("USDT") else "IRT"

        result = {"realized_pnl": ZERO, "closed": False, "fee": fee,
                  "fee_asset": quote_asset, "gross": gross,
                  "avg_entry": ZERO, "position_amount": ZERO,
                  "last_buy_price": None}

        # deposit guard: a manual IRT top-up (walled cash into Nobitex) must
        # NOT count as profit. Any IRT balance jump beyond what the fill
        # itself adds is treated as a transfer and the equity baseline is
        # shifted by the same amount so سود کل stays clean.
        prev_irt_total = self.total("IRT")

        quote = self.balances.setdefault(
            quote_asset, {"available": ZERO, "locked": ZERO})
        base = self.balances.setdefault(
            base_asset, {"available": ZERO, "locked": ZERO})

        pos = self.storage.position(market) or {}
        pos_amount = d(pos.get("amount", "0"))
        pos_avg = d(pos.get("avg_entry", "0"))
        pos_realized = d(pos.get("realized_pnl", "0"))
        # cumulative Toman invested in this position (for the UI detail);
        # tops up grow it, a full close resets it via upsert below
        buys_irt = d(pos.get("total_buys_irt") or 0)

        if side == "BUY":
            if quote["available"] < gross + fee:
                raise ValueError("INSUFFICIENT_BALANCE: %s < %s" %
                                 (quote["available"], gross + fee))
            quote["available"] -= gross + fee
            base["available"] += amount
            new_amount = pos_amount + amount
            pos_avg = ((pos_amount * pos_avg + gross + fee) / new_amount
                       if new_amount else ZERO)
            pos_amount = new_amount
            opened = pos_amount == amount and pos_amount > 0
            # top-up tracking: every BUY restarts the holding timer and
            # GROWS the invested total cumulatively (UI shows last-buy time
            # + the updated purchase amount next to the profit trigger);
            # last_buy_price records THE MOST RECENT buy price so the UI
            # «قیمت خرید» resets to it (SS100d) while avg_entry keeps the
            # true weighted mean for P&L
            last_buy = (fill.get("timestamp") or utc_now_iso())
            invested = buys_irt + gross + fee
            last_buy_price = price
            result["last_buy_price"] = float(price)
        else:  # SELL
            if base["available"] < amount:
                raise ValueError("INSUFFICIENT_BALANCE: %s %s < %s" %
                                 (base_asset, base["available"], amount))
            base["available"] -= amount
            quote["available"] += gross - fee
            realized = (gross - fee) - pos_avg * amount
            self.realized_pnl_total += realized
            pos_realized += realized
            pos_amount -= amount
            result["realized_pnl"] = realized
            result["closed"] = pos_amount == 0
            opened = False
            last_buy = None
            # full close -> reset EVERYTHING so a future re-buy starts
            # from zero (last_buy_price=0 explicitly clears it; None would
            # preserve the old value); partial sell -> keep them
            last_buy_price = 0 if pos_amount == 0 else None
            invested = 0 if pos_amount == 0 else None

        self.storage.upsert_position(
            market, str(pos_amount), str(pos_avg), str(pos_realized),
            last_buy_at=last_buy, total_buys_irt=invested,
            last_buy_price=last_buy_price)
        self._save()
        # deposit accounting (BUY only — a SELL also raises IRT but that is
        # sale proceeds, not a transfer). Anything beyond the fill's own
        # debit is an external top-up: accumulated as deposits so سود کل
        # ignores it (conversion to USDT happens in portfolio_snapshot).
        deposit = ZERO
        if side == "BUY" and quote_asset == "IRT":
            expected_delta = -(gross + fee)
            actual_delta = self.total("IRT") - prev_irt_total
            excess = actual_delta - expected_delta
            if excess > Decimal("1"):
                deposit = excess
                self.deposits_irt = getattr(self, "deposits_irt", ZERO) + excess
                self._save()
        result["deposit_irt"] = float(deposit)
        result["avg_entry"] = pos_avg
        result["position_amount"] = pos_amount
        return result

    # ---------------------------------------------------------------- pnl
    def unrealized(self, market: str, price) -> Decimal:
        pos = self.storage.position(market)
        if not pos or d(pos["amount"]) == 0:
            return ZERO
        return (d(price) - d(pos["avg_entry"])) * d(pos["amount"])

    # ----------------------------------------------------------- SS21 doc
    def portfolio_snapshot(self, prices: dict, usdt_rates: dict) -> dict:
        """SS21 Portfolio Snapshot + SS22 risk block.

        prices: {market: last_price}; usdt_rates: {asset: price_in_usdt}
        """
        usdtirt = usdt_rates.get("IRT")
        irt_per_usdt = (1.0 / float(usdtirt)) if usdtirt else 0.0
        now = utc_now_iso()
        assets_out = []
        total_value = ZERO
        unrealized_total = ZERO
        for asset, b in self.balances.items():
            total_amt = b["available"] + b["locked"]
            if total_amt == 0 and asset not in prices:
                continue
            rate = d(str(usdt_rates.get(asset, "1" if asset == self.base else "0")))
            market_value = total_amt * rate
            total_value += market_value
            assets_out.append({
                "asset": asset,
                "available": str(b["available"]),
                "locked": str(b["locked"]),
                "total": str(total_amt),
                "quote_asset": "USDT",
                "current_price": float(rate),
                "market_value": float(market_value),
                "portfolio_weight": 0.0,  # filled below
            })

        positions_out = []
        exposure = {}
        for pos in self.storage.positions():
            amt = d(pos["amount"])
            if amt == 0:
                continue
            price = d(str(prices.get(pos["market"], "0")))
            if price == 0:
                continue
            pos_avg = d(pos["avg_entry"])
            # pre-held wallet coins have unknown entry -> no fake PnL
            upnl = self.unrealized(pos["market"], price) if pos_avg > 0 else ZERO
            if pos_avg > 0:
                unrealized_total += upnl
            value = amt * price
            # exposure must be unit-consistent: positions in IRT-quoted
            # markets are converted to USDT via the IRT rate so exposure
            # ratios compare like with like (SS22)
            quote_asset = "USDT" if pos["market"].endswith("USDT") else "IRT"
            quote_rate = d(str(usdt_rates.get(quote_asset, "0")))
            value_usdt = value * quote_rate
            # NOTE: the coin itself is already valued via its balance above;
            # position rows here are for display/exposure, NOT added again to
            # total_value (prevents double counting).
            exposure[pos["market"]] = float(round(value_usdt, 4))
            positions_out.append({
                "market": pos["market"],
                "amount": float(amt),
                "avg_entry_price": float(pos_avg),
                "current_price": float(price),
                "market_value": float(value_usdt),
                "unrealized_pnl": float(upnl),
                "realized_pnl": float(d(pos["realized_pnl"])),
                "opened_at": pos["opened_at"],
            })

        equity = total_value
        if self.starting_equity <= 0 and equity > 0:
            # first-ever valuation becomes the baseline of total profit
            self.starting_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
            self._save()
        drawdown = ((self.peak_equity - equity) / self.peak_equity
                    if self.peak_equity else ZERO)

        for a in assets_out:
            mv = d(str(a["market_value"]))
            a["portfolio_weight"] = float(round(mv / equity, 6)) if equity else 0.0

        cash = self.total(self.base)
        # real Toman (IRT) cash: the IRT ledger IS the Toman wallet — for
        # IRT-quoted markets the spendable cash is the IRT balance itself,
        # not the USDT dust converted at the USDTIRT rate.
        irt_cash = self.total("IRT")
        irt_avail = self.available("IRT")
        cash_ratio = float(round(cash / equity, 6)) if equity else 0.0
        invested = sum(exposure.values())
        # exposure_ratio = invested / total equity, capped at 1.0: coins the
        # account holds are already part of equity, so anything above 1.0
        # would be fake leverage and would block all trading forever.
        exposure_ratio = (float(round(d(str(invested)) / equity, 6))
                          if equity else 0.0)
        exposure_ratio = float(min(1.0, exposure_ratio))

        if drawdown >= Decimal("0.10") or exposure_ratio > 0.55:
            risk_status = "HIGH"
        elif drawdown >= Decimal("0.05") or exposure_ratio > 0.35:
            risk_status = "ELEVATED"
        else:
            risk_status = "NORMAL"

        return {
            "timestamp": now,
            "account": {
                "equity_usdt": float(round(equity, 2)),
                "available_usdt": float(round(cash, 2)),
                "equity_irt": float(round(equity * d(str(irt_per_usdt)), 2))
                if irt_per_usdt else 0.0,
                "available_irt": float(round(cash * d(str(irt_per_usdt)), 2))
                if irt_per_usdt else 0.0,
                # REAL Toman balances straight from the IRT ledger (the
                # wallet's Toman account), not the USDT-cash conversion:
                "irt_total": float(round(irt_cash, 2)),
                "irt_available": float(round(irt_avail, 2)),
            },
            "assets": assets_out,
            "positions": positions_out,
            "risk": {
                "total_equity": float(round(equity, 2)),
                "exposure": exposure,
                "exposure_ratio": exposure_ratio,
                "cash_ratio": cash_ratio,
                "unrealized_pnl": float(round(unrealized_total, 4)),
                "realized_pnl": float(round(self.realized_pnl_total, 4)),
                "drawdown": {
                    "current": float(round(drawdown, 6)),
                    "max_seen": float(round(
                        (self.peak_equity - equity) / self.peak_equity, 6))
                    if self.peak_equity else 0.0,
                },
                "risk_status": risk_status,
                # TOTAL profit in percent: change of the whole account value
                # since the wallet was first synced, EXCLUDING manual IRT
                # deposits (they are transfers, not trading profit).
                "starting_equity": float(round(self.starting_equity, 2))
                if self.starting_equity else None,
                "deposits_irt": float(round(getattr(self, "deposits_irt",
                                                    ZERO), 2)),
                "total_profit_pct": float(round(
                    (equity - self.starting_equity -
                     getattr(self, "deposits_irt", ZERO) / d(str(irt_per_usdt or 0))
                     ) / self.starting_equity * 100, 2))
                    if self.starting_equity and irt_per_usdt else None,
            },
        }
