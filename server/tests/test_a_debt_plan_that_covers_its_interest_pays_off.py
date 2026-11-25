"""A payoff plan whose payments cover the interest pays the debts off
(specs/invariants-and-fuzzing.md, M16; `engine.debt.simulate` and
`engine.debt.build`).

Property, for generated debts (balances, rates from 0 to past any real
card, minimums from a cent above the first month's interest up), under
either method, with or without extra money, and the minimums-only
baseline:

* the total owed never rises from one month to the next;
* every debt is paid off inside the walk's limit when its minimum would
  clear it there, and the month count is then a whole number; a walk cut
  off at the limit says so (no month count, no debt-free date) instead;
* the money paid is the money owed plus the interest less what is left,
  per debt and in total, to within a cent a month of rounding;
* no figure is NaN or infinite.

And for generated households (tests/money_households.py; their cards and
loans carry no issuer rate, so every rate is the page's estimate, per
specs/honest-figures.md) the page's plan says the same: what it owes is
the debts it lists, and a plan that finishes paid what it owed plus its
interest.
"""

from __future__ import annotations

import datetime as dt
import math
import unittest

from hypothesis import given
from hypothesis import strategies as st

from oikonome.engine import debt

from . import props
from .money_households import Household
from .util import TODAY

START = dt.date(2026, 7, 15)


@st.composite
def covered_debts(draw):
    """Debts whose minimum covers the first month's interest by at least a
    cent, so a balance can only fall."""
    out = []
    for i in range(draw(st.integers(1, 5))):
        bal = draw(st.integers(1, 5_000_000)) / 100
        apr = draw(st.one_of(st.just(0.0), st.integers(0, 4500).map(lambda b: b / 100)))
        interest = bal * apr / 1200
        floor = max(0.01, math.ceil(interest * 100 + 1) / 100)
        minimum = draw(st.integers(int(floor * 100), int(floor * 100) + 200_000)) / 100
        out.append({"id": f"d{i}", "name": f"Debt {i}", "balance": bal,
                    "apr": apr, "min_payment": minimum,
                    "due_now": draw(st.booleans()) and draw(st.booleans())})
    return out


def _amortizes(d) -> bool:
    """The minimum clears the balance inside the walk's limit even at its
    first month's interest."""
    return d["min_payment"] >= (d["balance"] * d["apr"] / 1200
                                + d["balance"] / (debt.MAX_MONTHS - 2) + 0.01)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


class APlanCoveringItsInterestPaysOff(unittest.TestCase):

    def _holds(self, debts, plan):
        series = [v for _, v in plan["series"]]
        self.assertTrue(all(_finite(v) for v in series), series)
        for a, b in zip(series, series[1:]):
            self.assertLessEqual(b, a + 0.005, series)
        for k in ("total_interest", "total_paid", "monthly", "starting"):
            self.assertTrue(_finite(plan[k]), (k, plan[k]))
        self.assertFalse(plan["growing"])
        start = {d["id"]: d["balance"] for d in debts}
        if all(_amortizes(d) for d in debts):
            self.assertTrue(plan["finishes"], plan["series"][-1])
        if not plan["finishes"]:
            # a walk cut off at its limit says so, and claims no month count
            self.assertIsNone(plan["months"])
            self.assertIsNone(plan["debt_free"])
            self.assertEqual(len(series), debt.MAX_MONTHS + 1)
            months = debt.MAX_MONTHS
        else:
            self.assertIsInstance(plan["months"], int)
            self.assertLessEqual(plan["months"], debt.MAX_MONTHS)
            months = plan["months"]
        slack = 0.01 * (months + 2)
        for d in plan["debts"]:
            if plan["finishes"]:
                self.assertEqual(d["remaining"], 0)
                self.assertIsNotNone(d["paid_off"])
            self.assertAlmostEqual(
                d["paid"], start[d["id"]] + d["interest"] - d["remaining"],
                delta=slack)
        self.assertAlmostEqual(
            plan["total_paid"],
            plan["starting"] + plan["total_interest"]
            - sum(d["remaining"] for d in plan["debts"]), delta=slack)
        self.assertAlmostEqual(plan["starting"], sum(start.values()),
                               delta=0.01)

    @given(covered_debts(), st.sampled_from(debt.METHODS),
           st.one_of(st.just(0.0), st.integers(0, 500_000).map(lambda c: c / 100)))
    def test_the_total_falls_and_reaches_zero(self, debts, method, extra):
        for rollover in (True, False):
            plan = debt.simulate(debts, method=method, extra=extra,
                                 start=START, rollover=rollover)
            self._holds(debts, plan)

    def test_the_pages_plan_pays_what_it_owes(self):
        for seed in props.seeds():
            with self.subTest(seed=seed):
                hh = Household(seed, ledger=False)
                try:
                    page = debt.build(hh.conn, TODAY)
                finally:
                    hh.close()
                listed = [d for d in page["debts"] if not d["skip"]]
                for d in listed:
                    self.assertTrue(_finite(d["apr"]) and _finite(d["min_payment"]), d)
                for key in ("plan", "alt", "minimums"):
                    plan = page[key]
                    self.assertAlmostEqual(
                        plan["starting"], sum(d["balance"] for d in listed),
                        delta=0.01)
                    if plan["finishes"]:
                        self.assertAlmostEqual(
                            plan["total_paid"],
                            plan["starting"] + plan["total_interest"],
                            delta=0.01 * (plan["months"] + 2))


if __name__ == "__main__":
    unittest.main()
