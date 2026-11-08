"""Two hand-made accounts whose names slug alike are told apart by a "-2"
suffix the account doors append; when the number before it is a full
card number, the re-key must cut the number and keep the suffix.

Read as part of the number, a card typed in groups and suffixed
("visa-4111-1111-1111-1111-2") becomes a seventeen-digit run whose last
four ("1112") the card does not have, and the record of where the
account went names a different lineage from the one a re-import of the
"-2" account looks up. Each account must land on its own last four plus
its own suffix, keep a lineage of its own, and hold rows whose ids are
the ones an import into its new id mints, so the same statement adds
nothing to either.
"""
import unittest

from oikonome.sync import base, csvimport, restore

from .test_reimport_into_a_rekeyed_numbered_account_adds_nothing import (
    CSV, CSV_MAP, SPELLS_A_NUMBER, _export, _rows, _seed)
from .util import make_db, write_config

GROUPED = "manual:visa-4111-1111-1111-1111"        # a test card number
RUN = "manual:mc-5500000000000004"                  # another, unbroken


class ACollisionSuffix(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        for aid in (GROUPED, GROUPED + "-2", RUN, RUN + "-2"):
            _seed(self.conn, aid, "Card")
            self.assertTrue(csvimport.import_csv(
                self.conn, aid, CSV, CSV_MAP)["imported"])
        # the suffixed ones hidden: still moved, still their own lineage
        self.conn.execute(
            "UPDATE accounts SET user_removed_at = now()"
            " WHERE id IN (%s, %s)", (GROUPED + "-2", RUN + "-2"))

    def test_the_pure_ids_keep_the_suffix_beside_the_last_four(self):
        self.assertEqual(base.renumbered_manual_id(GROUPED + "-2"),
                         "manual:visa-1111-2")
        self.assertEqual(base.renumbered_manual_id(RUN + "-2"),
                         "manual:mc-0004-2")
        self.assertEqual(base.split_lineage_id(GROUPED + "-2"),
                         "manual:visa-…1111-2")
        self.assertEqual(base.split_lineage_id(RUN + "-2"),
                         "manual:mc-…0004-2")
        # an unsuffixed id, and every id migration 199 already cut, as before
        self.assertEqual(base.split_lineage_id(GROUPED),
                         "manual:visa-…1111")
        self.assertEqual(base.split_lineage_id(
            "manual:qif-visa-4111111111111111-2"),
            "manual:qif-visa-…1111-2")

    def test_each_account_moves_to_its_own_last_four_and_suffix(self):
        older = _export(self.conn)
        before = {a: _rows(self.conn, a) for a in
                  (GROUPED, GROUPED + "-2", RUN, RUN + "-2")}
        moved = base.rekey_numbered_accounts(self.conn)
        want = {GROUPED: "manual:visa-1111",
                GROUPED + "-2": "manual:visa-1111-2",
                RUN: "manual:mc-0004", RUN + "-2": "manual:mc-0004-2"}
        self.assertEqual(moved, want)
        olds = sorted(r["old_account_id"] for r in self.conn.execute(
            "SELECT old_account_id FROM account_adoptions").fetchall())
        self.assertEqual(olds, sorted(base.split_lineage_id(o)
                                      for o in want))
        self.assertEqual(len(set(olds)), 4)
        for old, new in want.items():
            rows = _rows(self.conn, new)
            self.assertEqual(len(rows), len(before[old]))
            for tid, (day, amount, name) in rows.items():
                self.assertIn(tid, {base.file_row_id("csv", new, day, amount,
                                                     name, n)
                                    for n in (1, 2)}, (new, name))
            self.assertEqual(base.cut_lineage_targets(self.conn, old), [new])
        # every lineage the "-N" walk of a full id finds, in order
        self.assertEqual(
            base.cut_lineage_candidates(self.conn, GROUPED),
            [(GROUPED, ["manual:visa-1111"]),
             (GROUPED + "-2", ["manual:visa-1111-2"])])
        # the same statement into each moved account adds nothing
        self.conn.execute("UPDATE accounts SET user_removed_at = NULL")
        for new in want.values():
            held = _rows(self.conn, new)
            csvimport.import_csv(self.conn, new, CSV, CSV_MAP)
            self.assertEqual(_rows(self.conn, new), held, new)
        # and the archive written before the move restores onto them
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"]
        restore.restore_zip(self.conn, older)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"], n)
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts").fetchall()]
        self.assertEqual([i for i in ids if SPELLS_A_NUMBER.search(i)], [])
        self.assertEqual(sorted(i for i in ids if i.startswith("manual:")),
                         sorted(want.values()))


class ATrailingGroupWithNoTwin(unittest.TestCase):
    """A number whose own last group is short ("…-7890-123") is not a
    door's suffix: no account was ever under the id without it. Read as
    one, the moved id and its lineage would keep seven of the number's
    digits; the whole run must be cut to its last four."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _moves(self, old, new, lineage):
        _seed(self.conn, old, "Checking")
        self.assertEqual(base.rekey_numbered_accounts(self.conn), {old: new})
        self.assertEqual([r["old_account_id"] for r in self.conn.execute(
            "SELECT old_account_id FROM account_adoptions").fetchall()],
            [lineage])
        self.assertEqual(base.cut_lineage_targets(self.conn, old), [new])
