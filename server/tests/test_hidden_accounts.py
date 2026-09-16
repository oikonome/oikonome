"""Hiding one account off a shared connection excludes it from EVERYTHING.

The case: a Plaid login covers several accounts and one of them isn't
wanted. Disconnecting is too blunt (it takes the whole
institution), purging destroys history, and "exclude from budget" still
shows the account and counts its balance in net worth.

`user_removed_at` dropping the account from the Accounts list and nulling
its balance is not enough on its own: its TRANSACTIONS would still move the
budget, the cash-flow chart and the ledger, and every sync would keep
writing new ones — so "hide" would not mean what anyone clicking it
assumes.

The contract pinned here:

* hidden accounts join `links.shadow_ids`, which is the one list every money
  aggregate already consults — so the exclusion reaches spend, net worth,
  bills, budgets, savings and retirement without each of them growing its
  own filter;
* `upsert_transactions` refuses to store rows for a hidden account (the
  aggregator fetches per ITEM, so the rows still arrive — we drop them);
* the ledger hides them, EXCEPT when the account is asked for by id, which
  is how the history stays reachable;
* unhiding puts it all back.
"""

import unittest

from oikonome.engine import links
from oikonome.sync import base as sync_base

from .util import add_txn, make_db, seed_accounts, write_config


class HiddenAccountTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _hide(self, account_id):
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=now(), balance_current=NULL, "
            "balance_available=NULL WHERE id=%s", (account_id,))

    def test_hidden_account_joins_the_ignore_list(self):
        self.assertNotIn("card", links.shadow_ids(self.conn))
        self._hide("card")
        self.assertIn("card", links.shadow_ids(self.conn),
                      "hidden accounts must join the list every money "
                      "aggregate already consults, or hiding is cosmetic")

    def test_unhide_restores_it(self):
        self._hide("card")
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=NULL WHERE id='card'")
        self.assertNotIn("card", links.shadow_ids(self.conn))

    def test_sync_stops_storing_transactions_for_a_hidden_account(self):
        """Aggregators fetch per item, so the rows keep arriving — the point
        is that we stop writing them."""
        self._hide("card")
        txn = sync_base.Transaction(
            id="new-1", account_id="card", date="2026-07-20", amount=12.0,
            name="COFFEE", merchant_name="COFFEE", category_primary="FOOD",
            category_detailed=None, category_plaid=None,
            category_plaid_detailed=None, category_plaid_confidence=None,
            pending=0, raw={})
        sync_base.upsert_transactions(self.conn, [txn])
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id='new-1'"
        ).fetchone()["n"]
        self.assertEqual(n, 0, "a hidden account kept pulling data")

        # ...and resumes on unhide
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=NULL WHERE id='card'")
        sync_base.upsert_transactions(self.conn, [txn])
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id='new-1'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_ledger_hides_the_rows_but_keeps_them_reachable(self):
        from oikonome.web import data
        add_txn(self.conn, "2026-07-14", 40.0, "HIDDEN SPEND", account="card")
        add_txn(self.conn, "2026-07-14", 25.0, "VISIBLE SPEND", account="chk")
        self._hide("card")
        payees = {r["payee"] for r in data.transactions(self.conn, 2026, 7)}
        self.assertIn("VISIBLE SPEND", payees)
        self.assertNotIn("HIDDEN SPEND", payees,
                         "hidden account's spend still shows in the ledger")
        # asking for that account directly still works — history is kept,
        # not destroyed, and the user must be able to look at it
        direct = data.transactions(self.conn, 2026, 7, account_id="card")
        self.assertEqual({r["payee"] for r in direct}, {"HIDDEN SPEND"})
        # and the search/paged ledger agrees with the month view
        rows, _, _, _, _, _hits = data.search_transactions(self.conn, "SPEND")
        self.assertEqual({r["payee"] for r in rows}, {"VISIBLE SPEND"})


class HiddenAccountSqlScopeTests(unittest.TestCase):
    """Both halves of "ignore these accounts" must agree.

    It has TWO implementations: the Python helper `links.shadow_ids()` and
    the SQL session variable `app.shadow_ids`. Teach only the Python one and
    a hidden account vanishes from Today and the budget while its
    transactions still count on Reports → Spending, in the forecast runway
    and in the emailed report — the surfaces whose whole job is "every
    total". Both read one SQL constant.

    These tests go through a real tenant_connect so the GUC is actually set;
    calling the Python helper alone would pass with the SQL half broken.
    """

    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        write_config(self.conn)
        add_txn(self.conn, "2026-07-14", 40.0, "HIDDEN SPEND", account="card")
        add_txn(self.conn, "2026-07-14", 25.0, "VISIBLE SPEND", account="chk")

    def tearDown(self):
        self.conn.close()

    def _hide_and_rescope(self, account_id):
        from oikonome.engine import links
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=now(), balance_current=NULL "
            "WHERE id=%s", (account_id,))
        links.set_shadow_scope(self.conn)     # what tenant_connect does

    def test_sql_session_var_agrees_with_the_python_helper(self):
        from oikonome.engine import links
        self._hide_and_rescope("card")
        guc = self.conn.execute(
            "SELECT current_setting('app.shadow_ids', true) AS v"
        ).fetchone()["v"] or ""
        self.assertIn("card", guc.split(","),
                      "app.shadow_ids does not know about hidden accounts — "
                      "Reports, forecast and the emailed runway all read it")
        self.assertEqual(set(links.shadow_ids(self.conn)), set(guc.split(",")),
                         "the SQL and Python definitions of 'ignore these' "
                         "have drifted again")

    def test_reports_spending_excludes_a_hidden_account(self):
        from oikonome.engine import reporting
        before = self.conn.execute(
            f"SELECT COALESCE(SUM(t.amount),0) AS s FROM transactions t "
            f"WHERE {reporting.SPEND_WHERE}").fetchone()["s"]
        self.assertEqual(float(before), 65.0)      # both accounts counted
        self._hide_and_rescope("card")
        after = self.conn.execute(
            f"SELECT COALESCE(SUM(t.amount),0) AS s FROM transactions t "
            f"WHERE {reporting.SPEND_WHERE}").fetchone()["s"]
        self.assertEqual(float(after), 25.0,
                         "hidden account's spend still counts on Reports")


if __name__ == "__main__":
    unittest.main()
