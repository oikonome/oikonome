"""When the database has no connection slot to give, the unpooled doors
answer the same brown-out a starved pool does — 503 with Retry-After —
instead of a bare 500, and the signup form keeps what was typed.

Signup, email confirmation and the reclaim doors open an unpooled admin
connection. A server out of slots (SQLSTATE 53300) or not answering at all
raises psycopg.OperationalError at connect; unmapped, that is a 500 that a
client cannot back off on and that loses the form. Any other operational
error is still a real fault and still a 500.
"""

import os
import unittest
from unittest import mock

import psycopg
from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class _TooManyClients(psycopg.OperationalError):
    sqlstate = "53300"


class _SomeOtherFault(psycopg.OperationalError):
    sqlstate = "XX000"


class FullDatabaseShedsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod
        cls.client = TestClient(appmod.app, raise_server_exceptions=False)

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        for target in ("_deliver_verification", "_operator_signup_notice",
                       "_deliver_reclaim"):
            p = mock.patch.object(self.appmod, target)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                           "OIKONOME_OPEN_SIGNUP": "1"})
        env.start()
        self.addCleanup(env.stop)

    def _refuse_connect(self, exc):
        p = mock.patch.object(tenancy, "admin_connect", side_effect=exc)
        p.start()
        self.addCleanup(p.stop)

    def test_api_signup_answers_503_when_slots_run_out(self):
        self._refuse_connect(_TooManyClients("sorry, too many clients"))
        r = self.client.post("/api/signup", data={
            "email": "full-db@example.dev", "password": PW})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.headers.get("Retry-After"), "15")

    def test_api_signup_answers_503_when_the_server_does_not_answer(self):
        # a connect failure carries no sqlstate at all
        self._refuse_connect(psycopg.OperationalError("connection timeout"))
        r = self.client.post("/api/signup", data={
            "email": "no-db@example.dev", "password": PW})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.headers.get("Retry-After"), "15")

    def test_signup_form_rerenders_with_what_was_typed(self):
        self._refuse_connect(_TooManyClients("sorry, too many clients"))
        r = self.client.post("/signup", data={
            "email": "form-full-db@example.dev", "password": PW})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("form-full-db@example.dev", r.text)
        self.assertIn("briefly busy", r.text)
        self.assertNotIn("Something went wrong", r.text)
        self.assertEqual(r.headers.get("Retry-After"), "15")

    def test_verify_email_answers_503_page(self):
        self._refuse_connect(_TooManyClients("sorry, too many clients"))
        r = self.client.get("/verify-email?token=whatever")
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("Catching our breath", r.text)

    def test_an_unrelated_operational_error_is_still_a_500(self):
        self._refuse_connect(_SomeOtherFault("internal error"))
        r = self.client.post("/api/signup", data={
            "email": "fault-db@example.dev", "password": PW})
        self.assertEqual(r.status_code, 500, r.text)

    def test_admin_connect_bounds_its_wait(self):
        with mock.patch.object(tenancy.psycopg, "connect") as connect:
            tenancy.admin_connect("postgresql://x@localhost/db")
            self.assertEqual(connect.call_args.kwargs["connect_timeout"],
                             tenancy.ADMIN_CONNECT_TIMEOUT_S)
            tenancy.admin_connect(
                "postgresql://x@localhost/db?connect_timeout=3")
            self.assertNotIn("connect_timeout", connect.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
