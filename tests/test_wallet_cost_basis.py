"""Exchange wallet cost basis — the Nobitex ledger is the P&L authority.

The wallet page's «سود/زیان» is built from the exchange's own BUY/SELL
history, including manual trades. The local weighted average can survive
a partial sell or a re-buy and then disagree with both the Toman amount
and the percentage (ATOM was locally 433,564 while the exchange's actual
remaining lot cost 447,284 Toman). These tests pin the average-cost
reader and the engine repair path.

Run:  python -m unittest tests.test_wallet_cost_basis -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_contract import TempCase  # noqa: E402
from nobitex_bot.engine import TradingEngine  # noqa: E402
from nobitex_bot.nobitex_api import NobitexAPI  # noqa: E402
from nobitex_bot.utils import canonical_symbol  # noqa: E402


def _tx(tp: str, amount: str, price: str, unit: str, ts: str,
        coin: str = "اتم") -> dict:
    return {
        "id": abs(hash((tp, amount, price, ts))) % 10**9,
        "amount": amount,
        "tp": tp,
        "created_at": ts,
        "description": "%s %s %s به قیمت واحد %s %s" % (
            "خرید" if tp == "buy" else "فروش", amount.lstrip("-"), coin,
            price, unit),
    }


class TestExchangeCostBasis(unittest.TestCase):
    def _api(self, txs: list[dict], balance: str = "1") -> NobitexAPI:
        api = NobitexAPI.__new__(NobitexAPI)
        api.cfg = SimpleNamespace(rls_to_irt_divisor=10)
        api._irt_divisor = 10

        def fake_get(path, params=None):
            if path == "/users/wallets/list":
                return {"status": "ok", "wallets": [
                    {"currency": "atom", "balance": balance,
                     "rialBalance": 1_000_000},
                ]}
            if path == "/users/transactions-history":
                return {"status": "ok", "transactions": txs,
                        "hasNext": False}
            raise AssertionError(path)

        api._get = fake_get
        return api

    def test_average_cost_after_partial_sell_and_rebuy(self):
        api = self._api([
            _tx("buy", "2", "100,000", "تومان", "2026-10-01T10:00:00Z"),
            _tx("sell", "-1", "150,000", "تومان", "2026-10-02T10:00:00Z"),
            _tx("buy", "0.5", "200,000", "تومان", "2026-10-03T10:00:00Z"),
        ], balance="1.5")
        c = api.wallet_cost_bases(usdt_toman=100_000)["ATOM"]
        self.assertAlmostEqual(c["amount"], 1.5)
        self.assertAlmostEqual(c["cost_irt"], 200_000.0)
        self.assertAlmostEqual(c["avg_irt"], 133_333.3333, places=3)
        self.assertEqual(c["quote"], "IRT")
        self.assertTrue(c["complete"])

    def test_a_closed_lot_does_not_taint_the_rebuy_quote(self):
        """ATOM: Tether buys + sell, then a fresh Toman buy.

        Net quantities per quote would leave a Tether balance, but that
        lot was fully sold. The new lot is Toman and must be filed in
        ATOMIRT (the exchange's wallet rialBalance is Toman).
        """
        api = self._api([
            _tx("buy", "1", "2", "تتر", "2026-10-01T10:00:00Z"),
            _tx("sell", "-1", "3,000,000", "تومان",
                "2026-10-02T10:00:00Z"),
            _tx("buy", "0.5", "4,000,000", "تومان",
                "2026-10-03T10:00:00Z"),
        ], balance="0.5")
        c = api.wallet_cost_bases(usdt_toman=100_000)["ATOM"]
        self.assertAlmostEqual(c["amount"], 0.5)
        self.assertAlmostEqual(c["cost_irt"], 2_000_000.0)
        self.assertAlmostEqual(c["avg_irt"], 4_000_000.0)
        self.assertEqual(c["quote"], "IRT")
        self.assertEqual(c["last_quote"], "IRT")

    def test_tether_buy_is_converted_to_toman(self):
        api = self._api([
            _tx("buy", "2", "1.5", "تتر", "2026-10-01T10:00:00Z"),
        ], balance="2")
        c = api.wallet_cost_bases(usdt_toman=200_000)["ATOM"]
        self.assertAlmostEqual(c["cost_irt"], 600_000.0)
        self.assertAlmostEqual(c["avg_irt"], 300_000.0)
        self.assertEqual(c["quote"], "USDT")

    def test_history_is_paginated_before_average_cost(self):
        api = NobitexAPI.__new__(NobitexAPI)
        api.cfg = SimpleNamespace(rls_to_irt_divisor=10)
        api._irt_divisor = 10
        seen_pages = []

        def fake_get(path, params=None):
            if path == "/users/wallets/list":
                return {"status": "ok", "wallets": [
                    {"currency": "atom", "balance": "2",
                     "rialBalance": 500_000},
                ]}
            if path == "/users/transactions-history":
                page = int((params or {}).get("page") or 1)
                seen_pages.append(page)
                if page == 1:
                    return {"status": "ok", "hasNext": True,
                            "transactions": [
                                _tx("buy", "0.5", "100,000", "تومان",
                                    "2026-10-03T10:00:00Z"),
                                _tx("buy", "0.5", "200,000", "تومان",
                                    "2026-10-02T10:00:00Z")]}
                return {"status": "ok", "hasNext": False,
                        "transactions": [
                            _tx("buy", "1", "400,000", "تومان",
                                "2026-10-01T10:00:00Z")]}
            raise AssertionError(path)

        api._get = fake_get
        c = api.wallet_cost_bases(usdt_toman=100_000)["ATOM"]
        self.assertEqual(seen_pages, [1, 2])
        self.assertEqual(c["transactions"], 3)
        self.assertEqual(c["pages"], 2)
        self.assertTrue(c["complete"])
        self.assertAlmostEqual(c["cost_irt"], 550_000.0)
        self.assertAlmostEqual(c["avg_irt"], 275_000.0)


class TestExchangeDailyPnl(unittest.TestCase):
    def test_latest_daily_row_is_converted_from_rial_to_toman(self):
        api = NobitexAPI.__new__(NobitexAPI)
        api.cfg = SimpleNamespace(rls_to_irt_divisor=10)
        api._post = lambda path, body: {"status": "ok", "data": [
            {"report_date": "2026-10-24", "total_profit": "1000000",
             "total_profit_percentage": "1.00",
             "total_balance": "50000000"},
            {"report_date": "2026-10-25", "total_profit": "-414811.6",
             "total_profit_percentage": "-0.72",
             "total_balance": "57217098"},
        ]}
        out = api.portfolio_daily_profit()
        self.assertEqual(out["report_date"], "2026-10-25")
        self.assertAlmostEqual(out["profit_irt"], -41481.16)
        self.assertAlmostEqual(out["pct"], -0.72)
        self.assertAlmostEqual(out["balance_irt"], 5721709.8)


class TestEngineCostBasisRepair(TempCase):
    def _engine(self) -> TradingEngine:
        self.cfg.exchange.mode = "demo"
        self.cfg.exchange.live_trading = False
        e = TradingEngine(self.cfg)
        e.auto_trading = False
        e.run_cycle()
        return e

    def test_exchange_cost_replaces_the_local_average(self):
        e = self._engine()
        e.storage.upsert_position("ATOMIRT", Decimal("0.29949"),
                                  Decimal("433564"), Decimal("0"))
        e._wallet_market_assets = {"ATOMIRT": "ATOM"}
        fixed = e._apply_wallet_cost_bases({
            "ATOM": {
                "amount": 0.29949, "cost_irt": 133957.08516,
                "quote": "IRT", "last_quote": "IRT",
                "last_buy_price_irt": 447284.0,
                "last_buy_at": "2026-10-25T18:24:25Z",
            },
        }, usdt_toman=269_507.0, as_of="2026-10-26T00:00:00Z")
        self.assertEqual(fixed, 1)
        pos = e.storage.position("ATOMIRT")
        self.assertAlmostEqual(float(pos["avg_entry"]), 447284.0, places=4)
        self.assertAlmostEqual(float(pos["last_buy_price"]), 447284.0,
                               places=4)
        e._market_change_24h = lambda market: 1.61
        row = e._position_row(pos, {"ATOMIRT": 445044.0}, 1 / 269_507.0)
        self.assertAlmostEqual(row["invested_exact"], 133957.08516,
                               delta=0.02)
        self.assertAlmostEqual(row["pnl_exact"],
                               0.29949 * 445044.0 - 133957.08516, delta=0.02)
        self.assertLess(row["profit_pct"], 0)
        self.assertEqual(row["ch24_pct"], 1.61)

    def test_a_newer_local_buy_is_not_overwritten_by_an_older_read(self):
        e = self._engine()
        now = datetime.now(timezone.utc)
        e.storage.upsert_position(
            "ATOMIRT", Decimal("0.29949"), Decimal("433564"), Decimal("0"),
            last_buy_at=(now + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"))
        e._wallet_market_assets = {"ATOMIRT": "ATOM"}
        fixed = e._apply_wallet_cost_bases({
            "ATOM": {
                "amount": 0.29949, "cost_irt": 133957.09,
                "quote": "IRT", "last_quote": "IRT",
                "last_buy_at": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
        }, usdt_toman=269_507.0,
            as_of=(now - timedelta(minutes=5)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"))
        self.assertEqual(fixed, 0)
        self.assertAlmostEqual(
            float(e.storage.position("ATOMIRT")["avg_entry"]), 433564.0)

    def test_history_quote_files_the_coin_in_the_real_market(self):
        e = self._engine()
        markets = {"ZECIRT", "ZECUSDT"}
        self.assertEqual(
            e._wallet_market_for("ZEC", Decimal("1"), markets, live={},
                                 hist={"quote": "IRT"}),
            "ZECIRT")
        self.assertEqual(
            e._wallet_market_for("ZEC", Decimal("1"), markets, live={},
                                 hist={"quote": "USDT"}),
            "ZECUSDT")

    def test_exchange_daily_report_is_used_for_the_24h_card(self):
        e = self._engine()
        e._exchange_daily_pnl_cached = lambda: {
            "report_date": "2026-10-25", "profit_irt": -41481.16,
            "pct": -0.72, "balance_irt": 5721709.8,
            "source": "nobitex-portfolio-daily",
        }
        out = e._profit_24h_payload(5_721_562.6, [], 6_030_224.0, True)
        self.assertEqual(out["basis"], "exchange_daily")
        self.assertEqual(out["coverage"], "exact")
        self.assertAlmostEqual(out["pnl_irt"], -41481.16)
        self.assertAlmostEqual(out["pct"], -0.72)
        self.assertEqual(out["report_date"], "2026-10-25")

    def test_incomplete_history_never_overwrites_the_book(self):
        e = self._engine()
        e.storage.upsert_position("ATOMIRT", Decimal("0.29949"),
                                  Decimal("433564"), Decimal("0"))
        e._wallet_market_assets = {"ATOMIRT": "ATOM"}
        fixed = e._apply_wallet_cost_bases({
            "ATOM": {
                "amount": 0.29949, "cost_irt": 133957.08516,
                "quote": "IRT", "complete": False,
            },
        }, usdt_toman=269_507.0)
        self.assertEqual(fixed, 0)
        self.assertAlmostEqual(
            float(e.storage.position("ATOMIRT")["avg_entry"]), 433564.0)

    def test_xempire_alias_points_at_the_real_xirt_market(self):
        self.assertEqual(canonical_symbol("XEMPIREIRT"), "XIRT")
        e = self._engine()
        self.assertEqual(
            e._wallet_market_for("X", Decimal("1"), {"XIRT"}, live={},
                                 hist={"quote": "IRT"}),
            "XIRT")


if __name__ == "__main__":
    unittest.main()
