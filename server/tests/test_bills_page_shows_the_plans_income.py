"""Bills & Income states the month in the plan's own terms.

The page used to print an income of its own (a nine-month average of what
landed) and "Left for spending & savings" as that average minus the bills,
worked out on the page — a different income and a different "left" from
the Budget header and Today for the same month. The
page shows the plan's income (budget.plan_line, the figure Budget and Today
print) and "left" = the plan's to-spend + excess, sent by the server with
its words (api_bills.bills_hero).
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import api, api_bills  # noqa: F401 — api first: it mounts the area modules

from .util import add_bill, make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


class HeroIsThePlanTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=600, other_monthly=400,
                     budgeted_income_monthly=5000)
        add_bill(self.conn, "Harbor Lofts", 1500.0,
                 next_due=dt.date.today() + dt.timedelta(days=12))

    def hero(self):
        out = api_bills.bills_list(user=_user(self.conn), month=None)
        return {t["key"]: t for t in out["totals"]["hero"]}, out["totals"]

    def test_income_and_left_are_the_plans(self):
        hero, totals = self.hero()
        from oikonome import localtime
        today = localtime.now_local(budget.load_config(self.conn)).date()
        plan = budget.month_status(self.conn, today)["plan_line"]
        self.assertEqual(hero["income"]["amount"], plan["income"])
        self.assertEqual(hero["income"]["amount"], 5000)
        self.assertEqual(hero["income"]["note"],
                         "the monthly income set on Budget")
        self.assertEqual(hero["bills"]["amount"], plan["bills"])
        # left = to spend + excess: 1,000 + (5,000 − 1,500 − 1,000)
        self.assertEqual(hero["left"]["amount"],
                         plan["to_spend"] + plan["excess"])
        self.assertEqual(hero["left"]["amount"], 3500)
        self.assertEqual(hero["left"]["label"], "Left after bills")
        self.assertEqual(hero["left"]["note"],
                         "to spend $1,000 + excess $2,500")
        # never the page's old arithmetic on the other income basis
        self.assertNotEqual(hero["left"]["amount"],
                            round(totals["income_monthly"]
                                  - totals["bills_monthly"]))

    def test_no_income_shows_bills_alone(self):
        write_config(self.conn, food_monthly=600, other_monthly=400)
        hero, _ = self.hero()
        self.assertNotIn("income", hero)
        self.assertNotIn("left", hero)
        self.assertEqual(hero["bills"]["amount"], 1500)


class HeroWordsTests(unittest.TestCase):
    def test_a_savings_leg_is_named_and_a_plan_past_its_income_is_red(self):
        st = {"income_source": "paychecks",
              "plan_line": {"income": 4000, "bills": 2500, "to_spend": 1200,
                            "saved": 500, "excess": -200}}
        rows = {r["key"]: r for r in api_bills.bills_hero(st, bills_monthly=2500.0)}
        self.assertEqual(rows["left"]["label"], "Left after bills and savings")
        self.assertEqual((rows["left"]["amount"], rows["left"]["tone"]),
                         (1000, ""))
        self.assertEqual(rows["left"]["note"], "to spend $1,200 + excess -$200")
        # no paycheck portion sent: the note names the figure as what it
        # is, every counted deposit — never paychecks alone
        self.assertEqual(rows["income"]["note"], "this month's money in")

    def test_paychecks_are_named_only_with_the_portion_they_are(self):
        st = {"income_source": "paychecks", "income_expected": 1830.4,
              "plan_line": {"income": 4000, "bills": 2500, "to_spend": 1200,
                            "excess": 300}}
        rows = {r["key"]: r for r in api_bills.bills_hero(st, bills_monthly=2500.0)}
        self.assertEqual(rows["income"]["note"],
                         "this month's money in, incl. $1,830 of paychecks "
                         "still due")
        closed = {r["key"]: r for r in api_bills.bills_hero(
            st, bills_monthly=2500.0, closed=True)}
        self.assertEqual(closed["income"]["note"], "the month's money in")


if __name__ == "__main__":
    unittest.main()
