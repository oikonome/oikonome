"""A rename the household already journals under a whole name is not
written a second time by a restore that would cut it.

The restore cuts a journal row's result where the archive's alias for the
same descriptor is a machine's (the resolver's spelling). A household whose
own alias for that descriptor is a person's keeps its journal row whole, so
the cut row looked new and the journal gained a duplicate of a rename it
already held. Synthetic numbers throughout.
"""
import unittest

from oikonome.sync import restore

from .test_restore_v2 import _zip
from .util import make_db, write_config

RAW = "INVENTED SHOP 4111 1111 1111 1111"
WHOLE = "Invented Shop 4111 1111 1111 1111"
AT = "2026-03-01 10:00:00+00"


class ARenameHeldWholeIsPresent(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method)"
            " VALUES (%s, %s, 'manual')", (RAW, WHOLE))
        self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical,"
            " to_canonical, at) VALUES (%s, 'Old Name', %s, %s)",
            (RAW, WHOLE, AT))

    def tearDown(self):
        self.conn.close()

    def test_the_journal_keeps_one_row(self):
        z = _zip({
            "merchant_canonical": (
                ["raw_merchant", "canonical", "method", "as_of"],
                [{"raw_merchant": RAW, "canonical": WHOLE, "method": "llm",
                  "as_of": AT}]),
            "merchant_renames": (
                ["id", "raw_merchant", "from_canonical", "to_canonical",
                 "at"],
                [{"id": 7, "raw_merchant": RAW, "from_canonical": "Old Name",
                  "to_canonical": WHOLE, "at": AT}])})
        _r, out = restore.restore_outcomes(self.conn, z)
        rows = self.conn.execute(
            "SELECT to_canonical FROM merchant_renames").fetchall()
        self.assertEqual([r["to_canonical"] for r in rows], [WHOLE])
        self.assertEqual(out.written.get("merchant_renames", 0), 0)


if __name__ == "__main__":
    unittest.main()
