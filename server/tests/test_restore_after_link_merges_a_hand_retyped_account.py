"""A restored account the person retyped by hand still finds its live twin.

When a bank is linked before the old install's archive is restored, the
restore reconciles each restored account onto the live one it duplicates.
That pairing compares the bank's own type, not the stored one: a person may
have set a card's type by hand (tracking it as a loan), and the feed keeps
naming it a credit card in the account's raw record. Compared by the stored
type, the retyped copy never matches its live twin nor is reported as a
possible duplicate, so its history is counted twice without a word.

What these protect:

  * a hand-retyped restored card merges onto the live card with its mask,
    and the type the person gave it is kept;
  * it is NEVER merged onto a live checking account that happens to share
    its last digits;
  * the same holds when the hand-set type is on the live copy instead;
  * a retyped copy whose bank type is unknown is never merged on its mask
    alone — it is named as a possible duplicate instead;
  * a link that serves a retyped account by the bank's type retires the
    tokenless shell it replaced, and one whose bank type is unknown keeps
    its shell.
"""

import datetime as dt
import unittest

from oikonome.engine.compat import jsonb
from oikonome.sync import adopt

from .util import make_db

D = dt.date


def _item(conn, iid, *, status, token=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id, institution_name,"
        " status, access_token, tx_update_status)"
        " VALUES (%s,'plaid','ins_nw','Northwind Bank',%s,%s,"
        " 'HISTORICAL_UPDATE_COMPLETE')", (iid, status, token))


def _account(conn, aid, iid, mask, *, type_, feed_type, user_set=False,
             name="Account"):
    raw = {"account_id": aid, "type": feed_type} if feed_type else None
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, mask, type_user_set,"
        " raw) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (aid, iid, name, type_, mask, user_set, jsonb(raw) if raw else None))


def _txn(conn, tid, aid, date, amount, name):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name,"
        " pending, removed) VALUES (%s,%s,%s,%s,%s,0,0)",
        (tid, aid, date, amount, name))


class RestoredRetypedTwin(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", status="restored")
        _item(self.conn, "new-item", status="ok", token="tok-live")

    def _restored_card(self, *, feed_type="credit"):
        _account(self.conn, "old-card", "old-item", "4242", type_="loan",
                 feed_type=feed_type, user_set=True, name="Fabrikam Card")
        _txn(self.conn, "old-pre", "old-card", D(2024, 3, 1), 80.0,
             "CONTOSO FUEL")

    def _live(self, aid, type_):
        _account(self.conn, aid, "new-item", "4242", type_=type_,
                 feed_type=type_)
        _txn(self.conn, f"{aid}-t", aid, D(2026, 7, 4), 15.0, "Cafe Corner")

    def _ids(self):
        return sorted(r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE mask = '4242'").fetchall())

    def test_a_retyped_restored_card_merges_onto_its_live_twin(self):
        self._restored_card()
        self._live("live-card", "credit")
        merged, notes = adopt.merge_restored_onto_live(self.conn,
                                                       ["old-card"])
        self.assertEqual([m["account_id"] for m in merged], ["live-card"])
        self.assertEqual(self._ids(), ["live-card"])
        row = self.conn.execute(
            "SELECT type, type_user_set FROM accounts WHERE id='live-card'"
        ).fetchone()
        self.assertEqual((row["type"], row["type_user_set"]), ("loan", True))
        self.assertEqual(self.conn.execute(
            "SELECT account_id FROM transactions WHERE id='old-pre'"
        ).fetchone()["account_id"], "live-card")
        self.assertFalse(any("twice" in n for n in notes))

    def test_a_retyped_card_never_merges_onto_a_checking_sharing_its_mask(self):
        self._restored_card()
        self._live("live-chk", "depository")
        pairs, ambiguous = adopt._restored_twins(self.conn, ["old-card"])
        self.assertEqual((pairs, ambiguous), ([], []))
        merged, _notes = adopt.merge_restored_onto_live(self.conn,
                                                        ["old-card"])
        self.assertEqual(merged, [])
        self.assertEqual(self._ids(), ["live-chk", "old-card"])

    def test_a_retype_on_the_live_copy_is_read_the_banks_way_too(self):
        _account(self.conn, "old-card", "old-item", "4242", type_="credit",
                 feed_type="credit", name="Fabrikam Card")
        _account(self.conn, "live-card", "new-item", "4242", type_="loan",
                 feed_type="credit", user_set=True)
        pairs, ambiguous = adopt._restored_twins(self.conn, ["old-card"])
        self.assertEqual([(r["id"], lv["id"]) for r, lv in pairs],
                         [("old-card", "live-card")])
        self.assertEqual(ambiguous, [])

    def test_an_unknown_bank_type_is_named_never_merged(self):
        self._restored_card(feed_type=None)
        self._live("live-card", "credit")
        pairs, ambiguous = adopt._restored_twins(self.conn, ["old-card"])
        self.assertEqual(pairs, [])
        self.assertEqual([r["id"] for r in ambiguous], ["old-card"])


class ReplacedShellByTheBanksType(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", status="restored")
        _item(self.conn, "new-item", status="ok", token="tok-live")

    def _status(self):
        return self.conn.execute(
            "SELECT status FROM items WHERE id='old-item'"
        ).fetchone()["status"]

    def test_a_shell_holding_a_retyped_card_the_link_serves_is_retired(self):
        _account(self.conn, "old-card", "old-item", "4242", type_="loan",
                 feed_type="credit", user_set=True)
        n = adopt.retire_replaced_shells(
            self.conn, live_item_id="new-item",
            incoming=[{"mask": "4242", "type": "credit"}])
        self.assertEqual(n, 1)
        self.assertEqual(self._status(), "archived")

    def test_a_retyped_card_of_unknown_bank_type_keeps_its_shell(self):
        _account(self.conn, "old-card", "old-item", "4242", type_="loan",
                 feed_type=None, user_set=True)
        n = adopt.retire_replaced_shells(
            self.conn, live_item_id="new-item",
            incoming=[{"mask": "4242", "type": "loan"}])
        self.assertEqual(n, 0)
        self.assertEqual(self._status(), "restored")


if __name__ == "__main__":
    unittest.main()
