"""The reports and projections respect the two boundaries every other
money aggregate does: business-entity money stays out of the personal
view, and "today" is the household's day, not the container's.

- An investment account assigned to a business entity is out of the
  personal Investment Fees picture (and back in when entities combine).
- A pinned primary checking that was since assigned to an entity does
  not seed the personal cash runway, and pinning such an account is
  refused at the door.
- The Month lens judges a month open or final against the household's
  day: at 6pm Pacific on the 31st the month is still in progress even
  though UTC has rolled to the 1st.
"""

import datetime as dt
import unittest
import uuid
from unittest import mock

from oikonome.engine import budget, forecast, reporting

from .util import TODAY, add_txn, make_db, seed_accounts, write_config


def _entity(conn) -> str:
    from oikonome.engine import entities
    return str(entities.create_entity(conn, name="Side Co",
                                      structure="sole_prop")["id"])


class EntitySeparationTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES ('brk-1', 'plaid', 'Side Brokerage') "
            "ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype,
                                     balance_current, updated_at)
               VALUES ('brk-acct', 'brk-1', 'Side Brokerage', 'investment',
                       'brokerage', 5000, now())""")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 12.0,
                "ADVISORY FEE", account="brk-acct")

    def tearDown(self):
        self.conn.close()

    def _fees_total(self):
        return float(reporting.compute_fees(self.conn)["total"])

    def test_business_brokerage_fees_stay_out_of_the_personal_report(self):
        before = self._fees_total()
        self.assertGreater(before, 0)
        eid = _entity(self.conn)
        self.conn.execute("UPDATE accounts SET entity_id=%s WHERE id='brk-acct'",
                          (eid,))
        self.conn.execute("SELECT set_config('app.combine_entities', '', false)")
        self.assertEqual(self._fees_total(), 0)
        self.conn.execute("SELECT set_config('app.combine_entities', 'true', false)")
        self.assertEqual(self._fees_total(), before)

    def test_pinned_business_checking_does_not_seed_the_runway(self):
        acct = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' "
            "AND subtype='checking' ORDER BY balance_current DESC LIMIT 1"
        ).fetchone()["id"]
        self.conn.execute("UPDATE accounts SET balance_current=9999, "
                          "balance_available=9999 WHERE id=%s", (acct,))
        cfg = budget.load_config(self.conn)
        cfg["checking_account_id"] = acct
        self.assertEqual(forecast._checking_balance(self.conn, cfg), 9999)
        eid = _entity(self.conn)
        self.conn.execute("UPDATE accounts SET entity_id=%s WHERE id=%s",
                          (eid, acct))
        self.conn.execute("SELECT set_config('app.combine_entities', '', false)")
        self.assertNotEqual(forecast._checking_balance(self.conn, cfg), 9999)


def _signed_in_household():
    """(client, tenant_id) for a fresh owner signed in through /login."""
    import os
    from fastapi.testclient import TestClient
    from oikonome.db import tenancy
    from .util import _ensure_db
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web import security
    security._limiter._hits.clear()
    email = f"hd-{uuid.uuid4().hex[:8]}@x.dev"
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, email)
        from oikonome.auth import passwords
        admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, "
            "verified_at) VALUES (%s, %s, %s, now())",
            (tid, email, passwords.hash_password("correct-horse-battery")))
    finally:
        admin.close()
    client = TestClient(appmod.app)
    r = client.post("/login", data={"email": email,
                                    "password": "correct-horse-battery"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return client, tid


class PinDoorTests(unittest.TestCase):
    def setUp(self):
        from oikonome.db import tenancy
        self.client, self.tid = _signed_in_household()
        self.conn = tenancy.tenant_connect(self.tid)
        write_config(self.conn)
        seed_accounts(self.conn)
        self.acct = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
        ).fetchone()["id"]
        self.conn.execute("UPDATE accounts SET entity_id=%s WHERE id=%s",
                          (_entity(self.conn), self.acct))

    def tearDown(self):
        self.conn.close()

    def _type(self):
        return self.conn.execute("SELECT type FROM accounts WHERE id=%s",
                                 (self.acct,)).fetchone()["type"]

    def test_pinning_a_business_account_is_refused_without_a_type_change(self):
        r = self.client.post("/api/accounts/classify", json={
            "account_id": self.acct, "kind": "credit/credit card",
            "primary_checking": True})
        self.assertEqual(r.status_code, 400, r.text)
        # the refusal rolled the whole request back — the type is untouched
        self.assertEqual(self._type(), "depository")
        self.assertIsNone(budget.load_config(self.conn).get("checking_account_id"))

    def test_the_combined_view_allows_the_pin(self):
        cfg = budget.load_config(self.conn)
        cfg["combine_entities"] = True
        budget.save_config(self.conn, cfg)
        r = self.client.post("/api/accounts/classify", json={
            "account_id": self.acct, "kind": "depository/checking",
            "primary_checking": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(budget.load_config(self.conn)["checking_account_id"],
                         self.acct)

    def test_the_legacy_form_door_refuses_too(self):
        r = self.client.post("/accounts/classify", data={
            "account_id": self.acct, "kind": "depository/checking",
            "primary_checking": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("business", r.headers["location"])
        self.assertIsNone(budget.load_config(self.conn).get("checking_account_id"))


class HouseholdDayTests(unittest.TestCase):
    def test_month_lens_is_still_open_at_six_pm_pacific_on_the_31st(self):
        from oikonome.db import tenancy
        client, tid = _signed_in_household()
        conn = tenancy.tenant_connect(tid)
        try:
            write_config(conn)
            cfg = budget.load_config(conn)
            cfg["timezone"] = "America/Los_Angeles"
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        # 2026-09-01 01:00 UTC == 2026-08-31 18:00 Pacific
        import zoneinfo
        local = dt.datetime(2026, 8, 31, 18, 0,
                            tzinfo=zoneinfo.ZoneInfo("America/Los_Angeles"))
        with mock.patch("oikonome.localtime.now_local", return_value=local), \
             mock.patch("oikonome.web.lenses.dt") as fake_dt:
            fake_dt.date = mock.MagicMock(wraps=dt.date)
            fake_dt.date.today = lambda: dt.date(2026, 9, 1)
            fake_dt.datetime = dt.datetime
            fake_dt.timedelta = dt.timedelta
            out = client.get("/api/lens/month").json()
        self.assertEqual((out.get("y"), out.get("m")), (2026, 8))
        self.assertNotEqual(out.get("status"), "final")


if __name__ == "__main__":
    unittest.main()
