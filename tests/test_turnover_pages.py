"""Deposit/withdraw turnover must be read across ALL pages.

User report: «الان واریز کردم IRT ولی جزو سود حساب کرده». The exchange
returns 20 deposits per page plus `hasNext`; reading only page 1 dropped
older deposits, so Σdeposits − Σwithdraws read millions of Toman too low
and the gap surfaced as fake profit. These tests lock the paging contract.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from nobitex_bot.nobitex_api import NobitexAPI, NobitexError


def make_api(tmp: str) -> NobitexAPI:
    cfg = SimpleNamespace(
        data_dir=tmp, nobitex_base_url="", nobitex_username="",
        nobitex_password="", nobitex_totp_secret="",
        rls_to_irt_divisor=10, rest_timeout=12)
    return NobitexAPI(cfg)


class TurnoverPaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="turnover_")
        self.api = make_api(self.tmp)

    def test_all_pages_are_merged(self):
        calls = []

        def fake_post(path, body):
            calls.append(dict(body))
            if body.get("page") == 1:
                return {"status": "ok",
                        "deposits": [{"id": "d1", "amount": "100"}],
                        "withdraws": [{"id": "w1", "amount": "50"}],
                        "hasNext": True}
            return {"status": "ok",
                    "deposits": [{"id": "d2", "amount": "200"}],
                    # the API repeats withdrawals on every page
                    "withdraws": [{"id": "w1", "amount": "50"}],
                    "hasNext": False}

        self.api._post = fake_post
        out = self.api.deposits_withdraws()
        self.assertEqual([x["id"] for x in out["deposits"]], ["d1", "d2"])
        self.assertEqual([x["id"] for x in out["withdraws"]], ["w1"])
        self.assertEqual(out["pages"], 2)
        self.assertFalse(out["truncated"])
        self.assertEqual([c.get("page") for c in calls], [1, 2])

    def test_a_server_ignoring_page_cannot_loop_forever(self):
        page = {"status": "ok",
                "deposits": [{"id": "d1", "amount": "100"}],
                "withdraws": [], "hasNext": True}
        self.api._post = lambda path, body: page
        out = self.api.deposits_withdraws(max_pages=5)
        # stopped as soon as a page brought nothing new
        self.assertEqual(out["pages"], 2)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["deposits"]), 1)

    def test_a_failed_later_page_raises_instead_of_truncating_totals(self):
        def fake_post(path, body):
            if body.get("page") == 1:
                return {"status": "ok",
                        "deposits": [{"id": "d1", "amount": "100"}],
                        "withdraws": [], "hasNext": True}
            return {"status": "failed", "message": "boom"}

        self.api._post = fake_post
        with self.assertRaises(NobitexError) as ctx:
            self.api.deposits_withdraws()
        self.assertEqual(ctx.exception.code, "DEPOSITS_LIST_FAILED")

    def test_wallet_filter_is_sent_on_every_page(self):
        calls = []

        def fake_post(path, body):
            calls.append(dict(body))
            page = body.get("page")
            return {"status": "ok",
                    "deposits": ([{"id": "d%d" % page}] if page == 1 else []),
                    "withdraws": [], "hasNext": page == 1}

        self.api._post = fake_post
        self.api.deposits_withdraws(wallet_id="WAL-1")
        self.assertEqual([c.get("wallet") for c in calls], ["WAL-1", "WAL-1"])


if __name__ == "__main__":
    unittest.main()
