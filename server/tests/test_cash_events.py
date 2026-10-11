"""budget.cash_events (bonus + investment-funding detection), the
alert sources it feeds (funding/transfer/bonus), the excluded_accounts
budget exclusion, and the maintenance-job + exclude API endpoints."""

import datetime as dt
import unittest
import uuid
from unittest import mock

from oikonome.engine import alerts, budget

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _seed_investment(conn):
    """A brokerage-style investment institution: one investment account +
    the institution's own cash (depository) account."""
    conn.execute("INSERT INTO items (id, aggregator, institution_name, "
                 "access_token) VALUES ('brk','test','Northwind','tok-brk') "
                 "ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                 "balance_current) VALUES "
                 "('brkinv','brk','Northwind Investment','investment','brokerage',90000)"
                 " ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                 "balance_current) VALUES "
                 "('brkcash','brk','Northwind Cash','depository','checking',1500)"
                 " ON CONFLICT (tenant_id, id) DO NOTHING")


class CashEventsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _seed_investment(self.conn)
        add_bill(self.conn, "Acme Payroll", 2600, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=3))

    def tearDown(self):
        self.conn.close()

    def month(self):
        return budget.cash_events(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))

    def test_funding_and_large_split(self):
        # inflow from the investment institution → funding; >= $15k → large
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200,
                "NORTHWIND BROKERAGE XFER", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -20000,
                "Northwind transfer", account="chk")
        ev = self.month()
        self.assertEqual(ev["funding_mtd"],
                         [((TODAY - dt.timedelta(days=3)).isoformat(), 1200.0)])
        self.assertEqual(ev["funding_mtd_total"], 1200.0)
        self.assertEqual(ev["large_mtd"],
                         [((TODAY - dt.timedelta(days=2)).isoformat(), 20000.0)])

    def test_ytd_spans_the_year_and_skips_large(self):
        add_txn(self.conn, TODAY.replace(month=2, day=10), -800,
                "Northwind Brokerage Xfer", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200,
                "NORTHWIND BROKERAGE XFER", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -20000,
                "Northwind transfer", account="chk")   # large: never in ytd
        self.assertEqual(self.month()["funding_ytd_total"], 2000.0)

    def test_bonus_needs_pay_key_and_175x(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=5), -2600,
                "ACME PAYROLL DIRECT DEP", account="chk")   # normal paycheck
        add_txn(self.conn, TODAY - dt.timedelta(days=4), -6000,
                "ACME PAYROLL BONUS", account="chk")        # > 2600 × 1.75
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -6000,
                "Aunt Milly gift", account="chk")           # no pay key
        ev = self.month()
        self.assertEqual(ev["bonus"],
                         [((TODAY - dt.timedelta(days=4)).isoformat(), 6000.0)])

    def test_investment_institutions_own_cash_account_is_invisible(self):
        # interest micro-deposits INTO the institution's depository account
        # must not read as "sold stock to cover spending"
        add_txn(self.conn, TODAY - dt.timedelta(days=1), -0.50,
                "NORTHWIND CASH INTEREST", account="brkcash")
        ev = self.month()
        self.assertEqual(ev["funding_mtd"], [])
        self.assertEqual(ev["funding_ytd_total"], 0)

    def test_no_investment_institution_means_no_funding(self):
        self.conn.execute("UPDATE accounts SET type='depository' "
                          "WHERE id='brkinv'")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200,
                "NORTHWIND BROKERAGE XFER", account="chk")
        self.assertEqual(self.month()["funding_mtd"], [])

    def test_month_status_carries_events(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200,
                "NORTHWIND BROKERAGE XFER", account="chk")
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["events"]["funding_mtd_total"], 1200.0)


class InstitutionMatchIsWholeWordTests(unittest.TestCase):
    """An inflow is investment funding only when its text names the
    investment institution's brand, as whole words. A first-word substring
    test reads a Zelle from a person who shares the institution's first
    name, or a paycheck from an employer whose name contains it, as money
    pulled from investments."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _seed_investment(self.conn)

    def tearDown(self):
        self.conn.close()

    def _institution(self, name):
        self.conn.execute("UPDATE items SET institution_name = %s "
                          "WHERE id = 'brk'", (name,))

    def month(self):
        return budget.cash_events(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))

    def test_person_and_employer_sharing_a_word_are_not_funding(self):
        self._institution("Charles Schwab")
        add_bill(self.conn, "Saint Charles Bakery Payroll", 2600, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=3))
        add_txn(self.conn, TODAY - dt.timedelta(days=5), -300,
                "ZELLE FROM CHARLES WILSON", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=4), -6000,
                "SAINT CHARLES BAKERY PAYROLL BONUS", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200,
                "CHARLES SCHWAB MONEYLINK XFER", account="chk")
        ev = self.month()
        self.assertEqual(ev["funding_mtd"],
                         [((TODAY - dt.timedelta(days=3)).isoformat(), 1200.0)])
        self.assertEqual(ev["funding_mtd_total"], 1200.0)
        # the employer's bonus is still seen as a bonus
        self.assertEqual(ev["bonus"],
                         [((TODAY - dt.timedelta(days=4)).isoformat(), 6000.0)])

    def test_a_bank_containing_the_word_is_not_the_brokerage(self):
        self._institution("Morgan Stanley")
        add_txn(self.conn, TODAY - dt.timedelta(days=4), -900,
                "JPMORGAN CHASE EXT TRNSFR", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1500,
                "MORGAN STANLEY ACH TRANSFER", account="chk")
        ev = self.month()
        self.assertEqual(ev["funding_mtd"],
                         [((TODAY - dt.timedelta(days=3)).isoformat(), 1500.0)])

    def test_generic_words_in_the_institution_name_are_not_required(self):
        # 'Investments' says nothing about who sent the money: the bank text
        # rarely carries it, and it must neither be required nor suffice
        self._institution("Northwind Investments")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200,
                "NORTHWIND MONEYLINE", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -400,
                "GLENWICK INVESTMENTS DIVIDEND", account="chk")
        self.assertEqual(self.month()["funding_mtd_total"], 1200.0)

    def test_payroll_key_is_a_whole_word(self):
        # a bonus is a deposit naming the employer, not one whose text merely
        # contains the employer's word inside another word
        add_bill(self.conn, "Acme Payroll", 2600, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=3))
        add_txn(self.conn, TODAY - dt.timedelta(days=4), -6000,
                "ACMEVILLE LAND SALE", account="chk")
        self.assertEqual(self.month()["bonus"], [])


class InstitutionBrandMatchTests(unittest.TestCase):
    """The brand an institution is recognised by, in both directions.

    Institution names come from the aggregator in their marketing form
    ('Fidelity - NetBenefits', 'Empower Retirement'); the bank prints a
    short descriptor ('FIDELITY INVESTMENTS MONEYLINE', 'EMPOWER TRUST CO').
    Requiring every word of the marketing name misses the real transfer, so
    the alert reads $0 while the household draws down investments. Accepting
    any single word reads a Zelle from a person named Charles as stock sold,
    or 'TEMPLETON HARDWARE' as a Franklin Templeton withdrawal. A brand is matched
    on its full name (any order), on a curated brand alias such as
    'schwab', or — when every word in it is a given name or a generic
    word — on its phrase as written."""

    # (institution name as the aggregator reports it, bank descriptor)
    MUST_HIT = [
        ("Charles Schwab", "SCHWAB BROKERAGE MONEYLINK"),
        ("Charles Schwab", "Schwab Brokerage DES:MONEYLINK"),
        ("Fidelity - NetBenefits", "FIDELITY INVESTMENTS MONEYLINE"),
        ("Empower Retirement", "EMPOWER TRUST CO DES:DISTRIB"),
        ("E*TRADE", "ETRADE SECURITIES"),
        ("E*TRADE", "E*TRADE ACH TRANSFER"),
        ("E*TRADE from Morgan Stanley", "ETRADE SECURITIES"),
        ("E*TRADE from Morgan Stanley", "MORGAN STANLEY ACH TRANSFER"),
        ("Raymond James Client Access", "RAYMOND JAMES & ASSOC"),
        ("Morgan Stanley", "MORGAN STANLEY ACH TRANSFER"),
        ("Edward Jones", "EDWARD JONES INVESTMENTS"),
        ("Franklin Templeton", "FRANKLIN TEMPLETON ACH TRANSFER"),
        ("Northwestern Mutual", "NORTHWESTERN MUTUAL WITHDRAWAL"),
        ("M1 Finance", "M1 FINANCE TRANSFER"),
    ]
    MUST_MISS = [
        ("Charles Schwab", "ZELLE FROM CHARLES WILSON"),
        ("Charles Schwab", "ST CHARLES CANDLE WORKS PAYROLL"),
        ("E*TRADE from Morgan Stanley", "ZELLE FROM MORGAN LEE"),
        ("E*TRADE from Morgan Stanley", "STANLEY HARDWARE REFUND"),
        ("E*TRADE", "TRADE SCHOOL TUITION REFUND"),
        ("Raymond James Client Access", "RAYMOND SMITH"),
        ("Raymond James Client Access", "JAMES AUTO REPAIR"),
        ("Raymond James Client Access", "ZELLE FROM JAMES RAYMOND"),
        ("Edward Jones", "ZELLE FROM EDWARD KIM"),
        ("Fidelity - NetBenefits", "NETFLIX REFUND"),
        ("Empower Retirement", "RETIREMENT PARTY GIFT"),
        ("Franklin Templeton", "TEMPLETON HARDWARE"),
        ("Franklin Templeton", "ZELLE FROM OCTAVIA TEMPLETON"),
        ("Northwestern Mutual", "MUTUAL AID SOCIETY"),
        ("Northwestern Mutual", "NORTHWESTERN RAILWAY DIR DEP"),
        ("M1 Finance", "FINANCE CHARGE"),
    ]

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _seed_investment(self.conn)

    def tearDown(self):
        self.conn.close()

    def _funding(self, institution, text):
        self.conn.execute("UPDATE items SET institution_name = %s "
                          "WHERE id = 'brk'", (institution,))
        self.conn.execute("DELETE FROM transactions")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200, text,
                account="chk")
        return budget.cash_events(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))

    def test_real_descriptors_of_the_brand_are_funding(self):
        for inst, text in self.MUST_HIT:
            with self.subTest(institution=inst, text=text):
                ev = self._funding(inst, text)
                self.assertEqual(ev["funding_mtd_total"], 1200.0)
                self.assertEqual(ev["funding_ytd_total"], 1200.0)

    def test_people_employers_and_other_banks_are_not_funding(self):
        for inst, text in self.MUST_MISS:
            with self.subTest(institution=inst, text=text):
                ev = self._funding(inst, text)
                self.assertEqual(ev["funding_mtd_total"], 0)
                self.assertEqual(ev["funding_ytd_total"], 0)

    def test_own_cash_account_follows_the_same_brand(self):
        # the institution's own sweep account carries the institution's
        # name; its interest is never funding, however the name is spelled
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('swp','test','Schwab Bank',"
            "'tok-swp')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('swpcash','swp','Sweep',"
            "'depository','checking',100)")
        self.conn.execute("UPDATE items SET institution_name = "
                          "'Charles Schwab' WHERE id = 'brk'")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), -0.5,
                "SCHWAB BANK INTEREST", account="swpcash")
        ev = budget.cash_events(self.conn, TODAY.replace(day=1),
                                TODAY + dt.timedelta(days=1))
        self.assertEqual(ev["funding_mtd"], [])

    def test_payroll_key_skips_a_given_name(self):
        # an employer whose name leads with a given name is recognised by
        # its distinctive word, and a Zelle from someone with that first
        # name is not a bonus
        add_bill(self.conn, "Charles Riverbend Labs Payroll", 2600,
                 income=True, frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=3))
        add_txn(self.conn, TODAY - dt.timedelta(days=5), -6000,
                "ZELLE FROM CHARLES WILSON", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=4), -6000,
                "RIVERBEND LABS PAYROLL BONUS", account="chk")
        ev = budget.cash_events(self.conn, TODAY.replace(day=1),
                                TODAY + dt.timedelta(days=1))
        self.assertEqual(ev["bonus"],
                         [((TODAY - dt.timedelta(days=4)).isoformat(), 6000.0)])


class CashEventAlertTests(unittest.TestCase):
    def test_build_folds_funding_transfer_bonus(self):
        built = alerts.build({"events": {
            "funding_mtd": [("2026-07-12", 1200.0)],
            "funding_mtd_total": 1200.0, "funding_ytd_total": 2000.0,
            "large_mtd": [("2026-07-13", 20000.0)],
            "bonus": [("2026-07-11", 6000.0)],
        }})
        by_kind = {a["kind"]: a for a in built}
        self.assertEqual(by_kind["funding"]["severity"], "bad")
        self.assertIn("$1,200 pulled from investments", by_kind["funding"]["message"])
        self.assertIn("$2,000 year to date", by_kind["funding"]["message"])
        self.assertEqual(by_kind["transfer"]["severity"], "info")
        self.assertIn("$20,000 on 07/13/26", by_kind["transfer"]["message"])
        self.assertEqual(by_kind["bonus"]["severity"], "info")
        self.assertIn("$6,000", by_kind["bonus"]["message"])
        # worst first: funding (bad) leads
        self.assertEqual(built[0]["kind"], "funding")

    def test_build_without_events_is_unchanged(self):
        self.assertEqual(alerts.build({}), [])


class ExcludedAccountsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_excluded_account_spend_is_invisible_to_the_budget(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 100, "Coffee",
                account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 400, "Gadget",
                account="card")
        write_config(self.conn, excluded_accounts=["card"])
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["variable_actual"], 100.0)
        write_config(self.conn)                       # exclusion removed
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["variable_actual"], 500.0)


class ManageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os

        from .util import _ensure_db, seed_accounts, write_config as _wc
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient

        from oikonome.db import tenancy
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"jobs-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            _wc(conn)
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def test_exclude_toggle_roundtrip(self):
        r = self.client.post("/api/accounts/exclude",
                             json={"account_id": "card", "excluded": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["excluded"])
        self.assertEqual(self.client.get("/api/settings").json()
                         ["excluded_accounts"], ["card"])
        r = self.client.post("/api/accounts/exclude",
                             json={"account_id": "card", "excluded": False})
        self.assertFalse(r.json()["excluded"])
        self.assertIsNone(self.client.get("/api/settings").json()
                          ["excluded_accounts"])

    def test_exclude_unknown_account_404(self):
        r = self.client.post("/api/accounts/exclude",
                             json={"account_id": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_jobs_sync_returns_per_item_results(self):
        # no simplefin/plaid/coinbase items on this tenant → empty result set
        r = self.client.post("/api/jobs/sync", json={})
        self.assertEqual(r.status_code, 200)
        # `started` distinguishes this from "a sync was already running",
        # which also answers an empty result set — see
        # test_sync_door_already_running
        self.assertEqual(r.json(),
                         {"ok": True, "started": True, "results": {}})

    def test_jobs_email_sends_and_reports_subject(self):
        from oikonome.web import report
        sent = {}
        def fake_send(subject, plain, html, recipients, sender=None,
                      **kwargs):
            sent["subject"], sent["recipients"] = subject, recipients
        with mock.patch.object(report, "send", side_effect=fake_send):
            r = self.client.post("/api/jobs/email", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["subject"], sent["subject"])
        self.assertIn("budget", r.json()["subject"].lower())
        self.assertTrue(sent["recipients"])

    def test_jobs_email_surfaces_smtp_failure(self):
        from oikonome.web import report
        with mock.patch.object(report, "send",
                               side_effect=OSError("connection refused")):
            r = self.client.post("/api/jobs/email", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("email failed", r.json()["detail"])

    def test_jobs_email_rate_limited(self):
        from oikonome.web import report
        with mock.patch.object(report, "send"):
            for _ in range(3):
                self.client.post("/api/jobs/email", json={})
            r = self.client.post("/api/jobs/email", json={})
        self.assertEqual(r.status_code, 429)
