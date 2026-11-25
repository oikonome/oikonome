"""The compliance calendar carries the federal dates a business owes
beside its state ones — the quarterly estimate, the annual return for its
structure and, when a payee crossed the line, the 1099-NEC filing — each
moved past a weekend or a federal holiday, and the estimated-tax reply
names the same day (specs/business-books.md → Deadlines)."""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import compliance, entities, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts


class FederalDatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"fed-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _fed(self, ent, today):
        return {o["title"]: o["due"] for o in compliance.obligations(
            self.conn, ent, today=today) if o["source"] == "federal"}

    def test_a_single_member_llc_owes_the_estimate_and_its_schedule_c_return(self):
        ent = entities.create_entity(
            self.conn, name="Example Works LLC", structure="single_member_llc",
            state="ID", formation_date="2026-03-02")
        fed = self._fed(ent, dt.date(2026, 11, 18))
        # no income-tax rate set: the dates are owed all the same
        self.assertEqual(fed["Federal estimated tax (1040-ES)"], "2027-01-15")
        self.assertEqual(
            fed["Federal return with Schedule C (Form 1040) for 2026"],
            "2027-04-15")

    def test_a_partnership_files_in_march(self):
        ent = entities.create_entity(
            self.conn, name="Example Partners LLC",
            structure="multi_member_llc", formation_date="2025-05-01")
        fed = self._fed(ent, dt.date(2026, 11, 18))
        self.assertEqual(fed["Partnership return (Form 1065) for 2026"],
                         "2027-03-15")

    def test_a_date_on_a_weekend_or_holiday_moves_to_the_next_business_day(self):
        # April 15, 2028 is a Saturday and Emancipation Day (Sunday the
        # 16th) is observed Monday the 17th: the return is due the 18th.
        # January 15, 2028 is a Saturday and the Monday is a holiday.
        ent = entities.create_entity(
            self.conn, name="Example Shop", structure="sole_prop",
            formation_date="2024-01-01")
        fed = self._fed(ent, dt.date(2027, 12, 1))
        self.assertEqual(fed["Federal estimated tax (1040-ES)"], "2028-01-18")
        self.assertEqual(
            fed["Federal return with Schedule C (Form 1040) for 2027"],
            "2028-04-18")
        # D.C. Emancipation Day observed on Friday April 15, 2022
        self.assertEqual(compliance.business_day(dt.date(2022, 4, 15)),
                         dt.date(2022, 4, 18))
        self.assertEqual(selfemploy.quarterly_deadlines(2027)[3],
                         dt.date(2028, 1, 18))

    def test_a_business_not_yet_formed_owes_no_earlier_return(self):
        ent = entities.create_entity(
            self.conn, name="Example New LLC", structure="single_member_llc",
            formation_date="2026-09-01")
        fed = self._fed(ent, dt.date(2026, 2, 10))
        self.assertNotIn(
            "Federal return with Schedule C (Form 1040) for 2025", fed)
        self.assertEqual(
            fed["Federal return with Schedule C (Form 1040) for 2026"],
            "2027-04-15")

    def test_the_1099_filing_shows_once_a_payee_crossed_the_line(self):
        ent = entities.create_entity(
            self.conn, name="Example Build LLC", structure="single_member_llc",
            formation_date="2025-01-01")
        seed_accounts(self.conn)
        entities.assign_account(self.conn, "chk", ent["id"])
        today = dt.date(2027, 1, 5)
        self.assertNotIn("1099-NEC forms for 2026", self._fed(ent, today))
        add_txn(self.conn, dt.date(2026, 6, 1), 2500, "Example Joinery",
                account="chk", primary="GENERAL_SERVICES")
        fed = self._fed(ent, today)
        self.assertEqual(fed["1099-NEC forms for 2026"], "2027-02-01")


if __name__ == "__main__":
    unittest.main()
