"""The household member of a business: one at most, kept by a restore, and
measured against figures on its own footing.

* At most one member of a business is the household. Two writers marking
  two different members at once must leave exactly one mark, not two —
  the estimate and the capital account would otherwise read different
  members' shares.
* An export and restore carries the mark. Without it the estimate quietly
  widens to every member's tax and the capital account loses the
  household's share.
* When the estimate is the household member's share, the profit it is a
  rate of and the estimates already paid toward it are that member's
  share too — the business's own profit would show half the real rate.
* When the estimate covers every member, it says why, so a screen never
  asks for a mark that would change nothing.
"""

import datetime as dt
import os
import pathlib
import threading
import time
import unittest

import psycopg

import oikonome
from oikonome.db import tenancy
from oikonome.engine import entities, equity, selfemploy
from oikonome.sync import export, restore

from .util import TEST_DB, add_txn, make_db, write_config

TAX = "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT"


def _business(conn, structure="multi_member_llc"):
    e = entities.create_entity(conn, name="Two Owners LLC",
                               structure=structure,
                               business_start_date="2030-01-01")
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                 "balance_current) VALUES ('biz','it1','Biz Checking',"
                 "'depository','checking',1000)")
    entities.assign_account(conn, "biz", e["id"])
    return e["id"]


def _marked(conn, eid):
    return [m["member_name"] for m in entities.list_members(conn, eid)
            if m["is_household"]]


class OneHouseholdMemberTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.eid = _business(self.conn)
        self.a = entities.add_member(self.conn, self.eid, member_name="Member A",
                                     ownership_pct=50)
        self.b = entities.add_member(self.conn, self.eid, member_name="Member B",
                                     ownership_pct=50)

    def tearDown(self):
        self.conn.close()

    def _second_connection(self):
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
        return tenancy.tenant_connect(tid, dsn)

    def test_marking_a_member_that_is_not_there_keeps_the_real_mark(self):
        """A stale member id (deleted elsewhere, or another business's)
        answers 'no such member' and changes nothing — the household mark
        the business already has stays where it was."""
        import uuid
        entities.update_member(self.conn, self.eid, self.a["id"],
                               is_household=True)
        out = entities.update_member(self.conn, self.eid, str(uuid.uuid4()),
                                     is_household=True)
        self.assertIsNone(out)
        self.assertEqual(_marked(self.conn, self.eid), ["Member A"])

    def test_two_concurrent_marks_leave_one_household_member(self):
        other = self._second_connection()
        errors = []
        try:
            with self.conn.transaction():
                entities.update_member(self.conn, self.eid, self.a["id"],
                                       is_household=True)

                def mark_b():
                    try:
                        entities.update_member(other, self.eid, self.b["id"],
                                               is_household=True)
                    except Exception as e:          # noqa: BLE001
                        errors.append(e)
                t = threading.Thread(target=mark_b)
                t.start()
                # the second writer runs while the first is still open
                time.sleep(0.6)
            t.join(10)
        finally:
            other.close()
        self.assertEqual(errors, [])
        self.assertEqual(_marked(self.conn, self.eid), ["Member B"])

    def test_the_database_refuses_a_second_household_member(self):
        entities.update_member(self.conn, self.eid, self.a["id"],
                               is_household=True)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "UPDATE entity_membership SET is_household = TRUE "
                "WHERE id = %s", (self.b["id"],))

    def test_the_capital_account_names_the_marked_member(self):
        entities.update_member(self.conn, self.eid, self.a["id"],
                               ownership_pct=60)
        entities.update_member(self.conn, self.eid, self.b["id"],
                               ownership_pct=40, is_household=True)
        cap = equity.capital_summary(self.conn, self.eid)
        self.assertEqual(cap["household_share_pct"], 40.0)


class HouseholdMarkSurvivesARestoreTests(unittest.TestCase):

    def setUp(self):
        self.src = make_db()
        self.dest = make_db()
        write_config(self.src)
        self.eid = _business(self.src)
        self.a = entities.add_member(self.src, self.eid, member_name="Member A",
                                     ownership_pct=60, is_household=True)
        entities.add_member(self.src, self.eid, member_name="Member B",
                            ownership_pct=40)

    def tearDown(self):
        self.src.close()
        self.dest.close()

    def test_the_household_mark_comes_back_from_an_export(self):
        restore.restore_zip(self.dest, export.build_zip(self.src))
        self.assertEqual(_marked(self.dest, self.eid), ["Member A"])
        self.assertEqual(
            equity.capital_summary(self.dest, self.eid)["household_share_pct"],
            60.0)

    def test_a_restore_racing_a_settings_mark_keeps_one_and_completes(self):
        """A restore that brings back a marked member while the Settings
        door is marking another member of the same business must wait for
        that mark and keep it — not insert a second mark the unique index
        then refuses, aborting the whole restore."""
        blob = export.build_zip(self.src)
        # the archive's marked member is new to the destination again
        self.src.execute("DELETE FROM entity_membership WHERE id = %s",
                         (self.a["id"],))
        b = [m for m in entities.list_members(self.src, self.eid)
             if m["member_name"] == "Member B"][0]
        tid = self.src.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
        other = tenancy.tenant_connect(tid, dsn)
        errors = []
        try:
            with other.transaction():
                entities.update_member(other, self.eid, b["id"],
                                       is_household=True)

                def run_restore():
                    try:
                        restore.restore_zip(self.src, blob)
                    except Exception as e:          # noqa: BLE001
                        errors.append(e)
                t = threading.Thread(target=run_restore)
                t.start()
                # the restore reaches the membership rows while the mark
                # is still uncommitted
                time.sleep(1.5)
            t.join(30)
        finally:
            other.close()
        self.assertEqual(errors, [])
        self.assertEqual(_marked(self.src, self.eid), ["Member B"])
        self.assertIn("Member A", [m["member_name"] for m in
                                   entities.list_members(self.src, self.eid)])

    def test_every_member_column_is_read_back(self):
        """A column added to entity_membership and not to restore vanishes
        on every recovery; this names the ones deliberately left out."""
        cols = {r["column_name"] for r in self.src.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'entity_membership'").fetchall()}
        not_restored = {"tenant_id", "created_at"}
        src = (pathlib.Path(oikonome.__file__).parent
               / "sync/restore.py").read_text()
        i = src.index("INSERT INTO entity_membership")
        stmt = src[i:src.index("VALUES", i)]
        self.assertEqual(
            sorted(c for c in cols - not_restored if c not in stmt), [])


class HouseholdShareFootingTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.eid = _business(self.conn)
        entities.update_entity(self.conn, self.eid, income_tax_rate=20)
        add_txn(self.conn, dt.date(2031, 3, 2), -100000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        # the business paid each partner's first estimate
        for i, d in enumerate(("2031-04-14", "2031-04-15")):
            add_txn(self.conn, d, 7000.0, "IRS USATAXPYMT", account="biz",
                    primary="GOVERNMENT_AND_NON_PROFIT", detailed=TAX,
                    txn_id=f"es{i}")

    def tearDown(self):
        self.conn.close()

    def _est(self):
        return selfemploy.estimated_tax(self.conn, self.eid, 2031,
                                        today=dt.date(2033, 3, 1))

    def test_the_household_share_is_set_against_its_own_profit_and_payments(self):
        entities.add_member(self.conn, self.eid, member_name="Member A",
                            ownership_pct=50, is_household=True)
        entities.add_member(self.conn, self.eid, member_name="Member B",
                            ownership_pct=50)
        est = self._est()
        self.assertEqual(est["covers"], "household")
        self.assertEqual(est["covered_profit"], est["net_profit"] / 2)
        self.assertEqual(est["estimated_tax_paid"], 14000.0)
        self.assertEqual(est["covered_tax_paid"], 7000.0)
        # the member's rate on their own share is the business's rate
        self.assertAlmostEqual(
            est["total_estimated"] / est["covered_profit"],
            est["business_total"] / est["net_profit"], places=3)
        self.assertIsNone(est["household_unavailable"])

    def test_the_whole_business_figure_is_set_against_the_whole_business(self):
        est = self._est()
        self.assertEqual(est["covered_profit"], est["net_profit"])
        self.assertEqual(est["covered_tax_paid"], 14000.0)

    def test_an_estimate_for_every_member_says_why(self):
        self.assertEqual(self._est()["household_unavailable"], "none_marked")
        entities.add_member(self.conn, self.eid, member_name="Member A",
                            ownership_pct=50, is_household=True)
        entities.add_member(self.conn, self.eid, member_name="Member B",
                            ownership_pct=45)
        # marked, but the shares do not make 100%: the mark changes nothing
        est = self._est()
        self.assertEqual(est["covers"], "all_members")
        self.assertEqual(est["household_unavailable"], "shares_incomplete")

    def test_an_s_corp_estimate_is_never_the_households_share(self):
        entities.update_entity(self.conn, self.eid, structure="s_corp")
        entities.add_member(self.conn, self.eid, member_name="Member A",
                            ownership_pct=100, is_household=True)
        self.assertEqual(self._est()["household_unavailable"], "s_corp")


if __name__ == "__main__":
    unittest.main()
