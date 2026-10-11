"""Every door that writes a category name bounds it, and a bulk set only
stamps a name that already exists.

A category is stored on each row, rule and split it touches and rendered in
every picker, report and email. A bulk set reaches ten thousand rows in one
request, so an unbounded name there is kilobytes per row for the asking,
and a name nobody picked from a list (the bulk pickers only offer the
standard set and the household's own categories) is a typo or a flood. A new
custom name is made on one row, and is bounded there too.
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import data

from .util import _ensure_db, seed_accounts

LONG = "X" * (data.CATEGORY_MAX_LEN + 1)


class CategoryNamesAreBounded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"catlen-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            for i, (cat, over) in enumerate((
                    ("GENERAL_MERCHANDISE", None),
                    ("GENERAL_MERCHANDISE", "Kids activities"),
                    ("GENERAL_MERCHANDISE", None))):
                conn.execute(
                    "INSERT INTO transactions (id, account_id, date, amount,"
                    " name, merchant_name, category_primary,"
                    " category_override, pending, removed, raw)"
                    " VALUES (%s, 'card', %s, 12.5, 'CORNER GROCER',"
                    " 'CORNER GROCER', %s, %s, 0, 0, '{}'::jsonb)",
                    (f"c{i}", dt.date(2025, 3, 1), cat, over))
        finally:
            conn.close()

    def _cats(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return {r["id"]: r["category_override"] for r in conn.execute(
                "SELECT id, category_override FROM transactions").fetchall()}
        finally:
            conn.close()

    def _bulk(self, category):
        return self.client.post("/api/transactions/bulk", json={
            "ids": ["c0", "c2"], "action": "category", "category": category})

    def test_bulk_refuses_a_name_nobody_could_pick(self):
        before = self._cats()
        r = self._bulk("Brand new name")
        self.assertEqual(400, r.status_code, r.text)
        r = self._bulk(LONG)
        self.assertEqual(400, r.status_code, r.text)
        self.assertEqual(before, self._cats())

    def test_bulk_takes_the_standard_set_and_the_households_own(self):
        self.assertEqual(200, self._bulk("FOOD_AND_DRINK").status_code)
        r = self._bulk("Kids activities")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("Kids activities", self._cats()["c2"])

    def test_bulk_flags_need_no_category(self):
        r = self.client.post("/api/transactions/bulk", json={
            "ids": ["c0"], "action": "biz_on", "category": LONG})
        self.assertEqual(200, r.status_code, r.text)

    def test_a_single_row_may_make_a_new_name_but_not_an_endless_one(self):
        r = self.client.post("/api/transactions/c0/category",
                             json={"category": "Brand new name"})
        self.assertEqual(200, r.status_code, r.text)
        r = self.client.post("/api/transactions/c2/category",
                             json={"category": LONG})
        self.assertEqual(400, r.status_code, r.text)
        self.assertIsNone(self._cats()["c2"])

    def test_the_merchant_and_rename_doors_are_bounded_too(self):
        for path, body in (
                ("/api/bills/merchant-category",
                 {"payee": "CORNER GROCER", "category": LONG}),
                ("/api/bills/merchant-category/preview",
                 {"payee": "CORNER GROCER", "category": LONG}),
                ("/api/categories/rename",
                 {"old": "Kids activities", "new": LONG}),
                ("/api/rules/set",
                 {"merchant": "CORNER GROCER", "category": LONG})):
            r = self.client.post(path, json=body)
            self.assertEqual(400, r.status_code, (path, r.text))
        self.assertEqual("Kids activities", self._cats()["c1"])

    def test_a_long_name_the_household_already_uses_still_applies(self):
        """Older versions and imports stored names past today's cap, and
        the pickers keep offering them. The cap is for new names: one the
        household already uses applies through every door."""
        old_long = "Personal care and wellbeing: " + "y" * 60
        self.assertGreater(len(old_long), data.CATEGORY_MAX_LEN)
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE transactions SET category_override=%s "
                         "WHERE id='c1'", (old_long,))
        finally:
            conn.close()
        r = self.client.post("/api/transactions/c0/category",
                             json={"category": old_long})
        self.assertEqual(200, r.status_code, r.text)
        r = self._bulk(old_long)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(old_long, self._cats()["c2"])
        for path, body in (
                ("/api/bills/merchant-category/preview",
                 {"payee": "CORNER GROCER", "category": old_long}),
                ("/api/rules/set",
                 {"merchant": "CORNER GROCER", "category": old_long})):
            r = self.client.post(path, json=body)
            self.assertEqual(200, r.status_code, (path, r.text))


if __name__ == "__main__":
    unittest.main()
