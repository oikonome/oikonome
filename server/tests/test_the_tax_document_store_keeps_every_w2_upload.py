"""What the tax-document store holds to for every W-2.

A W-2 box that is a JSON `true` is not a number; a W-2 commit is bounded
to a handful of rows; a household being erased takes no document; and a
second upload of the same W-2 is stored as its own row, never refused.
"""

import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.engine import taxdocs

from .util import make_db


def _row(year, box1, employer="ACME STAFFING"):
    return {"year": year, "employer": employer, "ein": "•••••6789",
            "boxes": {"1": box1}}


class TaxDocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def store(self, *rows):
        token = staging.put(self.conn, "taxdoc",
                            meta={"kind": "w2", "rows": []})
        return taxdocs.commit(self.conn, token, list(rows))

    def docs(self):
        return self.conn.execute(
            "SELECT * FROM income_documents ORDER BY id").fetchall()

    def test_a_boolean_box_is_not_a_number(self):
        self.store({"year": 2024, "employer": "A",
                    "boxes": {"1": True, "2": 5.0}})
        doc = self.docs()[0]
        self.assertIsNone(doc["primary_amount"])
        self.assertNotIn("1", doc["amounts"])

    def test_a_w2_commit_is_bounded_to_a_handful_of_rows(self):
        with self.assertRaises(ValueError):
            self.store(*[_row(2024, float(i + 1), f"E{i}")
                         for i in range(taxdocs.MAX_W2_ROWS + 1)])
        self.assertEqual(self.docs(), [])

    def test_the_same_w2_uploaded_twice_is_stored_not_refused(self):
        self.store(_row(2024, 50000.0))
        self.store(_row(2024, 50000.0))
        self.assertEqual(len(self.docs()), 2)
        self.assertEqual(self.conn.execute(
            "SELECT primary_wages FROM income_annual WHERE year = 2024"
        ).fetchone()["primary_wages"], 50000.0)

    def test_a_household_being_erased_takes_no_document(self):
        token = staging.put(self.conn, "taxdoc",
                            meta={"kind": "w2", "rows": []})
        with mock.patch("oikonome.erasure.push_may_write",
                        return_value=False):
            with self.assertRaises(taxdocs.HouseholdErasing):
                taxdocs.commit(self.conn, token, [_row(2024, 1.0)])
        self.assertEqual(self.docs(), [])
        self.assertIsNotNone(staging.pop(self.conn, "taxdoc", token))


if __name__ == "__main__":
    unittest.main()
