"""An income kind reads the same on the Overview picture and in the Income
tab's legend: one function decides its whole dollars, adding up to "came
in" (specs/shown-figures.md, "One income figure on Overview and Income").
Rounded on its own in the legend, a kind could sit a dollar from the same
kind in the picture one tab over.
"""
import datetime as dt
import pathlib
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)
_ROOT = pathlib.Path(__file__).resolve().parents[2]


class OneFigureForAKind(unittest.TestCase):
    def test_the_legend_dollars_add_up_to_came_in(self):
        kinds = {"paychecks": 2500.5, "interest": 10.5, "other": 490.5}
        shown = reporting.income_kinds_shown(kinds, 3001.5)
        self.assertEqual(sum(shown.values()), 3002)
        # cell by cell these read 2500 + 10 + 490, two short of "$3,002"
        self.assertNotEqual(
            shown, {k: reporting.whole_dollars(v) for k, v in kinds.items()})

    def test_the_picture_prints_the_same_dollars(self):
        inc = {"total": 3001.5, "paychecks": 2500.5, "interest": 10.5,
               "other": 490.5}
        ins, _outs = reporting._flow_shown(
            inc, 2900.0, {"fixed": 1000.0, "variable": 1900.0}, 101.5)
        self.assertEqual(
            {n["key"]: n["dollars"] for n in ins},
            reporting.income_kinds_shown(inc, inc["total"]))

    def test_overview_and_income_agree_over_one_window(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        for month in (8, 9, 10):
            add_txn(conn, dt.date(2026, month, 3), -1000.5, "CLIENT DEPOSIT",
                    account="chk", primary="INCOME")
            add_txn(conn, dt.date(2026, month, 28), -3.5, "INTEREST PAID",
                    account="chk", primary="INCOME",
                    detailed="INCOME_INTEREST_EARNED")
            add_txn(conn, dt.date(2026, month, 9), 40.0, "Grocer",
                    account="chk", primary="FOOD_AND_DRINK")
        flow = reporting.flow_breakdown(conn, TODAY, "3m")
        income = reporting.income_window(conn, TODAY, "3m")
        self.assertEqual(income["total"], flow["in"]["total"])
        self.assertEqual(
            income["kinds_shown"],
            {n["key"]: n["dollars"] for n in flow["in"]["shown"]
             if n["key"] in income["kinds"]})
        self.assertEqual(sum(income["kinds_shown"].values()),
                         reporting.whole_dollars(income["total"]))

    def test_both_legends_print_the_shown_kind(self):
        web = (_ROOT / "webapp/src/pages/CashFlow.tsx").read_text()
        mob = (_ROOT / "mobile/src/app/cashflow.tsx").read_text()
        self.assertTrue("w.kinds_shown[k]" in web)
        self.assertTrue("w.kinds_shown" in mob)
        self.assertFalse("{KIND_LABELS[k]} {money(v)}" in web)
        self.assertFalse("{KIND_LABELS[k]} {m$(v)}" in mob)


if __name__ == "__main__":
    unittest.main()
