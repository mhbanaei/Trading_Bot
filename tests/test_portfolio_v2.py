"""پرتفوی نوبیتکسی — بلوک portfolio_v2 در /api/status + داشبورد.

قراردادهایی که این تست‌ها پین می‌کنند (خواستهٔ کاربر):

* همهٔ اعداد پرتفوی به تومان است: ردیف یک بازار تتری با نرخ زندهٔ
  دلار-تومان تبدیل می‌شود و ردیف بازار تومانی دست‌نخورده می‌ماند — پس
  جمع ستون «ارزش» تومان است، نه دلار (باگ قدیمی: قاطی‌شدن دو واحد)؛
* «سود/زیان ۲۴ساعت» هر سکه از تغییر قیمت ۲۴ساعتِ همان سکه روی مقدار
  خودِ کاربر می‌آید و از کاتالوگ زندهٔ صرافی خوانده می‌شود — بدون
  اسنپ‌شات؛ پس سکه‌ای که در سبد تحلیلی چرخه نیست هم عدد ۲۴ساعت دارد؛
* «سهم از پرتفوی» = ارزش سکه ÷ ارزش کل حساب، و جمع سهم‌ها ۱۰۰٪؛
* هر جمعِ سرور، دقیقاً جمع همان ردیف‌هایی است که UI چاپ می‌کند.

سکه‌های این تست‌ها عمداً بیرون از سبد اسنپ‌شات دمو هستند (ATOMUSDT،
XYZUSDT…) تا قیمت‌شان فقط از کاتالوگ زنده بیاید — همان قولی که پنل
می‌دهد: «بدون نیاز به اسنپ‌شات».

Run:  python -m unittest tests.test_portfolio_v2 -v
"""
from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from tests.test_live_universe import FakeExchange, _row  # noqa: E402
from nobitex_bot.config import PaperCfg  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI  # noqa: E402
from nobitex_bot.utils import utc_now_iso  # noqa: E402
from nobitex_bot.web import STATIC_DIR  # noqa: E402


def catalog(**overrides):
    """کاتالوگ زندهٔ کوچک: نرخ دلار-تومان + بازارهای خواسته‌شده."""
    rows = {"USDTIRT": _row("USDTIRT", 100000.0)}
    rows.update(overrides)
    return rows


class PvCase(TempCase):
    def _engine(self, starting_irt: float = 0.0) -> TradingEngine:
        self.cfg.exchange.mode = "demo"
        self.cfg.exchange.live_trading = False
        self.cfg.paper = PaperCfg(starting_usdt=0.0,
                                  starting_irt=starting_irt)
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()               # اسنپ‌شات چرخه (سبد تحلیلی)
        return e

    def _use_fake(self, e, rows: dict) -> FakeExchange:
        fx = FakeExchange(rows)
        e.exchange = fx             # کاتالوگ زنده دوباره بسته می‌شود
        return fx

    def _plant(self, e, market: str, amount: str, entry: str) -> None:
        e.storage.upsert_position(market, Decimal(amount), Decimal(entry),
                                  Decimal("0"))

    def _pv(self, e) -> dict:
        st = e.status()
        self.assertIn("portfolio_v2", st)
        json.dumps(st["portfolio_v2"])       # JSON-safe برای /api/status
        return st["portfolio_v2"]

    def _snapshot_symbols(self, e) -> set:
        return {m.get("symbol") for m in (e.status()["markets_view"] or [])}

    @staticmethod
    def _row_of(pv: dict, market: str) -> dict:
        return next(r for r in pv["rows"] if r["symbol"] == market)


class TestTomanNormalization(PvCase):
    def test_usdt_market_row_is_valued_in_toman(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 110.0)))
        self._plant(e, "ATOMUSDT", "1", "100")
        pv = self._pv(e)
        rate = pv["toman_per_usdt"]
        self.assertTrue(pv["toman_rate_ok"])
        self.assertGreater(rate, 0)
        r = self._row_of(pv, "ATOMUSDT")
        # قیمت همین ردیف از کاتالوگ زنده آمده، نه از اسنپ‌شات
        self.assertEqual(r["price_usdt"], 110.0)
        # ارزش و مبلغ خرید هر دو در همان واحد تومان‌اند (نه یکی دلار)
        # (نرخ در payload تا ۲ رقم گرد شده، پس اختلاف کمتر از یک تومان)
        self.assertAlmostEqual(r["value_irt"], 110.0 * rate, delta=2)
        self.assertAlmostEqual(r["invested_irt"], 100.0 * rate, delta=2)
        self.assertAlmostEqual(r["pnl_irt"], 10.0 * rate, delta=1)
        self.assertEqual(r["pnl_pct"], 10.0)          # درصد مستقل از واحد
        self.assertAlmostEqual(r["value_usdt"], 110.0, places=2)
        self.assertEqual(r["value_quote"], 110.0)     # عدد بازار خودش هم می‌ماند
        self.assertEqual(pv["unit"], "IRT")

    def test_irt_market_row_is_never_multiplied_by_the_dollar_rate(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMIRT=_row("ATOMIRT", 5000.0)))
        self._plant(e, "ATOMIRT", "2", "4000")
        pv = self._pv(e)
        r = self._row_of(pv, "ATOMIRT")
        self.assertEqual(r["quote"], "IRT")
        self.assertAlmostEqual(r["value_irt"], 10_000.0, places=2)
        self.assertAlmostEqual(r["invested_irt"], 8_000.0, places=2)
        self.assertAlmostEqual(r["pnl_irt"], 2_000.0, places=2)
        self.assertAlmostEqual(pv["coins_value_irt"], 10_000.0, places=2)

    def test_coins_value_is_the_sum_of_the_printed_rows(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 110.0),
                                  XYZUSDT=_row("XYZUSDT", 50.0)))
        self._plant(e, "ATOMUSDT", "1", "100")
        self._plant(e, "XYZUSDT", "3", "60")
        pv = self._pv(e)
        self.assertAlmostEqual(pv["coins_value_irt"],
                               sum(r["value_irt"] for r in pv["rows"]),
                               places=2)
        self.assertAlmostEqual(pv["open_pnl_irt"],
                               sum(r["pnl_irt"] for r in pv["rows"]),
                               places=2)
        self.assertAlmostEqual(pv["invested_irt"],
                               sum(r["invested_irt"] for r in pv["rows"]),
                               places=2)
        self.assertEqual(pv["row_count"], len(pv["rows"]))


class TestTwentyFourHour(PvCase):
    def test_24h_profit_is_the_coin_move_on_my_amount(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 110.0,
                                                change=10.0)))
        self._plant(e, "ATOMUSDT", "1", "100")
        pv = self._pv(e)
        rate = pv["toman_per_usdt"]
        r = self._row_of(pv, "ATOMUSDT")
        # ۲۴ ساعت پیش ۱۰۰ دلار بوده: ۱۰ دلار رشد روی مقدار من
        self.assertEqual(r["ch24_pct"], 10.0)
        self.assertAlmostEqual(r["ch24_irt"], 10.0 * rate, delta=1)
        self.assertAlmostEqual(pv["pnl_24h_irt"], 10.0 * rate, delta=1)
        self.assertEqual(pv["pnl_24h_pct"], 10.0)
        self.assertEqual(pv["rows_24h"], 1)

    def test_a_coin_outside_the_cycle_snapshot_still_gets_its_24h(self):
        """سکه‌ای که در سبد تحلیلی چرخهٔ دمو نیست: قیمت و ۲۴ساعتش از کاتالوگ."""
        e = self._engine()
        self._use_fake(e, catalog(XYZUSDT=_row("XYZUSDT", 33.0, change=-6.0)))
        self._plant(e, "XYZUSDT", "10", "40")
        pv = self._pv(e)
        rate = pv["toman_per_usdt"]
        r = self._row_of(pv, "XYZUSDT")
        self.assertEqual(r["price_usdt"], 33.0)        # فقط کاتالوگ این را دارد
        self.assertAlmostEqual(r["value_irt"], 330.0 * rate, delta=5)
        self.assertEqual(r["ch24_pct"], -6.0)
        # قیمت ۲۴ ساعت پیش ۳۵.۱ بوده؛ افت روی ۱۰ سکه
        self.assertAlmostEqual(r["ch24_irt"], (330.0 - 330.0 / 0.94) * rate,
                               delta=5)
        self.assertLess(pv["pnl_24h_irt"], 0)

    def test_missing_24h_is_excluded_not_counted_as_zero(self):
        e = self._engine()
        rows = catalog(ATOMUSDT=_row("ATOMUSDT", 110.0, change=10.0))
        rows["XYZUSDT"] = {k: v for k, v in _row("XYZUSDT", 10.0).items()
                           if k != "change_24h"}
        self._use_fake(e, rows)
        self._plant(e, "ATOMUSDT", "1", "100")
        self._plant(e, "XYZUSDT", "5", "10")
        pv = self._pv(e)
        self.assertEqual(pv["rows_24h"], 1)
        self.assertEqual(pv["row_count"], 2)
        self.assertAlmostEqual(pv["pnl_24h_irt"], 10.0 * pv["toman_per_usdt"],
                               delta=1)


class TestSharesAndTotals(PvCase):
    def test_weights_are_the_share_of_the_whole_account(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 100.0),
                                  XYZUSDT=_row("XYZUSDT", 100.0)))
        # مثل یک sync واقعی کیف پول: سکه‌ها و تومانِ نقد داخل دفتر می‌آیند
        e.portfolio.overwrite_balances({
            "IRT": {"available": 500_000_000, "locked": 0},
            "ATOM": {"available": 1, "locked": 0},
            "XYZ": {"available": 1, "locked": 0}})
        self._plant(e, "ATOMUSDT", "1", "100")
        self._plant(e, "XYZUSDT", "1", "100")
        pv = self._pv(e)
        self.assertGreater(pv["total_irt"], 0)
        coins = pv["coins_value_irt"]
        self.assertAlmostEqual(coins, 2 * 100.0 * pv["toman_per_usdt"],
                               delta=100)
        for r in pv["rows"]:
            self.assertAlmostEqual(r["weight"], r["value_irt"] / pv["total_irt"],
                                   places=6)
        # سهم سکه‌ها + سهم نقد = کل حساب (۱۰۰٪) — تا یک واحد گردکردن
        total_w = sum(r["weight"] for r in pv["rows"]) + pv["cash_weight"]
        self.assertAlmostEqual(total_w, 1.0, places=3)
        self.assertGreater(pv["cash_irt"], 0)

    def test_rows_are_sorted_by_value_desc(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 100.0),
                                  XYZUSDT=_row("XYZUSDT", 100.0)))
        self._plant(e, "XYZUSDT", "1", "100")
        self._plant(e, "ATOMUSDT", "2", "100")
        pv = self._pv(e)
        self.assertEqual([r["symbol"] for r in pv["rows"]],
                         ["ATOMUSDT", "XYZUSDT"])

    def test_empty_wallet_is_honest_and_does_not_invent_numbers(self):
        e = self._engine()
        self._use_fake(e, catalog())
        pv = self._pv(e)
        self.assertEqual(pv["rows"], [])
        self.assertEqual(pv["row_count"], 0)
        self.assertEqual(pv["pnl_24h_irt"], 0)
        self.assertIsNone(pv["pnl_24h_pct"])
        self.assertIsNone(pv["open_pnl_pct"])
        self.assertIsNone(pv["coins_value_pct"])

    def test_equity_includes_a_coin_the_snapshot_does_not_price(self):
        """کوینِ بیرون اسنپ‌شات هم در «ارزش کل حساب» می‌آید، نه صفر."""
        e = self._engine()
        self._use_fake(e, catalog(XYZUSDT=_row("XYZUSDT", 100.0)))
        e.portfolio.overwrite_balances({
            "IRT": {"available": 10_000_000, "locked": 0},
            "XYZ": {"available": 1, "locked": 0}})
        self._plant(e, "XYZUSDT", "1", "100")
        pv = self._pv(e)
        coins = 100.0 * pv["toman_per_usdt"]
        self.assertAlmostEqual(pv["coins_value_irt"], coins, delta=2)
        self.assertAlmostEqual(pv["total_irt"], 10_000_000 + pv["coins_value_irt"],
                               delta=5)

    def test_total_pnl_matches_the_holdings_pnl_block(self):
        """کارت «سود/زیان کل» و بلوک holdings_pnl نباید دو عدد بدهند."""
        e = self._engine(starting_irt=200_000_000.0)
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 120.0)))
        self._plant(e, "ATOMUSDT", "1", "100")
        st = e.status()
        pv = st["portfolio_v2"]
        self.assertEqual(pv["total_pnl_irt"],
                         st["holdings_pnl"]["profit_capital_irt"])
        self.assertEqual(pv["total_pnl_pct"],
                         st["holdings_pnl"]["growth_pct"])
        self.assertEqual(pv["deposited_irt"],
                         st["holdings_pnl"]["deposited_irt"])
        self.assertEqual(pv["cash_available_irt"],
                         st["portfolio"]["account"]["irt_available"])


class TestRowProfitPercentMatchesTheMoney(PvCase):
    """«سود از خرید» percent = the SAME (ارزش − مبلغ خرید) ÷ مبلغ خرید.

    Live account truth (2026-10-25): XRPIRT showed «+۰٫۳۷٪» next to
    ‎-۱٬۷۷۴ تومان and CVXIRT showed «−۴٫۵۵٪» next to +۱۵٬۵۳۷ تومان — the
    percent read the LAST buy price while the money read the weighted
    average cost. The user's rule: our value minus our purchase amount is
    the profit, and the percent must be that same profit over the same
    purchase amount (a loss can never carry a plus sign).
    """

    def test_a_cheaper_topup_never_turns_a_loss_into_a_plus_percent(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 60.0)))
        # میانگین خرید ۱۰۰، آخرین خرید ۵۰ → قیمت ۶۰ یعنی هنوز زیان
        self._plant(e, "ATOMUSDT", "2", "100")
        e.storage.upsert_position("ATOMUSDT", Decimal("2"),
                                  Decimal("100"), Decimal("0"),
                                  last_buy_price=Decimal("50"))
        row = e._position_row(e.storage.position("ATOMUSDT"),
                              {"ATOMUSDT": 60.0}, 0.0)
        self.assertEqual(row["invested_disp"], 200.0)
        self.assertEqual(row["value_disp"], 120.0)
        self.assertEqual(row["pnl_money"], -80.0)
        self.assertEqual(row["profit_pct"], -40.0)   # نه ‎+۲۰٪
        self.assertEqual(row["profit_pct"] > 0, row["pnl_money"] > 0)

    def test_a_richer_topup_never_turns_a_profit_into_a_negative_percent(self):
        e = self._engine()
        self._use_fake(e, catalog(CVXIRT=_row("CVXIRT", 650000.0)))
        # میانگین خرید ۵۸۰٬۰۰۰، آخرین خرید ۶۸۸٬۰۰۰ → قیمت ۶۵۰٬۰۰۰ یعنی سود
        self._plant(e, "CVXIRT", "0.2", "580000")
        e.storage.upsert_position("CVXIRT", Decimal("0.2"),
                                  Decimal("580000"), Decimal("0"),
                                  last_buy_price=Decimal("688000"))
        row = e._position_row(e.storage.position("CVXIRT"),
                              {"CVXIRT": 650000.0}, 0.0)
        self.assertEqual(row["pnl_money"], 14000.0)
        self.assertEqual(row["profit_pct"], 12.07)   # نه ‎-۵٫۵۲٪
        self.assertEqual(row["profit_pct"] > 0, row["pnl_money"] > 0)

    def test_the_section_total_has_the_same_sign_as_its_rows(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 60.0)))
        self._plant(e, "ATOMUSDT", "2", "100")
        e.storage.upsert_position("ATOMUSDT", Decimal("2"),
                                  Decimal("100"), Decimal("0"),
                                  last_buy_price=Decimal("50"))
        m = e._holdings_pnl({"ATOMUSDT": 60.0})["by_market"]["USDT"]
        self.assertLess(m["pnl"], 0)
        self.assertLess(m["pnl_pct"], 0)


class TestExchangeWalletValuation(PvCase):
    """Toman numbers come from the exchange wallet, not a guessed ticker.

    Live account (2026-10-25): XIRT had no live market and was valued at
    1.38 T while Nobitex's own wallet showed ~5.4 T per unit; the Toman
    profit/value of that row (and the account equity) was therefore wrong.
    These tests pin rialBalance/divisor as the authoritative per-unit
    value, including the USDT-market conversion, and pin the 24h column to
    the coin's market move even for a holding bought seconds ago.
    """

    def _holding(self, st: dict, market: str) -> dict:
        return next(h for h in st["holdings"] if h["symbol"] == market)

    def test_irt_wallet_value_overrides_a_missing_ticker(self):
        e = self._engine()
        self._use_fake(e, catalog())
        e.portfolio.overwrite_balances({
            "IRT": {"available": 1_000, "locked": 0},
            "XIRT": {"available": 100, "locked": 0}})
        # exactly what sync_real_wallet() caches from rialBalance/10
        e._wallet_valuations = {"XIRT": {
            "amount": 100.0, "value_irt": 500.0, "price_irt": 5.0,
            "source": "wallet"}}
        e.portfolio.exchange_valuations_irt = {"IRT": 1_000.0,
                                               "XIRT": 500.0}
        self._plant(e, "XIRT", "100", "0")
        st = e.status()
        h = self._holding(st, "XIRT")
        self.assertEqual(h["price"], 5.0)
        self.assertEqual(h["value_irt"], 500.0)
        self.assertEqual(h["price_source"], "wallet")
        self.assertIsNone(h["profit_pct"])       # cost basis unknown
        self.assertIsNone(h["ch24_pct"])         # no 24h market exists
        self.assertEqual(st["portfolio_v2"]["rows"][0]["value_irt"], 500.0)
        # equity is the exchange wallet total, not the un-priced ticker sum
        self.assertEqual(st["portfolio"]["account"]["equity_irt"], 1_500.0)

    def test_usdt_wallet_value_is_converted_to_exact_toman(self):
        e = self._engine()
        self._use_fake(e, catalog(
            USDTIRT=_row("USDTIRT", 269_507.0),
            ATOMUSDT=_row("ATOMUSDT", 100.0, change=10.0)))
        e.portfolio.overwrite_balances({
            "USDT": {"available": 10, "locked": 0},
            "ATOM": {"available": 1, "locked": 0}})
        e._wallet_valuations = {
            "USDT": {"amount": 10.0, "value_irt": 2_695_070.0,
                     "price_irt": 269_507.0, "source": "wallet"},
            "ATOM": {"amount": 1.0, "value_irt": 26_950_700.0,
                     "price_irt": 26_950_700.0, "source": "wallet"}}
        e.portfolio.exchange_valuations_irt = {
            "USDT": 2_695_070.0, "ATOM": 26_950_700.0}
        self._plant(e, "ATOMUSDT", "1", "100")
        st = e.status()
        pv = st["portfolio_v2"]
        rate = pv["toman_per_usdt"]
        row = pv["rows"][0]
        self.assertEqual(row["quote"], "USDT")
        self.assertEqual(row["value_irt"], 26_950_700.0)
        # the cost is my $100 buy converted at the live Toman rate;
        # the VALUE is the exchange wallet's own rialBalance (exact).
        self.assertAlmostEqual(row["invested_irt"], 100.0 * rate, delta=2)
        self.assertAlmostEqual(row["pnl_irt"],
                               26_950_700.0 - 100.0 * rate, delta=2)
        self.assertEqual(row["ch24_pct"], 10.0)
        self.assertAlmostEqual(row["ch24_irt"],
                               26_950_700.0 - 26_950_700.0 / 1.1,
                               delta=2)
        self.assertEqual(st["portfolio"]["account"]["equity_irt"],
                         29_645_770.0)

    def test_24h_is_the_market_move_even_when_bought_just_now(self):
        e = self._engine()
        self._use_fake(e, catalog(ATOMUSDT=_row("ATOMUSDT", 110.0,
                                                change=10.0)))
        self._plant(e, "ATOMUSDT", "1", "100")
        # bought seconds ago: the old UI showed profit-since-purchase (0%)
        # instead of the coin's 24h move (10%) for exactly this case.
        e.storage.upsert_position(
            "ATOMUSDT", Decimal("1"), Decimal("100"), Decimal("0"),
            last_buy_at=utc_now_iso())
        st = e.status()
        h = self._holding(st, "ATOMUSDT")
        self.assertEqual(h["ch24_pct"], 10.0)
        self.assertAlmostEqual(h["ch24_money"], 110.0 - 110.0 / 1.1,
                               places=2)
        self.assertEqual(h["profit_pct"], 10.0)   # since purchase, separate


class TestAdapterRialBalance(unittest.TestCase):
    """NobitexAPI.portfolio must read rialBalance, not re-price the ticker."""

    def test_wallet_rialbalance_is_the_exact_toman_value(self):
        api = NobitexAPI.__new__(NobitexAPI)
        api.cfg = SimpleNamespace(rls_to_irt_divisor=10)
        api._irt_divisor = 10
        api._get = lambda path, params=None: {"wallets": [
            {"currency": "rls", "activeBalance": "5000000",
             "blockedBalance": "0", "rialBalance": 5000000},
            {"currency": "xirt", "activeBalance": "100",
             "blockedBalance": "0", "rialBalance": 500000},
            {"currency": "usdt", "activeBalance": "0.5",
             "blockedBalance": "0", "rialBalance": 500000},
        ]}
        api._stats_all = lambda: {"usdt-rls": {"latest": "1000000"}}
        doc = api.portfolio()
        by = {a["asset"]: a for a in doc["assets"]}
        self.assertEqual(by["IRT"]["value_irt"], 500_000.0)
        self.assertEqual(by["XIRT"]["value_irt"], 50_000.0)
        self.assertEqual(by["XIRT"]["price_irt"], 500.0)
        self.assertEqual(by["XIRT"]["value_source"], "wallet")
        self.assertEqual(by["USDT"]["value_irt"], 50_000.0)
        self.assertEqual(doc["account"]["equity_irt"], 600_000.0)


class TestDashboardPanel(unittest.TestCase):
    """پنل پرتفوی نوبیتکسی که کاربر می‌بیند (HTML/داشبورد)."""

    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_new_tab_and_its_boxes_exist(self):
        for marker in ('data-tab="netport"', 'id="tab-netport"',
                       'id="pvHero"', 'id="pvTable"', 'id="pvDonut"',
                       'id="pvLegend"', 'id="pvSummary"', 'id="pvHealth"',
                       'id="pvBarMini"', 'id="pvLegendMini"',
                       'id="pvGoTab"', 'data-pvunit="usd"',
                       "function renderPortfolioV2", "function pvHeroCards",
                       "function pvTableHtml", "function pvSummaryHtml",
                       "function pvHealthCards", "function drawDonut",
                       "function pvProfit24Card", "function pvTotalProfitCard",
                       "portfolio_v2"):
            self.assertIn(marker, self.html)

    def test_hero_is_rendered_by_the_poll_loop(self):
        self.assertIn('setBox("#kpis"', self.html)
        self.assertIn('setBox("#pvTable"', self.html)
        self.assertIn('activeTab==="netport"', self.html)
        # هستهٔ پرتفوی از خود payload می‌آید، نه از ردیف‌های اسنپ‌شات
        panel = self.html.split("function pvTableHtml")[1].split("function pvSummaryHtml")[0]
        self.assertNotIn("markets_view", panel)
        self.assertIn("portfolio_v2", panel)

    def test_moved_boxes_live_in_the_new_tab_not_the_overview(self):
        """وضعیت ریسک / کیفیت داده / چرخه‌ها به تب پرتفوی منتقل شده‌اند."""
        health = self.html.split("function pvHealthCards")[1].split("function pvSlices")[0]
        for label in ("وضعیت ریسک", "کیفیت داده", "چرخه‌ها و عملکرد"):
            self.assertIn(label, health)
        # کارت ۲۴ساعتِ مبنای سرمایه هم داخل همین بلوک رسم می‌شود
        self.assertIn("pvProfit24Card(s.profit_24h||{})", health)
        self.assertIn("سود ۲۴ساعت کل حساب", self.html)
        hero = self.html.split("function pvHeroCards")[1].split("function pvHealthCards")[0]
        for label in ("وضعیت ریسک", "کیفیت داده", "ارزش کل (تومان)",
                      "موجودی نقد (تومان)", "چرخه‌ها"):
            self.assertNotIn(label, hero)
        for label in ("ارزش کل حساب", "سود/زیان ۲۴ساعت دارایی‌ها",
                      "سود/زیان کل", "موجودی نقد (قابل خرید)"):
            self.assertIn(label, hero)

    def test_portfolio_tab_needs_no_snapshot_and_keeps_the_eye_and_unit(self):
        # تنظیم چشم/واحد در مرورگر ذخیره می‌شود و در هر دو نما یکی است
        self.assertIn('localStorage.setItem("pvHidden"', self.html)
        self.assertIn('localStorage.setItem("pvUnit"', self.html)
        self.assertIn("pvMoneySigned", self.html)

    def test_holdings_24h_is_the_coin_move_and_shows_the_money(self):
        self.assertIn("ch24_pct", self.html)
        self.assertIn("ch24_money", self.html)
        self.assertIn("p24MoneyFor", self.html)
        self.assertIn("value_exact", self.html)
        # the old «bought <24h -> profit since purchase» branch is gone:
        # it made the ۲۴ساعت column change meaning and jump at 24h.
        self.assertNotIn("age<24&&h.value_irt", self.html)

    def test_theme_is_the_brand_teal(self):
        login = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
        for page in (self.html, login):
            self.assertIn("--accent:#10cf89", page)
            self.assertNotIn("#3b82f6", page)
            self.assertNotIn("--green:#22c55e", page)


if __name__ == "__main__":
    unittest.main()
