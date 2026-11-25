"""Every door that lands a file's rows — the no-JS upload and mapping
pages, the API upload, the mapped-CSV finish and the multi-account YNAB
finish — runs them in one transaction that first asks
`erasure.push_may_write`. A request authenticated before the household's
erasure began must not write the file's accounts, rows and batch back
under a tenant id nothing will ever sweep again (the domain tables have no
foreign key to tenants), and an import that fails part-way must take back
every row it wrote, not leave a partial batch counting in every total."""
import contextlib
import os
import unittest
import unittest.mock
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

KNOWN_CSV = (b"Date,Description,Amount\n"
             b"2026-07-01,CORNER GROCER,-40.00\n"
             b"2026-07-02,PAYCHECK,1500.00\n")
# headers the auto-mapper does not know: parked for a column mapping
ODD_CSV = (b"When,What,How Much\n"
           b"2026-07-10,SHOP A,-12.00\n"
           b"2026-07-11,SHOP B,-8.50\n")
ODD_MAPPING = {"date_col": "When", "amount_col": "How Much",
               "name_col": "What"}
_YNAB_HEAD = ('"Account","Flag","Date","Payee","Category Group/Category",'
              '"Category Group","Category","Memo","Outflow","Inflow",'
              '"Cleared"\n')
YNAB_REGISTER = (
    _YNAB_HEAD
    + '"Checking","","2026-07-10","Corner Grocer","Everyday: Groceries",'
      '"Everyday","Groceries","","$42.00","$0.00","Cleared"\n'
    + '"Visa","","2026-07-11","Fuel Stop","Everyday: Fuel","Everyday",'
      '"Fuel","","$30.00","$0.00","Cleared"\n').encode()
YNAB_ANSWER = {"Checking": "chk", "Visa": "card"}


class _Household:
    """A signed-in household of its own, so one test's erasure leaves the
    next test a live one."""

    def __init__(self):
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(appmod.app)
        self.client.post("/api/signup", data={
            "email": f"erase-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def counts(self) -> tuple[int, int]:
        admin = tenancy.admin_connect()
        try:
            t = admin.execute("SELECT count(*) AS n FROM transactions "
                              "WHERE tenant_id=%s", (self.tid,)).fetchone()
            b = admin.execute("SELECT count(*) AS n FROM import_batches "
                              "WHERE tenant_id=%s", (self.tid,)).fetchone()
            return t["n"], b["n"]
        finally:
            admin.close()

    def parked(self, filename: str, data: bytes, **form) -> str:
        r = self.client.post(
            "/api/import", data={"account_id": "chk",
                                 "amount_sign": "bank", **form},
            files={"file": (filename, data, "text/csv")})
        self.assertOk(r)
        need = r.json()["mapping_needed"]
        assert need and need.get("token"), r.text[:300]
        return need["token"]

    @staticmethod
    def assertOk(r):
        assert r.status_code == 200, r.text[:300]


def _doors(h: _Household):
    """(name, prepare, call) for each file-import door. `prepare` parks
    what the finishing doors finish, before the erasure begins."""
    def upload_form(_):
        return h.client.post("/import", data={"account_id": "chk",
                                               "amount_sign": "bank"},
                             files={"file": ("bank.csv", KNOWN_CSV,
                                             "text/csv")})

    def upload_api(_):
        return h.client.post("/api/import", data={"account_id": "chk",
                                                  "amount_sign": "bank"},
                             files={"file": ("bank.csv", KNOWN_CSV,
                                             "text/csv")})

    def park_odd():
        return h.parked("odd.csv", ODD_CSV)

    def mapped_form(token):
        return h.client.post("/import/mapped",
                             data={"token": token, **ODD_MAPPING})

    def mapped_api(token):
        return h.client.post("/api/import/mapped",
                             json={"token": token, **ODD_MAPPING})

    def park_ynab():
        return h.parked("register.csv", YNAB_REGISTER, ynab_choice="1")

    def ynab_api(token):
        return h.client.post("/api/import/ynab-accounts",
                             json={"token": token, "accounts": YNAB_ANSWER})

    return [("upload form", lambda: None, upload_form),
            ("upload api", lambda: None, upload_api),
            ("mapped form", park_odd, mapped_form),
            ("mapped api", park_odd, mapped_api),
            ("ynab api", park_ynab, ynab_api)]


class FileImportDoorsNeverWriteIntoAnErasedHousehold(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True

    @contextlib.contextmanager
    def _erasure_begins_after_sign_in(self, tid, how):
        """The erasure starts after the request authenticated and before
        the door takes the household's sync lock: the window in which a
        sign-in and an erasure can race."""
        from oikonome.sync import base as sync_base
        real = sync_base.tenant_sync_lock

        @contextlib.contextmanager
        def flip_then_lock(conn, tenant_id):
            admin = tenancy.admin_connect()
            try:
                if how == "pending_delete":
                    admin.execute("UPDATE tenants SET status='pending_delete'"
                                  " WHERE id=%s", (tid,))
                else:
                    admin.execute("DELETE FROM tenants WHERE id=%s", (tid,))
            finally:
                admin.close()
            with real(conn, tenant_id) as held:
                yield held
        with unittest.mock.patch.object(sync_base, "tenant_sync_lock",
                                        flip_then_lock):
            yield

    def test_no_door_writes_once_the_erasure_has_begun(self):
        for how in ("pending_delete", "row deleted"):
            for i in range(5):
                h = _Household()
                name, prepare, call = _doors(h)[i]
                with self.subTest(door=name, erasure=how):
                    token = prepare()
                    before = h.counts()
                    with self._erasure_begins_after_sign_in(h.tid, how):
                        r = call(token)
                    self.assertEqual(r.status_code, 409, r.text[:300])
                    self.assertIn("being deleted", r.text)
                    self.assertEqual(h.counts(), before)

    def test_the_ynab_finish_called_directly_refuses_too(self):
        from oikonome.web import pages
        h = _Household()
        token = h.parked("register.csv", YNAB_REGISTER, ynab_choice="1")
        before = h.counts()
        conn = tenancy.tenant_connect(h.tid)
        try:
            with self._erasure_begins_after_sign_in(h.tid, "pending_delete"):
                with self.assertRaises(HTTPException) as cm:
                    pages.run_ynab_import(conn, token, YNAB_ANSWER, {})
        finally:
            conn.close()
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(h.counts(), before)

    def test_an_import_failing_part_way_leaves_no_partial_batch(self):
        from oikonome.sync import batches
        h = _Household()
        for name, prepare, call in _doors(h):
            with self.subTest(door=name):
                token = prepare()
                before = h.counts()
                # every row is written by then: the failure lands after
                # the importer's last upsert, before the batch is closed
                with unittest.mock.patch.object(
                        batches, "finish",
                        side_effect=RuntimeError("synthetic failure")):
                    r = call(token)
                self.assertEqual(r.status_code, 200, r.text[:300])
                self.assertIn("import failed", r.text)
                self.assertEqual(h.counts(), before)


if __name__ == "__main__":
    unittest.main()
