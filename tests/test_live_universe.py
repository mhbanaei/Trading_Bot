"""Live market universe — قیمت‌گذاری و فروش بدون نیاز به اسنپ‌شات.

Reproduced live failures these tests pin:

* «حد ضرر مشخص شده اما فروش نمی‌شود»: ATOMUSDT (stop 1.88374، قیمت 1.763)
  در اسنپ‌شات نبود (سبد معاملاتی فقط ۲۲ بازار تتری بالای حجم را دارد)، پس
  `_manage_exits` قیمتی برایسنجش نداشت و هیچ تصمیمی ساخته نمی‌شد؛
* تصمیم‌های خروجی که ساخته می‌شدند (۲۴ تصمیم EXIT_STOP برای UNIUSDT) با
  `registry_symbols = set(market_by_symbol.keys())` یعنی «فقط بازارهای
  اسنپ‌شات» در اعتبارسنجی UNKNOWN_MARKET می‌گرفتند و دور ریخته می‌شدند؛
* «فروش» روی دارایی‌های کیف پول با «بازار در اسنپ‌شات فعلی نیست» رد می‌شد؛
* موتور ریسک سفارش را تأیید می‌کرد و صرافی رد می‌کرد: `SmallOrder: Order
  value is limited to above 0.5.` (موقعیت‌های خرد) و `OverValueOrder:
  Order Validation Failed` (فروش بیشتر از موجودی آزاد/سکه‌های قفل‌شده در
  سفارش‌های فروش خود ربات).

Run:  python -m unittest tests.test_live_universe -v
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.config import PaperCfg  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI, NobitexError  # noqa: E402
from nobitex_bot.universe import MarketUniverse  # noqa: E402
from nobitex_bot.validation import (ValidationResult,  # noqa: E402
                                    validate_ai_decision)
from nobitex_bot.web import STATIC_DIR, BotHTTPServer  # noqa: E402
from nobitex_bot.webauth import DashboardAuth  # noqa: E402

TS = "2026-10-14T22:00:00.000Z"


def _row(symbol, price, change=0.0, volume=1000.0, quote=None):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol[:-3]
    return {"symbol": symbol, "base_asset": base,
            "quote_asset": quote or ("USDT" if symbol.endswith("USDT")
                                     else "IRT"),
            "last_price": price, "best_bid": price * 0.999,
            "best_ask": price * 1.001, "spread": price * 0.002,
            "spread_percent": 0.2, "volume_24h": volume / max(price, 1e-9),
            "quote_volume_24h": volume, "change_24h": change,
            "high_24h": price * 1.1, "low_24h": price * 0.9,
            "tradable": True, "timestamp": TS, "received_at": TS}


class FakeExchange:
    """Minimal adapter with a WHOLE-catalog call, a wallet and a convert
    endpoint — the parts of the real Nobitex adapter the engine uses."""

    name = "fake"

    def __init__(self, catalog: dict, wallet: dict | None = None):
        self.catalog = dict(catalog)
        self.wallet = {k: Decimal(str(v)) for k, v in (wallet or {}).items()}
        self.locked: dict = {}
        self.all_tickers_calls = 0
        self.ticker_calls: list = []
        self.converted: list = []
        self.cancelled: list = []
        self.orders: list = []
        self.open_sell_orders: list = []

    # ---- discovery / market data
    def markets(self):
        return [{**r, "status": "ACTIVE", "tradable": True}
                for r in self.catalog.values()]

    def all_tickers(self):
        self.all_tickers_calls += 1
        return {s: dict(r) for s, r in self.catalog.items()}

    def ticker(self, symbol):
        self.ticker_calls.append(symbol)
        if symbol not in self.catalog:
            raise RuntimeError("no such market: %s" % symbol)
        return dict(self.catalog[symbol])

    def orderbook(self, symbol, depth=20):
        px = float(self.catalog[symbol]["last_price"])
        return {"symbol": symbol, "timestamp": TS, "sequence": 1,
                "bids": [{"price": px * 0.999, "amount": 1.0,
                          "notional": px}],
                "asks": [{"price": px * 1.001, "amount": 1.0,
                          "notional": px}],
                "depth_levels": 1, "bid_volume": 1.0, "ask_volume": 1.0,
                "imbalance": 0.0}

    def trades(self, symbol, limit=30):
        return []

    def candles(self, symbol, timeframe="30m", limit=120):
        return []

    # ---- wallet / orders
    def portfolio(self):
        assets = []
        for coin, amt in self.wallet.items():
            locked = self.locked.get(coin, Decimal("0"))
            assets.append({"asset": coin, "available": str(amt),
                           "locked": str(locked),
                           "total": str(amt + locked),
                           "quote_asset": "USDT",
                           "price_usdt": 1.0,
                           "value_usdt": float(amt + locked)})
        return {"timestamp": TS, "account": {"equity_usdt": 100.0},
                "assets": assets}

    def open_orders(self, market=None):
        return [o for o in self.open_sell_orders
                if not market or o.get("market") == market]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        for o in list(self.open_sell_orders):
            if o["order_id"] == order_id:
                self.open_sell_orders.remove(o)
                # the exchange frees the locked coins on cancel
                coin = o.get("coin")
                amt = o.get("freed") or Decimal("0")
                if coin:
                    self.locked[coin] = max(
                        Decimal("0"), self.locked.get(coin, Decimal("0"))
                        - amt)
                    self.wallet[coin] = self.wallet.get(coin, Decimal("0")) \
                        + amt
        return {"order_id": order_id, "status": "CANCELLED"}

    def convert_wallet_balance(self, coin):
        self.converted.append(coin.upper())
        self.wallet[coin.upper()] = Decimal("0")
        return {"converted": True, "coin": coin.upper(),
                "converted_amount": "250"}

    def place_limit_order(self, market, side, price, amount,
                          client_order_id):
        self.orders.append({"market": market, "side": side,
                            "price": price, "amount": amount})
        return {"order_id": "ex1", "client_order_id": client_order_id,
                "market": market, "side": side, "order_type": "LIMIT",
                "price": str(price), "amount": str(amount),
                "matched_amount": str(amount), "unmatched_amount": "0",
                "status": "FILLED", "created_at": TS, "updated_at": TS}

    def order_status(self, order_id, market=""):
        return {"order_id": str(order_id), "status": "FILLED",
                "matched_amount": "0", "average_price": "0", "fee": "0"}

    def health(self):
        return {"api": {"status": "HEALTHY", "latency_ms": 1}}

    def _get(self, path, params=None):
        return {"orders": [], "wallets": []}

    def _post(self, path, body=None):
        return {"status": "ok"}

    def _stats_all(self):
        return {}


class UniverseCase(TempCase):
    def _engine(self) -> TradingEngine:
        self.cfg.exchange.mode = "demo"
        self.cfg.exchange.live_trading = False
        self.cfg.paper = PaperCfg(starting_usdt=10000.0,
                                  starting_irt=500000000.0)
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()          # one snapshot: the cycle's own universe
        return e

    def _use_fake(self, e, catalog: dict, wallet: dict | None = None,
                  real: bool = False) -> FakeExchange:
        fx = FakeExchange(catalog, wallet)
        e.exchange = fx                 # rebinds the live universe
        e._real_attached = real
        return fx

    def _plant(self, e, market: str, amount: str, entry: str,
               stop: str | None = None) -> None:
        e.storage.upsert_position(market, Decimal(amount), Decimal(entry),
                                  Decimal("0"))
        if stop is not None:
            plans = e.storage.kv_get("exit_plans", {}) or {}
            plans[market] = {"stop": float(stop), "stop_pct": -3.0,
                             "auto": True, "stage": 0}
            e.storage.kv_set("exit_plans", plans)


class TestMarketUniverse(UniverseCase):
    def test_one_call_covers_every_market_and_is_cached(self):
        fx = FakeExchange({"BTCUSDT": _row("BTCUSDT", 100.0),
                           "ATOMUSDT": _row("ATOMUSDT", 1.5),
                           "XIRT": _row("XIRT", 5000.0)})
        uni = MarketUniverse(fx, data_dir=self.tmp, ttl=30)
        rows = uni.refresh()
        self.assertEqual(set(rows), {"BTCUSDT", "ATOMUSDT", "XIRT"})
        self.assertEqual(fx.all_tickers_calls, 1)
        self.assertEqual(uni.price("ATOMUSDT"), 1.5)
        # cached: a second refresh inside the TTL does not re-fetch
        uni.refresh()
        self.assertEqual(fx.all_tickers_calls, 1)
        self.assertTrue(uni.is_fresh())
        self.assertEqual(uni.status()["count"], 3)

    def test_forced_refresh_refetches(self):
        fx = FakeExchange({"BTCUSDT": _row("BTCUSDT", 100.0)})
        uni = MarketUniverse(fx, data_dir=self.tmp, ttl=30)
        uni.refresh()
        fx.catalog["BTCUSDT"] = _row("BTCUSDT", 120.0)
        uni.refresh(force=True)
        self.assertEqual(fx.all_tickers_calls, 2)
        self.assertEqual(uni.price("BTCUSDT"), 120.0)

    def test_sweep_fallback_without_a_catalog_call(self):
        class SweepOnly:
            name = "sweep"
            def markets(self):
                return [{"symbol": "BTCUSDT", "base_asset": "BTC",
                         "quote_asset": "USDT", "tradable": True}]
            def ticker(self, symbol):
                return {"symbol": symbol, "last_price": 7.0}
        uni = MarketUniverse(SweepOnly(), data_dir=self.tmp)
        uni.refresh()
        self.assertEqual(uni.price("BTCUSDT"), 7.0)

    def test_ranked_sorts_filters_and_annotates(self):
        fx = FakeExchange({"AAAUSDT": _row("AAAUSDT", 1.0, change=1.0,
                                           volume=500.0),
                           "BBBUSDT": _row("BBBUSDT", 2.0, change=9.0,
                                           volume=100.0),
                           "CCCIRT": _row("CCCIRT", 3000.0, change=50.0,
                                          volume=90000.0)})
        uni = MarketUniverse(fx, data_dir=self.tmp)
        uni.refresh()
        # gainers are ranked over the WHOLE exchange (no snapshot, no
        # quote filter) — the biggest 24h change wins
        self.assertEqual(uni.movers("gainers", limit=1)[0]["symbol"],
                         "CCCIRT")
        tether_gainers = uni.ranked(sort="gainers", quote="USDT")
        self.assertEqual(tether_gainers[0]["symbol"], "BBBUSDT")
        tether = uni.ranked(sort="volume", quote="USDT")
        self.assertEqual([r["symbol"] for r in tether],
                         ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(uni.ranked(search="ccc")[0]["symbol"], "CCCIRT")
        rows = uni.ranked(extras={"BBBUSDT": {"held": True}})
        self.assertTrue(next(r for r in rows
                             if r["symbol"] == "BBBUSDT")["held"])
        self.assertEqual(uni.ranked(min_quote_volume=1000.0)[0]["symbol"],
                         "CCCIRT")

    def test_disk_warm_start_needs_no_network(self):
        fx = FakeExchange({"BTCUSDT": _row("BTCUSDT", 55.0)})
        MarketUniverse(fx, data_dir=self.tmp, ttl=30).refresh()
        cold = FakeExchange({"BTCUSDT": _row("BTCUSDT", 99.0)})
        warm = MarketUniverse(cold, data_dir=self.tmp, ttl=30)
        self.assertEqual(warm.price("BTCUSDT"), 55.0)   # from disk
        self.assertEqual(cold.all_tickers_calls, 0)
        self.assertTrue(warm.status()["from_disk"])


class TestExitsWithoutSnapshot(UniverseCase):
    def test_stop_fires_for_a_held_coin_outside_the_snapshot(self):
        e = self._engine()
        snapshot = {m["symbol"]: m["last_price"]
                    for m in e.latest_snapshot["markets"]}
        self.assertNotIn("ATOMUSDT", snapshot)     # demo fixture list
        self._plant(e, "ATOMUSDT", "1.667996", "1.942", stop="1.88374")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.763),
                           "USDTIRT": _row("USDTIRT", 100000.0)})
        out = e._manage_exits(dict(snapshot), "cyc_live")
        self.assertEqual(len(out), 1, "the live catalog must arm the stop")
        self.assertEqual(out[0]["symbol"], "ATOMUSDT")
        self.assertIn("EXIT_STOP", out[0]["reason_codes"])
        self.assertAlmostEqual(out[0]["position_size"]["amount"],
                               float(Decimal("1.667996")), places=6)

    def test_held_market_price_is_merged_into_cycle_prices(self):
        e = self._engine()
        self._plant(e, "ATOMUSDT", "1.0", "2.0")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.5)})
        prices = {m["symbol"]: m["last_price"]
                  for m in e.latest_snapshot["markets"]}
        e._augment_prices_with_holdings(prices)
        self.assertEqual(prices.get("ATOMUSDT"), 1.5)

    def test_validation_accepts_held_markets_outside_the_snapshot(self):
        """The old gate (`set(market_by_symbol)`) dropped valid exits."""
        e = self._engine()
        self._plant(e, "ATOMUSDT", "1.667996", "1.942", stop="1.88374")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.763)})
        market_by_symbol = {m["symbol"]: m
                            for m in e.latest_snapshot["markets"]}
        out = e._manage_exits(
            {m["symbol"]: m["last_price"]
             for m in e.latest_snapshot["markets"]}, "cyc_val")
        dec = out[0]
        old_gate = ValidationResult(ok=True)
        validate_ai_decision(dec, set(market_by_symbol.keys()), old_gate)
        self.assertFalse(old_gate.ok)              # documents the old bug
        self.assertIn("UNKNOWN_MARKET",
                      [i.code for i in old_gate.issues])
        new_gate = ValidationResult(ok=True)
        validate_ai_decision(dec, e._allowed_symbols(market_by_symbol),
                             new_gate)
        self.assertTrue(new_gate.ok,
                        "allowed symbols must include held markets: %r"
                        % ([i.code for i in new_gate.issues],))

    def test_below_minimum_position_is_converted_instead_of_parked(self):
        e = self._engine()
        # the convert market moves REAL coins, so it obeys the same gate as
        # every live order: the adapter is attached AND live trading is on
        e.cfg.exchange.live_trading = True
        self._plant(e, "ZECUSDT", "0.0000269505", "1208.8", stop="1172.536")
        fx = self._use_fake(e,
                            {"ZECUSDT": _row("ZECUSDT", 1160.0),
                             "USDTIRT": _row("USDTIRT", 100000.0)},
                            wallet={"ZEC": Decimal("0.0000269505")},
                            real=True)
        out = e._manage_exits({}, "cyc_min")
        self.assertEqual(out, [])                  # nothing to sell: no order
        self.assertEqual(fx.converted, ["ZEC"])    # ... but closed anyway
        self.assertNotIn("ZECUSDT", e.storage.kv_get("exit_plans", {}))
        types = [x["event_type"] for x in e.recent_events(50)]
        self.assertIn("EXIT_CONVERTED_BELOW_MIN", types)

    def test_below_minimum_is_never_converted_while_live_trading_is_off(self):
        """SAFETY: a process holding the real adapter but with live trading
        OFF must not move a single coin. This is the exact state a
        diagnostic script reaches by merely constructing the engine
        (`live_trading=false` in its own copy of the config), so the
        below-minimum exit must stay parked instead of converting."""
        e = self._engine()
        self.assertFalse(e.cfg.exchange.live_trading)
        self._plant(e, "ZECUSDT", "0.0000269505", "1208.8", stop="1172.536")
        fx = self._use_fake(e,
                            {"ZECUSDT": _row("ZECUSDT", 1160.0),
                             "USDTIRT": _row("USDTIRT", 100000.0)},
                            wallet={"ZEC": Decimal("0.0000269505")},
                            real=True)
        self.assertEqual(e._manage_exits({}, "cyc_live_off"), [])
        self.assertEqual(fx.converted, [],
                         "live trading is OFF: the wallet must be untouchable")
        plan = e.storage.kv_get("exit_plans", {}).get("ZECUSDT") or {}
        self.assertIn("below_min_until_ts", plan)
        types = [x["event_type"] for x in e.recent_events(50)]
        self.assertNotIn("EXIT_CONVERTED_BELOW_MIN", types)

    def test_below_minimum_stays_parked_when_the_option_is_off(self):
        e = self._engine()
        e.cfg.risk.exit_convert_below_min = False
        self._plant(e, "ZECUSDT", "0.0000269505", "1208.8", stop="1172.536")
        fx = self._use_fake(e,
                            {"ZECUSDT": _row("ZECUSDT", 1160.0),
                             "USDTIRT": _row("USDTIRT", 100000.0)},
                            wallet={"ZEC": Decimal("0.0000269505")},
                            real=True)
        out = e._manage_exits({}, "cyc_min_off")
        self.assertEqual(out, [])
        self.assertEqual(fx.converted, [])
        self.assertIn("ZECUSDT", e.storage.kv_get("exit_plans", {}))
        self.assertIn("below_min_until_ts",
                      e.storage.kv_get("exit_plans", {})["ZECUSDT"])

    def test_sellable_position_still_places_a_real_order(self):
        e = self._engine()
        self._plant(e, "ATOMUSDT", "10.0", "2.0", stop="1.9")
        fx = self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.8)},
                            wallet={"ATOM": Decimal("10.0")}, real=True)
        out = e._manage_exits({}, "cyc_sell")
        self.assertEqual(len(out), 1)
        self.assertEqual(fx.converted, [], "an order-sized exit must NOT "
                                           "go through the convert market")
        self.assertAlmostEqual(out[0]["position_size"]["amount"], 10.0,
                               places=6)

    def test_free_sell_amount_clamps_to_the_wallet_and_frees_locked_coins(self):
        e = self._engine()
        fx = self._use_fake(e, {"WALUSDT": _row("WALUSDT", 0.06)},
                            wallet={"WAL": Decimal("10.0")}, real=True)
        fx.locked["WAL"] = Decimal("32.0")
        fx.open_sell_orders = [{"order_id": "777", "market": "WALUSDT",
                                "side": "SELL", "coin": "WAL",
                                "freed": Decimal("32.0")}]
        self._plant(e, "WALUSDT", "42.0", "0.0573", stop="0.060043")
        out = e._manage_exits({}, "cyc_lock")
        self.assertEqual(fx.cancelled, ["777"],
                         "our own resting SELL locks the coins; the exit "
                         "must free them before selling")
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["position_size"]["amount"], 42.0,
                               places=6)
        # and when the wallet simply holds less than the ledger says, the
        # order is clamped to the REAL free balance (never more than the
        # wallet has: that is what OverValueOrder rejects)
        fx.wallet["WAL"] = Decimal("20.0")
        fx.locked["WAL"] = Decimal("0")
        e.storage.upsert_position("WALUSDT", Decimal("42.0"),
                                  Decimal("0.0573"), Decimal("0"))
        plans = e.storage.kv_get("exit_plans", {}) or {}
        plans["WALUSDT"] = {"stop": 0.060043, "stop_pct": -3.0, "auto": True,
                            "stage": 0}
        e.storage.kv_set("exit_plans", plans)
        out = e._manage_exits({}, "cyc_clamp")
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["position_size"]["amount"], 20.0,
                               places=6)

    def test_empty_wallet_clears_the_stale_row_instead_of_retrying(self):
        e = self._engine()
        self._plant(e, "ATOMUSDT", "5.0", "2.0", stop="1.9")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.8)},
                       wallet={}, real=True)
        out = e._manage_exits({}, "cyc_stale")
        self.assertEqual(out, [])
        self.assertNotIn("ATOMUSDT", e.storage.kv_get("exit_plans", {}))
        self.assertEqual(float(e.storage.position("ATOMUSDT")["amount"]), 0.0)
        types = [x["event_type"] for x in e.recent_events(50)]
        self.assertIn("EXIT_STALE_POSITION_CLEARED", types)


class TestWholeExchangeView(UniverseCase):
    def test_markets_view_lists_the_whole_catalog(self):
        e = self._engine()
        self._plant(e, "ATOMUSDT", "1.667996", "1.942", stop="1.88374")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.763),
                           "NEWUSDT": _row("NEWUSDT", 9.0)})
        s = e.status()
        rows = {r["symbol"]: r for r in s["markets_view"]}
        self.assertIn("ATOMUSDT", rows)
        self.assertIn("NEWUSDT", rows)
        self.assertTrue(rows["ATOMUSDT"]["held"])
        self.assertFalse(rows["ATOMUSDT"]["in_snapshot"])
        self.assertEqual(rows["ATOMUSDT"]["source"], "universe")
        self.assertEqual(s["universe"]["count"], 2)

    def test_markets_universe_ranks_and_flags(self):
        e = self._engine()
        self._plant(e, "ATOMUSDT", "1.0", "2.0")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.5, change=-4.0,
                                            volume=10.0),
                           "HOTUSDT": _row("HOTUSDT", 3.0, change=8.0,
                                           volume=999.0)})
        out = e.markets_universe(sort="gainers", limit=1)
        self.assertEqual(out["count"], 1)
        row = out["rows"][0]
        self.assertEqual(row["symbol"], "HOTUSDT")
        self.assertTrue(row["enabled"])
        self.assertFalse(row["held"])
        allrows = e.markets_universe()
        atom = next(r for r in allrows["rows"] if r["symbol"] == "ATOMUSDT")
        self.assertTrue(atom["held"])
        self.assertAlmostEqual(atom["held_amount"], 1.0, places=6)
        self.assertAlmostEqual(atom["value_usdt"], 1.5, places=6)
        self.assertEqual(allrows["universe"]["count"], 2)

    def test_sell_now_does_not_need_a_snapshot(self):
        e = self._engine()
        self._plant(e, "ATOMUSDT", "1.0", "2.0", stop="1.9")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.76)})
        e.portfolio.overwrite_balances({
            "ATOM": {"available": Decimal("1.0"),
                     "locked": Decimal("0")},
            "USDT": {"available": Decimal("50.0"),
                     "locked": Decimal("0")}})
        res = e.sell_position_now("ATOMUSDT")
        self.assertNotIn("اسنپ", str(res.get("error") or ""))
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("market"), "ATOMUSDT")

    def test_manual_buy_does_not_need_a_snapshot(self):
        """A manual BUY on a market outside the snapshot must get PAST the
        market lookup + validation (the old «اسنپ‌شات» dead end). Whether
        the RISK engine then approves is its own decision: a market the
        settings exclude is refused for a DATA reason (no collected
        orderbook/liquidity), not because a file is missing."""
        e = self._engine()
        self._use_fake(e, {"NEWUSDT": _row("NEWUSDT", 2.0)})
        e.portfolio.overwrite_balances({
            "USDT": {"available": Decimal("100.0"),
                     "locked": Decimal("0")}})
        e.cfg.exchange.trade_quote_markets = "usdt"
        res = e.manual_buy("NEWUSDT", 4.0)
        text = str(res.get("error") or "") + str(res.get("rejected") or "")
        self.assertNotIn("اسنپ", text)
        self.assertNotIn("UNKNOWN_MARKET", text)
        # the decision itself is recorded (lineage), whatever risk decides
        saved = [d for d in e.storage.decisions_recent(20)
                 if d.get("symbol") == "NEWUSDT"]
        self.assertTrue(saved, "the decision must exist")
        if not res.get("ok"):
            self.assertTrue(res.get("rejected"),
                            "only the risk engine may refuse it now: %r" %
                            (res,))


class TestDashboardCatalog(UniverseCase):
    """«همهٔ بازارهای نوبیتکس» — the panel the user actually clicks.

    The HTTP route is driven through the REAL dashboard server, because
    that (not the engine method) is the interface the browser talks to:
    it must answer with live rows for a holding the cycle's snapshot never
    covered, and its query parameters (search / sort / limit / quote) must
    shape the table.
    """

    def _serve(self, engine) -> int:
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"web": {"password": ""}}),
                            encoding="utf-8")
        auth = DashboardAuth(engine.cfg, config_path=cfg_path)
        server = BotHTTPServer(("127.0.0.1", 0), engine, auth)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(stop)
        return server.server_address[1]

    def _get(self, port: int, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body.decode("utf-8"))

    def test_endpoint_serves_the_live_catalog_without_a_snapshot(self):
        e = self._engine()
        self._plant(e, "ATOMUSDT", "1.667996", "1.942", stop="1.88374")
        self._use_fake(e, {"ATOMUSDT": _row("ATOMUSDT", 1.763),
                           "NEWUSDT": _row("NEWUSDT", 9.0)})
        port = self._serve(e)
        status, doc = self._get(port, "/api/markets_universe?sort=gainers")
        self.assertEqual(status, 200)
        rows = {r["symbol"]: r for r in doc["catalog"]}
        self.assertIn("ATOMUSDT", rows)
        self.assertIn("NEWUSDT", rows)
        self.assertEqual(doc["catalog_count"], len(doc["catalog"]))
        atom = rows["ATOMUSDT"]
        self.assertAlmostEqual(atom["last_price"], 1.763, places=6)
        self.assertTrue(atom["held"], "the held flag drives the sell button")
        self.assertAlmostEqual(atom["held_amount"], 1.667996, places=6)
        self.assertFalse(atom["in_snapshot"])
        self.assertEqual(atom["source"], "universe")

    def test_endpoint_honours_search_sort_and_limit(self):
        e = self._engine()
        self._use_fake(e, {"AAAUSDT": _row("AAAUSDT", 1.0, change=1.0,
                                           volume=500.0),
                           "NEWUSDT": _row("NEWUSDT", 9.0, change=40.0,
                                           volume=10.0),
                           "BBBIRT": _row("BBBIRT", 3000.0, change=2.0,
                                          volume=80000.0)})
        port = self._serve(e)
        _st, doc = self._get(port, "/api/markets_universe?search=NEW&limit=1")
        self.assertEqual([r["symbol"] for r in doc["catalog"]], ["NEWUSDT"])
        self.assertEqual(doc["search"], "NEW")
        _st, doc = self._get(port, "/api/markets_universe?sort=gainers&limit=1")
        self.assertEqual(doc["catalog"][0]["symbol"], "NEWUSDT")
        self.assertEqual(doc["sort"], "gainers")
        _st, doc = self._get(port, "/api/markets_universe?quote=USDT")
        self.assertEqual(sorted(r["symbol"] for r in doc["catalog"]),
                         ["AAAUSDT", "NEWUSDT"])

    def test_dashboard_ships_the_catalog_panel(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for marker in ('id="uniBox"', 'id="uniSearch"', 'id="uniSort"',
                       'id="uniQuote"', 'id="uniHeld"', 'id="uniRefresh"',
                       "/api/markets_universe", "function loadUniverse",
                       "function universeTable"):
            self.assertIn(marker, html)
        # wired to the LIVE catalog and re-rendered by the poll loop
        self.assertIn('setBox("#uniBox"', html)
        self.assertIn("loadUniverse(false)", html)
        self.assertIn("loadUniverse(true)", html)
        # each row can act on the coin: sell what is held, buy any market
        panel = html.split("function universeTable")[1].split("function marketsTable")[0]
        self.assertIn("btn-sellnow", panel)
        self.assertIn("btn-buynow", panel)
        # and it never reads the snapshot-only view
        self.assertNotIn("markets_view", panel)


class TestReadOnlyAdapterLock(unittest.TestCase):
    """NOBITEX_READ_ONLY=1 must make the ADAPTER itself refuse every
    wallet-moving call, whatever engine path reaches it.

    Live incident this pins: a read-only diagnostic process picked up the
    saved real session token, hot-attached the real adapter and — because
    the below-minimum exit path called the convert endpoint — converted 7
    real positions while the dashboard said «معامله واقعی: خاموش».
    """

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="nobot_ro_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        self.cfg = SimpleNamespace(
            data_dir=str(self.tmp), nobitex_base_url="",
            nobitex_username="", nobitex_password="", nobitex_totp_secret="",
            rls_to_irt_divisor=10, rest_timeout=12)

    def _lock(self, value="1"):
        old = os.environ.get("NOBITEX_READ_ONLY")

        def restore():
            if old is None:
                os.environ.pop("NOBITEX_READ_ONLY", None)
            else:
                os.environ["NOBITEX_READ_ONLY"] = old

        self.addCleanup(restore)
        os.environ["NOBITEX_READ_ONLY"] = value

    def test_flag_parsing(self):
        api = NobitexAPI(self.cfg)
        os.environ.pop("NOBITEX_READ_ONLY", None)
        self.assertFalse(api.is_read_only())
        for on in ("1", "true", "YES", "on"):
            os.environ["NOBITEX_READ_ONLY"] = on
            self.assertTrue(api.is_read_only(), on)
        for off in ("", "0", "false", "no", "off"):
            os.environ["NOBITEX_READ_ONLY"] = off
            self.assertFalse(api.is_read_only(), off)

    def test_every_write_is_refused_while_locked(self):
        self._lock()
        api = NobitexAPI(self.cfg)
        for call, args in (("place_limit_order",
                            ("BTCUSDT", "BUY", 100.0, 1.0, "bot-x")),
                           ("convert_wallet_balance", ("WAL",)),
                           ("cancel_order", ("123",))):
            with self.assertRaises(NobitexError, msg=call) as ctx:
                getattr(api, call)(*args)
            self.assertEqual(ctx.exception.code, "READ_ONLY", call)

    def test_reads_still_work_while_locked(self):
        self._lock()
        api = NobitexAPI(self.cfg)
        api._public_get = lambda path, params=None: {
            "stats": {"btc-usdt": {"latest": "83550"}}}
        api._stats_cache = {}
        api._stats_ts = 0.0
        tick = api.ticker("BTCUSDT")
        self.assertEqual(tick["last_price"], 83550.0)


if __name__ == "__main__":
    unittest.main()
