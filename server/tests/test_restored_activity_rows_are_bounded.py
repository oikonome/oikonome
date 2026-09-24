"""A restored activity-log row is no bigger than one the app would write.

The activity feed returns every row's `target` and `detail` on each read. A
CSV cell in a restore archive may be megabytes (the cell cap is sized for a
receipt photo), so an uncapped `target` or `detail` from one crafted row
would make every later read of the feed carry megabytes. The row itself is
the household's record and still lands: the target is clipped to the
longest a real one runs, and an oversized `detail` is dropped.
"""

import csv
import io
import json
import unittest
import uuid
import zipfile

from oikonome.sync import restore

from .util import make_db, write_config


def _activity_zip(row: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=list(row))
        w.writeheader()
        w.writerow(row)
        z.writestr("activity_log.csv", s.getvalue())
    return buf.getvalue()


def _row(**over) -> dict:
    r = {"id": str(uuid.uuid4()), "at": "2026-09-01T12:00:00+00:00",
         "actor": "sam@example.com", "kind": "txn", "action": "note",
         "target": "t0001", "label": "Example Grocer", "summary": "added a note",
         "detail": json.dumps({"after": "milk"})}
    r.update(over)
    return r


class RestoredActivityBoundsTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _stored(self, rid):
        return self.conn.execute(
            "SELECT target, detail FROM activity_log WHERE id = %s::uuid",
            (rid,)).fetchone()

    def test_a_huge_detail_is_dropped_and_a_huge_target_clipped(self):
        r = _row(target="x" * 1_000_000,
                 detail=json.dumps({"after": "y" * 1_000_000}))
        counts = restore.restore_zip(self.conn, _activity_zip(r))
        self.assertEqual(counts.get("activity_log"), 1, "the row still lands")
        got = self._stored(r["id"])
        self.assertTrue(got["detail"] is None, "the oversized detail was kept")
        self.assertLessEqual(len(got["target"]), restore.ACTIVITY_TARGET_MAX)

    def test_an_ordinary_row_is_untouched(self):
        r = _row()
        restore.restore_zip(self.conn, _activity_zip(r))
        got = self._stored(r["id"])
        self.assertEqual(got["target"], "t0001")
        self.assertEqual(got["detail"], {"after": "milk"})


if __name__ == "__main__":
    unittest.main()
