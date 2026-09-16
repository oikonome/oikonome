"""Plan naming carries no personal data in code.

Plan metadata must carry no specific employer's plan names or portal plan
keys — that is somebody's personal data. It is derived generically from
the plan code instead; a tenant labels their own plans
via config `plan_names` (the account row itself can also simply
be renamed — imports never overwrite an existing account row).
"""

import unittest

from oikonome.sync import plan_csv

from .util import make_db, write_config

HEADER = ("Trade Date,Investments,Ticker,Transaction,"
          "Transaction Amount,Share Price,Total shares")

CSV = f"""{HEADER}
07/01/2025,Vanguard 500 Idx,VFIAX,Contribution,"1,000.00",100.00,10.000
"""


class PlanCsvNamingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_plan_metadata_is_generic(self):
        # account ids derive from the provider slug and the plan code; the
        # module carries no plan names and no portal plan keys
        for plan in plan_csv.PLAN_CODES:
            self.assertEqual(plan_csv.account_id("plan", plan), f"plan-{plan.lower()}")
        src = open(plan_csv.__file__).read()
        self.assertNotIn("planKey", src)

    def test_default_names_are_generic(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        acct = self.conn.execute(
            "SELECT name FROM accounts WHERE id='plan-401a'").fetchone()
        self.assertEqual(acct["name"], "Workplace plan 401A")
        txn = self.conn.execute(
            "SELECT name FROM transactions WHERE id LIKE 'plan:%'"
        ).fetchone()
        self.assertTrue(txn["name"].startswith("Workplace plan 401A: Contribution"),
                        txn["name"])

    def test_tenant_config_labels_plans(self):
        write_config(self.conn,
                     plan_names={"401A": "MegaCorp 401A"})
        plan_csv.import_csv(self.conn, "401A", CSV)
        acct = self.conn.execute(
            "SELECT name FROM accounts WHERE id='plan-401a'").fetchone()
        self.assertEqual(acct["name"], "MegaCorp 401A")
        txn = self.conn.execute(
            "SELECT name FROM transactions WHERE id LIKE 'plan:%'"
        ).fetchone()
        self.assertIn("MegaCorp 401A: Contribution", txn["name"])

    def test_401k_plans_are_accepted_too(self):
        res = plan_csv.import_csv(self.conn, "401k", CSV)
        self.assertEqual(res["plan"], "401K")
        acct = self.conn.execute(
            "SELECT name, subtype FROM accounts WHERE id='plan-401k'"
        ).fetchone()
        self.assertEqual(acct["name"], "Workplace plan 401K")
        self.assertEqual(acct["subtype"], "401k")

    def test_existing_account_row_is_never_renamed(self):
        # converter-migrated installs keep whatever their account is called
        self.conn.execute(
            """INSERT INTO items (id, aggregator, institution_name, status)
               VALUES ('plan', 'csv', 'Workplace plan', 'archived')""")
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype)
               VALUES ('plan-401a', 'plan', 'My Employer 401A',
                       'investment', '401a')""")
        plan_csv.import_csv(self.conn, "401A", CSV)
        acct = self.conn.execute(
            "SELECT name FROM accounts WHERE id='plan-401a'").fetchone()
        self.assertEqual(acct["name"], "My Employer 401A")


if __name__ == "__main__":
    unittest.main()
