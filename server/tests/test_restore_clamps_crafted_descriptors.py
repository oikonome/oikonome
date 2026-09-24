"""A restored descriptor is never longer than a real one.

An archive is attacker-shaped input: a script token reaches the import door,
and a CSV cell may be megabytes — the limit is sized for a receipt photo, not
for a payee name. The descriptors are the strings the identity pass reads word
by word as the restore finishes, that the generated search column
concatenates, and that every payee list renders, so one crafted cell is paid
for on every read of that ledger from then on.

Some of them are also btree keys — the alias map's key, a merchant's
lower(name), a check number — where an index entry past about 2,700 bytes is
refused outright, so an unclamped cell is not a strange-looking row but a
restore that aborts entirely and leaves the household with nothing.

Cut at the door, not refused: a transaction is the household's record of money
moving, so the row still lands — with a name of an honest length. Nothing
under the cap is touched, which is why a round trip of real data is unchanged.
"""

import csv
import io
import unittest
import zipfile

from oikonome.sync import restore

from .util import TODAY, make_db

ACCT_COLS = ["id", "item_id", "name", "type", "subtype"]
ACCT_ROW = {"id": "chk", "item_id": "it1", "name": "Test Checking",
            "type": "depository", "subtype": "checking"}
T_COLS = ["id", "account_id", "date", "amount", "name", "merchant_name",
          "merchant_outlet", "merchant_name_set_aside", "location_city",
          "check_number"]


def _zip(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl, cols, data in (("accounts", ACCT_COLS, [ACCT_ROW]),
                                ("transactions", T_COLS, rows)):
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=cols)
            w.writeheader()
            for r in data:
                w.writerow({c: ("" if r.get(c) is None else r.get(c))
                            for c in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


class RestoredDescriptorsAreClampedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_crafted_cell_lands_cut_to_length_and_the_row_still_lands(self):
        huge = "juniper " * 40_000
        restore.restore_zip(self.conn, _zip([
            {"id": "crafted", "account_id": "chk", "date": str(TODAY),
             "amount": "12.00", "name": huge, "merchant_name": huge,
             "merchant_outlet": huge, "merchant_name_set_aside": huge,
             "location_city": huge, "check_number": huge}]))
        row = self.conn.execute(
            "SELECT name, merchant_name, merchant_outlet, location_city, "
            "       check_number, merchant_name_set_aside AS aside "
            "  FROM transactions WHERE id='crafted'").fetchone()
        self.assertIsNotNone(row, "the transaction was dropped, not clamped")
        for col in ("name", "merchant_name", "merchant_outlet",
                    "location_city", "check_number", "aside"):
            self.assertEqual(len(row[col]), restore.MAX_DESCRIPTOR, col)

    def test_an_honest_descriptor_is_returned_exactly(self):
        line = "TAPPAY JUNIPERS MKT SPRINGFIELD OR"
        restore.restore_zip(self.conn, _zip([
            {"id": "honest", "account_id": "chk", "date": str(TODAY),
             "amount": "12.00", "name": line,
             "merchant_name": "Juniper's Mkt",
             "merchant_outlet": "Juniper's Mkt Springfield"}]))
        row = self.conn.execute(
            "SELECT name, merchant_name, merchant_outlet FROM transactions "
            " WHERE id='honest'").fetchone()
        self.assertEqual(row["name"], line)
        self.assertEqual(row["merchant_name"], "Juniper's Mkt")
        self.assertEqual(row["merchant_outlet"], "Juniper's Mkt Springfield")

    def test_a_crafted_merchant_row_name_is_clamped_too(self):
        """The merchants member carries the names the ledger's descriptors
        are matched against — the same reading, the same cap."""
        import uuid
        mid = str(uuid.uuid4())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["id", "name", "name_source",
                                              "kind"])
            w.writeheader()
            w.writerow({"id": mid, "name": "harbor " * 40_000,
                        "name_source": "plaid", "kind": "merchant"})
            z.writestr("merchants.csv", s.getvalue())
            s2 = io.StringIO()
            w2 = csv.DictWriter(s2, fieldnames=["raw_merchant", "canonical",
                                                "method", "merchant_id"])
            w2.writeheader()
            w2.writerow({"raw_merchant": "HARBOR LIGHTS", "canonical":
                         "Harbor Lights", "method": "manual",
                         "merchant_id": mid})
            z.writestr("merchant_canonical.csv", s2.getvalue())
        restore.restore_zip(self.conn, buf.getvalue())
        row = self.conn.execute("SELECT name FROM merchants WHERE id=%s",
                                (mid,)).fetchone()
        self.assertEqual(len(row["name"]), restore.MAX_DESCRIPTOR)

    def test_a_crafted_alias_key_does_not_abort_the_restore(self):
        """The alias map keys on the descriptor itself, so its key is the
        one an over-long cell would refuse an index entry for — and with it
        the household's whole archive."""
        huge = "juniper " * 40_000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["raw_merchant", "canonical",
                                              "method"])
            w.writeheader()
            w.writerow({"raw_merchant": huge, "canonical": huge,
                        "method": "manual"})
            z.writestr("merchant_canonical.csv", s.getvalue())
            s2 = io.StringIO()
            w2 = csv.DictWriter(s2, fieldnames=["merchant",
                                                "category_primary"])
            w2.writeheader()
            w2.writerow({"merchant": huge,
                         "category_primary": "GENERAL_MERCHANDISE"})
            z.writestr("merchant_categories.csv", s2.getvalue())
        counts = restore.restore_zip(self.conn, buf.getvalue())
        self.assertEqual(counts.get("merchant_canonical"), 1)
        self.assertEqual(counts.get("merchant_categories"), 1)
        for table, col in (("merchant_canonical", "raw_merchant"),
                           ("merchant_categories", "merchant")):
            got = self.conn.execute(
                f"SELECT {col} AS v FROM {table}").fetchone()["v"]
            self.assertEqual(len(got), restore.MAX_DESCRIPTOR, table)


if __name__ == "__main__":
    unittest.main()
