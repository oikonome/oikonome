"""A genuine archive from an older install restores, with what cannot be
used left out row by row and reported, never the whole archive refused and
never a value that breaks the app afterwards.

* An amount larger than any real sum (older import doors accepted one) is
  one row left out and counted, not a refused archive.
* Budget amounts in the settings blob are checked the way the settings door
  checks them: a string or a non-finite value there made Today and Budget
  answer 500, with no door to take it back out.
* Two fronted business movements (a contribution and a reimbursement) for
  one transaction, which an install from before that rule can hold, restore
  as the earliest one, whether or not this install has the index that
  enforces the rule, and the restore says it left the other out.
"""
import json
import unittest
import uuid
from pathlib import Path

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import restore

from .test_restore_guards import _zip
from .util import TEST_DB, _admin_dsn, make_db, write_config

MIGRATION = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
             / "migrations" / "160_one_fronted_movement_everywhere.sql")


def _csv_cell(v: str) -> str:
    return '"' + v.replace('"', '""') + '"'


class OversizedAmountsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_one_huge_amount_is_one_row_left_out(self):
        counts = restore.restore_zip(self.conn, _zip({
            "transactions.csv": ("id,account_id,date,amount,name\n"
                                 "big-1,chk,2026-07-01,5e12,BIG\n"
                                 "ok-1,chk,2026-07-01,12.5,OK\n"),
            "amazon_orders.csv": ("dedup_key,date,amount\n"
                                  "a-big,2026-07-01,1e300\n"
                                  "a-ok,2026-07-01,3.5\n")}))
        self.assertEqual(counts.get("transactions"), 1)
        self.assertEqual(counts.get("amazon_orders"), 1)
        self.assertEqual(counts.get("out_of_range"), 2)
        self.assertTrue(any("left out" in n for n in counts["_notes"]))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = 'big-1'").fetchone())

    def test_a_non_finite_amount_still_refuses_the_archive(self):
        """No genuine export writes NaN or infinity: that archive is not
        one, and the restore stays all or nothing for it."""
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, _zip({
                "transactions.csv": ("id,account_id,date,amount,name\n"
                                     "nan-1,chk,2026-07-01,nan,X\n")}))


class BudgetAmountsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _restore(self, cfg):
        return restore.restore_zip(self.conn, _zip({
            "tenant_settings.csv": "config\n" + _csv_cell(json.dumps(cfg))
            + "\n"}))

    def test_unusable_amounts_are_dropped_and_the_pages_render(self):
        counts = self._restore({
            "food_monthly": "lots", "other_monthly": 1e300,
            "budgeted_income_monthly": "NaN",
            "custom_buckets": [{"name": "Coffee", "parent": "food",
                                "monthly": "a lot", "merchants": ["Cafe"]}]})
        cfg = budget.load_config(self.conn)
        # the unusable values are left out, so this instance keeps its own
        for k in ("food_monthly", "other_monthly"):
            self.assertIsInstance(cfg[k], (int, float))
            self.assertLess(cfg[k], 1e12)
        self.assertNotIn("budgeted_income_monthly", cfg)
        self.assertEqual(cfg["custom_buckets"][0]["monthly"], 0.0)
        self.assertEqual(cfg["custom_buckets"][0]["merchants"], ["Cafe"])
        self.assertTrue(any("budget" in n for n in counts["_notes"]))
        import datetime as dt
        budget.month_status(self.conn, dt.date(2026, 7, 15))  # must not raise

    def test_good_amounts_restore_as_given(self):
        self._restore({"food_monthly": "1,200", "other_monthly": 300,
                       "budgeted_income_monthly": 5000,
                       "custom_buckets": [{"name": "Coffee",
                                           "parent": "food",
                                           "monthly": 40}]})
        cfg = budget.load_config(self.conn)
        self.assertEqual((cfg["food_monthly"], cfg["other_monthly"],
                          cfg["budgeted_income_monthly"],
                          cfg["custom_buckets"][0]["monthly"]),
                         (1200.0, 300.0, 5000.0, 40.0))

    def test_a_bool_is_not_an_amount(self):
        self._restore({"food_monthly": True, "other_monthly": 10})
        self.assertNotEqual(budget.load_config(self.conn).get("food_monthly"),
                            True)


class FrontedMovementsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.ent = str(uuid.uuid4())
        self.first, self.second = str(uuid.uuid4()), str(uuid.uuid4())

    def _archive(self):
        rows = [
            # the later one first: the archive's order must not decide
            (self.second, "reimbursement", "2026-07-02 10:00:00+00"),
            (self.first, "contribution", "2026-07-01 10:00:00+00")]
        return _zip({
            "business_entity.csv": ("id,name,structure\n"
                                    f"{self.ent},Example LLC,"
                                    "single_member_llc\n"),
            "transactions.csv": ("id,account_id,date,amount,name\n"
                                 "fr-1,chk,2026-07-01,10,SUPPLIES\n"),
            "equity_movement.csv": (
                "id,entity_id,kind,amount,date,txn_id,created_at\n"
                + "".join(f"{i},{self.ent},{k},10,2026-07-01,fr-1,{at}\n"
                          for i, k, at in rows))})

    def _kept(self):
        return [r["id"] for r in self.conn.execute(
            "SELECT id::text AS id FROM equity_movement WHERE txn_id='fr-1'")]

    def test_the_earliest_is_kept_and_the_other_reported(self):
        counts = restore.restore_zip(self.conn, self._archive())
        self.assertEqual(self._kept(), [self.first])
        self.assertTrue(any("business movement" in n
                            for n in counts["_notes"]))

    def test_the_same_without_the_index(self):
        """An upgraded install may not have the index yet; the restore's
        own check reads the table, so it chooses the same row there."""
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        admin.execute("DROP INDEX IF EXISTS "
                      "equity_movement_one_fronted_per_txn")
        self.addCleanup(admin.execute, MIGRATION.read_text())
        restore.restore_zip(self.conn, self._archive())
        self.assertEqual(self._kept(), [self.first])

    def test_the_booked_business_keeps_its_movement(self):
        """Where the charge is booked to the business of the LATER
        movement, that one is the owner's decision and stays — the choice
        the upgrade makes — and both keep the moment they were recorded."""
        other = str(uuid.uuid4())
        z = _zip({
            "business_entity.csv": (
                "id,name,structure\n"
                f"{self.ent},Example LLC,single_member_llc\n"
                f"{other},Sample Works LLC,single_member_llc\n"),
            "transactions.csv": ("id,account_id,date,amount,name,entity_id\n"
                                 f"fr-1,chk,2026-07-01,10,SUPPLIES,{other}\n"),
            "equity_movement.csv": (
                "id,entity_id,kind,amount,date,txn_id,created_at\n"
                f"{self.first},{self.ent},contribution,10,2026-07-01,fr-1,"
                "2026-07-01 10:00:00+00\n"
                f"{self.second},{other},reimbursement,10,2026-07-01,fr-1,"
                "2026-07-02 10:00:00+00\n")})
        restore.restore_zip(self.conn, z)
        self.assertEqual(self._kept(), [self.second])
        at = self.conn.execute(
            "SELECT created_at::date::text AS d FROM equity_movement"
            " WHERE id=%s", (self.second,)).fetchone()["d"]
        self.assertEqual(at, "2026-07-02")


class MarksAndJournalColumnsTests(unittest.TestCase):
    """A 1099 mark carries the merchant it follows and what a merge folded
    into it; a rename journal row carries the category rules it moved.
    Each is what an undo on the restored instance needs."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_the_new_columns_round_trip(self):
        ent, mid, gone = (str(uuid.uuid4()) for _ in range(3))
        folded = [{"from": "Old Name", "id": gone, "raws": ["old name"],
                   "had_into": False}]
        rules = {"rules": [{"merchant": "old name",
                            "category_primary": "SERVICES",
                            "source": "user", "fate": "moved"}]}
        z = _zip({
            "merchants.csv": f"id,name\n{mid},Example Design Co\n",
            "business_entity.csv": ("id,name,structure\n"
                                    f"{ent},Example LLC,single_member_llc\n"),
            "vendor_1099.csv": (
                "id,entity_id,merchant,reportable,merchant_id,merged_from\n"
                f"{uuid.uuid4()},{ent},Example Design Co,true,{mid},"
                f"{_csv_cell(json.dumps(folded))}\n"
                f"{uuid.uuid4()},{ent},Nobody Here,true,{gone},[]\n"),
            "merchant_renames.csv": (
                "id,raw_merchant,from_canonical,to_canonical,at,rules\n"
                "1,old name,,Example Design Co,2026-07-01 10:00:00+00,"
                f"{_csv_cell(json.dumps(rules))}\n")})
        restore.restore_zip(self.conn, z)
        marks = {r["merchant"]: r for r in self.conn.execute(
            "SELECT merchant, merchant_id::text AS mid, merged_from"
            " FROM vendor_1099").fetchall()}
        self.assertEqual(marks["Example Design Co"]["mid"], mid)
        self.assertEqual(marks["Example Design Co"]["merged_from"], folded)
        # a pointer to a merchant the archive does not carry is dropped
        self.assertIsNone(marks["Nobody Here"]["mid"])
        self.assertEqual(self.conn.execute(
            "SELECT rules FROM merchant_renames").fetchone()["rules"], rules)


if __name__ == "__main__":
    unittest.main()
