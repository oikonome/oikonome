"""A folder analyze points an export ZIP at the restore door.

The folder door holds every file in memory under the ordinary 50 MB upload
cap; the restore door (POST /api/import) streams a single archive to disk
under the 500 MB restore cap. So an export ZIP too large for the folder door
is not a dead end: its plan row says so and names the restore door, and a
batch that is one ZIP and nothing else carries `restore_door` at the top so
a client can send it there whatever its size.
"""

import io
import os
import unittest
import uuid
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from .util import _ensure_db


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", "{}")
    return buf.getvalue()


class RestoreDoorFlagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"rzip-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def _analyze(self, files):
        return self.client.post("/api/import/bulk/analyze", files=[
            ("files", (n, d, "application/octet-stream")) for n, d in files])

    def test_a_single_export_zip_names_the_restore_door(self):
        r = self._analyze([("household-export.zip", _zip())])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["restore_door"], "/api/import")

    def test_a_zip_too_large_for_the_folder_door_is_sent_to_the_restore_door(
            self):
        from oikonome.web import pages
        real = pages.read_capped

        async def capped(f, *a, **kw):
            if (f.filename or "").endswith(".zip"):
                await f.read()
                return None               # as if it blew the 50 MB cap
            return await real(f, *a, **kw)
        with mock.patch.object(pages, "read_capped", capped):
            r = self._analyze([("household-export.zip", _zip())])
        self.assertEqual(r.status_code, 200, r.text)
        row = r.json()["files"][0]
        self.assertEqual(row["action"], "skip")
        self.assertTrue(row["oversized"])
        self.assertEqual(row["restore_door"], "/api/import")
        self.assertIn("500 MB", row["note"])

    def test_a_mixed_batch_carries_no_top_level_flag(self):
        csv = b"Date,Description,Amount\n2021-03-01,COFFEE,-4.50\n"
        r = self._analyze([("household-export.zip", _zip()),
                           ("checking.csv", csv)])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("restore_door", r.json())


class ClientsReadTheRestoreDoorTests(unittest.TestCase):
    """The clients act on the shapes above: the top-level door on a one-ZIP
    batch, the door on an oversized ZIP row, and the door on the edge's
    413. A client keyed on a flag the server never sends would leave every
    one of those archives in the folder plan, refused."""

    def _src(self, rel):
        import pathlib
        import oikonome
        p = pathlib.Path(oikonome.__file__).parent.parent.parent / rel
        if not p.exists():
            self.skipTest(f"{rel} not present")
        return p.read_text()

    def test_both_clients_key_on_restore_door(self):
        for api, page in (("webapp/src/api/client.ts",
                           "webapp/src/pages/Import.tsx"),
                          ("mobile/src/lib/api.ts",
                           "mobile/src/app/imports.tsx")):
            a, p = self._src(api), self._src(page)
            for rel, src in ((api, a), (page, p)):
                self.assertNotIn("use_restore_door", src, rel)
            # the plan row and the 413 body
            self.assertGreaterEqual(a.count("restore_door"), 3, api)
            # the row test, the batch's top-level door, the refusal's door
            self.assertIn("!!f.restore_door", p, page)
            self.assertIn("p.restore_door", p, page)
            self.assertRegex(p, r"onError: \(e\) => \{[^}]*restore", page)


if __name__ == "__main__":
    unittest.main()
