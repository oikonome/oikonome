"""The restore's two rules for a number hold in the income and business
tables too: a non-finite number refuses the whole archive (no genuine
export writes one, see `restore._f`), and a figure no stored row could
hold refuses only its row.

* A stored document's year far past any tax year (1e308) was landed in
  the restore's list of years to work out again, and the year's figures
  were then asked of the database with it, which refused the number and
  ended the restore with no reason a person could act on. Such a row is
  malformed.
* The year and upload id cells of the income tables, the year locks read
  ahead of them, a business obligation's fee and the order of a rename
  journal's rows were read under a catch
  that took "inf" and "nan" for text, so the archive's rule became one
  malformed row there and nowhere else.
"""

import unittest
import uuid

from oikonome.sync import restore

from .test_restore_v2 import _zip
from .util import make_db, write_config

UPLOAD_COLS = ["id", "uid", "kind", "filename", "stored_at"]
VALUE_COLS = ["upload_id", "year", "fields"]


def _upload():
    return {"id": "1", "uid": str(uuid.uuid4()), "kind": "document",
            "filename": "invented.pdf", "stored_at": "2026-01-05 10:00:00"}


def _value(year):
    return {"upload_id": "1", "year": year, "fields": '{"agi": 51000}'}


class _Restore(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def restore(self, tables):
        return restore.restore_outcomes(self.conn, _zip(tables))

    def refused(self, tables):
        with self.assertRaises(restore._ArchiveRefused) as e:
            self.restore(tables)
        self.assertIn("not finite", str(e.exception))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM income_uploads").fetchone()["n"], 0)


class AYearNoTaxYearCanBe(_Restore):
    def test_is_a_malformed_row_and_the_rest_lands(self):
        _r, out = self.restore({
            "income_uploads": (UPLOAD_COLS, [_upload()]),
            "income_upload_values": (VALUE_COLS, [
                _value("1e308"), _value("99999"), _value("2024")])})
        self.assertEqual(out.refused.get(("income_upload_values",
                                          "malformed")), 2)
        self.assertEqual([r["year"] for r in self.conn.execute(
            "SELECT year FROM income_upload_values").fetchall()], [2024])


class ANonFiniteNumberRefusesTheArchive(_Restore):
    def test_in_a_stored_documents_year(self):
        for v in ("inf", "nan"):
            with self.subTest(v=v):
                self.refused({
                    "income_uploads": (UPLOAD_COLS, [_upload()]),
                    "income_upload_values": (VALUE_COLS, [_value(v)])})

    def test_in_a_stored_documents_upload_id(self):
        self.refused({
            "income_uploads": (UPLOAD_COLS, [_upload()]),
            "income_upload_values": (VALUE_COLS, [
                {"upload_id": "inf", "year": "2024",
                 "fields": '{"agi": 51000}'}])})

    def test_in_an_upload_id(self):
        self.refused({"income_uploads": (UPLOAD_COLS, [
            {**_upload(), "id": "nan"}])})

    def test_in_an_income_years_year(self):
        self.refused({
            "income_uploads": (UPLOAD_COLS, [_upload()]),
            "income_annual": (["year", "agi"], [
                {"year": "inf", "agi": "51000"}])})

    def test_in_a_tax_documents_year(self):
        self.refused({
            "income_uploads": (UPLOAD_COLS, [_upload()]),
            "income_documents": (["id", "year", "form", "amounts"], [
                {"id": "1", "year": "nan", "form": "W-2",
                 "amounts": "{}"}])})

    def test_in_a_rename_journals_id(self):
        self.refused({
            "income_uploads": (UPLOAD_COLS, [_upload()]),
            "merchant_renames": (["id", "raw_merchant", "to_canonical"], [
                {"id": "nan", "raw_merchant": "INVENTED SHOP",
                 "to_canonical": "Invented Shop"}])})

    def _business(self, table, cols, row):
        ent = str(uuid.uuid4())
        return {
            "income_uploads": (UPLOAD_COLS, [_upload()]),
            "business_entity": (["id", "name", "structure"], [
                {"id": ent, "name": "Invented Works", "structure": "llc"}]),
            table: (cols, [{**row, "id": str(uuid.uuid4()),
                            "entity_id": ent}])}

    def test_in_an_obligations_fee(self):
        for v in ("nan", "inf", "1e999"):
            with self.subTest(v=v):
                self.refused(self._business(
                    "compliance_obligation",
                    ["id", "entity_id", "title", "due_date", "fee"],
                    {"title": "Invented filing", "due_date": "2026-04-15",
                     "fee": v}))

    def test_in_a_trips_miles(self):
        self.refused(self._business(
            "mileage_log", ["id", "entity_id", "date", "miles"],
            {"date": "2026-02-03", "miles": "inf"}))


if __name__ == "__main__":
    unittest.main()
