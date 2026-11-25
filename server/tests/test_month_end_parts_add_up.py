"""The month-end figure the cash tiles subtract and its two printed
halves (specs/today-figures.md, "One bills-still-due figure" and
"Envelopes are their own row"): `report.month_end_parts`.

* bills + envelopes = the total as printed, whole dollars;
* the total is bills_remaining_month to the cent, and the halves in
  cents add up to it within a cent;
* neither half is ever below nothing, and a half with nothing in it
  prints nothing.
"""

from __future__ import annotations

import random
import unittest

from oikonome.engine import budget
from oikonome.engine.reporting import whole_dollars
from oikonome.web import report

from . import props
from .envelope_households import EnvelopeHousehold
from .util import TODAY


def _check(case, parts):
    case.assertEqual(parts["bills"] + parts["envelopes"],
                     whole_dollars(parts["total"]), parts)
    case.assertLessEqual(abs(parts["bills_cents"] + parts["envelopes_cents"]
                             - parts["total"]), 0.011, parts)
    case.assertGreaterEqual(parts["bills"], 0, parts)
    case.assertGreaterEqual(parts["envelopes"], 0, parts)
    if parts["envelopes_cents"] == 0:
        case.assertEqual(parts["envelopes"], 0, parts)
    if parts["bills_cents"] == 0:
        case.assertEqual(parts["bills"], 0, parts)


class TheSplitAddsUpTests(unittest.TestCase):

    def test_generated_figures(self):
        """20k drawn walks (more in long mode): a few bills, a few
        reserves, in cents and in the thirds a yearly pool's daily share
        leaves."""
        n = 200_000 if props.LONG else 20_000
        rng = random.Random(props.run_seed() or 20)
        for i in range(n):
            planned = [round(rng.uniform(0, 2500), 2)
                       for _ in range(rng.randint(0, 5))]
            reserves = [rng.choice((0.0, round(rng.uniform(0, 400), 2),
                                    rng.uniform(0, 300)))
                        for _ in range(rng.randint(0, 4))]
            parts = report.split_month_end(planned, reserves)
            with self.subTest(i=i):
                _check(self, parts)
                total = sum(planned)
                for r in reserves:
                    total += r
                self.assertEqual(parts["total"], round(total, 2))

    def test_households(self):
        """For generated households with pools: the parts the page prints
        are the figure the cash tiles subtract."""
        for seed in props.seeds(default=6):
            h = EnvelopeHousehold(seed)
            try:
                st = report.gather(h.conn, TODAY)
                parts = report.month_end_parts(
                    h.conn, budget.load_config(h.conn), TODAY,
                    st["buckets"]["fixed"]["envelopes"])
                with self.subTest(seed=seed):
                    _check(self, parts)
                    self.assertEqual(parts["total"],
                                     st["bills_remaining_month"])
                    self.assertEqual(st["month_end_parts"]["bills"],
                                     parts["bills"])
                    self.assertEqual(st["month_end_parts"]["envelopes"],
                                     parts["envelopes"])
                    # both halves, only when both are above $0; printed
                    # beside the still-due figures, never under the
                    # checking-at-month-end balance (part of it is already
                    # out of that figure)
                    text = st["month_end_parts"]["text"]
                    both = parts["bills"] > 0 and parts["envelopes"] > 0
                    self.assertEqual(text is not None, both)
                    if both:
                        self.assertEqual(
                            text, f"bills ${parts['bills']:,} + envelopes "
                                  f"${parts['envelopes']:,}")
                        _s, plain, html = report.build(st)
                        if st.get("runway"):
                            self.assertNotIn(
                                f"still owed this month: {text}",
                                plain + html)
            finally:
                h.close()


if __name__ == "__main__":
    unittest.main()
