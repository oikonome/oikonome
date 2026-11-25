"""Restoring the same archive twice adds nothing the second time, and a
tax document is told apart from another by the value that is stored.

- A rename journal row without its time lands stamped with the time of
  the restore. Presence compared that stamp with the archive's blank
  cell, which never matches, so every re-restore appended another copy
  of the same rename.
- A tax document's amounts are stored with non-finite numbers read as
  null. Presence was decided on the archive's own spelling, where NaN
  and null differ: one copy the household held then answered for two
  archived documents, and the second was dropped as already present.
"""
import csv
import io
import json
import unittest
import zipfile

from oikonome.sync import restore

from .util import make_db, write_config


def _member(rows: list[dict]) -> str:
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=list(rows[0]))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return s.getvalue()


def _zip(**members) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in members.items():
            z.writestr(f"{name}.csv", _member(rows))
    return buf.getvalue()


class _Case(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)


class ARenameWithoutItsTimeIsFoundAgain(_Case):

    def test_a_second_restore_finds_the_rename_present(self):
        data = _zip(merchant_renames=[
            {"id": "1", "raw_merchant": "CORNER CAFE 01",
             "from_canonical": "Corner Cafe 01",
             "to_canonical": "Corner Cafe", "at": "", "rules": ""}])
        restore.restore_zip(self.conn, data)
        _, out = restore.restore_outcomes(self.conn, data)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchant_renames"
            " WHERE to_canonical = 'Corner Cafe'").fetchone()["n"], 1)
        self.assertEqual(out.refused.get(("merchant_renames", "present")), 1)


class ADocumentIsJudgedAsItIsStored(_Case):

    def test_one_held_copy_answers_for_one_archived_document(self):
        self.conn.execute(
            "INSERT INTO income_documents (id, year, form, owner, payer,"
            " primary_amount, amounts) VALUES (1, 2024, '1099-INT',"
            " 'primary', 'Harbor Bank', 40, %s)",
            (json.dumps({"box1": None}),))

        def doc(i, amounts):
            return {"id": str(i), "year": "2024", "form": "1099-INT",
                    "owner": "primary", "payer": "Harbor Bank",
                    "ein_last4": "", "primary_amount": "40",
                    "amounts": amounts, "notes": ""}
        # two archived copies that store the same: the one held answers
        # for one of them, the other lands
        _, out = restore.restore_outcomes(self.conn, _zip(income_documents=[
            doc(1, '{"box1": NaN}'), doc(2, '{"box1": null}')]))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM income_documents"
            " WHERE payer = 'Harbor Bank'").fetchone()["n"], 2)
        self.assertEqual(out.refused.get(("income_documents", "present")), 1)


if __name__ == "__main__":
    unittest.main()
