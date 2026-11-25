"""The setup checklist counts the bill offers the Bills page lists.

An offer the list leaves out (it lapsed a full cycle ago, it adds a card
payment filed before such offers were refused) waits unanswered and is
never deleted. The wizard's Bills step finishes when no offer is left,
and the 'proposals' alert retires when the queue is empty: both must
read the list the page shows (bills.pending_proposals). Counting the raw
pending rows left the step and the alert asking about offers no page can
show and no button can reach.
"""

import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import bills, budget
from oikonome.web import api  # noqa: F401 — api mounts the areas
from oikonome.web import api_imports

from .util import make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


class UnlistedOffersAreNotCounted(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        today = localtime.now_local(budget.load_config(self.conn)).date()
        # a weekly series that stopped two months ago
        bills._insert_proposal(
            self.conn, pid="prop:lapsed", kind="add", payee="Bluebell Dance",
            amount=30.0, frequency="WEEKLY", interval=1,
            next_due=today - dt.timedelta(days=60),
            evidence={"cycle_days": 7, "hits": 5})
        # a card payment filed before card payments stopped being offered
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, bill_type, payee, amount, "
            "frequency, interval, next_due, evidence, status, created_at) "
            "VALUES ('prop:old-card', 'add', 'occurrence', "
            "'Northwind Credit Crd Epay', 800.0, 'MONTHLY', 1, %s, "
            "'{}'::jsonb, 'pending', now())",
            (today + dt.timedelta(days=5),))
        self.conn.commit()
        self.assertEqual(bills.pending_proposals(self.conn), [])

    def test_the_wizard_has_no_offers_left_to_review(self):
        ob = api_imports.onboarding_api(user=_user(self.conn))
        self.assertEqual(ob["pending_proposals"], 0)

    def test_the_proposals_alert_retires_with_the_listed_queue(self):
        self.conn.execute(
            "INSERT INTO alerts_log (kind, severity, message, first_seen, "
            "last_seen) VALUES ('proposals', 'info', '2 proposed', "
            "current_date, current_date)")
        self.conn.commit()
        self.assertTrue(bills.settle_proposals_alert(self.conn))


if __name__ == "__main__":
    unittest.main()
