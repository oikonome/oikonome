"""The bill offers a person is shown are ones they can act on as written.

bills.pending_proposals is the one list the Bills page, approve-all, the
mail and the Today count read. An offer that could not be acted on as
written, or that went stale, waits unanswered and unlisted:

- an offer to add a credit-card payment as a bill is never filed — the
  card machinery already pays card debt, so the bill would count the
  same money twice;
- several offers to add one merchant at different amounts are one row;
- an offer to remove or change a bill that is not active, or to join a
  fee or a merchant to a bill that does not exist, is not listed;
- an unanswered offer to add a bill drops once its expected date is a
  full cycle past; a series still charging keeps its offer current.
"""

import datetime as dt
import unittest

from oikonome.engine import bills

from .util import add_bill, make_db, write_config

DAY = dt.date(2026, 5, 20)


def _offer(conn, pid, payee, amount, *, kind="add", next_due=None,
           frequency="WEEKLY", interval=1, evidence=None):
    ok = bills._insert_proposal(
        conn, pid=pid, kind=kind, payee=payee, amount=amount,
        frequency=frequency, interval=interval,
        next_due=next_due or DAY + dt.timedelta(days=3),
        evidence=evidence if evidence is not None
        else {"cycle_days": 7, "hits": 5})
    return ok


def _listed(conn, day=DAY):
    return [p["id"] for p in bills.pending_proposals(conn, day)]


class OffersListedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_card_payment_is_never_offered_as_a_bill(self):
        self.assertFalse(_offer(self.conn, "prop:card", "Northwind Credit Crd Epay",
                                812.40, frequency="WEEKLY", interval=2,
                                evidence={"cycle_days": 14, "hits": 6}))
        self.assertFalse(_offer(self.conn, "prop:card2", "Lakeside Visa",
                                300.00, evidence={"cycle_days": 7, "hits": 4,
                                                  "category": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}))
        # one filed before the rule is not listed either
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, bill_type, payee, amount, "
            "frequency, interval, next_due, evidence, status, created_at) "
            "VALUES ('prop:old-card', 'add', 'occurrence', "
            "'Northwind Credit Crd Epay', 812.40, 'WEEKLY', 2, %s, "
            "'{}'::jsonb, 'pending', now())", (DAY,))
        self.assertEqual(_listed(self.conn), [])

    def test_one_merchant_at_several_amounts_is_one_offer(self):
        _offer(self.conn, "prop:h1", "Harbor Market", 41.00,
               next_due=DAY + dt.timedelta(days=1))
        _offer(self.conn, "prop:h2", "Harbor Market", 63.00,
               next_due=DAY + dt.timedelta(days=4))
        _offer(self.conn, "prop:h3", "HARBOR MARKET #12", 85.00,
               next_due=DAY + dt.timedelta(days=2))
        _offer(self.conn, "prop:other", "Bluebell Dance", 30.00)
        self.assertEqual(sorted(_listed(self.conn)), ["prop:h2", "prop:other"])

    def test_turning_down_the_one_row_turns_down_its_twins(self):
        _offer(self.conn, "prop:h1", "Harbor Market", 41.00)
        _offer(self.conn, "prop:h2", "Harbor Market", 63.00,
               next_due=DAY + dt.timedelta(days=4))
        self.assertNotIn("error", bills.apply_proposal(self.conn, "prop:h2",
                                                       "reject"))
        self.assertEqual(_listed(self.conn), [])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM bill_proposals WHERE id='prop:h1'"
        ).fetchone()["status"], "rejected")

    def test_an_offer_about_a_bill_that_is_not_active_is_not_listed(self):
        add_bill(self.conn, "Pine Ridge Clinic", 120.0)
        add_bill(self.conn, "Corner Coffee", 25.0)
        bills.archive_bill(self.conn, "Corner Coffee")
        # no bill named this at all (the live one has a longer name)
        _offer(self.conn, "prop:r1", "Pine Ridge", 120.0, kind="remove",
               frequency=None, next_due=None, evidence={})
        # archived already
        _offer(self.conn, "prop:r2", "Corner Coffee", 25.0, kind="remove",
               frequency=None, next_due=None, evidence={})
        # a live bill: listed
        _offer(self.conn, "prop:r3", "Pine Ridge Clinic", 120.0,
               kind="remove", frequency=None, next_due=None, evidence={})
        # a merchant to join a bill that does not exist
        _offer(self.conn, "prop:m1", "Kestrel Mart", 9.0, kind="merchant",
               frequency=None, next_due=None,
               evidence={"bill_payee": "Snacks"})
        # …and one to join the live bill
        _offer(self.conn, "prop:m2", "Pine Ridge Labs", 9.0, kind="merchant",
               frequency=None, next_due=None,
               evidence={"bill_payee": "Pine Ridge Clinic"})
        self.assertEqual(sorted(_listed(self.conn)), ["prop:m2", "prop:r3"])

    def test_an_unanswered_offer_drops_a_full_cycle_past_its_date(self):
        _offer(self.conn, "prop:gone", "Tidewater Gym", 45.00,
               frequency="MONTHLY", next_due=DAY - dt.timedelta(days=31),
               evidence={"cycle_days": 30, "hits": 4})
        _offer(self.conn, "prop:late", "Maple Tutoring", 60.00,
               frequency="MONTHLY", next_due=DAY - dt.timedelta(days=12),
               evidence={"cycle_days": 30, "hits": 4})
        # no measured cycle: the cadence's own length (every 2 weeks)
        _offer(self.conn, "prop:biweekly", "Lantern Cleaners", 80.00,
               frequency="WEEKLY", interval=2,
               next_due=DAY - dt.timedelta(days=15), evidence={"hits": 5})
        self.assertEqual(_listed(self.conn), ["prop:late"])
        # nothing was deleted: the lapsed offers wait, unanswered
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM bill_proposals WHERE status='pending'"
        ).fetchone()["n"], 3)

    def test_a_series_still_charging_keeps_its_offer_current(self):
        _offer(self.conn, "prop:s", "Tidewater Gym", 45.00, frequency="MONTHLY",
               next_due=DAY - dt.timedelta(days=40),
               evidence={"cycle_days": 30, "hits": 4})
        self.assertEqual(_listed(self.conn), [])
        # the nightly finds the series again with a newer charge: the
        # standing offer takes the new date (and is not a new offer)
        self.assertFalse(_offer(self.conn, "prop:s", "Tidewater Gym", 45.00,
                                frequency="MONTHLY",
                                next_due=DAY + dt.timedelta(days=9),
                                evidence={"cycle_days": 30, "hits": 5}))
        self.assertEqual(_listed(self.conn), ["prop:s"])
        # an answered offer is never rewritten
        bills.apply_proposal(self.conn, "prop:s", "reject")
        _offer(self.conn, "prop:s", "Tidewater Gym", 45.00, frequency="MONTHLY",
               next_due=DAY + dt.timedelta(days=40),
               evidence={"cycle_days": 30, "hits": 6})
        row = self.conn.execute("SELECT status, next_due FROM bill_proposals "
                                "WHERE id='prop:s'").fetchone()
        self.assertEqual((row["status"], row["next_due"]),
                         ("rejected", DAY + dt.timedelta(days=9)))


if __name__ == "__main__":
    unittest.main()
