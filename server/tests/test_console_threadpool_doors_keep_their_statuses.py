"""The console's invite and create-instance doors run their body in the
threadpool (a lock wait on the loop stalls every request the worker
serves). Moving the body off the loop must not change what the client
hears: the sign-in bounce, the cross-site refusal, the single-household
refusal and an HTTPException raised inside the worker all reach the
client with their own status."""
import os
import unittest
import unittest.mock
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient

from oikonome import ext

from .util import _ensure_db

TOKEN = "test-admin-token-" + "t" * 32
DOORS = (("/admin/console/invite", "invite"),
         ("/admin/console/create-instance", "create"))


class ConsoleThreadpoolDoors(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        self.addCleanup(os.environ.pop, "OIKONOME_ADMIN_TOKEN", None)
        from oikonome.web import security
        security._limiter._hits.clear()
        self.addCleanup(ext.set_gate, None)
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def _email(self):
        return f"tp-{uuid.uuid4().hex[:8]}@example.dev"

    def test_an_unsigned_caller_is_bounced_to_the_login_page(self):
        bare = TestClient(self.appmod.app)
        for path, _ in DOORS:
            with self.subTest(path=path):
                r = bare.post(path, data={"email": self._email()},
                              follow_redirects=False)
                # the router's cloak may answer first; either way nothing
                # past the gate ran and it is not a server error
                self.assertIn(r.status_code, (303, 404), r.text[:200])
                if r.status_code == 303:
                    self.assertEqual(r.headers["location"], "/admin/console")

    def test_the_bounce_inside_the_worker_reaches_the_client(self):
        from oikonome.web import adminconsole
        with unittest.mock.patch.object(adminconsole, "_authed",
                                        lambda _r: False):
            for path, _ in DOORS:
                with self.subTest(path=path):
                    r = self.client.post(path, data={"email": self._email()},
                                         follow_redirects=False)
                    self.assertEqual(r.status_code, 303, r.text[:200])
                    self.assertEqual(r.headers["location"],
                                     "/admin/console")

    def test_a_cross_site_post_is_refused(self):
        for path, _ in DOORS:
            with self.subTest(path=path):
                r = self.client.post(
                    path, data={"email": self._email()},
                    headers={"Origin": "https://elsewhere.example"},
                    follow_redirects=False)
                self.assertEqual(r.status_code, 403, r.text[:200])

    def test_a_single_household_instance_refuses_with_its_notice(self):
        from oikonome.web import adminconsole
        with unittest.mock.patch.object(adminconsole, "_multi_tenant",
                                        lambda: False):
            for path, _ in DOORS:
                with self.subTest(path=path):
                    r = self.client.post(path, data={"email": self._email()},
                                         follow_redirects=False)
                    self.assertEqual(r.status_code, 200, r.text[:200])
                    self.assertIn("single household", r.text)

    def test_an_http_exception_raised_in_the_worker_keeps_its_status(self):
        from oikonome.web import adminconsole

        def refuse(*_a, **_k):
            raise HTTPException(422, "refused in the worker")

        g = ext.gate._resolve()
        with unittest.mock.patch.object(g, "invite_options", refuse,
                                        create=True):
            r = self.client.post("/admin/console/invite",
                                 data={"email": self._email()},
                                 follow_redirects=False)
        self.assertEqual(r.status_code, 422, r.text[:200])
        with unittest.mock.patch.object(adminconsole, "_provision_instance",
                                        refuse):
            r = self.client.post("/admin/console/create-instance",
                                 data={"email": self._email()},
                                 follow_redirects=False)
        self.assertEqual(r.status_code, 422, r.text[:200])


if __name__ == "__main__":
    unittest.main()
