"""Which import batches own a row, and so which undo may delete it.

Rolling an import back deletes the rows it is the LAST owner of and
nothing else:

- a line an import adds is that import's;
- a later import whose file holds the same line joins its owners, so
  undoing the first import leaves the row to the later one, and undoing
  the last owner removes it;
- a row no batch owns (a feed's, one stored before batches or by an import
  without one) stays owned by nobody through every later import, so no
  undo ever deletes it;
- a row an import brings back into the ledger (it had been set aside) is
  one that import added, and that import alone owns it.

Every amount and name is invented.
"""

import datetime as dt
import random
import unittest

from oikonome.sync import base, batches, ofximport, reimport

from .test_imported_dates_are_the_day_the_source_printed import _line, _ofx
from .util import make_db, write_config


class BatchOwnsWhatItAdded(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _file(self, *fids):
        return _ofx("".join(_line(f, f"202603{1 + i % 27:02d}", "-5.00",
                                  f"SHOP {f}")
                            for i, f in enumerate(fids)))

    def _import(self, data):
        bid = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        r = ofximport.import_ofx(self.conn, "chk", data, batch_id=bid)
        batches.finish(self.conn, bid, r["imported"])
        return bid

    def _raw(self, fid):
        row = self.conn.execute("SELECT raw FROM transactions WHERE id = %s",
                                (f"ofx:chk:{fid}",)).fetchone()
        return None if row is None else dict(row["raw"])

    def test_an_untagged_row_survives_undoing_an_import_that_matched_it(self):
        self._import(self._file("A"))
        self.conn.execute("UPDATE transactions SET raw = raw - '_batch' "
                          "- '_batches' WHERE id = 'ofx:chk:A'")
        b = self._import(self._file("A"))
        self.assertNotIn(b, self._raw("A").get("_batches") or [])
        batches.rollback(self.conn, b)
        self.assertIsNotNone(self._raw("A"))

    def test_undoing_an_import_removes_the_rows_it_added(self):
        b = self._import(self._file("A", "B"))
        self.assertEqual(self._raw("A")["_batches"], [b])
        batches.rollback(self.conn, b)
        self.assertIsNone(self._raw("A"))
        self.assertIsNone(self._raw("B"))

    def test_a_row_added_by_one_import_and_matched_by_another(self):
        a = self._import(self._file("A"))
        b = self._import(self._file("A", "B"))
        raw = self._raw("A")
        self.assertEqual((raw["_batches"], raw["_batch"]), ([a, b], b))
        self.assertEqual(self._raw("B")["_batches"], [b])
        batches.rollback(self.conn, b)
        self.assertIsNotNone(self._raw("A"))      # A still owns it
        self.assertIsNone(self._raw("B"))         # B was B's alone
        self.assertEqual(self._raw("A")["_batches"], [a])
        batches.rollback(self.conn, a)
        self.assertIsNone(self._raw("A"))

    def test_undoing_the_first_import_hands_its_rows_to_a_later_one(self):
        lines = [f"L{i}" for i in range(30)]
        a = self._import(self._file(*lines))
        b = self._import(self._file(*lines))
        res = batches.rollback(self.conn, a)
        self.assertEqual(int(res), 0)
        # the later import covered the same window: the undo says so
        self.assertIsNotNone(res.warning)
        self.assertIn("1 later import", res.warning)
        for f in lines:
            self.assertEqual(self._raw(f)["_batches"], [b])
        self.assertEqual(int(batches.rollback(self.conn, b)), 30)
        self.assertTrue(all(self._raw(f) is None for f in lines))

    def test_the_history_shows_what_an_undo_would_remove_today(self):
        a = self._import(self._file("A"))
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                          "VALUES ('ofx:chk:A', 'kept')")
        b = self._import(self._file("A"))

        def shown():
            return {x["id"]: (x["row_count"], x["annotations"])
                    for x in batches.recent(self.conn)}
        # both own the row: undoing either alone removes nothing
        self.assertEqual((shown()[a], shown()[b]), ((0, 0), (0, 0)))
        batches.rollback(self.conn, a)
        self.assertEqual(shown()[b], (1, 1))

    def test_a_row_brought_back_into_the_ledger_is_that_imports(self):
        a = self._import(self._file("A"))
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE id = 'ofx:chk:A'")
        bid = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        r = ofximport.import_ofx(self.conn, "chk", self._file("A"),
                                 batch_id=bid)
        batches.finish(self.conn, bid, r["imported"])
        self.assertEqual((r["imported"], r["matched"]), (1, 0))
        self.assertEqual(self._raw("A")["_batches"], [bid])
        self.assertEqual(int(batches.rollback(self.conn, bid)), 1)
        self.assertIsNone(self._raw("A"))
        self.assertEqual(int(batches.rollback(self.conn, a)), 0)

    def test_a_row_no_batch_owns_survives_every_later_import_undone(self):
        self._import(self._file("A"))
        self.conn.execute("UPDATE transactions SET raw = raw - '_batch' "
                          "- '_batches' WHERE id = 'ofx:chk:A'")
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                          "VALUES ('ofx:chk:A', 'kept')")
        b = self._import(self._file("A"))
        c = self._import(self._file("A"))
        self.assertEqual(self._raw("A")["_batches"], [])
        batches.rollback(self.conn, b)
        self.assertIsNotNone(self._raw("A"))
        batches.rollback(self.conn, c)
        self.assertIsNotNone(self._raw("A"))
        self.assertEqual(self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id = 'ofx:chk:A'"
        ).fetchone()["note"], "kept")

    def test_a_single_tag_row_is_owned_by_its_tag_and_later_imports(self):
        # a row from before owner lists: `_batch` names the import that
        # added it, and there is no `_batches` key at all
        a = self._import(self._file("A"))
        self.conn.execute("UPDATE transactions SET raw = raw - '_batches' "
                          "WHERE id = 'ofx:chk:A'")
        b = self._import(self._file("A"))
        c = self._import(self._file("A"))
        self.assertEqual(self._raw("A")["_batches"], [a, b, c])
        batches.rollback(self.conn, b)
        batches.rollback(self.conn, c)
        self.assertIsNotNone(self._raw("A"))
        batches.rollback(self.conn, a)
        self.assertIsNone(self._raw("A"))

    def test_a_non_list_owner_value_is_owned_by_nobody(self):
        self._import(self._file("A"))
        b = self._import(self._file("A"))
        self.conn.execute("UPDATE transactions SET raw = jsonb_set(raw, "
                          "'{_batches}', '{\"x\": 1}'::jsonb) "
                          "WHERE id = 'ofx:chk:A'")
        self.assertEqual(reimport.batch_owners(self._raw("A")), set())
        c = self._import(self._file("A"))
        self.assertEqual(self._raw("A")["_batches"], [])
        for bid in (b, c):
            batches.rollback(self.conn, bid)
        self.assertIsNotNone(self._raw("A"))


class RandomImportsAndUndos(unittest.TestCase):
    """Over random runs of imports (each adding some new lines and writing
    some stored ones again) and undos in random order: an undo deletes a
    row only when that batch is its last remaining owner, a row no batch
    ever owned survives every run, and once every batch is undone exactly
    the never-owned rows remain."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _write(self, lines, bid):
        txns = [base.Transaction(id=f"csv:p:{n}", account_id="chk",
                                 date=dt.date(2026, 3, 1 + n % 27),
                                 amount=5.0 + n, name=f"SHOP {n}")
                for n in lines]
        base.upsert_transactions(self.conn, batches.tag(self.conn, txns, bid))

    def _live(self):
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'csv:p:%%'")}

    def test_random_runs(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                self.conn.execute(
                    "DELETE FROM transactions WHERE id LIKE 'csv:p:%%'")
                rnd = random.Random(seed)
                orphans = {f"csv:p:{n}" for n in range(3)}
                base.upsert_transactions(self.conn, [
                    base.Transaction(id=i, account_id="chk",
                                     date=dt.date(2026, 3, 1), amount=1.0,
                                     name=i) for i in orphans])
                # the model: each row's owners, nobody's for the orphans
                owners: dict[str, list] = {i: [] for i in orphans}
                made, nxt = [], 3
                for _ in range(rnd.randint(3, 6)):
                    bid = batches.create(self.conn, "csv", "chk", "f.csv")
                    old = sorted(int(i.rsplit(":", 1)[1])
                                 for i in self._live())
                    again = rnd.sample(old, rnd.randint(0, len(old)))
                    new = list(range(nxt, nxt + rnd.randint(0, 3)))
                    nxt += len(new)
                    self._write(again + new, bid)
                    for n in again:
                        if owners[f"csv:p:{n}"]:
                            owners[f"csv:p:{n}"].append(bid)
                    for n in new:
                        owners[f"csv:p:{n}"] = [bid]
                    made.append(bid)
                rnd.shuffle(made)
                for bid in made:
                    last = {i for i, o in owners.items() if o == [bid]}
                    before = self._live()
                    batches.rollback(self.conn, bid)
                    self.assertEqual(before - self._live(), last)
                    for i in last:
                        del owners[i]
                    for o in owners.values():
                        if bid in o:
                            o.remove(bid)
                    self.assertLessEqual(orphans, self._live())
                self.assertEqual(self._live(), orphans)


if __name__ == "__main__":
    unittest.main()
