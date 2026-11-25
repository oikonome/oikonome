"""The plan identity holds for generated households
(specs/invariants-and-fuzzing.md, M9; households from
tests/money_households.py with a plan, seeds from tests/props.py).

Property, on `budget.month_status` and on the Today payload both clients
(and the Budget page) draw:

* the month's excess is income − the plan's bills − planned spending −
  the monthly plan of every savings goal in monthly mode:
  `plan_surplus == income − avg_bills − plan_variable − savings_plan`,
  and None exactly when there is no income;
* `savings_plan` is the sum of the goals `plan_goals` lists, and those are
  exactly the household's monthly-mode goals (a sweep goal takes from the
  excess only when the month provides it);
* the plan rows say the same: the Savings row's plan is `savings_plan`,
  the Excess row is `plan_surplus` (shown only when there is one), and the
  variable bar's budget is the sum of the judged rows — the paced
  variable budget, which is `plan_variable` when nothing scales or paces
  the month;
* the payload carries month_status's figures rounded to the cent, never a
  second derivation;
* the plan line adds up as printed: `plan_line` holds its terms in whole
  dollars, income − bills − to spend − saved is exactly the printed
  excess, the excess is the plan's own figure rounded once and each term
  is within a dollar of its own; the daily email's plain and HTML renders
  print those terms, so the chain the reader adds up is the one shown.
"""

from __future__ import annotations

import re
from html import escape as html_escape
import unittest

from oikonome.engine import budget, reporting

from . import props
from .money_surfaces import Surfaces


def _c(x) -> int:
    return int(round(float(x) * 100))


class ThePlanLineAddsUp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.households = [Surfaces(s, doors=("today",)) for s in props.seeds(8)]

    @classmethod
    def tearDownClass(cls):
        for s in cls.households:
            s.close()

    def test_the_excess_is_income_less_bills_spending_and_every_goal(self):
        for s in self.households:
            st = s.status
            with self.subTest(seed=s.seed):
                if not st["income"]:
                    self.assertIsNone(st["plan_surplus"])
                    continue
                self.assertAlmostEqual(
                    st["plan_surplus"],
                    st["income"] - st["avg_bills"] - st["plan_variable"]
                    - st["savings_plan"], places=6)

    def test_the_savings_plan_is_every_monthly_goal(self):
        for s in self.households:
            st = s.status
            with self.subTest(seed=s.seed):
                monthly = [(g["name"], round(g["monthly_plan"], 2))
                           for g in s.hh.goals if g.get("mode") != "sweep"]
                self.assertEqual([(g["name"], g["monthly_plan"])
                                  for g in st["plan_goals"]], monthly)
                self.assertEqual(_c(st["savings_plan"]),
                                 sum(_c(p) for _, p in monthly))

    def test_the_payload_carries_the_status_figures(self):
        for s in self.households:
            st, t = s.status, s.payload["today"]
            with self.subTest(seed=s.seed):
                for key in ("avg_bills", "plan_variable", "savings_plan"):
                    self.assertEqual(t[key], round(st[key], 2), key)
                for key in ("income", "plan_surplus"):
                    self.assertEqual(
                        t[key], None if st[key] is None else round(st[key], 2),
                        key)
                if t["plan_surplus"] is not None:
                    # the shown terms produce the shown excess, to the cent
                    # (each term is rounded once, so within a cent per term)
                    self.assertLessEqual(abs(
                        _c(t["income"]) - _c(t["avg_bills"])
                        - _c(t["plan_variable"]) - _c(t["savings_plan"])
                        - _c(t["plan_surplus"])), 2)

    def test_the_plan_line_adds_up_as_printed(self):
        lines = 0
        for s in self.households:
            st, t = s.status, s.payload["today"]
            with self.subTest(seed=s.seed):
                pl = t["plan_line"]
                self.assertEqual(pl, st["plan_line"])
                if st["plan_surplus"] is None:
                    self.assertIsNone(pl)
                    continue
                lines += 1
                saved = pl["saved"] or 0
                self.assertEqual(pl["income"] - pl["bills"] - pl["to_spend"]
                                 - saved, pl["excess"])
                self.assertEqual(pl["excess"],
                                 reporting.whole_dollars(st["plan_surplus"]))
                self.assertEqual(pl["saved"] is None,
                                 st["savings_plan"] <= 0.005)
                for key, raw in (("income", st["income"]),
                                 ("bills", st["avg_bills"]),
                                 ("to_spend", st["plan_variable"]),
                                 ("saved", st["savings_plan"])):
                    if pl[key] is not None:
                        self.assertLessEqual(abs(pl[key] - raw), 1, key)
                # the leg is the dated bills and the pools' set-aside, said
                # as both when the plan has a pool (plan_bills_label)
                parts = st["avg_bills_parts"]
                self.assertEqual(_c(parts["bills"] + parts["envelopes"]),
                                 _c(st["avg_bills"]))
                self.assertLessEqual(abs(pl["bills"] - parts["bills"]
                                         - parts["envelopes"]), 1)
                label = st["plan_bills_label"]
                self.assertEqual(label, "bills & envelopes"
                                 if st["buckets"]["fixed"]["envelopes"]
                                 else "bills")
                # the email prints the same terms, so they add up there too
                m = re.search(
                    r"Plan: \$([\d,]+) in -> \$([\d,]+) " + re.escape(label)
                    + r"(?: \(.*?\))? -> "
                    r"\$([\d,]+) to spend(?: -> \$([\d,]+) saved)? -> "
                    r"(-?)\$([\d,]+) planned excess(?: · none this month)?",
                    s.email_text)
                self.assertIsNotNone(m, s.email_text)
                n = [int(g.replace(",", "")) if g else 0
                     for g in (m[1], m[2], m[3], m[4], m[6])]
                n[4] = -n[4] if m[5] else n[4]
                self.assertEqual(n[0] - n[1] - n[2] - n[3], n[4])
                self.assertEqual(n, [pl["income"], pl["bills"],
                                     pl["to_spend"], saved, pl["excess"]])
                # and the HTML mail, which is the Today template
                html = s.email_html
                for word, v in (("in", pl["income"]),
                                # the HTML escapes the ampersand
                                (re.escape(html_escape(label)), pl["bills"]),
                                ("to spend", pl["to_spend"]),
                                ("saved", pl["saved"]),
                                # the Budget summary's words for it
                                # (todayview.plan_excess_words)
                                ("planned excess(?: · none this month)?",
                                 pl["excess"])):
                    if v is None:
                        continue
                    money = ("-" if v < 0 else "") + f"${abs(v):,}"
                    self.assertRegex(html, re.escape(money)
                                     + r"</b>\s*" + word + "</span>", word)
        self.assertTrue(lines)

    def test_the_line_is_rounded_to_its_excess(self):
        """A line whose terms, each rounded alone, miss the excess by a
        dollar: $3,442.60 − $1,332.60 − $1,028.60 − $324.60 = $756.80
        prints $3,443 − $1,333 − $1,029 − $325 beside $757 one by one."""
        pl = budget.plan_line(3442.60, 1332.60, 1028.60, 324.60, 756.80)
        self.assertEqual(pl["excess"], 757)
        self.assertEqual(pl["income"] - pl["bills"] - pl["to_spend"]
                         - pl["saved"], 757)
        self.assertIsNone(budget.plan_line(None, 10, 10, 0, None))
        no_goal = budget.plan_line(100.4, 30.3, 20.3, 0, 49.8)
        self.assertIsNone(no_goal["saved"])
        self.assertEqual(no_goal["income"] - no_goal["bills"]
                         - no_goal["to_spend"], no_goal["excess"])

    def test_the_plan_rows_say_the_same(self):
        for s in self.households:
            t = s.payload["today"]
            rows = {r["label"]: r for r in t["plan_rows"]}
            with self.subTest(seed=s.seed):
                if t["savings_plan"] > 0.005:
                    self.assertEqual(rows["Savings / Investment"]["budget"],
                                     t["savings_plan"])
                if t["plan_surplus"] is not None and t["plan_surplus"] > 0.005:
                    self.assertEqual(rows["Excess cash"]["budget"],
                                     t["plan_surplus"])
                else:
                    self.assertNotIn("Excess cash", rows)
                judged = [r for r in t["plan_rows"] if r["judged"]]
                self.assertLessEqual(abs(
                    sum(_c(r["budget"]) for r in judged)
                    - _c(t["plan_total"]["budget"])), len(judged))
                self.assertLessEqual(abs(
                    _c(t["plan_total"]["budget"])
                    - _c(t["variable_budget_paced"])), len(judged))
                if t["variable_scale"] is None and t["paced_from"] is None:
                    self.assertEqual(t["variable_budget_paced"],
                                     t["plan_variable"])


if __name__ == "__main__":
    unittest.main()
