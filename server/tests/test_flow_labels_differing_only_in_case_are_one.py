"""A deposit a file import stored under its bank's own spelling of the
income label ("Income", "income") before imports wrote the canonical
INCOME is income, as the same line imported today is.

Every income figure, the paycheck tile and the spend exclusions match
the literal INCOME, so a legacy row spelled any other way was spending
money in to all of them. Migration 223 rewrites exactly the rows today's
importer would label INCOME (flowmap.flow_category on the stored label):
a deposit whose label is INCOME in another case. An outflow filed under
"Income" is a correction or a reversal, which the importer leaves as
written, and so does the migration; a person's own override is theirs.

Every amount and name is invented.
"""

import pathlib
import unittest

from oikonome.sync import flowmap

from .util import add_txn, make_db, write_config

MIGRATION = (pathlib.Path(flowmap.__file__).parent.parent / "db" /
             "migrations" /
             "223_flow_labels_differing_only_in_case_are_one.sql")

# (label, amount in the engine's sign: negative is money in)
CASES = [
    ("Income", -1000.0),
    ("income", -250.0),
    ("iNcOmE", -75.0),
    ("INCOME", -60.0),
    ("Income", 40.0),            # an outflow under the label: left alone
    ("Incomes", -30.0),          # not the label in another case
    ("\u0131ncome", -25.0),     # dotless i: not INCOME to the importer
    ("Paycheck", -20.0),         # another word: not this migration's
    ("GENERAL_MERCHANDISE", -10.0),
]


class FlowLabelsDifferingOnlyInCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_primary, category_override FROM transactions"
            " WHERE id = %s", (tid,)).fetchone()

    def test_a_deposit_under_the_label_in_another_case_becomes_income(self):
        ids = [add_txn(self.conn, "2026-03-02", amt, f"DEPOSIT {i}",
                       primary=label, account="chk")
               for i, (label, amt) in enumerate(CASES)]
        self.conn.execute(MIGRATION.read_text())
        for tid, (label, amt) in zip(ids, CASES):
            want = ("INCOME" if label.isascii()
                    and label.upper() == "INCOME" and amt < 0 else label)
            self.assertEqual(self._cat(tid)["category_primary"], want,
                             (label, amt))

    def test_the_migration_agrees_with_todays_importer(self):
        for i, (label, amt) in enumerate(CASES):
            if label.upper() != "INCOME":
                continue
            tid = add_txn(self.conn, "2026-03-02", amt, f"PAY {i}",
                          primary=label, account="chk")
            self.conn.execute(MIGRATION.read_text())
            imported = flowmap.flow_category(label, amt)[0] or label
            self.assertEqual(self._cat(tid)["category_primary"], imported,
                             (label, amt))

    def test_a_persons_override_is_left_as_they_wrote_it(self):
        tid = add_txn(self.conn, "2026-03-02", -500.0, "DEPOSIT",
                      primary="Income", override="income", account="chk")
        self.conn.execute(MIGRATION.read_text())
        row = self._cat(tid)
        self.assertEqual((row["category_primary"], row["category_override"]),
                         ("INCOME", "income"))

    def test_running_it_twice_changes_nothing_more(self):
        tid = add_txn(self.conn, "2026-03-02", -500.0, "DEPOSIT",
                      primary="Income", account="chk")
        self.conn.execute(MIGRATION.read_text())
        n = self.conn.execute(MIGRATION.read_text()).rowcount
        self.assertEqual(n, 0)
        self.assertEqual(self._cat(tid)["category_primary"], "INCOME")


if __name__ == "__main__":
    unittest.main()
