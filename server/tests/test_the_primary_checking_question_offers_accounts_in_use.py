"""The "which is your main checking account" question offers accounts in use.

An account can carry a balance and still be one nobody uses: a closed
account left at $0 for its history, an old import with its last statement
balance. Offering those beside the account the bills actually run through
makes the question harder than it is. The invariant: when some candidates
have moved money in the last 90 days and others have not, only the ones in
use are offered — while at least two of them remain. The narrowing shortens
a question and never answers it: a list cut to one account would be pinned
without asking. When none has moved money (a files-only household's old
history), all of them are still offered.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast

from .util import add_txn, make_db


class PickableChecking(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            """INSERT INTO accounts (id, name, type, subtype, balance_current)
               VALUES ('old', 'Old Checking (closed)', 'depository',
                       'checking', 0)""")
        self.conn.execute(
            """INSERT INTO accounts (id, name, type, subtype, balance_current)
               VALUES ('chk2', 'Joint Checking', 'depository',
                       'checking', 900)""")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_an_unused_account_gives_way_to_the_ones_in_use(self):
        today = dt.date.today()
        for acct in ("chk", "chk2"):
            add_txn(self.conn, today - dt.timedelta(days=5), 20.0, "GROCER",
                    account=acct)
        add_txn(self.conn, today - dt.timedelta(days=400), 20.0, "GROCER",
                account="old")
        self.assertEqual(
            sorted(forecast.pickable_checking_ids(self.conn, self.cfg)),
            ["chk", "chk2"])

    def test_the_narrowing_never_answers_the_question_itself(self):
        """One account in use beside quiet ones is still a question: cut
        to one, the list would be pinned without anybody being asked."""
        today = dt.date.today()
        add_txn(self.conn, today - dt.timedelta(days=5), 20.0, "GROCER",
                account="chk")
        self.assertEqual(
            sorted(forecast.pickable_checking_ids(self.conn, self.cfg)),
            ["chk", "chk2", "old"])

    def test_with_nothing_recent_every_candidate_is_still_offered(self):
        today = dt.date.today()
        add_txn(self.conn, today - dt.timedelta(days=300), 20.0, "GROCER",
                account="chk")
        add_txn(self.conn, today - dt.timedelta(days=400), 20.0, "GROCER",
                account="old")
        self.assertEqual(
            sorted(forecast.pickable_checking_ids(self.conn, self.cfg)),
            ["chk", "chk2", "old"])


if __name__ == "__main__":
    unittest.main()
