"""A full account, loan or routing number never sits in a plaintext `raw`.

raw leaves the instance verbatim through the export and the portability
archive. The ingest paths keep these numbers out, but only for rows written
after each rule existed: rows stored earlier, and archives exported
earlier, still carry them. The stored rows are cut once by a migration and
a restore cuts an old archive on the way in — with exactly the rules the
ingest paths apply, so a value the importer would have kept (a memo, a
reference number, an aggregator's own account id) is kept here too.
"""

import csv
import io
import json
import pathlib
import re
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.engine.compat import jsonb
from oikonome.sync import csvimport, restore

from .util import TODAY, add_txn, make_db

MIGRATION = (pathlib.Path(restore.__file__).parent.parent / "db" /
             "migrations" / "146_raw_identifier_scrub.sql")
LINE_MIGRATION = MIGRATION.with_name("148_raw_statement_line_scrub.sql")

FULL = "000011112222"
ROUTING = "000099990000"


def _plant(conn):
    """Rows in the shape the ingest paths wrote before their scrubs."""
    conn.execute("UPDATE accounts SET raw=%s WHERE id='chk'", (jsonb({
        "guid": "ACT-demo", "account_number": FULL,
        "routing_number": ROUTING, "name": "Checking"}),))
    conn.execute(
        "INSERT INTO liabilities (account_id, as_of, raw) "
        "VALUES ('card', now(), %s)", (jsonb({
            "account_id": "card", "account_number": FULL,
            "last_statement_balance": 250.0}),))
    add_txn(conn, TODAY, 10.0, "CSV ROW", txn_id="csv-1")
    add_txn(conn, TODAY, 11.0, "OFX ROW", txn_id="ofx-1")
    add_txn(conn, TODAY, 12.0, "AGGREGATOR ROW", txn_id="agg-1")
    add_txn(conn, TODAY, 13.0, "STATEMENT ROW", txn_id="pdf-1")
    for tid, raw in (
            ("csv-1", {"Account Number": FULL, "Routing #": ROUTING,
                       "Description": "CSV ROW",
                       "Memo": "ref 000011112222"}),
            ("ofx-1", {"acct": FULL, "fitid": "F1"}),
            ("pdf-1", {"line": "01/05 PAYMENT CARD 4000123456789010 13.00",
                       "flow": "out"}),
            ("agg-1", {"account_id": "acc-abcdefgh", "account_owner": None,
                       "transaction_id": "agg-1",
                       "payment_meta": {"reference_number": FULL}})):
        conn.execute("UPDATE transactions SET raw=%s WHERE id=%s",
                      (jsonb(raw), tid))


def _raws(conn) -> dict:
    return {
        "account": conn.execute(
            "SELECT raw FROM accounts WHERE id='chk'").fetchone()["raw"],
        "liability": conn.execute(
            "SELECT raw FROM liabilities WHERE account_id='card'"
        ).fetchone()["raw"],
        **{r["id"]: r["raw"] for r in conn.execute(
            "SELECT id, raw FROM transactions "
            "WHERE id IN ('csv-1','ofx-1','agg-1')").fetchall()}}


def _export_zip(conn, tables) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl in tables:
            rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()
            s = io.StringIO()
            if rows:
                cols = [c for c in rows[0].keys()
                        if c not in ("tenant_id", "access_token")]
                w = csv.DictWriter(s, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({k: (json.dumps(r[k])
                                    if isinstance(r[k], (dict, list))
                                    else r[k]) for k in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


class RawIdentifierTests(unittest.TestCase):

    def assertScrubbed(self, raws):
        self.assertEqual(raws["account"],
                         {"guid": "ACT-demo", "name": "Checking"})
        self.assertEqual(raws["liability"],
                         {"account_id": "card",
                          "last_statement_balance": 250.0})
        self.assertEqual(raws["csv-1"], {
            "Account Number": "2222", "Routing #": "0000",
            "Description": "CSV ROW",
            # a memo is not an identifier column, whatever it looks like
            "Memo": "ref 000011112222"})
        self.assertEqual(raws["ofx-1"], {"acct": "2222", "fitid": "F1"})
        # nothing the importers would have kept is touched
        self.assertEqual(raws["agg-1"], {
            "account_id": "acc-abcdefgh", "account_owner": None,
            "transaction_id": "agg-1",
            "payment_meta": {"reference_number": FULL}})

    def test_stored_rows_are_cut_once_and_stay_cut(self):
        conn = make_db()
        self.addCleanup(conn.close)
        _plant(conn)
        conn.execute(MIGRATION.read_text())
        self.assertScrubbed(_raws(conn))
        conn.execute(MIGRATION.read_text())
        self.assertScrubbed(_raws(conn))

    def test_an_old_archive_is_cut_on_the_way_in(self):
        a = make_db()
        _plant(a)
        data = _export_zip(a, ["items", "accounts", "liabilities",
                               "transactions"])
        a.close()
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, "restore-raw-ids")
        finally:
            admin.close()
        b = tenancy.tenant_connect(tid)
        self.addCleanup(b.close)
        restore.restore_zip(b, data)
        self.assertScrubbed(_raws(b))

    def test_the_migration_names_the_importers_identifier_columns(self):
        """The migration and the CSV importer must agree on which columns
        are identifiers, or rows stored before and after it differ."""
        body = MIGRATION.read_text()
        arr = re.search(r"unnest\(ARRAY\[(.*?)\]\)", body, re.S).group(1)
        self.assertEqual(tuple(re.findall(r"'([^']*)'", arr)),
                         csvimport._ID_HEADERS)

    def test_an_aggregator_row_keeps_its_own_fields_on_the_way_in(self):
        """The identifier rule is a CSV-header rule. An aggregator's raw
        carries its own field names next to numbers, flags and nested
        objects; a key that merely reads like an identifier header there
        is the aggregator's data, and cutting it to 4 characters would
        destroy it for good."""
        agg = {"transaction_id": "agg-2", "amount": 12.5, "pending": False,
               "Bank Account": "Everyday Checking",
               "location": {"city": None}}
        self.assertEqual(restore.scrub_transaction_raw(agg), agg)
        # a file-import row with the same column is still cut, batch
        # tags and all
        row = {"Bank Account": FULL, "Description": "CSV ROW",
               "_batch": "b1", "_batches": ["b1"]}
        self.assertEqual(restore.scrub_transaction_raw(row)["Bank Account"],
                         "2222")
        # the OFX account hint is cut whatever else the row carries
        self.assertEqual(restore.scrub_transaction_raw(
            {"acct": FULL, "amount": 1.0, "cc": False})["acct"], "2222")

    def test_the_importer_rule_and_the_migration_cut_the_same_values(self):
        """Text and numbers under an identifier header are cut; a flag, a
        list or an object is not, by the Python rule and the SQL alike."""
        raw = {"Account Number": FULL, "Routing": 123456789,
               "Card Number": True, "IBAN": {"v": FULL},
               "Member Number": [FULL], "Memo": "kept"}
        conn = make_db()
        self.addCleanup(conn.close)
        add_txn(conn, TODAY, 1.0, "MIXED", txn_id="mixed-1")
        conn.execute("UPDATE transactions SET raw=%s WHERE id='mixed-1'",
                     (jsonb(raw),))
        conn.execute(MIGRATION.read_text())
        stored = conn.execute("SELECT raw FROM transactions "
                              "WHERE id='mixed-1'").fetchone()["raw"]
        self.assertEqual(csvimport.scrub_identifiers(raw), stored)
        self.assertEqual(stored["Routing"], "6789")
        self.assertIs(stored["Card Number"], True)


if __name__ == "__main__":
    unittest.main()


class StatementLineTests(unittest.TestCase):
    """The statement importer masks long digit runs in the line it keeps;
    rows stored before that rule, and archives taken before it, get the
    same cut."""

    def test_stored_statement_lines_are_masked_once_and_stay_masked(self):
        conn = make_db()
        try:
            _plant(conn)
            conn.execute(LINE_MIGRATION.read_text())
            line = conn.execute(
                "SELECT raw->>'line' AS line FROM transactions WHERE id='pdf-1'"
            ).fetchone()["line"]
            self.assertEqual(line, "01/05 PAYMENT CARD …9010 13.00")
            conn.execute(LINE_MIGRATION.read_text())
            again = conn.execute(
                "SELECT raw->>'line' AS line FROM transactions WHERE id='pdf-1'"
            ).fetchone()["line"]
            self.assertEqual(again, line)
        finally:
            conn.close()

    def test_an_old_archive_line_is_masked_on_the_way_in(self):
        out = restore.scrub_transaction_raw(
            {"line": "01/05 PAYMENT CARD 4000123456789010 13.00",
             "flow": "out"})
        self.assertEqual(out["line"], "01/05 PAYMENT CARD …9010 13.00")
        self.assertEqual(out["flow"], "out")

