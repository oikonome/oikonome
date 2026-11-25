"""The reimbursement flag, the outbound-pause resume and the invitation
resend read their bodies through the typed reader, and take every body the
web client and the mobile app send.

Each kept its released reading where the reading was meant: the flag's
`expected` is a number or the text of one (float() always read both) and
null or "" is no expected amount; its `partial` is true/false, which is all
either client sends; the resume's `version` is the fingerprint string the
banner echoes, and left out or null it lifts the pause unconditionally; the
resend reads one address. What changed is that a value of the wrong kind
— a list, an object, a bool where a number belongs — is a 400 naming the
field instead of a str() of it.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .test_bill_doors_refuse_wrong_types import BAD_VALUES
from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
DAY = dt.date.today() - dt.timedelta(days=2)


class FlagResumeAndResendDoors(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        cls.security = security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app, raise_server_exceptions=False)
        cls.client.post("/api/signup", data={
            "email": f"flagres-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.txns = [add_txn(conn, DAY, -20.0 - i, f"SHAPE LUNCH {i}")
                        for i in range(8)]
        finally:
            conn.close()

    def _post(self, path, **kw):
        self.security._limiter._hits.clear()
        return self.client.post("/api" + path, **kw)

    # ---- the reimbursement flag ---------------------------------------

    def test_the_flag_takes_every_shipped_body(self):
        # web: a bare flag from the table, a partial one, the planner's
        # expected amount; mobile: {partial, expected} with expected null
        # when the box is empty; a script's expected typed as text
        bodies = ({}, {"partial": True},
                  {"partial": False, "expected": 45.6},
                  {"partial": False, "expected": None},
                  {"expected": "12.50"}, {"expected": ""})
        for txn, body in zip(self.txns, bodies):
            with self.subTest(body=body):
                r = self._post(f"/reimburse/{txn}/flag", json=body)
                self.assertEqual(r.status_code, 200, r.text)
        # no body at all, as a script may post
        r = self._post(f"/reimburse/{self.txns[6]}/flag")
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_flag_refuses_a_wrong_kind_by_name(self):
        for body, words in (({"expected": [1]}, "expected must be a number"),
                            ({"expected": True}, "expected must be a number"),
                            ({"expected": "lots"},
                             "expected must be a number"),
                            ({"partial": "yes"},
                             "partial must be true or false")):
            with self.subTest(body=body):
                r = self._post(f"/reimburse/{self.txns[7]}/flag", json=body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["detail"], words)

    # ---- the outbound-pause resume ------------------------------------

    def test_the_resume_takes_the_shipped_bodies(self):
        # the banner echoes its fingerprint; an older banner sends none
        for body in ({"version": "a1b2c3"}, {}, {"version": None}):
            with self.subTest(body=body):
                self._post("/auth/elevate", json={"password": PW})
                r = self._post("/settings/outbound/resume", json=body)
                self.assertIn(r.status_code, (200, 409), r.text)
                self.assertNotEqual(r.status_code, 400, r.text)

    def test_the_resume_refuses_a_container_version(self):
        for bad in ([1], {"a": 1}, True):
            with self.subTest(bad=bad):
                self._post("/auth/elevate", json={"password": PW})
                r = self._post("/settings/outbound/resume",
                               json={"version": bad})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["detail"],
                                 "version must be a string")

    # ---- the invitation resend ------------------------------------------

    def test_the_resend_reads_one_address(self):
        r = self._post("/settings/recipients/resend",
                       json={"email": "nobody@example.dev"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"],
                         "that address is not a saved recipient")
        r = self._post("/settings/recipients/resend", json={"email": ""})
        self.assertEqual(r.json()["detail"], "email is required")
        r = self._post("/settings/recipients/resend", json={"email": 7})
        self.assertEqual(r.json()["detail"], "email must be an address")

    # ---- the matrix ---------------------------------------------------

    def test_no_wrongly_typed_key_answers_5xx(self):
        failures = []
        for path, keys in (
                (f"/reimburse/{self.txns[7]}/flag", ("expected", "partial")),
                ("/settings/outbound/resume",
                 ("version", "password", "totp_code", "recovery_code")),
                ("/settings/recipients/resend", ("email",))):
            for key in keys:
                for bad in BAD_VALUES:
                    self._post("/auth/elevate", json={"password": PW})
                    r = self._post(path, json={key: bad})
                    if r.status_code >= 500:
                        failures.append(f"{path} {key}={str(bad)[:12]!r} "
                                        f"-> {r.status_code}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
