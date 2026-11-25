"""The Month and Year lens money maps are rows the server builds and sends
(`plan_rows`, `plan_total`), by the same builder as Today's map
(todayview.map_rows), so the web and the phone print the same rows and
compute none (specs/shown-figures.md, "The Month and Year lens money
maps").

Invariants: the rows come in the wizard's order with Today's words; each
row's status is todayview.row_status over the figures as sent; the
Variable spending row is the sum of the judged rows; a label opens its
bucket for the lens's month (or the year); the Savings row is the
standing Savings goal against what the ledger says was transferred, an
average or an estimate only when that figure is missing, and says so; a
year with no budgeted month has no map. All figures invented.
"""

import datetime as dt
import unittest

from oikonome.web import lenses, todayview

from .util import freeze_month
from .util import TODAY, add_bill, add_ledger_history, add_txn, make_db, \
    write_config


def _bucket(actual, expected, budget, kids=None, remaining=None):
    b = {"actual": actual, "expected": expected, "month_budget": budget}
    if kids:
        b["children"] = kids
        b["display_actual"] = actual - sum(k["actual"] for k in kids)
        b["display_expected"] = expected - sum(k["expected"] for k in kids)
        b["remaining_budget"] = (remaining if remaining is not None
                                 else budget - sum(k["month_budget"]
                                                   for k in kids))
    return b



def _figures(status):
    """A status's figures, without the words written from them."""
    return {k: status[k] for k in ("kind", "amount", "pending")}

class MapRowsTests(unittest.TestCase):
    """map_rows over a lens-shaped bucket set (already paced)."""

    def rows(self, **kw):
        food = _bucket(507.0, 550.0, 700.0,
                       kids=[{"name": "Dining", "actual": 95.0,
                              "expected": 90.0, "month_budget": 100.0}])
        other = _bucket(230.0, 260.0, 300.0)
        fixed = {"actual": 1250.0, "expected": 1500.0, "month_budget": 1800.0}
        args = dict(ledger=lambda k: todayview.ledger_path(k, 2026, 3),
                    pending=300.0,
                    savings=lenses.lens_savings_row(
                        {"monthly_plan": 500.0, "rate_90d": 450.0}, 400.0,
                        projected="avg / mo, last 3 months"),
                    excess=220.0)
        args.update(kw)
        return todayview.map_rows(food, other, fixed, **args)

    def test_rows_in_the_wizard_order_with_todays_words(self):
        rows = todayview.sent_plan_rows(self.rows())
        self.assertEqual([r["label"] for r in rows],
                         ["Food", "Dining", "Everything else", "Bills",
                          "Savings / Investment", "Excess cash"])
        food, dining, other, bills, sav, excess = rows
        # Food is what the carve-out left of both its spend and its budget
        self.assertEqual((food["actual"], food["budget"]), (412.0, 600.0))
        self.assertEqual(_figures(food["status"]),
                         {"kind": "left", "amount": 188.0, "pending": 0.0})
        self.assertEqual(dining["status"]["kind"], "ahead")
        self.assertIsNone(other["caption"])
        self.assertEqual(bills["caption"], "posted $1,250 · awaiting $300 · "
                                           "not counted in the verdict")
        self.assertEqual(_figures(bills["status"]),
                         {"kind": "due", "amount": 250.0, "pending": 300.0})
        self.assertEqual((sav["actual"], sav["budget"], sav["caption"]),
                         (400.0, 500.0, "transferred vs plan"))
        self.assertEqual((excess["actual_label"], excess["budget"]),
                         ("—", 220.0))
        self.assertEqual(food["to"], "/transactions?bucket=food&y=2026&m=3")
        self.assertEqual(dining["to"],
                         "/transactions?bucket=Dining&y=2026&m=3")

    def test_the_total_is_the_sum_of_the_judged_rows(self):
        rows = self.rows()
        t = todayview.plan_total(rows)
        self.assertEqual((t["actual"], t["budget"]), (737.0, 1000.0))
        self.assertEqual(_figures(t["status"]),
                         {"kind": "left", "amount": 263.0, "pending": 0.0})
        self.assertEqual(t["status"]["text"], "$263 left")

    def test_a_year_link_names_no_month(self):
        rows = self.rows(ledger=lambda k: todayview.ledger_path(k, 2026))
        self.assertEqual(rows[0]["to"], "/transactions?bucket=food&y=2026")

    def test_no_excess_row_without_a_surplus(self):
        labels = [r["label"] for r in self.rows(excess=None)]
        self.assertNotIn("Excess cash", labels)
        labels = [r["label"] for r in self.rows(excess=0.0)]
        self.assertNotIn("Excess cash", labels)


class LensSavingsRowTests(unittest.TestCase):
    goal = {"monthly_plan": 500.0, "rate_90d": 400.0}

    def test_the_ledger_figure_is_what_was_transferred(self):
        [r] = lenses.lens_savings_row(self.goal, 300.0, projected="x")
        self.assertEqual((r["actual"], r["budget"], r["caption"]),
                         (300.0, 500.0, "transferred vs plan"))
        # zero transferred is a figure, not a missing one
        [r] = lenses.lens_savings_row(self.goal, 0.0, projected="x")
        self.assertEqual((r["actual"], r["caption"]),
                         (0.0, "transferred vs plan"))

    def test_without_it_the_rate_is_named_for_what_it_is(self):
        [r] = lenses.lens_savings_row(self.goal, None, 9,
                                      projected="estimate from the last 3 months")
        self.assertEqual((r["actual"], r["budget"], r["caption"]),
                         (3600.0, 4500.0, "estimate from the last 3 months"))

    def test_a_plan_only_goal_carries_its_plan_alone(self):
        [r] = lenses.lens_savings_row({"monthly_plan": 500.0,
                                       "rate_90d": None}, None,
                                      projected="x")
        self.assertEqual((r["actual_label"], r["budget"], r["caption"]),
                         ("—", 500.0, None))

    def test_no_row_without_a_goal_a_month_or_any_money(self):
        self.assertEqual(lenses.lens_savings_row(None, 100.0, projected="x"),
                         [])
        self.assertEqual(lenses.lens_savings_row(self.goal, 100.0, 0,
                                                 projected="x"), [])
        self.assertEqual(lenses.lens_savings_row(
            {"monthly_plan": 0.0, "rate_90d": 0.0}, 0.0, projected="x"), [])


class LensRepliesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=400)
        add_ledger_history(self.conn, dt.date(2026, 1, 1))

    def tearDown(self):
        self.conn.close()

    def test_the_month_reply_sends_rows_from_its_own_buckets(self):
        add_bill(self.conn, "Rent", 1200.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(self.conn, dt.date(2026, 6, 1), 1200.0, "RENT")
        add_txn(self.conn, dt.date(2026, 6, 9), 250.0, "GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 6, 12), 75.0, "HARDWARE")
        freeze_month(self.conn, 2026, 6)
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        rows = {x["label"]: x for x in r["plan_rows"]}
        self.assertEqual(list(rows)[:3], ["Food", "Everything else", "Bills"])
        self.assertEqual((rows["Food"]["actual"], rows["Food"]["budget"]),
                         (r["buckets"]["food"]["actual"],
                          r["buckets"]["food"]["month_budget"]))
        self.assertEqual((rows["Bills"]["actual"], rows["Bills"]["budget"],
                          rows["Bills"]["pending"]),
                         (r["bills"]["posted"], r["bills"]["planned"],
                          r["bills"]["awaiting"]))
        self.assertEqual(rows["Food"]["to"],
                         "/transactions?bucket=food&y=2026&m=6")
        for row in r["plan_rows"]:
            self.assertEqual(row["status"], todayview.row_status(row))
        judged = [x for x in r["plan_rows"] if x["judged"]]
        self.assertAlmostEqual(r["plan_total"]["actual"],
                               sum(x["actual"] for x in judged), 2)
        self.assertAlmostEqual(r["plan_total"]["budget"],
                               sum(x["budget"] for x in judged), 2)

    def test_the_year_reply_sends_rows_over_its_budgeted_months(self):
        add_txn(self.conn, dt.date(2026, 2, 12), 900.0, "GROCER",
                primary="FOOD_AND_DRINK")
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        rows = {x["label"]: x for x in y["plan_rows"]}
        ba = y["buckets_annual"]
        self.assertEqual((rows["Food"]["actual"], rows["Food"]["budget"]),
                         (ba["food"]["actual"], ba["food"]["month_budget"]))
        # no dated bill in the year: no Bills row (a period with no dated
        # bill sends none)
        self.assertEqual(ba["fixed"]["month_budget"], 0)
        self.assertNotIn("Bills", rows)
        self.assertEqual(rows["Food"]["to"], "/transactions?bucket=food&y=2026")
        self.assertIsNotNone(y["plan_total"])

    def test_a_year_with_no_budgeted_month_has_no_map(self):
        y = lenses.year_summary(self.conn, 2027, today=TODAY)
        self.assertIsNone(y["plan_rows"])
        self.assertIsNone(y["plan_total"])


class LensSavingsInTheReplies(unittest.TestCase):
    """The lens's Savings row is the standing Savings goal against the
    ledger's own figure for the lens's months."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=400,
                     savings_goals=[{"name": "Savings", "monthly_plan": 250.0,
                                     "account_id": "sav", "tokens": [],
                                     "start_balance": 0}])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")
        add_ledger_history(self.conn, dt.date(2026, 1, 1))
        add_ledger_history(self.conn, dt.date(2026, 1, 1), account="sav")
        add_txn(self.conn, dt.date(2026, 6, 3), -180.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")

    def tearDown(self):
        self.conn.close()

    def test_the_month_row_is_that_months_transfer(self):
        freeze_month(self.conn, 2026, 6)
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        [sav] = [x for x in r["plan_rows"]
                 if x["label"] == "Savings / Investment"]
        self.assertEqual((sav["actual"], sav["budget"], sav["caption"]),
                         (r["saved_month"], 250.0, "transferred vs plan"))

    def test_the_year_row_is_the_plan_times_its_months(self):
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        months = sum(1 for c in y["cells"]
                     if c["status"] in ("final", "projected"))
        [sav] = [x for x in y["plan_rows"]
                 if x["label"] == "Savings / Investment"]
        self.assertEqual(sav["budget"], 250.0 * months)
        if y["saved_ytd"] is not None:
            self.assertEqual((sav["actual"], sav["caption"]),
                             (y["saved_ytd"], "transferred vs plan"))
        else:
            self.assertEqual(sav["caption"], "estimate from the last 3 months")


if __name__ == "__main__":
    unittest.main()
