"""/export/dump streams EVERY
tenant's rows via pg_dump. It was gated only on the OIKONOME_HOSTED flag —
an operator promise, not a fact about the data — so a multi-tenant
self-host WITHOUT the flag let any single owner download every household's
data. The gate now also counts tenants; single-tenant self-host (the
supported dump case) is unchanged.
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
            self.assertEqual(r.status_code, 500)   # pre-existing behavior
        else:
            self.assertEqual(r.status_code, 200)

    def test_hosted_flag_still_refuses_even_single_tenant(self):
        from oikonome.web import pages
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}), \
                mock.patch.object(pages, "_tenant_count", return_value=1):
            r = export_get(self.owner, "/export/dump")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
