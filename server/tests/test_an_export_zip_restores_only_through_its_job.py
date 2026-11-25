"""An export ZIP is restored only by the background job, and a household
being deleted is answered 409 at the door that starts it.

Every door that takes an export ZIP starts the restore job. The shared
import dispatcher has no restore of its own: a ZIP that reaches it is
refused, whatever the case of its name, and a name that only looks like
one goes down the file-import path, where it restores nothing.
"""
import unittest
from unittest import mock

from oikonome.sync import restore, restore_job
from oikonome.web import pages

from .util import make_db, write_config


class TheDispatcherRestoresNothing(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_zip_reaching_the_dispatcher_is_refused(self):
        for name in ("export.zip", "EXPORT.ZIP", "export.zip ",
                     " export.Zip"):
            with self.subTest(name=name), \
                    mock.patch.object(restore, "restore_zip") as rz, \
                    mock.patch.object(restore_job, "start") as st:
                result, _ = pages.dispatch_import(
                    self.conn, "", "bank", name, b"PK\x03\x04", role="owner")
                self.assertIn("error", result)
                rz.assert_not_called()
                st.assert_not_called()


class AnErasedHouseholdIsAnswered409(unittest.TestCase):
    """The claim refuses a household being deleted (tested with the job);
    the doors that start it answer that refusal as every other file
    import does."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid

        from fastapi.testclient import TestClient

        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"ez-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_the_api_door_answers_409(self):
        erased = {"error": restore.ERASED_MSG, "erased": True}
        with mock.patch.object(restore_job, "start", return_value=erased), \
                mock.patch.object(restore, "foreign_archive_refusal",
                                  return_value=None):
            r = self.client.post(
                "/api/import",
                data={"account_id": "", "amount_sign": "bank",
                      "password": "correct-horse-battery"},
                files={"file": ("export.zip", b"PK\x03\x04",
                                "application/zip")})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("being deleted", r.text)


if __name__ == "__main__":
    unittest.main()
