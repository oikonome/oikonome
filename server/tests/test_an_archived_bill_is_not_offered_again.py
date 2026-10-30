"""Archiving a bill is a decision about its merchant, and the nightly
detection pass honours it.

Detection used to treat only ACTIVE bills as known, so the morning after a
subscription was archived the pass found the same monthly charges and
offered to add it back — the archive read as "never heard of it". An
archived bill's merchant now silences the search for new bills, the
income search and the Plaid-stream cross-check alike, a pending offer for
it is withdrawn when the bill is archived, and restoring the bill is the
one way the household changes its mind.
"""

import datetime as dt
import threading
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import bills

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _stream(merchant):
    return {"stream_id": "str-" + merchant.lower(), "direction": "outflow",
            "merchant": merchant, "description": merchant, "amount": 12.99,
            "frequency": "MONTHLY", "is_active": True, "status": "MATURE",
            "first_date": "2026-01-05", "last_date": "2026-07-05",
            "predicted_next_date": "2026-08-05",
            "transaction_ids": ["a", "b", "c"]}


class AnArchivedBillIsNotOfferedAgain(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # a subscription charged on the 5th for the last four months
        for n in (4, 3, 2, 1):
            y, m = TODAY.year, TODAY.month - n
            while m <= 0:
                y, m = y - 1, m + 12
            add_txn(self.conn, dt.date(y, m, 5), 12.99, "FABRIKAM STREAMING")

    def tearDown(self):
        self.conn.close()

    def _adds(self):
        return [r["payee"] for r in self.conn.execute(
            "SELECT payee FROM bill_proposals WHERE kind='add' "
            "AND status='pending'").fetchall()]

    def test_the_fixture_is_one_detection_offers_when_nothing_is_known(self):
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_an_archived_bill_silences_the_offer(self):
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()),
                         self._adds())
        # restored, the active bill covers it the same way
        bills.restore_bill(self.conn, "Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()))

    def test_archiving_withdraws_the_standing_offer(self):
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()))
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()))
        superseded = self.conn.execute(
            "SELECT count(*) AS n FROM bill_proposals WHERE kind='add' "
            "AND status='superseded'").fetchone()["n"]
        self.assertGreaterEqual(superseded, 1)

    def test_the_plaid_cross_check_counts_an_archived_merchant_as_covered(self):
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        out = bills.propose_from_plaid_streams(
            self.conn, [_stream("Fabrikam Streaming")], TODAY)
        self.assertEqual((out["proposed"], out["covered"]), (0, 1))
        self.assertEqual(self._adds(), [])

    def test_an_archived_income_series_is_not_offered_again(self):
        for n in (4, 3, 2, 1):
            y, m = TODAY.year, TODAY.month - n
            while m <= 0:
                y, m = y - 1, m + 12
            add_txn(self.conn, dt.date(y, m, 1), -2400.00, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME")
        add_bill(self.conn, "Northwind Payroll", 2400.00, income=True,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Northwind Payroll")
        bills.run(self.conn, TODAY)
        self.assertFalse(any("northwind" in p.lower() for p in self._adds()),
                         self._adds())

    def _months_ago(self, n, day):
        y, m = TODAY.year, TODAY.month - n
        while m <= 0:
            y, m = y - 1, m + 12
        return dt.date(y, m, day)

    def _retire_by_detection(self, payee):
        """The bill leaves service the way the nightly retires it: an offer
        to remove it, approved — not the household's own Archive button."""
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES (%s, 'remove', %s, 0, 'pending', '{}')",
            ("prop:test-remove:" + payee, payee))
        out = bills.apply_proposal(self.conn, "prop:test-remove:" + payee,
                                   "approve")
        self.assertNotIn("error", out, out)

    def test_approving_a_stale_remove_offer_keeps_the_households_archive(self):
        """The household archived the bill by hand while a remove offer for
        it was still pending. Approving that stale offer afterwards must not
        turn their decision into detection's: the series stays declined."""
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:stale-remove', 'remove', "
            "'Fabrikam Streaming', 0, 'pending', '{}')")
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        bills.apply_proposal(self.conn, "prop:stale-remove", "approve")
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()),
                         self._adds())

    def test_a_stale_remove_offer_cannot_undo_an_archive_made_before_the_mark(self):
        """A bill archived by hand before archives were marked carries no
        mark at all. Approving a remove offer still pending for it must not
        stamp it as retired by detection; the offer is simply moot."""
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self.conn.execute("UPDATE bills SET active = 0 "
                          "WHERE payee = 'Fabrikam Streaming'")
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:old-remove', 'remove', "
            "'Fabrikam Streaming', 0, 'pending', '{}')")
        out = bills.apply_proposal(self.conn, "prop:old-remove", "approve")
        self.assertNotIn("error", out, out)
        status = self.conn.execute(
            "SELECT status FROM bill_proposals WHERE id = 'prop:old-remove'"
        ).fetchone()["status"]
        self.assertEqual(status, "superseded")
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()),
                         self._adds())

    def test_archiving_adds_to_the_withdrawn_list_rather_than_replacing_it(self):
        """The list of offers an archive withdrew is what restoring or
        deleting releases. An archive must add its own withdrawals to it,
        never overwrite ones the nightly pass recorded in the meantime."""
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self.conn.execute(
            "UPDATE bills SET raw = COALESCE(raw, '{}'::jsonb) || "
            "'{\"archive_withdrew\": [\"prop:earlier\"]}'::jsonb "
            "WHERE payee = 'Fabrikam Streaming'")
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:pending-add', 'add', "
            "'Fabrikam Streaming', 12.99, 'pending', '{}')")
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        raw = self.conn.execute(
            "SELECT raw FROM bills WHERE payee = 'Fabrikam Streaming'"
        ).fetchone()["raw"]
        self.assertEqual(sorted(raw["archive_withdrew"]),
                         ["prop:earlier", "prop:pending-add"])

    def test_a_bill_detection_retired_does_not_silence_its_merchant(self):
        """Only what the household archived by hand is a decision. A
        one-time bill or ended subscription the pass itself retired must
        not stop the same merchant's new recurring charges being offered —
        otherwise years of retired one-offs silently blind detection."""
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self._retire_by_detection("Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_an_archived_bill_does_not_silence_a_merchant_sharing_a_word(self):
        """The silence is scoped to the archived bill's own merchant: an
        archived "Fabrikam Insurance" must not stop a different recurring
        charge whose name merely shares a word with it being offered."""
        add_bill(self.conn, "Fabrikam Insurance", 80.00,
                 merchant="Fabrikam Insurance",
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Insurance")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_the_nightly_pass_does_not_withdraw_offers_for_a_shared_word(self):
        """A standing offer for one merchant is not withdrawn because some
        other, archived bill shares a word with it."""
        bills.run(self.conn, TODAY)
        before = self._adds()
        self.assertTrue(any("fabrikam" in p.lower() for p in before))
        add_bill(self.conn, "Fabrikam Insurance", 80.00,
                 merchant="Fabrikam Insurance",
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Insurance")
        self.assertEqual(sorted(self._adds()), sorted(before))
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_deleting_an_archived_bill_lets_its_offer_be_made_again(self):
        """An offer withdrawn because its bill was archived comes back once
        the archive row is gone. Offer ids are deterministic and filing is
        insert-or-nothing, so a withdrawn row left in place would block the
        same offer forever."""
        bills.run(self.conn, TODAY)
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()))
        bills.delete_bill(self.conn, "Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_restoring_then_deleting_lets_the_offer_be_made_again(self):
        """Restoring lifts the decision too: what archiving withdrew is
        released, so after the restored bill is later deleted the offer can
        be filed again."""
        bills.run(self.conn, TODAY)
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        bills.restore_bill(self.conn, "Fabrikam Streaming")
        bills.delete_bill(self.conn, "Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_the_income_fixture_is_one_detection_offers(self):
        for n in (4, 3, 2, 1):
            add_txn(self.conn, self._months_ago(n, 1), -2400.00,
                    "NORTHWIND PAYROLL", account="chk", primary="INCOME")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("northwind" in p.lower() for p in self._adds()),
                        self._adds())

    def test_a_retired_income_series_does_not_silence_a_new_paycheck(self):
        """An income series the pass retired (the paychecks stopped) must
        not block a paycheck series with the same employer token later."""
        for n in (4, 3, 2, 1):
            add_txn(self.conn, self._months_ago(n, 1), -2400.00,
                    "NORTHWIND PAYROLL", account="chk", primary="INCOME")
        add_bill(self.conn, "Northwind Payroll", 2400.00, income=True,
                 next_due=TODAY + dt.timedelta(days=10))
        self._retire_by_detection("Northwind Payroll")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("northwind" in p.lower() for p in self._adds()),
                        self._adds())

    def test_the_plaid_cross_check_offers_a_merchant_detection_retired(self):
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self._retire_by_detection("Fabrikam Streaming")
        out = bills.propose_from_plaid_streams(
            self.conn, [_stream("Fabrikam Streaming")], TODAY)
        self.assertEqual(out["proposed"], 1, out)

    def test_editing_an_archived_bill_keeps_the_decision(self):
        """A save that leaves the bill archived must not drop the mark that
        says the household archived it by hand."""
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        add_bill(self.conn, "Fabrikam Streaming", 13.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()),
                         self._adds())

    def test_an_archived_paycheck_with_a_merchant_identity_stays_declined(self):
        """A paycheck series carrying a merchant identity is matched by that
        identity alone, so the income search must hand the declined rule
        each deposit's merchant: read without it, the rule matches nothing
        and the series the household archived is offered again."""
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Northwind','manual') RETURNING id::text AS id").fetchone()["id"]
        ids = [add_txn(self.conn, self._months_ago(n, 1), -2400.00,
                       "NORTHWIND PAYROLL", account="chk", primary="INCOME")
               for n in (4, 3, 2, 1)]
        self.conn.execute("UPDATE transactions SET merchant_id=%s "
                          "WHERE id = ANY(%s)", (mid, ids))
        add_bill(self.conn, "Northwind Payroll", 2400.00, income=True,
                 merchants=["Northwind"],
                 next_due=TODAY + dt.timedelta(days=10))
        raw = self.conn.execute("SELECT raw FROM bills WHERE payee=%s",
                                ("Northwind Payroll",)).fetchone()["raw"]
        self.assertEqual([r["id"] for r in raw["merchant_refs"]], [mid])
        bills.archive_bill(self.conn, "Northwind Payroll")
        bills.run(self.conn, TODAY)
        self.assertFalse(any("northwind" in p.lower() for p in self._adds()),
                         self._adds())

    def _archived_elsewhere(self, payee, amount, by, **kw):
        """The bill as a mirror follower receives it: the archived row and
        its mark copied in as data — archive_bill never runs here."""
        add_bill(self.conn, payee, amount,
                 next_due=TODAY + dt.timedelta(days=10), **kw)
        self.conn.execute(
            "UPDATE bills SET active=0, raw = raw || jsonb_build_object("
            "'archived_by', %s::text) WHERE payee=%s", (by, payee))

    def test_the_nightly_pass_withdraws_an_offer_archived_elsewhere(self):
        """An instance that receives a hand-archived bill by copy never runs
        archive_bill, so the nightly pass itself withdraws a standing offer
        the archived bill's own rule claims — otherwise it waits forever on
        a series the household already declined."""
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()))
        self._archived_elsewhere("Fabrikam Streaming", 12.99, "user")
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()),
                         self._adds())

    def test_the_nightly_pass_keeps_an_offer_a_retired_bill_shares(self):
        """A bill detection retired decides nothing, and an archived bill of
        another merchant sharing a word claims nothing: neither withdraws
        the standing offer."""
        bills.run(self.conn, TODAY)
        before = sorted(self._adds())
        self.assertTrue(any("fabrikam" in p.lower() for p in before))
        self._archived_elsewhere("Fabrikam Streaming", 12.99, "detection")
        self._archived_elsewhere("Fabrikam Insurance", 80.00, "user",
                                 merchant="Fabrikam Insurance")
        bills.run(self.conn, TODAY)
        self.assertEqual(sorted(self._adds()), before)

    def test_an_income_offer_archived_elsewhere_is_withdrawn(self):
        for n in (4, 3, 2, 1):
            add_txn(self.conn, self._months_ago(n, 1), -2400.00,
                    "NORTHWIND PAYROLL", account="chk", primary="INCOME")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("northwind" in p.lower() for p in self._adds()))
        self._archived_elsewhere("Northwind Payroll", 2400.00, "user",
                                 income=True)
        bills.run(self.conn, TODAY)
        self.assertFalse(any("northwind" in p.lower() for p in self._adds()),
                         self._adds())

    def test_archiving_one_paycheck_keeps_another_employers_offer(self):
        """An income series whose descriptor leads with a payroll word is
        keyed on its employer: archiving the old job's 'ACH PAYROLL ACME'
        must not withdraw the new job's 'ACH PAYROLL CONTOSO' offer —
        withdrawn, that offer could never be filed again."""
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:contoso', 'add', 'ACH PAYROLL CONTOSO', "
            "3100, 'pending', '{\"income\": true}')")
        add_bill(self.conn, "ACH PAYROLL ACME", 2400.00, income=True,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.archive_bill(self.conn, "ACH PAYROLL ACME")
        self.assertIn("ACH PAYROLL CONTOSO", self._adds())

    def test_an_archived_paycheck_does_not_silence_another_employer(self):
        """The nightly income search sets aside only the archived series'
        own deposits: a new employer whose deposits also lead with a
        payroll word is still offered."""
        for n in (4, 3, 2, 1):
            add_txn(self.conn, self._months_ago(n, 1), -3100.00,
                    "ACH PAYROLL CONTOSO", account="chk", primary="INCOME")
        self._archived_elsewhere("ACH PAYROLL ACME", 2400.00, "user",
                                 income=True)
        bills.run(self.conn, TODAY)
        self.assertTrue(any("contoso" in p.lower() for p in self._adds()),
                        self._adds())

    def test_an_offer_an_active_bill_covered_comes_back_after_its_delete(self):
        """The nightly supersedes a standing offer once an active bill
        covers it, and records it on that bill: archived and deleted, the
        bill releases it and the series can be offered again."""
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()))
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()))
        bills.archive_bill(self.conn, "Fabrikam Streaming")
        bills.delete_bill(self.conn, "Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def _inflow(self, merchant):
        return {**_stream(merchant), "direction": "inflow", "amount": 3100,
                "stream_id": "str-" + merchant.lower().replace(" ", "-")}

    def test_the_plaid_cross_check_reads_a_paycheck_by_its_employer(self):
        """A paycheck bill whose descriptor leads with a payroll word covers
        its own employer's Plaid stream, not another employer's."""
        add_bill(self.conn, "ACH PAYROLL ACME", 2400.00, income=True,
                 next_due=TODAY + dt.timedelta(days=10))
        out = bills.propose_from_plaid_streams(
            self.conn, [self._inflow("ACH PAYROLL ACME"),
                        self._inflow("ACH PAYROLL CONTOSO")], TODAY)
        self.assertEqual((out["proposed"], out["covered"]), (1, 1), out)
        self.assertTrue(any("contoso" in p.lower() for p in self._adds()),
                        self._adds())

    def test_a_pending_paycheck_offer_does_not_cover_another_employer(self):
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:contoso', 'add', 'ACH PAYROLL CONTOSO', "
            "3100, 'pending', '{\"income\": true}')")
        out = bills.propose_from_plaid_streams(
            self.conn, [self._inflow("ACH PAYROLL ACME")], TODAY)
        self.assertEqual(out["proposed"], 1, out)

    def test_an_active_paycheck_does_not_cover_another_employers_offer(self):
        """The nightly supersedes offers an active bill covers, in its own
        direction and on its employer: an active 'ACH PAYROLL ACME' must not
        silence a pending 'ACH PAYROLL CONTOSO' paycheck offer, and a bill
        must not silence a paycheck offer sharing its word."""
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:contoso', 'add', 'ACH PAYROLL CONTOSO', "
            "3100, 'pending', '{\"income\": true}'), "
            "('prop:fabrikam-pay', 'add', 'FABRIKAM STREAMING REFUND', "
            "300, 'pending', '{\"income\": true}')")
        add_bill(self.conn, "ACH PAYROLL ACME", 2400.00, income=True,
                 next_due=TODAY + dt.timedelta(days=10))
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        adds = self._adds()
        self.assertIn("ACH PAYROLL CONTOSO", adds)
        self.assertIn("FABRIKAM STREAMING REFUND", adds)

    def test_an_active_paycheck_still_covers_its_own_offer(self):
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:acme', 'add', 'ACH PAYROLL ACME INC', "
            "2400, 'pending', '{\"income\": true}')")
        add_bill(self.conn, "ACH PAYROLL ACME", 2400.00, income=True,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        self.assertNotIn("ACH PAYROLL ACME INC", self._adds())

    def test_a_delete_during_an_archive_leaves_the_offer_standing(self):
        """Archiving withdraws a standing offer and then records it on the
        bill. A delete landing between the two leaves no row to record on;
        the offer must go back to standing, not stay withdrawn for good."""
        bills.run(self.conn, TODAY)
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        real = bills._rule_key

        def key_then_delete(b):
            other = tenancy.tenant_connect(tid)
            try:
                bills.delete_bill(other, "Fabrikam Streaming")
            finally:
                other.close()
            return real(b)

        with mock.patch.object(bills, "_rule_key", key_then_delete):
            bills.archive_bill(self.conn, "Fabrikam Streaming")
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_an_active_bill_does_not_cover_an_offer_sharing_one_word(self):
        """The nightly supersedes an offer an active bill covers by that
        bill's own rule: a 'City of Springfield Utilities' bill does not cover a
        'CITY WATER' offer that merely shares its first word."""
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:city-water', 'add', 'CITY WATER', "
            "40, 'pending', '{}')")
        add_bill(self.conn, "City of Springfield Utilities", 90.00,
                 merchant="City of Springfield Utilities",
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        self.assertIn("CITY WATER", self._adds())

    def test_a_paycheck_does_not_cover_a_spending_series(self):
        """New spending detection is covered by spending bills only: an
        active 'Acme Payroll' paycheck must not hide a monthly 'ACME
        INSURANCE' debit from being offered."""
        for n in (4, 3, 2, 1):
            add_txn(self.conn, self._months_ago(n, 12), 61.40,
                    "ACME INSURANCE")
        add_bill(self.conn, "Acme Payroll", 2400.00, income=True,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        self.assertTrue(any("acme" in p.lower() for p in self._adds()),
                        self._adds())

    def _offer_id(self, word):
        rows = [r["id"] for r in self.conn.execute(
            "SELECT id FROM bill_proposals WHERE kind='add' "
            "AND status='pending' AND lower(payee) LIKE %s",
            (f"%{word}%",)).fetchall()]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_a_detected_bill_detection_retired_is_offered_again(self):
        """A bill created by approving the nightly's offer, then retired by
        an approved offer to remove it, decides nothing: when the series
        resumes at the same price and cadence it is offered again — the
        approved offer that created the bill must not swallow the new one
        (offer ids are deterministic)."""
        bills.run(self.conn, TODAY)
        out = bills.apply_proposal(self.conn, self._offer_id("fabrikam"),
                                   "approve")
        self.assertNotIn("error", out, out)
        payee = self.conn.execute(
            "SELECT payee FROM bills WHERE active=1").fetchone()["payee"]
        self._retire_by_detection(payee)
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_a_retired_bill_releases_the_offers_it_covered(self):
        """Offers the nightly superseded because an active bill covered
        them are let go when detection retires that bill."""
        bills.run(self.conn, TODAY)
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        self.assertFalse(any("fabrikam" in p.lower() for p in self._adds()))
        self._retire_by_detection("Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_a_bill_with_no_merchant_covers_only_offers_leading_with_its_word(self):
        """A hand-typed bill with no merchant yet is known by its payee's
        lead word, and covers an offer only when the offer leads with it
        too: 'City of Springfield Utilities' does not cover 'CONTOSO CITY LIFE'."""
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:contoso-city-life', 'add', 'CONTOSO CITY LIFE', "
            "47, 'pending', '{}'), ('prop:city-springfield', 'add', "
            "'CITY OF SPRINGFIELD UTIL', 90, 'pending', '{}')")
        add_bill(self.conn, "City of Springfield Utilities", 90.00,
                 next_due=TODAY + dt.timedelta(days=10))
        bills.run(self.conn, TODAY)
        adds = self._adds()
        self.assertIn("CONTOSO CITY LIFE", adds)
        self.assertNotIn("CITY OF SPRINGFIELD UTIL", adds)

    def test_approving_an_offer_a_bill_covers_is_released_with_that_bill(self):
        """Approving an offer for a merchant a hand-added bill already
        covers withdraws it, recorded on that bill: once the bill is
        archived and deleted the series can be offered again."""
        bills.run(self.conn, TODAY)
        pid = self._offer_id("fabrikam")
        add_bill(self.conn, "Fabrikam", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        out = bills.apply_proposal(self.conn, pid, "approve")
        self.assertIn("error", out)
        bills.archive_bill(self.conn, "Fabrikam")
        bills.delete_bill(self.conn, "Fabrikam")
        bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())

    def test_a_retirement_during_the_nightly_leaves_the_covered_offer_standing(self):
        """Detection retires a bill after the nightly read it as active but
        before it superseded the offer that bill covers. The retired bill
        has already let go of its list, so the offer must not be recorded
        there and withdrawn: it stays standing."""
        bills.run(self.conn, TODAY)
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, status, "
            "evidence) VALUES ('prop:retire', 'remove', "
            "'Fabrikam Streaming', 0, 'pending', '{}')")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        real = bills._supersede_covered_offers

        def retire_then_supersede(conn, rows):
            other = tenancy.tenant_connect(tid)
            try:
                out = bills.apply_proposal(other, "prop:retire", "approve")
                self.assertNotIn("error", out, out)
            finally:
                other.close()
            return real(conn, rows)

        with mock.patch.object(bills, "_supersede_covered_offers",
                               retire_then_supersede):
            bills.run(self.conn, TODAY)
        self.assertTrue(any("fabrikam" in p.lower() for p in self._adds()),
                        self._adds())


class AnArchiveLiftedDuringTheNightlyPass(unittest.TestCase):
    """The nightly pass withdraws a standing offer an archived bill claims
    and records it on that bill, so restoring or deleting the bill can let
    it go. A restore or delete pressed while the pass runs must not lose
    that record: an offer left withdrawn and listed nowhere can never be
    filed again (offer ids are deterministic, filing insert-or-nothing)."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        for n in (4, 3, 2, 1):
            y, m = TODAY.year, TODAY.month - n
            while m <= 0:
                y, m = y - 1, m + 12
            add_txn(self.conn, dt.date(y, m, 5), 12.99, "FABRIKAM STREAMING")
        bills.run(self.conn, TODAY)
        self.assertTrue(self._fabrikam_pending())
        # archived on another instance and copied in: archive_bill never
        # ran here, so the nightly pass is the one that withdraws the offer
        add_bill(self.conn, "Fabrikam Streaming", 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self.conn.execute(
            "UPDATE bills SET active=0, raw = raw || "
            "'{\"archived_by\": \"user\"}'::jsonb "
            "WHERE payee='Fabrikam Streaming'")
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def _fabrikam_pending(self):
        return any("fabrikam" in (r["payee"] or "").lower()
                   for r in self.conn.execute(
                       "SELECT payee FROM bill_proposals WHERE kind='add' "
                       "AND status='pending'").fetchall())

    def _lift_during(self, step, lift, *, wait):
        """Run the nightly pass and, right after `step` inside it, lift
        the archive from a second connection. `wait` holds the pass open
        so a lift that blocks on it has queued before it commits."""
        errors, threads = {}, []

        def other():
            c = tenancy.tenant_connect(self.tid)
            try:
                lift(c, "Fabrikam Streaming")
            except Exception as e:                       # noqa: BLE001
                errors["lift"] = type(e).__name__
            finally:
                c.close()

        real = getattr(bills, step)

        def step_then_lift(*a, **kw):
            out = real(*a, **kw)
            th = threading.Thread(target=other)
            threads.append(th)
            th.start()
            if wait:
                time.sleep(1.0)
            else:
                th.join(30)
            return out

        with mock.patch.object(bills, step, step_then_lift):
            bills.run(self.conn, TODAY)
        for th in threads:
            th.join(30)
        self.assertEqual(errors, {})

    def test_a_restore_waiting_on_the_pass_releases_what_it_withdrew(self):
        self._lift_during("advance_received_income", bills.restore_bill,
                          wait=True)
        bills.delete_bill(self.conn, "Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertTrue(self._fabrikam_pending())

    def test_a_delete_waiting_on_the_pass_releases_what_it_withdrew(self):
        self._lift_during("advance_received_income", bills.delete_bill,
                          wait=True)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM bills WHERE payee='Fabrikam Streaming'"
        ).fetchone())
        bills.run(self.conn, TODAY)
        self.assertTrue(self._fabrikam_pending())

    def test_a_restore_before_the_pass_records_leaves_the_offer_standing(self):
        self._lift_during("_declined_matchers", bills.restore_bill,
                          wait=False)
        self.assertTrue(self._fabrikam_pending())
        bills.delete_bill(self.conn, "Fabrikam Streaming")
        bills.run(self.conn, TODAY)
        self.assertTrue(self._fabrikam_pending())

    def test_a_delete_before_the_pass_records_leaves_the_offer_standing(self):
        self._lift_during("_declined_matchers", bills.delete_bill,
                          wait=False)
        bills.run(self.conn, TODAY)
        self.assertTrue(self._fabrikam_pending())


if __name__ == "__main__":
    unittest.main()
