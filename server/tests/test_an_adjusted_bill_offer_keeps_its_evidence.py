"""Approving a bill offer with the person's changes keeps what it found.

`POST /api/bills/proposal` with `action: approve` may carry `adjust`
({name, amount, cadence, due_day}). The bill is created from the offer with
those values instead, checked the way the bill save checks them, and keeps
the merchants and history the offer was found from exactly as an approval
without changes keeps them. A change that cannot be applied (a name another
bill holds, a bad value) leaves the offer pending and no bill behind.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

import psycopg

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills as rd
from oikonome.engine.compat import as_dict
from oikonome.sync import base
from oikonome.web import api

from .util import (TODAY, _ensure_db, add_bill, add_txn, seed_accounts,
                   write_config)


class AdjustedApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def _household(self):
        """A fresh household with one pending offer: a monthly charge."""
        from oikonome.web import security
        security._limiter._hits.clear()
        client = TestClient(self.app)
        client.post("/api/signup", data={
            "email": f"adjust-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        self.addCleanup(conn.close)
        seed_accounts(conn)
        write_config(conn)
        # written the way a feed writes them, so the charge has a merchant
        # the offer's bill can take as its identity
        base.upsert_transactions(conn, [base.Transaction(
            id=f"feed-{i}", account_id="card",
            date=dt.date(TODAY.year, TODAY.month - 5 + i, 8), amount=9.99,
            name="TUNEBOX MUSIC 800-555", merchant_name="Tunebox Music")
            for i in range(5)])
        rd.run(conn, TODAY)
        adds = conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='add'").fetchall()
        self.assertEqual(len(adds), 1)
        return client, conn, adds[0]

    def _approve(self, client, pid, adjust=None, action="approve"):
        body = {"pid": pid, "action": action}
        if adjust is not None:
            body["adjust"] = adjust
        return client.post("/api/bills/proposal", json=body)

    @staticmethod
    def _status(conn, pid):
        return conn.execute("SELECT status FROM bill_proposals WHERE id=%s",
                            (pid,)).fetchone()["status"]

    def test_the_adjusted_bill_carries_the_changes_and_the_found_identity(self):
        # the same offer approved as it stands, in a second household: the
        # identity it ends with is the one the adjusted bill must keep
        plain_c, plain_conn, plain_p = self._household()
        r = self._approve(plain_c, plain_p["id"])
        self.assertEqual(r.status_code, 200, r.text)
        plain = rd.get_bill(plain_conn, plain_p["payee"])
        plain_raw = as_dict(plain["raw"])

        client, conn, p = self._household()
        r = self._approve(client, p["id"], {
            "name": "Tunebox", "amount": 10.99, "cadence": "MONTHLY:1",
            "due_day": 20})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "payee": "Tunebox"})
        self.assertIsNone(rd.get_bill(conn, p["payee"]))
        bill = rd.get_bill(conn, "Tunebox")
        raw = as_dict(bill["raw"])
        self.assertEqual(bill["amount"], -10.99)
        self.assertEqual(raw["recurrence"],
                         {"frequency": "MONTHLY", "interval": 1,
                          "byMonthDay": [20]})
        self.assertEqual(bill["due_on"].day, 20)
        self.assertGreaterEqual(bill["due_on"], dt.date.today())
        # what the offer found is attached exactly as without the changes
        self.assertEqual(bill["merchant"], plain["merchant"])
        self.assertEqual(plain_raw["merchant_names"], ["Tunebox Music"])
        self.assertEqual(raw["merchant_names"], plain_raw["merchant_names"])
        self.assertEqual([r["name"] for r in raw["merchant_refs"]],
                         [r["name"] for r in plain_raw["merchant_refs"]])
        self.assertTrue(all(r["id"] for r in raw["merchant_refs"]))
        self.assertEqual(raw.get("lastDueOn"), plain_raw.get("lastDueOn"))
        self.assertEqual(raw.get("source"), "detected")
        # the person set the amount, so drift leaves it alone for a while
        self.assertIn("manual_edited_at", raw)
        # and the offer is decided, its evidence untouched
        after = conn.execute("SELECT status, evidence FROM bill_proposals "
                             "WHERE id=%s", (p["id"],)).fetchone()
        self.assertEqual(after["status"], "approved")
        self.assertEqual(as_dict(after["evidence"]), as_dict(p["evidence"]))

    def test_a_rename_alone_is_not_a_hand_edit_of_the_amount(self):
        client, conn, p = self._household()
        r = self._approve(client, p["id"], {"name": "Tunebox"})
        self.assertEqual(r.status_code, 200, r.text)
        bill = rd.get_bill(conn, "Tunebox")
        self.assertEqual(bill["amount"], -abs(p["amount"]))
        self.assertNotIn("manual_edited_at", as_dict(bill["raw"]))

    def test_blank_fields_are_left_alone(self):
        client, conn, p = self._household()
        r = self._approve(client, p["id"], {"name": "", "amount": None,
                                            "cadence": "", "due_day": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["payee"], p["payee"])
        self.assertIsNotNone(rd.get_bill(conn, p["payee"]))

    def test_a_taken_name_leaves_the_offer_pending(self):
        client, conn, p = self._household()
        add_bill(conn, "Northwind Power", 120.0, source="manual")
        r = self._approve(client, p["id"], {"name": "Northwind Power"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._status(conn, p["id"]), "pending")
        self.assertIsNone(rd.get_bill(conn, p["payee"]))
        self.assertEqual(
            rd.get_bill(conn, "Northwind Power")["amount"], -120.0)

    def test_values_the_bill_save_refuses_are_refused_first(self):
        client, conn, p = self._household()
        for adjust in ({"amount": -5}, {"amount": 0}, {"amount": "lots"},
                       {"amount": True},
                       {"cadence": "HOURLY:1"}, {"cadence": "MONTHLY:0"},
                       {"cadence": "YEARLY:40"}, {"due_day": 0},
                       {"due_day": 32}, {"due_day": "5"}, {"name": 7},
                       "monthly"):
            r = self._approve(client, p["id"], adjust)
            self.assertEqual(r.status_code, 400, (adjust, r.text))
            self.assertEqual(self._status(conn, p["id"]), "pending", adjust)
        self.assertIsNone(rd.get_bill(conn, p["payee"]))

    def test_a_dismissal_ignores_adjust(self):
        client, conn, p = self._household()
        r = self._approve(client, p["id"], {"cadence": "HOURLY:1"},
                          action="reject")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._status(conn, p["id"]), "rejected")

    def test_only_an_offer_to_add_can_be_adjusted(self):
        client, conn, p = self._household()
        conn.execute("UPDATE bill_proposals SET kind='remove' WHERE id=%s",
                     (p["id"],))
        r = self._approve(client, p["id"], {"amount": 12})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._status(conn, p["id"]), "pending")

    def test_a_yearly_due_day_keeps_the_month_it_is_due_in(self):
        client, conn, p = self._household()
        r = self._approve(client, p["id"], {"cadence": "YEARLY:1",
                                            "due_day": 3})
        self.assertEqual(r.status_code, 200, r.text)
        bill = rd.get_bill(conn, p["payee"])
        self.assertEqual(bill["due_on"].day, 3)
        self.assertEqual(as_dict(bill["raw"])["recurrence"]["frequency"],
                         "YEARLY")

    def _bill(self, conn, payee):
        bill = rd.get_bill(conn, payee)
        return bill, as_dict(bill["raw"])

    def test_an_offer_adjusted_to_an_envelope_has_no_due_date(self):
        client, conn, p = self._household()
        r = self._approve(client, p["id"], {"cadence": "ENVELOPE:1",
                                            "due_day": 12})
        self.assertEqual(r.status_code, 200, r.text)
        bill, raw = self._bill(conn, p["payee"])
        self.assertIsNone(bill["due_on"])
        self.assertIsNone(raw["dueOn"])
        self.assertEqual(raw["bill_type"], "envelope")
        self.assertEqual(raw["recurrence"], {})

    def test_a_weekly_offer_keeps_its_due_date_when_a_day_is_sent(self):
        client, conn, p = self._household()
        r = self._approve(client, p["id"], {"cadence": "WEEKLY:1",
                                            "due_day": 28})
        self.assertEqual(r.status_code, 200, r.text)
        bill, raw = self._bill(conn, p["payee"])
        self.assertEqual(bill["due_on"], p["next_due"])
        self.assertNotIn("byMonthDay", raw["recurrence"])

    def test_a_new_name_another_bill_already_covers_is_refused(self):
        # not its exact name: the new name leads with the token another
        # active bill is keyed on, or one of its merchant's tokens, so the
        # two would claim the same charges
        for other, merchant, new in (
                ("Northwind Power", None, "Northwind Power Co"),
                ("House electric", "northwind energy", "Northwind")):
            client, conn, p = self._household()
            rd.save_bill(conn, payee=other, amount=120.0, frequency="MONTHLY",
                         merchant=merchant, source="manual")
            r = self._approve(client, p["id"], {"name": new,
                                                "amount": 11.0})
            self.assertEqual(r.status_code, 409, (new, r.text))
            self.assertIn(other, r.json()["detail"])
            self.assertEqual(self._status(conn, p["id"]), "pending")
            self.assertIsNone(rd.get_bill(conn, p["payee"]))
            self.assertIsNone(rd.get_bill(conn, new))

    def test_a_new_name_an_archived_bill_holds_is_refused_as_a_rename_is(self):
        # the Bills page's own rename refuses a name, or a spelling of it,
        # an archived bill still holds; the archived bill is left as it was
        for new in ("Northwind Power", "northwind power"):
            client, conn, p = self._household()
            add_bill(conn, "Northwind Power", 120.0, source="manual")
            rd.archive_bill(conn, "Northwind Power")
            r = self._approve(client, p["id"], {"name": new})
            self.assertEqual(r.status_code, 409, (new, r.text))
            self.assertEqual(self._status(conn, p["id"]), "pending")
            self.assertIsNone(rd.get_bill(conn, p["payee"]))
            kept = rd.get_bill(conn, "Northwind Power")
            self.assertEqual((kept["active"], kept["amount"]), (0, -120.0))

    def test_a_save_that_loses_a_race_for_the_name_leaves_the_offer_pending(self):
        client, conn, p = self._household()
        real = rd.save_bill

        def racing(c, **kw):
            if kw.get("payee") == "Tunebox":
                raise psycopg.errors.UniqueViolation("duplicate key")
            return real(c, **kw)
        with mock.patch.object(rd, "save_bill", side_effect=racing):
            r = self._approve(client, p["id"], {"name": "Tunebox",
                                                "amount": 10.99})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._status(conn, p["id"]), "pending")
        self.assertIsNone(rd.get_bill(conn, p["payee"]))
        self.assertIsNone(rd.get_bill(conn, "Tunebox"))

    def test_an_offer_already_tracked_is_withdrawn_and_nothing_is_adjusted(self):
        # the person added the bill by hand while the offer sat on screen:
        # approving it with changes withdraws it, inside the same
        # transaction the changes would have run in, and tells the client
        # so — no second bill, no rename, the hand-added one untouched
        client, conn, p = self._household()
        add_bill(conn, "Tunebox", 12.49, source="manual")
        r = self._approve(client, p["id"], {"name": "Tunebox Family",
                                            "amount": 15.99})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json(), {
            "detail": "'Tunebox' is already tracked — this offer was "
                      "withdrawn",
            "superseded": True})
        self.assertEqual(self._status(conn, p["id"]), "superseded")
        self.assertIsNone(rd.get_bill(conn, "Tunebox Family"))
        self.assertIsNone(rd.get_bill(conn, p["payee"]))
        kept = rd.get_bill(conn, "Tunebox")
        self.assertEqual((kept["active"], kept["amount"]), (1, -12.49))
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) AS n FROM bills").fetchone()["n"], 1)

    def _income_offer(self):
        """A household whose ledger also holds a monthly paycheck, and the
        offer to add it as income."""
        client, conn, _ = self._household()
        for m in range(2, 8):
            add_txn(conn, dt.date(TODAY.year, m, 1), -2500.0,
                    "ACME WIDGETS PAYROLL", account="chk", primary="INCOME")
        rd.run(conn, TODAY)
        offer = next(
            r for r in conn.execute(
                "SELECT * FROM bill_proposals WHERE status='pending' "
                "AND kind='add'").fetchall()
            if as_dict(r["evidence"]).get("income"))
        return client, conn, offer

    def test_an_adjusted_income_offer_stays_income(self):
        # the sign and the income flag come from the offer, not from the
        # adjusted amount, which is always a positive dollar value: a raise
        # typed into a paycheck offer must not land as a bill to pay
        client, conn, offer = self._income_offer()
        r = self._approve(client, offer["id"], {"name": "Acme salary",
                                                "amount": 2650,
                                                "due_day": 3})
        self.assertEqual(r.status_code, 200, r.text)
        bill = rd.get_bill(conn, "Acme salary")
        self.assertEqual(bill["amount"], 2650.0)
        self.assertEqual(bill["due_on"].day, 3)
        self.assertIsNone(rd.get_bill(conn, offer["payee"]))
        # changing only the schedule keeps the offer's own amount, still in
        client2, conn2, offer2 = self._income_offer()
        r = self._approve(client2, offer2["id"], {"due_day": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(rd.get_bill(conn2, offer2["payee"])["amount"],
                         abs(offer2["amount"]))


class DueDayOnTheCadenceTests(unittest.TestCase):
    """A due day the person picks moves the day of the month the bill is
    due on, in the cycle it is already due in. It never pulls a quarterly
    or a two-yearly bill into the next month or the next year: the plan
    would count a charge that is not coming and the bill would read
    missed when it did not arrive."""

    TODAY = dt.date(2026, 10, 17)

    def on(self, day, freq, interval, due):
        return api._next_on_day(self.TODAY, day, freq, interval, due)

    def test_a_quarterly_bill_moves_its_day_inside_its_own_quarter(self):
        self.assertEqual(self.on(16, "MONTHLY", 3, dt.date(2027, 1, 15)),
                         dt.date(2027, 1, 16))

    def test_a_day_already_passed_steps_on_by_the_bills_interval(self):
        self.assertEqual(self.on(5, "MONTHLY", 1, dt.date(2026, 10, 20)),
                         dt.date(2026, 11, 5))
        self.assertEqual(self.on(5, "MONTHLY", 3, dt.date(2026, 10, 20)),
                         dt.date(2027, 1, 5))

    def test_a_short_month_takes_its_last_day(self):
        self.assertEqual(self.on(31, "MONTHLY", 1, dt.date(2026, 11, 3)),
                         dt.date(2026, 11, 30))

    def test_a_two_yearly_bill_keeps_its_year(self):
        self.assertEqual(self.on(3, "YEARLY", 2, dt.date(2027, 12, 10)),
                         dt.date(2027, 12, 3))
        self.assertEqual(self.on(5, "YEARLY", 2, dt.date(2026, 10, 20)),
                         dt.date(2028, 10, 5))

    def test_no_due_date_counts_from_this_month(self):
        self.assertEqual(self.on(20, "MONTHLY", 1, None),
                         dt.date(2026, 10, 20))
        self.assertEqual(self.on(2, "MONTHLY", 2, None),
                         dt.date(2026, 12, 2))

    def test_cadences_with_no_day_of_the_month_keep_their_date(self):
        due = dt.date(2026, 10, 22)
        for freq in ("WEEKLY", "DAILY", "ONE_TIME"):
            self.assertEqual(self.on(3, freq, 1, due), due, freq)
        self.assertIsNone(self.on(3, "ENVELOPE", 1, due))


if __name__ == "__main__":
    unittest.main()
