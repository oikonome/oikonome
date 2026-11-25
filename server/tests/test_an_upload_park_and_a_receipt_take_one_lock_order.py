"""A file parked between requests and a receipt snapped at the same time
take the household's locks in one order.

Every writer that waits on more than one household lock takes them in
one order: the file-storage lock first, then the reimbursement-pairing
lock, then the merchant-identity lock, then rows. A receipt snap reserves
its bytes (storage) and then finds its charge under the pairing lock; a
restore takes storage before it re-keys anything. A file import takes the
pairing and identity locks before its first row, and some imports end by
parking the upload to ask the person something: a CSV whose columns need
mapping, a YNAB register of several accounts, a file placed under a typed
new-account name. Parking stores bytes, so it needs the storage lock. An
import that asked for storage only then, while it held pairing, would
wait on a snap that holds storage and waits on pairing, and Postgres
breaks that cycle by killing one of them: the snap or the upload answers
500.

So every door that takes the ledger locks takes storage first, and the
staging area serializes on that same storage lock rather than a key of
its own, which leaves no second byte lock to order.
"""

from __future__ import annotations

import ast
import os
import pathlib
import threading
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .lock_order_util import Gate, Side, deadlocks, deadlocks_since
from .util import _ensure_db, seed_accounts

PW = "correct-horse-battery"
# no importer recognizes these columns, so the import parks for mapping
UNMAPPED = (b"Alpha,Beta,Gamma\n"
            b"first,second,third\n"
            b"fourth,fifth,sixth\n")
ROW_CSV = (b"Date,Description,Amount\n"
           b"2026-03-02,CORNER BAKERY,-12.00\n")
PDF = b"%PDF-1.4\n% synthetic receipt\n"
QUOTA_ON = {"OIKONOME_HOUSEHOLD_STORAGE_MB": "2048"}
SRC = pathlib.Path(__file__).resolve().parent.parent / "oikonome"


def _advisory_waiter(other_than: int, timeout: float = 10.0) -> bool:
    import time
    admin = tenancy.admin_connect()
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if admin.execute(
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory'"
                    " AND NOT granted AND pid <> %s",
                    (other_than,)).fetchone():
                return True
            time.sleep(0.02)
        return False
    finally:
        admin.close()


class AParkAndAReceiptSnapShareOneLockOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c = TestClient(self.app)
        email = f"park-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.c.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
        finally:
            conn.close()
        r = self.c.post("/api/import", data={"account_id": "chk",
                                             "amount_sign": "bank"},
                        files={"file": ("chk.csv", ROW_CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.row = conn.execute(
                "SELECT id FROM transactions WHERE name='CORNER BAKERY'"
            ).fetchone()["id"]
        finally:
            conn.close()

    def _snap_holding_storage_meets(self, parker) -> dict:
        """A receipt snap paused after it reserved its bytes (it holds the
        storage lock and is about to ask for the pairing lock), then
        `parker` run on another connection, then the snap let go."""
        from oikonome.engine import entities, receipts
        gate = Gate()
        before = deadlocks()
        with mock.patch.dict(os.environ, QUOTA_ON), \
                mock.patch.object(entities, "live_txn_id",
                                  gate.before(entities.live_txn_id)):
            snap = Side(self.tid, lambda c: receipts.add(
                c, self.row, PDF, "application/pdf"))
            snap.start()
            self.assertTrue(gate.reached.wait(10),
                            "the snap never reached its charge lookup")
            out: dict = {}

            def run():
                try:
                    out["result"] = parker()
                except BaseException as e:               # noqa: BLE001
                    out["error"] = e
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            # the parker queues on a lock the snap holds; under the wrong
            # order it does so already holding the pairing lock
            self.assertTrue(_advisory_waiter(snap.pid),
                            "the parking import never waited on the snap")
            gate.go.set()
            self.assertTrue(snap.finish(), "the snap never finished")
            worker.join(60)
        self.assertFalse(worker.is_alive(), "the parking import hung")
        self.assertEqual(deadlocks_since(before), 0,
                         "the parked upload and the receipt snap deadlocked")
        self.assertIsNone(snap.error, snap.error)
        self.assertIsNone(out.get("error"), out.get("error"))
        return out["result"]

    def test_a_csv_parked_for_mapping_meets_a_receipt_snap(self):
        def upload():
            r = TestClient(self.app, cookies=self.c.cookies).post(
                "/api/import",
                data={"account_id": "chk", "amount_sign": "bank"},
                files={"file": ("odd.csv", UNMAPPED, "text/csv")})
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()
        body = self._snap_holding_storage_meets(upload)
        self.assertTrue((body.get("mapping_needed") or {}).get("token"),
                        body)

    def test_any_park_under_the_ledger_locks_meets_a_receipt_snap(self):
        # the YNAB park, the mapped re-stage and the folder job's park by
        # name all stage their upload under the ledger locks
        from oikonome.db import staging
        from oikonome.web.bulk_import import hold_ledger_write_locks

        def park():
            conn = tenancy.tenant_connect(self.tid)
            try:
                with conn.transaction():
                    hold_ledger_write_locks(conn)
                    return staging.put(conn, "csv",
                                       files=[("odd.csv", UNMAPPED)])
            finally:
                conn.close()
        self.assertTrue(self._snap_holding_storage_meets(park))


def _calls_in_order(fn: ast.AST) -> list[str]:
    """The dotted names of every call in `fn`, in source order."""
    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = (f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else "")
            base = (f.value.id if isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name) else "")
            calls.append((node.lineno, node.col_offset,
                          f"{base}.{name}" if base else name))
    return [c for _, _, c in sorted(calls)]


def _functions():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{path.relative_to(SRC)}:{node.name}", node


# Every function that stores household file bytes (staging.put, or a
# reserve against the storage quota), and why the storage lock it takes
# there is never asked for while it holds the pairing or identity lock.
# A new byte writer must be added here with its reason.
BYTE_WRITERS = {
    # stages under the ledger locks, which take storage first
    "web/pages.py:_stash_csv": "ledger",
    "web/pages.py:_ynab_accounts_needed": "ledger",
    "web/pages.py:_mapping_retry": "ledger",
    "web/bulk_import.py:_park_by_name": "ledger",
    # its own transaction, before any ledger lock
    "web/bulk_import.py:analyze": "alone",
    "engine/taxdocs.py:stage": "alone",
    # storage before pairing, in its own body
    "engine/receipts.py:add": "storage-first",
    # a restore takes storage first (hold_restore_write_locks); its
    # receipts reserve their bytes in their lander's parse
    "sync/landers.py:_receipt": "ledger",
    # the lock helpers themselves
    "db/staging.py:put": "helper",
    "web/bulk_import.py:hold_ledger_write_locks": "helper",
}


class EveryByteWriterTakesTheOneStorageLockFirst(unittest.TestCase):
    def test_the_storage_lock_is_spelled_once(self):
        """One key for every byte the household holds, spelled in one
        helper; no byte writer keeps a lock of its own to order."""
        spelled = sorted(
            str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
            if "'oikonome:storage:'" in p.read_text()
            or "'oikonome:staging:'" in p.read_text())
        self.assertEqual(spelled, ["engine/storage_quota.py"])

    def test_the_ledger_locks_start_with_storage(self):
        funcs = dict(_functions())
        order = [c for c in _calls_in_order(
            funcs["web/bulk_import.py:hold_ledger_write_locks"])
            if c in ("storage_quota.hold_lock", "hold_pairing_lock",
                     "merchant_identity._lock_identity")]
        self.assertEqual(order, ["storage_quota.hold_lock",
                                 "hold_pairing_lock",
                                 "merchant_identity._lock_identity"])
        restore = _calls_in_order(
            funcs["web/bulk_import.py:hold_restore_write_locks"])
        self.assertIn("hold_ledger_write_locks", restore)

    def test_staging_serializes_on_the_storage_lock(self):
        put = _calls_in_order(dict(_functions())["db/staging.py:put"])
        self.assertIn("storage_quota.hold_lock", put)

    def test_every_byte_writer_is_accounted_for(self):
        found = {}
        for name, fn in _functions():
            calls = _calls_in_order(fn)
            if any(c in ("staging.put", "storage_quota.reserve",
                         "_sq.reserve", "storage_quota.hold_lock")
                   for c in calls):
                found[name] = calls
        self.assertEqual(sorted(found), sorted(BYTE_WRITERS),
            "a function now stores household bytes: list it in "
            "BYTE_WRITERS with the reason its storage lock comes first")
        for name, calls in found.items():
            if BYTE_WRITERS[name] != "storage-first":
                continue
            first_bytes = min(i for i, c in enumerate(calls)
                              if c.endswith("reserve"))
            later = [i for i, c in enumerate(calls)
                     if c in ("live_txn_id", "hold_pairing_lock",
                              "hold_ledger_write_locks")]
            self.assertTrue(later and all(i > first_bytes for i in later),
                            f"{name} asks for pairing before storage")


class TheHtmlMappedSubmitRefusesAScriptToken(unittest.TestCase):
    """A collector token may finish a mapped import only through the API
    door, where the importability check runs; the page route never sets
    the push channel that check keys on, so it must stay shut to tokens."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def test_bearer_token_gets_403_on_the_page_route(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        c = TestClient(self.app)
        c.post("/api/signup", data={
            "email": f"tok-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        minted = c.post("/api/tokens", json={"name": "mapped-page",
                                             "password": PW}).json()
        bare = TestClient(self.app)
        form = {"token": "x", "date_col": "Alpha", "name_col": "Beta"}
        hdr = {"Authorization": f"Bearer {minted['token']}"}
        r = bare.post("/import/mapped", headers=hdr, data=form,
                      follow_redirects=False)
        self.assertEqual(r.status_code, 403, r.text)
        # the API door is the one a token may open
        r = bare.post("/api/import/mapped", headers=hdr, json=form)
        self.assertNotEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
