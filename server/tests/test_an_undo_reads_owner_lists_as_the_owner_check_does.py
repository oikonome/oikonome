"""An undo deletes a row exactly when the owner check says the undone
import is its only owner — however the stored list is spelled.

The owner check (reimport.batch_owner_list) reads a `_batches` list as
its distinct non-empty string ids, so `[b, b]`, `[b, 5]` and `[b, ""]`
are owned by `b` alone. The rollback's SQL and Recent imports' count
must read the same list the same way: counting raw elements instead
would keep a row the import alone owns (the undo then leaves it owned by
nobody, beyond the reach of every later undo) and leave it out of the
count Recent imports offers to remove.

Every amount and name is invented.
"""

import json
import unittest

from oikonome.sync import batches, ofximport, reimport

from .test_imported_dates_are_the_day_the_source_printed import _line, _ofx
from .util import make_db, write_config


class UndoReadsOwnersAsTheOwnerCheck(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _import(self):
        bid = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        r = ofximport.import_ofx(
            self.conn, "chk",
            _ofx(_line("A", "20260310", "-5.00", "SHOP A")), batch_id=bid)
        batches.finish(self.conn, bid, r["imported"])
        return bid

    def _check(self, shape):
        b = self._import()
        other = batches.create(self.conn, "ofx", "chk", "other.ofx")
        owners = [b if x == "B" else other if x == "O" else x for x in shape]
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_set(raw, '{_batches}',"
            " %s::jsonb) WHERE id = 'ofx:chk:A'", (json.dumps(owners),))
        raw = self.conn.execute("SELECT raw FROM transactions"
                                " WHERE id = 'ofx:chk:A'").fetchone()["raw"]
        sole = reimport.batch_owner_list(raw) == [b]
        listed = {r["id"]: r["row_count"]
                  for r in batches.recent(self.conn, 10)}
        self.assertEqual(listed.get(b), 1 if sole else 0, shape)
        batches.rollback(self.conn, b)
        gone = self.conn.execute("SELECT 1 FROM transactions"
                                 " WHERE id = 'ofx:chk:A'").fetchone() is None
        self.assertEqual(gone, sole, shape)

    def test_a_repeated_owner_is_one_owner(self):
        self._check(["B", "B"])

    def test_a_non_string_entry_owns_nothing(self):
        self._check(["B", 5])

    def test_an_empty_or_null_entry_owns_nothing(self):
        self._check(["B", "", None])

    def test_a_nested_list_entry_owns_nothing(self):
        self._check([["B"], "B"])

    def test_two_owners_keep_the_row(self):
        self._check(["B", "O", "B"])

    def test_only_junk_is_owned_by_nobody(self):
        self._check([5, "", None])


if __name__ == "__main__":
    unittest.main()
