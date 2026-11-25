"""Retirement counts the saving the ledger shows until someone types one.

What breaks if these fail: payroll contributions land in a workplace
plan every month, yet the projection runs "saving nothing" and its
catch-up says "on top of what the plan already saves" (nothing)
(specs/retirement-figures.md).
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "a-long-test-passphrase"


class LedgerSavingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"saving-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def setUp(self):
        self.conn = tenancy.tenant_connect(self.tid)
        self.conn.execute("DELETE FROM transactions")
        self.conn.execute("DELETE FROM accounts")
        seed_accounts(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('plan','it1','Workplace 401A','investment','401a',120000),"
            "('brk','it1','Brokerage','investment','brokerage',50000)")
        write_config(self.conn, birthdate="1980-03-01",
                     ret_wizard_answers=None)
        today = dt.date.today()
        # the plan's ledger is older than the year read, so the average is
        # over twelve months (a younger one is read by the days it covers)
        add_txn(self.conn, today - dt.timedelta(days=500), 5.0,
                "Workplace 401A: Fee", account="plan",
                primary="TRANSFER_OUT", detailed="PLAN_FEE")
        for k in range(12):
            day = today - dt.timedelta(days=15 + 30 * k)
            add_txn(self.conn, day, -425.0, "Workplace 401A: Contribution",
                    account="plan", primary="TRANSFER_IN",
                    detailed="PLAN_CONTRIBUTION")
            # money into a brokerage is not retirement saving
            add_txn(self.conn, day, -900.0, "Deposit", account="brk",
                    primary="TRANSFER_IN",
                    detailed="TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS")
        # nor is a fund exchange inside the plan
        add_txn(self.conn, today - dt.timedelta(days=40), -3000.0,
                "Workplace 401A: Transfer In", account="plan",
                primary="TRANSFER_IN", detailed="PLAN_TRANSFER_IN")

    def tearDown(self):
        self.conn.close()

    def test_the_last_twelve_months_of_contributions_are_the_saving(self):
        inp = self.client.get("/api/retirement?spend=60000").json()["inputs"]
        self.assertEqual((inp["saving_basis"], inp["employer_mo"],
                          inp["saving_mo"], inp["saving_found_mo"]),
                         ("ledger", 400, 400, 400))
        self.assertEqual(inp["saving_yr"], 4800)
        # an older client shows the figure it projects, not "not set"
        self.assertEqual(inp["saving_source"], "saved")

    def test_a_typed_figure_replaces_the_ledgers(self):
        inp = self.client.get(
            "/api/retirement?spend=60000&employer_mo=1000").json()["inputs"]
        self.assertEqual((inp["saving_basis"], inp["employer_mo"],
                          inp["saving_found_mo"]), ("what_if", 1000, 400))

    def test_no_contributions_is_no_saving(self):
        self.conn.execute("DELETE FROM transactions WHERE account_id = 'plan'")
        inp = self.client.get("/api/retirement?spend=60000").json()["inputs"]
        self.assertEqual((inp["saving_basis"], inp["saving_source"],
                          inp["employer_mo"], inp["saving_found_mo"]),
                         ("none", "none", 0, None))

    def test_cash_says_what_card_balances_it_is_net_of(self):
        b = self.client.get("/api/retirement?spend=60000").json()["buckets"]
        # checking $5,000 less the $250 card
        self.assertEqual((b["cash"], b["cards_from_cash"]), (4750.0, 250.0))


if __name__ == "__main__":
    unittest.main()
