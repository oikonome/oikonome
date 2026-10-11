"""POST /api/accounts/{id}/remove — hide / disconnect / purge modes."""

from __future__ import annotations

import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base

from .util import _ensure_db, add_txn, seed_accounts


class AccountRemoveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def setUp(self):
        # signup is rate-limited per IP across the suite
        try:
            from oikonome.web import security
            security._limiter._hits.clear()
        except Exception:
            pass
        self.client = TestClient(self.app)
        self.email = f"rm-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/signup", data={
            "email": self.email, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        me = self.client.get("/api/me").json()
        self.tid = me["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            add_txn(conn, "2026-06-01", 12.50, "Coffee", account="chk")
        finally:
            conn.close()

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _as_plaid(self, item_id="it1", token="access-token-x"):
        conn = self._conn()
        try:
            # upsert_item does not rewrite aggregator on conflict — set it
            # explicitly so disconnect takes the Plaid /item/remove path
            name = conn.execute(
                "SELECT institution_name FROM items WHERE id=%s",
                (item_id,)).fetchone()["institution_name"]
            sync_base.upsert_item(conn, item_id, "plaid", name or "Bank",
                                  token)
            conn.execute(
                "UPDATE items SET aggregator='plaid' WHERE id=%s",
                (item_id,))
        finally:
            conn.close()

    def test_hide_soft_removes_keeps_connection(self):
        r = self.client.post("/api/accounts/chk/remove",
                             json={"mode": "hide"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "hide")
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT user_removed_at, balance_current "
                "FROM accounts WHERE id='chk'").fetchone()
            it = conn.execute(
                "SELECT status FROM items WHERE id='it1'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row["user_removed_at"])
        self.assertIsNone(row["balance_current"])
        self.assertNotEqual(it["status"], "archived")
        accs = self.client.get("/api/accounts").json()["accounts"]
        me = next(a for a in accs if a["id"] == "chk")
        self.assertIsNotNone(me["user_removed_at"])

    def test_hide_survives_upsert_accounts(self):
        self.client.post("/api/accounts/chk/remove", json={"mode": "hide"})
        conn = self._conn()
        try:
            sync_base.upsert_accounts(conn, "it1", [
                sync_base.Account(
                    id="chk", name="Test Checking", type="depository",
                    subtype="checking", balance_current=9999.0,
                    balance_available=9999.0)])
            row = conn.execute(
                "SELECT user_removed_at, balance_current FROM accounts "
                "WHERE id='chk'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row["user_removed_at"])
        self.assertIsNone(row["balance_current"])

    def test_disconnect_plaid_calls_item_remove(self):
        from oikonome.sync import plaid as plaid_mod
        self._as_plaid(token="access-token-x")
        fake = mock.MagicMock()
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            r = self.client.post("/api/accounts/chk/remove",
                                 json={"mode": "disconnect"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"] and body["plaid_released"])
        fake.remove_item.assert_called_once_with("access-token-x")
        conn = self._conn()
        try:
            st = conn.execute(
                "SELECT status, access_token FROM items WHERE id='it1'"
            ).fetchone()
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE account_id='chk'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(st["status"], "archived")
        self.assertIsNone(st["access_token"])
        self.assertGreaterEqual(n, 1)

    def test_disconnect_treats_item_not_found_as_released(self):
        """A concurrent disconnect click, the reaper's straggler retry, or
        an orphan-reconcile can remove the Item at Plaid first. Our own
        /item/remove then returns ITEM_NOT_FOUND — which means the
        connection is genuinely gone, i.e. released, not a failure. It must
        take the success branch (access_token cleared, archived_reason NULL),
        not the release-failed one that keeps a dead token and tells the user
        'we'll keep retrying' about a bank that is already disconnected."""
        from oikonome.sync import plaid as plaid_mod
        self._as_plaid(token="access-token-gone")
        fake = mock.MagicMock()
        fake.remove_item.side_effect = plaid_mod.PlaidError(
            {"error_code": "ITEM_NOT_FOUND", "error_type": "ITEM_ERROR",
             "error_message": "already gone"}, 400)
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            r = self.client.post("/api/accounts/chk/remove",
                                 json={"mode": "disconnect"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["plaid_released"])
        self.assertFalse(body.get("release_failed"))
        conn = self._conn()
        try:
            st = conn.execute(
                "SELECT status, access_token, archived_reason FROM items "
                "WHERE id='it1'").fetchone()
        finally:
            conn.close()
        self.assertEqual(st["status"], "archived")
        self.assertIsNone(st["access_token"])
        self.assertIsNone(st["archived_reason"])

    def test_disconnect_purge_deletes_data_and_releases_plaid(self):
        from oikonome.sync import plaid as plaid_mod
        self._as_plaid(token="tok-purge")
        fake = mock.MagicMock()
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            r = self.client.post("/api/accounts/chk/remove",
                                 json={"mode": "disconnect_purge"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["plaid_released"])
        self.assertGreaterEqual(body["purged"]["accounts"], 1)
        fake.remove_item.assert_called_once()
        conn = self._conn()
        try:
            n_acct = conn.execute(
                "SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
            n_txn = conn.execute(
                "SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
            st = conn.execute(
                "SELECT status FROM items WHERE id='it1'").fetchone()
        finally:
            conn.close()
        self.assertEqual(n_acct, 0)
        self.assertEqual(n_txn, 0)
        self.assertEqual(st["status"], "archived")

    def test_a_purge_that_keeps_rows_says_so(self):
        """Rows a bank feed relies on survive a delete, and so does the
        account holding them. The reply carries the purge's own sentence
        about them instead of claiming everything went."""
        from oikonome.sync import plaid as plaid_mod
        kept = {"accounts": 0, "transactions": 3, "holdings": 0,
                "crypto_holdings": 0, "kept_transactions": 2,
                "note": "2 rows were kept: the bank feed already relies "
                        "on them. The accounts holding them stay."}
        r = self.client.post("/api/accounts/add", json={
            "name": "Shoebox", "kind": "depository/cash", "balance": "40"})
        aid = r.json()["account_id"]
        with mock.patch.object(sync_base, "purge_account_data",
                               return_value=dict(kept)):
            r = self.client.post(f"/api/accounts/{aid}/remove",
                                 json={"mode": "purge"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("2 rows were kept", r.json()["note"])
        self._as_plaid(token="tok-kept")
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=mock.MagicMock()), \
                mock.patch.object(sync_base, "purge_item_accounts",
                                  return_value=dict(kept)):
            r = self.client.post("/api/accounts/chk/remove",
                                 json={"mode": "disconnect_purge"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("deleted local data", r.json()["note"])
        self.assertIn("2 rows were kept", r.json()["note"])

    def test_purge_manual_account(self):
        r = self.client.post("/api/accounts/add", json={
            "name": "Shoebox", "kind": "depository/cash", "balance": "40"})
        self.assertEqual(r.status_code, 200)
        aid = r.json()["account_id"]
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 200, r.text)
        ids = {a["id"] for a in
               self.client.get("/api/accounts").json()["accounts"]}
        self.assertNotIn(aid, ids)

    def _file_account_with_noted_row(self, accounts):
        """A manual account holding one file-imported row whose note says a
        feed set its copy aside — naming `accounts`, or none — and a bank
        feed that still syncs."""
        r = self.client.post("/api/accounts/add", json={
            "name": "Old Card (file)", "kind": "credit/credit card",
            "balance": "0"})
        self.assertEqual(r.status_code, 200, r.text)
        aid = r.json()["account_id"]
        conn = self._conn()
        try:
            add_txn(conn, "2025-02-03", 18.0, "MAPLE DINER", account=aid,
                    txn_id="csv:noted")
            sync_base.mark_stood_in(conn, {"csv:noted": accounts}, "feed")
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name)"
                " VALUES ('feedit', 'plaid', 'Sample Bank')")
        finally:
            conn.close()
        return aid

    def _exists(self, table, ident):
        conn = self._conn()
        try:
            return conn.execute(f"SELECT 1 FROM {table} WHERE id=%s",
                                (ident,)).fetchone() is not None
        finally:
            conn.close()

    def test_rows_kept_by_a_note_naming_no_account_ask_first(self):
        aid = self._file_account_with_noted_row([])
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 409, r.text)
        body = r.json()
        self.assertEqual(body["error"], "kept_rows_unconfirmed")
        self.assertEqual(body["kept_transactions"], 1)
        self.assertIn("1 imported row", body["detail"])
        self.assertIn("may never send", body["detail"])
        # nothing was deleted on the way to the question
        self.assertTrue(self._exists("accounts", aid))
        self.assertTrue(self._exists("transactions", "csv:noted"))
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge", "force_kept": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["purged"]["kept_transactions"], 0)
        self.assertFalse(self._exists("accounts", aid))
        self.assertFalse(self._exists("transactions", "csv:noted"))

    def test_only_a_literal_true_forces(self):
        aid = self._file_account_with_noted_row([])
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge", "force_kept": "yes"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue(self._exists("transactions", "csv:noted"))

    def test_a_row_a_live_feed_account_relies_on_is_kept_even_forced(self):
        aid = self._file_account_with_noted_row(["chk"])
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["purged"]["kept_transactions"], 1)
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge", "force_kept": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["purged"]["kept_transactions"], 1)
        self.assertTrue(self._exists("transactions", "csv:noted"))
        self.assertTrue(self._exists("accounts", aid))

    def test_with_no_feed_left_a_note_naming_no_account_holds_nothing(self):
        aid = self._file_account_with_noted_row([])
        conn = self._conn()
        try:
            conn.execute("UPDATE items SET status='archived'"
                         " WHERE id='feedit'")
        finally:
            conn.close()
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["purged"]["kept_transactions"], 0)
        self.assertFalse(self._exists("transactions", "csv:noted"))

    def test_an_archived_feed_whose_account_remains_still_holds_the_row(self):
        aid = self._file_account_with_noted_row([])
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype)"
                " VALUES ('feedacct', 'feedit', 'Card', 'credit',"
                " 'credit card')")
            conn.execute("UPDATE items SET status='archived'"
                         " WHERE id='feedit'")
        finally:
            conn.close()
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue(self._exists("transactions", "csv:noted"))

    def test_purging_an_account_takes_every_pointer_at_it_with_it(self):
        """Account ids are named by string from four places the schema
        cannot see: the excluded list, the checking anchor, a savings
        goal, and a dismissed link pairing. A pointer left behind is
        silent — the goal counts nothing for ever, and the pairing the
        person rejected starts being suggested again."""
        from oikonome.engine import budget
        from oikonome.db import tenancy
        r = self.client.post("/api/accounts/add", json={
            "name": "Shoebox", "kind": "depository/cash", "balance": "40"})
        aid = r.json()["account_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["excluded_accounts"] = [aid, "other-acct"]
                cfg["checking_account_id"] = aid
                # TOKENED on purpose: a token-less goal passes either way,
                # so it would not prove anything about the pointer
                cfg["savings_goals"] = [{"name": "Savings",
                                         "account_id": aid,
                                         "tokens": ["transfer"]}]
                cfg["link_dismissed"] = ["|".join(sorted((aid, "zzz")))]
            self.assertEqual(self.client.post(
                f"/api/accounts/{aid}/remove",
                json={"mode": "purge"}).status_code, 200)
            cfg = budget.load_config(conn)
        finally:
            conn.close()
        self.assertEqual(cfg.get("excluded_accounts"), ["other-acct"])
        self.assertIsNone(cfg.get("checking_account_id"))
        # the goal keeps the dead id on purpose: dropping it would widen a
        # tokened goal to every account instead of narrowing it to none
        self.assertEqual(cfg["savings_goals"][0]["account_id"], aid)
        self.assertTrue(cfg["savings_goals"][0]["account_deleted"])
        self.assertFalse(cfg.get("link_dismissed"))

    def test_purge_live_connection_refused(self):
        self._as_plaid()
        r = self.client.post("/api/accounts/chk/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("disconnect_purge", r.json()["detail"])

    def test_an_account_purged_from_a_disconnected_bank_stays_deleted(self):
        """The bridge still carries the deleted account. Brought back by
        the reconnect door, the bank's next pull would recreate it with
        its history — so the door refuses, saying why."""
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, "sfin-main", "simplefin",
                                  "SimpleFIN bridge",
                                  "https://u:p@bridge.test/simplefin")
            child = "sfin-main:demo-bank"
            sync_base.upsert_item(conn, child, "simplefin-org", "Demo Bank",
                                  None)
            conn.execute(
                "UPDATE items SET aggregator='simplefin-org', "
                "status='archived', archived_at=now() WHERE id=%s", (child,))
            for aid in ("sf-chk", "sf-sav"):
                conn.execute(
                    "INSERT INTO accounts (id, item_id, name, type) "
                    "VALUES (%s,%s,%s,'depository')", (aid, child, aid))
        finally:
            conn.close()
        r = self.client.post("/api/accounts/sf-chk/remove",
                             json={"mode": "purge"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(f"/api/connections/{child}/reconnect")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("sync would recreate it", r.json()["detail"])
        conn = self._conn()
        try:
            self.assertEqual(conn.execute(
                "SELECT status FROM items WHERE id=%s",
                (child,)).fetchone()["status"], "archived")
        finally:
            conn.close()

    def test_bad_mode_400(self):
        r = self.client.post("/api/accounts/chk/remove",
                             json={"mode": "vaporize"})
        self.assertEqual(r.status_code, 400)

    def test_connection_disconnect_still_releases_plaid(self):
        """Regression: /connections/.../disconnect uses shared helper."""
        from oikonome.sync import plaid as plaid_mod
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, "plaid-y", "plaid", "Chase", "tok-y")
            conn.execute(
                "UPDATE items SET aggregator='plaid' WHERE id='plaid-y'")
            conn.execute(
                "UPDATE accounts SET item_id='plaid-y' "
                "WHERE id IN ('chk','card')")
            conn.execute("DELETE FROM items WHERE id='it1'")
        finally:
            conn.close()
        fake = mock.MagicMock()
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            r = self.client.post("/api/connections/plaid-y/disconnect")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["plaid_released"])
        fake.remove_item.assert_called_once_with("tok-y")

    def test_disconnect_waits_for_a_running_sync(self):
        """Archiving a connection while its sync is in flight must be
        refused, not raced: the pull would re-upsert the item it is
        working on. The door answers 409 and changes nothing."""
        from oikonome.sync import plaid as plaid_mod
        self._as_plaid(token="access-token-x")
        holder = self._conn()
        try:
            key = f"oikonome:sync:{self.tid}"
            got = holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (key,)
            ).fetchone()["ok"]
            self.assertTrue(got)
            fake = mock.MagicMock()
            with mock.patch.object(plaid_mod.Client, "for_tenant",
                                   return_value=fake):
                r = self.client.post("/api/accounts/chk/remove",
                                     json={"mode": "disconnect"})
                self.assertEqual(r.status_code, 409, r.text)
                r = self.client.post("/api/accounts/chk/remove",
                                     json={"mode": "disconnect_purge"})
                self.assertEqual(r.status_code, 409, r.text)
                r = self.client.post("/api/connections/it1/disconnect")
                self.assertEqual(r.status_code, 409, r.text)
            fake.remove_item.assert_not_called()
            conn = self._conn()
            try:
                st = conn.execute(
                    "SELECT status FROM items WHERE id='it1'").fetchone()
            finally:
                conn.close()
            self.assertNotEqual(st["status"], "archived")
        finally:
            holder.execute("SELECT pg_advisory_unlock_all()")
            holder.close()


if __name__ == "__main__":
    unittest.main()
