"""Four figures and lists both clients used to work out themselves now
come from the server, which says them once:

- the Budget page's summary line for a plan with no income
  (budget.plan_legs; specs/honest-figures.md, "A plan with no income still
  prints its line");
- the reimbursement matcher's room on an awaiting charge before the
  candidates door answers (data.reimb_left; specs/shown-figures.md, "The
  matcher counts against the server's room");
- the Income tab's sentence for a payer who stopped paying
  (reporting.dropped_words; specs/shown-figures.md, "Income tab: a payer
  who stopped");
- the people a W-2 can be filed under (taxdocs.w2_people;
  specs/w2-wages.md, "Who a W-2 can belong to").
"""

import datetime as dt
import unittest

from oikonome.engine import budget, reporting, taxdocs
from oikonome.web import api, api_imports, api_reimburse, data  # noqa: F401

from .util import add_txn, make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


class PlanLegsTests(unittest.TestCase):
    def test_a_plan_with_no_income_prints_bills_to_spend_and_saved(self):
        self.assertEqual(budget.plan_legs(None, 1234.6, 800.4, 250.5, None),
                         {"income": None, "bills": 1235, "to_spend": 800,
                          "saved": 250, "excess": None})

    def test_no_savings_leg_is_null(self):
        self.assertIsNone(budget.plan_legs(None, 900, 700, 0, None)["saved"])

    def test_a_plan_with_an_income_is_the_plan_lines_terms(self):
        legs = budget.plan_legs(5000.4, 1200.3, 2000.2, 300.0, 1499.9)
        pl = budget.plan_line(5000.4, 1200.3, 2000.2, 300.0, 1499.9)
        self.assertEqual(legs, {k: pl[k] for k in
                                ("income", "bills", "to_spend", "saved",
                                 "excess")})
        self.assertEqual(legs["income"] - legs["bills"] - legs["to_spend"]
                         - legs["saved"], legs["excess"])

    def test_the_month_without_income_sends_its_legs(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=600, other_monthly=400)
        st = budget.month_status(conn, dt.date.today())
        self.assertIsNone(st["plan_line"])
        self.assertEqual(st["plan_legs"]["to_spend"], 1000)
        self.assertIsNone(st["plan_legs"]["income"])
        self.assertIsNone(st["plan_legs"]["excess"])


class MatcherRoomTests(unittest.TestCase):
    def test_an_awaiting_charge_states_the_room_the_candidates_door_states(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        day = dt.date.today() - dt.timedelta(days=5)
        charge = add_txn(conn, day, 120.0, "CLINIC VISIT", primary="MEDICAL")
        dep = add_txn(conn, day + dt.timedelta(days=2), -45.0,
                      "CLAIM PAYMENT", account="chk", primary="INCOME")
        data.flag_reimbursement(conn, charge, partial=True)
        data.link_reimbursements(conn, charge, [dep], partial=True)
        conn.commit()
        out = api_reimburse.reimburse_pending(user=_user(conn))
        row = next(r for r in out["pending"] if r["id"] == charge)
        self.assertEqual(row["left_amount"], 75.0)
        cand = api_reimburse.reimburse_candidates(charge, user=_user(conn))
        self.assertEqual(row["left_amount"], cand["anchor"]["left_amount"])


class DroppedWordsTests(unittest.TestCase):
    def test_a_payer_who_stopped_is_named_with_what_it_paid_before(self):
        self.assertEqual(
            reporting.dropped_words([["Acme Payroll", 0.0, 0, 900.4, None]],
                                    "vs the year before"),
            "Nothing this time from Acme Payroll ($900), paid the year before.")

    def test_three_are_named_and_the_rest_counted(self):
        rows = [[f"Payer {c}", 0.0, 0, 830.0 - 90 * i, None]
                for i, c in enumerate("ABCDE")]
        self.assertEqual(
            reporting.dropped_words(rows, "vs Mar 1–9"),
            "Nothing this time from Payer A ($830), Payer B ($740), "
            "Payer C ($650) or 2 others, paid Mar 1–9.")

    def test_every_dropped_payer_is_counted_past_the_listed_ten(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        today = dt.date(2026, 6, 15)
        add_txn(conn, dt.date(2024, 1, 10), -380.0, "GADGETCO PAYROLL",
                account="chk", primary="INCOME")
        for i in range(13):
            add_txn(conn, dt.date(2025, 2, 3 + i), -(310.0 + 7 * i),
                    f"PAYER {chr(65 + i)} LLC", account="chk",
                    primary="INCOME")
        add_txn(conn, dt.date(2026, 3, 10), -380.0, "GADGETCO PAYROLL",
                account="chk", primary="INCOME")
        conn.commit()
        w = reporting.income_window(conn, today, "1y")
        self.assertEqual(len(w["dropped_sources"]), 10)
        self.assertIn(" or 10 others, paid ", w["dropped_text"])

    def test_a_payer_under_a_dollar_or_renamed_by_month_did_not_stop(self):
        # a payer that rounds to $0 is no change, and a bank's interest
        # line that names its month is one payer, paid in both windows
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        today = dt.date(2026, 6, 15)
        add_txn(conn, dt.date(2024, 1, 10), -380.0, "GADGETCO PAYROLL",
                account="chk", primary="INCOME")
        add_txn(conn, dt.date(2025, 2, 10), -0.3, "PAYER A LLC",
                account="chk", primary="INCOME")
        # exactly half a dollar prints as $0 too (halves round to even)
        add_txn(conn, dt.date(2025, 2, 12), -0.5, "PAYER B LLC",
                account="chk", primary="INCOME")
        add_txn(conn, dt.date(2025, 3, 31), -4.0,
                "MARCH SAVINGS INTEREST PAID", account="chk",
                primary="INCOME")
        add_txn(conn, dt.date(2026, 4, 30), -4.0,
                "APRIL SAVINGS INTEREST PAID", account="chk",
                primary="INCOME")
        add_txn(conn, dt.date(2026, 3, 10), -380.0, "GADGETCO PAYROLL",
                account="chk", primary="INCOME")
        conn.commit()
        w = reporting.income_window(conn, today, "1y")
        self.assertEqual(w["dropped_sources"], [])
        self.assertIsNone(w["dropped_text"])

    def test_nothing_dropped_or_no_previous_window_says_nothing(self):
        self.assertIsNone(reporting.dropped_words([], "vs the year before"))
        self.assertIsNone(reporting.dropped_words(
            [["Acme Payroll", 0.0, 0, 900.0, None]], None))

    def test_the_income_window_sends_the_sentence(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        today = dt.date(2026, 6, 15)
        # the ledger reaches back past the previous window, so it has one
        add_txn(conn, dt.date(2024, 1, 10), -380.0, "GADGETCO PAYROLL",
                account="chk", primary="INCOME")
        add_txn(conn, dt.date(2025, 2, 10), -700.0, "WIDGETCO PAYROLL",
                account="chk", primary="INCOME")
        add_txn(conn, dt.date(2026, 3, 10), -380.0, "GADGETCO PAYROLL",
                account="chk", primary="INCOME")
        conn.commit()
        w = reporting.income_window(conn, today, "1y")
        self.assertTrue(w["dropped_sources"])
        self.assertIn("($700)", w["dropped_text"])
        self.assertTrue(w["dropped_text"].startswith("Nothing this time from"))


class W2PeopleTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_both_people_are_offered_for_any_household(self):
        both = [{"owner": "primary", "label": "Primary earner"},
                {"owner": "spouse", "label": "Spouse"}]
        self.assertEqual(taxdocs.w2_people(self.conn), both)
        # a stored return's filing status narrows nothing
        self.conn.execute(
            "INSERT INTO income_annual (year, agi, filing_status, joint) "
            "VALUES (2024, 41000, 'single', 0)")
        self.assertEqual(taxdocs.w2_people(self.conn), both)

    def test_the_stored_list_sends_the_people(self):
        self.conn.commit()
        out = api_imports.taxdoc_stored_api(user=_user(self.conn))
        self.assertEqual([p["owner"] for p in out["w2_people"]],
                         ["primary", "spouse"])


if __name__ == "__main__":
    unittest.main()
