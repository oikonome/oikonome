"""A restored merchant that collides with a live one on its SECOND unique
key must be skipped, not abort the whole all-or-nothing restore.

merchants carries a partial unique index on plaid_entity_id beside the
(tenant_id, id) primary key. The restore's insert used a column-list
arbiter on the PK only, so a ZIP whose merchant row carried an entity id
already live under a different local id raised through the transaction
and undid every table the restore had written.
"""

import csv
import io
import unittest
import zipfile

from oikonome.sync import restore

from .util import make_db, write_config


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in files.items():
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow(r)
            z.writestr(name, s.getvalue())
    return buf.getvalue()


LIVE = "11111111-1111-4111-8111-111111111111"
RESTORED = "22222222-2222-4222-8222-222222222222"


class MerchantEntityCollisionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_entity_id_collision_does_not_abort_the_restore(self):
        # a restore REPLACES the tenant's rows, so the collision that can
        # abort it is inside the archive itself: two merchant rows that
        # share a plaid_entity_id (a merge that raced an export). The
        # first must land and the second be skipped — not the whole
        # transaction rolled back.
        data = _zip({"merchants.csv": [
            {"id": LIVE, "plaid_entity_id": "ent-1",
             "name": "Live Co", "name_source": "plaid",
             "kind": "merchant", "logo_url": "", "website": "",
             "phone": "", "mcc": "", "parent_id": "", "merged_into": "",
             "created_at": ""},
            {"id": RESTORED, "plaid_entity_id": "ent-1",
             "name": "Restored Co", "name_source": "plaid",
             "kind": "merchant", "logo_url": "", "website": "",
             "phone": "", "mcc": "", "parent_id": "", "merged_into": "",
             "created_at": ""}]})
        # no exception is the headline; the report shows one row landed
        # and the duplicate was skipped (the restore's own post-pass may
        # prune merchants no transaction references, so the report — not
        # a later SELECT — is the evidence)
        report = restore.restore_zip(self.conn, data)
        self.assertEqual(report.get("merchants"), 1, report)
        self.assertEqual(report.get("already_present"), 1, report)

if __name__ == "__main__":
    unittest.main()
