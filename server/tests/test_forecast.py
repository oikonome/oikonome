"""Forecast walk arithmetic and card-payoff scenarios. Do not weaken one of
these to make a change pass. Fixtures are per-tenant, liabilities raw is
jsonb, config rides tenant_settings, dates are date objects.
"""

import datetime as dt
import unittest

from oikonome.engine import forecast
from oikonome.engine.compat import jsonb

from .util import TODAY, add_bill, add_txn, make_db, write_config


class TestForecast(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)

    def tearDown(self):
        self.conn.close()

    def test_walk_arithmetic_exact(self):
        """checking 5000 − card 250 = 4750 start; one $300 bill on +5d,
        one $1000 paycheck on +10d, zero variable burn."""
        add_bill(self.conn, "Water", 300.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        add_bill(self.conn, "Paycheck Co", 1000.0, frequency="MONTHLY",
                 next_due=TODAY + dt.timedelta(days=10), income=True,
                 last_seen=TODAY - dt.timedelta(days=20))
        f = forecast.build(self.conn, today=TODAY, days=30)
        self.assertEqual(f["start"], 4750.0)
        curve = dict(f["plan"]["series"])
        self.assertEqual(curve[str(TODAY + dt.timedelta(days=5))], 4450.0)
        self.assertEqual(curve[str(TODAY + dt.timedelta(days=10))], 5450.0)
        self.assertIsNone(f["plan"]["negative_date"])
        self.assertEqual(f["plan"]["min"], 4450.0)

    def test_negative_detection(self):
        self.conn.execute("UPDATE accounts SET balance_available=100, "
                          "balance_current=100 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_bill(self.conn, "Rent", 900.0, next_due=TODAY + dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=27))
        f = forecast.build(self.conn, today=TODAY, days=30)
        self.assertEqual(f["plan"]["negative_date"],
                         str(TODAY + dt.timedelta(days=3)))

    def test_first_negative_amount_is_the_balance_at_that_date(self):
        """A small early dip plus a deeper later trough are two different
        facts: `negative_date` is the first day under $0 and
        `first_neg_amount` is the balance ON that day — never the deeper
        `min`, which belongs to `min_date`. The cash-critical message pairs
        date with amount from these, so they must not cross-wire."""
        self.conn.execute("UPDATE accounts SET balance_available=100, "
                          "balance_current=100 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_bill(self.conn, "Water", 150.0,
                 next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        add_bill(self.conn, "Insurance", 5000.0,
                 next_due=TODAY + dt.timedelta(days=40),
                 last_seen=TODAY - dt.timedelta(days=20))
        f = forecast.build(self.conn, today=TODAY, days=60)
        s = f["pace_stmt"]
        self.assertEqual(s["negative_date"], str(TODAY + dt.timedelta(days=5)))
        self.assertEqual(s["first_neg_amount"], -50.0)
        self.assertLess(s["min"], -5000.0)           # the later trough
        self.assertNotEqual(s["min_date"], s["negative_date"])

    def test_first_negative_amount_absent_when_never_negative(self):
        f = forecast.build(self.conn, today=TODAY, days=30)
        self.assertIsNone(f["plan"]["negative_date"])
        self.assertIsNone(f["plan"]["first_neg_amount"])

    def test_no_bills_no_crash(self):
        f = forecast.build(self.conn, today=TODAY, days=30)
        self.assertEqual(f["plan"]["min"], f["start"])

    def test_received_paycheck_is_not_scheduled_again(self):
        """A paycheck that already posted to checking is in the live
        starting balance. Walking it in again on its due date would count
        the same money twice for every day between the deposit and payday."""
        add_bill(self.conn, "Paycheck Co", 1000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=1),
                 income=True, last_seen=TODAY - dt.timedelta(days=13))
        add_txn(self.conn, TODAY, -1000.0, "Paycheck Co", account="chk",
                primary="INCOME")
        f = forecast.build(self.conn, today=TODAY, days=30)
        curve = dict(f["plan"]["series"])
        self.assertEqual(curve[str(TODAY + dt.timedelta(days=1))], f["start"])
        # the FOLLOWING paycheck is still expected
        self.assertEqual(curve[str(TODAY + dt.timedelta(days=15))],
                         f["start"] + 1000.0)
        self.assertEqual(forecast.next_paycheck(self.conn, TODAY),
                         TODAY + dt.timedelta(days=15))

    def test_a_transfer_of_paycheck_size_does_not_mark_it_received(self):
        """A savings→checking move that happens to match the paycheck is
        not income; the paycheck is still due and still scheduled."""
        add_bill(self.conn, "Paycheck Co", 1000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=1),
                 income=True, last_seen=TODAY - dt.timedelta(days=13))
        add_txn(self.conn, TODAY, -1000.0, "Transfer from savings",
                account="chk", primary="TRANSFER_IN")
        f = forecast.build(self.conn, today=TODAY, days=30)
        curve = dict(f["plan"]["series"])
        self.assertEqual(curve[str(TODAY + dt.timedelta(days=1))],
                         f["start"] + 1000.0)

    def test_income_toggle_caps_paychecks(self):
        """When the retirement-saving toggle lowers configured take-home, the
        forecast uses min(observed median, scenario biweekly) immediately."""
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     income_biweekly_saving=4000.0,
                     income_biweekly_not_saving=6000.0,
                     retirement_saving=True)
        add_bill(self.conn, "Paycheck Co", 5000.0, frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=10), income=True,
                 last_seen=TODAY - dt.timedelta(days=18))
        for i in range(3):   # observed deposits still at the higher amount
            add_txn(self.conn, TODAY - dt.timedelta(days=4 + 14 * i), -6000.0,
                    "PAYCHECK CO PAYROLL", account="chk", primary="INCOME")
        f = forecast.build(self.conn, today=TODAY, days=30)
        curve = dict(f["plan"]["series"])
        d9, d10 = (str(TODAY + dt.timedelta(days=n)) for n in (9, 10))
        self.assertAlmostEqual(curve[d10] - curve[d9], 4000.0, places=2)
        # toggle off → the cap is the higher scenario; median rules again
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     income_biweekly_saving=4000.0,
                     income_biweekly_not_saving=6000.0,
                     retirement_saving=False)
        f = forecast.build(self.conn, today=TODAY, days=30)
        curve = dict(f["plan"]["series"])
        self.assertAlmostEqual(curve[d10] - curve[d9], 6000.0, places=2)

    def test_statement_scenario_splits_payment(self):
        """Autopay scenario: statement balance on the due date, remainder on
        the next cycle ~1 month later; the full-balance scenario unchanged.
        Conservation: both scenarios retire the same total debt."""
        due = TODAY + dt.timedelta(days=5)
        self.conn.execute("UPDATE accounts SET balance_current=1000 WHERE id='card'")
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"last_statement_balance": 300.0,
                    "next_payment_due_date": due.isoformat()}),))
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual(f["card_events"], [(due.isoformat(), -1000.0,
                                             "Test Card payment")])
        stmt = {d: a for d, a, _ in f["card_events_stmt"]}
        self.assertEqual(stmt[due.isoformat()], -300.0)
        ny, nm = (due.year, due.month + 1) if due.month < 12 else (due.year + 1, 1)
        next_due = due.replace(year=ny, month=nm)
        self.assertEqual(stmt[next_due.isoformat()], -700.0)
        self.assertEqual(sum(stmt.values()), -1000.0)   # conservation
        bal_due = dict(f["pace"]["series"])[due.isoformat()]
        bal_stmt = dict(f["pace_stmt"]["series"])[due.isoformat()]
        self.assertAlmostEqual(bal_stmt - bal_due, 700.0, places=2)
        self.assertAlmostEqual(f["pace_stmt"]["end"], f["pace"]["end"], places=2)

    def test_autopay_marks_carry_only_real_due_dates(self):
        """The cash chart draws a line per dated autopay. A card whose issuer
        never gave a due date is still SCHEDULED (tomorrow, conservatively),
        but it must not become a dated line: a mark labelled "autopay" on a
        day nothing is known to happen is a guess wearing a fact's clothes."""
        due = TODAY + dt.timedelta(days=5)
        self.conn.execute("UPDATE accounts SET balance_current=1000 WHERE id='card'")
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"last_statement_balance": 300.0,
                    "next_payment_due_date": due.isoformat()}),))
        f = forecast.build(self.conn, today=TODAY, days=60)
        marks = {d: (a, nm) for d, a, nm in f["card_autopay"]}
        self.assertEqual(marks[due.isoformat()], (-300.0, "Test Card"))
        # the rolled remainder is a real dated cycle too
        ny, nm_ = (due.year, due.month + 1) if due.month < 12 else (due.year + 1, 1)
        self.assertEqual(marks[due.replace(year=ny, month=nm_).isoformat()][0], -700.0)
        # the name is the card, not the event sentence — the clients compose
        # the label so several cards on one day can share a line
        self.assertNotIn("autopay", marks[due.isoformat()][1])

    def test_a_card_without_a_due_date_is_scheduled_but_not_marked(self):
        """The undated card still gets its conservative tomorrow payment in
        the walk; it just never reaches the chart as a dated line."""
        self.conn.execute("UPDATE accounts SET balance_current=1000 WHERE id='card'")
        f = forecast.build(self.conn, today=TODAY, days=60)
        tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
        self.assertEqual([d for d, _, _ in f["card_events"]], [tomorrow])
        self.assertEqual(f["card_autopay"], [])

    def test_a_due_date_already_past_is_not_marked(self):
        """A stale liabilities row (due date in the past) is the same case:
        the walk pulls it to tomorrow, the chart says nothing."""
        self.conn.execute("UPDATE accounts SET balance_current=1000 WHERE id='card'")
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"last_statement_balance": 300.0,
                    "next_payment_due_date":
                        (TODAY - dt.timedelta(days=3)).isoformat()}),))
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual(f["card_autopay"], [])

    def test_statement_missing_falls_back_to_full(self):
        """No liabilities data → the autopay scenario equals the full-balance
        one (conservative, never optimistic)."""
        f = forecast.build(self.conn, today=TODAY, days=30)
        self.assertEqual([(d, a) for d, a, _ in f["card_events_stmt"]],
                         [(d, a) for d, a, _ in f["card_events"]])
        self.assertEqual(f["pace_stmt"]["series"], f["pace"]["series"])

    def test_checking_account_config_override(self):
        """The checking_account_id config beats the largest-balance
        fallback."""
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('chk2','it1','Second Checking','depository','checking',9000,9000)")
        f = forecast.build(self.conn, today=TODAY, days=10)
        self.assertEqual(f["checking"], 9000.0)      # fallback: biggest balance
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     checking_account_id="chk")
        f = forecast.build(self.conn, today=TODAY, days=10)
        self.assertEqual(f["checking"], 5000.0)      # explicit config wins


if __name__ == "__main__":
    unittest.main()
