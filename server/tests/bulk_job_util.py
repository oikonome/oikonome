"""Shared helpers for the folder-import job tests: submit a staged plan,
run its job here on the calling thread (what the worker does), and read
what each file came to."""

import os
import uuid

from oikonome.db import tenancy

PW = "correct-horse-battery"
BANK_CSV = (b"Date,Description,Amount\n"
            b"2021-03-01,COFFEE SHOP,-4.00\n"
            b"2021-03-02,PAYCHECK,1500.00\n"
            b"2021-03-03,GROCERY,-80.00\n"
            b"2021-03-04,GAS,-30.00\n")


def csv_for(tag: str) -> bytes:
    """A small bank CSV whose rows are unique to `tag`."""
    return (b"Date,Description,Amount\n"
            + f"2021-04-01,{tag} ONE,-10.00\n".encode()
            + f"2021-04-02,{tag} TWO,-20.00\n".encode()
            + f"2021-04-03,{tag} THREE,-30.00\n".encode())


def tenant_of(conn) -> str:
    return str(conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])


def _stepped_up():
    """A caller whose step-up already holds."""


def submit(conn, token, decisions, role="owner",
           step_up=_stepped_up) -> dict:
    """Submit with the queue nudge held back: the test runs the job
    itself, and no worker anywhere should pick it up. An archive in the
    plan restores as for a caller already stepped up."""
    from unittest import mock

    from oikonome.jobs import bulk_import_job
    from oikonome.web import bulk_import
    with mock.patch.object(bulk_import_job, "nudge"):
        return bulk_import.submit(conn, token, decisions, {"role": role},
                                  step_up=step_up)


def run_job(conn, job_id) -> dict:
    """Run the job to its end here and return its status."""
    from oikonome.jobs import bulk_import_job
    bulk_import_job.run_job(tenant_of(conn), job_id)
    return bulk_import_job.status(conn, job_id)


def submit_and_run(conn, token, decisions, role="owner",
                   step_up=_stepped_up) -> list[dict]:
    """The old one-shot run, through the job: the per-file results in the
    shape the run door has always answered with."""
    from oikonome.jobs import bulk_import_job
    sub = submit(conn, token, decisions, role=role, step_up=step_up)
    st = run_job(conn, sub["job_id"])
    return bulk_import_job.compat_results(st)


def file_rows(conn, job_id) -> dict:
    return {r["file_id"]: r for r in conn.execute(
        "SELECT file_id, filename, state, result, data IS NULL AS gone"
        "  FROM bulk_import_files WHERE job_id = %s ORDER BY file_id",
        (job_id,)).fetchall()}


class SignedInClient:
    """A household signed up through the API, with its TestClient."""

    def __init__(self, prefix="bj"):
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        from fastapi.testclient import TestClient
        from oikonome.web import security
        from .util import seed_accounts
        appmod.DEV_MODE = True
        security._limiter._hits.clear()
        self.c = TestClient(appmod.app)
        self.email = f"{prefix}-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.c.post("/api/signup",
                        data={"email": self.email, "password": PW})
        assert r.status_code == 200, r.text
        admin = tenancy.admin_connect()
        try:
            self.tid = str(admin.execute(
                "SELECT tenant_id FROM users WHERE email=%s",
                (self.email,)).fetchone()["tenant_id"])
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
        finally:
            conn.close()

    def conn(self):
        return tenancy.tenant_connect(self.tid)

    def analyze(self, files):
        r = self.c.post("/api/import/bulk/analyze", files=[
            ("files", (name, data, "application/octet-stream"))
            for name, data in files])
        assert r.status_code == 200, r.text
        return r.json()

    def stale_elevation(self):
        """Elevated once, long enough ago that the window has closed."""
        r = self.c.post("/api/auth/elevate", json={"password": PW})
        assert r.status_code == 200, r.text
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE sessions SET elevated_at = now() - interval '20 min'"
                " WHERE user_id = (SELECT id FROM users WHERE email=%s)",
                (self.email,))
        finally:
            admin.close()

    def ledger_count(self) -> int:
        conn = self.conn()
        try:
            return conn.execute(
                "SELECT count(*) AS n FROM transactions").fetchone()["n"]
        finally:
            conn.close()
