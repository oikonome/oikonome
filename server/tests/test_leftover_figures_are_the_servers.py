"""The last figures the clients worked out move to the server
(specs/shown-figures.md, "Smaller leftovers"): the money map's words
beside each row and its "Variable spending" row, the Month lens's left
and net, the spending line a narrowed month prints — spending only, by
the rule the day totals and the verdict use, a business's spending when
every listed row is a business's — and each day divider's whole-day
total, whatever part of the day the page has shown."""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import entities
from oikonome.web import data, lenses, todayview

from .util import (_admin_dsn, _ensure_db, add_txn, make_db, seed_accounts,
                   TEST_DB, write_config)


def _row(actual, expected, budget, *, judged=True, label="Food", pending=0.0,
         actual_label=None):
    return {"label": label, "judged": judged, "actual": actual,
            "expected": expected, "budget": budget, "pending": pending,
            "actual_label": actual_label}


class RowStatusTests(unittest.TestCase):
    def test_each_state_and_its_amount(self):
        self.assertEqual(todayview.row_status(_row(520, 400, 500)),
                         {"kind": "over", "amount": 20, "pending": 0.0, "vs_pace": None,
                          "text": "over by $20", "tone": "neg"})
        self.assertEqual(todayview.row_status(_row(450, 400, 500)),
                         {"kind": "ahead", "amount": 50, "pending": 0.0, "vs_pace": None,
                          "text": "$50 over pace so far", "tone": "neg"})
        self.assertEqual(todayview.row_status(_row(300, 400, 500)),
                         {"kind": "left", "amount": 200, "pending": 0.0, "vs_pace": None,
                          "text": "$200 left", "tone": "pos"})
        self.assertEqual(
            todayview.row_status(_row(900, 1000, 1500, judged=False,
                                      label="Bills", pending=200)),
            {"kind": "due", "amount": 400, "pending": 200, "vs_pace": None,
             "text": "$200 awaiting · $400 still due", "tone": "mut"})

    def test_rows_with_nothing_to_say(self):
        # under 50 cents past the budget is not over
        self.assertEqual(todayview.row_status(_row(500.4, 400, 500))["kind"],
                         "ahead")
        self.assertIsNone(todayview.row_status(_row(10, 0, 0)))
        self.assertIsNone(todayview.row_status(
            _row(0, 300, 300, judged=False, label="Savings / Investment",
                 actual_label="—")))
        self.assertIsNone(todayview.row_status(
            _row(1500, 1500, 1500, judged=False, label="Bills")))

    def test_the_email_words_come_from_the_same_status(self):
        self.assertEqual(todayview._row_delta(_row(520, 400, 500))["text"],
                         "over by $20")
        self.assertEqual(
            todayview._row_delta(_row(900, 1000, 1500, judged=False,
                                      label="Bills", pending=200))["text"],
            "$200 awaiting · $400 still due")

    def test_the_variable_row_is_the_sum_of_its_rows_with_a_status(self):
        t = todayview.plan_total([_row(300, 400, 500),
                                  _row(260, 200, 300, label="Pets"),
                                  _row(0, 0, 100, judged=False,
                                       label="Bills")])
        self.assertEqual((t["actual"], t["expected"], t["budget"]),
                         (560, 600, 800))
        self.assertEqual(t["status"], {"kind": "left", "amount": 240,
                                       "pending": 0.0, "vs_pace": None,
                                       "text": "$240 left",
                                       "tone": "pos"})


class MonthLensFiguresTests(unittest.TestCase):
    def test_left_and_net_are_sent(self):
        conn = make_db()
        try:
            write_config(conn)
            seed_accounts(conn)
            add_txn(conn, dt.date(2026, 3, 5), 120.0, "GROCER",
                    primary="FOOD_AND_DRINK", account="chk")
            d = lenses.month_summary(conn, 2026, 3,
                                     today=dt.date(2026, 4, 2))
            self.assertAlmostEqual(d["remaining"],
                                   d["variable_budget"] - d["variable_actual"],
                                   places=2)
            if d["income"]["actual"] is None:
                self.assertIsNone(d["net"])
            else:
                self.assertAlmostEqual(
                    d["net"], d["income"]["actual"] - d["spend_total"],
                    places=2)
        finally:
            conn.close()


class ListedSpendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod  # noqa: F811
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"listed-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_txn(conn, dt.date(2026, 7, 3), 120.0, "GROCER",
                    primary="FOOD_AND_DRINK", account="card")
            add_txn(conn, dt.date(2026, 7, 9), 500.0, "CARD PAYMENT",
                    primary="LOAN_PAYMENTS", account="chk",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
            add_txn(conn, dt.date(2026, 7, 15), -2000.0, "EMPLOYER",
                    primary="INCOME", account="chk")
        finally:
            conn.close()

    def test_the_listed_rows_spend_counts_spending_only(self):
        # $120 of groceries and a $500 card payment: a plain sum of the
        # amount column read $620 (and the paycheck took it below zero);
        # the listed rows SPENT $120
        page = self.client.get("/api/transactions?y=2026&m=7").json()
        self.assertEqual(page["mode"], "month")
        self.assertEqual(page["listed_spend"], 120.0)
        self.assertEqual(page["listed_spend"],
                         round(sum(r["spend_amount"] for r in page["rows"]),
                               2))



def _payload(conn, **kw):
    """The ledger page's reply, as GET /api/transactions builds it (the
    app is imported first: the route modules load through it)."""
    import oikonome.web.app  # noqa: F401
    from oikonome.web import api_ledger
    return api_ledger._transactions_payload(conn, **kw)


class WholeDaySpendTests(unittest.TestCase):
    """A day divider prints the server's figure for the WHOLE day: a day
    of 12 rows worth $300 reads $300 when only 8 are on screen."""

    DAY = dt.date(2026, 6, 13)

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"wd-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # twelve purchases of $25: $300 spent
            for i in range(12):
                add_txn(conn, self.DAY, 25.0,
                        f"CAFE WILLOWMERE {i}", primary="FOOD_AND_DRINK",
                        account="card")
            # the same day's card payment and paycheck spend nothing
            add_txn(conn, self.DAY, 900.0, "CARD PAYMENT",
                    primary="LOAN_PAYMENTS", account="chk",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
            add_txn(conn, self.DAY, -1500.0, "EMPLOYER",
                    primary="INCOME", account="chk")
            add_txn(conn, self.DAY - dt.timedelta(days=1), 15.0,
                    "CAFE WILLOWMERE EVE", primary="FOOD_AND_DRINK",
                    account="card")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.admin.close()

    def test_a_month_sends_each_days_whole_spend(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            page = _payload(conn, y=2026, m=6)
        finally:
            conn.close()
        key = self.DAY.isoformat()
        self.assertEqual(page["day_spend"][key], 300.0)
        self.assertEqual(page["day_spend"][(self.DAY - dt.timedelta(days=1))
                                           .isoformat()], 15.0)
        # the day's figure is its rows' spend_amount, every row of the day
        self.assertEqual(page["day_spend"][key], round(sum(
            r["spend_amount"] for r in page["rows"]
            if r["date"][:10] == key), 2))

    def test_a_page_cut_inside_a_day_still_reads_the_whole_day(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            first = _payload(
                conn, q="cafe willowmere", per_page=8)
            second = _payload(
                conn, q="cafe willowmere", per_page=8, page=2)
        finally:
            conn.close()
        key = self.DAY.isoformat()
        self.assertEqual(len(first["rows"]), 8)
        self.assertTrue(all(r["date"][:10] == key for r in first["rows"]))
        # eight rows on screen add to $200; the divider reads $300
        self.assertEqual(first["day_spend"], {key: 300.0})
        self.assertEqual(second["day_spend"][key], 300.0)
        self.assertEqual(round(sum(r["spend_amount"] for r in
                                   first["rows"] + second["rows"]
                                   if r["date"][:10] == key), 2), 300.0)
        # only the page's days are named
        self.assertEqual(set(second["day_spend"]),
                         {r["date"][:10] for r in second["rows"]})


class BusinessListedSpendTests(unittest.TestCase):
    """A month narrowed to a business's rows counts their business
    spending ("$85 spent (business)"); a mixed list keeps the household
    rule, and the combined toggle makes every row household money."""

    DAY = dt.date(2026, 5, 20)

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"bl-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES ('bizchk','it1','Biz Checking',"
                "'depository','checking',4000)")
            add_txn(conn, self.DAY, 40.0, "HOME LUNCH", account="card",
                    primary="FOOD_AND_DRINK")
            add_txn(conn, self.DAY, 60.0, "BIZ HOSTING", account="bizchk")
            add_txn(conn, self.DAY, 25.0, "BIZ POSTAGE", account="bizchk")
            # a business transfer and its income spend nothing
            add_txn(conn, self.DAY, 300.0, "BIZ TO SAVINGS",
                    account="bizchk", primary="TRANSFER_OUT")
            add_txn(conn, self.DAY, -700.0, "CLIENT PAYMENT",
                    account="bizchk", primary="INCOME")
            ent = entities.create_entity(conn, name="Fernhollow LLC",
                                         structure="sole_prop")
            entities.assign_account(conn, "bizchk", ent["id"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.admin.close()

    def _page(self, **kw):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return _payload(conn, y=2026, m=5, **kw)
        finally:
            conn.close()

    def test_business_rows_count_their_business_spending(self):
        page = self._page(scope="business")
        self.assertEqual(len(page["rows"]), 4)
        self.assertEqual(page["listed_scope"], "business")
        self.assertEqual(page["listed_spend"], 85.0)
        # the household figures do not move: those rows spent no household
        # money, so the day divider still reads none of it
        self.assertEqual(page["day_spend"], {self.DAY.isoformat(): 0.0})

    def test_a_mixed_list_keeps_the_household_rule(self):
        page = self._page()
        self.assertEqual(page["listed_scope"], "household")
        self.assertEqual(page["listed_spend"], 40.0)
        self.assertEqual(self._page(scope="personal")["listed_spend"], 40.0)

    def test_the_combined_toggle_counts_household_money(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, combine_entities=True)
            conn.commit()
        finally:
            conn.close()
        page = self._page(scope="business")
        self.assertEqual(page["listed_scope"], "household")
        self.assertEqual(page["listed_spend"], 85.0)

    def test_an_empty_list_is_the_households(self):
        self.assertEqual(data.listed_spend(None, []),
                         {"amount": 0.0, "scope": "household"})


if __name__ == "__main__":
    unittest.main()
