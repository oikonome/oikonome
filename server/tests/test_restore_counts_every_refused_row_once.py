"""A row a restore refuses is counted under the reason it was refused, and
an archive older than a migration that cut card numbers gets that cut.

The counters used to say what the loop saw, not what the database did: a
receipt line or a link whose parent was missing was counted "already
present" (the write hid the parent check in its SQL, so a refusal and a
duplicate looked alike), and a receipt refused for its type was counted
nowhere. And an archive written before the cut of card numbers in merchant
names and activity labels planted them whole again.
"""

import csv
import io
import json
import unittest
import uuid
import zipfile

from oikonome.sync import archive_upgrades, restore

from .util import make_db


def _zip(members: dict, manifest_schema=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in members.items():
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
            z.writestr(name, s.getvalue())
        if manifest_schema:
            z.writestr(archive_upgrades.MANIFEST, archive_upgrades.manifest(
                manifest_schema, "test", list(members)))
    return buf.getvalue()


def _old_archive(mid: str, manifest_schema=None) -> bytes:
    return _zip({
        "transactions.csv": [{"id": "rc-1", "account_id": "chk",
                              "date": "2026-07-01", "amount": 5,
                              "name": "ACH INVENTED", "merchant_id": mid}],
        "merchants.csv": [{"id": mid, "kind": "merchant",
                           "name": "ACH 4000.0000.0000.0002 INVENTED",
                           "name_source": "layer1"}],
        "activity_log.csv": [{"id": str(uuid.uuid4()),
                              "at": "2026-07-01T00:00:00+00:00",
                              "actor": "pat@example.test", "kind": "bill",
                              "action": "add",
                              "label": "Card 4000 0000 0000 0002",
                              "summary": "added Card 4000 0000 0000 0002"}],
    }, manifest_schema)


class RefusedRowsTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_each_refused_row_is_counted_under_its_reason(self):
        counts, out = restore.restore_outcomes(self.conn, _zip({
            "receipt_items.csv": [{"receipt_id": str(uuid.uuid4()),
                                   "line": 1, "description": "x", "qty": 1,
                                   "amount": 2, "tag": ""}],
            "receipts.csv": [{"id": str(uuid.uuid4()), "txn_id": "",
                              "image": "aGVsbG8=", "mime": "text/html",
                              "kind": "receipt"}],
            "account_links.csv": [{"group_id": str(uuid.uuid4()),
                                   "account_id": "no-such-account",
                                   "home_rank": 0}],
        }))
        self.assertNotIn("already_present", counts)
        self.assertEqual(out.refused, {("receipt_items", "orphaned"): 1,
                                       ("account_links", "orphaned"): 1,
                                       ("receipts", "malformed"): 1})
        self.assertEqual(counts["orphaned"], 2)
        self.assertEqual(counts["malformed"], 1)

    def test_an_archive_from_before_the_cut_comes_back_cut(self):
        restore.restore_zip(self.conn, _old_archive(str(uuid.uuid4())))
        name = self.conn.execute(
            "SELECT m.name FROM merchants m JOIN transactions t"
            " ON t.merchant_id = m.id WHERE t.id = 'rc-1'").fetchone()["name"]
        self.assertEqual(name, "ACH …0002 INVENTED")
        row = self.conn.execute(
            "SELECT label, summary FROM activity_log").fetchone()
        self.assertEqual((row["label"], row["summary"]),
                         ("Card …0002", "added Card …0002"))

    def test_an_archive_claiming_to_have_had_the_cut_is_cut_anyway(self):
        """The manifest is the archive's own word, and an archive is text
        anyone can edit: a claim to be newer than the cut skips nothing."""
        for schema in (archive_upgrades.migration_names()[-1],
                       next(n for n in archive_upgrades.migration_names()
                            if n.startswith("204_"))):
            conn = make_db()
            self.addCleanup(conn.close)
            restore.restore_zip(conn, _old_archive(str(uuid.uuid4()),
                                                   schema))
            name = conn.execute(
                "SELECT m.name FROM merchants m JOIN transactions t"
                " ON t.merchant_id = m.id WHERE t.id = 'rc-1'"
            ).fetchone()["name"]
            self.assertEqual(name, "ACH …0002 INVENTED", schema)
            self.assertEqual(json.loads(archive_upgrades.manifest(
                schema, "t", []))["schema"], schema)
