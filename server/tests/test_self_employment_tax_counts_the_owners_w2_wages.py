"""Social Security on self-employment earnings starts where the owner's
W-2 wages stop (specs/business-books.md, "Social Security after the
owner's W-2 wages").

Schedule SE charges the 12.4% only on the part of the year's wage base the
owner's own W-2 Social Security wages left open. Ignoring those wages
charges Social Security a second time on a W-2 earner's side business;
netting the wrong person's wages charges too little. So each business says
whose it is (primary earner, spouse, or not set), only that person's W-2s
count, only their box 3, and a business that does not say nets nothing.
"""
import datetime as dt
import json
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import entities, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts

PROFIT = 60_000.0
SE_BASE = round(PROFIT * 0.9235, 2)
MEDICARE_ONLY = round(SE_BASE * 0.029, 2)
UNCAPPED = round(SE_BASE * 0.124 + MEDICARE_ONLY, 2)
# 2026's base is 184,500: wages of 160,000 leave 24,500 open
PART_OPEN = round(24_500 * 0.124 + MEDICARE_ONLY, 2)
NO_W2 = ("This business is set as {}'s, but no 2026 W-2 of theirs is on "
         "file yet, so no W-2 wages are counted and the estimate may be "
         "high. Wages count once the year's W-2 is added.")
NO_W2_PRIMARY = NO_W2.format("the primary earner")
NO_W2_SPOUSE = NO_W2.format("the spouse")
FOLDED = ("2 W-2s with the same box 1 and no EIN telling them apart are "
          "counted once: the same form stored twice must not count twice.")
PRIMARY_UNPROVEN = (
    "This business is set as the primary earner's, but their 2026 W-2 "
    "wages are not counted: a W-2 filed under the primary earner cannot yet "
    "be told apart from one an older app filed there without asking whose "
    "it was. The estimate may be high.")


class PureSocialSecurityRoomTests(unittest.TestCase):
    def test_wages_over_the_base_leave_medicare_only(self):
        self.assertEqual(selfemploy._se_tax(SE_BASE, 2026, 210_000.0),
                         MEDICARE_ONLY)

    def test_wages_under_the_base_leave_the_rest_of_it_open(self):
        self.assertEqual(selfemploy._se_tax(SE_BASE, 2026, 160_000.0),
                         PART_OPEN)

    def test_no_wages_is_the_old_figure(self):
        self.assertEqual(selfemploy._se_tax(SE_BASE, 2026), UNCAPPED)


class OwnerWagesInTheEstimateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"sew-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Side Shop LLC", structure="single_member_llc")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('biz','it1','Biz Checking',"
            "'depository','checking',0)")
        entities.assign_account(self.conn, "biz", self.ent["id"])
        add_txn(self.conn, dt.date(2026, 3, 1), -PROFIT, "CLIENT",
                account="biz", primary="INCOME", txn_id="rev")
        self._n = 0

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _owner(self, owner):
        entities.update_entity(self.conn, self.ent["id"], owner=owner)

    def _w2(self, owner, boxes, year=2026, payer="Employer", last4=None):
        self._n += 1
        self.conn.execute(
            "INSERT INTO income_documents (id, year, form, owner, payer,"
            " ein_last4, primary_amount, amounts) VALUES (%s,%s,'W-2',%s,"
            "%s,%s,%s,%s)", (self._n, year, owner, payer, last4,
                             boxes.get("1"), json.dumps(boxes)))

    def _est(self):
        return selfemploy.estimated_tax(self.conn, self.ent["id"], 2026,
                                        today=dt.date(2026, 7, 15))

    def test_a_new_business_does_not_say_whose_it_is(self):
        self.assertEqual(self.ent["owner"], "unknown")
        self.assertEqual([o["value"] for o in self.ent["owner_options"]],
                         ["primary", "spouse", "unknown"])

    def test_the_owners_wages_close_the_base(self):
        self._owner("spouse")
        self._w2("spouse", {"1": 195_000.0, "3": 210_000.0, "5": 210_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], MEDICARE_ONLY)
        self.assertEqual(
            est["ss_wages_note"],
            "This business is set as the spouse's: their 2026 W-2 "
            "Social Security wages of $210,000 already reach the $184,500 Social Security "
            "wage base, so the profit is charged Medicare only.")

    def test_wages_short_of_the_base_leave_its_rest_open(self):
        self._owner("spouse")
        self._w2("spouse", {"1": 160_000.0, "3": 160_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], PART_OPEN)
        self.assertEqual(
            est["ss_wages_note"],
            "This business is set as the spouse's: their 2026 W-2 "
            "Social Security wages of $160,000 leave $24,500 of the $184,500 Social Security "
            "wage base, and only that much of the profit is charged Social "
            "Security.")

    def test_a_spouses_business_nets_the_spouses_wages_only(self):
        self._owner("spouse")
        self._w2("primary", {"1": 210_000.0, "3": 210_000.0})
        self._w2("spouse", {"1": 160_000.0, "3": 160_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], PART_OPEN)
        self.assertTrue(est["ss_wages_note"].startswith(
            "This business is set as the spouse's: their 2026 W-2 Social "
            "Security wages of $160,000"), est["ss_wages_note"])

    def test_a_yearly_wage_figure_is_not_social_security_wages(self):
        """income_annual holds box-1 wages from a return: it never stands
        in for box 3."""
        self._owner("spouse")
        self.conn.execute("INSERT INTO income_annual (year, spouse_wages)"
                          " VALUES (2026, 210000)")
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(est["ss_wages_note"], NO_W2_SPOUSE)

    def test_a_business_not_set_nets_nothing_and_says_where_to_set_it(self):
        self._w2("primary", {"1": 210_000.0, "3": 210_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(
            est["ss_wages_note"],
            "W-2 wages are not counted: this business does not say whose it "
            "is, so the estimate may be high. Set whose business it is in "
            "the business's details.")

    def test_a_box_3_of_zero_is_a_real_zero(self):
        """A job outside Social Security has a real box 1 and no Social
        Security wages: box 1 must not stand in for them."""
        self._owner("spouse")
        self._w2("spouse", {"1": 95_000.0, "3": 0})
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertTrue(est["ss_wages_note"].startswith(
            "This business is set as the spouse's: their 2026 W-2 "
            "Social Security wages of $0 leave $184,500"), est["ss_wages_note"])

    def test_an_unread_box_3_counts_as_none_and_is_said(self):
        self._owner("spouse")
        self._w2("spouse", {"1": 95_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertIn(
            "1 of the spouse's 2026 W-2s has no Social Security "
            "wages (box 3) read, so it counts as none. The estimate may be "
            "high.", est["ss_wages_note"])

    def test_a_w2_nobody_has_claimed_counts_for_nobody_and_is_said(self):
        self._owner("spouse")
        self._w2("unassigned", {"1": 210_000.0, "3": 210_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(
            est["ss_wages_note"],
            NO_W2_SPOUSE + " 1 W-2 for 2026 is not marked whose, so it is "
            "not counted. The estimate may be high.")

    def test_only_last_years_w2_on_file_nets_nothing_and_says_so(self):
        """A year's W-2 arrives the next January, so the year in progress
        has none: an earlier year's wages are never used for it (the job
        may have ended, the base moves), and the note says nothing is
        counted yet."""
        self._owner("spouse")
        self._w2("spouse", {"1": 155_000.0, "3": 160_000.0}, year=2025)
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(est["ss_wages_note"], NO_W2_SPOUSE)

    def test_an_owner_who_left_the_w2_job_is_not_netted_last_years_wages(self):
        """Last year's W-2 from a job that ended: this year's whole SE
        base is open to Social Security, and netting last year's wages
        would read the estimate low."""
        self._owner("spouse")
        self._w2("spouse", {"1": 210_000.0, "3": 184_000.0}, year=2025)
        self.assertEqual(self._est()["se_tax"], UNCAPPED)

    def test_a_finished_year_with_its_own_w2_is_netted(self):
        self._owner("spouse")
        add_txn(self.conn, dt.date(2025, 3, 1), -PROFIT, "CLIENT",
                account="biz", primary="INCOME", txn_id="rev25")
        self._w2("spouse", {"1": 150_000.0, "3": 160_000.0}, year=2025)
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], 2025,
                                       today=dt.date(2026, 7, 15))
        # 2025's base is 176,100: 160,000 leaves 16,100 open
        self.assertEqual(est["se_tax"],
                         round(16_100 * 0.124 + MEDICARE_ONLY, 2))
        self.assertTrue(est["ss_wages_note"].startswith(
            "This business is set as the spouse's: their 2025 W-2 "
            "Social Security wages of $160,000 leave $16,100 of the $176,100"),
            est["ss_wages_note"])

    def test_a_later_years_w2_never_stands_in(self):
        self._owner("spouse")
        self._w2("spouse", {"1": 210_000.0, "3": 210_000.0}, year=2027)
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(est["ss_wages_note"], NO_W2_SPOUSE)

    def test_an_owner_with_no_w2_on_file_is_told_they_will_count(self):
        self._owner("spouse")
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(est["ss_wages_note"], NO_W2_SPOUSE)

    def test_the_same_w2_uploaded_twice_counts_once(self):
        self._owner("spouse")
        for _ in range(2):
            self._w2("spouse", {"1": 75_000.0, "3": 80_000.0},
                     payer="Invented Mills", last4="4410")
        est = self._est()
        # 80,000 once leaves 104,500, more than the SE base: uncapped
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertIn(FOLDED, est["ss_wages_note"])
        self.assertIn("wages of $80,000 ", est["ss_wages_note"])

    def test_the_same_w2_with_its_ein_read_on_one_copy_counts_once(self):
        self._owner("spouse")
        self._w2("spouse", {"1": 75_000.0, "3": 80_000.0},
                 payer="Invented Mills", last4="4410")
        self._w2("spouse", {"1": 75_000.0, "3": 80_000.0},
                 payer="INVENTED MILLS INC", last4=None)
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertIn(FOLDED, est["ss_wages_note"])

    def test_an_absurd_box_3_is_read_as_not_read(self):
        self._owner("spouse")
        self._w2("spouse", {"1": 1.0, "3": 9e14})
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertIn("has no Social Security wages (box 3) read",
                      est["ss_wages_note"])

    def test_a_primary_earners_w2_is_not_netted_it_may_be_a_defaulted_one(self):
        """An app older than the whose-is-it question files every W-2 of a
        year under the primary earner when the year has none yet, a
        spouse's included, and nothing stored tells that W-2 from one a
        person filed there: so a primary earner's W-2 is never netted."""
        self._owner("primary")
        self._w2("primary", {"1": 210_000.0, "3": 210_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(est["ss_wages_note"], PRIMARY_UNPROVEN)

    def test_an_older_apps_spouse_w2_never_closes_the_primarys_base(self):
        from oikonome.engine import taxdocs
        self._owner("primary")
        with self.conn.transaction():
            taxdocs._commit_w2s(self.conn, [{
                "year": 2026, "employer": "Invented Clinic",
                "ein": "12-3456781",
                "boxes": {"1": 150_000.0, "3": 160_000.0}}], [2026])
        self.assertEqual(self.conn.execute(
            "SELECT owner FROM income_documents").fetchone()["owner"],
            "primary")
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(est["ss_wages_note"], PRIMARY_UNPROVEN)

    def test_two_employers_count_twice(self):
        self._owner("spouse")
        self._w2("spouse", {"1": 75_000.0, "3": 80_000.0},
                 payer="Invented Mills", last4="4410")
        self._w2("spouse", {"1": 75_000.0, "3": 80_000.0},
                 payer="Invented Foundry", last4="7720")
        est = self._est()
        self.assertEqual(est["se_tax"], round(
            24_500 * 0.124 + MEDICARE_ONLY, 2))
        self.assertNotIn("counted once", est["ss_wages_note"])

    def test_another_business_of_the_same_owner_is_said_not_combined(self):
        self._owner("spouse")
        other = entities.create_entity(
            self.conn, name="Second Invented Shop", structure="sole_prop",
            owner="spouse")
        self.assertTrue(other["id"])
        self._w2("spouse", {"1": 160_000.0, "3": 160_000.0})
        est = self._est()
        self.assertTrue(est["ss_wages_note"].endswith(
            "1 other business set as the spouse's is figured "
            "separately, so together they may be charged Social Security past "
            "the wage base: the estimate may be high."), est["ss_wages_note"])

    def _partnership(self, pct_a, pct_b):
        self.conn.execute("UPDATE business_entity SET structure ="
                          " 'multi_member_llc' WHERE id = %s",
                          (self.ent["id"],))
        for name, pct, ours in (("Member A", pct_a, True),
                                ("Member B", pct_b, False)):
            self.conn.execute(
                "INSERT INTO entity_membership (entity_id, member_name,"
                " ownership_pct, is_household) VALUES (%s,%s,%s,%s)",
                (self.ent["id"], name, pct, ours))

    def test_a_partnership_nets_the_marked_members_share_only(self):
        self._owner("spouse")
        self._partnership(50, 50)
        self._w2("spouse", {"1": 210_000.0, "3": 210_000.0})
        est = self._est()
        half = round(round(PROFIT / 2, 2) * 0.9235, 2)
        self.assertEqual(est["se_tax"], round(half * 0.029, 2))
        self.assertTrue(est["ss_wages_note"].startswith(
            "The member marked as this household is set as the "
            "spouse: their 2026 W-2 Social Security wages"), est["ss_wages_note"])

    def test_a_partnership_with_nobody_marked_says_so(self):
        self._owner("spouse")
        self.conn.execute("UPDATE business_entity SET structure ="
                          " 'multi_member_llc' WHERE id = %s",
                          (self.ent["id"],))
        self._w2("spouse", {"1": 210_000.0, "3": 210_000.0})
        est = self._est()
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(
            est["ss_wages_note"],
            "The spouse's 2026 W-2 wages are not counted: no member "
            "of this business is marked as this household. The estimate may "
            "be high.")

    def test_a_marked_member_with_incomplete_shares_is_told_the_shares(self):
        self._owner("spouse")
        self._partnership(50, None)
        self._w2("spouse", {"1": 210_000.0, "3": 210_000.0})
        est = self._est()
        self.assertEqual(est["household_unavailable"], "shares_incomplete")
        self.assertEqual(est["se_tax"], UNCAPPED)
        self.assertEqual(
            est["ss_wages_note"],
            "The spouse's 2026 W-2 wages are not counted until "
            "every member has an ownership share and together they make "
            "100%. The estimate may be high.")

    def test_no_w2_anywhere_says_nothing(self):
        self.assertIsNone(self._est()["ss_wages_note"])


class WhoseBusinessDoorTests(unittest.TestCase):
    """The door takes `owner`; a body that does not name it (an older
    client) leaves it as it is."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"whose-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_owner_is_set_kept_and_refused_when_unknown(self):
        r = self.client.post("/api/business/entities", json={
            "name": "Invented Studio", "structure": "sole_prop",
            "owner": "spouse"})
        self.assertEqual(r.status_code, 200, r.text)
        eid = r.json()["id"]
        self.assertEqual(r.json()["owner"], "spouse")
        r = self.client.patch(f"/api/business/entities/{eid}",
                              json={"state": "ID"})
        self.assertEqual(r.json()["owner"], "spouse", "an older client reset it")
        r = self.client.patch(f"/api/business/entities/{eid}",
                              json={"owner": "primary"})
        self.assertEqual(r.json()["owner"], "primary")
        r = self.client.patch(f"/api/business/entities/{eid}",
                              json={"owner": "cousin"})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.client.post("/api/business/entities", json={
            "name": "Invented Shed", "structure": "sole_prop"})
        self.assertEqual(r.json()["owner"], "unknown")
        self.assertEqual(r.json()["owner_options"][0],
                         {"value": "primary", "label": "Primary earner"})


if __name__ == "__main__":
    unittest.main()
