"""A rule on two days of the month reaches clients that know one day.

The edit forms, the wizard's Adjust and the ledger fit all spoke "MONTHLY:1"
and one date, so any save through them could silently rewrite [15, -1] to a
single day: one paycheck a month gone from the forecast, under a row that
still read "monthly". Two invariants close that: a save that did not name
new days never rewrites the rule's days, and the stored due date always sits
on the rule. A date a person types off the rule is refused, because nothing
can store one moved occurrence. Every surface says "twice a month".
"""
import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import activity, bills as rd, continuity
from oikonome.engine.compat import as_dict, jsonb
from oikonome.web import api  # noqa: F401  (routes the app serves)
from oikonome.web.pages import _cadence_label

from .test_twice_a_month_pay_stays_twice_a_month import (_PayrollFixture,
                                                         fifteenth_and_last)
from .util import _ensure_db, add_bill, seed_accounts, write_config

NAME = "Northwind Logistics"


class TheEngineNeverCollapsesTheRule(_PayrollFixture):
    def setUp(self):
        super().setUp()
        add_bill(self.conn, NAME, 2000.0, income=True,
                 next_due=dt.date(2026, 11, 15), by_month_day=[15, -1],
                 last_seen=dt.date(2026, 10, 31))

    def _save(self, **kw):
        rd.save_bill(self.conn, **{**dict(
            payee=NAME, amount=2000.0, frequency="MONTHLY", interval=1,
            income=True, source="detected"), **kw})

    def test_a_date_off_the_rule_keeps_both_days_and_lands_on_the_next_payday(self):
        self._save(next_due="2026-12-14")
        self.assertEqual(self.rule()["byMonthDay"], [15, -1])
        self.assertEqual(self.bill()["due_on"], dt.date(2026, 12, 15))
        self.assertEqual(as_dict(self.bill()["raw"])["dueOn"], "2026-12-15")
        self.assertEqual(float(self.bill()["monthly_amount"]), 4000.0)

    def test_a_single_day_off_the_rule_does_not_replace_it(self):
        self._save(by_month_day=31, next_due="2026-11-30")
        self.assertEqual(self.rule()["byMonthDay"], [15, -1])

    def test_a_list_of_days_is_the_explicit_change(self):
        self._save(by_month_day=[20], next_due="2026-11-20")
        self.assertEqual(self.rule()["byMonthDay"], [20])
        self.assertEqual(float(self.bill()["monthly_amount"]), 2000.0)

    def test_a_new_rule_with_no_date_starts_on_its_next_payday(self):
        self._save(payee="Second Payer", by_month_day=[1, 15], next_due=None)
        row = rd.get_bill(self.conn, "Second Payer")
        self.assertIsNotNone(row["due_on"])
        self.assertIn(row["due_on"].day, (1, 15))


class ApprovingTwiceAMonthOverALaterDate(_PayrollFixture):
    def _offer_over_a_drifted_date(self):
        add_bill(self.conn, NAME, 2000.0, frequency="WEEKLY", interval=2,
                 income=True, next_due=dt.date(2026, 11, 17),
                 last_seen=dt.date(2026, 10, 31))
        self.deposit(fifteenth_and_last(), amount=2000.0)
        rd.run(self.conn, today=dt.date(2026, 11, 5))
        return self.conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='frequency_change'").fetchone()

    def test_the_due_date_lands_on_the_rule_and_the_next_edit_keeps_it(self):
        offer = self._offer_over_a_drifted_date()
        self.assertEqual(as_dict(offer["evidence"])["by_month_day"], [15, -1])
        got = rd.apply_proposal(self.conn, offer["id"], "approve")
        self.assertNotIn("error", got)
        self.assertEqual(self.bill()["due_on"], dt.date(2026, 11, 30))
        # the Accept-amount button: no date, the same cadence
        rd.save_bill(self.conn, payee=NAME, amount=2100.0, frequency="MONTHLY",
                     interval=1, income=True, source="detected")
        self.assertEqual(self.rule()["byMonthDay"], [15, -1])
        self.assertEqual(float(self.bill()["monthly_amount"]), 4200.0)


class TheFitCarriesItsDays(_PayrollFixture):
    def _events(self):
        return [{"date": d, "amount": 2000.0, "txn_id": str(i)}
                for i, d in enumerate(fifteenth_and_last(n=6))]

    def test_the_fit_names_both_days_and_says_twice_a_month(self):
        fit = rd.ledger_fit(self._events(), dt.date(2026, 7, 2))
        self.assertEqual(fit["by_month_day"], [15, -1])
        self.assertEqual(fit["cadence"], "MONTHLY:1")
        self.assertIn("twice a month", fit["label"])
        self.assertNotIn("every 15 days", fit["label"])

    def test_a_dismissed_monthly_hint_does_not_hide_a_twice_a_month_fit(self):
        fit = rd.ledger_fit(self._events(), dt.date(2026, 7, 2))
        once = {"kind": "occurrence", "cadence": "MONTHLY:1",
                "amount": fit["amount"]}
        self.assertFalse(rd.fit_matches_dismissal(fit, once))
        self.assertTrue(rd.fit_matches_dismissal(
            fit, dict(once, by_month_day=[15, -1])))


class ASingleDayPayrollIsOfferedBack(_PayrollFixture):
    def test_deposits_on_two_days_offer_the_real_shape(self):
        add_bill(self.conn, NAME, 2000.0, income=True,
                 next_due=dt.date(2026, 11, 15), by_month_day=[15],
                 last_seen=dt.date(2026, 10, 31))
        self.deposit(fifteenth_and_last(), amount=2000.0)
        rd.run(self.conn, today=dt.date(2026, 11, 5))
        offer = self.conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='frequency_change'").fetchone()
        self.assertIsNotNone(offer)
        self.assertEqual(as_dict(offer["evidence"])["by_month_day"], [15, -1])

    def test_a_true_monthly_payroll_gets_no_offer(self):
        add_bill(self.conn, NAME, 2000.0, income=True,
                 next_due=dt.date(2026, 11, 15), by_month_day=[15],
                 last_seen=dt.date(2026, 10, 15))
        self.deposit([dt.date(2026, m, 15) for m in range(1, 11)],
                     amount=2000.0)
        rd.run(self.conn, today=dt.date(2026, 11, 5))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM bill_proposals WHERE status='pending' "
            "AND kind='frequency_change'").fetchone())


class EveryLabelSaysTwiceAMonth(_PayrollFixture):
    WORDS = "twice a month (15th and last day)"

    def test_the_cadence_label(self):
        self.assertEqual(_cadence_label("MONTHLY", 1, [15, -1]), self.WORDS)
        self.assertEqual(_cadence_label("MONTHLY", 1, [1, 15]),
                         "twice a month (1st and 15th)")
        self.assertEqual(_cadence_label("MONTHLY", 1, [15]), "monthly")
        self.assertEqual(_cadence_label("MONTHLY", 1), "monthly")
        self.assertEqual(_cadence_label("WEEKLY", 2, [15, -1]), "every 2 weeks")

    def test_the_activity_log_tells_the_change(self):
        before = {"amount": 1, "frequency": "MONTHLY", "interval": 1}
        after = dict(before, by_month_day=[15, -1])
        self.assertIn(f"cadence monthly → {self.WORDS}",
                      activity._bill_changes({"before": before, "after": after}))
        self.assertIn(self.WORDS, activity._bill_facts({"after": after}))
        add_bill(self.conn, NAME, 2000.0, income=True,
                 next_due=dt.date(2026, 11, 15), by_month_day=[15, -1])
        self.assertEqual(
            activity.bill_snapshot(self.bill())["by_month_day"], [15, -1])

    def test_the_continuity_packet(self):
        add_bill(self.conn, NAME, 2000.0, income=True,
                 next_due=dt.date(2026, 11, 15), by_month_day=[15, -1])
        packet = continuity.gather(self.conn, members=[],
                                   today=dt.date(2026, 11, 5))
        self.assertEqual(packet["income"][0]["cadence"], self.WORDS)

    def test_the_offer_helper_reads_the_evidence(self):
        self.assertEqual(rd.offer_days({"evidence": {"by_month_day": [15, -1]}}),
                         [15, -1])
        self.assertIsNone(rd.offer_days({"evidence": {"by_month_day": 15}}))
        self.assertIsNone(rd.offer_days({"evidence": {}}))


class TheDoors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        self.client.post("/api/signup", data={
            "email": f"twice-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = self.client.get("/api/me").json()["tenant_id"]
        self.conn = tenancy.tenant_connect(tid)
        self.addCleanup(self.conn.close)
        seed_accounts(self.conn)
        write_config(self.conn)
        self.today = dt.date.today()
        self.due = dt.date(self.today.year + 1, 3, 15)
        add_bill(self.conn, NAME, 2000.0, income=True, next_due=self.due,
                 by_month_day=[15, -1])

    def _bill(self, name=NAME):
        return rd.get_bill(self.conn, name)

    def _rule(self, name=NAME):
        return as_dict(self._bill(name)["raw"])["recurrence"]

    def _save(self, **kw):
        return self.client.post("/api/bills/save", json={
            **dict(payee=NAME, orig_payee=NAME, amount=2000, income=True,
                   cadence="MONTHLY:1"), **kw})

    def _offer(self, *, kind="add", payee="Harbor Payroll"):
        pid = f"prop:{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """INSERT INTO bill_proposals
                 (id, kind, bill_type, payee, amount, frequency, interval,
                  next_due, evidence, status, created_at)
               VALUES (%s,%s,'occurrence',%s,3000,'MONTHLY',1,%s,%s,'pending',now())""",
            (pid, kind, payee, self.due,
             jsonb({"by_month_day": [15, -1], "income": True})))
        return pid

    def test_a_typed_date_off_the_rule_is_refused_and_nothing_moves(self):
        r = self._save(next_due=(self.due - dt.timedelta(days=1)).isoformat(),
                       payee="Renamed Payer")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("15th and last day", r.json()["detail"])
        self.assertIsNone(self._bill("Renamed Payer"))
        self.assertEqual(self._rule()["byMonthDay"], [15, -1])
        self.assertEqual(self._bill()["due_on"], self.due)

    def test_a_typed_date_on_the_rule_moves_the_next_payday(self):
        end = dt.date(self.due.year, 3, 31)
        self.assertEqual(self._save(next_due=end.isoformat()).status_code, 200)
        self.assertEqual(self._rule()["byMonthDay"], [15, -1])
        self.assertEqual(self._bill()["due_on"], end)

    def test_the_stored_date_sent_back_is_not_a_move(self):
        # a stored date from before this rule held; the form echoes it
        self.conn.execute(
            "UPDATE bills SET due_on=%s, raw = raw || %s::jsonb WHERE payee=%s",
            (self.due + dt.timedelta(days=2),
             jsonb({"dueOn": (self.due + dt.timedelta(days=2)).isoformat()}),
             NAME))
        r = self._save(next_due=(self.due + dt.timedelta(days=2)).isoformat())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._bill()["due_on"], dt.date(self.due.year, 3, 31))

    def test_an_edit_with_no_days_keeps_the_rule(self):
        self.assertEqual(self._save(amount=2100).status_code, 200)
        self.assertEqual(self._rule()["byMonthDay"], [15, -1])
        self.assertEqual(float(self._bill()["monthly_amount"]), 4200.0)

    def test_picking_monthly_names_the_one_day(self):
        day = dt.date(self.due.year, 3, 20)
        r = self._save(next_due=day.isoformat(), by_month_day=[20])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._rule()["byMonthDay"], [20])
        self.assertEqual(float(self._bill()["monthly_amount"]), 2000.0)

    def test_add_as_bill_carries_the_fits_days(self):
        r = self._save(payee="Harbor Payroll", orig_payee=None,
                       by_month_day=[1, 15],
                       next_due=dt.date(self.due.year, 3, 1).isoformat())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._rule("Harbor Payroll")["byMonthDay"], [1, 15])
        self.assertEqual(
            float(self._bill("Harbor Payroll")["monthly_amount"]), 4000.0)

    def test_bad_days_are_refused(self):
        for bad in ("15", [0], [32], [-2], [True], [1, 2, 3], [], ["x"], [1.5]):
            self.assertEqual(self._save(by_month_day=bad).status_code, 400, bad)
        self.assertEqual(self._save(by_month_day=[1, 15],
                                    cadence="WEEKLY:2").status_code, 400)
        self.assertEqual(self._rule()["byMonthDay"], [15, -1])

    def test_rows_and_offers_carry_the_days_and_the_words(self):
        pid = self._offer()
        d = self.client.get("/api/bills").json()
        row = next(b for b in d["bills"] if b["payee"] == NAME)
        self.assertEqual(row["by_month_day"], [15, -1])
        self.assertEqual(row["cadence"], "twice a month (15th and last day)")
        self.assertEqual(row["cadence_value"], "MONTHLY:1")
        offer = next(p for p in d["proposals"] if p["id"] == pid)
        self.assertEqual(offer["by_month_day"], [15, -1])
        self.assertEqual(offer["cadence"], "twice a month (15th and last day)")
        one = self.client.get("/api/bills/bill", params={"payee": NAME}).json()
        self.assertEqual(one["by_month_day"], [15, -1])
        hist = self.client.get("/api/bills/history",
                               params={"payee": "Harbor Payroll"}).json()
        self.assertEqual(hist["proposals"][0]["by_month_day"], [15, -1])
        self.assertIn("twice a month", hist["proposals"][0]["cadence"])
        hist = self.client.get("/api/bills/history",
                               params={"payee": NAME}).json()
        self.assertEqual(hist["bill"]["by_month_day"], [15, -1])

    def test_a_plain_monthly_row_has_no_days(self):
        add_bill(self.conn, "Plain Rent", 900.0, next_due=self.due)
        d = self.client.get("/api/bills").json()
        row = next(b for b in d["bills"] if b["payee"] == "Plain Rent")
        self.assertIsNone(row["by_month_day"])
        self.assertEqual(row["cadence"], "monthly")

    def _approve(self, pid, adjust):
        return self.client.post("/api/bills/proposal", json={
            "pid": pid, "action": "approve", "adjust": adjust})

    def test_the_wizard_day_off_the_rule_is_refused_in_the_form(self):
        pid = self._offer()
        r = self._approve(pid, {"due_day": 14})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("15th and last day", r.json()["detail"])
        self.assertIsNone(self._bill("Harbor Payroll"))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM bill_proposals WHERE id=%s",
            (pid,)).fetchone()["status"], "pending")

    def test_the_wizard_last_day_keeps_the_rule(self):
        r = self._approve(self._offer(), {"due_day": 31})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._rule("Harbor Payroll")["byMonthDay"], [15, -1])
        self.assertEqual(self._bill("Harbor Payroll")["due_on"],
                         dt.date(self.due.year, 3, 31))

    def test_the_wizard_amount_keeps_the_rule(self):
        r = self._approve(self._offer(), {"amount": 3100})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._rule("Harbor Payroll")["byMonthDay"], [15, -1])
        self.assertEqual(
            float(self._bill("Harbor Payroll")["monthly_amount"]), 6200.0)

    def test_the_wizard_picking_monthly_names_the_day(self):
        r = self._approve(self._offer(), {"cadence": "MONTHLY:1",
                                          "by_month_day": [15]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._rule("Harbor Payroll")["byMonthDay"], [15])
        self.assertEqual(
            float(self._bill("Harbor Payroll")["monthly_amount"]), 3000.0)

    def test_the_mail_says_it_too(self):
        from oikonome.notify import mailact
        pid = self._offer()
        out = mailact.gather(self.conn, {})
        got = next(p for p in out["proposals"] if p["pid"] == pid)
        self.assertEqual(got["cadence"], "twice a month (15th and last day)")


if __name__ == "__main__":
    unittest.main()
