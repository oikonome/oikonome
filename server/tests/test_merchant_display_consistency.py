"""What a merchant is called is ONE fact, on every surface.

The merchant-canonical map keys on what a row DISPLAYS —
COALESCE(merchant_outlet, merchant_name, name) — so a user rename lands on
the name the user actually saw. Every surface that names a merchant must
resolve through that same key. Keyed differently, a rename shows the new
name on the Merchants page while the ledger and search still use the old
one, and the bulk "apply to all" write scope resolves a DIFFERENT canonical
than the page displays — the write then matches a different set of rows
than the user was looking at.

Also pinned here: the ledger rows' counts_as_spend flag must agree with the
verdict's own definition of spending (budget.SPEND_ONLY_SQL), row for row —
the client renders day subtotals from it, and a drifted flag would show a
different number than the verdict charges.
"""

import unittest

from oikonome.engine import budget, merchant_dedup
from oikonome.web import data

from .util import add_txn, make_db


def _set_outlet(conn, txn_id, outlet):
    """The fuel-arm splitter writes merchant_outlet at sync time; tests set
    it directly — the split itself is test_fuel_arm_merchant's subject."""
    conn.execute("UPDATE transactions SET merchant_outlet=%s WHERE id=%s",
                 (outlet, txn_id))


class RenamedMerchantOnTheLedger(unittest.TestCase):
    """A rename keyed on the display name (here: an outlet) must show on
    the Transactions page, the Today recent pane, and be findable by the
    NEW name in both search paths."""

    def setUp(self):
        self.conn = make_db()
        self.tid = add_txn(self.conn, "2026-07-10", 42.50, "NORTHWIND CLUB GAS #0555",
                           merchant="Northwind Club")
        _set_outlet(self.conn, self.tid, "Northwind Club Gas")
        # the user's rename, via the real feature — it keys the manual
        # canonical row on the displayed string, i.e. the outlet
        merchant_dedup.rename(self.conn, "Northwind Club Gas", "Fuel Stop")

    def tearDown(self):
        self.conn.close()

    def test_the_transactions_page_shows_the_new_name(self):
        rows = data.transactions(self.conn, 2026, 7)
        row = next(r for r in rows if r["id"] == self.tid)
        self.assertEqual(row["payee"], "Fuel Stop")

    def test_the_today_recent_pane_shows_the_new_name(self):
        rows = data.transactions_by_ids(self.conn, [self.tid])
        self.assertEqual(rows[0]["payee"], "Fuel Stop")

    def test_universal_search_finds_the_new_name(self):
        rows, total, _sum, _amz, _spend, _hits = data.search_transactions(
            self.conn, "fuel stop")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], self.tid)
        self.assertEqual(rows[0]["payee"], "Fuel Stop")

    def test_month_browse_search_finds_the_new_name(self):
        rows = data.transactions(self.conn, 2026, 7, search="fuel stop")
        self.assertEqual([r["id"] for r in rows], [self.tid])


class BulkWriteScopeMatchesThePage(unittest.TestCase):
    """The bulk-category target set groups rows exactly the way the page
    displays them. An outlet row ("Northwind Club Gas") renamed to "Fuel Stop"
    displays under the new name and under NOTHING else — so it must be in
    "Fuel Stop"'s write scope and out of "Northwind Club"'s. Keyed on
    merchant_name it would be the other way around."""

    def setUp(self):
        self.conn = make_db()
        self.pump = add_txn(self.conn, "2026-07-08", 60, "NORTHWIND CLUB GAS #0555",
                            merchant="Northwind Club")
        _set_outlet(self.conn, self.pump, "Northwind Club Gas")
        self.store = add_txn(self.conn, "2026-07-09", 120, "NORTHWIND CLUB WHSE #0555",
                             merchant="Northwind Club")
        merchant_dedup.rename(self.conn, "Northwind Club Gas", "Fuel Stop")

    def tearDown(self):
        self.conn.close()

    def _displayed_under(self, name):
        """The rows the page groups under `name` — the display layer's own
        join, straight from its single definition."""
        rows = self.conn.execute(
            f"""SELECT t.id FROM transactions t {merchant_dedup.MC_JOIN}
                WHERE t.removed = 0
                  AND {merchant_dedup.DISPLAY_MERCHANT} = %s""",
            (name,)).fetchall()
        return {r["id"] for r in rows}

    def test_the_renamed_outlet_is_in_its_own_write_scope(self):
        targets = {r["id"]
                   for r in data._merchant_targets(self.conn, "Fuel Stop")}
        self.assertEqual(targets, self._displayed_under("Fuel Stop"))
        self.assertEqual(targets, {self.pump})

    def test_the_sibling_brand_rows_stay_out_of_that_scope(self):
        targets = {r["id"]
                   for r in data._merchant_targets(self.conn, "Northwind Club")}
        self.assertEqual(targets, self._displayed_under("Northwind Club"))
        self.assertEqual(targets, {self.store})


class CountsAsSpendMatchesTheVerdict(unittest.TestCase):
    """counts_as_spend on ledger rows is the verdict's own spend rule:
    money out only, never a credit-card payment, never a transfer (the
    effective category, so a manual override excludes too), never a row on
    a loan account."""

    def setUp(self):
        self.conn = make_db()
        # a plain purchase — the one row that counts
        self.purchase = add_txn(self.conn, "2026-07-05", 25, "Grocer")
        # the card-payment leg the checking account already recorded
        self.card_pay = add_txn(
            self.conn, "2026-07-06", 500, "Card Payment",
            primary="LOAN_PAYMENTS",
            detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        # a transfer the user marked by hand — the override excludes it
        self.xfer = add_txn(self.conn, "2026-07-07", 80, "Venmo",
                            override="TRANSFER_OUT")
        # money in never counts
        self.deposit = add_txn(self.conn, "2026-07-08", -300, "Refund")
        # the loan-servicer side of a payment the checking account recorded
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('loan1','it1','Test Mortgage','loan','mortgage',100000) "
            "ON CONFLICT (tenant_id, id) DO NOTHING")
        self.loan_row = add_txn(self.conn, "2026-07-09", 900, "Mortgage Co",
                                account="loan1")

    def tearDown(self):
        self.conn.close()

    def _flags(self):
        return {r["id"]: r["counts_as_spend"]
                for r in data.transactions(self.conn, 2026, 7)}

    def test_each_exclusion_and_the_row_that_counts(self):
        flags = self._flags()
        self.assertIs(flags[self.purchase], True)
        self.assertIs(flags[self.card_pay], False)
        self.assertIs(flags[self.xfer], False)
        self.assertIs(flags[self.deposit], False)
        self.assertIs(flags[self.loan_row], False)

    def test_the_flag_agrees_with_spend_only_sql_row_for_row(self):
        # the server-side aggregate rule, applied to the same seeded set —
        # the flag and the verdict's SQL must partition it identically
        spend_ids = {r["id"] for r in self.conn.execute(
            f"SELECT t.id FROM transactions t WHERE t.removed = 0"
            f"{budget.SPEND_ONLY_SQL}").fetchall()}
        flags = self._flags()
        self.assertEqual({tid for tid, f in flags.items() if f}, spend_ids)

    def test_search_rows_carry_the_flag_too(self):
        rows, *_ = data.search_transactions(self.conn, "grocer")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["counts_as_spend"], True)
        rows, *_ = data.search_transactions(self.conn, "venmo")
        self.assertIs(rows[0]["counts_as_spend"], False)


if __name__ == "__main__":
    unittest.main()
