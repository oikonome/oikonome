"""A demo instance never hands out the database dump. Its owner login is
printed for every visitor, and on a single-tenant demo box every other
gate on the dump passes — so anyone holding the public demo login could
download the whole instance database, password hashes and sessions
included. The ticket is refused, and so is the download for a ticket
minted before the tenant became a demo."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.web import demoguard, pages

from .util import _ensure_db

PW = "correct-horse-battery"


class DemoInstanceRefusesDatabaseDump(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.owner = TestClient(appmod.app)
        cls.owner.post("/api/signup", data={
            "email": f"demo-dump-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _gates_open(self):
        return mock.patch.multiple(pages, _tenant_count=lambda: 1)

    def test_no_dump_ticket_on_a_demo(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}), \
                self._gates_open(), \
                mock.patch.object(demoguard, "is_demo", return_value=True):
            r = self.owner.post("/api/export/token",
                                json={"kind": "dump", "password": PW})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"], demoguard.DENIED)

    def test_an_earlier_ticket_does_not_open_the_dump_on_a_demo(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}), \
                self._gates_open():
            t = self.owner.post("/api/export/token",
                                json={"kind": "dump", "password": PW})
            self.assertEqual(t.status_code, 200, t.text)
            with mock.patch.object(demoguard, "is_demo", return_value=True):
                r = self.owner.get(t.json()["url"])
        self.assertEqual(r.status_code, 403, r.text[:200])
        self.assertEqual(r.json()["detail"], demoguard.DENIED)

    def test_household_zip_stays_available_on_a_demo(self):
        # synthetic data explored and exported is the point of a demo
        with mock.patch.object(demoguard, "is_demo", return_value=True):
            r = self.owner.post("/api/export/token",
                                json={"kind": "zip", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
