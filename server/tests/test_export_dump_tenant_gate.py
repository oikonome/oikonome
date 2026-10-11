"""/export/dump streams EVERY tenant's rows via pg_dump, so it refuses on
any instance holding more than one tenant.

The OIKONOME_HOSTED flag is an operator promise, not a fact about the data;
gating on it alone would let any single owner of a multi-tenant instance
without the flag download every household's data. A single-tenant
self-host (the supported dump case) still gets its dump.
"""

import os
import unittest
import uuid
from unittest import mock

from .util import make_db
from .export_ticket import export_get


class DumpTenantGateTests(unittest.TestCase):
    """/export/dump refuses on a multi-tenant instance even without
    OIKONOME_HOSTED — the flag is a promise, the tenant count is a fact."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"dump-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        # guarantee a second tenant exists (the shared test DB usually has
        # many, but this module must hold when run alone)
        make_db().close()

    def test_multi_tenant_dump_refused_without_hosted_flag(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        r = export_get(self.owner, "/export/dump")
        self.assertEqual(r.status_code, 403)
        self.assertIn("tenant", r.json()["detail"].lower())

    def test_single_tenant_dump_unchanged(self):
        # the shared test DB is inherently multi-tenant; a count of 1 models
        # the real single-tenant self-host box the 200 branch is for
        import shutil

        from oikonome.web import pages
        os.environ.pop("OIKONOME_HOSTED", None)
        with mock.patch.object(pages, "_tenant_count", return_value=1):
            r = export_get(self.owner, "/export/dump")
        if shutil.which("pg_dump") is None:
            self.assertEqual(r.status_code, 500)   # no pg_dump binary
        else:
            self.assertEqual(r.status_code, 200)

    def test_hosted_flag_still_refuses_even_single_tenant(self):
        from oikonome.web import pages
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}), \
                mock.patch.object(pages, "_tenant_count", return_value=1):
            r = export_get(self.owner, "/export/dump")
        self.assertEqual(r.status_code, 403)


class DumpKeepsThePasswordOffTheCommandLine(unittest.TestCase):
    """A process's command line is readable by every account on the host,
    so the dump runs pg_dump with a password-free connection string and
    hands libpq the password through its environment."""

    DSN = "postgresql://admin_role:s3cret-Value@db.example:5432/oik?sslmode=require"

    @classmethod
    def setUpClass(cls):
        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True

    def test_the_password_leaves_the_dsn_for_the_environment(self):
        from psycopg.conninfo import conninfo_to_dict

        from oikonome.web import pages
        dsn, env = pages._dsn_without_password(self.DSN)
        self.assertNotIn("s3cret-Value", dsn)
        self.assertEqual(env["PGPASSWORD"], "s3cret-Value")
        self.assertEqual(conninfo_to_dict(dsn), {
            "user": "admin_role", "host": "db.example", "port": "5432",
            "dbname": "oik", "sslmode": "require"})

    def test_the_dump_process_is_started_without_it_in_argv(self):
        import shutil

        from fastapi.testclient import TestClient
        from psycopg.conninfo import conninfo_to_dict

        from oikonome.db import tenancy
        from oikonome.web import pages
        from oikonome.web.app import app
        password = conninfo_to_dict(tenancy.ADMIN_DSN).get("password")
        if not password:
            self.skipTest("the admin DSN carries no password here")
        owner = TestClient(app)
        owner.post("/api/signup", data={
            "email": f"dumpenv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        seen = {}

        class _Proc:
            stdout = None
            stderr = None

            def poll(self):
                return 0

        import subprocess
        real_popen, real_which = subprocess.Popen, shutil.which

        def popen(argv, *a, **kw):
            if not (isinstance(argv, list) and argv[:1] == ["pg_dump"]):
                return real_popen(argv, *a, **kw)
            seen["argv"], seen["env"] = argv, kw.get("env")
            return _Proc()

        def which(name, *a, **kw):
            return "/x/pg_dump" if name == "pg_dump" else real_which(
                name, *a, **kw)
        os.environ.pop("OIKONOME_HOSTED", None)
        with mock.patch.object(pages, "_tenant_count", return_value=1), \
                mock.patch.object(shutil, "which", which), \
                mock.patch.object(subprocess, "Popen", popen):
            try:
                r = export_get(owner, "/export/dump")
            except RuntimeError:
                r = None    # the stand-in process has no output to stream
        self.assertTrue(seen, f"pg_dump was never started: "
                              f"{getattr(r, 'text', '')}")
        self.assertFalse(any(password in a for a in seen["argv"]))
        self.assertEqual(seen["env"]["PGPASSWORD"], password)


if __name__ == "__main__":
    unittest.main()
