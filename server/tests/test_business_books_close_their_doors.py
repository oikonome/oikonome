"""The business books count each dollar once, and keep what the owner set.

* A loan account assigned to the business is the servicer's side of
  payments the checking account already recorded: never gross receipts,
  never a second expense.
* The standard mileage rate replaces actual car costs; an estimate takes
  one or the other.
* A 1099 vendor mark follows its merchant through a rename or a merge, and
  the merge's undo puts it back. A vendor's own refund comes off the form
  amount the way it comes off the P&L.
* A bucket set on the Books tab does not take a row out of the Schedule C
  review while it still has no line.
* A charge paid from a business account cannot be capitalized or
  reimbursed as if a member had paid it.
* A fronted movement on a charge the bank feed withdrew stops counting.

Figures and names are invented.
"""
import datetime as dt
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import (books, equity, entities, merchant_dedup,
                             merchant_identity, selfemploy)
from oikonome.engine.compat import jsonb

from .util import TEST_DB, _admin_dsn, _ensure_db, add_txn, seed_accounts

YEAR = 2026


class _Biz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"bk-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Harbor Lane Studio", structure="sole_prop",
            business_start_date="2025-01-01")
        self.eid = self.ent["id"]
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('biz','it1','Biz Checking',"
            "'depository','checking',4000)")
        entities.assign_account(self.conn, "biz", self.eid)

    def tearDown(self):
        self.conn.close()
        self.admin.close()


class LoanAccountTests(_Biz):
    def test_a_loan_payments_servicer_leg_is_not_revenue(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('van','it1','Van Loan','loan','auto',"
            "18000)")
        entities.assign_account(self.conn, "van", self.eid)
        add_txn(self.conn, dt.date(YEAR, 3, 1), -5000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        for m in (3, 4):
            add_txn(self.conn, dt.date(YEAR, m, 5), 600.0, "VAN LOAN PMT",
                    account="biz", primary="LOAN_PAYMENTS",
                    detailed="LOAN_PAYMENTS_CAR_PAYMENT", txn_id=f"chk{m}")
            add_txn(self.conn, dt.date(YEAR, m, 6), -600.0, "PAYMENT RECEIVED",
                    account="van", primary="LOAN_PAYMENTS", txn_id=f"svc{m}")
            add_txn(self.conn, dt.date(YEAR, m, 28), 90.0, "INTEREST CHARGED",
                    account="van", primary="LOAN_PAYMENTS", txn_id=f"int{m}")
        p = books.pnl(self.conn, self.eid, YEAR)
        self.assertEqual(p["revenue"], 5000.0)
        self.assertEqual(p["operating_expenses"], 1200.0)   # checking legs
        ids = {t["id"] for t in books.entity_transactions(
            self.conn, self.eid, YEAR)}
        self.assertFalse(ids & {"svc3", "svc4", "int3", "int4"})
        self.assertFalse(books.txn_belongs_to_entity(self.conn, self.eid,
                                                      "svc3"))
        self.assertNotIn("PAYMENT RECEIVED", {
            v["merchant"] for v in selfemploy.vendor_totals(
                self.conn, self.eid, YEAR)})


class MileageOrActualTests(_Biz):
    def setUp(self):
        super().setUp()
        add_txn(self.conn, dt.date(2025, 3, 1), -50000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        add_txn(self.conn, dt.date(2025, 4, 1), 3000.0, "FUEL STOP",
                account="biz", primary="TRANSPORTATION", txn_id="fuel")
        add_txn(self.conn, dt.date(2025, 5, 1), 1000.0, "AUTO REPAIR",
                account="biz", primary="TRANSPORTATION", txn_id="fix")
        books.classify(self.conn, "fix", "operating",
                       sched_c_line="Car and truck expenses")
        add_txn(self.conn, dt.date(2025, 5, 2), 50.0, "CITY GARAGE",
                account="biz", primary="TRANSPORTATION",
                detailed="TRANSPORTATION_PARKING", txn_id="park")

    def _est(self):
        return selfemploy.estimated_tax(self.conn, self.eid, 2025,
                                        today=dt.date(2026, 6, 1))

    def test_standard_mileage_replaces_actual_car_costs(self):
        selfemploy.add_trip(self.conn, self.eid, date=dt.date(2025, 6, 1),
                            miles=10000)                  # $7,000 at 70c
        e = self._est()
        self.assertEqual(e["vehicle_method"], "standard_mileage")
        self.assertEqual(e["mileage_deduction"], 7000.0)
        self.assertEqual(e["vehicle_expenses_added_back"], 4000.0)  # not parking
        # 50,000 - 4,050 operating + 4,000 added back - 7,000 mileage
        self.assertEqual(e["taxable_profit"], 42950.0)

    def test_larger_actual_costs_are_kept_and_mileage_not_taken(self):
        selfemploy.add_trip(self.conn, self.eid, date=dt.date(2025, 6, 1),
                            miles=1000)                   # $700 < $4,000
        e = self._est()
        self.assertEqual(e["vehicle_method"], "actual")
        self.assertEqual(e["mileage_deduction"], 0.0)
        self.assertEqual(e["mileage_logged"], 700.0)
        self.assertEqual(e["taxable_profit"], 45950.0)


class FaresAreNotOwnVehicleCostsTests(_Biz):
    """The standard rate stands in for running the owner's own car, so a
    ride share or a transit fare is deducted beside the miles, not
    replaced by them — even once its suggested car line is confirmed."""

    def test_a_ride_share_next_to_a_mileage_log_stays_deducted(self):
        add_txn(self.conn, dt.date(2025, 3, 1), -20000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        add_txn(self.conn, dt.date(2025, 4, 1), 1000.0, "RIDE APP TRIP",
                account="biz", primary="TRANSPORTATION",
                detailed="TRANSPORTATION_TAXIS_AND_RIDE_SHARES",
                txn_id="ride")
        books.classify(self.conn, "ride", "operating",
                       sched_c_line="Car and truck expenses")
        add_txn(self.conn, dt.date(2025, 4, 2), 200.0, "METRO FARE CARD",
                account="biz", primary="TRANSPORTATION",
                detailed="TRANSPORTATION_PUBLIC_TRANSIT", txn_id="metro")
        selfemploy.add_trip(self.conn, self.eid, date=dt.date(2025, 6, 1),
                            miles=2000)                   # $1,400 at 70c
        e = selfemploy.estimated_tax(self.conn, self.eid, 2025,
                                     today=dt.date(2026, 6, 1))
        self.assertEqual(e["net_profit"], 18800.0)
        self.assertEqual(e["vehicle_method"], "standard_mileage")
        self.assertEqual(e["vehicle_expenses_added_back"], 0.0)
        self.assertEqual(e["mileage_deduction"], 1400.0)
        self.assertEqual(e["taxable_profit"], 18800.0 - 1400.0)

    def test_fares_larger_than_the_miles_do_not_drop_the_miles(self):
        add_txn(self.conn, dt.date(2025, 3, 1), -20000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        add_txn(self.conn, dt.date(2025, 4, 1), 2000.0, "RIDE APP TRIP",
                account="biz", primary="TRANSPORTATION",
                detailed="TRANSPORTATION_TAXIS_AND_RIDE_SHARES",
                txn_id="ride")
        selfemploy.add_trip(self.conn, self.eid, date=dt.date(2025, 6, 1),
                            miles=2000)
        e = selfemploy.estimated_tax(self.conn, self.eid, 2025,
                                     today=dt.date(2026, 6, 1))
        self.assertEqual(e["vehicle_method"], "standard_mileage")
        self.assertEqual(e["mileage_deduction"], 1400.0)
        self.assertEqual(e["taxable_profit"], 18000.0 - 1400.0)


class ShopCostsAreVehicleCostsTests(_Biz):
    """Repairs and servicing are coded as automotive services and sit on
    another line, but the standard rate stands in for them as much as for
    fuel: added back under the rate, and counted when choosing it."""

    def setUp(self):
        super().setUp()
        add_txn(self.conn, dt.date(2025, 3, 1), -20000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        add_txn(self.conn, dt.date(2025, 4, 1), 500.0, "FUEL STOP",
                account="biz", primary="TRANSPORTATION",
                detailed="TRANSPORTATION_GAS", txn_id="fuel")
        add_txn(self.conn, dt.date(2025, 4, 9), 300.0, "MAPLE TIRE CENTER",
                account="biz", primary="GENERAL_SERVICES",
                detailed="GENERAL_SERVICES_AUTOMOTIVE", txn_id="tires")

    def _est(self):
        return selfemploy.estimated_tax(self.conn, self.eid, 2025,
                                        today=dt.date(2026, 6, 1))

    def test_a_repair_on_another_line_is_added_back(self):
        books.classify(self.conn, "tires", "operating",
                       sched_c_line="Repairs and maintenance")
        selfemploy.add_trip(self.conn, self.eid, date=dt.date(2025, 6, 1),
                            miles=2000)                   # $1,400 at 70c
        e = self._est()
        self.assertEqual(e["vehicle_method"], "standard_mileage")
        self.assertEqual(e["vehicle_expenses_added_back"], 800.0)
        self.assertEqual(e["taxable_profit"], 20000.0 - 1400.0)

    def test_a_repair_counts_in_the_method_choice(self):
        add_txn(self.conn, dt.date(2025, 5, 2), 700.0, "OAK STREET AUTO",
                account="biz", primary="GENERAL_SERVICES",
                detailed="GENERAL_SERVICES_AUTOMOTIVE", txn_id="shop")
        selfemploy.add_trip(self.conn, self.eid, date=dt.date(2025, 6, 1),
                            miles=2000)       # $1,400 < $1,500 actual
        e = self._est()
        self.assertEqual(e["vehicle_method"], "actual")
        self.assertEqual(e["mileage_deduction"], 0.0)
        self.assertEqual(e["taxable_profit"], 20000.0 - 1500.0)


class EstimateReadsOnceTests(_Biz):
    def test_a_future_year_does_not_walk_back_year_by_year(self):
        real = books.entity_transactions
        calls = []

        def counting(conn, eid, year=None):
            calls.append(year)
            return real(conn, eid, year)

        with mock.patch.object(books, "entity_transactions", counting):
            e = selfemploy.estimated_tax(self.conn, self.eid, 2100,
                                         today=dt.date(2026, 10, 2))
        self.assertEqual(calls, [None])
        self.assertEqual(e["next_deadline"], "2100-04-15")
        self.assertEqual(e["next_payment"], e["quarterly"])

    def test_january_still_reaches_last_years_fourth_payment(self):
        add_txn(self.conn, dt.date(2025, 3, 1), -40000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        e = selfemploy.estimated_tax(self.conn, self.eid, 2026,
                                     today=dt.date(2026, 1, 10))
        self.assertEqual(e["next_deadline"], "2026-01-15")
        self.assertGreater(e["next_payment"], 0)


class VendorRefundTests(_Biz):
    def test_a_vendors_own_refund_comes_off_the_form_amount(self):
        add_txn(self.conn, dt.date(YEAR, 2, 1), 2500.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="pay")
        add_txn(self.conn, dt.date(YEAR, 3, 1), -1000.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="back")
        selfemploy.mark_vendor(self.conn, self.eid, "QUILL DESIGN")
        v = {x["merchant"]: x for x in selfemploy.vendor_totals(
            self.conn, self.eid, YEAR)}["QUILL DESIGN"]
        self.assertEqual(v["paid"], 1500.0)
        self.assertFalse(v["needs_1099"])          # under the $2,000 line
        p = books.pnl(self.conn, self.eid, YEAR)
        self.assertEqual(p["operating_expenses"], 1500.0)

    def test_a_client_repayment_does_not(self):
        add_txn(self.conn, dt.date(YEAR, 2, 1), 2500.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="pay")
        add_txn(self.conn, dt.date(YEAR, 3, 1), -1000.0, "NORTHWIND CLIENT",
                account="biz", primary="INCOME", txn_id="client")
        v = {x["merchant"]: x for x in selfemploy.vendor_totals(
            self.conn, self.eid, YEAR)}["QUILL DESIGN"]
        self.assertEqual(v["paid"], 2500.0)

    def _quill(self, year):
        selfemploy.mark_vendor(self.conn, self.eid, "QUILL DESIGN")
        return {x["merchant"]: x for x in selfemploy.vendor_totals(
            self.conn, self.eid, year)}.get("QUILL DESIGN")

    def test_a_refund_of_last_years_payment_leaves_this_years_form(self):
        """The form counts each year's payments gross. A January refund of
        a December overpayment reverses last year's money; netted off this
        year it would erase part of a different year's payments and drop a
        contractor paid in full under the line."""
        add_txn(self.conn, dt.date(YEAR - 1, 12, 20), 500.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="dec")
        add_txn(self.conn, dt.date(YEAR, 3, 1), 2100.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="mar")
        add_txn(self.conn, dt.date(YEAR, 1, 10), -500.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="jan-back")
        v = self._quill(YEAR)
        self.assertEqual(v["paid"], 2100.0)
        self.assertTrue(v["needs_1099"])
        # and last year's form is what it reported: the payment, gross
        self.assertEqual(self._quill(YEAR - 1)["paid"], 500.0)

    def test_a_same_year_refund_still_comes_off_beside_an_older_payment(self):
        add_txn(self.conn, dt.date(YEAR - 1, 12, 20), 700.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="dec")
        add_txn(self.conn, dt.date(YEAR, 2, 1), 2500.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="feb")
        add_txn(self.conn, dt.date(YEAR, 3, 1), -1000.0, "QUILL DESIGN",
                account="biz", primary="GENERAL_SERVICES", txn_id="mar-back")
        self.assertEqual(self._quill(YEAR)["paid"], 1500.0)


class VendorMarkFollowsTheMerchantTests(_Biz):
    def setUp(self):
        super().setUp()
        for n, d in enumerate((2, 3)):
            add_txn(self.conn, dt.date(YEAR, d, 1), 1500.0, "JOE PLUMBING",
                    account="biz", primary="GENERAL_SERVICES",
                    merchant="Joe Plumbing", txn_id=f"joe{n}")
        add_txn(self.conn, dt.date(YEAR, 4, 1), 900.0, "JOE SMITH PLUMBING",
                account="biz", primary="GENERAL_SERVICES",
                merchant="Joe Smith Plumbing", txn_id="joe-b")
        merchant_identity.resolve(self.conn)

    def _vendors(self):
        return {v["merchant"]: v for v in selfemploy.vendor_totals(
            self.conn, self.eid, YEAR)}

    def _rename(self, old, new):
        with self.conn.transaction():
            merchant_dedup.rename(self.conn, old, new)
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC").fetchall()]

    def _undo(self, ids):
        for cid in ids:
            with self.conn.transaction():
                merchant_dedup.undo(self.conn, cid)

    def test_a_rename_keeps_the_mark(self):
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Plumbing",
                               tin_last4="1234")
        self._rename("Joe Plumbing", "Smith Plumbing Co")
        v = self._vendors()["Smith Plumbing Co"]
        self.assertTrue(v["reportable"])
        self.assertTrue(v["needs_1099"])
        self.assertEqual(v["tin_last4"], "1234")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM vendor_1099").fetchone()["n"], 1)

    def test_a_merge_folds_the_mark_and_undo_puts_it_back(self):
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Plumbing",
                               tin_last4="1234")
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Smith Plumbing",
                               reportable=False)
        ids = self._rename("Joe Plumbing", "Joe Smith Plumbing")
        v = self._vendors()["Joe Smith Plumbing"]
        self.assertEqual(v["paid"], 3900.0)
        self.assertTrue(v["reportable"])
        self.assertEqual(v["tin_last4"], "1234")
        self._undo(ids)
        after = self._vendors()
        self.assertTrue(after["Joe Plumbing"]["reportable"])
        self.assertEqual(after["Joe Plumbing"]["tin_last4"], "1234")
        self.assertFalse(after["Joe Smith Plumbing"]["reportable"])
        self.assertIsNone(after["Joe Smith Plumbing"]["tin_last4"])

    def test_a_merge_onto_an_unmarked_merchant_moves_the_mark_and_back(self):
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Plumbing")
        ids = self._rename("Joe Plumbing", "Joe Smith Plumbing")
        self.assertTrue(self._vendors()["Joe Smith Plumbing"]["reportable"])
        self._undo(ids)
        after = self._vendors()
        self.assertTrue(after["Joe Plumbing"]["reportable"])
        self.assertFalse(after["Joe Smith Plumbing"]["reportable"])

    def test_a_mark_whose_merchant_was_merged_away_still_reads(self):
        """A merge that bypassed the hook (a nightly re-resolve retiring a
        merchant into another) is followed at read time."""
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Plumbing")
        a, b = (self.conn.execute(
            "SELECT id FROM merchants WHERE name = %s", (n,)).fetchone()["id"]
            for n in ("Joe Plumbing", "Joe Smith Plumbing"))
        self.conn.execute("UPDATE transactions SET merchant_id = %s "
                          "WHERE merchant_id = %s", (b, a))
        self.conn.execute("UPDATE merchants SET merged_into = %s "
                          "WHERE id = %s", (b, a))
        self.assertTrue(self._vendors()["Joe Smith Plumbing"]["reportable"])

    def _mark_both(self):
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Plumbing",
                               tin_last4="1234")
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Smith Plumbing",
                               reportable=False)

    def _marks(self):
        return self.conn.execute(
            "SELECT merchant_id::text AS mid, reportable, tin_last4 "
            "FROM vendor_1099 WHERE entity_id = %s", (self.eid,)).fetchall()

    def test_marking_a_payee_two_old_marks_follow_folds_them(self):
        """Two marks, one per merchant, both following one survivor after
        a merge the hook never saw: marking the payee keeps one mark."""
        self._mark_both()
        a, b = (self.conn.execute(
            "SELECT id FROM merchants WHERE name = %s", (n,)).fetchone()["id"]
            for n in ("Joe Plumbing", "Joe Smith Plumbing"))
        self.conn.execute("UPDATE transactions SET merchant_id = %s "
                          "WHERE merchant_id = %s", (b, a))
        self.conn.execute("UPDATE merchants SET merged_into = %s "
                          "WHERE id = %s", (b, a))
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Smith Plumbing",
                               tin_last4="5678")
        marks = self._marks()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["mid"], str(b))
        self.assertTrue(marks[0]["reportable"])
        self.assertEqual(marks[0]["tin_last4"], "5678")

    def test_a_fold_on_marking_is_journalled_so_undo_brings_it_back(self):
        """Two marks left following one merchant (a merge the hook did not
        fold) fold when the payee is marked; undoing that merge brings the
        second mark back with its own flags, not a payee with no mark."""
        self._mark_both()
        from unittest import mock
        with mock.patch.object(selfemploy, "follow_merchant_rename",
                               return_value=0):
            ids = self._rename("Joe Plumbing", "Joe Smith Plumbing")
        self.assertEqual(len(self._marks()), 2)
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Smith Plumbing",
                               reportable=False)
        self.assertEqual(len(self._marks()), 1)
        self._undo(ids)
        after = self._vendors()
        self.assertTrue(after["Joe Plumbing"]["reportable"])
        self.assertEqual(after["Joe Plumbing"]["tin_last4"], "1234")
        self.assertFalse(after["Joe Smith Plumbing"]["reportable"])
        self.assertEqual(len(self._marks()), 2)

    def test_a_fold_keeps_the_folded_marks_own_journal(self):
        self._mark_both()
        a = self.conn.execute(
            "SELECT id FROM vendor_1099 WHERE merchant = 'Joe Plumbing'"
        ).fetchone()["id"]
        from oikonome.engine.compat import jsonb
        older = {"from": "Joe P", "id": None, "raws": ["JOE P SVC"],
                 "had_into": True,
                 "mark": {"reportable": True, "tin_last4": None},
                 "into_prior": {"reportable": True, "tin_last4": None}}
        self.conn.execute("UPDATE vendor_1099 SET merged_from = %s, "
                          "note = 'paid by check' WHERE id = %s",
                          (jsonb([older]), a))
        b_mid = self.conn.execute(
            "SELECT id FROM merchants WHERE name = 'Joe Smith Plumbing'"
        ).fetchone()["id"]
        a_mid = self.conn.execute(
            "SELECT id FROM merchants WHERE name = 'Joe Plumbing'"
        ).fetchone()["id"]
        self.conn.execute("UPDATE transactions SET merchant_id = %s "
                          "WHERE merchant_id = %s", (b_mid, a_mid))
        self.conn.execute("UPDATE merchants SET merged_into = %s "
                          "WHERE id = %s", (b_mid, a_mid))
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Smith Plumbing",
                               reportable=False)
        kept = self.conn.execute(
            "SELECT note, merged_from FROM vendor_1099 WHERE entity_id = %s",
            (self.eid,)).fetchall()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["note"], "paid by check")
        notes = kept[0]["merged_from"]
        self.assertIn(older, notes)
        folded = [n for n in notes if n.get("from") == "Joe Plumbing"]
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["mark"],
                         {"reportable": True, "tin_last4": "1234"})
        self.assertTrue(folded[0]["raws"])

    def test_the_nightly_string_clean_folds_the_marks_it_merges(self):
        self._mark_both()
        rows = self.conn.execute(
            "UPDATE merchant_canonical SET canonical = 'Joe Smith Plumbing' "
            "WHERE canonical = 'Joe Plumbing' "
            "RETURNING raw_merchant, merchant_id").fetchall()
        self.assertTrue(rows)
        retired = merchant_identity.re_resolve_recleaned(
            self.conn, {r["raw_merchant"]: r["merchant_id"] for r in rows})
        self.assertEqual(retired, [{"from": "Joe Plumbing",
                                    "into": "Joe Smith Plumbing"}])
        marks = self._marks()
        self.assertEqual(len(marks), 1)
        self.assertTrue(marks[0]["reportable"])
        self.assertEqual(marks[0]["tin_last4"], "1234")
        v = self._vendors()["Joe Smith Plumbing"]
        self.assertEqual(v["paid"], 3900.0)
        # and the payee can be marked again without a clash
        selfemploy.mark_vendor(self.conn, self.eid, "Joe Smith Plumbing",
                               reportable=False)
        self.assertEqual(len(self._marks()), 1)

    def test_the_upgrade_gives_old_marks_their_merchant(self):
        from pathlib import Path
        import psycopg
        mig = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
               / "migrations" / "164_vendor_marks_follow_their_merchant.sql")
        self.conn.execute(
            "INSERT INTO vendor_1099 (entity_id, merchant, reportable) "
            "VALUES (%s, 'Joe Plumbing', true)", (self.eid,))
        try:
            with self.admin.transaction(force_rollback=True):
                self.admin.execute(mig.read_text())
                got = self.admin.execute(
                    "SELECT v.merchant_id = m.id AS ok FROM vendor_1099 v "
                    "JOIN merchants m ON m.tenant_id = v.tenant_id "
                    "AND m.name = v.merchant WHERE v.tenant_id = %s",
                    (self.tid,)).fetchone()
                self.assertTrue(got["ok"])
                self.admin.execute(mig.read_text())        # re-runnable
        except psycopg.Rollback:
            pass


class VendorMarkSurvivesTheTwinFoldTests(_Biz):
    """The nightly fold joins two merchants of one name and the prune later
    deletes the emptied one; a 1099 mark made on that one must move with
    its rows, so the contractor stays on the year-end list through a
    later rename."""

    NAME = "Birch Lane Carpentry"

    def setUp(self):
        super().setUp()
        add_txn(self.conn, dt.date(YEAR, 2, 1), 1200.0, "BIRCH LANE CARPENTRY",
                account="biz", primary="GENERAL_SERVICES",
                merchant=self.NAME, txn_id="birch0")
        merchant_identity.resolve(self.conn)
        self.survivor = self._mid()
        self.twin = self.conn.execute(
            "INSERT INTO merchants (name, name_source, created_at)"
            " VALUES (%s, 'layer1', now() + interval '1 minute')"
            " RETURNING id::text AS id", (self.NAME,)).fetchone()["id"]
        add_txn(self.conn, dt.date(YEAR, 3, 1), 800.0, "BIRCH LANE CARPENTRY",
                account="biz", primary="GENERAL_SERVICES",
                merchant=self.NAME, txn_id="birch1")
        self.conn.execute("UPDATE transactions SET merchant_id = %s"
                          " WHERE id = 'birch1'", (self.twin,))
        self.conn.execute(
            "INSERT INTO vendor_1099 (entity_id, merchant, merchant_id,"
            " reportable, tin_last4) VALUES (%s, %s, %s, true, '5678')",
            (self.eid, self.NAME, self.twin))

    def _mid(self):
        return self.conn.execute(
            "SELECT merchant_id::text AS id FROM transactions"
            " WHERE id = 'birch0'").fetchone()["id"]

    def _mark(self):
        return self.conn.execute(
            "SELECT merchant_id::text AS mid, merchant, tin_last4"
            " FROM vendor_1099 WHERE entity_id = %s", (self.eid,)).fetchall()

    def test_the_fold_moves_the_mark_to_the_survivor(self):
        st = merchant_identity.fold_twins(self.conn)
        self.assertEqual(st["folded"], 1)
        marks = self._mark()
        self.assertEqual([m["mid"] for m in marks], [self.survivor])
        self.assertEqual(marks[0]["tin_last4"], "5678")
        merchant_identity.prune_empty(self.conn)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchants WHERE id = %s", (self.twin,)).fetchone())
        with self.conn.transaction():
            merchant_dedup.rename(self.conn, self.NAME, "Birch Lane Builders")
        v = {v["merchant"]: v for v in selfemploy.vendor_totals(
            self.conn, self.eid, YEAR)}["Birch Lane Builders"]
        self.assertTrue(v["reportable"])
        self.assertEqual(v["tin_last4"], "5678")

    def test_the_fold_folds_into_a_mark_the_survivor_already_has(self):
        self.conn.execute(
            "INSERT INTO vendor_1099 (entity_id, merchant, merchant_id,"
            " reportable) VALUES (%s, 'Birch Lane (old name)', %s, false)",
            (self.eid, self.survivor))
        merchant_identity.fold_twins(self.conn)
        marks = self._mark()
        self.assertEqual([m["mid"] for m in marks], [self.survivor])
        self.assertEqual(marks[0]["tin_last4"], "5678")

    def test_the_prune_holds_a_merchant_a_mark_names(self):
        self.conn.execute("DELETE FROM transactions WHERE id = 'birch1'")
        self.conn.execute("UPDATE merchants SET merged_into = %s"
                          " WHERE id = %s", (self.survivor, self.twin))
        merchant_identity.prune_empty(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM merchants WHERE id = %s", (self.twin,)).fetchone())

    def test_the_upgrade_repairs_a_mark_left_naming_nothing(self):
        """A mark the old fold and prune left naming a deleted merchant
        takes the live merchant of its name again."""
        from pathlib import Path
        sql = (Path(merchant_identity.__file__).resolve().parents[1] / "db"
               / "migrations"
               / "173_a_vendor_mark_names_a_merchant_that_exists.sql")

        class _Undo(Exception):
            pass

        try:
            with self.admin.transaction():
                self.admin.execute("ALTER TABLE vendor_1099 DROP CONSTRAINT"
                                   " vendor_1099_merchant_fk")
                # what the old fold and prune left: the twin gone, its
                # mark still naming it
                self.admin.execute(
                    "DELETE FROM transactions WHERE tenant_id = %s"
                    " AND id = 'birch1'", (self.tid,))
                self.admin.execute(
                    "DELETE FROM merchants WHERE tenant_id = %s AND id = %s",
                    (self.tid, self.twin))
                self.admin.execute(sql.read_text())
                row = self.admin.execute(
                    "SELECT merchant_id::text AS mid FROM vendor_1099"
                    " WHERE tenant_id = %s", (self.tid,)).fetchone()
                self.assertIsNotNone(self.admin.execute(
                    "SELECT 1 FROM pg_constraint"
                    " WHERE conname = 'vendor_1099_merchant_fk'").fetchone())
                raise _Undo
        except _Undo:
            pass
        self.assertEqual(row["mid"], self.survivor)

    def test_a_deleted_merchant_leaves_the_mark_by_name(self):
        self.conn.execute("DELETE FROM transactions WHERE id = 'birch1'")
        self.conn.execute("DELETE FROM merchants WHERE id = %s", (self.twin,))
        self.assertEqual([(m["mid"], m["merchant"]) for m in self._mark()],
                         [(None, self.NAME)])


class BucketThenLineTests(_Biz):
    def test_a_bucket_alone_leaves_the_row_in_the_review(self):
        add_txn(self.conn, dt.date(YEAR, 5, 1), 80.0, "PAPER CO",
                account="biz", txn_id="paper")
        self.assertEqual(books.unclassified_count(self.conn, self.eid), 1)
        books.classify(self.conn, "paper", "operating")      # Books tab
        self.assertEqual(books.unclassified_count(self.conn, self.eid), 1)
        s = books.suggest_lines(self.conn, self.eid)
        self.assertEqual([r["txn_id"] for r in s], ["paper"])
        books.classify(self.conn, "paper", s[0]["suggested_bucket"],
                       sched_c_line="Supplies")
        self.assertEqual(books.unclassified_count(self.conn, self.eid), 0)

    def test_a_start_up_bucket_keeps_its_bucket_and_leaves_the_review(self):
        add_txn(self.conn, dt.date(YEAR, 5, 1), 80.0, "PAPER CO",
                account="biz", txn_id="paper")
        books.classify(self.conn, "paper", "startup_195")
        self.assertEqual(books.unclassified_count(self.conn, self.eid), 0)

    def test_the_suggestion_keeps_a_bucket_already_set(self):
        add_txn(self.conn, dt.date(YEAR, 5, 1), 80.0, "PAPER CO",
                account="biz", txn_id="paper")
        books.classify(self.conn, "paper", "operating")
        self.conn.execute("UPDATE business_entity SET business_start_date ="
                          " '2030-01-01' WHERE id = %s", (self.eid,))
        s = books.suggest_lines(self.conn, self.eid)
        self.assertEqual(s[0]["suggested_bucket"], "operating")


class FrontedChargeTests(_Biz):
    def test_a_business_accounts_charge_cannot_be_fronted(self):
        other = entities.create_entity(self.conn, name="Cedar Row LLC",
                                       structure="single_member_llc")
        add_txn(self.conn, dt.date(YEAR, 5, 1), 300.0, "LAPTOP STAND",
                account="biz", txn_id="b1")
        for fn in (equity.contribute_expense, equity.reimburse_expense):
            for ent in (other["id"], self.eid):
                with self.subTest(fn=fn.__name__, entity=ent):
                    with self.assertRaises(equity.FrontedExpenseConflict):
                        fn(self.conn, ent, "b1")
        self.assertIsNone(self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id='b1'"
        ).fetchone()["entity_id"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM equity_movement").fetchone()["n"], 0)

    def test_a_personal_charge_still_can(self):
        add_txn(self.conn, dt.date(YEAR, 5, 1), 300.0, "LAPTOP STAND",
                account="card", txn_id="p1")
        mv = equity.contribute_expense(self.conn, self.eid, "p1")
        self.assertEqual(mv["amount"], 300.0)

    def test_a_withdrawn_charges_movement_stops_counting_then_goes(self):
        add_txn(self.conn, dt.date(YEAR, 5, 1), 300.0, "LAPTOP STAND",
                account="card", txn_id="p1")
        equity.contribute_expense(self.conn, self.eid, "p1")
        self.assertEqual(equity.capital_summary(
            self.conn, self.eid)["contributions"], 300.0)
        stamp = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=10)).isoformat()
        self.conn.execute(
            "UPDATE transactions SET removed = 1, raw = %s WHERE id = 'p1'",
            (jsonb({"feed_removed_at": stamp}),))
        self.assertEqual(equity.capital_summary(
            self.conn, self.eid)["contributions"], 0.0)
        # still inside the twin wait: kept, in case it is carried
        self.assertEqual(equity.release_withdrawn_fronted(
            self.conn, ["p1"], older_than_days=30), 0)
        self.assertEqual(equity.release_withdrawn_fronted(
            self.conn, ["p1"], older_than_days=7), 1)
        self.assertEqual(equity.list_movements(self.conn, self.eid), [])
        said = self.conn.execute(
            "SELECT label FROM activity_log WHERE target = 'p1'").fetchone()
        self.assertIn("contribution", said["label"])

    def test_a_retired_hold_is_not_released(self):
        add_txn(self.conn, dt.date(YEAR, 5, 1), 300.0, "LAPTOP STAND",
                account="card", txn_id="p1")
        equity.contribute_expense(self.conn, self.eid, "p1")
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id='p1'")
        self.assertEqual(equity.release_withdrawn_fronted(
            self.conn, ["p1"]), 0)


class EntityPatchShapeTests(_Biz):
    def test_bad_shapes_are_value_errors(self):
        bad = ({"name": None}, {"name": "   "}, {"name": {"a": 1}},
               {"ein": 123456789}, {"state": ["ID"]},
               {"registered_agent": 5}, {"structure": {"x": 1}},
               {"fiscal_year_end": "June"}, {"fiscal_year_end": "13-45"},
               {"fiscal_year_end": "02-30"})
        for f in bad:
            with self.subTest(f=f):
                with self.assertRaises(ValueError):
                    entities.update_entity(self.conn, self.eid, **f)
        self.assertEqual(entities.get_entity(self.conn, self.eid)["name"],
                         "Harbor Lane Studio")

    def test_good_shapes_still_save(self):
        out = entities.update_entity(self.conn, self.eid, name=" Harbor Two ",
                                     fiscal_year_end="02-29", state=None)
        self.assertEqual(out["name"], "Harbor Two")
        self.assertEqual(out["fiscal_year_end"], "02-29")

    def test_create_refuses_the_same_shapes(self):
        with self.assertRaises(ValueError):
            entities.create_entity(self.conn, name=7, structure="sole_prop")
        with self.assertRaises(ValueError):
            entities.create_entity(self.conn, name="X", structure="sole_prop",
                                   fiscal_year_end="31-12")

    def test_require_active_locks_the_entity_row(self):
        with self.conn.transaction():
            entities.require_active(self.conn, self.eid)
            mode = self.conn.execute(
                "SELECT mode FROM pg_locks WHERE locktype = 'tuple' "
                "AND pid = pg_backend_pid()").fetchall()
            held = self.conn.execute(
                "SELECT 1 FROM pg_locks l JOIN pg_class c ON c.oid = l.relation"
                " WHERE l.pid = pg_backend_pid() AND c.relname = "
                "'business_entity' AND l.mode = 'RowShareLock'").fetchone()
        self.assertIsNotNone(held, mode)

    def test_require_active_outside_a_transaction_only_answers(self):
        self.assertEqual(entities.require_active(self.conn, self.eid)["id"],
                         self.eid)
        entities.archive_entity(self.conn, self.eid)
        with self.assertRaises(ValueError):
            entities.require_active(self.conn, self.eid)

    def test_a_door_write_holds_off_an_archive_until_it_commits(self):
        import psycopg
        from oikonome.web import api
        other = tenancy.tenant_connect(self.tid)
        try:
            other.execute("SET lock_timeout = '300ms'")
            with api._entity_write(self.conn, self.eid):
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    entities.archive_entity(other, self.eid)
            self.assertEqual(entities.archive_entity(
                other, self.eid)["status"], "archived")
        finally:
            # a pooled connection keeps a session setting into the next
            # test that draws it
            other.execute("RESET lock_timeout")
            other.close()

    def test_an_edit_racing_an_archive_does_not_land_in_the_closed_books(self):
        """The edit's early check reads the business as active while an
        archive is still uncommitted; once the archive commits, the edit
        must be refused rather than written into the archived row."""
        import threading
        other = tenancy.tenant_connect(self.tid)
        out: dict = {}

        def edit():
            try:
                out["r"] = entities.update_entity(
                    self.conn, self.eid, name="Renamed Studio")
            except ValueError as e:
                out["e"] = e
        try:
            with other.transaction():
                entities.archive_entity(other, self.eid)
                t = threading.Thread(target=edit)
                t.start()
                t.join(0.5)                 # the edit waits on the row
                self.assertTrue(t.is_alive())
            t.join(10)
            self.assertFalse(t.is_alive())
            self.assertIn("archived", str(out.get("e")), out)
            self.assertEqual(entities.get_entity(
                self.conn, self.eid)["name"], "Harbor Lane Studio")
        finally:
            other.close()

    def test_delete_forever_queues_behind_an_assignment_without_deadlock(self):
        """An assignment locks its account row and then reads the entity
        FOR SHARE; delete-forever takes the same rows before the entity,
        so the two wait in line instead of each holding the other's."""
        import threading
        other = tenancy.tenant_connect(self.tid)
        errors: list = []

        def delete():
            try:
                entities.delete_forever(other, self.eid, "Harbor Lane Studio")
            except Exception as e:                     # noqa: BLE001
                errors.append(e)
        try:
            with self.conn.transaction():
                self.conn.execute("SELECT id FROM accounts WHERE id = 'biz' "
                                  "FOR UPDATE")
                t = threading.Thread(target=delete)
                t.start()
                t.join(0.5)                 # the delete is now waiting
                self.assertTrue(t.is_alive())
                self.conn.execute("SET LOCAL lock_timeout = '3s'")
                self.assertFalse(entities._is_archived(self.conn, self.eid))
            t.join(10)
            self.assertFalse(t.is_alive())
            self.assertEqual(errors, [])
            self.assertIsNone(entities.get_entity(self.conn, self.eid))
        finally:
            other.close()


class MemberEditTests(_Biz):
    def test_a_member_can_be_changed_and_removed(self):
        m = entities.add_member(self.conn, self.eid, member_name="Pat Lee",
                                ownership_pct=5)
        out = entities.update_member(self.conn, self.eid, m["id"],
                                     ownership_pct=50, is_manager=True)
        self.assertEqual(out["ownership_pct"], 50.0)
        self.assertTrue(out["is_manager"])
        self.assertEqual(out["member_name"], "Pat Lee")
        mv = equity.record_movement(self.conn, self.eid, kind="contribution",
                                    amount=100, date=dt.date(YEAR, 1, 2),
                                    member_id=m["id"])
        self.assertTrue(entities.delete_member(self.conn, self.eid, m["id"]))
        self.assertEqual(entities.list_members(self.conn, self.eid), [])
        left = equity.list_movements(self.conn, self.eid)
        self.assertEqual([(x["id"], x["member_id"]) for x in left],
                         [(mv["id"], None)])
        self.assertEqual(equity.capital_summary(
            self.conn, self.eid)["contributions"], 100.0)

    def test_scoped_to_its_entity_and_shapes_checked(self):
        other = entities.create_entity(self.conn, name="Cedar Row LLC",
                                       structure="single_member_llc")
        m = entities.add_member(self.conn, self.eid, member_name="Pat Lee")
        self.assertIsNone(entities.update_member(
            self.conn, other["id"], m["id"], ownership_pct=10))
        self.assertFalse(entities.delete_member(self.conn, other["id"],
                                                m["id"]))
        self.assertIsNone(entities.update_member(
            self.conn, self.eid, "not-a-uuid", ownership_pct=10))
        for f in ({"ownership_pct": 101}, {"ownership_pct": "x"},
                  {"member_name": ""}, {"is_manager": "yes"},
                  {"member_name": {"a": 1}}):
            with self.subTest(f=f):
                with self.assertRaises(ValueError):
                    entities.update_member(self.conn, self.eid, m["id"], **f)


if __name__ == "__main__":
    unittest.main()
