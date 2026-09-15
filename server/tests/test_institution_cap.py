"""Counting connected institutions, and who may be capped on them.

Count = live Plaid/MX Items + SimpleFIN per-bank children (the bridge row
and collector/manual items never count; archiving frees the slot). An
instance a household runs for itself is never capped, so the cap is None
there and the ADD door never blocks; an update/re-auth is exempt in any
case."""

import os
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.sync import base as sync_base

from .util import _admin_dsn, _ensure_db, make_db, TEST_DB


def _item(conn, item_id, aggregator, status="ok"):
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, status)
           VALUES (%s, %s, %s, %s)""",
        (item_id, aggregator, item_id, status))


class CountTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_counts_plaid_mx_and_simplefin_children_only(self):
        _item(self.conn, "p1", "plaid")
        _item(self.conn, "p2", "plaid")
        _item(self.conn, "m1", "mx")
        _item(self.conn, "sfin-main", "simplefin")          # bridge: no
        _item(self.conn, "sfin-main:chase", "simplefin-org")  # bank: yes
        _item(self.conn, "sfin-main:acme", "simplefin-org")
        _item(self.conn, "coinbase", "coinbase")            # collector: no
        _item(self.conn, "manual", "csv")                   # manual: no
        self.assertEqual(sync_base.institution_count(self.conn), 5)

    def test_archived_frees_the_slot(self):
        _item(self.conn, "p1", "plaid")
        _item(self.conn, "p2", "plaid", status="archived")
        self.assertEqual(sync_base.institution_count(self.conn), 1)


class CapEnvTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("OIKONOME_HOSTED", "OIKONOME_HOSTED_MAX_INSTITUTIONS")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_selfhost_uncapped(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.assertIsNone(sync_base.institution_cap())




class CapCheckTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        os.environ["OIKONOME_HOSTED"] = "1"
        os.environ["OIKONOME_HOSTED_MAX_INSTITUTIONS"] = "2"

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        os.environ.pop("OIKONOME_HOSTED_MAX_INSTITUTIONS", None)
        self.conn.close()

    def test_under_cap_passes(self):
        _item(self.conn, "p1", "plaid")
        sync_base.check_institution_cap(self.conn)   # no raise


    def test_selfhost_never_blocks(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        for i in range(20):
            _item(self.conn, f"p{i}", "plaid")
        sync_base.check_institution_cap(self.conn)   # no raise


class PlaidDoorTests(unittest.TestCase):
    """The /accounts/plaid/link door consults the cap before adding a
    connection; update mode is exempt from it."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        cls._saved = {k: os.environ.get(k) for k in
                      ("OIKONOME_DEV", "OIKONOME_HOSTED",
                       "OIKONOME_HOSTED_MAX_INSTITUTIONS")}
        os.environ.update({"OIKONOME_DEV": "1", "OIKONOME_HOSTED": "1",
                           "OIKONOME_HOSTED_MAX_INSTITUTIONS": "1"})
        from fastapi.testclient import TestClient

        from oikonome.auth import sessions
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        cls.tid = str(tenancy.create_tenant(
            cls.admin, f"cap-{uuid.uuid4().hex[:8]}"))
        # totp_secret set: a hosted account needs an enrolled factor before
        # the server allows writes
        u = cls.admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, totp_secret) "
            "VALUES (%s, %s, 'x', 'enc:cp:test') RETURNING id",
            (cls.tid, f"cap-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()
        cls.client.cookies.set(
            sessions.COOKIE_NAME,
            sessions.create_session(cls.admin, u["id"], cls.tid, "ua"))
        conn = tenancy.tenant_connect(cls.tid)
        try:
            _item(conn, "p-existing", "plaid")
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


    def test_update_mode_exempt_from_cap(self):
        # update of the existing item gets PAST the cap gate (it then fails
        # later on missing Plaid credentials — a redirect mentioning Plaid,
        # never the cap copy)
        r = self.client.post("/accounts/plaid/link",
                             data={"item_id": "p-existing"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("Disconnect%20one", r.headers["location"])


if __name__ == "__main__":
    unittest.main()
