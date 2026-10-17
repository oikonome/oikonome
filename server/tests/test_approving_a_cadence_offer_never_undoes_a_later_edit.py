"""Approving a cadence offer (a new frequency, or a switch to an envelope)
must never undo what happened to the bill after the offer was made.

The offer is worked out from the bill as it stood that night: its amount
and its due date. Between the offer and the Approve the person may type a
different amount in, or the nightly may drift the amount and roll the due
date on. Approving must then withdraw the offer ("has changed since")
rather than write its stale amount over the person's, or set a due date
they typed back. A due date the nightly rolled along the bill's own
schedule is not such a change: the offer approves and keeps the later
date. A pinned price is the person's answer about
the AMOUNT only: the cadence is a different fact, so a pinned bill is still
offered a new cadence, and approving it changes the cadence and leaves the
pinned amount alone — the due-date check still applies. A switch to an
envelope clears the pin: it was on a per-charge price the pool no longer
has. A cadence the person picked by hand since withdraws the offer too,
with or without a moved due date. An offer that still
describes the bill approves as before, and the nightly offers a withdrawn
one again from the bill's current figures."""

import datetime as dt
import unittest

from oikonome.engine import bills
from oikonome.engine.compat import as_dict, jsonb

from .util import TODAY, add_bill, add_txn, make_db, write_config

NEXT = TODAY + dt.timedelta(days=10)
PID = "prop:cadence"


class ApprovingACadenceOffer(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Harbor Gym", 40.0, next_due=NEXT)

    def tearDown(self):
        self.conn.close()

    def _offer(self, kind="frequency_change", **ev):
        evidence = {"old_amount": 40.0, "old_due": NEXT.isoformat(), **ev}
        bills._insert_proposal(
            self.conn, pid=PID, kind=kind, payee="Harbor Gym", amount=45.0,
            bill_type="envelope" if kind == "type_change" else "occurrence",
            frequency=None if kind == "type_change" else "WEEKLY",
            interval=1, next_due=None if kind == "type_change" else NEXT,
            evidence=evidence)

    def _bill(self):
        return bills.get_bill(self.conn, "Harbor Gym")

    def _status(self):
        return self.conn.execute("SELECT status FROM bill_proposals WHERE id=%s",
                                 (PID,)).fetchone()["status"]

    def test_a_pinned_price_is_not_overwritten(self):
        # pinned at a price the offer was not made from: the pin owns the
        # amount, so only the cadence moves and the offer still approves
        for kind in ("frequency_change", "type_change"):
            with self.subTest(kind=kind):
                self.conn.execute("DELETE FROM bill_proposals")
                add_bill(self.conn, "Harbor Gym", 40.0, next_due=NEXT)
                self._offer(kind)
                add_bill(self.conn, "Harbor Gym", 52.0, next_due=NEXT,
                         source="manual")
                self.conn.execute(
                    "UPDATE bills SET raw = raw || %s::jsonb "
                    "WHERE payee='Harbor Gym'", (jsonb({"lock_amount": True}),))
                out = bills.apply_proposal(self.conn, PID, "approve")
                self.assertNotIn("error", out, out)
                b = self._bill()
                if kind == "frequency_change":
                    self.assertTrue(as_dict(b["raw"]).get("lock_amount"))
                    self.assertAlmostEqual(abs(b["amount"]), 52.0)
                    self.assertEqual(b["frequency"], "EVERY_WEEK")
                    self.assertEqual(b["due_on"], NEXT)
                else:
                    # a switch to an envelope changes what the amount
                    # means (one charge → the month's pool): the pinned
                    # per-charge price would be a cap the month overruns,
                    # so the offer's monthly figure is written, and the
                    # pin (on a price that no longer exists) is cleared
                    self.assertAlmostEqual(abs(b["amount"]), 45.0)
                    self.assertFalse(as_dict(b["raw"]).get("lock_amount"))
                    self.assertIn("pin", out.get("note", ""))
                    self.assertEqual(as_dict(b["raw"]).get("bill_type"),
                                     "envelope")
                self.assertEqual(self._status(), "approved")

    def test_a_pinned_per_charge_price_becomes_the_monthly_pool(self):
        # a $6 coffee pinned per charge, bought several times a month: the
        # envelope it becomes holds the month's spend, not one cup
        add_bill(self.conn, "Corner Coffee", 6.0, next_due=NEXT)
        self.conn.execute(
            "UPDATE bills SET raw = raw || %s::jsonb "
            "WHERE payee='Corner Coffee'", (jsonb({"lock_amount": True}),))
        bills._insert_proposal(
            self.conn, pid="prop:coffee", kind="type_change",
            payee="Corner Coffee", amount=54.0, bill_type="envelope",
            evidence={"old_amount": 6.0, "old_due": NEXT.isoformat()})
        out = bills.apply_proposal(self.conn, "prop:coffee", "approve")
        self.assertNotIn("error", out, out)
        b = bills.get_bill(self.conn, "Corner Coffee")
        self.assertEqual(as_dict(b["raw"]).get("bill_type"), "envelope")
        self.assertAlmostEqual(abs(b["amount"]), 54.0)
        # the machine's pool figure is not pinned: drift may correct it
        self.assertFalse(as_dict(b["raw"]).get("lock_amount"))

    def test_a_due_date_rolled_along_the_schedule_still_approves(self):
        # the offer outlived the nightly roll of the bill it is about; the
        # roll moved nothing the offer was worked out from
        self._offer()
        self.conn.execute(
            "UPDATE bills SET due_on=%s, raw = raw || %s::jsonb "
            "WHERE payee='Harbor Gym'",
            (TODAY - dt.timedelta(days=3),
             jsonb({"dueOn": (TODAY - dt.timedelta(days=3)).isoformat()})))
        bills._advance_due_dates(self.conn, TODAY, {"due_advanced": 0})
        self.conn.execute(
            "UPDATE bill_proposals SET evidence = evidence || %s::jsonb "
            "WHERE id=%s",
            (jsonb({"old_due": (TODAY - dt.timedelta(days=3)).isoformat()}),
             PID))
        rolled = self._bill()["due_on"]
        self.assertGreater(rolled, TODAY - dt.timedelta(days=3))
        out = bills.apply_proposal(self.conn, PID, "approve")
        self.assertNotIn("error", out, out)
        b = self._bill()
        self.assertEqual(b["frequency"], "EVERY_WEEK")
        self.assertGreaterEqual(b["due_on"], rolled)
        self.assertEqual(self._status(), "approved")

    def test_a_pinned_price_still_withdraws_on_a_hand_moved_due_date(self):
        self._offer()
        later = NEXT + dt.timedelta(days=33)    # off the monthly schedule
        self.conn.execute(
            "UPDATE bills SET due_on=%s, raw = raw || %s::jsonb "
            "WHERE payee='Harbor Gym'", (later, jsonb({"lock_amount": True})))
        out = bills.apply_proposal(self.conn, PID, "approve")
        self.assertIn("changed since", out.get("error", ""), out)
        self.assertEqual(self._bill()["due_on"], later)
        self.assertEqual(self._bill()["frequency"], "EVERY_MONTH")
        self.assertEqual(self._status(), "superseded")

    def test_an_amount_typed_in_since_withdraws_the_offer(self):
        self._offer()
        add_bill(self.conn, "Harbor Gym", 52.0, next_due=NEXT, source="manual")
        out = bills.apply_proposal(self.conn, PID, "approve")
        self.assertIn("changed since", out.get("error", ""), out)
        self.assertAlmostEqual(abs(self._bill()["amount"]), 52.0)
        self.assertEqual(self._status(), "superseded")

    def test_a_hand_moved_due_date_is_not_set_back(self):
        self._offer()
        later = NEXT + dt.timedelta(days=33)    # off the monthly schedule
        self.conn.execute("UPDATE bills SET due_on=%s WHERE payee='Harbor Gym'",
                          (later,))
        out = bills.apply_proposal(self.conn, PID, "approve")
        self.assertIn("error", out)
        self.assertEqual(self._bill()["due_on"], later)
        self.assertEqual(self._status(), "superseded")

    def test_an_offer_without_its_starting_figures_never_sets_the_date_back(self):
        # offers filed before the starting figures were recorded still
        # approve, but change the cadence alone and keep a later due date
        bills._insert_proposal(
            self.conn, pid=PID, kind="frequency_change", payee="Harbor Gym",
            amount=45.0, frequency="WEEKLY", interval=1,
            next_due=NEXT - dt.timedelta(days=20), evidence={})
        out = bills.apply_proposal(self.conn, PID, "approve")
        self.assertNotIn("error", out, out)
        b = self._bill()
        self.assertEqual(b["frequency"], "EVERY_WEEK")
        self.assertAlmostEqual(abs(b["amount"]), 40.0)
        self.assertEqual(b["due_on"], NEXT)

    def test_an_offer_that_still_describes_the_bill_approves(self):
        self._offer()
        out = bills.apply_proposal(self.conn, PID, "approve")
        self.assertNotIn("error", out, out)
        b = self._bill()
        self.assertEqual(b["frequency"], "EVERY_WEEK")
        self.assertAlmostEqual(abs(b["amount"]), 45.0)
        self.assertEqual(b["due_on"], NEXT)
        self.assertEqual(self._status(), "approved")

    def test_a_switch_to_an_envelope_that_still_fits_approves(self):
        self._offer("type_change", old_due=NEXT.isoformat())
        out = bills.apply_proposal(self.conn, PID, "approve")
        self.assertNotIn("error", out, out)
        self.assertEqual(self._status(), "approved")


class ACadencePickedByHandSinceWithdrawsTheOffer(unittest.TestCase):
    """The offer is stamped with the cadence it was made from. A cadence
    the person set by hand since is their answer, and approving would
    overwrite it — whether or not the due date moved with it, and a moved
    date is judged against the cadence the offer saw, not the new one."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Harbor Gym", 40.0, next_due=NEXT)
        b = bills.get_bill(self.conn, "Harbor Gym")
        self.assertTrue(bills._offer_cadence(
            self.conn, b, pid=PID, kind="frequency_change",
            payee="Harbor Gym", amount=40.0, frequency="WEEKLY",
            interval=1, next_due=NEXT, evidence={}))

    def tearDown(self):
        self.conn.close()

    def _hand_edit(self, frequency, interval, due):
        add_bill(self.conn, "Harbor Gym", 40.0, frequency=frequency,
                 interval=interval, next_due=due)

    def _approve(self):
        return bills.apply_proposal(self.conn, PID, "approve")

    def test_a_longer_cycle_with_its_own_next_date_withdraws(self):
        for freq, iv, due in (("YEARLY", 1, NEXT.replace(year=NEXT.year + 1)),
                              ("WEEKLY", 2, NEXT + dt.timedelta(days=14))):
            with self.subTest(cadence=f"{freq}:{iv}"):
                self.conn.execute("UPDATE bill_proposals SET status='pending'")
                self._hand_edit(freq, iv, due)
                out = self._approve()
                self.assertIn("has changed since", out.get("error", ""), out)
                b = bills.get_bill(self.conn, "Harbor Gym")
                rec = as_dict(b["raw"])["recurrence"]
                self.assertEqual((rec["frequency"], rec["interval"]),
                                 (freq, iv))
                self.assertEqual(b["due_on"], due)

    def test_a_cadence_only_edit_withdraws(self):
        self._hand_edit("MONTHLY", 3, NEXT)
        out = self._approve()
        self.assertIn("has changed since", out.get("error", ""), out)
        rec = as_dict(bills.get_bill(self.conn, "Harbor Gym")["raw"])["recurrence"]
        self.assertEqual(rec["interval"], 3)

    def test_the_nightly_roll_on_the_stamped_cadence_still_approves(self):
        rolled = (NEXT.replace(month=NEXT.month % 12 + 1,
                               year=NEXT.year + (NEXT.month == 12)))
        self.conn.execute(
            "UPDATE bills SET due_on=%s, raw = raw || %s::jsonb "
            "WHERE payee='Harbor Gym'",
            (rolled, jsonb({"dueOn": rolled.isoformat()})))
        out = self._approve()
        self.assertNotIn("error", out, out)
        self.assertEqual(bills.get_bill(self.conn, "Harbor Gym")["frequency"],
                         "EVERY_WEEK")


class TheNightlyOffersFromTheBillAsItIsNow(unittest.TestCase):
    """The offer records the figures it was made from, and one withdrawn
    because the bill moved past it is offered again with the new ones."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # billed monthly, but charged every week for the last six weeks
        last = TODAY - dt.timedelta(days=2)
        for n in range(6):
            add_txn(self.conn, last - dt.timedelta(days=7 * n), 20.0,
                    "HARBOR GYM")
        add_bill(self.conn, "Harbor Gym", 20.0, next_due=NEXT,
                 last_seen=last - dt.timedelta(days=28))

    def tearDown(self):
        self.conn.close()

    def _offers(self):
        return self.conn.execute(
            "SELECT * FROM bill_proposals WHERE kind='frequency_change'"
        ).fetchall()

    def test_the_offer_carries_its_figures_and_comes_back_after_a_withdrawal(self):
        bills.run(self.conn, TODAY)
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        ev = as_dict(offers[0]["evidence"])
        b = bills.get_bill(self.conn, "Harbor Gym")
        self.assertAlmostEqual(ev["old_amount"], abs(b["amount"]))
        self.assertEqual(ev["old_due"],
                         b["due_on"].isoformat() if b["due_on"] else None)
        add_bill(self.conn, "Harbor Gym", 21.0, next_due=b["due_on"],
                 source="manual")
        out = bills.apply_proposal(self.conn, offers[0]["id"], "approve")
        self.assertIn("changed since", out.get("error", ""), out)
        bills.run(self.conn, TODAY)
        again = self._offers()
        self.assertEqual([o["status"] for o in again], ["pending"])
        self.assertAlmostEqual(as_dict(again[0]["evidence"])["old_amount"], 21.0)
        out = bills.apply_proposal(self.conn, again[0]["id"], "approve")
        self.assertNotIn("error", out, out)

    def test_a_pinned_price_still_gets_a_cadence_offer(self):
        # the pin is about the amount; the cadence is a different fact
        self.conn.execute("UPDATE bills SET raw = raw || %s::jsonb "
                          "WHERE payee='Harbor Gym'",
                          (jsonb({"lock_amount": True}),))
        bills.run(self.conn, TODAY)
        offers = self._offers()
        self.assertEqual([o["status"] for o in offers], ["pending"])
        out = bills.apply_proposal(self.conn, offers[0]["id"], "approve")
        self.assertNotIn("error", out, out)
        b = bills.get_bill(self.conn, "Harbor Gym")
        self.assertEqual(b["frequency"], "EVERY_WEEK")
        self.assertAlmostEqual(abs(b["amount"]), 20.0)
        self.assertTrue(as_dict(b["raw"]).get("lock_amount"))


if __name__ == "__main__":
    unittest.main()
