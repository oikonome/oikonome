"""Untyped JSON body ids must not reach the SQL adapter as raw containers.

Several routes take `body: dict = Body(...)` with no per-field type
validation and pass an id straight into a `%s` parameter against a TEXT
column. A caller sending a JSON array or object for that id makes psycopg
try to bind a Python list/dict — `operator does not exist: text = smallint[]`
(list) or `cannot adapt type 'dict'`. Neither is the app's one carve-out
(InvalidTextRepresentation / 22P02), so it falls through to a bare 500 and a
logged traceback for what is ordinary bad client input. str()-coercing the id
(the pattern the sibling reimburse_link already uses) turns each into a clean
not-found / no-op instead of a server error.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts


def _client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


# the container values that used to crash psycopg's adapter
BAD_IDS = ([1, 2], {"a": 1}, [{"nested": 1}])


class BodyIdTypeCoercionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.client.post("/api/signup", data={
            "email": f"coerce-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
        finally:
            conn.close()

    def test_reimburse_unlink_non_string_ids_do_not_500(self):
        for bad in BAD_IDS:
            r = self.client.post("/api/reimburse/unlink",
                                 json={"expense_id": bad, "reimburse_id": "x"})
            self.assertNotEqual(r.status_code, 500, r.text)
            self.assertEqual(r.status_code, 200, r.text)

    def test_accounts_exclude_non_string_id_is_not_found_not_500(self):
        for bad in BAD_IDS:
            r = self.client.post("/api/accounts/exclude",
                                 json={"account_id": bad})
            self.assertNotEqual(r.status_code, 500, r.text)
            self.assertEqual(r.status_code, 404, r.text)

    def test_accounts_balance_non_string_id_is_not_found_not_500(self):
        for bad in BAD_IDS:
            r = self.client.post("/api/accounts/balance",
                                 json={"account_id": bad, "balance": "10"})
            self.assertNotEqual(r.status_code, 500, r.text)
            self.assertEqual(r.status_code, 404, r.text)

    def test_import_rollback_non_string_batch_id_does_not_500(self):
        for bad in BAD_IDS:
            r = self.client.post("/api/import/rollback",
                                 json={"batch_id": bad})
            self.assertNotEqual(r.status_code, 500, r.text)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["deleted"], 0)

    def test_accounts_classify_non_string_fields_do_not_500(self):
        for bad in (123, *BAD_IDS):
            r = self.client.post("/api/accounts/classify",
                                 json={"kind": bad, "account_id": "chk"})
            self.assertEqual(r.status_code, 400, r.text)
        for bad in BAD_IDS:
            r = self.client.post(
                "/api/accounts/classify",
                json={"kind": "depository/checking", "account_id": bad})
            self.assertEqual(r.status_code, 400, r.text)

    def test_accounts_classify_rejects_unknown_type(self):
        r = self.client.post("/api/accounts/classify",
                             json={"kind": "spaceship/x",
                                   "account_id": "chk"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_accounts_rename_non_string_id_does_not_500(self):
        for bad in BAD_IDS:
            r = self.client.post("/api/accounts/rename",
                                 json={"account_id": bad, "name": "N"})
            self.assertNotEqual(r.status_code, 500, r.text)
            self.assertEqual(r.status_code, 400, r.text)

    def test_accounts_rename_non_string_name_is_rejected_not_written(self):
        # str() on a container would write "{'a': 1}" as the display name
        for bad in BAD_IDS:
            r = self.client.post("/api/accounts/rename",
                                 json={"account_id": "chk", "name": bad})
            self.assertEqual(r.status_code, 400, r.text)

    def test_business_doors_non_string_bodies_do_not_500(self):
        # tier-gated or not, none of these may 500 on a container value —
        # the containers must die at validation before any SQL adapter
        eid = str(uuid.uuid4())
        for bad in BAD_IDS:
            for path, payload in (
                (f"/api/business/entities/{eid}/reimburse",
                 {"txn_id": bad, "mode": "contribute"}),
                (f"/api/business/entities/{eid}/equity",
                 {"kind": "contribution", "amount": 5,
                  "date": "2026-01-01", "form": bad}),
                ("/api/business/entities", {"name": bad, "structure": bad}),
                (f"/api/business/entities/{eid}/members",
                 {"member_name": bad, "ownership_pct": 50}),
            ):
                r = self.client.post(path, json=payload)
                self.assertNotEqual(r.status_code, 500,
                                    f"{path}: {r.text}")


if __name__ == "__main__":
    unittest.main()
