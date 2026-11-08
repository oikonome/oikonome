"""Two approvals of different bill offers cannot both create a bill on the
same charges.

An approval checks that no active bill already claims the charges its bill
would match, then creates the bill. Two approvals running at once — one
offer renamed onto the other's charges, the other approved as it stands —
would each check before the other's bill was committed, both pass, and the
plan would count the charge twice. The approvals take turns on the
household's claim lock, so the second sees the first one's bill and its
offer is withdrawn.
"""

import datetime as dt
import os
import threading
import unittest
from unittest import mock

from psycopg.types.json import Jsonb

from oikonome.db import tenancy
from oikonome.engine import alerts, bills
from oikonome.notify import mailact
from oikonome.web import api

from .util import TEST_DB, make_db, write_config


def _offer(conn, pid, payee):
    conn.execute(
        """INSERT INTO bill_proposals (id, kind, bill_type, payee, amount,
               frequency, "interval", next_due, evidence, status, created_at)
           VALUES (%s, 'add', 'occurrence', %s, 15.49, 'MONTHLY', 1, %s, %s,
                   'pending', now())""",
        (pid, payee, dt.date.today() + dt.timedelta(days=10), Jsonb({})))


class TwoApprovalsOneChargeTests(unittest.TestCase):
    def setUp(self):
        self.a = make_db()
        write_config(self.a)
        tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.b = tenancy.tenant_connect(tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
        _offer(self.a, "prop:x", "Spinwheel Audio")
        _offer(self.a, "prop:y", "NORTHWIND STREAM")

    def tearDown(self):
        self.a.close()
        self.b.close()

    def test_the_second_approval_sees_the_first_ones_bill(self):
        out: dict = {}
        # approval A holds the claim lock: offer X is approved and renamed
        # onto Northwind's charges, not yet committed
        with self.a.transaction():
            api._hold_bill_claims(self.a)
            r = bills.apply_proposal(self.a, "prop:x", "approve")
            api._adjust_approved_bill(self.a, r["approved"],
                                      {"name": "Northwind"})
            # approval B of offer Y, as it stands, on another connection
            t = threading.Thread(target=lambda: out.update(
                r=api._decide_proposal(self.b, "prop:y", "approve", {})))
            t.start()
            t.join(1.0)
            waited = t.is_alive()
        t.join(10)
        self.assertTrue(waited, "the second approval did not wait its turn")
        self.assertIn("already tracked", out["r"].get("error", ""))
        live = [r["payee"] for r in self.a.execute(
            "SELECT payee FROM bills WHERE active = 1")]
        self.assertEqual(live, ["Northwind"])
        self.assertEqual(self.a.execute(
            "SELECT status FROM bill_proposals WHERE id='prop:y'"
        ).fetchone()["status"], "superseded")


    def test_two_plain_approvals_of_one_merchant_make_one_bill(self):
        """The email button and the form door call apply_proposal with no
        lock of their own: two of them approving offers that share a key
        token must still take turns, so the second offer is withdrawn
        rather than becoming a second bill on the same charges."""
        _offer(self.a, "prop:m1", "QUILLCAST.COM")
        _offer(self.a, "prop:m2", "Quillcast")
        out: dict = {}
        with self.a.transaction():
            r = bills.apply_proposal(self.a, "prop:m1", "approve")
            self.assertNotIn("error", r, r)
            t = threading.Thread(target=lambda: out.update(
                r=bills.apply_proposal(self.b, "prop:m2", "approve")))
            t.start()
            t.join(1.0)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertIn("already tracked", out["r"].get("error", ""), out["r"])
        live = sorted(r["payee"] for r in self.a.execute(
            "SELECT payee FROM bills WHERE active = 1"))
        self.assertEqual(live, ["QUILLCAST.COM"])
        self.assertEqual(self.a.execute(
            "SELECT status FROM bill_proposals WHERE id='prop:m2'"
        ).fetchone()["status"], "superseded")

    def test_an_email_tap_on_a_withdrawn_offer_is_not_a_failure(self):
        """The second of two same-merchant offers tapped in the email finds
        the first one's bill and is withdrawn. What the person asked for —
        the merchant tracked — has happened, so the page must not say the
        tap failed and ask for a retry (which would then report the offer
        as already decided), and with the queue empty the proposals alert
        is retired as on any other decision."""
        _offer(self.a, "prop:m1", "QUILLCAST.COM")
        _offer(self.a, "prop:m2", "Quillcast")
        self.a.execute("UPDATE bill_proposals SET status='rejected' "
                       "WHERE id IN ('prop:x', 'prop:y')")
        alerts.log(self.a, [{"kind": "proposals", "severity": "info",
                             "message": "2 bills to review."}],
                   dt.date.today())
        self.assertNotIn("error",
                         bills.apply_proposal(self.a, "prop:m1", "approve"))
        tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        tok = mailact.parse(mailact.token(
            tid, "me@example.dev", "bill", "prop:m2", "approve"))
        with mock.patch.object(mailact, "_standing", return_value=None):
            out = mailact.apply(tok)
        self.assertEqual(out["state"], "already", out)
        self.assertIn("QUILLCAST.COM", out.get("sentence", ""), out)
        self.assertEqual(self.a.execute(
            "SELECT count(*) AS n FROM alerts_log WHERE kind='proposals' "
            "AND active=1").fetchone()["n"], 0)
        # a second tap of the same button says the same thing
        with mock.patch.object(mailact, "_standing", return_value=None):
            again = mailact.apply(tok)
        self.assertEqual(again["state"], "already", again)
        live = [r["payee"] for r in self.a.execute(
            "SELECT payee FROM bills WHERE active = 1")]
        self.assertEqual(live, ["QUILLCAST.COM"])

    def test_an_email_tap_on_a_stale_change_offer_names_the_real_reason(self):
        """An amount-change offer overtaken by a later hand edit of the bill
        is withdrawn too, but not because the merchant is "already tracked":
        the engine's sentence says the bill changed since the offer was
        made, and the page must repeat that rather than the add-offer
        wording. The bill keeps the hand-set amount."""
        from .util import add_bill
        nxt = dt.date.today() + dt.timedelta(days=10)
        add_bill(self.a, "Harbor Gym", 40.0, next_due=nxt)
        bills._insert_proposal(
            self.a, pid="prop:amt", kind="amount_change", payee="Harbor Gym",
            amount=45.0, bill_type="occurrence", frequency="MONTHLY",
            interval=1, next_due=nxt, evidence={"old": 40.0})
        add_bill(self.a, "Harbor Gym", 52.0, next_due=nxt, source="manual")
        tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        tok = mailact.parse(mailact.token(
            tid, "me@example.dev", "bill", "prop:amt", "approve"))
        with mock.patch.object(mailact, "_standing", return_value=None):
            out = mailact.apply(tok)
        self.assertEqual(out["state"], "already", out)
        self.assertIn("changed since", out["sentence"], out)
        self.assertNotIn("already tracked", out["sentence"], out)
        self.assertEqual(abs(bills.get_bill(self.a, "Harbor Gym")["amount"]), 52.0)

    def test_a_hand_add_waits_for_an_approval_in_flight(self):
        """A bill added by hand while an approval is between its check and
        its commit would be invisible to that check, and the charge would
        be claimed twice. The hand save takes its turn on the same lock,
        so it lands after the approval or before its check, never inside."""
        tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        user = {"tenant_id": tid, "email": "me@example.dev",
                "role": "owner", "id": None}
        out: dict = {}
        with self.a.transaction():
            bills.hold_bill_claims(self.a)
            t = threading.Thread(target=lambda: out.update(
                r=api.bills_save_api(user=user, body={
                    "payee": "Lanternfish Gym", "amount": 31.0,
                    "cadence": "MONTHLY:1"})))
            t.start()
            t.join(1.0)
            waited = t.is_alive()
        t.join(10)
        self.assertTrue(waited, "the hand add did not wait its turn")
        self.assertTrue(out.get("r"), out)

    def test_a_restore_waits_for_an_approval_in_flight(self):
        """Restoring an archived bill makes it active and claiming again, so
        it takes its turn on the same lock as an add."""
        from .util import add_bill
        add_bill(self.a, "Lanternfish Pool", 22.0)
        bills.archive_bill(self.a, "Lanternfish Pool")
        out: dict = {}
        with self.a.transaction():
            bills.hold_bill_claims(self.a)
            t = threading.Thread(target=lambda: out.update(
                n=bills.restore_bill(self.b, "Lanternfish Pool")))
            t.start()
            t.join(1.0)
            waited = t.is_alive()
        t.join(10)
        self.assertTrue(waited, "the restore did not wait its turn")
        self.assertEqual(out.get("n"), 1, out)

    def test_the_claim_lock_refuses_a_connection_with_no_household(self):
        """The lock is keyed by household. A caller with no household set
        would share one key with every other such caller across all
        households, so it is refused rather than queued behind them."""
        with self.a.transaction():
            self.a.execute("SELECT set_config('app.tenant_id', '', true)")
            with self.assertRaises(RuntimeError):
                bills.hold_bill_claims(self.a)


if __name__ == "__main__":
    unittest.main()
