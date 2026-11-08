"""A merchant-wide category write's held undo names each row it moved by
id; when a re-key moves those rows onto new ids, the undo follows them.

The undo is held on the server for a week (category_undo) and restores
each row's prior category by id. Left naming the old ids after the rows
were re-hashed or keyed, it would answer "undone" and silently skip
every one of them, leaving the write in place.
"""
import json
import unittest
from unittest import mock

from oikonome.sync import adopt, base, csvimport

from .test_reimport_into_a_rekeyed_numbered_account_adds_nothing import (
    CSV, CSV_MAP, OLD, _seed)
from .util import make_db, write_config


class HeldUndo(unittest.TestCase):
    def test_held_undo_follows_rehashed_rows(self):
        conn = make_db()
        write_config(conn)
        self.addCleanup(conn.close)
        _seed(conn, OLD, "Everyday Checking …1234")
        csvimport.import_csv(conn, OLD, CSV, CSV_MAP)
        ids = sorted(r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s",
            (OLD,)).fetchall())
        payload = {"category": "FOOD_AND_DRINK", "canons": [],
                   "affected": [{"id": i, "prev": "GENERAL_MERCHANDISE"}
                                for i in ids] + [
                       {"id": "chk-untouched", "prev": None}]}
        conn.execute("INSERT INTO category_undo (payload)"
                     " VALUES (%s::jsonb)", (json.dumps(payload),))
        moved = base.rekey_numbered_accounts(conn)
        new = moved[OLD]
        now = {r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s",
            (new,)).fetchall()}
        self.assertEqual(len(now), len(ids))
        held = conn.execute(
            "SELECT payload FROM category_undo").fetchone()["payload"]
        named = [a["id"] for a in held["affected"]]
        self.assertEqual(named[-1], "chk-untouched")
        self.assertEqual(set(named[:-1]), now)
        self.assertEqual([a["prev"] for a in held["affected"]],
                         [a["prev"] for a in payload["affected"]])
        self.assertEqual(held["category"], "FOOD_AND_DRINK")


def _held_ids(conn):
    return [a["id"] for a in conn.execute(
        "SELECT payload FROM category_undo").fetchone()["payload"][
            "affected"]]


class UndoMovesWithItsSlice(unittest.TestCase):
    """The undo is rewritten in the transaction that moves its rows, not
    after every slice has committed: an undo claimed in between would
    restore nothing and still be spent, and a migrate dying in between
    would leave it on ids that are gone."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        _seed(self.conn, OLD, "Everyday Checking …1234")
        csvimport.import_csv(self.conn, OLD, CSV, CSV_MAP)
        self.ids = sorted(r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s",
            (OLD,)).fetchall())
        self.conn.execute(
            "INSERT INTO category_undo (payload) VALUES (%s::jsonb)",
            (json.dumps({"category": "FOOD_AND_DRINK", "canons": [],
                         "affected": [{"id": i, "prev": None}
                                      for i in self.ids]}),))

    def test_a_committed_slice_already_carries_its_undo(self):
        secret = base.txn_id_secret(self.conn, create=True)
        pairs = [(i, base.keyed_file_id(secret, i)) for i in self.ids[:2]]
        real = adopt._rewrite_txn_slice
        calls = []

        def second_slice_dies(conn, run):
            calls.append(run)
            if len(calls) > 1:
                raise RuntimeError("the migrate died here")
            return real(conn, run)

        with mock.patch.object(adopt, "_REWRITE_SLICE", 1), \
                mock.patch.object(adopt, "_rewrite_txn_slice",
                                  second_slice_dies):
            with self.assertRaises(RuntimeError):
                adopt.rewrite_txn_ids(self.conn, pairs)
        held = _held_ids(self.conn)
        self.assertIn(pairs[0][1], held)       # moved, and its undo with it
        self.assertNotIn(pairs[0][0], held)
        self.assertIn(pairs[1][0], held)       # not moved, undo untouched

    def test_stale_undo_ids_are_found_and_carried(self):
        # an undo left on an id its row has since moved off (a migrate
        # that died between the move and the carry)
        secret = base.txn_id_secret(self.conn, create=True)
        old = self.ids[0]
        new = base.keyed_file_id(secret, old)
        self.conn.execute("UPDATE transactions SET id = %s WHERE id = %s",
                          (new, old))
        self.assertGreaterEqual(base.carry_stale_mentions(self.conn), 1)
        held = _held_ids(self.conn)
        self.assertIn(new, held)
        self.assertNotIn(old, held)


if __name__ == "__main__":
    unittest.main()


class TheSliceLocksTheUndoFirst(UndoMovesWithItsSlice):
    """The undo door locks its row, then the rows it restores; the slice
    rewrites that row inside its own transaction, so it must lock the
    undo before the rows too, or each could wait on the other."""

    def test_the_undo_row_is_held_while_the_rows_are(self):
        import psycopg
        from oikonome.db import tenancy
        secret = base.txn_id_secret(self.conn, create=True)
        pairs = [(i, base.keyed_file_id(secret, i)) for i in self.ids[:2]]
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]
        seen = []

        def probe(conn, olds, news):
            # the rows are locked by now; the undo row must be too
            # SET LOCAL inside the transaction: the connection is pooled,
            # and a session-level timeout would ride on to whichever test
            # draws it next
            other = tenancy.tenant_connect(tid)
            try:
                try:
                    with other.transaction():
                        other.execute("SET LOCAL lock_timeout = '200ms'")
                        other.execute(
                            "SELECT 1 FROM category_undo FOR UPDATE")
                    seen.append("free")
                except psycopg.errors.LockNotAvailable:
                    seen.append("held")
            finally:
                other.close()

        with mock.patch.object(base, "carry_unmatch_memory", probe):
            adopt.rewrite_txn_ids(self.conn, pairs)
        self.assertEqual(seen, ["held"])
