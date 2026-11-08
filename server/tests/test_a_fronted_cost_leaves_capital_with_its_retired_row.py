"""A capitalized or reimbursable cost stops counting on the capital
account the moment its charge leaves the books, however it left.

The books skip every removed row. A fronted movement is the other half of
that cost: while it stays on a row the books no longer count, it credits
the member's capital (or keeps the business owing them) for money the
books say was never spent. That holds for a hold the person retired, a
hold the connector stopped carrying, and a file row retired onto another
source's copy that already carries its own answer. Putting the row back
(retiring is reversible) brings the movement back with it.
"""
import datetime as dt
import unittest

from oikonome.engine import entities, equity
from oikonome.sync import adopt, base
from oikonome.web import data

from .util import TODAY, add_txn, make_db, write_config


class RetiredFrontedTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Example Lodging Co", structure="sole_prop")
        self.eid = self.ent["id"]
        self.old = TODAY - dt.timedelta(days=data.PENDING_STUCK_DAYS + 2)

    def _summary(self):
        return equity.capital_summary(self.conn, self.eid, today=TODAY)

    def test_a_hold_the_person_retires_leaves_capital(self):
        hold = add_txn(self.conn, self.old, 1234.56, "HARBORVIEW INN DEPOSIT",
                       pending=1)
        equity.contribute_expense(self.conn, self.eid, hold)
        self.assertEqual(self._summary()["contributions"], 1234.56)
        data.retire_pending(self.conn, hold, today=TODAY)
        s = self._summary()
        self.assertEqual(s["contributions"], 0.0)
        self.assertEqual(s["capital_balance"], 0.0)
        self.assertEqual(s["movements_balance"], 0.0)

    def test_a_reimbursable_hold_the_person_retires_is_owed_no_more(self):
        hold = add_txn(self.conn, self.old, 410.0, "HARBORVIEW INN DEPOSIT",
                       pending=1)
        equity.reimburse_expense(self.conn, self.eid, hold)
        self.assertEqual(self._summary()["reimbursements"], 410.0)
        data.retire_pending(self.conn, hold, today=TODAY)
        self.assertEqual(self._summary()["reimbursements"], 0.0)

    def test_putting_the_retired_hold_back_counts_it_again(self):
        hold = add_txn(self.conn, self.old, 75.25, "HARBORVIEW INN DEPOSIT",
                       pending=1)
        equity.contribute_expense(self.conn, self.eid, hold)
        data.retire_pending(self.conn, hold, today=TODAY)
        self.conn.execute("UPDATE transactions SET removed = 0,"
                          " retired_at = NULL WHERE id = %s", (hold,))
        self.assertEqual(self._summary()["contributions"], 75.25)

    def test_a_hold_the_connector_stops_carrying_leaves_capital(self):
        hold = add_txn(self.conn, TODAY, 62.0, "LANTERN CAFE",
                       pending=1, txn_id="sfin:hold-a")
        equity.contribute_expense(self.conn, self.eid, hold)
        base.reconcile_vanished_pendings(
            self.conn, ["card"], set(), "sfin:", TODAY - dt.timedelta(days=30))
        self.assertEqual(self._summary()["contributions"], 0.0)

    def test_a_movement_left_on_a_retired_twin_is_not_counted_twice(self):
        keep = add_txn(self.conn, TODAY, 88.0, "QUILL OFFICE SUPPLY")
        lose = add_txn(self.conn, TODAY, 88.0, "QUILL OFFICE SUPPLY",
                       txn_id="csv:quill-1")
        equity.contribute_expense(self.conn, self.eid, keep)
        equity.contribute_expense(self.conn, self.eid, lose)
        self.assertEqual(self._summary()["contributions"], 176.0)
        adopt._retire_twin(self.conn, keep, lose)
        # the survivor already answered the question, so the loser's
        # movement stays behind on a row the books no longer count
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM equity_movement WHERE txn_id = %s",
            (lose,)).fetchone()["n"], 1)
        self.assertEqual(self._summary()["contributions"], 88.0)

    def test_a_settled_hold_keeps_counting_on_its_posted_charge(self):
        hold = add_txn(self.conn, TODAY - dt.timedelta(days=2), 40.0,
                       "LANTERN CAFE", pending=1, txn_id="hold-b")
        equity.contribute_expense(self.conn, self.eid, hold)
        base.upsert_transactions(self.conn, [base.Transaction(
            id="posted-b", account_id="card", date=TODAY, amount=40.0,
            name="LANTERN CAFE", category_primary="FOOD_AND_DRINK",
            raw={"pending_transaction_id": "hold-b"})])
        self.assertEqual(self._summary()["contributions"], 40.0)


if __name__ == "__main__":
    unittest.main()
