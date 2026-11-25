"""The money map's status words and the cash notice's sentence are
written once, on the server, and every surface prints them: the page,
the phone, the email HTML and its plain text. A client that composed its
own copy could drift from the others. The clients also print the plan line's own terms only; a figure
picked from a different definition when one is missing is a second
definition (specs/counted-figures.md). Figures are invented."""

import datetime as dt
import pathlib
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _row(actual, expected, budget, *, judged=True, label="Food",
         pending=0.0):
    return {"label": label, "judged": judged, "actual": actual,
            "expected": expected, "budget": budget, "pending": pending,
            "actual_label": None}


class RowWordsTests(unittest.TestCase):
    def test_every_status_carries_its_words_and_tone(self):
        cases = [
            (_row(620, 400, 600), "over by $20", "neg"),
            (_row(470, 400, 600), "$70 over pace so far", "neg"),
            (_row(310, 400, 600), "$290 left", "pos"),
            (_row(700, 900, 1300, judged=False, label="Bills",
                  pending=150), "$150 awaiting · $450 still due", "mut"),
        ]
        for row, text, tone in cases:
            st = todayview.row_status(row)
            self.assertEqual((st["text"], st["tone"]), (text, tone))

    def test_the_email_prints_the_status_text(self):
        row = _row(470, 400, 600)
        self.assertEqual(todayview._row_delta(row)["text"],
                         todayview.row_status(row)["text"])


class ChipSaysTodayTests(unittest.TestCase):
    def test_a_category_over_today_says_today_not_the_month(self):
        """The summary face's chips are today's figures: a category past
        today's allowance says "over by $X today", so it is not read as
        the month's overrun printed on the Month and Budget pages."""
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=300, other_monthly=300)
        add_txn(conn, TODAY, 90.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        d = report.gather(conn, TODAY)
        chips = todayview.build_context(d)["day"]["simple"]["chips"]
        over = [c["text"] for c in chips if c["tone"] == "neg"]
        self.assertTrue(over)
        for text in over:
            self.assertRegex(text, r" over by \$[\d,]+ today$")


class ExcessIsThePlansTests(unittest.TestCase):
    def test_the_excess_line_says_it_is_the_plans(self):
        """The folded excess line under a money map is the plan line's
        figure (bills evened out), not what this month leaves: the server
        says so, and the page, the phone and the email print its words."""
        from oikonome.web import lenses
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=300, other_monthly=300,
                     budgeted_income_monthly=4000)
        d = report.gather(conn, TODAY)
        rows, _t = todayview.today_map(d)
        lens = lenses.month_summary(conn, TODAY.year, TODAY.month,
                                    today=TODAY)
        for got in (rows, lens["plan_rows"]):
            ex = next(r for r in got if r["label"] == "Excess cash")
            self.assertIn("planned excess", ex["caption"])
            self.assertTrue(ex["caption"].startswith(
                f"${round(ex['budget']):,}"))
        _s, plain, html = report.build(d)
        self.assertIn("planned excess", html)
        self.assertNotIn("excess stays in checking", html)


class ExcessOnAMonthPastItsBudgetTests(unittest.TestCase):
    def test_none_is_left_only_once_the_overrun_spends_it_all(self):
        """The Budget summary printed a bare green excess, and its Excess
        row "— / $X", in a month whose spending had used it all up. The
        server words both: a plan figure; "none this month" only when the
        overrun of the month's budget is at least the excess — a smaller
        overrun leaves some of it, and the words stay the plan's."""
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=300, other_monthly=300,
                     budgeted_income_monthly=4000)
        d = report.gather(conn, TODAY)
        self.assertEqual(todayview.plan_excess_words(d),
                         {"text": "planned excess", "tone": "pos"})
        # past the month's budget, by less than the excess
        add_txn(conn, TODAY, 900.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        d = report.gather(conn, TODAY)
        self.assertGreater(d["variable_actual"], d["variable_budget_paced"])
        self.assertEqual(todayview.plan_excess_words(d),
                         {"text": "planned excess", "tone": "pos"})
        rows, _t = todayview.today_map(d)
        ex = next(r for r in rows if r["label"] == "Excess cash")
        self.assertNotIn("none this month", ex["caption"])
        # ...and by more than all of it
        add_txn(conn, TODAY, 4000.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        d = report.gather(conn, TODAY)
        self.assertEqual(todayview.plan_excess_words(d),
                         {"text": "planned excess · none this month",
                          "tone": "mut"})
        rows, _t = todayview.today_map(d)
        ex = next(r for r in rows if r["label"] == "Excess cash")
        self.assertIn("none this month", ex["caption"])


class NoExcessHasNothingToUseUpTests(unittest.TestCase):
    def test_a_plan_with_no_positive_excess_is_never_used_up(self):
        """A plan whose excess is zero or below never had one to spend:
        an overrun says nothing of using it up. Figures are invented."""
        base = {"variable_actual": 900.0, "variable_budget_paced": 600.0}
        for ex in (None, 0.0, 0.4, -250.0):
            self.assertFalse(todayview.excess_used_up(
                {**base, "plan_surplus": ex}), ex)
        self.assertTrue(todayview.excess_used_up(
            {**base, "plan_surplus": 250.0}))
        st = {**base, "plan_surplus": -250.0,
              "plan_legs": {"excess": -250}}
        self.assertEqual(todayview.plan_excess_words(st),
                         {"text": "planned excess", "tone": "neg"})


class CashSentenceTests(unittest.TestCase):
    def _note(self, **kw):
        base = {"state": "tight", "kind": "headroom", "amount": 640.0,
                "slow_to_per_day": None, "until": "2026-03-20", "ask": None}
        base.update(kw)
        return base

    def test_each_kind_has_one_sentence(self):
        self.assertEqual(
            todayview.cash_sentence(self._note()),
            "Cash is tight — $640 left until your next paycheck "
            "(03/20/26) after the bills due before then.")
        self.assertEqual(
            todayview.cash_sentence(self._note(
                state="critical", kind="date", amount=85.0,
                slow_to_per_day=41.0)),
            "Short $85 by 03/20/26 — cover it from savings, or slow "
            "spending to $41/day.")
        self.assertEqual(
            todayview.cash_sentence(self._note(
                state="critical", kind="today", amount=210.0)),
            "Short $210 today — checking doesn't cover the bills due "
            "before your next paycheck (03/20/26). Cover it from savings.")
        self.assertEqual(
            todayview.cash_sentence(self._note(ask="Add your paycheck.")),
            "Add your paycheck.")
        self.assertIsNone(todayview.cash_sentence(self._note(state=None)))


class NoticeOnEverySurfaceTests(unittest.TestCase):
    def setUp(self):
        # checking $100 and a $1,150 bill in five days: short by then
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.conn.execute("UPDATE accounts SET balance_available=100, "
                          "balance_current=100 WHERE id='chk'")
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_bill(self.conn, "Paycheck Co", 1000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=3),
                 income=True, last_seen=TODAY - dt.timedelta(days=11))
        add_bill(self.conn, "Water", 1150.0,
                 next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        self.d = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def test_the_sentence_rides_the_notice_and_both_emails(self):
        note = todayview.build_context(self.d)["cash_notice"]
        self.assertEqual(note["state"], "critical")
        self.assertTrue(note["text"].startswith("Short $50 by "))
        _s, plain, html = report.build(self.d)
        self.assertIn("!! CASH: " + note["text"], plain)
        self.assertIn(note["text"].replace("&", "&amp;"), html)


class ClientsPrintTests(unittest.TestCase):
    """The clients carry no sentence of their own and no second figure."""

    FILES = ["webapp/src/pages/Today.tsx",
             "webapp/src/components/MoneyMap.tsx",
             "mobile/src/app/(tabs)/index.tsx",
             "mobile/src/components/money-map.tsx"]

    def test_no_client_writes_the_words(self):
        for f in self.FILES:
            src = (ROOT / f).read_text()
            for words in ("Cash gets tight", "Cash is tight",
                          "more than planned`", "still due`",
                          "`over by ${money(st"):
                self.assertNotIn(words, src, f"{f} composes {words!r}")

    def test_no_client_falls_back_to_another_figure(self):
        for f in self.FILES:
            src = (ROOT / f).read_text()
            for chain in ("pl ? pl.bills : d.avg_bills",
                          "d.avg_bills ?? d.scheduled_month",
                          "pl ? pl.income : d.income"):
                self.assertNotIn(chain, src, f"{f} falls back: {chain!r}")


if __name__ == "__main__":
    unittest.main()
