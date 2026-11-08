"""A statement re-imported after restoring an archive written before the
card-number cut read dotted and slashed numbers adds no rows.

That build stored "4000.0000.0000.0002" in a file row's name and an
account's display name whole. The restore cuts them with the cut every
door now uses (sync.card_number), and a file row's id was never hashed
over the cut name, so the re-imported statement must find every row it
already holds: a cut that differed between the restore and the import
would add each line a second time.
"""

import csv
import io
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import csvimport, export, restore

from .util import make_db, write_config

DOTTED = "4000.0000.0000.0002"
CSV = f"""Date,Description,Amount
2026-03-10,CARD {DOTTED} PAYMENT,-42.50
2026-03-11,INVENTED STORE {DOTTED.replace('.', '/')},-12.00
"""
MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


def _as_the_older_build_wrote_it(data: bytes) -> bytes:
    """The archive with each file row's name spelled as the import read it,
    number whole, and the account's display name holding one too."""
    src = zipfile.ZipFile(io.BytesIO(data))
    whole = {r["Date"]: r["Description"] for r in
             csv.DictReader(io.StringIO(CSV))}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in src.namelist():
            body = src.read(name)
            if name in ("transactions.csv", "accounts.csv"):
                rows = list(csv.DictReader(io.StringIO(body.decode())))
                for r in rows:
                    if name == "transactions.csv" and r["date"] in whole:
                        r["name"] = whole[r["date"]]
                    if name == "accounts.csv" and r["id"] == "chk":
                        r["display_name"] = f"Checking {DOTTED}"
                s = io.StringIO()
                w = csv.DictWriter(s, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
                body = s.getvalue().encode()
            z.writestr(name, body)
    return buf.getvalue()


class ReimportAfterOlderArchiveTests(unittest.TestCase):

    def setUp(self):
        a = make_db()
        write_config(a)
        csvimport.import_csv(a, "chk", CSV, MAPPING)
        self.data = _as_the_older_build_wrote_it(export.build_zip(a))
        a.close()
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, "restore-pre-widening")
        finally:
            admin.close()
        self.b = tenancy.tenant_connect(tid)
        self.addCleanup(self.b.close)
        restore.restore_zip(self.b, self.data)

    def _rows(self):
        return self.b.execute(
            "SELECT name FROM transactions WHERE id LIKE 'csv:%%'"
            " AND removed = 0 ORDER BY date").fetchall()

    def test_the_restored_names_keep_only_the_last_four(self):
        names = [r["name"] for r in self._rows()]
        self.assertEqual(len(names), 2)
        for n in names:
            self.assertNotIn("0000", n)
            self.assertIn("…0002", n)
        acct = self.b.execute("SELECT display_name FROM accounts"
                              " WHERE id = 'chk'").fetchone()
        self.assertEqual(acct["display_name"], "Checking …0002")

    def test_reimporting_the_same_statement_adds_nothing(self):
        before = len(self._rows())
        csvimport.import_csv(self.b, "chk", CSV, MAPPING)
        self.assertEqual(len(self._rows()), before)


if __name__ == "__main__":
    unittest.main()
