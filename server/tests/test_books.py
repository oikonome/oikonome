"""Business books — bucket classification, §195/§248 start-up
deduction calc, and the P&L."""
import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import books, entities

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   TODAY)


class StartupDeductionTests(unittest.TestCase):
    def test_under_5k_fully_immediate(self):
        d = books.startup_deduction(3000)
        self.assertEqual(d["immediate"], 3000.0)
        self.assertEqual(d["amortizable"], 0.0)

    def test_between_caps_amortizes_remainder(self):
        d = books.startup_deduction(20000)
        self.assertEqual(d["immediate"], 5000.0)
        self.assertEqual(d["amortizable"], 15000.0)
        self.assertEqual(d["monthly_amortization"], round(15000 / 180, 2))

    def test_phaseout_above_50k(self):
        # $53,000 total → immediate = 5000 - (53000-50000) = 2000
        d = books.startup_deduction(53000)
        self.assertEqual(d["immediate"], 2000.0)
        self.assertEqual(d["amortizable"], 51000.0)

    def test_phaseout_fully_eliminates_at_55k(self):
        d = books.startup_deduction(60000)
        self.assertEqual(d["immediate"], 0.0)
        self.assertEqual(d["amortizable"], 60000.0)

    def test_boundary_exactly_50k_keeps_full_immediate(self):
        # phaseout begins only once costs EXCEED $50k — at exactly $50k the
        # full $5,000 immediate deduction survives (guards a > vs >= off-by-one)
        d = books.startup_deduction(50000)
        self.assertEqual(d["immediate"], 5000.0)
        self.assertEqual(d["amortizable"], 45000.0)

    def test_boundary_exactly_55k_zeroes_immediate(self):
        # $5k reduced dollar-for-dollar by the $5k excess → exactly 0, floored
        d = books.startup_deduction(55000)
        self.assertEqual(d["immediate"], 0.0)
        self.assertEqual(d["amortizable"], 55000.0)


class DefaultBucketTests(unittest.TestCase):
    def test_before_start_is_startup(self):
        start = dt.date(2026, 7, 1)
        self.assertEqual(books.default_bucket(start, dt.date(2026, 6, 1)),
                         "startup_195")

    def test_on_or_after_start_is_operating(self):
        start = dt.date(2026, 7, 1)
        self.assertEqual(books.default_bucket(start, dt.date(2026, 7, 15)),
                         "operating")

    def test_no_start_date_defaults_operating(self):
        self.assertEqual(books.default_bucket(None, dt.date(2026, 6, 1)),
                         "operating")


class PnlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"bk-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="sole_prop",
            business_start_date="2026-07-01")
        # a business account: all its transactions are business
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('biz','it1','Biz Checking','depository','checking',1000)")
        entities.assign_account(self.conn, "biz", self.ent["id"])

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_classify_and_pnl(self):
        # revenue (money in = negative), operating expense, an org cost pre-open
        add_txn(self.conn, TODAY, -500.0, "CLIENT PAYMENT", account="biz",
                primary="INCOME", txn_id="rev1")
        add_txn(self.conn, TODAY, 80.0, "OFFICE DEPOT", account="biz",
                txn_id="op1")
        add_txn(self.conn, dt.date(2026, 6, 15), 120.00, "SECRETARY OF STATE",
                account="biz", txn_id="org1")
        books.classify(self.conn, "op1", "operating", sched_c_line="Supplies")
        books.classify(self.conn, "org1", "organizational")

        p = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(p["revenue"], 500.0)
        self.assertEqual(p["operating_expenses"], 80.0)
        self.assertEqual(p["net_operating"], 420.0)
        self.assertEqual(p["organizational"], 120.00)
        self.assertEqual(p["operating_by_line"].get("Supplies"), 80.0)
        # org cost feeds the §248 deduction calc
        self.assertEqual(p["organizational_deduction"]["immediate"], 120.00)

    def test_bucket_only_reclassify_keeps_the_confirmed_schedule_c_line(self):
        """`sched_c_line` is an OPTIONAL argument, so the Books tab's
        bucket dropdown sends bucket alone — and EXCLUDED wrote that None over
        a line the user had already confirmed, quietly demoting the row to
        Unclassified on the P&L and in the year-end accountant package."""
        add_txn(self.conn, TODAY, 80.0, "OFFICE DEPOT", account="biz",
                txn_id="keep1")
        books.classify(self.conn, "keep1", "operating", sched_c_line="Supplies",
                       note="box of pens")
        books.classify(self.conn, "keep1", "startup_195")   # bucket only
        row = self.conn.execute(
            "SELECT bucket, sched_c_line, note FROM business_txn_class "
            "WHERE txn_id='keep1'").fetchone()
        self.assertEqual(row["bucket"], "startup_195")      # the ask applied
        self.assertEqual(row["sched_c_line"], "Supplies")   # the rest survived
        self.assertEqual(row["note"], "box of pens")

    def test_entity_transactions_tolerates_null_amount(self):
        # a business-assigned txn with NULL amount must not crash the
        # worksheet (float(None) → TypeError 500); it renders as 0.0.
        add_txn(self.conn, TODAY, None, "AMOUNTLESS", account="biz",
                txn_id="nullamt")
        rows = books.entity_transactions(self.conn, self.ent["id"])
        row = next(r for r in rows if r["id"] == "nullamt")
        self.assertEqual(row["amount"], 0.0)

    def test_reclassified_capital_injection_is_not_revenue(self):
        # The owner's capital transfer into the business account came from
        # the aggregator as TRANSFER_IN; a merchant-wide rule later rewrote
        # its category. Capital is still not revenue — unless the user
        # says INCOME outright.
        add_txn(self.conn, TODAY, -300.0, "CLIENT", account="biz",
                primary="INCOME", txn_id="rev3")
        add_txn(self.conn, TODAY, -5000.0, "VENMO OWNER", account="biz",
                primary="GENERAL_SERVICES", txn_id="cap1")
        self.conn.execute("UPDATE transactions SET category_plaid='TRANSFER_IN', "
                          "category_source='rule_user' WHERE id='cap1'")
        p = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(p["revenue"], 300.0)
        self.conn.execute("UPDATE transactions SET category_override='INCOME' "
                          "WHERE id='cap1'")
        p = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(p["revenue"], 5300.0)

    def test_cc_payment_excluded_from_pnl(self):
        # a credit-card payment (detailed category) must NOT count as revenue
        # or operating expense — it's a double-count, not a business cost
        add_txn(self.conn, TODAY, -300.0, "CLIENT", account="biz",
                primary="INCOME", txn_id="rev2")
        add_txn(self.conn, TODAY, 500.0, "CHASE CARD PMT", account="biz",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", txn_id="ccp")
        p = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(p["revenue"], 300.0)
        self.assertEqual(p["operating_expenses"], 0.0)   # CC pmt excluded
        self.assertEqual(p["net_operating"], 300.0)

    def test_unclassified_pre_start_defaults_to_startup(self):
        # a pre-open cost with NO explicit class defaults to §195 start-up
        add_txn(self.conn, dt.date(2026, 6, 10), 12.00, "DOMAIN REGISTRAR",
                account="biz", txn_id="su1")
        p = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(p["startup_195"], 12.00)
        self.assertEqual(p["operating_expenses"], 0.0)


if __name__ == "__main__":
    unittest.main()


class SchedCSuggestTests(unittest.TestCase):
    """Suggest-and-confirm for Schedule C lines.

    The personal side AUTO-APPLIES a learned merchant category because a
    wrong one is cosmetic. A wrong Schedule C line is a wrong number on a
    filed tax return, so the business side proposes and a human confirms —
    deliberately the opposite behaviour, and the reason this returns
    suggestions instead of writing them.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"sc-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('bizchk','it1','Biz','depository',"
            "'checking',1000)")
        self.ent = entities.create_entity(self.conn, name="Acme LLC",
                                          structure="sole_prop")
        entities.assign_account(self.conn, "bizchk", self.ent["id"])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _suggest(self):
        return {s["txn_id"]: s
                for s in books.suggest_lines(self.conn, self.ent["id"])}

    def test_learns_from_this_entitys_own_prior_decision(self):
        a = add_txn(self.conn, TODAY, 40.0, "AWS", account="bizchk")
        b = add_txn(self.conn, TODAY, 55.0, "AWS", account="bizchk")
        books.classify(self.conn, a, "operating", sched_c_line="Utilities")
        self.conn.commit()
        s = self._suggest()
        self.assertNotIn(a, s, "an already-classified row is not suggested")
        self.assertEqual("Utilities", s[b]["suggested_line"])
        self.assertEqual("merchant", s[b]["source"],
                         "a prior decision by this entity is the strong source")

    def test_falls_back_to_category_with_a_weaker_source(self):
        t = add_txn(self.conn, TODAY, 30.0, "SOME CAFE", account="bizchk",
                    primary="FOOD_AND_DRINK")
        self.conn.commit()
        s = self._suggest()
        self.assertEqual("Meals", s[t]["suggested_line"])
        self.assertEqual("category", s[t]["source"],
                         "a category-derived guess must be marked weak")

    def test_unknown_merchant_and_category_suggests_nothing(self):
        """Silence beats a confident wrong line on a tax form."""
        t = add_txn(self.conn, TODAY, 20.0, "MYSTERY VENDOR",
                    account="bizchk", primary="")
        self.conn.commit()
        s = self._suggest()
        self.assertIsNone(s[t]["suggested_line"])
        self.assertIsNone(s[t]["source"])

    def test_suggesting_writes_nothing(self):
        """The whole point: nothing is classified until a human confirms."""
        add_txn(self.conn, TODAY, 30.0, "SOME CAFE", account="bizchk",
                primary="FOOD_AND_DRINK")
        self.conn.commit()
        self._suggest()
        n = self.conn.execute(
            "SELECT count(*) AS n FROM business_txn_class").fetchone()["n"]
        self.assertEqual(0, n, "suggest_lines wrote a classification")

    def test_suggestion_carries_a_bucket_so_confirm_is_one_action(self):
        """classify() requires a bucket, and the bucket is not a guess — it
        falls out of the entity's start date. Returning it means a confirm
        is one click instead of asking the user to restate a date-derived
        fact."""
        t = add_txn(self.conn, TODAY, 30.0, "SOME CAFE", account="bizchk",
                    primary="FOOD_AND_DRINK")
        self.conn.commit()
        s = self._suggest()[t]
        self.assertIn(s["suggested_bucket"], books.BUCKETS)

    def test_invented_line_is_refused(self):
        """Schedule C Part II is a printed list — an invented line cannot be
        reported, so it must not be storable."""
        t = add_txn(self.conn, TODAY, 10.0, "X", account="bizchk")
        self.conn.commit()
        with self.assertRaises(ValueError):
            books.classify(self.conn, t, "operating",
                           sched_c_line="creative_accounting")


class YearEndPackageTests(unittest.TestCase):
    """One file with everything a CPA asks for.

    Requested as "one button producing P&L, balance sheet, 1099s, mileage
    log and the categorized ledger". Each part already had its own
    endpoint; the failure mode this fixes is not a missing report, it is a
    person exporting four things in March and forgetting the fifth.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        self.client = TestClient(app)
        r = self.client.post("/api/signup", data={
            "email": f"pkg-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            self.ent = entities.create_entity(
                conn, name="Acme LLC", structure="sole_prop")
            conn.commit()
        finally:
            conn.close()

    def test_package_contains_every_report(self):
        import io
        import zipfile
        r = self.client.get(
            f"/api/business/entities/{self.ent['id']}/package.zip")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("application/zip", r.headers["content-type"])
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        for f in ("README.txt", "profit-and-loss.csv", "ledger.csv",
                  "balance-sheet.csv", "vendors-1099.csv", "mileage.csv"):
            self.assertIn(f, names, f"package is missing {f}")

    def test_readme_says_what_this_is_not(self):
        """Bookkeeping output, not a filed return — and unclassified rows
        are called out, because they need a decision before filing."""
        import io
        import zipfile
        r = self.client.get(
            f"/api/business/entities/{self.ent['id']}/package.zip")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        readme = z.read("README.txt").decode()
        self.assertIn("NOT tax advice", readme)
        self.assertIn("Unclassified", readme)

    def test_download_filename_survives_a_non_latin_entity_name(self):
        """`_filename_safe` must not keep every Unicode-aware
        str.isalnum() letter; otherwise Greek/Cyrillic/CJK letters pass
        straight through and raise the latin-1 UnicodeEncodeError the function
        exists to prevent. A business named in a non-Latin script is entirely
        ordinary."""
        from oikonome.web.api import _filename_safe
        for name in ("\u03a9 Consulting", "\u65e5\u672c\u306e\u4f1a\u793e",
                     "\u042f\u043d\u0434\u0435\u043a\u0441", "Caf\u00e9 Ltd"):
            out = _filename_safe(name, "entity")
            # must be encodable as a header value, and never empty
            f'attachment; filename="{out}"'.encode("latin-1")
            self.assertTrue(out)

    def test_unknown_entity_404s(self):
        r = self.client.get(
            "/api/business/entities/00000000-0000-0000-0000-000000000000"
            "/package.zip")
        self.assertEqual(404, r.status_code)

    def test_pnl_line_keys_are_formula_safe(self):
        """An unclassified expense's Schedule-C line IS its
        free-text category_override, used as a dict key and written into
        profit-and-loss.csv — the one file in the ZIP that skipped
        _csv_safe while ledger.csv three lines down guarded the same
        strings. The ZIP is built to be opened by an accountant's Excel."""
        import io
        import zipfile
        payload = "=cmd|'/c calc.exe'!A1"
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, category_override, entity_id, pending, removed)
                   VALUES ('pnl-inj','chk','2026-03-10',120.0,
                           'EVIL VENDOR', %s, %s, 0, 0)""",
                (payload, self.ent["id"]))
            conn.commit()
        finally:
            conn.close()
        r = self.client.get(
            f"/api/business/entities/{self.ent['id']}/package.zip")
        self.assertEqual(200, r.status_code, r.text)
        pnl = zipfile.ZipFile(io.BytesIO(r.content)).read(
            "profit-and-loss.csv").decode()
        self.assertNotIn("\n" + payload, pnl,
                         "a formula-leading line key reached the CSV raw")
        self.assertIn("'" + payload, pnl,
                      "the neutralized (quote-prefixed) key should appear")
