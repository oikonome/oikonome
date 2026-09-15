"""Money-accuracy regressions: retirement buckets and net worth exclude linked shadow accounts.

* Envelope drip: the walk starts from LIVE checking, which this
period's envelope spend has already left. Dripping the full plan rate
(monthly/30.4) for the whole window therefore double-counts: an envelope
fully spent by the 5th still reserved ~$82 more over the rest of the month.
The drip is now remaining-pool ÷ days-left-in-period, resetting at each
period roll inside the window.

* Early-paid bills: upcoming_bill_occurrences (shared by runway AND forecast): the
current month matched paid bills MTD only (no lookback before month start)
and future months reserved every occurrence unmatched — so a bill due Aug 1
autopaid Jul 28 was double-counted (checking already reduced AND reserved).

* Shadow 401k: retirement.current_buckets ignored linked-account shadows, so a
dual-source 401k counted once per source.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast, links
from oikonome.engine import retirement as R

from .util import TODAY, add_bill, add_txn, make_db, write_config

EOM = dt.date(2026, 7, 31)            # TODAY is 2026-07-15 → 16 days left


class EnvelopeDripTests(unittest.TestCase):
    """The drip charges what's LEFT of each pool, not the full plan rate."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        # zero the fixture card: no card debt, no payment events — the plan
        # walk is then envelope-drip only, exact to the cent
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")

    def tearDown(self):
        self.conn.close()

    def curve(self, days):
        f = forecast.build(self.conn, today=TODAY, days=days)
        return dict(f["plan"]["series"])

    def test_fully_spent_envelope_not_dripped_again(self):
        """THE CASE: $100/mo envelope fully spent by the 5th — the rest
        of the month must NOT reserve the pool a second time (pre-fix it
        burned ~$3.29/day ≈ $53 more by Jul 31)."""
        add_bill(self.conn, "Brewhouse", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, dt.date(2026, 7, 5), 100.0, "BREWHOUSE",
                primary="FOOD_AND_DRINK")
        self.assertAlmostEqual(self.curve(16)[str(EOM)], 5000.0, places=2)

    def test_partially_spent_envelope_drips_the_remainder(self):
        """$40 spent → exactly the $60 left drips out by month end."""
        add_bill(self.conn, "Brewhouse", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, dt.date(2026, 7, 5), 40.0, "BREWHOUSE",
                primary="FOOD_AND_DRINK")
        self.assertAlmostEqual(self.curve(16)[str(EOM)], 4940.0, places=2)

    def test_new_month_starts_a_fresh_pool(self):
        """Across the month roll the pool resets — August drips its full
        $100 (no spend exists there yet), July only its $60 remainder."""
        add_bill(self.conn, "Brewhouse", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, dt.date(2026, 7, 5), 40.0, "BREWHOUSE",
                primary="FOOD_AND_DRINK")
        c = self.curve(47)                       # TODAY+47 = Aug 31
        self.assertAlmostEqual(c[str(EOM)], 4940.0, places=2)
        self.assertAlmostEqual(c["2026-08-31"], 4840.0, places=2)

    def test_annual_envelope_drips_the_year_remainder(self):
        """An annual pool: $31 already drawn this year leaves $169 over
        the 169 days to Dec 31 — exactly $1/day, not pool/12/30.4."""
        add_bill(self.conn, "Hostwise", 200.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_txn(self.conn, dt.date(2026, 3, 10), 31.0, "HOSTWISE",
                primary="GENERAL_SERVICES")
        self.assertAlmostEqual(self.curve(16)[str(EOM)], 5000.0 - 16.0,
                               places=2)

    def test_exhausted_annual_envelope_stops_dripping(self):
        add_bill(self.conn, "Hostwise", 200.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_txn(self.conn, dt.date(2026, 3, 10), 200.0, "HOSTWISE",
                primary="GENERAL_SERVICES")
        self.assertAlmostEqual(self.curve(16)[str(EOM)], 5000.0, places=2)


class PrepaidBillTests(unittest.TestCase):
    """upcoming_bill_occurrences matches payments across month boundaries:
    lookback before month start, and future-month occurrences are matched
    before being reserved."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def upcoming(self, today, end):
        return budget.upcoming_bill_occurrences(
            self.conn, budget.load_config(self.conn), today, end)

    def test_next_month_bill_paid_early_is_not_reserved(self):
        """THE CASE: rent due the 1st, autopaid ~4 days early every month
        (Jun 28 and Jul 28 in the ledger). On Jul 29 NOTHING is owed — the
        money already left checking. Pre-fix both the July occurrence
        (overdue-unpaid, its Jun 28 payment invisible) and the August one
        (future month, never matched) were reserved: $4,000 phantom."""
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 6, 28), 2000.0, "Rent",
                primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 7, 28), 2000.0, "Rent",
                primary="RENT_AND_UTILITIES")
        got = self.upcoming(dt.date(2026, 7, 29), dt.date(2026, 8, 15))
        self.assertEqual(got, [])

    def test_current_month_bill_paid_before_month_start(self):
        """Due Jul 2, paid Jun 28 — the lookback finds the payment; pre-fix
        the occurrence went overdue-unpaid and was re-dated to tomorrow."""
        add_bill(self.conn, "Insurance Co", 300.0,
                 next_due=dt.date(2026, 7, 2), last_seen=dt.date(2026, 6, 2))
        add_txn(self.conn, dt.date(2026, 6, 28), 300.0, "Insurance Co",
                primary="GENERAL_SERVICES")
        self.assertEqual(self.upcoming(TODAY, TODAY + dt.timedelta(days=14)),
                         [])

    def test_unpaid_future_bill_is_still_reserved(self):
        """Matching future months must not stop reserving genuinely unpaid
        bills — July's payment covers July, August stays owed."""
        add_bill(self.conn, "Netflix", 20.0, next_due=dt.date(2026, 8, 5),
                 last_seen=dt.date(2026, 7, 5))
        add_txn(self.conn, dt.date(2026, 7, 5), 20.0, "Netflix",
                primary="ENTERTAINMENT")
        got = self.upcoming(dt.date(2026, 7, 29), dt.date(2026, 8, 15))
        self.assertEqual([(b["payee"], b["due"]) for b in got],
                         [("Netflix", dt.date(2026, 8, 5))])

    def test_late_payment_for_last_month_does_not_cover_this_month(self):
        """A Jul 1 payment of JUNE's bill (due Jun 28, paid late) attaches to
        June's occurrence — July 28 stays reserved."""
        add_bill(self.conn, "Fitness Club", 50.0,
                 next_due=dt.date(2026, 7, 28), last_seen=dt.date(2026, 6, 28))
        add_txn(self.conn, dt.date(2026, 7, 1), 50.0, "Fitness Club",
                primary="PERSONAL_CARE")
        got = self.upcoming(TODAY, dt.date(2026, 8, 10))
        self.assertEqual([(b["payee"], b["due"]) for b in got],
                         [("Fitness Club", dt.date(2026, 7, 28))])


class RetirementShadowTests(unittest.TestCase):
    """current_buckets excludes linked shadows, same as net worth."""

    def _mk_401k(self, item_id, aggregator, acct_id):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING",
            (item_id, aggregator, item_id))
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                     balance_current, updated_at)
               VALUES (%s,%s,'Work 401k','investment','401k','9876',
                       100000, now())""",
            (acct_id, item_id))

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_dual_source_401k_counts_once(self):
        """Same $100k 401k through Plaid + SimpleFIN, linked: td must be
        $100k, not $200k (pre-fix the sim retired you on double money)."""
        self._mk_401k("pl", "plaid", "pl-401k")
        self._mk_401k("sf", "simplefin-org", "sf-401k")
        links.create(self.conn, ["pl-401k", "sf-401k"])
        b = R.current_buckets(self.conn)
        self.assertEqual(b["td"], 100_000.0)
        # fixture checking/card untouched: cash 5000 − card 250
        self.assertEqual(b["cash"], 4750.0)

    def test_unlinked_accounts_all_count(self):
        """No links → nothing is a shadow; both sources still add up (the
        exclusion must not eat unlinked accounts)."""
        self._mk_401k("pl", "plaid", "pl-401k")
        self._mk_401k("sf", "simplefin-org", "sf-401k")
        b = R.current_buckets(self.conn)
        self.assertEqual(b["td"], 200_000.0)


if __name__ == "__main__":
    unittest.main()
