"""The operator's portability archive leaves out every table the
household's own export leaves out.

Both discover the tenant's tables and skip a reasoned few. The household's
export skipped job bookkeeping, import scratch, sync history, key material
and the rest for what they carry; the archive skipped only the webhook
pair, so the categorizer's run record (which names the AI endpoint it used,
credentials in the URL included) rode out in the archive whose manifest
promises that secrets are excluded by design.
"""
import io
import json
import unittest
import uuid
import zipfile

from oikonome import tenant_export
from oikonome.db import tenancy
from oikonome.web.pages import EXPORT_SKIP

from .util import TEST_DB, _admin_dsn, _ensure_db

KEY = "sk-" + uuid.uuid4().hex


class ArchiveSkipsWhatTheExportSkips(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_the_two_lists_agree(self):
        self.assertEqual(set(), set(EXPORT_SKIP)
                         - set(tenant_export.ARCHIVE_TABLE_SKIP))

    def test_the_run_record_and_its_endpoint_stay_behind(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            tid = str(tenancy.create_tenant(
                admin, f"arch-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            conn.execute(
                "INSERT INTO job_progress (id, state, progress) "
                "VALUES ('llm', 'done', %s::jsonb)",
                (json.dumps({"url": f"https://user:{KEY}@proxy.example/v1"}),))
            conn.commit()
        finally:
            conn.close()
        data = tenant_export.build_archive(tid, operator="t", consented=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names)
        self.assertFalse([n for n in names if "job_progress" in n], names)
        self.assertNotIn(KEY.encode(), blob)


if __name__ == "__main__":
    unittest.main()
