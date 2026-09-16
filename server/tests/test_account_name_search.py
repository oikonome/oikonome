"""An account is findable by either of its names, and a rename is undoable.

The ledger's generated search_text column cannot reference the accounts
table, so account-name matching is resolved once on the accounts table and
joined in by id — which is also what makes it exist at all: without it,
searching "Chase" or an account's own nickname returned nothing. Both names
matter: the user's rename is how they think of the account, and the bank's
own name ("CREDIT CARD") is what a statement quotes. The bank's name is
retained under any rename so the editor can show it, and clearing the
display name reverts to it.
"""

import unittest

from oikonome.web import data

from .util import add_txn, make_db


class AccountNameSearch(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # rows whose own text says nothing about the account they sit on,
        # so any match below is the account-name path, not search_text
        add_txn(self.conn, "2026-08-01", 12.00, "Coffee Shop", account="card")
        add_txn(self.conn, "2026-08-02", 30.00, "Hardware Store",
                account="card")
        add_txn(self.conn, "2026-08-03", 9.00, "Coffee Shop", account="chk")

    def tearDown(self):
        self.conn.close()

    def _search(self, term):
        rows, total, _sum, _az, _spend, hits = data.search_transactions(
            self.conn, term)
        return rows, total, hits

    def test_bank_name_finds_the_accounts_rows(self):
        rows, total, hits = self._search("test card")
        self.assertEqual(total, 2)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["bank_name"], "Test Card")
        # not renamed, so nothing to explain
        self.assertFalse(hits[0]["via_bank"])

    def test_display_name_and_bank_name_both_match_after_a_rename(self):
        data.set_account_display_name(self.conn, "card", "Groceries Card")
        # the new name finds the rows...
        _rows, total, hits = self._search("groceries card")
        self.assertEqual(total, 2)
        self.assertFalse(hits[0]["via_bank"])
        # ...and so does the bank's own, flagged so the page can explain a
        # result that shows neither word
        _rows, total, hits = self._search("test card")
        self.assertEqual(total, 2)
        self.assertTrue(hits[0]["via_bank"])
        self.assertEqual(hits[0]["bank_name"], "Test Card")

    def test_clearing_the_display_name_reverts_to_the_banks(self):
        data.set_account_display_name(self.conn, "card", "Groceries Card")
        data.set_account_display_name(self.conn, "card", "")
        row = next(r for r in data.accounts_detail(self.conn)
                   if r["id"] == "card")
        self.assertEqual(row["name"], "Test Card")
        self.assertEqual(row["bank_name"], "Test Card")
        _rows, total, _hits = self._search("groceries card")
        self.assertEqual(total, 0)

    def test_accounts_detail_carries_the_bank_name_under_a_rename(self):
        data.set_account_display_name(self.conn, "card", "Groceries Card")
        row = next(r for r in data.accounts_detail(self.conn)
                   if r["id"] == "card")
        self.assertEqual(row["name"], "Groceries Card")
        self.assertEqual(row["bank_name"], "Test Card")

    def test_a_hidden_accounts_name_matches_nothing(self):
        self.conn.execute(
            "UPDATE accounts SET user_removed_at = now() WHERE id = 'card'")
        _rows, total, hits = self._search("test card")
        self.assertEqual(total, 0)
        self.assertEqual(hits, [])

    def test_a_merchant_search_reports_no_account_hits(self):
        rows, total, hits = self._search("coffee shop")
        self.assertEqual(total, 2)
        self.assertEqual(hits, [])

    def test_the_name_clause_widens_never_replaces_the_text_match(self):
        # a term matching BOTH a merchant and an account must return the
        # union: the checking row by its text, the card rows by the name
        data.set_account_display_name(self.conn, "card", "Coffee Fund")
        rows, total, hits = self._search("coffee")
        self.assertEqual(total, 3)
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
