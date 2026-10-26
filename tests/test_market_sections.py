"""بخش تتری / تومانی — the Toman (IRT) section is driven by REAL activity.

User rule (نمای کلی / holdings box):
- The IRT section is active ONLY when the user really invested Toman — a
  held Toman-market coin, a real *IRT order/fill, or a stop/take-profit
  plan on one. Without it the section shows no coin and no Toman cash row.
- A coin bought on the Tether market must never appear in the Toman box
  (and the reverse): every wallet balance is filed into exactly ONE
  market, the one the user REALLY bought it in.
- Each section always keeps its own money: Toman for IRT, dollars for
  USDT (`quote_of_market` is what both the engine and the UI split on).

Run:  python -m unittest tests.test_market_sections -v
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.utils import quote_of_market  # noqa: E402

TS = "2026-10-09T10:00:00.000Z"


class TestQuoteOfMarket(TempCase):
    """quote_of_market: the money side of a market symbol."""

    def test_plain_markets(self):
        self.assertEqual(quote_of_market("BTCUSDT"), "USDT")
        self.assertEqual(quote_of_market("BTCIRT"), "IRT")
        self.assertEqual(quote_of_market(" btc irt "), "IRT")

    def test_usdt_priced_in_toman_is_an_irt_market(self):
        # USDTIRT is the Tether market quoted in Toman — money side = Toman
        self.assertEqual(quote_of_market("USDTIRT"), "IRT")

    def test_unknown_market(self):
        self.assertIsNone(quote_of_market("BTC"))
        self.assertIsNone(quote_of_market(""))


class TestMarketSections(TempCase):
    """status()['market_sections'] — what turns each box on."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()
        return e

    def _fill(self, e, market, trade_id="live_order1", side="BUY",
              price="100", amount="1"):
        e.storage.save_fill({
            "trade_id": trade_id, "order_id": "ord1", "decision_id": "",
            "market": market, "side": side, "price": price, "amount": amount,
            "gross_value": str(Decimal(price) * Decimal(amount)),
            "fee_amount": "0", "fee_asset": "IRT", "timestamp": TS})

    def _order(self, e, market, exchange_id="exch-1", side="BUY"):
        e.storage.save_order({
            "order_id": "ord-" + market, "client_order_id": "bot-1",
            "decision_id": "", "market": market, "side": side,
            "order_type": "LIMIT", "price": "100", "amount": "1",
            "status": "OPEN", "fee_asset": "IRT", "created_at": TS,
            "updated_at": TS, "exchange_order_id": exchange_id})

    def _sections(self, e):
        return e.status()["market_sections"]

    # ---------------------------------------------------------- activity
    def test_nothing_activates_nothing(self):
        e = self._engine()
        act = self._sections(e)
        self.assertFalse(act["IRT"]["active"])
        self.assertFalse(act["USDT"]["active"])

    def test_real_usdt_buy_activates_only_the_tether_section(self):
        e = self._engine()
        self._fill(e, "BTCUSDT", trade_id="live_u1", price="60000")
        act = self._sections(e)
        self.assertTrue(act["USDT"]["active"])
        self.assertFalse(act["IRT"]["active"],
                         "a Tether purchase must not light the Toman section")

    def test_real_irt_buy_activates_the_toman_section(self):
        e = self._engine()
        self._fill(e, "ETHIRT", trade_id="live_i1", price="100000000")
        act = self._sections(e)
        self.assertTrue(act["IRT"]["active"])
        self.assertIn("ETHIRT", act["IRT"]["markets"])
        self.assertFalse(act["USDT"]["active"])

    def test_paper_fills_are_not_real_money(self):
        e = self._engine()
        self._fill(e, "BTCIRT", trade_id="trd_paper1")
        act = self._sections(e)
        self.assertFalse(act["IRT"]["active"])
        self.assertFalse(act["USDT"]["active"])

    def test_real_irt_order_activates_it_and_paper_order_does_not(self):
        e = self._engine()
        self._order(e, "SOLIRT", exchange_id="")
        self.assertFalse(self._sections(e)["IRT"]["active"])
        self._order(e, "SOLIRT", exchange_id="exch-2")
        self.assertTrue(self._sections(e)["IRT"]["active"])

    def test_exit_plan_on_a_toman_market_activates_it(self):
        e = self._engine()
        plans = e.storage.kv_get("exit_plans", {})
        plans["XIRT"] = {"stop": 90.0, "stage": 0}
        e.storage.kv_set("exit_plans", plans)
        act = self._sections(e)
        self.assertTrue(act["IRT"]["active"])
        self.assertFalse(act["USDT"]["active"])

    def test_holdings_count_per_section(self):
        e = self._engine()
        e.storage.upsert_position("BTCIRT", Decimal("1"), Decimal("5"), Decimal("0"))
        e.storage.upsert_position("ETHUSDT", Decimal("2"), Decimal("5"), Decimal("0"))
        act = self._sections(e)
        self.assertEqual(act["IRT"]["holdings"], 1)
        self.assertEqual(act["USDT"]["holdings"], 1)
        self.assertTrue(act["IRT"]["active"] and act["USDT"]["active"])


class TestWalletFiling(TempCase):
    """One wallet coin = ONE market: the market it was really bought in."""

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()
        return e

    def _markets(self, e, syms):
        e.latest_snapshot = {"markets": [{"symbol": s, "last_price": 100.0}
                                         for s in syms]}

    def _fill(self, e, market, trade_id="live_b1", side="BUY",
              price="5000000", amount="1", ts=TS):
        e.storage.save_fill({
            "trade_id": trade_id, "order_id": "ord" + trade_id,
            "decision_id": "", "market": market, "side": side,
            "price": price, "amount": amount,
            "gross_value": str(Decimal(price) * Decimal(amount)),
            "fee_amount": "0", "fee_asset": "IRT", "timestamp": ts})

    def _wallet(self, base="BTC", amount="1"):
        return {base: {"available": Decimal(amount), "locked": Decimal("0")}}

    def test_wallet_sync_seeds_one_market_per_coin(self):
        e = self._engine()
        self._markets(e, ["BTCUSDT", "BTCIRT"])   # snapshot offers BOTH
        self._fill(e, "BTCIRT")                   # the real buy was in Toman
        e._seed_positions_from_wallet(self._wallet())
        irt = e.storage.position("BTCIRT")
        usdt = e.storage.position("BTCUSDT")
        self.assertEqual(Decimal(str(irt["amount"])), Decimal("1"))
        self.assertTrue(usdt is None or Decimal(str(usdt["amount"])) == 0,
                        "a Toman-bought coin must not be seeded as a Tether "
                        "position (it would show inside the USDT box)")
        # entry rebuilt from the REAL buy price of that market
        self.assertEqual(Decimal(str(irt["avg_entry"])), Decimal("5000000"))

    def test_wallet_sync_refiles_a_coin_from_the_wrong_market(self):
        e = self._engine()
        self._markets(e, ["BTCUSDT", "BTCIRT"])
        # a stale row seeded under the wrong quote (the old wallet sync put
        # every held coin into the first market it found)
        e.storage.upsert_position("BTCUSDT", Decimal("1"),
                                  Decimal("60000"), Decimal("0"))
        self._fill(e, "BTCIRT")                   # real buys are Toman
        e._seed_positions_from_wallet(self._wallet())
        self.assertEqual(Decimal(str(e.storage.position("BTCUSDT")["amount"])),
                         Decimal("0"))
        self.assertEqual(Decimal(str(e.storage.position("BTCIRT")["amount"])),
                         Decimal("1"))

    def test_one_wallet_balance_is_never_seeded_twice(self):
        """Real buys on BOTH sides: the tracked market keeps the balance.

        The wallet reports ONE number per coin, so it cannot be split
        across two markets. The old sync handed that single balance to
        EVERY market of the coin — the same BTC then showed up in both
        boxes. Now the already-tracked market (which has its own real
        buys) keeps it and the other market gets nothing.
        """
        e = self._engine()
        self._markets(e, ["BTCUSDT", "BTCIRT"])
        e.storage.upsert_position("BTCUSDT", Decimal("1"),
                                  Decimal("60000"), Decimal("0"))
        self._fill(e, "BTCUSDT", trade_id="live_u2", price="60000",
                   amount="1", ts="2026-10-08T10:00:00.000Z")
        self._fill(e, "BTCIRT", trade_id="live_i2", ts=TS)
        e._seed_positions_from_wallet(self._wallet())
        held = [p for p in e.storage.positions()
                if p["market"] in ("BTCUSDT", "BTCIRT")
                and Decimal(str(p["amount"])) > 0]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["market"], "BTCUSDT")

    def test_configured_preference_decides_without_any_history(self):
        e = self._engine()
        self._markets(e, ["BTCUSDT", "BTCIRT"])
        e.cfg.exchange.trade_quote_markets = "irt"
        self.assertEqual(e._market_for_base("BTC", {"BTCUSDT", "BTCIRT"}),
                         "BTCIRT")
        e.cfg.exchange.trade_quote_markets = "usdt"
        self.assertEqual(e._market_for_base("BTC", {"BTCUSDT", "BTCIRT"}),
                         "BTCUSDT")

    def test_real_buy_market_wins_over_the_preference(self):
        e = self._engine()
        self._markets(e, ["BTCUSDT", "BTCIRT"])
        e.cfg.exchange.trade_quote_markets = "usdt"
        self._fill(e, "BTCIRT")                   # but the money went Toman
        self.assertEqual(e._market_for_base("BTC", {"BTCUSDT", "BTCIRT"}),
                         "BTCIRT")


class TestMarketPnlSplit(TempCase):
    """موجودی/ارزش/سود و زیان هر بازار جدا محاسبه می‌شود.

    A Tether position's dollars and a Toman position's Toman never share a
    number: `_holdings_pnl()['by_market']` reports each market in its own
    quote unit, and the top-level `_irt` numbers are the Toman side ONLY —
    a $ amount is never reported as Toman.
    """

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()
        return e

    def _plant(self, e, market, amount, entry):
        e.storage.upsert_position(market, Decimal(amount), Decimal(entry),
                                  Decimal("0"),
                                  last_buy_price=Decimal(entry))

    def test_tether_and_toman_are_never_summed(self):
        e = self._engine()
        # BTC bought with Tether: 0.1 @ $100, now $120 → +$2
        self._plant(e, "BTCUSDT", "0.1", "100")
        # the same coin held on the Toman market: 0.0001 @ 1,000,000,000 T
        self._plant(e, "BTCIRT", "0.0001", "1000000000")
        prices = {"BTCUSDT": 120.0, "BTCIRT": 1_200_000_000.0,
                  "USDTIRT": 100000.0}
        out = e._holdings_pnl(prices)
        m = out["by_market"]
        # Tether side, in dollars
        self.assertAlmostEqual(m["USDT"]["invested"], 10.0, places=6)
        self.assertAlmostEqual(m["USDT"]["value"], 12.0, places=6)
        self.assertAlmostEqual(m["USDT"]["pnl"], 2.0, places=6)
        self.assertAlmostEqual(m["USDT"]["pnl_pct"], 20.0, places=4)
        self.assertEqual(m["USDT"]["rows"], 1)
        # Toman side, in Toman
        self.assertAlmostEqual(m["IRT"]["invested"], 100000.0, places=2)
        self.assertAlmostEqual(m["IRT"]["value"], 120000.0, places=2)
        self.assertAlmostEqual(m["IRT"]["pnl"], 20000.0, places=2)
        self.assertAlmostEqual(m["IRT"]["pnl_pct"], 20.0, places=4)
        # top level = Toman only (never $10 + 100,000 T as one number)
        self.assertAlmostEqual(out["invested_irt"], 100000.0, places=2)
        self.assertAlmostEqual(out["value_irt"], 120000.0, places=2)
        self.assertAlmostEqual(out["pnl_irt"], 20000.0, places=2)
        self.assertAlmostEqual(out["usdt_invested"], 10.0, places=6)
        self.assertAlmostEqual(out["usdt_pnl"], 2.0, places=6)

    def test_usdt_only_account_reports_no_toman_numbers(self):
        e = self._engine()
        self._plant(e, "ETHUSDT", "2", "100")
        out = e._holdings_pnl({"ETHUSDT": 90.0, "USDTIRT": 100000.0})
        m = out["by_market"]
        self.assertEqual(m["IRT"]["rows"], 0)
        self.assertEqual(m["IRT"]["invested"], 0.0)
        self.assertAlmostEqual(m["IRT"]["pnl"], 0.0, places=6)
        self.assertIsNone(m["IRT"]["pnl_pct"])
        self.assertAlmostEqual(m["USDT"]["pnl"], -20.0, places=6)  # (90−100)×2

    def test_untouched_market_reports_no_profit(self):
        """بازاری که خریدی در آن نشده، سود و زیانی نمی‌سازد."""
        e = self._engine()
        self._plant(e, "SOLIRT", "0.01", "30000000")
        out = e._holdings_pnl({"SOLIRT": 33000000.0, "USDTIRT": 100000.0})
        m = out["by_market"]
        self.assertAlmostEqual(m["IRT"]["pnl"], 30000.0, places=2)
        self.assertEqual(m["USDT"]["rows"], 0)
        self.assertIsNone(m["USDT"]["pnl_pct"])

    def test_row_pnl_is_in_the_markets_own_unit(self):
        e = self._engine()
        self._plant(e, "ETHUSDT", "2", "100")          # $ side
        self._plant(e, "ETHIRT", "0.5", "10000000")     # Toman side
        prices = {"ETHUSDT": 110.0, "ETHIRT": 12_000_000.0,
                  "USDTIRT": 100000.0}
        usdtirt = float(e._usdt_rates(prices).get("IRT") or 0)
        row_u = e._position_row(e.storage.position("ETHUSDT"), prices,
                                usdtirt)
        row_i = e._position_row(e.storage.position("ETHIRT"), prices,
                                usdtirt)
        self.assertEqual(row_u["quote"], "USDT")
        self.assertEqual(row_i["quote"], "IRT")
        self.assertAlmostEqual(row_u["pnl_money"], 20.0, places=6)     # $20
        self.assertAlmostEqual(row_i["pnl_money"], 1000000.0, places=2)
        # each row's own money never borrows the other market's price
        self.assertAlmostEqual(row_u["invested_irt"], 200.0, places=6)
        self.assertAlmostEqual(row_i["invested_irt"], 5000000.0, places=2)

    def test_the_section_header_is_the_sum_of_the_displayed_rows(self):
        """سرِ هر بخش = جمع همان اعدادی که ردیف‌هایش نشان می‌دهند.

        Live truth: the five Tether rows printed $0.61 / $6.38 / $3.89 /
        $3.24 / $0.01 (ارزش) and $0.59 / $5.99 / $3.04 / $3.24 / $0.01
        (مبلغ خرید); their (ارزش − مبلغ خرید) added up to **$1.26**, but
        the header said **$1.25** because the raw values were summed first
        and rounded once (14.1255 − 12.8739 → 1.2516 → $1.25). The Toman
        box was off by a whole Toman the same way (۶۸۰٬۲۹۱٫۵۹ → ۶۸۰٬۲۹۲
        while its rows printed ۳۳۲٬۲۲۲ + ۳۴۸٬۰۶۹ = ۶۸۰٬۲۹۱).
        """
        e = self._engine()
        # تتری: دقیقاً اعداد ردیف‌های حساب واقعی = ۵ سکه روی صفحه
        self._plant(e, "SUSDT", "14.2522745", "0.04152086793396698")
        self._plant(e, "FILUSDT", "5.578", "1.073581078201576816535265289")
        self._plant(e, "STRKUSDT", "55.06", "0.05524")
        self._plant(e, "ATOMUSDT", "1.667996", "1.942")
        self._plant(e, "BATUSDT", "0.084915", "0.1372")
        # تومانی: جمع خام ۶۸۰٬۲۹۱٫۵۸۶ ولی جمع ردیف‌های چاپ‌شده ۶۸۰٬۲۹۱
        self._plant(e, "MAGICIRT", "10.7061372", "11038.0646")
        self._plant(e, "QNTIRT", "0.005294965", "23623700")
        prices = {"SUSDT": 0.04267, "FILUSDT": 1.143, "STRKUSDT": 0.07072,
                  "ATOMUSDT": 1.94, "BATUSDT": 0.141,
                  "MAGICIRT": 31031.0, "QNTIRT": 65735929.0,
                  "USDTIRT": 100000.0}
        usdtirt = float(e._usdt_rates(prices).get("IRT") or 0)
        out = e._holdings_pnl(prices)
        for quote_, markets in (("USDT", ("SUSDT", "FILUSDT", "STRKUSDT",
                                          "ATOMUSDT", "BATUSDT")),
                                ("IRT", ("MAGICIRT", "QNTIRT"))):
            rows = [e._position_row(e.storage.position(mk), prices, usdtirt)
                    for mk in markets]
            m = out["by_market"][quote_]
            self.assertEqual(m["rows"], len(markets))
            # جمع همان اعدادِ نمایشی ردیف‌ها، بدون گردکردن دوبارهٔ جمع
            self.assertAlmostEqual(
                m["invested"], sum(float(r["invested_disp"]) for r in rows),
                places=6)
            self.assertAlmostEqual(
                m["value"], sum(float(r["value_disp"]) for r in rows),
                places=6)
            self.assertAlmostEqual(
                m["pnl"], sum(float(r["pnl_money"]) for r in rows),
                places=6)
            # و اتحادِ دیدنی: «ارزش − مبلغ خرید» == «سود و زیان این بازار»
            self.assertAlmostEqual(m["pnl"], m["value"] - m["invested"],
                                   places=9)
            # هر عدد با دقتِ نمایش همان بازار (سنت/تومانِ کامل)
            step = 2 if quote_ == "USDT" else 0
            self.assertEqual(round(m["value"], step), m["value"])
            self.assertEqual(round(m["invested"], step), m["invested"])
        # اعداد همان پنج ردیف واقعی: ۱۴٫۱۳ − ۱۲٫۸۷ = ۱٫۲۶ (نه ۱٫۲۵)
        self.assertAlmostEqual(out["by_market"]["USDT"]["value"], 14.13,
                               places=6)
        self.assertAlmostEqual(out["by_market"]["USDT"]["invested"], 12.87,
                               places=6)
        self.assertAlmostEqual(out["by_market"]["USDT"]["pnl"], 1.26,
                               places=6)
        # بخش تومانی هم فقط جمع ردیف‌های چاپ‌شده است (تومانِ کامل)
        irt = out["by_market"]["IRT"]
        self.assertEqual(irt["value"], float(int(irt["value"])))
        self.assertAlmostEqual(irt["value"], 332222.0 + 348069.0, places=6)
        self.assertAlmostEqual(irt["pnl"], irt["value"] - irt["invested"],
                               places=9)


class TestOneWalletBalanceNeverFillsTwoMarkets(TempCase):
    """یک سکه = یک ردیف: جمع ردیف‌ها هرگز از موجودی کیف پول بیشتر نمی‌شود.

    Live account truth: FIL/STRK/ATOM sat in the Toman box AND the Tether
    box with the SAME amount (entry 0) — the Toman copy of a Tether-bought
    coin. The wallet reports ONE balance per coin, so rows adding up to
    more than that balance are double counting.
    """

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()
        return e

    def _wallet(self, base="FIL", amount="5.57969425"):
        return {base: {"available": Decimal(amount), "locked": Decimal("0")}}

    def _fill(self, e, market, trade_id="live_f1", side="BUY",
              price="100", amount="1", ts=TS):
        e.storage.save_fill({
            "trade_id": trade_id, "order_id": "ord" + trade_id,
            "decision_id": "", "market": market, "side": side,
            "price": price, "amount": amount,
            "gross_value": str(Decimal(price) * Decimal(amount)),
            "fee_amount": "0", "fee_asset": "IRT", "timestamp": ts})

    def _amount(self, e, market):
        p = e.storage.position(market)
        return Decimal(str(p["amount"])) if p else Decimal("0")

    def test_two_rows_holding_the_whole_balance_are_deduped(self):
        e = self._engine()
        e.storage.upsert_position("FILIRT", Decimal("5.57969425"),
                                  Decimal("212614"), Decimal("0"))
        e.storage.upsert_position("FILUSDT", Decimal("5.57969425"),
                                  Decimal("1.0735"), Decimal("0"))
        out = e._reconcile_wallet_totals(self._wallet(), {})
        self.assertEqual(out, 1)
        self.assertEqual(self._amount(e, "FILIRT"), Decimal("0"),
                         "the Toman copy of a Tether-bought coin must go")
        self.assertEqual(self._amount(e, "FILUSDT"), Decimal("5.57969425"))

    def test_real_toman_buys_keep_the_toman_row(self):
        e = self._engine()
        self._fill(e, "FILIRT", price="212614")
        e.storage.upsert_position("FILIRT", Decimal("5.57969425"),
                                  Decimal("212614"), Decimal("0"))
        e.storage.upsert_position("FILUSDT", Decimal("5.57969425"),
                                  Decimal("1.0735"), Decimal("0"))
        self.assertEqual(e._reconcile_wallet_totals(self._wallet(), {}), 1)
        self.assertEqual(self._amount(e, "FILUSDT"), Decimal("0"))
        self.assertEqual(self._amount(e, "FILIRT"), Decimal("5.57969425"))

    def test_a_genuine_split_inside_the_wallet_is_never_touched(self):
        e = self._engine()
        e.storage.upsert_position("FILIRT", Decimal("3"),
                                  Decimal("212614"), Decimal("0"))
        e.storage.upsert_position("FILUSDT", Decimal("2.57969425"),
                                  Decimal("1.0735"), Decimal("0"))
        self.assertEqual(e._reconcile_wallet_totals(self._wallet(), {}), 0)
        self.assertEqual(self._amount(e, "FILIRT"), Decimal("3"))
        self.assertEqual(self._amount(e, "FILUSDT"), Decimal("2.57969425"))

    def test_rows_bigger_than_the_wallet_are_clamped_not_duplicated(self):
        e = self._engine()
        self._fill(e, "FILUSDT", trade_id="live_f2", price="1.0735",
                   ts="2026-10-09T09:00:00.000Z")
        e.storage.upsert_position("FILIRT", Decimal("5"),
                                  Decimal("212614"), Decimal("0"))
        e.storage.upsert_position("FILUSDT", Decimal("5"),
                                  Decimal("1.0735"), Decimal("0"))
        self.assertEqual(e._reconcile_wallet_totals(self._wallet(), {}), 1)
        self.assertEqual(self._amount(e, "FILUSDT"), Decimal("5"))
        # فقط ۰٫۵۷۹۶۹۴۲۵ سکه برای ردیف دیگر مانده — نه ۵ تای تکراری
        self.assertEqual(self._amount(e, "FILIRT"), Decimal("0.57969425"))

    def test_zeroed_row_takes_its_exit_plan_with_it(self):
        e = self._engine()
        e.storage.upsert_position("ATOMIRT", Decimal("1.278464"),
                                  Decimal("417100"), Decimal("0"))
        e.storage.upsert_position("ATOMUSDT", Decimal("1.278464"),
                                  Decimal("1.942"), Decimal("0"))
        plans = e.storage.kv_get("exit_plans", {}) or {}
        plans["ATOMIRT"] = {"stop": 400000.0, "stage": 0}
        plans["ATOMUSDT"] = {"stop": 1.8, "stage": 0}
        e.storage.kv_set("exit_plans", plans)
        self.assertEqual(e._reconcile_wallet_totals(
            self._wallet("ATOM", "1.278464"), {}), 1)
        after = e.storage.kv_get("exit_plans", {}) or {}
        self.assertNotIn("ATOMIRT", after,
                         "a zeroed row must not keep lighting its section")
        self.assertIn("ATOMUSDT", after)

    def test_cycle_cleans_the_duplicate_without_any_wallet_sync(self):
        """حتی در دمو هم یک سکه در دو بخش نمی‌ماند."""
        e = self._engine()
        e.storage.upsert_position("FILIRT", Decimal("5.57969425"),
                                  Decimal("212614"), Decimal("0"))
        e.storage.upsert_position("FILUSDT", Decimal("5.57969425"),
                                  Decimal("1.0735"), Decimal("0"))
        e.portfolio.balances["FIL"] = {
            "available": Decimal("5.57969425"), "locked": Decimal("0")}
        e.run_cycle()
        held = sorted(p["market"] for p in e.storage.positions()
                      if Decimal(str(p["amount"])) > 0 and
                      p["market"].startswith("FIL"))
        self.assertEqual(held, ["FILUSDT"])
        self.assertFalse(e.status()["market_sections"]["IRT"]["active"])


class TestCorruptEntryIsRepairedFromOurOwnFills(TempCase):
    """«مبلغ خرید» و «سود و زیان این بازار» باید همان پولی باشد که
    داده‌ایم — نه عددی بیگانه با دفتر.

    Live account truth (2026-10-10): SUSDT با میانگین خریدِ
    ۱۶٫۵۱۲۰۹…$ در دفتر مانده بود، در حالی که همهٔ فیل‌های همان سکه
    حدود ۰٫۰۴$ بودند. نتیجه: «مبلغ خرید» ‎$۲۳۵ برای ۱۴٫۲۵ سکه‌ای که
    ‎$۰٫۶۱ ارزش داشت و «سود و زیان این بازار» ‎-۲۳۳٫۵۷$ — درست کنار
    ردیفی که «سود از خرید: +۱٫۰۹٪» نشان می‌داد. همان عدد خراب در فروش
    هم یک «زیان محقق‌شدهٔ» جعلی می‌ساخت، پس از فیل‌های خودمان تعمیر
    می‌شود (نه اینکه فقط نمایش عوض شود).
    """

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _fill(self, e, market, trade_id, side, price, amount, ts=TS):
        e.storage.save_fill({
            "trade_id": trade_id, "order_id": "o-" + trade_id,
            "decision_id": "", "market": market, "side": side,
            "price": price, "amount": amount,
            "gross_value": str(Decimal(price) * Decimal(amount)),
            "fee_amount": "0", "fee_asset": "USDT", "timestamp": ts})

    def _susdt(self, e):
        """همان ردیف و همان فیل‌های واقعی حساب کاربر."""
        e.storage.upsert_position(
            "SUSDT", Decimal("14.2522745"),
            Decimal("16.51209158303614230955885535"), Decimal("0"),
            last_buy_at="2026-10-09T04:17:41.403Z",
            total_buys_irt=Decimal("5483.653388017848370232199903"),
            last_buy_price=Decimal("0.04234"))
        self._fill(e, "SUSDT", "s-buy1", "BUY", "0.04061", "56.79",
                   "2026-10-08T07:46:14.654Z")
        self._fill(e, "SUSDT", "s-buy2", "BUY", "0.04234", "63.15",
                   "2026-10-09T04:17:41.403Z")
        self._fill(e, "SUSDT", "s-sell1", "SELL", "0.04396", "56.97",
                   "2026-10-10T07:35:48.540Z")

    def test_broken_mean_is_rebuilt_from_the_buys(self):
        e = self._engine()
        self._susdt(e)
        out = e._repair_position_entry("SUSDT")
        self.assertIsNotNone(out)
        pos = e.storage.position("SUSDT")
        want = (Decimal("56.79") * Decimal("0.04061") +
                Decimal("63.15") * Decimal("0.04234")) / Decimal("119.94")
        self.assertAlmostEqual(float(pos["avg_entry"]), float(want),
                               places=8)
        self.assertLess(float(pos["avg_entry"]), 0.05)
        # موجودی و قیمتِ آخرین خرید و تایمر دست‌نخورده می‌مانند
        self.assertEqual(Decimal(str(pos["amount"])), Decimal("14.2522745"))
        self.assertEqual(Decimal(str(pos["last_buy_price"])),
                         Decimal("0.04234"))
        # «مجموع خرید»ی که ۱۱۰۰ برابر واقعیت بود هم بازسازی می‌شود
        self.assertLess(float(pos["total_buys_irt"]), 5.0)

    def test_healthy_mean_is_never_touched(self):
        """میانگینِ سالم (اختلاف کارمزد/تاپ‌آپ) دست‌کاری نمی‌شود."""
        e = self._engine()
        self._susdt(e)
        good = (Decimal("56.79") * Decimal("0.04061") +
                Decimal("63.15") * Decimal("0.04234")) / Decimal("119.94")
        e.storage.upsert_position("SUSDT", Decimal("14.2522745"),
                                  good, Decimal("0"))
        self.assertIsNone(e._repair_position_entry("SUSDT"))
        self.assertEqual(Decimal(str(
            e.storage.position("SUSDT")["avg_entry"])), good)

    def test_a_one_letter_base_never_borrows_another_coin_fills(self):
        """بازار یک‌حرفی مثل SUSDT نباید خریدهای SOLUSDT/STRKUSDT را
        (که query پیشوندی «S%» می‌آورد) در میانگین خودش بریزد."""
        e = self._engine()
        self._fill(e, "SOLUSDT", "x1", "BUY", "200.0", "1",
                   "2026-10-07T00:00:00.000Z")
        self._fill(e, "STRKUSDT", "x2", "BUY", "0.05", "100",
                   "2026-10-07T01:00:00.000Z")
        self._fill(e, "SUSDT", "x3", "BUY", "0.04234", "10",
                   "2026-10-09T04:17:41.403Z")
        avg, _plan, lb_at, cost, lbp = e._reconstruct_entry(
            "SUSDT", Decimal("10"))
        self.assertAlmostEqual(float(avg), 0.04234, places=8)
        self.assertAlmostEqual(float(cost), 0.4234, places=8)
        self.assertAlmostEqual(float(lbp), 0.04234, places=8)
        self.assertEqual(lb_at, "2026-10-09T04:17:41.403Z")

    def test_the_section_total_is_the_sum_of_the_row_money(self):
        """جمع «ارزش − مبلغ خرید» روی ردیف‌ها = «سود و زیان این بازار»."""
        e = self._engine()
        self._susdt(e)
        e._repair_position_entry("SUSDT")
        # یک ردیف دوم و سالم هم به همان بخش اضافه می‌شود
        e.storage.upsert_position("FILUSDT", Decimal("5.578"),
                                  Decimal("1.073581078201576816535265289"),
                                  Decimal("0"),
                                  last_buy_price=Decimal("1.068"))
        prices = {"SUSDT": 0.0428, "FILUSDT": 1.12}
        rows = [e._position_row(e.storage.position(m), prices, 0.0)
                for m in ("SUSDT", "FILUSDT")]
        money = [r["pnl_money"] for r in rows]
        self.assertLess(rows[0]["invested_irt"], 1.0)     # ~۰٫۵۹$ نه ۲۳۵$
        for r in rows:
            self.assertAlmostEqual(r["pnl_money"],
                                   r["value_irt"] - r["invested_irt"],
                                   places=2)
        out = e._holdings_pnl(prices)
        m = out["by_market"]["USDT"]
        self.assertEqual(m["rows"], 2)
        self.assertAlmostEqual(m["pnl"], round(sum(money), 2), places=2)
        self.assertAlmostEqual(m["invested"],
                               round(sum(r["invested_irt"] for r in rows), 2),
                               places=2)
        self.assertGreater(m["pnl"], -1.0)               # هرگز ‎-۲۳۳$ نیست


class TestLastBuyBackfillIsSameCoinOnly(TempCase):
    """«قیمت خرید» هر ردیف فقط از فیل همان سکه پر می‌شود.

    `fills_by_base` یک query پیشوندی است؛ ردیف XIRT (سکهٔ «X») با base
    یک‌حرفی، در استارت بعدی جدیدترین خرید XRPIRT را به‌عنوان «آخرین
    قیمت خرید» خودش می‌گرفت (۳۴۶٬۵۸۴ برای سکهٔ ۱٫۳۷ دلاری) و ردیف را
    ‎-۱۰۰٪ / ‎-۳۵ میلیارد تومان نشان می‌داد. حالا برابری (سکه، واحد پول)
    هم شرط است.
    """

    def _engine(self):
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        return e

    def _fill(self, e, market, trade_id, price, amount, ts=TS):
        e.storage.save_fill({
            "trade_id": trade_id, "order_id": "o-" + trade_id,
            "decision_id": "", "market": market, "side": "BUY",
            "price": price, "amount": amount,
            "gross_value": str(Decimal(price) * Decimal(amount)),
            "fee_amount": "0", "fee_asset": "IRT", "timestamp": ts})

    def test_a_one_letter_base_never_borrows_another_coins_fill(self):
        e = self._engine()
        self._fill(e, "XRPIRT", "live_xrp", "346584.0", "0.39")
        self._fill(e, "XLMIRT", "live_xlm", "52000.0", "2")
        e.storage.upsert_position("XIRT", Decimal("1000"), Decimal("0"),
                                  Decimal("0"))
        e._backfill_last_buy_from_fills()
        p = e.storage.position("XIRT")
        self.assertFalse(p.get("last_buy_at"))
        self.assertIn(str(p.get("last_buy_price")), ("None", "0"))

    def test_it_still_uses_the_coins_own_fill(self):
        e = self._engine()
        self._fill(e, "XRPIRT", "live_xrp", "346584.0", "0.39",
                   "2026-10-25T07:39:40.042Z")
        self._fill(e, "XIRT", "live_x", "1.36", "1000",
                   "2026-10-24T10:00:00.000Z")
        e.storage.upsert_position("XIRT", Decimal("1000"), Decimal("1.36"),
                                  Decimal("0"))
        e._backfill_last_buy_from_fills()
        p = e.storage.position("XIRT")
        self.assertEqual(p.get("last_buy_at"), "2026-10-24T10:00:00.000Z")
        self.assertEqual(Decimal(str(p.get("last_buy_price"))), Decimal("1.36"))


if __name__ == "__main__":
    unittest.main()
