"""A net that prints as $0 is even: neither a gain nor a loss.

The words beside a net ("you made money", "you lost money"), its colour
and whether a savings rate is printed are decided from the whole dollars
the reader sees, never from the cents under them. A year that lost
under a dollar prints $0; reading the cents made it "you made money" with
a negative rate beside it ("-0.0% of income kept"), and a sub-dollar loss
in a month cell read as a red "+$0". Printed $0 reads "broke even", in a
neutral tone, with no rate. The yearly and monthly emails, the Year
lens's month cells, and both clients' Year totals and Cash Flow (which
share one helper, `netSign`, whose cases run in
test_client_money_twins) follow the one rule.
"""

import pathlib
import re
import unittest

import oikonome
from oikonome.web import lens_email, lenses

from .test_monthly_email_reads_like_the_month_lens import _month
from .test_monthly_email_reads_like_the_month_lens import _year as _month_year

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_PER_MONTH = 3700.0


def _year(spend_dec):
    """Twelve net months with as much in as out, December's spend
    `spend_dec` instead: the year's net is that month's alone."""
    cells = [{"m": m, "status": "net", "verdict": None, "variance": None,
              "income": _PER_MONTH, "spend": _PER_MONTH, "net": 0.0}
             for m in range(1, 12)]
    cells.append({"m": 12, "status": "net", "verdict": None,
                  "variance": None, "income": _PER_MONTH, "spend": spend_dec,
                  "net": round(_PER_MONTH - spend_dec, 2)})
    income = 12 * _PER_MONTH
    spend = 11 * _PER_MONTH + spend_dec
    saved = round(income - spend, 2)
    totals = {"income": income, "spend": spend, "saved": saved,
              "rate": round(100 * saved / income, 1)}
    totals["dollars"] = lenses.totals_dollars(totals, every_month=True)
    return {"y": 2026, "cells": cells, "totals": totals, "detail": {}}


class TheYearlyEmail(unittest.TestCase):
    def _ctx(self, spend_dec):
        return lens_email.year_mail_context(_year(spend_dec), 2026)

    def test_a_year_that_lost_under_a_dollar_broke_even(self):
        ctx = self._ctx(_PER_MONTH + 0.4)
        self.assertEqual(ctx["headline"], "2026: you broke even")
        self.assertEqual(ctx["verdict_tone"], "")
        tile = ctx["tiles"][0]
        self.assertEqual(tile["value"], "$0")
        self.assertEqual(tile["tone"], "")
        self.assertNotIn("% of income kept", tile["sub"])
        self.assertNotIn("more in than out", tile["sub"])

    def test_a_year_that_made_under_a_dollar_broke_even(self):
        ctx = self._ctx(_PER_MONTH - 0.3)
        self.assertEqual(ctx["headline"], "2026: you broke even")
        self.assertNotIn("% of income kept", ctx["tiles"][0]["sub"])

    def test_a_year_that_made_a_few_dollars_made_money_and_keeps_its_rate(self):
        ctx = self._ctx(_PER_MONTH - 12.0)
        self.assertEqual(ctx["headline"], "2026: you made money")
        self.assertEqual(ctx["verdict_tone"], "pos")
        self.assertIn("% of income kept", ctx["tiles"][0]["sub"])

    def test_a_year_that_lost_a_few_dollars_lost_money_without_a_rate(self):
        ctx = self._ctx(_PER_MONTH + 13.0)
        self.assertEqual(ctx["headline"], "2026: you lost money")
        self.assertEqual(ctx["tiles"][0]["tone"], "neg")
        self.assertNotIn("% of income kept", ctx["tiles"][0]["sub"])


class TheMonthlyEmail(unittest.TestCase):
    def test_a_month_that_lost_under_a_dollar_broke_even(self):
        ctx = lens_email.month_mail_context(
            _month(income={"actual": 7000.0, "budgeted": None},
                   spend_total=7000.4), _month_year(), 2026, 9)
        self.assertEqual(ctx["flow"]["net_text"], "$0")
        self.assertEqual(ctx["flow"]["tone"], "")
        self.assertIn("broke even", ctx["flow"]["sentence"])
        self.assertNotIn("made money", ctx["flow"]["sentence"])


class TheYearLensMonthCell(unittest.TestCase):
    def _words(self, net):
        return lenses.cell_words(
            {"status": "net", "income": _PER_MONTH,
             "spend": _PER_MONTH - net, "net": net}, "closed")

    def test_a_cell_that_lost_under_a_dollar_is_even_and_neutral(self):
        w = self._words(-0.4)
        self.assertEqual(w["figure"], "$0")
        self.assertEqual(w["tone"], "mut")

    def test_a_cell_that_lost_a_dollar_is_a_loss(self):
        w = self._words(-1.2)
        self.assertEqual(w["figure"], "−$1")
        self.assertEqual(w["tone"], "neg")


class BothClientsReadThePrintedSign(unittest.TestCase):
    """The Year totals and Cash Flow decide tone and rate with netSign."""

    SCREENS = ("webapp/src/pages/YearLens.tsx", "webapp/src/pages/CashFlow.tsx",
               "mobile/src/app/year.tsx", "mobile/src/app/cashflow.tsx")

    def test_every_screen_that_prints_a_year_net_uses_net_sign(self):
        for rel in self.SCREENS:
            src = (_APP / rel).read_text()
            self.assertRegex(src, r"\bnetSign\(", rel)

    def test_no_screen_tones_a_left_over_figure_by_its_cents(self):
        # the old shapes: a tone or a word picked by the raw value's sign
        raw = re.compile(r"(t\.saved|totals\.saved|flow\.saved|\bsaved)\s*>=\s*0")
        for rel in self.SCREENS:
            src = (_APP / rel).read_text()
            self.assertIsNone(raw.search(src), rel)


if __name__ == "__main__":
    unittest.main()
