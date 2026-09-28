"""The daily verdict is not mailed to a household that has no budget.

With nothing planned the engine still produces a perfectly consistent
verdict — "On budget · $0 of $0 left · $0 to spend today". That is
arithmetic, not news, and mailing it every morning to somebody still part
way through setup is a judgement on a plan they have not made yet. The
Today page and the native app hide the hero for the same reason; the email
mirrors the page, so it has to agree.

Two halves of the contract, both pinned here:
  * the SCHEDULED sweep skips the daily send while budgets are unset;
  * "email me now" (force_email) still sends, so the button never dead-ends
    and nothing about the instance becomes unreachable.

Weekly/monthly/yearly lens mail is untouched — those report what happened,
not what was planned.
"""

import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.web import report

from .util import accept_recipient_invite, make_db


class DailyEmailNeedsABudgetTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        # a household mid-setup: recipients confirmed, no plan yet
        budget.save_config(self.conn, {"email_recipients": ["one@example.com"],
                                       "dynamic_variable_budget": False})
        accept_recipient_invite(self.tid, "one@example.com")

    def tearDown(self):
        self.conn.close()

    def _send(self, **kw):
        with mock.patch.object(
                report, "send_each",
                return_value=[{"email": "one@example.com", "ok": True,
                               "error": None}]) as m:
            subject = worker.email_tenant(self.tid, send_it=True, **kw)
        return subject, m

    def test_the_scheduled_daily_send_is_skipped_without_a_budget(self):
        subject, m = self._send()
        self.assertEqual(subject, "")
        m.assert_not_called()

    def test_email_me_now_still_sends(self):
        _, m = self._send(force_email=True)
        self.assertTrue(m.called,
                        "the manual button must still work — a household "
                        "mid-setup asking to see the email should get it")

    def test_a_budget_restores_the_scheduled_send(self):
        budget.save_config(self.conn, {"email_recipients": ["one@example.com"],
                                       "food_monthly": 800,
                                       "dynamic_variable_budget": False})
        _, m = self._send()
        self.assertTrue(m.called)


if __name__ == "__main__":
    unittest.main()
