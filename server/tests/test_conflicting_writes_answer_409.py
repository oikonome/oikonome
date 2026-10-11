"""A write refused because of something already in place answers 409 with
the reason, never a 500 or a generic 400.

Two refusals of that kind live below the web layer: a hard delete of ledger
rows (an import rollback, an account purge) refuses while a sync holds the
household's lock, because the sync filters its pull against the import rows
it read at its start; and a personally-paid business cost already explained
for one business (capitalized or marked reimbursable) refuses a second
explanation. Both are "try again later / undo the other one first", which a
client can only say if the door answers 409 with the engine's own words.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


def _client() -> TestClient:
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return TestClient(appmod.app)


class _Household(unittest.TestCase):
    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = _client()
        r = self.client.post("/api/signup", data={
            "email": f"conflict-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()


class DeletesWaitForARunningSync(_Household):
    """While another session holds the tenant sync lock, every door that
    hard-deletes ledger rows says so with a 409 and deletes nothing."""

    def setUp(self):
        super().setUp()
        self.holder = tenancy.tenant_connect(self.tid)
        got = self.holder.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{self.tid}",)).fetchone()["ok"]
        self.assertTrue(got)

    def tearDown(self):
        self.holder.execute("SELECT pg_advisory_unlock_all()")
        self.holder.close()
        super().tearDown()

    def _txn_count(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"]

    def test_api_import_rollback_answers_409(self):
        r = self.client.post("/api/import/rollback", json={"batch_id": "b1"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("sync is running", r.json()["detail"])

    def test_form_import_rollback_says_so_on_the_page(self):
        r = self.client.post("/import/rollback", data={"batch_id": "b1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(r.headers["location"].startswith("/import?msg="))
        self.assertIn("sync", r.headers["location"])
        self.assertNotIn("rows%20removed", r.headers["location"])

    def test_account_purge_answers_409_and_keeps_the_account(self):
        add_txn(self.conn, TODAY, 12.0, "PAPER CO", account="chk")
        before = self._txn_count()
        r = self.client.post("/api/accounts/chk/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("sync is running", r.json()["detail"])
        self.assertEqual(self._txn_count(), before)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM accounts WHERE id='chk'").fetchone())

    def test_disconnect_purge_of_a_closed_connection_answers_409(self):
        self.conn.execute("UPDATE items SET status='archived' WHERE id='it1'")
        add_txn(self.conn, TODAY, 12.0, "PAPER CO", account="chk")
        before = self._txn_count()
        r = self.client.post("/api/accounts/chk/remove",
                             json={"mode": "disconnect_purge"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("sync is running", r.json()["detail"])
        self.assertEqual(self._txn_count(), before)


class FrontedExpenseConflictAnswers409(_Household):

    def setUp(self):
        super().setUp()
        from oikonome.engine import entities
        self.a = entities.create_entity(
            self.conn, name="First Venture LLC", structure="sole_prop")["id"]
        self.b = entities.create_entity(
            self.conn, name="Second Venture LLC", structure="sole_prop")["id"]
        self.txn = add_txn(self.conn, TODAY, 40.0, "PAPER CO", account="card")

    def test_second_explanation_for_another_business_is_409(self):
        r = self.client.post(f"/api/business/entities/{self.a}/reimburse",
                             json={"txn_id": self.txn, "mode": "contribute"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(f"/api/business/entities/{self.b}/reimburse",
                             json={"txn_id": self.txn, "mode": "reimburse"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("First Venture LLC", r.json()["detail"])

    def test_ordinary_bad_input_is_still_400(self):
        r = self.client.post(f"/api/business/entities/{self.a}/reimburse",
                             json={"txn_id": "no-such-txn",
                                   "mode": "contribute"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
