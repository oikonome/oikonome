"""A restore cuts card numbers out of every row it writes, whatever the
archive says about its own age, and runs no migration file to do it.

The cut used to arrive by replaying the migrations that cut stored rows
over the household, chosen by the schema the archive's manifest names.
So a hand-edited manifest naming the newest migration skipped them and
planted whole numbers, a manifest-less archive ran three migration files
on the household's pooled connection (temporary functions included), and
a cell of text dense with groups of four held a database backend for
minutes. The passes now cut each archived cell in Python as they write
it, the same cut every door applies.

A detected bill named with a card number moves to the cut name's id as it
lands; restored into the household that already holds the moved bill, it
is that bill, never a second active one beside it.

Every number here is invented (test-card shapes).
"""

import csv
import io
import json
import time
import unittest
import uuid
import zipfile

from oikonome.sync import archive_upgrades, restore

from .util import make_db

WHOLE = "4000 0000 0000 0002"
FLAT = "4000000000000002"


def _zip(members: dict, manifest_schema=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in members.items():
            s = io.StringIO()
            fields = []
            for r in rows:
                fields += [k for k in r if k not in fields]
            w = csv.DictWriter(s, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
            z.writestr(name, s.getvalue())
        if manifest_schema:
            z.writestr(archive_upgrades.MANIFEST, archive_upgrades.manifest(
                manifest_schema, "test", list(members)))
    return buf.getvalue()


def _archive(manifest_schema=None) -> bytes:
    shop = str(uuid.uuid4())
    ach = str(uuid.uuid4())
    pat = str(uuid.uuid4())
    return _zip({
        "transactions.csv": [
            {"id": "csv:invented-outlet", "account_id": "chk",
             "date": "2026-07-01", "amount": 5, "name": "OUTLET INVENTED",
             "merchant_outlet": f"OUTLET {WHOLE}", "merchant_id": shop},
            {"id": "rc-ach", "account_id": "chk", "date": "2026-07-02",
             "amount": 6, "name": "ACH INVENTED", "merchant_id": ach},
            {"id": "rc-pat", "account_id": "chk", "date": "2026-07-03",
             "amount": 7, "name": "PAT INVENTED", "merchant_id": pat}],
        "merchants.csv": [
            {"id": ach, "kind": "merchant",
             "name": "ACH 4000.0000.0000.0002 INVENTED",
             "name_source": "layer1"},
            {"id": shop, "kind": "merchant", "name": f"SHOP {WHOLE}",
             "name_source": "llm"},
            {"id": pat, "kind": "merchant", "name": f"Pat {WHOLE}",
             "name_source": "manual"}],
        "merchant_canonical.csv": [
            {"raw_merchant": "ACH 4000.0000.0000.0002 INVENTED",
             "canonical": "ACH 4000.0000.0000.0002 INVENTED",
             "method": "llm", "as_of": "2026-07-01T00:00:00+00:00"}],
        "merchant_renames.csv": [
            {"id": 1, "raw_merchant": "ACH 4000.0000.0000.0002 INVENTED",
             "from_canonical": f"OLD {WHOLE}",
             "to_canonical": "ACH 4000.0000.0000.0002 INVENTED",
             "at": "2026-07-01T00:00:00+00:00"}],
        "merchant_categories.csv": [
            {"merchant": f"SHOP {WHOLE}", "category_primary": "SHOPPING",
             "source": "llm", "classified_at": "2026-07-01T00:00:00+00:00"},
            {"merchant": f"Pat {WHOLE}", "category_primary": "GIFTS",
             "source": "user", "classified_at": "2026-07-01T00:00:00+00:00"}],
        "bills.csv": [
            {"id": f"rec:invented-card-{FLAT}", "type": "bill",
             "payee": f"Invented Card {FLAT}", "amount": 25,
             "frequency": "MONTHLY", "active": 1, "raw": "{}"},
            {"id": f"rec:typed-{FLAT}", "type": "bill",
             "payee": f"Typed {FLAT}", "amount": 9, "frequency": "MONTHLY",
             "active": 1, "raw": json.dumps({"source": "manual"})}],
        "bill_proposals.csv": [
            {"id": "prop:invented-1", "kind": "add",
             "payee": f"Invented Card {FLAT}", "amount": 25,
             "frequency": "MONTHLY", "status": "approved",
             "evidence": json.dumps({"description": f"INVENTED {WHOLE}"})},
            {"id": "prop:invented-2", "kind": "add",
             "payee": f"Gone Series {FLAT}", "amount": 3,
             "frequency": "MONTHLY", "status": "pending",
             "evidence": "{}"}],
        "manual_categories.csv": [
            {"transaction_id": "rc-ach", "category": "BILLS",
             "bill_id": f"rec:invented-card-{FLAT}",
             "set_at": "2026-07-02T00:00:00+00:00"}],
        "activity_log.csv": [
            {"id": str(uuid.uuid4()), "at": "2026-07-01T00:00:00+00:00",
             "actor": "pat@example.test", "kind": "bill", "action": "add",
             "label": f"Card {WHOLE}", "summary": f"added Card {WHOLE}"}],
    }, manifest_schema)


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()


class ACraftedManifestSkipsNothing(_Db):
    """The archive names the newest migration, so nothing it holds was cut
    by the migrations a replay keyed on that name would have skipped."""

    def setUp(self):
        super().setUp()
        restore.restore_zip(self.conn, _archive(
            archive_upgrades.migration_names()[-1]))

    def test_a_machines_merchant_name_and_its_alias_are_cut(self):
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM merchants").fetchall()}
        self.assertIn("ACH …0002 INVENTED", names)
        self.assertIn("SHOP …0002", names)
        # what a person typed stays
        self.assertIn(f"Pat {WHOLE}", names)
        alias = self._one("SELECT canonical FROM merchant_canonical")
        self.assertEqual(alias["canonical"], "ACH …0002 INVENTED")

    def test_the_rename_journal_is_cut(self):
        j = self._one("SELECT from_canonical, to_canonical"
                      "  FROM merchant_renames")
        self.assertEqual((j["from_canonical"], j["to_canonical"]),
                         ("OLD …0002", "ACH …0002 INVENTED"))

    def test_a_rule_follows_its_merchants_cut_name(self):
        rules = {r["merchant"] for r in self.conn.execute(
            "SELECT merchant FROM merchant_categories").fetchall()}
        self.assertEqual(rules, {"SHOP …0002", f"Pat {WHOLE}"})

    def test_a_file_rows_outlet_is_cut(self):
        row = self._one("SELECT merchant_outlet FROM transactions"
                        " WHERE date = '2026-07-01'")
        self.assertEqual(row["merchant_outlet"], "OUTLET …0002")

    def test_a_detected_bill_lands_under_its_cut_name(self):
        bills = {(r["id"], r["payee"]) for r in self.conn.execute(
            "SELECT id, payee FROM bills").fetchall()}
        self.assertEqual(bills, {
            ("rec:invented-card-0002", "Invented Card …0002"),
            # a bill a person made keeps what they typed
            (f"rec:typed-{FLAT}", f"Typed {FLAT}")})
        pin = self._one("SELECT bill_id FROM manual_categories")
        self.assertEqual(pin["bill_id"], "rec:invented-card-0002")

    def test_offers_are_cut(self):
        got = {r["id"]: (r["payee"], r["evidence"]) for r in
               self.conn.execute("SELECT id, payee, evidence"
                                 "  FROM bill_proposals").fetchall()}
        self.assertEqual(got["prop:invented-1"],
                         ("Invented Card …0002",
                          {"description": "INVENTED …0002"}))
        self.assertEqual(got["prop:invented-2"][0], "Gone Series …0002")

    def test_the_activity_log_is_cut(self):
        row = self._one("SELECT label, summary FROM activity_log")
        self.assertEqual((row["label"], row["summary"]),
                         ("Card …0002", "added Card …0002"))

    def test_nothing_left_holds_the_whole_number(self):
        for table, col in (("merchant_canonical", "canonical"),
                           ("merchant_renames", "to_canonical"),
                           ("bill_proposals", "payee"),
                           ("activity_log", "label")):
            hit = self._one(f"SELECT count(*) AS n FROM {table}"
                            f" WHERE {col} LIKE %s", f"%{WHOLE}%")
            self.assertEqual(hit["n"], 0, table)


class TheRestoreRunsNoMigrationFile(_Db):

    def test_a_manifest_less_archive_runs_no_temporary_function(self):
        ran = []
        real = self.conn.execute

        def watched(sql, *a, **kw):
            ran.append(str(sql))
            return real(sql, *a, **kw)

        self.conn.execute = watched
        try:
            restore.restore_zip(self.conn, _archive())
        finally:
            self.conn.execute = real
        self.assertEqual([s[:80] for s in ran if "pg_temp." in s], [])
        name = self._one("SELECT name FROM merchants"
                         " WHERE name LIKE 'ACH%%'")["name"]
        self.assertEqual(name, "ACH …0002 INVENTED")

    def test_text_dense_with_numbers_restores_in_moments(self):
        """8 KB of numbers in an offer's evidence held a backend for tens
        of minutes when the restore cut it in SQL."""
        dense = (f"{WHOLE}-" * 400)
        data = _zip({"bill_proposals.csv": [
            {"id": "prop:dense", "kind": "add", "payee": "Dense",
             "amount": 1, "frequency": "MONTHLY", "status": "pending",
             "evidence": json.dumps({"description": dense})}]})
        self.conn.execute("SET statement_timeout = '10s'")
        t0 = time.monotonic()
        restore.restore_zip(self.conn, data)
        self.assertLess(time.monotonic() - t0, 10)
        ev = self._one("SELECT evidence FROM bill_proposals")["evidence"]
        self.assertNotIn(WHOLE, ev["description"])


class APreCutArchiveIntoTheSameHousehold(_Db):
    """The household's bill was moved to its cut name by the upgrade; its
    backup from before the upgrade still names it in full."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO bills (id, type, payee, amount, frequency, active,"
            "                   raw)"
            " VALUES ('rec:invented-card-0002', 'bill',"
            "         'Invented Card …0002', 25, 'MONTHLY', 1, '{}')")

    def test_the_bill_comes_back_as_the_one_it_became(self):
        counts = restore.restore_zip(self.conn, _archive())
        bills = [(r["id"], r["payee"], r["active"]) for r in
                 self.conn.execute("SELECT id, payee, active FROM bills"
                                   " WHERE raw->>'source' IS DISTINCT FROM"
                                   "       'manual'").fetchall()]
        self.assertEqual(bills, [("rec:invented-card-0002",
                                  "Invented Card …0002", 1)])
        self.assertGreaterEqual(counts.get("already_present", 0), 1)
        # what named the bill by its old id or payee names the held one
        pin = self._one("SELECT bill_id FROM manual_categories")
        self.assertEqual(pin["bill_id"], "rec:invented-card-0002")
        offer = self._one("SELECT payee FROM bill_proposals"
                          " WHERE id = 'prop:invented-1'")
        self.assertEqual(offer["payee"], "Invented Card …0002")

    def test_a_held_bill_under_another_id_with_the_cut_name_is_kept_one(self):
        self.conn.execute("UPDATE bills SET id = 'rec:invented-card-0002-2'")
        restore.restore_zip(self.conn, _archive())
        n = self._one("SELECT count(*) AS n FROM bills"
                      " WHERE payee LIKE 'Invented Card%%'")["n"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
