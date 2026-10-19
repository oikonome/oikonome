"""Wizard backend: background sync with live progress
(job_progress row polled by the Sync step), per-account history coverage,
explicit wizard step-state marks, and simplefin-org sync_log stamps."""

import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import simplefin

from .test_simplefin import _transport
from .util import _ensure_db, add_txn, make_db


class SimplefinOrgLogSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_mask_extracted_from_account_name(self):
        # SimpleFIN carries no mask field; the last-4 lives in the NAME —
        # without it, cross-provider link suggestions can never fire
        sync_base.upsert_item(self.conn, "sfin-main", "simplefin", "SimpleFIN",
                              "https://u:p@bridge.test/simplefin")
        payload = {"accounts": [
            {"id": "acc-1", "name": "Everyday Checking (4242)",
             "currency": "USD", "balance": "100",
             "org": {"name": "Acme Bank"}, "transactions": []},
        ]}
        import httpx as _hx
        import json as _json
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_hx.MockTransport(
                           lambda r: _hx.Response(200,
                                                  text=_json.dumps(payload))))
        self.assertEqual(self.conn.execute(
            "SELECT mask FROM accounts WHERE id='acc-1'"
        ).fetchone()["mask"], "4242")

    def test_archived_org_child_stays_disconnected(self):
        sync_base.upsert_item(self.conn, "sfin-main", "simplefin", "SimpleFIN",
                              "https://u:p@bridge.test/simplefin")
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        kid = self.conn.execute(
            "SELECT id FROM items WHERE aggregator='simplefin-org' "
            "ORDER BY id LIMIT 1").fetchone()["id"]
        self.conn.execute(
            "UPDATE items SET status='archived' WHERE id=%s", (kid,))
        n_before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions t JOIN accounts a "
            "ON a.id=t.account_id WHERE a.item_id=%s", (kid,)).fetchone()["n"]
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        # not resurrected, no new rows for its accounts
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id=%s", (kid,)
        ).fetchone()["status"], "archived")
        n_after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions t JOIN accounts a "
            "ON a.id=t.account_id WHERE a.item_id=%s", (kid,)).fetchone()["n"]
        self.assertEqual(n_before, n_after)

    def test_org_children_get_sync_log_rows(self):
        # with only the bridge stamped, every SimpleFIN bank reads
        # "never synced" forever (last_ok is grouped per item)
        sync_base.upsert_item(self.conn, "sfin-main", "simplefin", "SimpleFIN",
                              "https://u:p@bridge.test/simplefin")
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        kids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM items WHERE aggregator='simplefin-org'")]
        self.assertTrue(kids)
        for kid in kids:
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM sync_log WHERE item_id=%s AND error IS NULL",
                (kid,)).fetchone(), f"no sync_log row for {kid}")


class WizardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"wiz-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def test_wizard_steps_roundtrip_and_validation(self):
        r = self.client.post("/api/settings",
                             json={"wizard_steps": {"connect": "done"}})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/settings",
                             json={"wizard_steps": {"sync": "skipped"}})
        self.assertEqual(r.status_code, 200)
        ob = self.client.get("/api/onboarding").json()
        # merge semantics: both marks survive independent posts
        self.assertEqual(ob["wizard_steps"],
                         {"connect": "done", "sync": "skipped"})
        # null clears a mark
        self.client.post("/api/settings",
                         json={"wizard_steps": {"sync": None}})
        self.assertEqual(self.client.get("/api/onboarding").json()
                         ["wizard_steps"], {"connect": "done"})
        # unknown step / bad state rejected
        self.assertEqual(self.client.post("/api/settings", json={
            "wizard_steps": {"bogus": "done"}}).status_code, 400)
        self.assertEqual(self.client.post("/api/settings", json={
            "wizard_steps": {"connect": "maybe"}}).status_code, 400)

    def _add_checking(self, conn, acct_id: str, name: str, bal: float = 100.0):
        conn.execute(
            """INSERT INTO items (id, aggregator, institution_name, access_token)
               SELECT 'manual','manual','Manual',NULL
               WHERE NOT EXISTS (SELECT 1 FROM items WHERE id='manual')""")
        conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype,
                                     balance_current, updated_at)
               VALUES (%s,'manual',%s,'depository','checking',%s,now())""",
            (acct_id, name, bal))

    def test_one_checking_account_is_pinned_when_the_connect_step_is_left(self):
        """Leaving the wizard's connect step with a single checking
        account in pins it as the primary checking: the Today checklist's
        step closes with the wizard's instead of staying open for a choice
        that was never a choice."""
        from oikonome.engine import budget
        conn = self._conn()
        try:
            budget.save_config(conn, {k: v for k, v in budget.load_config(conn).items()
                                      if k != "checking_account_id"})
            conn.execute("DELETE FROM accounts")
            self._add_checking(conn, "manual:chk-only", "Checking")
        finally:
            conn.close()
        ob = self.client.get("/api/onboarding").json()
        self.assertFalse(ob["primary_checking_set"])
        self.assertEqual(ob["checking_candidates"], ["manual:chk-only"])
        r = self.client.post("/api/settings",
                             json={"wizard_steps": {"connect": "done"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["checking_account_id"], "manual:chk-only")
        ob = self.client.get("/api/onboarding").json()
        self.assertTrue(ob["primary_checking_set"])
        self.assertEqual(ob["primary_checking_id"], "manual:chk-only")

    def test_two_checking_accounts_are_a_question_not_a_guess(self):
        """With two eligible checking accounts nothing is pinned: the
        wizard asks, and both are offered as candidates."""
        from oikonome.engine import budget, forecast
        conn = self._conn()
        try:
            budget.save_config(conn, {k: v for k, v in budget.load_config(conn).items()
                                      if k != "checking_account_id"})
            conn.execute("DELETE FROM accounts")
            self._add_checking(conn, "manual:chk-a", "Checking A", 500)
            self._add_checking(conn, "manual:chk-b", "Checking B", 50)
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
        finally:
            conn.close()
        self.client.post("/api/settings",
                         json={"wizard_steps": {"connect": "done"}})
        ob = self.client.get("/api/onboarding").json()
        self.assertFalse(ob["primary_checking_set"])
        self.assertEqual(ob["checking_candidates"],
                         ["manual:chk-a", "manual:chk-b"])
        # an existing pin is never moved by the auto-pick
        conn = self._conn()
        try:
            with budget.config_txn(conn) as cfg:
                cfg["checking_account_id"] = "manual:chk-b"
            conn.execute("DELETE FROM accounts WHERE id='manual:chk-a'")
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
            self.assertEqual(budget.load_config(conn)["checking_account_id"],
                             "manual:chk-b")
        finally:
            conn.close()

    def test_a_disconnected_banks_account_is_neither_pinned_nor_pickable(self):
        """A bank that was disconnected (or restored as a shell) left its
        accounts behind as history: they are not candidates, the auto-pick
        never takes one, and the pin door refuses one — a frozen balance
        is no anchor for the cash forecast."""
        from oikonome.engine import budget, forecast
        conn = self._conn()
        try:
            budget.save_config(conn, {k: v for k, v in budget.load_config(conn).items()
                                      if k != "checking_account_id"})
            conn.execute("DELETE FROM accounts")
            conn.execute(
                """INSERT INTO items (id, aggregator, institution_name,
                                      access_token, status)
                   SELECT 'pl-dead','plaid','Old Bank',NULL,'archived'
                   WHERE NOT EXISTS (SELECT 1 FROM items WHERE id='pl-dead')""")
            conn.execute(
                """INSERT INTO accounts (id, item_id, name, type, subtype,
                                         balance_current, updated_at)
                   VALUES ('pl-dead:chk','pl-dead','Old checking','depository',
                           'checking',900,now())""")
            self.assertEqual(forecast.eligible_checking_ids(
                conn, budget.load_config(conn)), [])
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
        finally:
            conn.close()
        r = self.client.post("/api/accounts/classify",
                             json={"account_id": "pl-dead:chk",
                                   "kind": "depository/checking",
                                   "primary_checking": True})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_restored_manual_checking_account_is_still_live(self):
        """An export restore leaves manual and file-import items wearing
        'restored' too; only an aggregator's restored shell is a dead
        bank. The manual checking stays a candidate, is auto-pinned, and
        the pin counts on the checklist."""
        from oikonome.engine import budget, forecast
        conn = self._conn()
        try:
            budget.save_config(conn, {k: v for k, v in budget.load_config(conn).items()
                                      if k != "checking_account_id"})
            conn.execute("DELETE FROM accounts")
            conn.execute(
                """INSERT INTO items (id, aggregator, institution_name,
                                      access_token, status)
                   SELECT 'manual','manual','Manual',NULL,'restored'
                   WHERE NOT EXISTS (SELECT 1 FROM items WHERE id='manual')""")
            conn.execute("UPDATE items SET status='restored' WHERE id='manual'")
            self._add_checking(conn, "manual:restored-chk", "Checking", 250)
            self.assertEqual(forecast.eligible_checking_ids(
                conn, budget.load_config(conn)), ["manual:restored-chk"])
            self.assertEqual(forecast.auto_pin_primary_checking(conn),
                             "manual:restored-chk")
        finally:
            conn.close()
        ob = self.client.get("/api/onboarding").json()
        self.assertTrue(ob["primary_checking_set"])
        self.assertEqual(ob["primary_checking_id"], "manual:restored-chk")

    def test_closed_imports_without_a_balance_are_not_offered_next_to_a_live_account(self):
        """A household that imported years of history carries the closed
        accounts those files described; none has a balance. They are
        eligible — the forecast may fall through to them — but the
        question "which account does the cash forecast anchor on?" offers
        only the accounts that carry a balance, so the wizard's pills and
        the Today checklist do not list an old import beside the live
        checking. With every account balance-less (a files-only
        first import) they are all offered, as before."""
        from oikonome.engine import budget, forecast
        conn = self._conn()
        try:
            budget.save_config(conn, {k: v for k, v in budget.load_config(conn).items()
                                      if k != "checking_account_id"})
            conn.execute("DELETE FROM accounts")
            self._add_checking(conn, "manual:live-a", "Checking", 500)
            self._add_checking(conn, "manual:live-b", "Cash", 50)
            conn.execute(
                """INSERT INTO accounts (id, item_id, name, type, subtype,
                                         balance_current, updated_at)
                   VALUES ('old-bank','manual','Old Bank checking',
                           'depository','checking',NULL,now())""")
            cfg = budget.load_config(conn)
            self.assertIn("old-bank", forecast.eligible_checking_ids(conn, cfg))
            self.assertEqual(forecast.pickable_checking_ids(conn, cfg),
                             ["manual:live-a", "manual:live-b"])
            # nothing carries a balance: every eligible account is offered
            conn.execute("UPDATE accounts SET balance_current=NULL")
            self.assertEqual(
                sorted(forecast.pickable_checking_ids(conn, cfg)),
                ["manual:live-a", "manual:live-b", "old-bank"])
        finally:
            conn.close()
        ob = self.client.get("/api/onboarding").json()
        self.assertFalse(ob["primary_checking_set"])
        self.assertEqual(sorted(ob["checking_candidates"]),
                         ["manual:live-a", "manual:live-b", "old-bank"])

    def test_a_dead_pin_is_replaced_where_the_forecast_already_anchors(self):
        """A pin carried over from an export whose accounts were re-created
        (or left behind by a purge) names a row that no longer exists; a
        removal by the household makes a row gone too. A relink keeps the
        old row on the archived Item, so it is the KEPT case below, not
        this one. The forecast falls through to the
        largest balance, and the auto-pick moves the pin there — the
        household chose once and is not asked again — while a household
        that never pinned is still asked. A dead pin whose row STILL
        EXISTS (its bank disconnected, reaped or restored as a shell) is
        kept: the reconnect's adoption carries it onto the new Item, and
        moving it meanwhile would lose the choice for good."""
        from oikonome.engine import budget, forecast
        conn = self._conn()
        try:
            conn.execute("DELETE FROM accounts")
            self._add_checking(conn, "manual:chk-big", "Checking", 900)
            self._add_checking(conn, "manual:chk-small", "Cash", 20)
            # the pinned account sits on a disconnected bank: dead, kept
            conn.execute(
                """INSERT INTO items (id, aggregator, institution_name,
                                      access_token, status)
                   SELECT 'pl-away','plaid','Away Bank',NULL,'archived'
                   WHERE NOT EXISTS (SELECT 1 FROM items WHERE id='pl-away')""")
            conn.execute(
                """INSERT INTO accounts (id, item_id, name, type, subtype,
                                         balance_current, updated_at)
                   VALUES ('pl-away:chk','pl-away','Chosen checking',
                           'depository','checking',2000,now())""")
            with budget.config_txn(conn) as cfg:
                cfg["checking_account_id"] = "pl-away:chk"
            self.assertFalse(forecast.pin_is_live(conn, budget.load_config(conn)))
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
            self.assertEqual(budget.load_config(conn)["checking_account_id"],
                             "pl-away:chk")
            # …and kept just the same when one other account is the ONLY
            # pickable one — the case the one-account auto-pin would
            # otherwise have taken
            conn.execute("DELETE FROM accounts WHERE id='manual:chk-small'")
            self.assertEqual(forecast.pickable_checking_ids(
                conn, budget.load_config(conn)), ["manual:chk-big"])
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
            self.assertEqual(budget.load_config(conn)["checking_account_id"],
                             "pl-away:chk")
            self._add_checking(conn, "manual:chk-small", "Cash", 20)
            # the household removes that account: the pin is gone for good
            conn.execute("UPDATE accounts SET user_removed_at=now() "
                         "WHERE id='pl-away:chk'")
            self.assertEqual(forecast.auto_pin_primary_checking(conn),
                             "manual:chk-big")
            # a pin on an id no row carries (an export's pin, a purge)
            with budget.config_txn(conn) as cfg:
                cfg["checking_account_id"] = "plaid:gone-with-the-relink"
            self.assertFalse(forecast.pin_is_live(conn, budget.load_config(conn)))
            self.assertEqual(forecast.auto_pin_primary_checking(conn),
                             "manual:chk-big")
            self.assertTrue(forecast.pin_is_live(conn, budget.load_config(conn)))
            # a live pin is never moved, even off the smaller account
            with budget.config_txn(conn) as cfg:
                cfg["checking_account_id"] = "manual:chk-small"
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
            # never pinned + two candidates: still a question
            with budget.config_txn(conn) as cfg:
                cfg.pop("checking_account_id", None)
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
        finally:
            conn.close()
        ob = self.client.get("/api/onboarding").json()
        self.assertFalse(ob["primary_checking_set"])

    def test_an_excluded_checking_account_is_not_pinned(self):
        """An account the household hid from every total is not its
        checking account — the auto-pick skips it the way the forecast's
        own pick does."""
        from oikonome.engine import budget, forecast
        conn = self._conn()
        try:
            with budget.config_txn(conn) as cfg:
                cfg.pop("checking_account_id", None)
                cfg["excluded_accounts"] = ["manual:chk-x"]
            conn.execute("DELETE FROM accounts")
            self._add_checking(conn, "manual:chk-x", "Old checking")
            self.assertIsNone(forecast.auto_pin_primary_checking(conn))
            self.assertEqual(forecast.eligible_checking_ids(
                conn, budget.load_config(conn)), [])
        finally:
            conn.close()

    def test_onboarding_counts_connections_not_children(self):
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, "sfin-main", "simplefin", "SimpleFIN",
                                  "https://u:p@bridge.test/simplefin")
            simplefin.sync(conn, "sfin-main",
                           "https://u:p@bridge.test/simplefin",
                           transport=_transport())
        finally:
            conn.close()
        ob = self.client.get("/api/onboarding").json()
        # the bridge is ONE connection; its per-bank children don't inflate
        self.assertEqual(ob["connections"], 1)

    def test_history_coverage(self):
        from .util import seed_accounts
        conn = self._conn()
        try:
            seed_accounts(conn)
            add_txn(conn, "2026-01-05", 20, "OLD", account="chk")
            add_txn(conn, "2026-07-01", 30, "NEW", account="chk")
        finally:
            conn.close()
        cov = self.client.get("/api/history/coverage").json()["accounts"]
        chk = next(a for a in cov if a["id"] == "chk")
        self.assertEqual(chk["earliest"], "2026-01-05")
        self.assertEqual(chk["latest"], "2026-07-01")
        self.assertGreaterEqual(chk["transactions"], 2)
        # accounts with no history report null bounds, not a crash
        card = next(a for a in cov if a["id"] == "card")
        self.assertIsNone(card["earliest"])

    def test_background_sync_progress_lifecycle(self):
        from oikonome.jobs import worker

        def fake_sync(tid, since_days=30, progress=None):
            progress("item", {"id": "i1", "name": "Bank A",
                              "status": "running", "transactions": 0})
            progress("item", {"id": "i1", "name": "Bank A",
                              "status": "ok", "transactions": 42})
            progress("categorize", {"done": 5, "total": 5})
            return {"i1": "ok:42"}

        with mock.patch.object(worker, "sync_tenant", side_effect=fake_sync):
            r = self.client.post("/api/jobs/sync/start")
            self.assertTrue(r.json()["started"])
            deadline = time.time() + 10
            st = {}
            while time.time() < deadline:
                st = self.client.get("/api/jobs/sync/status").json()
                if st["state"] in ("done", "error"):
                    break
                time.sleep(0.1)
        self.assertEqual(st["state"], "done")
        items = st["progress"]["items"]
        self.assertEqual(items, [{"id": "i1", "name": "Bank A",
                                  "status": "ok", "transactions": 42}])
        self.assertEqual(st["progress"]["categorize"],
                         {"done": 5, "total": 5})
        self.assertTrue(st["progress"]["ok"])

    def test_disconnect_plaid_releases_item_and_archives(self):
        from unittest import mock as _mock

        from oikonome.sync import plaid as plaid_mod
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, "plaid-x", "plaid", "Chase",
                                  "access-token-x")
        finally:
            conn.close()
        fake = _mock.MagicMock()
        with _mock.patch.object(plaid_mod.Client, "for_tenant",
                                return_value=fake):
            r = self.client.post(
                "/api/connections/plaid-x/disconnect").json()
        self.assertTrue(r["ok"] and r["plaid_released"])
        fake.remove_item.assert_called_once_with("access-token-x")
        conn = self._conn()
        try:
            self.assertEqual(conn.execute(
                "SELECT status FROM items WHERE id='plaid-x'"
            ).fetchone()["status"], "archived")
        finally:
            conn.close()

    def test_second_start_while_running_is_refused(self):
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO job_progress (id, state, progress)
                   VALUES ('sync','running','{}'::jsonb)
                   ON CONFLICT (tenant_id, id) DO UPDATE SET
                       state='running', started_at=now(), updated_at=now()""")
        finally:
            conn.close()
        r = self.client.post("/api/jobs/sync/start").json()
        self.assertFalse(r["started"])
        # ... but a STALE running row (died worker) is reclaimed
        conn = self._conn()
        try:
            conn.execute("UPDATE job_progress SET updated_at="
                         "now() - interval '20 minutes' WHERE id='sync'")
        finally:
            conn.close()
        from oikonome.jobs import worker
        with mock.patch.object(worker, "sync_tenant", return_value={}):
            r = self.client.post("/api/jobs/sync/start").json()
        self.assertTrue(r["started"])


if __name__ == "__main__":
    unittest.main()
