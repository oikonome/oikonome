"""Closed months whose frozen schedules differ only in a rolled due day
share one pass over the year's annual-envelope claims.

The nightly roll moves every recurring bill's due day forward a cycle, so
each month's snapshot freezes a different `dueOn` for the same bill. The
year's claims depend on the occurrence DATES that schedule expands to,
and a bill rolled one cycle on its cadence expands to the same dates — so
the Year page must not re-match the whole year once per budgeted month.
A schedule whose rows really differ (an amount edited mid-year) still
gets a pass of its own: the answer may differ, and a merged key would
hand one month another schedule's claims.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.web import lenses

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 7)


def _year_passes(fn):
    """How many times the whole-year claim pass matched occurrences."""
    calls = [0]
    real = budget.match_occurrences

    def counted(rows, occs):
        dues = [o["due"] for o in occs]
        if dues and min(dues) < dt.date(2026, 1, 1) <= dt.date(2027, 1, 1) <= max(dues):
            calls[0] += 1
        return real(rows, occs)

    with mock.patch.object(budget, "match_occurrences", counted):
        out = fn()
    return calls[0], out


class RolledScheduleShareTheYearPass(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=700, other_monthly=800)
        add_bill(self.conn, "Harbor Lofts", 1613.0,
                 next_due=dt.date(2026, 1, 30),
                 last_seen=dt.date(2025, 12, 30))
        add_bill(self.conn, "Namehost", 120.0, bill_type="envelope",
                 frequency=None, interval=12)
        for m in range(1, 11):
            add_txn(self.conn, dt.date(2026, m, 3), 30.0 + m, "CORNER GOODS")
            add_txn(self.conn, dt.date(2026, m, 28), 1613.0, "HARBOR LOFTS",
                    account="chk", primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 2, 14), 23.0, "NAMEHOST RENEWAL")
        add_txn(self.conn, dt.date(2026, 6, 14), 27.0, "NAMEHOST RENEWAL")
        # each month frozen with the due day the nightly roll had reached
        for m in range(1, 11):
            due = dt.date(2026, m, 30) if m != 2 else dt.date(2026, 2, 28)
            self.conn.execute(
                "UPDATE bills SET raw = jsonb_set(raw, '{dueOn}', to_jsonb(%s::text)) "
                "WHERE payee = 'Harbor Lofts'", (due.isoformat(),))
            budget.snapshot_month(self.conn, dt.date(2026, m, 20))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_a_rolled_due_day_does_not_cost_another_pass(self):
        passes, _ = _year_passes(
            lambda: lenses.year_summary(self.conn, 2026, TODAY))
        self.assertEqual(passes, 1)

    def test_the_shared_pass_answers_what_each_month_alone_would(self):
        """Each frozen schedule's claims, computed with nothing shared, are
        the claims the shared pass hands every month."""
        envs = budget._envelope_bills(self.conn)
        annual = [e for e in envs if e["period_months"] > 1]
        answers = set()
        for m in (1, 5, 10):
            rows = budget.snapshot_bill_rows(self.conn, 2026, m)
            bl = budget._recurring_bills(self.conn, rows=rows)
            answers.add(tuple(budget._envelope_year_claims(
                self.conn, envs, annual, 2026, [], memo={}, bills=bl)))
        self.assertEqual(len(answers), 1)

    def test_a_schedule_that_really_changed_gets_its_own_pass(self):
        envs = budget._envelope_bills(self.conn)
        annual = [e for e in envs if e["period_months"] > 1]
        memo: dict = {}
        rows = budget.snapshot_bill_rows(self.conn, 2026, 5)
        edited = [dict(r, amount=-1700.0) if r["payee"] == "Harbor Lofts"
                  else r for r in rows]

        def both():
            for sched in (rows, edited):
                budget._envelope_year_claims(
                    self.conn, envs, annual, 2026, [], memo=memo,
                    bills=budget._recurring_bills(self.conn, rows=sched))

        passes, _ = _year_passes(both)
        self.assertEqual(passes, 2)


if __name__ == "__main__":
    unittest.main()
