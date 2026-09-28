"""MX member-sync robustness: the members call paginates, its
failure is non-fatal to the sync, per-member sync counts are logged,
and an auth failure flags the item login_required."""

import unittest

import httpx

from oikonome.sync import mx

from .test_mx import PAYLOAD, _save_creds
from .util import make_db, write_config


def _routed(handler):
    return httpx.MockTransport(handler)


class MxMemberSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _save_creds(self.conn)

    def tearDown(self):
        self.conn.close()

    # ---- _members paginates ---------------------------------------
    def test_members_paginate(self):
        import copy
        pl = copy.deepcopy(PAYLOAD)
        # second account under a second member on members page 2
        pl["GET /users/USR-1/accounts"]["accounts"].append(
            {"guid": "ACT-9", "member_guid": "MBR-2", "name": "Savings",
             "type": "SAVINGS", "balance": 5.0, "currency_code": "USD"})

        def handler(req):
            key = f"{req.method} {req.url.path}"
            if key == "GET /users/USR-1/members":
                page = int(req.url.params.get("page", 1))
                if page == 1:
                    return httpx.Response(200, json={
                        "members": [{"guid": "MBR-1", "name": "Bank One"}],
                        "pagination": {"total_pages": 2}})
                return httpx.Response(200, json={
                    "members": [{"guid": "MBR-2", "name": "Bank Two"}],
                    "pagination": {"total_pages": 2}})
            body = pl.get(key)
            return httpx.Response(200 if body else 404, json=body or {})
        mx.sync(self.conn, transport=_routed(handler))
        names = {r["id"]: r["institution_name"] for r in self.conn.execute(
            "SELECT id, institution_name FROM items WHERE aggregator='mx'")}
        self.assertEqual(names.get("mx-MBR-1"), "Bank One")
        self.assertEqual(names.get("mx-MBR-2"), "Bank Two")  # page-2 name kept

    # ---- a failed _members doesn't kill the sync ---------------
    def test_members_failure_is_nonfatal(self):
        def handler(req):
            key = f"{req.method} {req.url.path}"
            if key == "GET /users/USR-1/members":
                return httpx.Response(500, json={"error": "boom"})
            body = PAYLOAD.get(key)
            return httpx.Response(200 if body else 404, json=body or {})
        r = mx.sync(self.conn, transport=_routed(handler))
        self.assertGreaterEqual(r["members"], 1)
        # item created with the "MX" fallback label, status ok
        row = self.conn.execute(
            "SELECT institution_name, status FROM items WHERE id='mx-MBR-1'"
        ).fetchone()
        self.assertEqual(row["institution_name"], "MX")
        self.assertEqual(row["status"], "ok")

    # ---- log_sync counts per member, not the batch total ---------
    def test_per_member_sync_count(self):
        # MBR-1 has 2 txns (1 real spend kept), give MBR-2 an account w/ txns
        import copy
        pl = copy.deepcopy(PAYLOAD)
        pl["GET /users/USR-1/accounts"]["accounts"].append(
            {"guid": "ACT-9", "member_guid": "MBR-2", "name": "Chk2",
             "type": "CHECKING", "balance": 5.0, "currency_code": "USD"})
        pl["GET /users/USR-1/members"]["members"].append(
            {"guid": "MBR-2", "name": "Bank Two"})
        pl["GET /users/USR-1/transactions"]["transactions"].append(
            {"guid": "TRN-9", "account_guid": "ACT-9", "amount": 7.0,
             "type": "DEBIT", "transacted_at": "2026-07-10T12:00:00Z",
             "description": "TEA", "status": "POSTED"})

        def handler(req):
            key = f"{req.method} {req.url.path}"
            body = pl.get(key)
            return httpx.Response(200 if body else 404, json=body or {})
        mx.sync(self.conn, transport=_routed(handler))
        rows = {r["item_id"]: r["added"] for r in self.conn.execute(
            "SELECT item_id, added FROM sync_log WHERE item_id LIKE 'mx-%'")}
        # MBR-2 got exactly its 1 txn, not the batch total (which is >1):
        # distinct per-member counts (MBR-1=2, MBR-2=1), never both showing
        # the batch total of 3, which a shared `added` counter would give
        self.assertEqual(rows.get("mx-MBR-2"), 1)
        self.assertEqual(rows.get("mx-MBR-1"), 2)

    # ---- auth failure → login_required (feeds link_alert) --------
    def test_auth_failure_sets_login_required(self):
        # first a good sync so an mx item exists
        mx.sync(self.conn, transport=_routed(
            lambda req: httpx.Response(
                200, json=PAYLOAD.get(f"{req.method} {req.url.path}") or {})
            if PAYLOAD.get(f"{req.method} {req.url.path}") is not None
            else httpx.Response(404, json={})))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id='mx-MBR-1'").fetchone()["status"],
            "ok")

        def handler(req):
            key = f"{req.method} {req.url.path}"
            if key == "GET /users/USR-1/accounts":
                return httpx.Response(401, json={"error": "unauthorized"})
            body = PAYLOAD.get(key)
            return httpx.Response(200 if body else 404, json=body or {})
        with self.assertRaises(Exception):
            mx.sync(self.conn, transport=_routed(handler))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id='mx-MBR-1'").fetchone()["status"],
            "login_required")


if __name__ == "__main__":
    unittest.main()
