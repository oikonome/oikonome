"""A merchant-wide category Undo restores every row of a large merchant.

The held snapshot carries every row the write moved — thousands for a
busy merchant — and the restore runs while the merchant-identity and rule
locks are held, so it is done in a few set-based statements rather than
one per row. These tests pin that the batched restore keeps the per-row
guards: each row gets its own prior category back, a row someone
overrode by hand is untouched, a row re-filed since the write belongs to
that later change, and the count is the rows actually restored."""

import unittest

from oikonome.web import data

from .test_canonical_rules import canon, cat
from .util import make_db

N = 3_000


class MerchantUndoRestoresManyRows(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        canon(self.conn, "HARBOR BAKERY", "Harbor Bakery")
        prior = ("PERSONAL_CARE", "GROCERIES", "SHOPPING")
        rows = [(f"r{i:05d}", "card", f"2025-0{1 + i % 9}-1{i % 10}",
                 5 + i % 40, "HARBOR BAKERY", "HARBOR BAKERY",
                 prior[i % 3]) for i in range(N)]
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, merchant_name, category_primary, pending,
                       removed, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,0,0,'{}')""", rows)
        self.prior = {r[0]: r[6] for r in rows}

    def tearDown(self):
        self.conn.close()

    def _cats(self):
        return {r["id"]: r["category_primary"] for r in self.conn.execute(
            "SELECT id, category_primary FROM transactions").fetchall()}

    def test_every_row_gets_its_own_prior_category_back(self):
        out = data.set_merchant_category(self.conn, "HARBOR BAKERY",
                                         "ENTERTAINMENT")
        self.assertEqual(set(self._cats().values()), {"ENTERTAINMENT"})
        n = data.undo_merchant_category(self.conn, out["undo"])
        self.assertEqual(n, N)
        self.assertEqual(self._cats(), self.prior)

    def test_hand_overrides_and_later_refiles_are_left_alone(self):
        out = data.set_merchant_category(self.conn, "HARBOR BAKERY",
                                         "ENTERTAINMENT")
        self.conn.execute("UPDATE transactions SET category_override="
                          "'TRAVEL' WHERE id='r00007'")
        self.conn.execute("UPDATE transactions SET category_primary="
                          "'MEDICAL' WHERE id='r02500'")
        n = data.undo_merchant_category(self.conn, out["undo"])
        self.assertEqual(n, N - 2)
        cats = self._cats()
        self.assertEqual(cats.pop("r00007"), "ENTERTAINMENT")
        self.assertEqual(cats.pop("r02500"), "MEDICAL")
        want = dict(self.prior)
        del want["r00007"], want["r02500"]
        self.assertEqual(cats, want)
        src = self.conn.execute(
            "SELECT DISTINCT category_source FROM transactions "
            "WHERE id NOT IN ('r00007','r02500')").fetchall()
        self.assertTrue(all(r["category_source"] for r in src))

    def test_a_repeated_row_is_restored_in_snapshot_order(self):
        data.set_merchant_category(self.conn, "HARBOR BAKERY",
                                   "ENTERTAINMENT")
        # a posted snapshot naming one row twice: the first entry restores
        # it, the second no longer finds the written category there
        n = data.undo_merchant_category(self.conn, {
            "canons": [{"canon": "Harbor Bakery", "prior_rule": None}],
            "category": "ENTERTAINMENT",
            "affected": [{"id": "r00001", "prev": "GROCERIES"},
                         {"id": "r00002", "prev": "TRAVEL"},
                         {"id": "r00001", "prev": "MEDICAL"}]})
        self.assertEqual(n, 2)
        self.assertEqual(cat(self.conn, "r00001"), "GROCERIES")
        self.assertEqual(cat(self.conn, "r00002"), "TRAVEL")
        self.assertEqual(cat(self.conn, "r00003"), "ENTERTAINMENT")


if __name__ == "__main__":
    unittest.main()
