"""The Today plan card's fourth cash tile — what is left after paying
cards and the remaining bills for this month. The payload carries
bills_remaining_month — unpaid bill occurrences from today through month
end, summed with the SAME shared helper the runway and forecast reserve with —
and the SPA renders checking − card debt − that. Live-only, like runway.
"""

import calendar
import datetime as dt
import unittest
import uuid

from .util import add_bill, add_txn, seed_accounts, write_config

TODAY = dt.date.today()
PAST = TODAY - dt.timedelta(days=40)
MONTH_END = dt.date(TODAY.year, TODAY.month,
                    calendar.monthrange(TODAY.year, TODAY.month)[1])


class BillsRemainingMonthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient

        from oikonome.db import tenancy
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post(
            "/api/signup",
            data={"email": f"brm-{uuid.uuid4().hex[:8]}@x.dev",
                  "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_txn(conn, PAST, 100.0, "OLD SPEND")   # ledger floor, past view
        finally:
            conn.close()
        cls.tid = tid

    def test_envelope_remainder_joins_the_tile(self):
        """Envelope bills are bills too, but have no occurrences — a tile
        that summed occurrences alone would read low against the forecast walk.
        A monthly envelope with money left adds exactly the walk's envelope-drip
        charge: max(0, pool − used)."""
        from oikonome.db import tenancy
        from oikonome.engine import budget
        from oikonome.web.api import _bills_remaining_month
        # mid-month vantage: the tomorrow→EOM drip window is never empty
        day = TODAY.replace(day=15)
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            base = _bills_remaining_month(conn, cfg, day)
            add_bill(conn, "Streaming Pool", 300.0, bill_type="envelope")
            add_txn(conn, day.replace(day=10), 120.0, "STREAMING POOL")
            got = _bills_remaining_month(conn, cfg, day)
            # shared class tenant — don't leak into the payload test's sum
            conn.execute("DELETE FROM bills WHERE payee='Streaming Pool'")
        finally:
            conn.close()
        self.assertAlmostEqual(got - base, 300.0 - 120.0, places=2)

    def test_exhausted_envelope_adds_nothing(self):
        """A pool already spent (or overflowed) is money already out of
        checking — the walk charges nothing more, so neither may the tile."""
        from oikonome.db import tenancy
        from oikonome.engine import budget
        from oikonome.web.api import _bills_remaining_month
        day = TODAY.replace(day=15)
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            base = _bills_remaining_month(conn, cfg, day)
            add_bill(conn, "Dining Pool", 200.0, bill_type="envelope")
            add_txn(conn, day.replace(day=5), 250.0, "DINING POOL")
            got = _bills_remaining_month(conn, cfg, day)
            conn.execute("DELETE FROM bills WHERE payee='Dining Pool'")
        finally:
            conn.close()
        self.assertAlmostEqual(got - base, 0.0, places=2)

    def test_annual_envelope_adds_rest_of_month_share(self):
        """An annual pool adds the rest-of-month share of its
        remaining YEAR pool — the walk's drip through EOM, not the whole
        pool and not the flat monthly plan share."""
        from oikonome.db import tenancy
        from oikonome.engine import budget
        from oikonome.web.api import _bills_remaining_month
        day = TODAY.replace(day=15)
        days_left_month = (MONTH_END - day).days
        days_left_year = (dt.date(day.year, 12, 31) - day).days
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            base = _bills_remaining_month(conn, cfg, day)
            add_bill(conn, "Insurance Pool", 1200.0, bill_type="envelope",
                     frequency=None, interval=12)
            got = _bills_remaining_month(conn, cfg, day)
            conn.execute("DELETE FROM bills WHERE payee='Insurance Pool'")
        finally:
            conn.close()
        self.assertAlmostEqual(
            got - base, 1200.0 * days_left_month / days_left_year, places=2)

    def test_live_payload_carries_remaining_bills(self):
        from oikonome.db import tenancy
        conn = tenancy.tenant_connect(self.tid)
        try:
            # one unpaid bill still due this month, one already paid —
            # only the unpaid occurrence may be reserved
            add_bill(conn, "Rent", 1500.0, next_due=MONTH_END,
                     last_seen=MONTH_END - dt.timedelta(days=31))
            add_bill(conn, "Gym", 50.0, next_due=TODAY,
                     last_seen=TODAY - dt.timedelta(days=30))
            add_txn(conn, TODAY, 50.0, "Gym", account="chk",
                    primary="GENERAL_SERVICES")
        finally:
            conn.close()
        r = self.client.get("/api/today/full").json()
        self.assertAlmostEqual(r["bills_remaining_month"], 1500.0, places=2)

    def test_last_day_of_month_keeps_unpaid_bills(self):
        """upcoming_bill_occurrences re-dates unpaid current-month
        occurrences to at least tomorrow — on the last calendar day an
        EOM-only horizon drops them all and the tile reads $0 with rent
        still unpaid."""
        from oikonome.db import tenancy
        from oikonome.engine import budget
        from oikonome.web.api import _bills_remaining_month
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_bill(conn, "EOM Rent", 1500.0,
                     next_due=TODAY.replace(day=15),
                     last_seen=TODAY.replace(day=15) - dt.timedelta(days=31))
            cfg = budget.load_config(conn)
            got = _bills_remaining_month(conn, cfg, MONTH_END)
            # shared class tenant — don't leak this bill into the
            # payload test's sum
            conn.execute("DELETE FROM bills WHERE payee='EOM Rent'")
        finally:
            conn.close()
        self.assertGreaterEqual(got, 1500.0)

    def test_past_day_is_null(self):
        r = self.client.get("/api/today/full",
                            params={"date": PAST.isoformat()}).json()
        self.assertFalse(r["is_today"])
        self.assertIsNone(r["bills_remaining_month"])


if __name__ == "__main__":
    unittest.main()
