"""A month's categories read one vocabulary on every page.

An envelope's spill past its cap is a row of its own inside the budget's
buckets, labelled "envelope overflow". The everyday-spending lists — the
Month lens's, the monthly and yearly emails' rings and the Year lens's —
showed it as a category of that name, beside the Spending tab, Today and
the Month lens's whole list, which count the purchase under its own
category. A list now counts the spill under the purchase's
category (budget.listed_category); the buckets are unchanged."""

import datetime as dt
import unittest

from oikonome.engine import budget, reporting
from oikonome.web import lens_email, lenses, todayview, report

from .util import add_bill, add_txn, make_db, write_config, freeze_month

TODAY = dt.date(2026, 11, 17)
LAST = dt.date(2026, 10, 31)


class AnEnvelopeThatSpilled(unittest.TestCase):
    """A $400 physio envelope; $150 and $270 of physio in October (a $20
    spill), $500 of groceries."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Birchwood Physio", 400.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, dt.date(2026, 10, 9), 150.0, "BIRCHWOOD PHYSIO",
                primary="MEDICAL")
        add_txn(self.conn, dt.date(2026, 10, 17), 270.0, "BIRCHWOOD PHYSIO",
                primary="MEDICAL")
        add_txn(self.conn, dt.date(2026, 10, 5), 500.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def test_the_month_lens_lists_the_spill_under_its_category(self):
        freeze_month(self.conn, 2026, 10)
        d = lenses.month_summary(self.conn, 2026, 10, today=TODAY)
        whole = dict((c[0], c[1]) for c in d["by_category"])
        self.assertNotIn("envelope overflow", whole)
        # the whole purchase, as the Spending tab counts it
        self.assertAlmostEqual(whole["MEDICAL"], 420.0)
        variable = dict((c[0], c[1]) for c in d["by_category_variable"])
        self.assertNotIn("envelope overflow", variable)
        self.assertAlmostEqual(variable["MEDICAL"], 20.0)
        sw = reporting.spending_window(self.conn, TODAY, "1m")
        self.assertEqual(dict((c[0], c[1]) for c in sw["categories"])
                         ["MEDICAL"], 420.0)

    def test_a_label_still_opens_its_rows(self):
        st = lenses.month_state(self.conn, 2026, 10, TODAY)
        info = lenses.month_category_ledger(st, "MEDICAL")
        self.assertAlmostEqual(info["amount"], 420.0)
        self.assertEqual(len(info["txn_ids"]), 2)

    def test_the_yearly_email_has_no_envelope_overflow_category(self):
        d = lenses.year_summary(self.conn, 2026, TODAY, with_detail=True)
        labels = [c[0] for c in d["detail"]["categories_variable"]]
        self.assertNotIn("envelope overflow", labels)
        ctx = lens_email.year_mail_context(d, 2026)
        self.assertNotIn("envelope overflow", repr(ctx).lower())

    def test_today_lists_no_envelope_overflow_either(self):
        st = budget.month_status(self.conn, LAST, closed=True)
        self.assertTrue(any(r["category"] == "envelope overflow"
                            for r in st["buckets"]["other"]["rows"]),
                        "the buckets keep the spill as its own row")
        ctx = todayview.build_context(report.gather(self.conn, LAST))
        self.assertNotIn("envelope overflow",
                         [e[0] for e in ctx.get("mtd") or []])


if __name__ == "__main__":
    unittest.main()
