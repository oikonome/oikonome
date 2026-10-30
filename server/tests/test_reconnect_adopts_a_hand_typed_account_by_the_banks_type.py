"""Reconnect adoption compares a held account by the bank's own type.

A person may set an account's type by hand (a card they track as a loan).
The feed keeps sending its own type, and the sync keeps that in the
account's raw record even while the hand-set type stands. Adoption, the
moved-login check and the repeat check must all compare by the bank's
type, or they disagree: a connection is released as "moved", adoption then
misses the retyped account, its history is stranded on a released Item and
the new link mints a duplicate.

What these protect:

  * a hand-retyped card is adopted onto the incoming card with the same
    mask, keeps its history and the type the person gave it;
  * it is NEVER welded onto an incoming account of another bank type that
    happens to share its last digits (a checking account);
  * pairing weighs every incoming account before any is paired, so the
    order the feed lists them in cannot decide where a history goes;
  * a held account whose bank type is unknown pairs on its mask only when
    that mask names exactly one account on each side.
"""

import datetime as dt
import unittest

from oikonome.engine.compat import jsonb
from oikonome.sync import adopt

from .util import make_db


def _item(conn, iid, status, inst_id, token=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id, "
        "institution_name, status, access_token) "
        "VALUES (%s,'plaid',%s,'Northwind Bank',%s,%s)",
        (iid, inst_id, status, token))


def _retyped_card(conn, aid, iid, mask, *, feed_type="credit", txns=3):
    raw = {"account_id": aid, "type": feed_type} if feed_type else None
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype, mask, "
        "type_user_set, raw) VALUES (%s,%s,'Retyped Card','loan',"
        "'credit card',%s,TRUE,%s)",
        (aid, iid, mask, jsonb(raw) if raw else None))
    for i in range(txns):
        conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed) VALUES (%s,%s,%s,%s,'LOAN PAYMENT',0,0)",
            (f"{aid}-t{i}", aid, dt.date(2026, 9, 12 + i), 50.0 + i))


class HandTypedAdoptionTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_nw")
        _item(self.conn, "new-item", "ok", "ins_nw", token="live-token")

    def _adopt(self, incoming):
        return adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_nw",
            institution_name="Northwind Bank", incoming=incoming)

    def _rows(self, mask):
        return [(r["id"], r["item_id"], r["type"], r["type_user_set"])
                for r in self.conn.execute(
                    "SELECT id, item_id, type, type_user_set FROM accounts "
                    "WHERE mask=%s AND item_id IN ('old-item','new-item') "
                    "ORDER BY id", (mask,))]

    def _count(self, aid):
        return self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id=%s",
            (aid,)).fetchone()["n"]

    def test_a_retyped_card_is_adopted_by_the_banks_type(self):
        _retyped_card(self.conn, "old-3103", "old-item", "3103")
        out = self._adopt([{"account_id": "new-3103", "mask": "3103",
                            "type": "credit"}])
        self.assertEqual([o["old_account_id"] for o in out], ["old-3103"])
        self.assertEqual(self._rows("3103"),
                         [("new-3103", "new-item", "loan", True)])
        self.assertEqual(self._count("new-3103"), 3)

    def test_a_retyped_card_never_takes_a_checking_account_sharing_its_mask(self):
        _retyped_card(self.conn, "old-3103", "old-item", "3103")
        out = self._adopt([{"account_id": "chk-3103", "mask": "3103",
                            "type": "depository"}])
        self.assertEqual(out, [])
        self.assertEqual(self._rows("3103"),
                         [("old-3103", "old-item", "loan", True)])
        self.assertEqual(self._count("old-3103"), 3)

    def test_the_card_pairs_with_the_card_whatever_order_the_feed_lists(self):
        for order in ((0, 1), (1, 0)):
            with self.subTest(order=order):
                conn = make_db()
                try:
                    _item(conn, "old-item", "restored", "ins_nw")
                    _item(conn, "new-item", "ok", "ins_nw", token="t")
                    _retyped_card(conn, "old-3103", "old-item", "3103")
                    feed = [{"account_id": "chk-3103", "mask": "3103",
                             "type": "depository"},
                            {"account_id": "card-3103", "mask": "3103",
                             "type": "credit"}]
                    out = adopt.adopt_accounts(
                        conn, item_id="new-item", institution_id="ins_nw",
                        institution_name="Northwind Bank",
                        incoming=[feed[i] for i in order])
                    self.assertEqual(
                        [(o["account_id"], o["old_account_id"]) for o in out],
                        [("card-3103", "old-3103")])
                finally:
                    conn.close()

    def test_an_account_with_no_bank_type_adopts_on_a_mask_unique_both_sides(self):
        _retyped_card(self.conn, "old-3103", "old-item", "3103",
                      feed_type=None)
        out = self._adopt([{"account_id": "new-3103", "mask": "3103",
                            "type": "credit"}])
        self.assertEqual([o["old_account_id"] for o in out], ["old-3103"])
        self.assertEqual(self._count("new-3103"), 3)

    def test_an_account_with_no_bank_type_refuses_a_mask_shared_by_two_incoming(self):
        _retyped_card(self.conn, "old-3103", "old-item", "3103",
                      feed_type=None)
        out = self._adopt([{"account_id": "chk-3103", "mask": "3103",
                            "type": "depository"},
                           {"account_id": "card-3103", "mask": "3103",
                            "type": "credit"}])
        self.assertEqual(out, [])
        self.assertEqual(self._count("old-3103"), 3)


class PairAccountsTests(unittest.TestCase):
    """The pairing rule on its own, without a database."""

    def _held(self, id_, mask, type_, *, by_hand=False, feed=None):
        return {"id": id_, "mask": mask, "type": type_,
                "type_user_set": by_hand, "feed_type": feed}

    def _pairs(self, pool, incoming):
        return [(a["account_id"], c["id"])
                for a, c in adopt.pair_accounts(pool, incoming)]

    def test_the_banks_type_is_read_past_a_hand_set_one(self):
        self.assertEqual(
            adopt.feed_identity(self._held("a", " 3103 ", "loan",
                                           by_hand=True, feed="Credit")),
            ("credit", "3103"))
        self.assertEqual(
            adopt.feed_identity(self._held("a", "3103", "loan",
                                           by_hand=True)),
            (None, "3103"))
        self.assertEqual(
            adopt.feed_identity(self._held("a", "3103", "credit")),
            ("credit", "3103"))

    def test_two_incoming_sharing_a_mask_and_type_take_nothing(self):
        pool = [self._held("old", "1111", "depository")]
        inc = [{"account_id": "x", "mask": "1111", "type": "depository"},
               {"account_id": "y", "mask": "1111", "type": "depository"}]
        self.assertEqual(self._pairs(pool, inc), [])

    def test_an_exact_pair_beside_an_untyped_twin_mask_is_still_made(self):
        # the typed account pairs exactly; the untyped one shares its mask
        # with it in the pool, so the fallback refuses rather than guess
        pool = [self._held("card", "2222", "credit"),
                self._held("old", "2222", "loan", by_hand=True)]
        inc = [{"account_id": "n-card", "mask": "2222", "type": "credit"}]
        self.assertEqual(self._pairs(pool, inc), [("n-card", "card")])


if __name__ == "__main__":
    unittest.main()
