"""A folder import stops when the person who started it is signed out.

A password change or a "sign out everywhere" ends every session it does
not keep — the answer to a stolen session. A job that went on importing
files under that session's authority would outlive the very sign-out
meant to end it. The job checks before every file; once the starting
credential is gone it stops for good, and the files it had not reached
are dropped."""

import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.jobs import bulk_import_job

from .bulk_job_util import SignedInClient, csv_for, file_rows
from .util import _ensure_db


class StarterSignedOutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _submit(self, h):
        plan = h.analyze([(f"bank-{t}.csv", csv_for(f"SHOP {t}"))
                          for t in ("A", "B", "C")])
        with mock.patch.object(bulk_import_job, "nudge"):
            r = h.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "background": True,
                "files": [dict(f, account_id="chk") for f in plan["files"]]})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["job_id"]

    def _run_and_after_first(self, h, job_id, act):
        real = bulk_import_job._run_one
        calls = []

        def first_then_act(*a, **kw):
            out = real(*a, **kw)
            calls.append(1)
            if len(calls) == 1:
                act()
            return out

        with mock.patch.object(bulk_import_job, "_run_one",
                               side_effect=first_then_act):
            bulk_import_job.run_job(h.tid, job_id)
        return calls

    def test_a_deleted_session_stops_the_rest(self):
        h = SignedInClient("so")
        job_id = self._submit(h)

        def sign_out():
            admin = tenancy.admin_connect()
            try:
                admin.execute("DELETE FROM sessions WHERE user_id = (SELECT"
                              " id FROM users WHERE email = %s)", (h.email,))
            finally:
                admin.close()

        calls = self._run_and_after_first(h, job_id, sign_out)
        self.assertEqual(len(calls), 1, "a file ran after the sign-out")
        conn = h.conn()
        try:
            st = bulk_import_job.status(conn, job_id)
            rows = file_rows(conn, job_id)
        finally:
            conn.close()
        self.assertEqual(st["state"], "stopped", st)
        self.assertEqual(st["stop_reason"], "signed_out")
        self.assertEqual(rows[0]["state"], "done")
        for i in (1, 2):
            self.assertEqual(rows[i]["state"], "skipped")
            self.assertTrue(rows[i]["gone"], "a skipped file kept its bytes")
        # stopped is final: a later run imports nothing more
        bulk_import_job.run_job(h.tid, job_id)
        self.assertEqual(h.ledger_count(), 3)

    def test_a_starter_demoted_to_viewer_stops_the_rest(self):
        h = SignedInClient("so2")
        job_id = self._submit(h)

        def demote():
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE users SET role = 'viewer' WHERE email"
                              " = %s", (h.email,))
            finally:
                admin.close()

        calls = self._run_and_after_first(h, job_id, demote)
        self.assertEqual(len(calls), 1)
        conn = h.conn()
        try:
            st = bulk_import_job.status(conn, job_id)
        finally:
            conn.close()
        self.assertEqual((st["state"], st["stop_reason"]),
                         ("stopped", "role_changed"))

    def test_a_sign_out_while_a_file_waits_its_turn_stops_it(self):
        """The starter is checked again once the file's turn comes, not
        only before it waits: a password change landing while the file
        waited behind a sync must not let it import afterwards."""
        h = SignedInClient("so3")
        plan = h.analyze([("bank-a.csv", csv_for("SHOP A"))])
        with mock.patch.object(bulk_import_job, "nudge"):
            r = h.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "background": True,
                "files": [dict(f, account_id="chk") for f in plan["files"]]})
        self.assertEqual(r.status_code, 202, r.text)
        job_id = r.json()["job_id"]
        lock = f"oikonome:sync:{h.tid}"
        blocker = tenancy.tenant_connect(h.tid)
        try:
            blocker.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock,))
            waits = []

            def sleep(s):
                waits.append(s)
                if len(waits) == 1:
                    admin = tenancy.admin_connect()
                    try:
                        admin.execute(
                            "DELETE FROM sessions WHERE user_id = (SELECT id"
                            " FROM users WHERE email = %s)", (h.email,))
                    finally:
                        admin.close()
                    blocker.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                                    (lock,))

            with mock.patch.object(bulk_import_job, "_sleep",
                                   side_effect=sleep):
                bulk_import_job.run_job(h.tid, job_id)
        finally:
            blocker.close()
        conn = h.conn()
        try:
            st = bulk_import_job.status(conn, job_id)
            rows = file_rows(conn, job_id)
        finally:
            conn.close()
        self.assertEqual(len(waits), 1)
        self.assertEqual((st["state"], st["stop_reason"]),
                         ("stopped", "signed_out"), st)
        self.assertEqual(rows[0]["state"], "skipped")
        self.assertTrue(rows[0]["gone"])
        self.assertEqual(h.ledger_count(), 0)


class ScriptTokenStarterTests(unittest.TestCase):
    """A script token cannot start a folder import at all — a job must
    have a signed-in person to answer to — and the credential check a job
    makes would still see a revoked token as gone."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_script_token_cannot_submit(self):
        from oikonome.db import staging
        from oikonome.web import bulk_import

        from .util import make_db
        conn = make_db()
        try:
            token = staging.put(conn, "bulk", [("a.csv", csv_for("SHOP A"))])
            with mock.patch.object(bulk_import_job, "nudge"), \
                    self.assertRaises(PermissionError):
                bulk_import.submit(conn, token,
                                   [{"index": 0, "action": "import",
                                     "account_id": "chk"}],
                                   {"role": "owner", "script_token": True})
            self.assertIsNotNone(staging.peek(conn, "bulk", token))
        finally:
            conn.close()

    def test_a_revoked_token_reads_as_signed_out(self):
        import hashlib

        from oikonome.auth import api_tokens
        h = SignedInClient("tk")
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute("SELECT id FROM users WHERE email = %s",
                                (h.email,)).fetchone()["id"]
            plain, row = api_tokens.mint(admin, h.tid, uid, "collector")
        finally:
            admin.close()
        fp = hashlib.sha256(plain.encode()).hexdigest()[:32]
        conn = h.conn()
        try:
            self.assertTrue(bulk_import_job.starter_credentials(
                conn, fp, "token"))
            admin = tenancy.admin_connect()
            try:
                api_tokens.revoke(admin, h.tid, row["id"])
            finally:
                admin.close()
            self.assertFalse(bulk_import_job.starter_credentials(
                conn, fp, "token"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
