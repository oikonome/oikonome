"""A restored account the bank reports with no mask, restored onto a
household that already linked the same bank, is never merged on a guess —
there is nothing to prove it is the live account — but it is never left
unmentioned either: the restore names it as an account that may now appear
twice, so the person can link or remove the extra copy."""

import unittest

from oikonome.sync import restore

from .test_restore_after_link_merges_onto_the_live_account import (
    D, _Base, _account, _item, _txn)


class _NoMask(_Base):
    masks = (None,)

    def _link_with(self, *accounts):
        b = self.b
        _item(b, "new-item", token="tok-new",
              update_status="HISTORICAL_UPDATE_COMPLETE")
        for aid, mask, type_ in accounts:
            _account(b, aid, "new-item", mask, type_=type_, name="HSA")
        _txn(b, "live-overlap", accounts[0][0], D(2026, 5, 20), 42.5,
             "Grocerymart")

    def _notes(self, counts):
        return " ".join(counts.get("_notes") or [])


class UnmaskedRestoredAccountIsNamed(_NoMask):

    def test_a_live_unmasked_account_of_its_type_is_flagged_not_merged(self):
        self._link_with(("new-acct", None, "depository"))
        counts = restore.restore_zip(self.b, self.zip)
        ids = sorted(r["id"] for r in self.b.execute(
            "SELECT id FROM accounts WHERE item_id IN"
            " ('new-item', 'old-item')").fetchall())
        # nothing merged: both copies stand, the restored history intact
        self.assertEqual(ids, ["new-acct", "old-acct-0"])
        self.assertIn("old-overlap", self._live("old-acct-0"))
        notes = self._notes(counts)
        self.assertIn("twice", notes)
        self.assertIn("Checking", notes)
        self.assertNotIn("Checking ••", notes)

    def test_the_only_live_account_of_its_type_is_flagged(self):
        self._link_with(("new-acct", "9876", "depository"))
        counts = restore.restore_zip(self.b, self.zip)
        self.assertIn("Checking", self._notes(counts))
        self.assertIn("twice", self._notes(counts))

    def test_masked_live_accounts_of_another_type_say_nothing(self):
        self._link_with(("new-acct", "9876", "credit"))
        counts = restore.restore_zip(self.b, self.zip)
        self.assertNotIn("twice", self._notes(counts))


if __name__ == "__main__":
    unittest.main()
