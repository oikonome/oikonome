"""Compliance calendar — the derived Idaho annual-report rule,
custom obligations, and .ics export with reminders."""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import compliance, entities

from .util import _admin_dsn, _ensure_db, TEST_DB


class ComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"cmp-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        self.ent = entities.create_entity(
            self.conn, name="Example Studio LLC",
            structure="single_member_llc",
            state="ID", formation_date="2025-03-01")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_state_annual_report_due_end_of_anniversary_month(self):
        # the first report falls in the anniversary month of the year AFTER
        # formation, on that month's last day
        obls = compliance.obligations(self.conn, self.ent,
                                      today=dt.date(2026, 7, 22))
        self.assertEqual(len(obls), 1)
        o = obls[0]
        self.assertEqual(o["title"], "Idaho LLC annual report")
        self.assertEqual(o["due"], "2027-03-31")
        self.assertEqual(o["fee"], 0.0)
        self.assertEqual(o["source"], "state")

    def test_state_report_rolls_to_next_year_once_past(self):
        obls = compliance.obligations(self.conn, self.ent,
                                      today=dt.date(2027, 8, 1))
        self.assertEqual(obls[0]["due"], "2028-03-31")

    def test_a_state_the_table_does_not_know_has_no_derived_rule(self):
        other = entities.create_entity(
            self.conn, name="OR Co", structure="sole_prop", state="OR",
            formation_date="2026-01-01")
        self.assertEqual(
            compliance.obligations(self.conn, other,
                                   today=dt.date(2026, 7, 22)), [])

    def test_custom_obligation_and_ordering(self):
        compliance.add_obligation(self.conn, self.ent["id"],
                                  title="Federal Form 1040-SE",
                                  due_date=dt.date(2027, 4, 15))
        obls = compliance.obligations(self.conn, self.ent,
                                      today=dt.date(2026, 7, 22))
        # soonest first: the March annual report before the April federal filing
        self.assertEqual([o["title"] for o in obls],
                         ["Idaho LLC annual report", "Federal Form 1040-SE"])

    def test_ics_has_events_and_reminders(self):
        ics = compliance.ics(self.conn, self.ent, today=dt.date(2026, 7, 22))
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("DTSTART;VALUE=DATE:20270331", ics)
        self.assertIn("Idaho LLC annual report", ics)
        # the reminder ladder (30 + 7 days before)
        self.assertIn("TRIGGER:-P30D", ics)
        self.assertIn("TRIGGER:-P7D", ics)
        self.assertEqual(ics.count("BEGIN:VALARM"), 6)  # 3 occurrences × 2

    def test_ics_neutralizes_carriage_return_injection(self):
        """A bare CR in owner-controlled title/note is a line terminator to
        many .ics parsers — it must be escaped, not passed through, or an
        obligation can inject new calendar lines."""
        compliance.add_obligation(
            self.conn, self.ent["id"],
            title="Renewal\r\nBEGIN:VEVENT\rSUMMARY:evil",
            due_date=dt.date(2027, 3, 1), note="a\rb")
        ics = compliance.ics(self.conn, self.ent, today=dt.date(2026, 7, 22))
        ics.split("END:VCALENDAR")[0]
        # no bare CR may appear except as part of the CRLF line separator
        self.assertNotIn("\rSUMMARY:evil", ics)
        self.assertNotIn("\rBEGIN:VEVENT", ics)
        # every CR in the payload is immediately followed by LF (folding)
        for i, ch in enumerate(ics):
            if ch == "\r":
                self.assertEqual(ics[i + 1], "\n", "bare CR leaked into .ics")

    def test_estimated_tax_picks_jan15_q4_deadline_in_early_january(self):
        """From Jan 1-14, the imminent deadline is the Q4
        1040-ES payment on Jan 15 (which is quarterly_deadlines(prev_year)[3]).
        Searching only (this_year, next_year) skipped it and pointed at April."""
        ent = entities.create_entity(
            self.conn, name="SE LLC", structure="sole_prop")
        ent = entities.update_entity(self.conn, ent["id"], income_tax_rate=22.0)
        obls = compliance.obligations(self.conn, ent, today=dt.date(2027, 1, 10))
        fed = next(o for o in obls if o["source"] == "federal")
        self.assertEqual(fed["due"], "2027-01-15")

    def test_estimated_tax_picks_apr15_after_jan15_passes(self):
        ent = entities.create_entity(
            self.conn, name="SE LLC2", structure="sole_prop")
        ent = entities.update_entity(self.conn, ent["id"], income_tax_rate=22.0)
        obls = compliance.obligations(self.conn, ent, today=dt.date(2027, 1, 20))
        fed = next(o for o in obls if o["source"] == "federal")
        self.assertEqual(fed["due"], "2027-04-15")

    def test_next_occurrence_leap_day_clamp_and_rollover(self):
        """A yearly custom obligation due Feb 29
        must roll forward to Feb 28 on non-leap years (not 500) and to Feb 29
        on leap years."""
        # past-due Feb-29 obligation, viewed from a non-leap year → Feb 28
        self.assertEqual(
            compliance._next_occurrence(dt.date(2024, 2, 29), "yearly",
                                        dt.date(2027, 3, 1)),
            dt.date(2028, 2, 29))          # next occurrence at/after Mar 2027
        self.assertEqual(
            compliance._next_occurrence(dt.date(2024, 2, 29), "yearly",
                                        dt.date(2026, 6, 1)),
            dt.date(2027, 2, 28))          # 2027 not leap → clamp to 28

    def test_ics_multi_year_dates_are_distinct_and_sequential(self):
        occ = compliance._state_annual_report(self.ent, dt.date(2026, 7, 22),
                                              n=3)
        dues = [o["due"] for o in occ]
        self.assertEqual(dues, [dt.date(2027, 3, 31), dt.date(2028, 3, 31),
                                dt.date(2029, 3, 31)])

    def test_add_obligation_rejects_non_numeric_fee(self):
        """A non-numeric fee must surface a clean ValueError (→ 400), not a
        raw psycopg DataError → 500."""
        with self.assertRaises(ValueError):
            compliance.add_obligation(
                self.conn, self.ent["id"], title="x",
                due_date=dt.date(2027, 1, 1), fee="abc")


if __name__ == "__main__":
    unittest.main()
