"""Money moving between the owner's own accounts is a transfer.

A withdrawal from the household's own brokerage into its own checking
arrives as a bare ACH descriptor, and the aggregator stamps it
INCOME / INCOME_CONTRACTOR. The FLOW-GUARD then bars seed/LLM from ever
revisiting a flow row, so a wrong INCOME is permanent and has to be
corrected by hand on every occurrence.

The signal is structural: the aggregator names a `financial_institution`
counterparty, and the instance already knows which institutions the owner
banks with. These tests pin both halves — that it fires on the owner's own
investment institution, and that it stays off everything else.
"""

import unittest

from oikonome.engine import llm_categorize
from oikonome.engine.compat import as_date, jsonb

from .util import make_db


def counterparty(name, kind="financial_institution"):
    return {"counterparties": [{"name": name, "type": kind,
                                "confidence_level": "VERY_HIGH"}]}


class OwnTransferTests(unittest.TestCase):
    def setUp(self):
        # make_db already seeds item 'it1' ("Test Bank") with a depository
        # 'chk' and a credit 'card' — that IS the depository-only institution
        # this pass must stay off, so it is reused rather than rebuilt.
        # Only the investment side needs adding.
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status) "
            "VALUES ('i_inv','plaid','Northwind Brokerage','ok')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) "
            "VALUES ('inv','i_inv','Northwind Autoportfolio',"
            "'investment','brokerage')")

    def tearDown(self):
        self.conn.close()

    def _txn(self, tid, amount, name, raw, *, primary="INCOME",
             account="chk", override=None):
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, category_override,
                   pending, removed, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s)""",
            (tid, account, as_date("2026-07-30"), amount, name, name,
             primary, override, jsonb(raw)))
        return tid

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_primary"]

    # ---- the defect ----------------------------------------------------

    def test_withdrawal_from_own_brokerage_is_a_transfer_not_income(self):
        self._txn("w1", -2000, "NORTHWIND BRKRG ACH-XFER-0000",
                  counterparty("Northwind Brokerage"))
        self.assertEqual(1, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual("TRANSFER_IN", self._cat("w1"))

    def test_direction_follows_the_sign(self):
        """The SAME descriptor appears on money going out and money coming
        back. One rule, direction by sign."""
        self._txn("out", 5000, "NORTHWIND BRKRG ACH-XFER-0000",
                  counterparty("Northwind Brokerage"),
                  primary="GENERAL_MERCHANDISE")
        self._txn("in", -5000, "NORTHWIND BRKRG ACH-XFER-0000",
                  counterparty("Northwind Brokerage"))
        llm_categorize.own_transfer_classify(self.conn)
        self.assertEqual("TRANSFER_OUT", self._cat("out"))
        self.assertEqual("TRANSFER_IN", self._cat("in"))

    def test_institution_naming_differences_still_match(self):
        """An item may say "Northwind Brokerage" where the aggregator says
        "Northwind Brokerage Inc." Compare on letters and digits with corporate
        suffixes dropped."""
        self.conn.execute(
            "UPDATE items SET institution_name='Northwind Brokerage Inc.' "
            "WHERE id='i_inv'")
        self._txn("w1", -2000, "NB", counterparty("Northwind Brokerage"))
        llm_categorize.own_transfer_classify(self.conn)
        self.assertEqual("TRANSFER_IN", self._cat("w1"))

    # ---- what it must NOT touch ----------------------------------------

    def test_user_override_is_sacred(self):
        self._txn("w1", -2000, "NORTHWIND BRKRG ACH-XFER-0000",
                  counterparty("Northwind Brokerage"), override="INCOME")
        llm_categorize.own_transfer_classify(self.conn)
        self.assertEqual(
            "INCOME", self.conn.execute(
                "SELECT category_override FROM transactions WHERE id='w1'"
            ).fetchone()["category_override"])

    def test_card_payment_to_an_own_institution_stays_a_card_payment(self):
        """Counterparty is the owner's OWN investment institution, so the
        only thing keeping this row put is the LOAN_PAYMENTS guard."""
        self._txn("p1", 400, "NORTHWIND BRKRG ACH-AUTOPAY-0000",
                  counterparty("Northwind Brokerage"),
                  primary="LOAN_PAYMENTS")
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual("LOAN_PAYMENTS", self._cat("p1"))

    def test_depository_only_institution_does_not_match(self):
        """A bonus from your own BANK is real income — the fixture's "Test
        Bank" holds depository + credit but no investment account, so it must
        not be swept into a transfer. A genuine account credit belongs in
        INCOME, and this guard is what keeps it there."""
        self._txn("b1", -100, "Test Bank Bonus", counterparty("Test Bank"))
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual("INCOME", self._cat("b1"))

    def test_unrelated_institution_does_not_match(self):
        """Fabrikam Securities is a financial institution, but not one of
        the owner's."""
        self._txn("f1", -900, "FABRIKAM SECURITIES",
                  counterparty("Fabrikam Securities"))
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual("INCOME", self._cat("f1"))

    def test_non_institution_counterparty_does_not_match(self):
        """A merchant that happens to share a name is not an institution."""
        self._txn("m1", -60, "NORTHWIND GIFT SHOP",
                  counterparty("Northwind Brokerage", kind="merchant"))
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual("INCOME", self._cat("m1"))

    def test_no_counterparties_does_not_match(self):
        self._txn("n1", -2000, "NORTHWIND BRKRG ACH-XFER-0000", {})
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual("INCOME", self._cat("n1"))

    def test_archived_item_does_not_make_an_institution_yours(self):
        self.conn.execute(
            "UPDATE items SET status='archived' WHERE id='i_inv'")
        self._txn("w1", -2000, "NORTHWIND BRKRG ACH-XFER-0000",
                  counterparty("Northwind Brokerage"))
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))

    def test_only_the_bank_side_is_rewritten(self):
        """The investment account's own internal rows are the importer's
        business, not this pass's."""
        self._txn("i1", -120, "NORTHWIND BRKRG ACH-SELL-0000",
                  counterparty("Northwind Brokerage"), account="inv")
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))

    def test_idempotent(self):
        self._txn("w1", -2000, "NORTHWIND BRKRG ACH-XFER-0000",
                  counterparty("Northwind Brokerage"))
        self.assertEqual(1, llm_categorize.own_transfer_classify(self.conn))
        self.assertEqual(0, llm_categorize.own_transfer_classify(self.conn))

    def test_runs_inside_the_sync_pass(self):
        self._txn("w1", -2000, "NORTHWIND BRKRG ACH-XFER-0000",
                  counterparty("Northwind Brokerage"))
        stats = llm_categorize.categorize_new(self.conn)
        self.assertEqual(1, stats["own_transfers"])
        self.assertEqual("TRANSFER_IN", self._cat("w1"))


if __name__ == "__main__":
    unittest.main()
