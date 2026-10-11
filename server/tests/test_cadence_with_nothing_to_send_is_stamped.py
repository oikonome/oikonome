"""A scheduled cadence the worker decides not to send is stamped as done.

Two decisions, both made on purpose and both final for the period:
  * nobody to mail — no confirmed, unmuted address — with no other channel
    wanting the cadence: the report is not built at all (a whole-ledger
    build for nobody, every hour of the send day and of the monthly
    catch-up days, is the failure) and the cadence is stamped with a note;
  * a daily verdict skipped because no budget is set: stamped with that
    note, so the cadence stops coming due every hour and Doctor does not
    read the silence as a relay that never sent.
The manual "email me now" button is untouched by both.
"""

import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.web import lens_email, report

from .util import accept_recipient_invite, make_db


class CadenceWithNothingToSendTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def tearDown(self):
        self.conn.close()

    def _note(self, job):
        row = self.conn.execute("SELECT note FROM job_runs WHERE job=%s",
                                (job,)).fetchone()
        return row and row["note"]

    def test_nobody_to_mail_stamps_the_monthly_card_without_building_it(self):
        budget.save_config(self.conn, {"food_monthly": 800,
                                       "dynamic_variable_budget": False})
        with mock.patch.object(worker, "_recipient_lists",
                               return_value=([], [])), \
             mock.patch.object(lens_email, "build_monthly") as build, \
             mock.patch.object(report, "send_each") as send:
            self.assertEqual(
                worker.email_tenant(self.tid, cadence="monthly"), "")
        build.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self._note("monthly-email"), worker.NO_RECIPIENTS)

    def test_email_me_now_with_nobody_to_mail_still_says_so(self):
        with mock.patch.object(worker, "_recipient_lists",
                               return_value=([], [])), \
             mock.patch.object(report, "send_each"):
            with self.assertRaises(worker.NoRecipients):
                worker.email_tenant(self.tid, cadence="monthly",
                                    force_email=True)

    def test_a_daily_skipped_for_no_budget_is_stamped_with_why(self):
        budget.save_config(self.conn, {"email_recipients": ["one@example.com"],
                                       "dynamic_variable_budget": False})
        accept_recipient_invite(self.tid, "one@example.com")
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.example.com"}), \
             mock.patch.object(report, "send_each") as send:
            self.assertEqual(worker.email_tenant(self.tid), "")
        send.assert_not_called()
        self.assertEqual(self._note("daily-email"), worker.NO_BUDGET)

    def _doctor_daily_row(self):
        from oikonome.web import doctor
        rows = [r for r in doctor.checks(self.tid)
                if r.get("name") == "daily email (worker)"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_doctor_reads_a_no_budget_day_as_a_decision_not_smtp(self):
        budget.save_config(self.conn, {"email_recipients": ["one@example.com"],
                                       "dynamic_variable_budget": False})
        accept_recipient_invite(self.tid, "one@example.com")
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.example.com"}), \
             mock.patch.object(report, "send_each"):
            worker.email_tenant(self.tid)
        row = self._doctor_daily_row()
        self.assertTrue(row["ok"])
        self.assertIn("no budget is set", row["detail"])
        self.assertNotIn("SMTP", row["detail"])

    def test_doctor_reads_a_day_with_nobody_to_mail_as_a_decision(self):
        budget.save_config(self.conn, {"food_monthly": 800,
                                       "dynamic_variable_budget": False})
        with mock.patch.object(worker, "_recipient_lists",
                               return_value=([], [])), \
             mock.patch.object(report, "send_each"):
            worker.email_tenant(self.tid)
        self.assertEqual(self._note("daily-email"), worker.NO_RECIPIENTS)
        row = self._doctor_daily_row()
        self.assertTrue(row["ok"])
        self.assertIn("no confirmed, unmuted address", row["detail"])
        self.assertNotIn("SMTP", row["detail"])

    def test_a_preview_without_a_budget_stamps_nothing(self):
        budget.save_config(self.conn, {"email_recipients": ["one@example.com"],
                                       "dynamic_variable_budget": False})
        accept_recipient_invite(self.tid, "one@example.com")
        worker.email_tenant(self.tid, send_it=False)
        self.assertIsNone(self._note("daily-email"))


if __name__ == "__main__":
    unittest.main()
