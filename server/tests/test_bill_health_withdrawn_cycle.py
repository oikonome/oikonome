"""A bill's health when the cycle it missed was charged and then withdrawn.

The Bills page's health verdict judges a bill on its LIVE charges, so a
bill whose last charge the bank feed took back (a posted row later
removed) looked like a bill gone quiet: red "stale" and a nag to review
it. When a withdrawn posted charge would have paid the missed cycle, the
bill WAS charged — the verdict is "withdrawn" with that charge's date,
there is no suggestion, and the nightly pass files no removal proposal on
its strength. It covers one cycle only: when the next cycle is missed as
well, the bill reads stale as before. A bill with no charge at all keeps
the stale verdict and its review suggestion.
"""

import datetime as dt
import unittest

from unittest import mock

from oikonome.engine import bills
from oikonome.sync import base

from .util import TODAY, add_bill, add_txn, make_db, write_config

PAYEE = "City Power"


def _withdraw(conn, txn_id):
    """The bank feed's removal delta, as the Plaid sync applies it."""
    base.mark_removed(conn, [txn_id])


class WithdrawnCycleHealthTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # live charges through May; the June charge is the cycle in question
        for d in (dt.date(2026, 3, 20), dt.date(2026, 4, 20),
                  dt.date(2026, 5, 20)):
            add_txn(self.conn, d, 100.0, "CITY POWER")
        add_bill(self.conn, PAYEE, 100.0, next_due=dt.date(2026, 7, 20),
                 last_seen=dt.date(2026, 5, 20))

    def tearDown(self):
        self.conn.close()

    def _health(self, today=TODAY):
        return bills.analyze_bills(self.conn, today)[PAYEE]

    def test_a_withdrawn_charge_on_the_missed_cycle_reads_withdrawn(self):
        _withdraw(self.conn, add_txn(self.conn, dt.date(2026, 6, 20), 100.0,
                                     "CITY POWER"))
        h = self._health()
        self.assertEqual(h["health"], "withdrawn")
        self.assertEqual(h["withdrawn_on"], dt.date(2026, 6, 20))
        # last_match stays the last LIVE charge
        self.assertEqual(h["last_match"], dt.date(2026, 5, 20))
        self.assertIsNone(h["suggestion"])

    def test_a_live_charge_on_the_cycle_reads_ok(self):
        add_txn(self.conn, dt.date(2026, 6, 20), 100.0, "CITY POWER")
        h = self._health()
        self.assertEqual(h["health"], "ok")
        self.assertIsNone(h["withdrawn_on"])

    def test_a_cycle_with_no_charge_at_all_still_reads_stale(self):
        h = self._health()
        self.assertEqual(h["health"], "stale")
        self.assertEqual(h["suggestion"],
                         {"action": "review", "reason": "missed_one_cycle"})
        self.assertIsNone(h["withdrawn_on"])

    def test_a_withdrawn_charge_off_the_bills_amount_does_not_count(self):
        _withdraw(self.conn, add_txn(self.conn, dt.date(2026, 6, 20), 400.0,
                                     "CITY POWER"))
        self.assertEqual(self._health()["health"], "stale")

    def test_the_next_cycle_missed_too_reads_stale_again(self):
        # June withdrawn, July withdrawn as well: one withdrawn cycle is
        # explained, two in a row is a gap worth a look
        for d in (dt.date(2026, 6, 20), dt.date(2026, 7, 20)):
            _withdraw(self.conn, add_txn(self.conn, d, 100.0, "CITY POWER"))
        h = self._health(dt.date(2026, 7, 28))
        self.assertEqual(h["health"], "stale")
        self.assertIsNotNone(h["suggestion"])

    def _removals(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM bill_proposals "
            "WHERE kind='remove' AND payee=%s", (PAYEE,)).fetchone()["n"]

    def test_no_removal_proposal_on_the_strength_of_a_withdrawn_cycle(self):
        _withdraw(self.conn, add_txn(self.conn, dt.date(2026, 6, 20), 100.0,
                                     "CITY POWER"))
        # two cycles past the last LIVE charge — silence since then would
        # propose removal, but the merchant did charge in June
        bills.run(self.conn, dt.date(2026, 7, 28))
        self.assertEqual(self._removals(), 0)

    def test_a_merchant_silent_two_cycles_is_still_proposed_for_removal(self):
        bills.run(self.conn, dt.date(2026, 7, 28))
        self.assertEqual(self._removals(), 1)

    def test_the_single_bill_verdict_reads_withdrawn_too(self):
        # the history page and the dismiss route ask for one bill
        _withdraw(self.conn, add_txn(self.conn, dt.date(2026, 6, 20), 100.0,
                                     "CITY POWER"))
        add_bill(self.conn, "Lakeside Water", 61.0,
                 next_due=dt.date(2026, 7, 5))
        one = bills.analyze_bills(self.conn, TODAY, payee=PAYEE)
        self.assertEqual(list(one), [PAYEE])
        self.assertEqual(one[PAYEE]["health"], "withdrawn")
        self.assertEqual(one[PAYEE]["withdrawn_on"], dt.date(2026, 6, 20))

    def test_the_nightly_pass_loads_the_evidence_once_and_only_when_asked(self):
        _withdraw(self.conn, add_txn(self.conn, dt.date(2026, 6, 20), 100.0,
                                     "CITY POWER"))
        # a second bill that has never been charged at its amount: it has
        # no last match to measure silence from, so it never asks
        add_bill(self.conn, "Lakeside Water", 61.0,
                 next_due=dt.date(2026, 7, 5))
        add_txn(self.conn, dt.date(2026, 3, 5), 400.0, "LAKESIDE WATER")
        with mock.patch.object(bills, "_withdrawn_rows",
                               wraps=bills._withdrawn_rows) as loads:
            bills.run(self.conn, dt.date(2026, 7, 28))
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(self._removals(), 0)

    def test_a_schedule_with_nothing_gone_quiet_never_loads_the_evidence(self):
        add_txn(self.conn, dt.date(2026, 6, 20), 100.0, "CITY POWER")
        with mock.patch.object(bills, "_withdrawn_rows",
                               wraps=bills._withdrawn_rows) as loads:
            bills.run(self.conn, dt.date(2026, 7, 2))
        self.assertEqual(loads.call_count, 0)

    def test_a_merge_retired_row_is_no_evidence(self):
        # retired by its own UPDATE, as a merge or restate does — not the
        # feed's removal delta, so the bill reads stale
        gone = add_txn(self.conn, dt.date(2026, 6, 20), 100.0, "CITY POWER")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          (gone,))
        self.assertEqual(self._health()["health"], "stale")


if __name__ == "__main__":
    unittest.main()
