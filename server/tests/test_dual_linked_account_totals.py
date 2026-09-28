"""A real account reached through two sources must be counted ONCE.

Linking two sources (Plaid, then SimpleFIN when Plaid access lapsed) leaves
TWO account rows carrying ONE real transaction history. `links.shadow_ids()`
/ the `app.shadow_ids` session variable name the non-primary rows so money
math skips them; a query that sums or counts `FROM transactions` without
that exclusion reports double.

Pinned here, per surface:

* the transactions ledger — the month view listed each row twice, and the
  search's `total` / `amount_sum` / spend average (all rendered by the SPA
  and the mobile app as trusted figures) read double;
* the investment-fee report — a $25 advisory fee posted on both copies was
  summed twice into by_platform, by_year and the headline total;
* merchant identity, which deliberately does NOT exclude shadows: it writes
  a per-ROW property, never a figure, and a shadow row must carry a correct
  merchant_id because its source starts serving the ledger the moment the
  primary's aggregator fails.

Asking for a shadow account BY ID still shows its history — the exclusion
is about totals, not about hiding data the user owns.
"""

import unittest

from oikonome.engine import links, merchant_identity, reporting
from oikonome.web import data

from .util import add_txn, make_db, write_config


def _mk_source(conn, item_id, aggregator, acct_id, *, mask="4321",
               typ="credit", subtype="credit card", bal=100.0):
    """One aggregator's view of one real-world account."""
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING",
        (item_id, aggregator, item_id))
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                 balance_current, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,now())""",
        (acct_id, item_id, acct_id, typ, subtype, mask, bal))


class LedgerCountsADualLinkedAccountOnce(unittest.TestCase):
    """web/data.py's ledger and universal search."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _mk_source(self.conn, "pl-1", "plaid", "pl-card")
        _mk_source(self.conn, "sf-1", "simplefin-org", "sf-card")
        # the SAME real purchase, arriving once per source
        add_txn(self.conn, "2026-07-14", 50.0, "DUALSPEND", account="pl-card",
                txn_id="dl-plaid")
        add_txn(self.conn, "2026-07-14", 50.0, "DUALSPEND", account="sf-card",
                txn_id="dl-simplefin")

    def tearDown(self):
        self.conn.close()

    def _link(self):
        links.create(self.conn, ["pl-card", "sf-card"])
        self.assertEqual(links.shadow_ids(self.conn), ["sf-card"])

    def test_month_view_lists_the_row_once_per_real_purchase(self):
        before = [r for r in data.transactions(self.conn, 2026, 7)
                  if r["payee"] == "DUALSPEND"]
        self.assertEqual(len(before), 2, "fixture must start doubled")
        self._link()
        after = [r for r in data.transactions(self.conn, 2026, 7)
                 if r["payee"] == "DUALSPEND"]
        self.assertEqual([r["id"] for r in after], ["dl-plaid"],
                         "the ledger listed the shadow source's copy too")

    def test_search_totals_do_not_move_when_a_shadow_copy_exists(self):
        self._link()
        rows, total, amount_sum, _amz, spend, _hits = data.search_transactions(
            self.conn, "DUALSPEND")
        self.assertEqual(total, 1)
        self.assertEqual(amount_sum, 50.0,
                         "amount_sum counted the same $50 purchase twice")
        self.assertEqual(spend["count"], 1)
        self.assertEqual(spend["sum"], 50.0,
                         "the 'avg of N' figure is computed off a doubled sum")
        self.assertEqual([r["id"] for r in rows], ["dl-plaid"])

    def test_totals_are_unchanged_by_the_link_itself(self):
        """The number a household sees must not move when they link two
        sources they already had — that is the whole promise of linking."""
        _rows, total, amount_sum, _amz, spend, _hits = data.search_transactions(
            self.conn, "DUALSPEND", account_id="pl-card")
        self._link()
        _rows2, total2, amount_sum2, _amz2, spend2, _hits = data.search_transactions(
            self.conn, "DUALSPEND")
        self.assertEqual((total, amount_sum, spend["sum"]),
                         (total2, amount_sum2, spend2["sum"]))

    def test_asking_for_the_shadow_account_by_id_still_shows_its_history(self):
        self._link()
        direct = data.transactions(self.conn, 2026, 7, account_id="sf-card")
        self.assertEqual([r["id"] for r in direct], ["dl-simplefin"])
        rows, total, _sum, _amz, _spend, _hits = data.search_transactions(
            self.conn, "DUALSPEND", account_id="sf-card")
        self.assertEqual(total, 1)
        self.assertEqual([r["id"] for r in rows], ["dl-simplefin"])

    def test_a_hidden_account_is_still_excluded(self):
        """The shadow predicate covers hidden accounts too; the older
        user_removed_at filter must not have been traded away for it."""
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=now() WHERE id='sf-card'")
        payees = [r["id"] for r in data.transactions(self.conn, 2026, 7)]
        self.assertNotIn("dl-simplefin", payees)


class InvestmentFeesCountADualLinkedFeeOnce(unittest.TestCase):
    """engine/reporting.py compute_fees()."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _mk_source(self.conn, "pl-inv", "plaid", "pl-brokerage",
                   mask="9876", typ="investment", subtype="brokerage",
                   bal=10000.0)
        _mk_source(self.conn, "sf-inv", "simplefin-org", "sf-brokerage",
                   mask="9876", typ="investment", subtype="brokerage",
                   bal=10000.0)
        for aid, tid in (("pl-brokerage", "fee-plaid"),
                         ("sf-brokerage", "fee-simplefin")):
            add_txn(self.conn, "2026-03-31", 25.0, "ADVISORY FEE",
                    account=aid, txn_id=tid)

    def tearDown(self):
        self.conn.close()

    def test_the_fee_drag_reports_what_one_platform_charged(self):
        doubled = reporting.compute_fees(self.conn)
        self.assertEqual(doubled["total"], 50.0, "fixture must start doubled")
        links.create(self.conn, ["pl-brokerage", "sf-brokerage"])
        r = reporting.compute_fees(self.conn)
        self.assertEqual(r["total"], 25.0,
                         "one $25 fee, charged once, reported twice")
        self.assertEqual(dict(r["by_year"]), {"2026": 25.0})
        self.assertEqual([[p[0], p[1], p[2]] for p in r["by_platform"]],
                         [["pl-inv", 25.0, 1]])

    def test_a_hidden_investment_account_drops_out_too(self):
        self.conn.execute("UPDATE accounts SET user_removed_at=now() "
                          "WHERE id='sf-brokerage'")
        self.assertEqual(reporting.compute_fees(self.conn)["total"], 25.0)


class MerchantIdentitySpansShadowAccountsOnPurpose(unittest.TestCase):
    """The deliberate non-exclusion.

    merchant_identity writes `transactions.merchant_id` — a property of a
    row, not a figure. A linked non-primary's rows are shown the instant its
    source becomes the effective primary, with no further resolve pass, so
    leaving them unresolved would blank the merchant name and logo exactly
    when they start being read. The liveness tests in prune_empty() and
    _retire_if_empty() count shadow rows for the same reason: a shadow row
    is a real pointer at the merchant.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _mk_source(self.conn, "pl-2", "plaid", "pl-chk", typ="depository",
                   subtype="checking")
        _mk_source(self.conn, "sf-2", "simplefin-org", "sf-chk",
                   typ="depository", subtype="checking")
        add_txn(self.conn, "2026-07-14", 12.0, "BLUE BOTTLE COFFEE",
                account="pl-chk", txn_id="mi-plaid")
        add_txn(self.conn, "2026-07-14", 12.0, "BLUE BOTTLE COFFEE",
                account="sf-chk", txn_id="mi-simplefin")
        links.create(self.conn, ["pl-chk", "sf-chk"])

    def tearDown(self):
        self.conn.close()

    def _mid(self, txn_id):
        return self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["merchant_id"]

    def test_the_shadow_rows_get_a_merchant_too(self):
        merchant_identity.resolve(self.conn)
        self.assertIsNotNone(self._mid("mi-plaid"))
        self.assertEqual(self._mid("mi-simplefin"), self._mid("mi-plaid"),
                         "a shadow row must resolve to the same merchant — "
                         "its source serves the ledger after a failover")

    def test_pruning_keeps_a_merchant_only_shadow_rows_point_at(self):
        merchant_identity.resolve(self.conn)
        mid = self._mid("mi-plaid")
        # retire the primary's copy; only the shadow's row still names it
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          ("mi-plaid",))
        merchant_identity.prune_empty(self.conn)
        alive = self.conn.execute("SELECT count(*) AS n FROM merchants "
                                  "WHERE id=%s", (mid,)).fetchone()["n"]
        self.assertEqual(alive, 1,
                         "pruned a merchant a live shadow row still points at")
        self.assertEqual(self._mid("mi-simplefin"), mid)

    def test_stats_measures_resolver_coverage_over_the_whole_ledger(self):
        merchant_identity.resolve(self.conn)
        st = merchant_identity.stats(self.conn)
        self.assertEqual(st["rows"], 2)
        self.assertEqual(st["resolved"], 2,
                         "coverage is 'did the resolver reach every row it "
                         "must write', shadow rows included")


if __name__ == "__main__":
    unittest.main()
