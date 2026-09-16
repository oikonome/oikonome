"""The operator portability archive covers the control-plane tables the
RLS-driven discovery cannot see — household invites, script tokens, push
subscriptions, the third parties who receive the daily email, and every
grant of support access — as metadata, never credentials.

`recipient_invites` belongs in it: a portability request is answered with an
archive of the tenant's personal data, and the addresses a household mails
its balances to (plus who added them and when each said yes) are exactly
that — third-party personal data the household controls. The list is
hand-maintained because discovery cannot see a table with no row-level
security; `test_control_plane_export_coverage` fails when it goes stale.
"""

import unittest
import uuid
import zipfile
import io
import json

from oikonome import tenant_export
from oikonome.db import tenancy
from oikonome.notify import recipient_invites

from .util import TEST_DB, _admin_dsn, _ensure_db


class PortabilityControlTablesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _manifest(self, tid: str) -> dict:
        data = tenant_export.build_archive(tid, operator="t", consented=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return json.loads(z.read("manifest.json"))

    def test_manifest_carries_every_control_plane_table_it_names(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            tid = str(tenancy.create_tenant(
                admin, f"port-{uuid.uuid4().hex[:8]}"))
            admin.execute(
                "INSERT INTO api_tokens (id, tenant_id, name, token_hash) "
                "VALUES (%s, %s, 'coinbase collector', %s)",
                (str(uuid.uuid4()), tid, uuid.uuid4().hex))
        finally:
            admin.close()
        m = self._manifest(tid)
        for key in tenant_export.ARCHIVE_CONTROL_TABLES:
            self.assertIn(key, m)
        self.assertEqual(m["api_tokens"][0]["name"], "coinbase collector")
        self.assertNotIn("token_hash", m["api_tokens"][0])

    def test_the_daily_email_recipients_and_their_consent_are_in_it(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            tid = str(tenancy.create_tenant(
                admin, f"port-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()
        guest = f"guest-{uuid.uuid4().hex[:8]}@example.test"
        recipient_invites.accept(recipient_invites.invite(tid, guest))

        row = self._manifest(tid)["recipient_invites"][0]
        self.assertEqual(row["email"], guest,
                         "a portability request must return the addresses "
                         "this household shares its financial mail with")
        self.assertTrue(row["accepted_at"],
                        "and the record of that person's consent")
        self.assertNotIn("token_hash", row,
                         "never the live invite link, which is a credential "
                         "sitting in someone's mailbox")


if __name__ == "__main__":
    unittest.main()
