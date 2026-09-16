"""Detection-engine invariants: cycle fitting, envelope fingerprint,
proposals lifecycle, drift, removal timing, due-date advancement.

These freeze shipped behaviour — do not weaken one to make a change pass.
Fixtures are tenant-per-test; json raw round-trips through compat (as_dict /
jsonb) and due_on is asserted as a date object (it is a DATE column).
"""

import datetime as dt
import unittest

from oikonome.engine import bills as rd
from oikonome.engine.compat import as_dict, jsonb

from . import util
from .util import TODAY, add_bill, add_txn, make_db, write_config


def ev(date, amount):
    return {"date": date, "amount": amount, "payee": "X",
            "category": "GENERAL_SERVICES", "is_food": False}


class TestFitters(unittest.TestCase):
    def test_weekly_fit(self):
        dates = [dt.date(2026, 5, 4) + dt.timedelta(days=7 * i) for i in range(8)]
        cyc, run = rd._fit_cycle(dates)
        self.assertEqual(cyc[0], 7)
        self.assertEqual(len(run), 8)

    def test_density_guard_kills_noise(self):
        """Dense shopping noise must not thread a fake 6-month cycle."""
        dates = sorted(dt.date(2025, 1, 1) + dt.timedelta(days=11 * i)
                       for i in range(50))
        fit = rd._fit_cycle(dates)
        if fit:
            self.assertLess(fit[0][0], 30)

    def test_amount_core_drops_outlier(self):
        events = [ev(dt.date(2026, 1, 1), 5000), ev(dt.date(2026, 1, 15), 5000),
                  ev(dt.date(2026, 2, 1), 41000), ev(dt.date(2026, 2, 15), 5000)]
        core, med = rd._amount_core(events)
        self.assertEqual(len(core), 3)             # bonus excluded
        self.assertEqual(med, 5000)

    def test_stable_max_deviation_not_mad(self):
        """[3700, 3760, 895] must NOT read as stable."""
        self.assertFalse(rd._stable([3700.00, 3760.00, 895.0]))
        self.assertTrue(rd._stable([120.0, 130.0, 140.0]))

    def test_refill_like(self):
        refills = [ev(TODAY, a) for a in (30, 30, 50, 30, 50, 30)]
        varied = [ev(TODAY, a) for a in (28.00, 21.00, 22.00, 24.00, 14.00, 35.00)]
        self.assertTrue(rd._refill_like(refills))
        self.assertFalse(rd._refill_like(varied))

    def test_complete_months_year_wrap(self):
        self.assertEqual(rd._complete_months(dt.date(2026, 2, 10), 3),
                         ["2025-11", "2025-12", "2026-01"])


class DetectBase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def pending(self, kind=None):
        q = "SELECT * FROM bill_proposals WHERE status='pending'"
        if kind:
            q += f" AND kind='{kind}'"
        return self.conn.execute(q).fetchall()


class TestProposals(DetectBase):
    def _monthly_series(self, name, amount, months=5, day=8):
        for i in range(months):
            m = TODAY.month - months + i
            y, m = (TODAY.year, m) if m >= 1 else (TODAY.year - 1, m + 12)
            add_txn(self.conn, dt.date(y, m, day), amount, name)

    def test_monthly_bill_proposed_and_idempotent(self):
        self._monthly_series("SPOTIFY", 11.99)
        rd.run(self.conn, TODAY)
        adds = self.pending("add")
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["payee"], "SPOTIFY")
        s2 = rd.run(self.conn, TODAY)
        self.assertEqual(s2["proposed_add"], 0)     # second run adds nothing

    def test_rejection_memory(self):
        self._monthly_series("SPOTIFY", 11.99)
        rd.run(self.conn, TODAY)
        pid = self.pending("add")[0]["id"]
        rd.apply_proposal(self.conn, pid, "reject")
        rd.run(self.conn, TODAY)
        self.assertEqual(len(self.pending("add")), 0)

    def test_approve_creates_bill(self):
        self._monthly_series("SPOTIFY", 11.99)
        rd.run(self.conn, TODAY)
        pid = self.pending("add")[0]["id"]
        rd.apply_proposal(self.conn, pid, "approve")
        bill = rd.get_bill(self.conn, "SPOTIFY")
        self.assertEqual(bill["amount"], -11.99)
        self.assertEqual(bill["active"], 1)

    def test_city_named_membership_survives_a_stray_lunch(self):
        """The group key is one lead token, so a city-named gym shares its
        key with every café in that city. Two lunches must not veto a
        twelve-hit exact-amount monthly membership: food is judged on the
        amount core, by majority."""
        self._monthly_series("SPRINGFIELD REC CENTER", 120.00, months=6, day=2)
        add_txn(self.conn, TODAY - dt.timedelta(days=40), 115.00,
                "SPRINGFIELD TAP HOUSE", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY - dt.timedelta(days=9), 95.00,
                "SPRINGFIELD TAP HOUSE", primary="FOOD_AND_DRINK")
        rd.run(self.conn, TODAY)
        adds = self.pending("add")
        self.assertEqual([p["payee"] for p in adds], ["SPRINGFIELD REC CENTER"])
        self.assertEqual(adds[0]["amount"], 120.00)
        self.assertEqual(adds[0]["bill_type"], "occurrence")

    def test_variable_utility_becomes_an_envelope(self):
        """A power bill: every month, an amount that moves with the season,
        one billing date that slips across a month end (two bills in one
        month, none the next). No steady occurrence there — the honest
        shape is an envelope over the six-month average: a year of monthly
        hits with no two alike must propose nothing else."""
        amts = [430.00, 380.00, 210.00, 95.00, 80.00, 150.00, 290.00]
        for i, amt in enumerate(amts):
            add_txn(self.conn, TODAY - dt.timedelta(days=15 + 30 * i), amt,
                    "CITY POWER", primary="RENT_AND_UTILITIES")
        rd.run(self.conn, TODAY)
        adds = [p for p in self.pending("add") if p["payee"] == "CITY POWER"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["bill_type"], "envelope")
        self.assertGreater(adds[0]["amount"], 150)

    def test_long_cycle_two_hits_need_the_same_merchant(self):
        """Two parking lots six months apart share a city token; that is
        not a semi-annual bill. At the two-hit minimum the merchant string
        must repeat, and every run hit must carry the near-exact amount."""
        add_txn(self.conn, TODAY - dt.timedelta(days=200), 50.00,
                "SPRINGFIELD DOWNTOWN PARKING", primary="ENTERTAINMENT")
        add_txn(self.conn, TODAY - dt.timedelta(days=18), 55.00,
                "SPRINGFIELD INTERNATIONAL AIRPORT", primary="TRAVEL")
        rd.run(self.conn, TODAY)
        self.assertEqual([p["payee"] for p in self.pending("add")], [])

    def test_food_needs_refill_fingerprint(self):
        self._monthly_series("BISTRO", 45.00)       # varied-ish single monthly
        for i, amt in enumerate((28.1, 33.9, 41.2, 19.8, 55.3, 24.6)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + i * 12), amt,
                    "BISTRO", primary="FOOD_AND_DRINK")
        rd.run(self.conn, TODAY)
        self.assertEqual([p["payee"] for p in self.pending("add")], [])


class TestMaintenance(DetectBase):
    def _bill_with_history(self, last_gap_days):
        add_bill(self.conn, "Netflix", 20.0, next_due=TODAY + dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=last_gap_days))
        for i in range(4):
            add_txn(self.conn, TODAY - dt.timedelta(days=last_gap_days + 30 * i),
                    20.0, "NETFLIX")

    def test_fresh_import_does_not_drift(self):
        """A bill approved from a just-imported ledger has nothing to
        drift FROM: every hit the median sees predates the approval. The
        proposal carries the current price (median of the last three
        on-cycle hits — the same estimator drift uses), so approving it and
        running the finder again must change nothing — otherwise 'drift'
        fires a minute after approve. Once a NEW hit
        lands after the approval, drift is back on."""
        # a price that rose over the history: 12.99 → 15.49 in the recent
        # three; whole-history median would be the old price
        for i, amt in enumerate((15.49, 15.49, 15.49, 12.99, 12.99, 12.99)):
            add_txn(self.conn, TODAY - dt.timedelta(days=12 + 30 * i), amt,
                    "NETFLIX.COM")
        rd.run(self.conn, TODAY)
        p = next(x for x in self.pending("add") if "NETFLIX" in x["payee"])
        self.assertEqual(p["amount"], 15.49)          # current price
        rd.apply_proposal(self.conn, p["id"], "approve")
        rd.run(self.conn, TODAY)                        # the very next pass
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM bill_proposals "
            "WHERE kind='amount_drift'").fetchone()["n"], 0)
        self.assertEqual(rd.get_bill(self.conn, "NETFLIX.COM")["amount"],
                         -15.49)
        # a new occurrence AFTER the approval, at a new price → drift again
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 17.99, "NETFLIX.COM")
        add_txn(self.conn, TODAY, 17.99, "NETFLIX.COM")
        rd.run(self.conn, TODAY)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM bill_proposals "
            "WHERE kind='amount_drift'").fetchone()["n"], 1)

    def test_malformed_amount_set_on_does_not_crash_detection(self):
        # a restore ZIP hands bills.raw over verbatim; a bad stamp is a
        # legacy row (no drift gate), never a nightly-pass exception
        add_bill(self.conn, "City Power", 440.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        self.conn.execute("""UPDATE bills SET raw = raw || '{"amount_set_on":
                             "not-a-date"}'::jsonb WHERE payee='City Power'""")
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt, "CITY POWER")
        rd.run(self.conn, TODAY)                        # must not raise
        self.assertEqual(rd.get_bill(self.conn, "City Power")["amount"], -370.0)

    def test_removal_at_two_cycles_not_before(self):
        self._bill_with_history(45)
        rd.run(self.conn, TODAY)
        self.assertEqual(len(self.pending("remove")), 0)
        self.conn.execute("DELETE FROM bill_proposals")
        self.conn.execute("DELETE FROM transactions")
        util._n[0] += 100
        self._bill_with_history(65)
        rd.run(self.conn, TODAY)
        self.assertEqual([p["payee"] for p in self.pending("remove")], ["Netflix"])

    def test_drift_auto_applies_with_audit(self):
        add_bill(self.conn, "City Power", 440.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt, "CITY POWER")
        rd.run(self.conn, TODAY)
        bill = rd.get_bill(self.conn, "City Power")
        self.assertEqual(bill["amount"], -370.0)
        audits = self.conn.execute(
            "SELECT * FROM bill_proposals WHERE kind='amount_drift' "
            "AND status='auto'").fetchall()
        self.assertEqual(len(audits), 1)

    def test_drift_notice_clears_once_the_amount_is_changed_again(self):
        """The drift alert is "your plan moved and you haven't looked at
        it", not a 24-hour digest.

        A condition of only "a drift was auto-applied recently" would
        survive the user editing the bill, since it stays true whatever
        they do. Once the amount no longer matches what the drift wrote,
        the user has been there and decided.
        """
        add_bill(self.conn, "City Power", 440.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt, "CITY POWER")
        rd.run(self.conn, TODAY)
        # still carrying the drifted amount -> the notice stands
        self.assertEqual([d["payee"] for d in rd.recent_auto_changes(self.conn)],
                         ["City Power"])
        # the user edits it to something else, by any route
        self.conn.execute("UPDATE bills SET amount = -400.0 WHERE payee='City Power'")
        self.assertEqual(rd.recent_auto_changes(self.conn), [])

    def test_drift_notice_clears_when_hand_edited_to_the_drifted_value(self):
        """The common case, because the alert is what sent them there: the
        user opens the bill, agrees with the new figure, and saves it
        unchanged. The amount then still matches what the drift wrote, so
        only the hand-edit stamp can tell that a decision was made."""
        add_bill(self.conn, "City Power", 440.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt, "CITY POWER")
        rd.run(self.conn, TODAY)
        self.assertEqual([d["payee"] for d in rd.recent_auto_changes(self.conn)],
                         ["City Power"])
        self.conn.execute(
            """UPDATE bills SET raw = jsonb_set(COALESCE(raw,'{}'::jsonb),
                   '{manual_edited_at}', to_jsonb(CURRENT_DATE::text))
               WHERE payee='City Power'""")
        self.assertEqual(rd.recent_auto_changes(self.conn), [])

    def test_drift_notice_survives_an_unrelated_edit(self):
        """Only THIS bill's amount answers this alert — editing another
        bill must not silence it."""
        add_bill(self.conn, "City Power", 440.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        add_bill(self.conn, "Netflix", 15.0, next_due=TODAY + dt.timedelta(days=5))
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt, "CITY POWER")
        rd.run(self.conn, TODAY)
        self.conn.execute("UPDATE bills SET amount = -19.0 WHERE payee='Netflix'")
        self.assertEqual([d["payee"] for d in rd.recent_auto_changes(self.conn)],
                         ["City Power"])

    def test_manual_edit_gets_drift_grace(self):
        """A hand-edited amount is drift-immune for GRACE_DAYS."""
        add_bill(self.conn, "City Power", 440.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25), source="manual")
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt, "CITY POWER")
        rd.run(self.conn, TODAY)
        self.assertEqual(rd.get_bill(self.conn, "City Power")["amount"], -440.0)

    def test_grace_expires(self):
        add_bill(self.conn, "City Power", 440.0, next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25), source="manual")
        # age the stamp past the window
        row = rd.get_bill(self.conn, "City Power")
        raw = as_dict(row["raw"])
        raw["manual_edited_at"] = (TODAY - dt.timedelta(days=rd.GRACE_DAYS + 1)).isoformat()
        self.conn.execute("UPDATE bills SET raw=%s WHERE id=%s",
                          (jsonb(raw), row["id"]))
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt, "CITY POWER")
        rd.run(self.conn, TODAY)
        self.assertEqual(rd.get_bill(self.conn, "City Power")["amount"], -370.0)

    def test_proposal_apply_does_not_stamp_grace(self):
        """Approving a machine proposal on a manual bill must not (re)start
        the drift-grace window — only hand edits stamp manual_edited_at."""
        rd.save_bill(self.conn, payee="Gym", amount=50.0, frequency="MONTHLY",
                     next_due=TODAY + dt.timedelta(days=5),
                     source="manual", manual_touch=False)
        raw = as_dict(rd.get_bill(self.conn, "Gym")["raw"])
        self.assertNotIn("manual_edited_at", raw)

    def test_income_never_drifts(self):
        add_bill(self.conn, "Paycheck Co", 5000.0, frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=2), income=True,
                 last_seen=TODAY - dt.timedelta(days=12))
        for i in range(3):
            add_txn(self.conn, TODAY - dt.timedelta(days=5 + 14 * i), -6000.0,
                    "PAYCHECK CO PAYROLL", account="chk", primary="INCOME")
        rd.run(self.conn, TODAY)
        self.assertEqual(rd.get_bill(self.conn, "Paycheck Co")["amount"], 5000.0)


class TestDueAdvance(DetectBase):
    def test_monthly_advances(self):
        add_bill(self.conn, "Netflix", 20.0,
                 next_due=TODAY - dt.timedelta(days=5),   # 07-10, past
                 last_seen=TODAY - dt.timedelta(days=35))
        rd.run(self.conn, TODAY)
        bill = rd.get_bill(self.conn, "Netflix")
        self.assertEqual(bill["due_on"], dt.date(2026, 8, 10))

    def test_new_bill_floor_promoted(self):
        """A hand-added bill (lastDueOn=None) whose first due date passes must
        gain lastDueOn=old-due so the floor doesn't travel forward."""
        add_bill(self.conn, "NewLease", 900.0, next_due=TODAY - dt.timedelta(days=1))
        rd.run(self.conn, TODAY)
        raw = as_dict(rd.get_bill(self.conn, "NewLease")["raw"])
        self.assertEqual(raw["lastDueOn"], str(TODAY - dt.timedelta(days=1)))
        self.assertEqual(raw["dueOn"], "2026-08-14")


class TestBillCrud(DetectBase):
    def test_save_round_trips(self):
        add_bill(self.conn, "Env", 130.0, bill_type="envelope")
        b = rd.get_bill(self.conn, "Env")
        self.assertEqual((b["amount"], b["frequency"], b["due_on"]),
                         (-130.0, "ENVELOPE", None))
        add_bill(self.conn, "Once", 50.0, frequency="ONE_TIME",
                 next_due=TODAY + dt.timedelta(days=10))
        raw = as_dict(rd.get_bill(self.conn, "Once")["raw"])
        self.assertEqual(raw["recurrence"], {})
        add_bill(self.conn, "Pay", 5000.0, frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=3), income=True)
        self.assertEqual(rd.get_bill(self.conn, "Pay")["amount"], 5000.0)

    def test_archive_restore_delete(self):
        add_bill(self.conn, "Temp", 9.0, next_due=TODAY + dt.timedelta(days=3))
        rd.archive_bill(self.conn, "Temp")
        self.assertEqual(rd.get_bill(self.conn, "Temp")["active"], 0)
        rd.restore_bill(self.conn, "Temp")
        self.assertEqual(rd.get_bill(self.conn, "Temp")["active"], 1)
        rd.delete_bill(self.conn, "Temp")
        self.assertIsNone(rd.get_bill(self.conn, "Temp"))

    def test_rename_moves_id(self):
        add_bill(self.conn, "Old Name", 9.0, next_due=TODAY + dt.timedelta(days=3))
        rd.rename_bill(self.conn, "Old Name", "New Name")
        self.assertIsNone(rd.get_bill(self.conn, "Old Name"))
        self.assertEqual(rd.get_bill(self.conn, "New Name")["id"], "rec:new-name")


class TestMerchantBackfill(unittest.TestCase):
    """bills.merchant is derived from the feed as the token INTERSECTION
    of the bill's real charges."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_backfill_intersection_absorbs_variant_and_address(self):
        # same merchant, two variants (one with an address) -> intersection
        # drops the address/variant tokens, leaving the stable core
        add_txn(self.conn, TODAY, 72.0, "WOOF PACK PET SPA CEDAR PLAZA",
                merchant="Woof Pack Pet Spa")
        add_txn(self.conn, TODAY - dt.timedelta(days=30), 72.0, "WOOF PACK PET SPA",
                merchant="Woof Pack Pet Spa")
        add_bill(self.conn, "Woof Pack Pet Spa", 72.0,
                 next_due=TODAY, last_seen=TODAY)
        n = rd.backfill_merchants(self.conn, today=TODAY)
        self.assertEqual(n, 1)
        m = self.conn.execute(
            "SELECT merchant FROM bills WHERE payee='Woof Pack Pet Spa'"
        ).fetchone()["merchant"]
        self.assertEqual(set(m.split()), {"woof", "pack"})

    def test_backfill_preserved_across_save_bill(self):
        add_bill(self.conn, "X", 9.0, next_due=TODAY)
        self.conn.execute("UPDATE bills SET merchant='woof pack' WHERE payee='X'")
        add_bill(self.conn, "X", 12.0, next_due=TODAY)          # edit amount
        self.assertEqual(
            self.conn.execute("SELECT merchant FROM bills WHERE payee='X'"
                              ).fetchone()["merchant"], "woof pack")


if __name__ == "__main__":
    unittest.main()


class ProposalDoubleApproveTests(unittest.TestCase):
    """Approving a proposal twice must apply it once.

    The status read and the act on it were a plain read followed by a write
    with nothing between them — so a double-tap on Approve, or the same
    proposal open in two tabs, both saw 'pending' and both ran the apply. An
    add-proposal created the bill twice; an amount change applied twice.
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _pending_add(self, payee="DOUBLE TAP GYM"):
        pid = "prop-double-tap"
        rd._insert_proposal(
            self.conn, pid=pid, kind="add", payee=payee, amount=40.0,
            bill_type="fixed", frequency="monthly", interval=1,
            next_due=TODAY + dt.timedelta(days=7), evidence={})
        return pid

    def test_the_second_approve_is_refused(self):
        pid = self._pending_add()
        first = rd.apply_proposal(self.conn, str(pid), "approve")
        self.assertNotIn("error", first, first)
        second = rd.apply_proposal(self.conn, str(pid), "approve")
        self.assertIn("error", second)
        self.assertIn("already-decided", second["error"])
        n = self.conn.execute(
            "SELECT count(*) AS n FROM bills WHERE payee=%s",
            ("DOUBLE TAP GYM",)).fetchone()["n"]
        self.assertEqual(n, 1, "the bill was created once")

    def test_the_decision_is_taken_under_a_row_lock(self):
        """Structural: without FOR UPDATE the check above is a race that
        only loses under concurrency, which a single-threaded test cannot
        reproduce reliably."""
        import inspect
        src = inspect.getsource(rd.apply_proposal)
        self.assertIn("FOR UPDATE", src)
        self.assertIn("with conn.transaction():", src)
