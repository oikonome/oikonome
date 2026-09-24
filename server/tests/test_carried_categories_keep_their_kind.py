"""A category pin that follows a charge onto another row keeps the kind of
hand that wrote it.

``override_source`` says who set ``category_override`` — a person, a store's
item match, a bill's stamp — and every writer refuses to overwrite a kind
above its own. A pin that arrives on the live row with its kind lost reads
as one nobody set, so the next store or bill pass is free to overwrite a
person's answer; and a person's "use automatic" is an EMPTY pin wearing
their kind, so a live row that has been reset must not have that reset
undone by a pin carried from the row it replaced.

Both carries are covered: the settlement swap, where the aggregator names
the predecessor, and the re-anchor sweep, which finds the predecessor
itself.
"""
import datetime as dt
import unittest

from oikonome.sync import base as sync_base, reanchor
from oikonome.sync.base import Transaction

from .util import TODAY, add_txn, make_db, write_config


def _pin(conn, tid, category, source):
    conn.execute("UPDATE transactions SET category_override=%s, "
                 "override_source=%s WHERE id=%s", (category, source, tid))


def _cat_of(conn, tid):
    r = conn.execute("SELECT category_override, override_source "
                     "FROM transactions WHERE id=%s", (tid,)).fetchone()
    return r["category_override"], r["override_source"]


class ReanchorKeepsTheKindTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_carried_pin_arrives_with_the_hand_that_set_it(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 42.0,
                      "HARDWARE STORE", txn_id="hw_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 42.0,
                      "HARDWARE STORE", txn_id="hw_new")
        _pin(self.conn, old, "HOME_IMPROVEMENT", "user")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        reanchor.reanchor_stranded(self.conn)
        self.assertEqual(_cat_of(self.conn, new), ("HOME_IMPROVEMENT", "user"))
        # and the retired row let go of both halves together
        self.assertEqual(_cat_of(self.conn, old), (None, None))

    def test_a_live_row_reset_to_automatic_stays_reset(self):
        """An empty pin with a person's kind on it is their answer of "no
        category". A bill's stamp carried off the retired row must not fill
        that emptiness, or the reset un-resets itself a day later."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 18.0,
                      "CITY WATER", txn_id="cw_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 18.0,
                      "CITY WATER", txn_id="cw_new")
        _pin(self.conn, old, "RENT_AND_UTILITIES", "bill")
        _pin(self.conn, new, None, "user")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        reanchor.reanchor_stranded(self.conn)
        self.assertEqual(_cat_of(self.conn, new), (None, "user"))
        # what the live row refused stays where it was written, rather than
        # being nulled on the way past
        self.assertEqual(_cat_of(self.conn, old), ("RENT_AND_UTILITIES", "bill"))


    def test_a_reset_to_automatic_follows_the_charge_on_its_own(self):
        """"Use automatic" is an empty pin wearing the person's kind, and
        on a row with no note or receipt that kind is the ONLY thing left
        to carry. A sweep that looks for a pin or a child walks past it, so
        the twin arrives with no kind at all and the next store or bill
        pass stamps the category the person just took off."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 64.0,
                      "PET SUPPLY", txn_id="ps_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 64.0,
                      "PET SUPPLY", txn_id="ps_new")
        _pin(self.conn, old, None, "user")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(out["moved"], 1)
        self.assertEqual(_cat_of(self.conn, new), (None, "user"))
        self.assertEqual(_cat_of(self.conn, old), (None, None))

    def test_a_reset_the_twin_has_already_answered_for_stays_put(self):
        """The live row has its own pin, so the reset has nowhere to go.
        It stays on the retired row and is counted as left behind —
        silently dropping it would read as a row nobody ever touched."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 21.0,
                      "FERRY", txn_id="fy_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 21.0,
                      "FERRY", txn_id="fy_new")
        _pin(self.conn, old, None, "user")
        _pin(self.conn, new, "TRAVEL", "user")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["left"]), (0, 1))
        self.assertEqual(_cat_of(self.conn, new), ("TRAVEL", "user"))
        self.assertEqual(_cat_of(self.conn, old), (None, "user"))


class SettlementKeepsTheKindTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _hold(self, tid, name="CORNER BAKERY", amount=40.0):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id=tid, account_id="chk", date=TODAY, amount=amount, name=name,
            pending=True)])

    def _post(self, tid, old, name="CORNER BAKERY", amount=40.0):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id=tid, account_id="chk", date=TODAY, amount=amount, name=name,
            pending=False, raw={"pending_transaction_id": old})])

    def test_a_pin_made_while_pending_posts_with_its_kind(self):
        self._hold("s:p1")
        _pin(self.conn, "s:p1", "FOOD_AND_DRINK", "user")
        self._post("s:s1", "s:p1")
        self.assertEqual(_cat_of(self.conn, "s:s1"),
                         ("FOOD_AND_DRINK", "user"))

    def test_a_reset_on_the_posted_row_survives_a_replayed_page(self):
        """The aggregator re-sends a settled page routinely, so the carry
        runs again on a posted row the person has since reset to automatic.
        Without the kind on that row the carry reads it as untouched and
        restores the pin it already handed over."""
        self._hold("s:p2")
        _pin(self.conn, "s:p2", "FOOD_AND_DRINK", "user")
        self._post("s:s2", "s:p2")
        _pin(self.conn, "s:s2", None, "user")       # "use automatic"
        self._post("s:s2", "s:p2")                  # the page comes again
        self.assertEqual(_cat_of(self.conn, "s:s2"), (None, "user"))


if __name__ == "__main__":
    unittest.main()
