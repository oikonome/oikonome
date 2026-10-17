"""A restore merge either lands whole or leaves nothing behind.

Merging a restored account onto the live one the household had already
linked moves its history, its notes and its settings across, deletes the
restored account and archives the tokenless connection it came on. If any
of that stops partway — a write fails, the job dies — a half-merged
account is worse than none: history on one copy and settings on the other,
or a connection archived whose account never moved. So each account's
merge is one transaction, the connection is archived inside it, and a
retry after a failure does the whole merge exactly once.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.sync import adopt, export, restore

from .test_restore_after_link_merges_onto_the_live_account import (
    _account, _fresh_household, _item, _txn)
from .util import make_db

D = dt.date


def _archive():
    a = make_db()
    try:
        _item(a, "old-item", token="tok-old")
        _account(a, "old-acct", "old-item", "1234")
        _txn(a, "old-pre", "old-acct", D(2024, 3, 1), 500.0, "LANDLORD RENT")
        _txn(a, "old-overlap", "old-acct", D(2026, 5, 20), 42.5,
             "POS DEBIT GROCERYMART 0042")
        a.execute("INSERT INTO transaction_notes (txn_id, note)"
                  " VALUES ('old-overlap', 'birthday party food')")
        return export.build_zip(a)
    finally:
        a.close()


class _Restored(unittest.TestCase):
    """The archive restored WITHOUT its merge, so each test can run the
    merge itself under the failure it wants."""

    def setUp(self):
        data = _archive()
        self.b = _fresh_household(type(self).__name__.lower())
        self.addCleanup(self.b.close)
        _item(self.b, "new-item", token="tok-new",
              update_status="HISTORICAL_UPDATE_COMPLETE")
        _account(self.b, "new-acct", "new-item", "1234")
        _txn(self.b, "live-overlap", "new-acct", D(2026, 5, 20), 42.5,
             "Grocerymart")
        with mock.patch.object(adopt, "merge_restored_onto_live",
                               return_value=([], [])):
            restore.restore_zip(self.b, data)
        self.assertIsNotNone(self.b.execute(
            "SELECT 1 FROM accounts WHERE id = 'old-acct'").fetchone())

    def state(self):
        b = self.b
        return {
            "accounts": sorted(r["id"] for r in b.execute(
                "SELECT id FROM accounts WHERE id IN"
                " ('old-acct', 'new-acct')").fetchall()),
            "rows": sorted((r["id"], r["account_id"], r["removed"])
                           for r in b.execute(
                               "SELECT id, account_id, removed"
                               " FROM transactions").fetchall()),
            "notes": sorted((r["txn_id"], r["note"]) for r in b.execute(
                "SELECT txn_id, note FROM transaction_notes").fetchall()),
            "adoptions": b.execute(
                "SELECT count(*) AS n FROM account_adoptions"
                ).fetchone()["n"],
            "shell": b.execute(
                "SELECT status FROM items WHERE id = 'old-item'"
                ).fetchone()["status"],
        }

    def merge(self):
        return adopt.merge_restored_onto_live(self.b, ["old-acct"])

    def assert_merged_once(self):
        s = self.state()
        self.assertEqual(s["accounts"], ["new-acct"])
        self.assertEqual(s["adoptions"], 1)
        self.assertEqual(s["shell"], "archived")
        live = {i for i, a, r in s["rows"] if a == "new-acct" and not r}
        self.assertEqual(live, {"old-pre", "live-overlap"})
        self.assertEqual(s["notes"],
                         [("live-overlap", "birthday party food")])


class AFailedMergeChangesNothing(_Restored):

    def test_a_failure_after_the_rows_moved_rolls_all_of_it_back(self):
        before = self.state()
        with mock.patch.object(adopt, "_rewrite_account_id_in_config",
                               side_effect=RuntimeError("settings write")):
            merged, notes = self.merge()
        self.assertEqual(merged, [])
        self.assertTrue(notes)          # the account is named as doubled
        self.assertEqual(self.state(), before)

    def test_a_retry_after_the_failure_merges_once(self):
        with mock.patch.object(adopt, "_rewrite_account_id_in_config",
                               side_effect=RuntimeError("settings write")):
            self.merge()
        merged, _ = self.merge()
        self.assertEqual(len(merged), 1)
        self.assert_merged_once()
        merged, notes = self.merge()          # and again: nothing to do
        self.assertEqual((merged, notes), ([], []))
        self.assert_merged_once()


class TheConnectionIsArchivedWithItsAccount(_Restored):
    """The connection is archived if and only if its account moved. Written
    apart from the move, a job that dies in between leaves a 'restored'
    connection with nothing on it, asking to be reconnected — and a retry
    never archives it, because its account has already gone."""

    def test_the_connection_is_never_archived_apart_from_the_move(self):
        real = adopt._retire_donors
        calls = []

        def dies_after_the_first_merge(conn, item_ids):
            calls.append(item_ids)
            if conn.info.transaction_status == 0:      # outside the merge
                raise RuntimeError("the job died")
            return real(conn, item_ids)

        with mock.patch.object(adopt, "_retire_donors",
                               side_effect=dies_after_the_first_merge):
            try:
                self.merge()
            except RuntimeError:
                pass
        s = self.state()
        moved = "old-acct" not in s["accounts"]
        self.assertEqual(moved, s["shell"] == "archived", s)
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
