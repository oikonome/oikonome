"""An owner's recovery holds new bank links when the household's own Plaid
or MX keys are saved.

Those keys decide which Plaid or MX client receives the bank logins the
owner types into Link or the MX widget. A hijacker who saved their own
client there keeps receiving every new bank login after the owner
recovers, unless the recovery holds the doors that hand logins out. So
the household's own keys alone arm the record (they used to be a note
riding on a record armed for something else), and hosted, which never
stores such keys, never arms it."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause as op

from .bank_link_hold_fixtures import PLAID_CLIENT, save_household_keys
from .util import _ensure_db, write_config


class RecoveryHoldsNewBankLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        client = TestClient(self.app)
        email = f"hold-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={
            "email": email, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE lower(email)=lower(%s)",
                (email,)).fetchone()["id"]
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            # only the household's own Plaid keys: no relay, no extra
            # recipients, no AI endpoint
            write_config(conn)
            save_household_keys(conn, mx=False)
        finally:
            conn.close()

    def _recover(self):
        admin = tenancy.admin_connect()
        try:
            return op.pause_for_rotation(admin, self.uid, reason=op.RECOVERY)
        finally:
            admin.close()

    def _cfg(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_household_keys_alone_arm_the_hold(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            rec = self._recover()
            self.assertIsNotNone(rec)
            self.assertTrue(rec["aggregator"])
            cfg = self._cfg()
            p = op.paused(cfg)
            self.assertIsNotNone(p)
            self.assertTrue(p["aggregator"])
            self.assertTrue(op.links_held(cfg, "plaid"))
            self.assertFalse(op.links_held(cfg, "mx"))
            self.assertEqual([k["client_id"] for k in p["aggregator_keys"]],
                             [PLAID_CLIENT])

    def test_hosted_never_arms_the_hold(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            self.assertIsNone(self._recover())
            cfg = self._cfg()
            self.assertIsNone(op.paused(cfg))
            self.assertFalse(op.links_held(cfg, "plaid"))


if __name__ == "__main__":
    unittest.main()
