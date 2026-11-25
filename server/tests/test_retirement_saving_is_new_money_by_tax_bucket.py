"""Retirement saving is new money, and each account is in its real bucket.

What breaks if these fail: a Roth conversion or a rollover between the
household's own accounts reads as a year of new saving and every
projection turns rosy; a young plan ledger is averaged over calendar
months it barely touched and understates saving; a bank account named
"Traditional Savings" is projected as a 401(k); an annuity is drawn with
a capital-gains haircut; a restored Social Security estimate near the
float ceiling makes the Retirement page fail on every load
(specs/retirement-figures.md).
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import retirement as retire

from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "a-long-test-passphrase"
TODAY = dt.date(2026, 11, 24)


class BucketOf(unittest.TestCase):
    def test_a_bank_account_is_cash_whatever_its_name(self):
        for name in ("Traditional Savings", "Traditional Checking",
                     "Roth Street Credit Union Savings"):
            with self.subTest(name=name):
                self.assertEqual(
                    retire.bucket_of("depository", "savings", name), "cash")
        self.assertEqual(retire.bucket_of("depository", "hsa", "Health"),
                         "td")

    def test_an_ira_held_at_a_bank_is_retirement_money(self):
        # a credit union's IRA share or IRA CD is typed as a bank account
        for name, sub, want in (("Traditional IRA Share", "savings", "td"),
                                ("IRA Savings", "savings", "td"),
                                ("Roth IRA CD", "cd", "roth"),
                                ("Roth Ira Share", "savings", "roth")):
            with self.subTest(name=name):
                self.assertEqual(retire.bucket_of("depository", sub, name),
                                 want)
        # "iras" or "Mirage" are not the word
        self.assertEqual(
            retire.bucket_of("depository", "savings", "Mirage Savings"),
            "cash")

    def test_roth_in_a_name_is_a_word(self):
        self.assertEqual(
            retire.bucket_of("investment", "brokerage", "Brothers Fund"),
            "taxable")
        self.assertEqual(
            retire.bucket_of("investment", None, "My Roth401k"), "roth")
        self.assertEqual(
            retire.bucket_of("investment", None, "Traditional IRA"), "td")

    def test_annuities_and_foreign_pre_tax_plans_are_tax_deferred(self):
        for sub in ("fixed annuity", "variable annuity", "other annuity",
                    "health reimbursement arrangement", "rrsp", "rrif",
                    "lira", "lif", "lrif", "lrsp", "prif", "rlif", "sipp"):
            with self.subTest(sub=sub):
                self.assertEqual(retire.bucket_of("investment", sub, "Plan"),
                                 "td")
        self.assertEqual(retire.bucket_of("investment", "tfsa", "Plan"),
                         "roth")
        self.assertIsNone(retire.bucket_of("investment", "rdsp", "Plan"))


class _Household(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"newmoney-{uuid.uuid4().hex[:8]}@example.dev",
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
            "('ira','it1','Rollover IRA','investment','ira',60000),"
            "('rira','it1','Roth IRA','investment','roth',20000),"
            "('old','it1','Old Workplace 401K','investment','401k',90000),"
            "('sav','it1','Traditional Savings','depository','savings',"
            "15000)")
        write_config(self.conn, birthdate="1980-03-01",
                     ret_wizard_answers=None)

    def tearDown(self):
        self.conn.close()

    def move(self, day, amount, src, dst, src_name="Transfer out",
             dst_name="Transfer in",
             detailed="TRANSFER_IN_ACCOUNT_TRANSFER"):
        add_txn(self.conn, day, amount, src_name, account=src,
                primary="TRANSFER_OUT",
                detailed="TRANSFER_OUT_ACCOUNT_TRANSFER")
        add_txn(self.conn, day + dt.timedelta(days=1), -amount, dst_name,
                account=dst, primary="TRANSFER_IN", detailed=detailed)

    def saving(self):
        return retire.ledger_saving(self.conn, TODAY)


class SavingIsNewMoney(_Household):
    def setUp(self):
        super().setUp()
        # the ledger is older than a year, so the average is over 12 months
        add_txn(self.conn, TODAY - dt.timedelta(days=500), 10.0, "Fee",
                account="ira", primary="TRANSFER_OUT",
                detailed="TRANSFER_OUT_ACCOUNT_TRANSFER")

    def test_a_backdoor_roth_counts_its_contribution_once(self):
        self.move(TODAY - dt.timedelta(days=60), 6000.0, "chk", "ira",
                  detailed="TRANSFER_IN_DEPOSIT")
        self.move(TODAY - dt.timedelta(days=50), 6000.0, "ira", "rira")
        self.assertEqual(self.saving()["monthly"], 500.0)

    def test_a_linked_rollover_is_not_saving(self):
        self.move(TODAY - dt.timedelta(days=90), 150000.0, "old", "ira",
                  detailed="TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS")
        self.assertIsNone(self.saving())

    def test_a_rollover_from_an_unlinked_plan_is_not_saving(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=90), -150000.0,
                "Direct Rollover Deposit", account="ira",
                primary="TRANSFER_IN", detailed="TRANSFER_IN_DEPOSIT")
        add_txn(self.conn, TODAY - dt.timedelta(days=80), -2400.0,
                "Contribution", account="ira", primary="TRANSFER_IN",
                detailed="TRANSFER_IN_DEPOSIT")
        self.assertEqual(self.saving()["monthly"], 200.0)

    def test_saving_routed_through_the_brokerage_is_saving(self):
        # checking -> brokerage -> Roth IRA: the brokerage deposit is not
        # retirement saving, so the Roth deposit is where it is counted
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('brk','it1','Brokerage','investment','brokerage',30000)")
        for m in range(12):
            d = TODAY - dt.timedelta(days=15 + 30 * m)
            self.move(d, 500.0, "chk", "brk")
            self.move(d + dt.timedelta(days=1), 500.0, "brk", "rira",
                      detailed="TRANSFER_IN_DEPOSIT")
        self.assertEqual(self.saving()["monthly"], 500.0)

    def test_an_unrelated_transfer_out_does_not_cancel_a_contribution(self):
        # the contribution's own partner is checking; a same-amount wire
        # out of the brokerage that same week is some other money
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('brk','it1','Brokerage','investment','brokerage',30000)")
        for m in range(12):
            d = TODAY - dt.timedelta(days=15 + 30 * m)
            self.move(d, 500.0, "chk", "rira", detailed="TRANSFER_IN_DEPOSIT")
            add_txn(self.conn, d, 500.0, "Wire out", account="brk",
                    primary="TRANSFER_OUT",
                    detailed="TRANSFER_OUT_ACCOUNT_TRANSFER")
        self.assertEqual(self.saving()["monthly"], 500.0)

    def test_a_move_into_traditional_savings_is_not_retirement_saving(self):
        self.move(TODAY - dt.timedelta(days=30), 1200.0, "chk", "sav")
        self.assertIsNone(self.saving())


class SavingAveragesOverTheDaysCovered(_Household):
    def test_a_young_plan_ledger_is_read_at_its_rate(self):
        # semi-monthly $400 since late June: 10 deposits over ~5 months
        days = [dt.date(2026, 6, 30), dt.date(2026, 7, 15),
                dt.date(2026, 7, 31), dt.date(2026, 8, 15),
                dt.date(2026, 8, 31), dt.date(2026, 9, 15),
                dt.date(2026, 9, 30), dt.date(2026, 10, 15),
                dt.date(2026, 10, 31), dt.date(2026, 11, 15)]
        add_txn(self.conn, dt.date(2026, 6, 26), 5.0, "Plan fee",
                account="old", primary="TRANSFER_OUT",
                detailed="PLAN_FEE")
        for d in days:
            add_txn(self.conn, d, -400.0, "Plan: Contribution",
                    account="old", primary="TRANSFER_IN",
                    detailed="PLAN_CONTRIBUTION")
        got = self.saving()
        # 152 days is ~5 months, so ~$800/mo (calendar months said 6)
        self.assertEqual(got["months"], 5.0)
        self.assertAlmostEqual(got["monthly"], 4000 * 365 / 152 / 12, 2)


    def test_months_is_one_type_at_the_floor(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=39), 1.0, "Plan fee",
                account="old", primary="TRANSFER_OUT", detailed="PLAN_FEE")
        add_txn(self.conn, TODAY - dt.timedelta(days=20), -900.0,
                "Plan: Contribution", account="old", primary="TRANSFER_IN",
                detailed="PLAN_CONTRIBUTION")
        got = self.saving()
        self.assertEqual(got["months"], 3.0)
        self.assertIsInstance(got["months"], float)


class ARestoredEstimateNearTheCeiling(_Household):
    def test_the_page_still_answers(self):
        for sched in ({"67": 1e308}, {"62": 1.5e308}, {"67": 2e12}):
            with self.subTest(sched=sched):
                write_config(self.conn, birthdate="1980-03-01",
                             ret_wizard_answers=None, ss_estimates=sched)
                r = self.client.get(
                    "/api/retirement?age=60&end=75&spend=40000&ssage=70")
                self.assertEqual(r.status_code, 200, r.text[:200])
                self.assertEqual(r.json()["inputs"]["your_ss"], 0)


class APersonsNameIsNotAnIra(unittest.TestCase):
    """A bank account is retirement money for the word IRA as a product
    writes it — in capitals too — never for a person called Ira."""

    NAMES = (
        ("Ira's Checking", "savings", "cash"),
        ("IRA'S CHECKING", "savings", "cash"),
        ("Ira Sample Savings", "savings", "cash"),
        ("IRA SAMPLE SAVINGS", "savings", "cash"),
        ("JOINT CHECKING - IRA", "savings", "cash"),
        ("Ira Share Draft", "savings", "cash"),
        ("Ira Primary Share", "savings", "cash"),
        ("IRA Share", "savings", "td"),
        ("IRA SAVINGS", "savings", "td"),
        ("IRA MONEY MARKET", "money market", "td"),
        ("IRA Money Market", "money market", "td"),
        ("IRA", "cd", "td"),
        ("Traditional IRA Share", "savings", "td"),
        ("SEP IRA", "savings", "td"),
        ("Share Certificate IRA", "cd", "td"),
        ("Ira CD", "cd", "td"),
        ("Roth Ira Cd", "cd", "roth"),
        ("roth ira certificate", "cd", "roth"),
        ("ROTH IRA SAVINGS", "savings", "roth"),
        ("SEP IRA CHECKING", "checking", "cash"),
        ("Traditional Savings", "savings", "cash"),
        ("Miranda's savings", "savings", "cash"),
        ("COVERDELL", "savings", "cash"),
    )

    def test_names(self):
        from oikonome.engine.retirement import bucket_of
        for name, sub, want in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(bucket_of("depository", sub, name), want)

    def test_a_checking_account_is_never_an_ira(self):
        from oikonome.engine.retirement import bucket_of
        for name in ("Roth IRA Checking", "IRA Share Draft", "Ira Share"):
            with self.subTest(name=name):
                self.assertEqual(
                    bucket_of("depository", "checking", name), "cash")


if __name__ == "__main__":
    unittest.main()
