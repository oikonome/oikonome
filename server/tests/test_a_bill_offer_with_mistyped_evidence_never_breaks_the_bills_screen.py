"""A bill offer's evidence is read by type. A restored archive (or a
hand-edited row) can carry a price change whose amounts are words, a day
that is not a date, monthly totals that are a list, a bill or merchant
named by a list or a map. None of it may turn the Bills screen into a 500:
the restore keeps each value only in the type the detector writes, and the
summary reads a value of another shape as no evidence.
"""
import json
import unittest

from oikonome.engine import bills
from oikonome.sync import export, restore
from oikonome.web.pages import _proposal_summary

from .util import jsonb, make_db, write_config

BAD = [
    {"charged_on": "2026-01-05", "old": 5, "new": "n/a"},
    {"charged_on": "x", "old": 5, "new": 6},
    {"charged_on": 20260105, "old": 5, "new": 6},
    {"monthly_totals": [1, 2]},
    {"monthly_totals": {"2026-01": "x"}},
    {"monthly_totals": {"2026-01": None, "2026-02": True}},
    {"old": float("inf"), "new": 2, "charged_on": "2026-01-05"},
]


class MistypedBillOfferEvidenceTests(unittest.TestCase):
    def test_the_summary_reads_a_mistyped_value_as_no_evidence(self):
        for ev in BAD:
            with self.subTest(ev=ev):
                self.assertIsInstance(_proposal_summary({"evidence": ev}), str)

    def test_the_summary_still_reads_well_typed_evidence(self):
        s = _proposal_summary({"evidence": {
            "charged_on": "2026-01-05", "old": 10, "new": 12.5,
            "monthly_totals": {"2026-01": 40, "2026-02": 50}}})
        self.assertIn("charged $12.50 on 01/05/26, the bill says $10.00", s)
        self.assertIn("$40–$50 a month over 2 months", s)

    def test_a_restore_keeps_only_well_typed_evidence(self):
        src = make_db()
        write_config(src)
        dest = make_db()
        try:
            # jsonb itself refuses a non-finite number, so none is stored
            rows = BAD[:-1] + [
                {"bill_payee": ["a"], "merchant_name": {"x": 1}},
                {"charged_on": "2026-01-05", "old": 10, "new": 12.5,
                 "monthly_totals": {"2026-01": 40, "2026-02": "x"},
                 "bill_payee": "PAPER LANTERN"}]
            for i, ev in enumerate(rows):
                src.execute(
                    "INSERT INTO bill_proposals (id, kind, payee, amount, "
                    "evidence, status) VALUES (%s, %s, %s, 10, %s, "
                    "'pending')",
                    (f"prop:typed:{i}", "merchant" if i == len(rows) - 2 else
                     "add", "PAPER LANTERN", jsonb(ev)))
            restore.restore_zip(dest, export.build_zip(src))
            got = {r["id"]: r for r in dest.execute(
                "SELECT id, payee, kind, evidence FROM bill_proposals "
                "WHERE id LIKE 'prop:typed:%'").fetchall()}
            self.assertEqual(len(got), len(rows))
            for r in got.values():
                ev = r["evidence"]
                for k in ("old", "new"):
                    if k in ev:
                        self.assertIsInstance(ev[k], (int, float))
                self.assertIsInstance(ev.get("monthly_totals", {}), dict)
                self.assertNotIn("x", json.dumps(ev.get("monthly_totals")))
                self.assertIsInstance(ev.get("bill_payee", ""), str)
                self.assertIsInstance(ev.get("merchant_name", ""), str)
                self.assertIsInstance(_proposal_summary(r), str)
                if r["kind"] == "merchant":
                    self.assertIsInstance(
                        bills.merchant_offer_summary(dest, r), str)
            kept = got[f"prop:typed:{len(rows) - 1}"]["evidence"]
            self.assertEqual(kept["new"], 12.5)
            self.assertEqual(kept["charged_on"], "2026-01-05")
            self.assertEqual(kept["monthly_totals"], {"2026-01": 40})
        finally:
            src.close()
            dest.close()

class AnOfferWithAnImpossibleLastSeenCanStillBeApproved(unittest.TestCase):
    """Restore keeps last_seen only when it reads as a date: the approve path
    hands it to the bill writer as one."""

    def test_restore_drops_an_impossible_last_seen(self):
        from oikonome.sync import restore
        ev = restore._offer_evidence_typed({"old": 10.0, "new": 12.0,
                                            "last_seen": "2026-02-30",
                                            "bill_payee": "Invented Gym"})
        self.assertNotIn("last_seen", ev)
        self.assertEqual(ev["bill_payee"], "Invented Gym")
        ok = restore._offer_evidence_typed({"last_seen": "2026-02-28"})
        self.assertEqual(ok["last_seen"], "2026-02-28")

class ARestoredRecurrenceDropsAnImpossibleEndDate(unittest.TestCase):
    def test_the_cleaner_drops_it_and_keeps_a_real_one(self):
        from oikonome.sync import restore
        bad = restore.clean_bill_raw({"recurrence": {"frequency": "MONTHLY",
                                                     "endOn": "2026-02-30"}})
        self.assertNotIn("endOn", bad["recurrence"])
        good = restore.clean_bill_raw({"recurrence": {"frequency": "MONTHLY",
                                                      "endOn": "2026-02-28"}})
        self.assertEqual(good["recurrence"]["endOn"], "2026-02-28")
        far = restore.clean_bill_raw({"recurrence": {"frequency": "MONTHLY",
                                                     "endOn": "9999-12-31"}})
        self.assertNotIn("endOn", far["recurrence"])

    def test_an_impossible_floor_is_dropped_and_the_night_still_runs(self):
        import datetime as dt
        from oikonome.sync import restore
        from oikonome.engine import bills
        from .util import add_bill, make_db, write_config
        raw = restore.clean_bill_raw({"lastDueOn": "2026-02-30", "dueOn": "x",
                                      "recurrence": {"frequency": "MONTHLY"}})
        self.assertNotIn("lastDueOn", raw)
        self.assertNotIn("dueOn", raw)
        # a row written before the cleaner learned this is still read
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Invented Payroll", 2500.0, income=True,
                 frequency="MONTHLY", interval=1,
                 next_due=dt.date(2026, 11, 1), last_seen=dt.date(2026, 10, 1))
        conn.execute("UPDATE bills SET raw = jsonb_set(raw, '{lastDueOn}', "
                     "'\"2026-02-30\"') WHERE payee = 'Invented Payroll'")
        bills.advance_received_income(conn, dt.date(2026, 11, 8))

class AOneTimeBillWithAnImpossibleStoredDueDateStillReads(unittest.TestCase):
    """Rows written before restore validated dates may carry February 30
    in raw dueOn: the Bills analysis, the nightly pass and an amount-only
    edit read it as no date — never a 500, never a lost night."""

    def test_analysis_nightly_and_edit_survive(self):
        import datetime as dt
        from oikonome.engine import bills
        from .util import add_bill, make_db, write_config
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Invented Dentist", 180.0, frequency=None,
                 next_due=dt.date(2026, 12, 1))
        conn.execute("UPDATE bills SET raw = jsonb_set(raw, '{dueOn}', "
                     "'\"2026-02-30\"') WHERE payee = 'Invented Dentist'")
        today = dt.date(2026, 11, 8)
        bills.analyze_bills(conn, today)
        bills.run(conn, today)
        bills.save_bill(conn, payee="Invented Dentist", amount=185.0,
                        frequency=None, next_due=None, last_seen="2026-02-30")
        kept = conn.execute("SELECT due_on FROM bills WHERE payee = 'Invented Dentist'").fetchone()
        self.assertIsNotNone(kept)
        self.assertEqual(bills.stored_date("2026-02-30"), None)
        self.assertEqual(bills.stored_date("2026-02-28"), dt.date(2026, 2, 28))


if __name__ == "__main__":
    unittest.main()


class AnOfferWithAnImpossibleCycleStillLists(unittest.TestCase):
    """A restored offer whose measured cycle or cadence runs past the
    longest cycle a bill may have is still listed: the lapse rule reads it
    as no cycle instead of overflowing the date, and the restore brings the
    cycle back inside the bound."""

    def test_the_lapse_rule_never_overflows(self):
        import datetime as dt
        today = dt.date(2026, 11, 8)
        for p, ev in (
                ({"kind": "add", "next_due": "2026-10-01",
                  "frequency": "MONTHLY", "interval": 1},
                 {"cycle_days": 5_000_000}),
                ({"kind": "add", "next_due": "2026-10-01",
                  "frequency": "YEARLY", "interval": 10_000}, {}),
                ({"kind": "add", "next_due": "2026-10-01",
                  "frequency": "DAILY", "interval": 2_000_000_000}, {}),
                ({"kind": "add", "next_due": "9999-12-01",
                  "frequency": "MONTHLY", "interval": 1}, {})):
            with self.subTest(p=p, ev=ev):
                self.assertFalse(bills.offer_lapsed(p, ev, today))

    def test_a_restored_offer_with_an_impossible_cycle_lists(self):
        import datetime as dt
        src = make_db()
        write_config(src)
        dest = make_db()
        self.addCleanup(src.close)
        self.addCleanup(dest.close)
        for i, (payee, freq, iv, ev) in enumerate((
                ("LANTERN GYM", "MONTHLY", 1, {"cycle_days": 5_000_000}),
                ("HARBOR PARKING", "YEARLY", 10_000, {}))):
            src.execute(
                "INSERT INTO bill_proposals (id, kind, payee, amount, "
                "frequency, interval, next_due, evidence, status) VALUES "
                "(%s, 'add', %s, 10, %s, %s, '2026-11-01', %s, 'pending')",
                (f"prop:cycle:{i}", payee, freq, iv,
                 jsonb(ev)))
        restore.restore_zip(dest, export.build_zip(src))
        rows = {r["id"]: r for r in dest.execute(
            "SELECT id, frequency, interval, evidence FROM bill_proposals "
            "WHERE id LIKE 'prop:cycle:%'").fetchall()}
        self.assertEqual(len(rows), 2)
        for r in rows.values():
            self.assertFalse(bills.cadence_too_long(r["frequency"],
                                                    r["interval"]))
            self.assertLessEqual(r["evidence"].get("cycle_days", 0),
                                 bills.MAX_CYCLE_DAYS)
        listed = bills.pending_proposals(dest, dt.date(2026, 11, 8))
        self.assertTrue({"prop:cycle:0", "prop:cycle:1"}
                        <= {p["id"] for p in listed})
