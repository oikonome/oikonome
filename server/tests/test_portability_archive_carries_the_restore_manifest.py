"""The portable data archive says which schema its rows are in, the way
the household's own export does.

A restore reads `manifest.json` to decide which data migrations an older
archive missed; an archive whose manifest it cannot read is taken for the
oldest, and every replay runs over the household. The portable archive
writes a `manifest.json` of its own, so without the export's fields it
read as the oldest archive there is, and an upload of it replayed every
migration over a current household while restoring nothing.
"""

import io
import unittest
import zipfile

from oikonome import tenant_export
from oikonome.db import tenancy
from oikonome.sync import archive_upgrades, export, restore

from .test_account_erasure_export_and_consent import _seed_tenant
from .util import _ensure_db


class PortabilityManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_archive_names_its_schema_and_is_refused(self):
        tid, _ = _seed_tenant()
        data = tenant_export.build_archive(tid, operator="op",
                                           consented=True)
        z = zipfile.ZipFile(io.BytesIO(data))
        conn = tenancy.tenant_connect(tid)
        try:
            self.assertEqual(archive_upgrades.archive_schema(z),
                             export.schema_version(conn))
            # not a backup: the restore door refuses it
            with self.assertRaises(ValueError):
                restore.restore_zip(conn, data)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
