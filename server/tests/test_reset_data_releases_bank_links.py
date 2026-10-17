"""reset-data releases bank links at the provider before it deletes them.

The items table holds the only copy of each linked bank's access token. A
data reset that deletes those rows without calling the provider leaves
every Item live and billed at the aggregator with no handle left to remove
it, and the wizard that runs next links the same banks a second time. So
the reset releases first (best effort: a provider outage must not make the
reset impossible), records what did not release so an operator can finish
it from the provider's dashboard, and does the wipe while every background
writer of the tenant is held off, in one transaction.
"""

import json
import unittest
import uuid
from unittest import mock

import httpx

from oikonome import erasure
from oikonome.db import reset, tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import mx, plaid

from .util import make_db, write_config


def _transport(fail_tokens: set, seen: list):
    def handler(request):
        if request.url.path == "/item/remove":
            tok = json.loads(request.content.decode())["access_token"]
            seen.append(tok)
            if tok in fail_tokens:
                return httpx.Response(500, text=json.dumps(
                    {"error_code": "INTERNAL_SERVER_ERROR"}))
            return httpx.Response(200, text=json.dumps({"request_id": "r"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class ResetDataReleasesBankLinksTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec")
        self.good = f"pl-ok-{uuid.uuid4().hex[:8]}"
        self.bad = f"pl-bad-{uuid.uuid4().hex[:8]}"
        sync_base.upsert_item(self.conn, self.good, "plaid", "A", "tok-good")
        sync_base.upsert_item(self.conn, self.bad, "plaid", "B", "tok-bad")
        self.seen: list = []

    def _patch_client(self, fail_tokens=frozenset()):
        seen = self.seen

        def for_tenant(conn, transport=None):
            return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                                transport=_transport(set(fail_tokens), seen))
        return mock.patch.object(plaid.Client, "for_tenant",
                                 staticmethod(for_tenant))

    def _pending(self):
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                "SELECT detail FROM admin_audit WHERE "
                "action='external_release_pending' AND target=%s",
                (self.tid,)).fetchall()
        finally:
            admin.close()
        return [r["detail"] if isinstance(r["detail"], dict)
                else json.loads(r["detail"]) for r in rows]

    def _items_left(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM items").fetchone()["n"]

    def test_every_linked_item_is_removed_at_the_provider(self):
        with self._patch_client():
            reset.reset_tenant_data(self.tid)
        self.assertEqual(sorted(self.seen), ["tok-bad", "tok-good"],
                         "the reset deleted the access tokens without "
                         "releasing their Items, which keep billing")
        self.assertEqual(self._items_left(), 0)
        self.assertEqual(self._pending(), [])

    def test_the_release_runs_before_the_rows_go_and_under_the_writer_locks(self):
        observed = {}
        real = plaid.release_tenant_items

        def spy(tenant_id, *a, **kw):
            observed["items_at_release"] = self._items_left()
            probe = tenancy.admin_connect()
            try:
                observed["writer_could_start"] = [
                    probe.execute("SELECT pg_try_advisory_lock(hashtext(%s)) "
                                  "AS ok", (k,)).fetchone()["ok"]
                    for k in erasure.writer_lock_keys(tenant_id)]
                probe.execute("SELECT pg_advisory_unlock_all()")
            finally:
                probe.close()
            return real(tenant_id, *a, **kw)

        before = self._items_left()
        with self._patch_client(), \
                mock.patch.object(plaid, "release_tenant_items", spy):
            reset.reset_tenant_data(self.tid)
        self.assertEqual(observed["items_at_release"], before)
        self.assertEqual(observed["writer_could_start"],
                         [False] * len(erasure.WRITER_LOCKS),
                         "a background writer could write into the tenant "
                         "while it was being wiped")

    def test_a_partial_failure_still_resets_and_leaves_a_retry_handle(self):
        with self._patch_client({"tok-bad"}):
            result = reset.reset_tenant_data(self.tid)
        self.assertEqual(self._items_left(), 0)
        self.assertIn("items", result)
        self.assertEqual(result.released.get("plaid_failed"), [self.bad])
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["retry"]["plaid_failed_item_ids"],
                         [self.bad])

    def test_the_cli_names_what_could_not_be_released(self):
        out = []
        with self._patch_client({"tok-bad"}), \
                mock.patch("builtins.print",
                           lambda *a, **kw: out.append(" ".join(map(str, a)))):
            self.assertEqual(reset.main(["reset", self.tid]), 0)
        text = "\n".join(out)
        self.assertIn(self.bad, text,
                      "the operator is not told which bank link is still "
                      "live at the provider")

    def test_a_provider_outage_does_not_block_the_reset(self):
        def boom(tenant_id, *a, **kw):
            raise RuntimeError("provider down")
        with mock.patch.object(plaid, "release_tenant_items", boom):
            result = reset.reset_tenant_data(self.tid)
        self.assertEqual(self._items_left(), 0)
        self.assertEqual(result.released.get("plaid_failed"), "all")
        self.assertEqual(
            sorted(self._pending()[0]["retry"]["plaid_failed_item_ids"]),
            sorted([self.good, self.bad]))

    def test_the_mx_user_is_released_but_the_subscription_is_not_touched(self):
        calls = []
        with self._patch_client(), \
                mock.patch.object(mx, "release_tenant_user",
                                  lambda tid, *a, **kw:
                                  calls.append(("mx", tid)) or "deleted"), \
                mock.patch("oikonome.ext.gate.release_tenant",
                           lambda tid: calls.append(("addon", tid)) or "x"):
            reset.reset_tenant_data(self.tid)
        # the logins (and whatever the household pays for) survive a data
        # reset, so the add-on's erasure release must not run
        self.assertEqual(calls, [("mx", self.tid)])

    def test_release_only_mode_keeps_the_rows(self):
        with self._patch_client():
            released = reset.release_links_only(self.tid)
        self.assertEqual(sorted(self.seen), ["tok-bad", "tok-good"])
        self.assertEqual(reset.unreleased(released), [])
        self.assertGreater(self._items_left(), 0)

    def test_the_summary_counts_the_bank_links(self):
        text = reset.summary([(self.tid, "owner@example.dev")])
        self.assertIn(f"{self._items_left()} bank link", text)


class OpenLinksAndQueuedReleasesAreNotLostTests(unittest.TestCase):
    """A reset must not silently drop the two other handles on live Items:
    a link the person finished at the bank that nobody exchanged yet (an
    open session), and a release that failed earlier and waits in
    plaid_release_queue. Open sessions are handed back before the release;
    the full instance reset, which deletes the queue with the database,
    retries it once and names every Item still in it."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec")
        self.lt = f"lt-reset-{uuid.uuid4().hex[:8]}"
        from oikonome.jobs import plaid_links
        plaid_links.record(self.tid, self.lt, "add", None, False, None)
        self.queued = f"pl-queued-{uuid.uuid4().hex[:8]}"
        self.held = f"pl-hold-{uuid.uuid4().hex[:8]}"
        admin = tenancy.admin_connect()
        try:
            for iid, why in ((self.queued, "erasure"),
                             (self.held, plaid.LINK_HOLD_REASON)):
                admin.execute(
                    "INSERT INTO plaid_release_queue (item_id, tenant, "
                    "access_token, client_id, base_url, reason) "
                    "VALUES (%s, %s, 'sealed', 'cid', 'https://x', %s)",
                    (iid, self.tid, why))
        finally:
            admin.close()
        self.addCleanup(self._drop_queue)

    def _drop_queue(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_release_queue "
                          "WHERE item_id = ANY(%s)",
                          ([self.queued, self.held],))
            admin.execute("DELETE FROM plaid_link_sessions WHERE tenant=%s",
                          (self.tid,))
        finally:
            admin.close()

    def _sessions_left(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM plaid_link_sessions "
                "WHERE tenant=%s", (self.tid,)).fetchone()["n"]
        finally:
            admin.close()

    def test_a_household_reset_hands_back_open_links_and_refuses_the_rest(self):
        from oikonome.jobs import plaid_links
        handed = []
        with mock.patch.object(plaid_links, "hand_back_open_sessions",
                               lambda tid, *a, **k:
                               handed.append(str(tid)) or {}), \
                mock.patch.object(plaid, "release_tenant_items",
                                  lambda *a, **k: 0):
            reset.reset_tenant_data(self.tid)
        self.assertEqual(handed, [self.tid])
        # still open at the bank: kept on record as the only handle on the
        # Item it may yet make, but never again offered or counted
        self.assertEqual(self._sessions_left(), 1)
        sess = plaid_links.load(self.lt, self.tid)
        self.assertEqual(sess["refuse"], reset.RESET_REFUSAL)
        self.assertEqual(plaid_links.open_sessions(self.tid), [])
        self.assertEqual(plaid_links.recent_open(self.tid, 3600), set())

    def test_a_link_finished_after_the_reset_hands_its_bank_back(self):
        from oikonome.jobs import plaid_links
        with mock.patch.object(plaid_links, "hand_back_open_sessions",
                               lambda tid, *a, **k: {}), \
                mock.patch.object(plaid, "release_tenant_items",
                                  lambda *a, **k: 0):
            reset.reset_tenant_data(self.tid)
        # the person finishes at the bank after the reset; the LINK webhook
        # resolves the token and the worker finishes the session
        self.assertEqual(plaid_links.tenant_for_link(self.lt), self.tid)
        iid = f"pl-late-{uuid.uuid4().hex[:8]}"
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/link/token/get":
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": [{"results": {"item_add_results": [
                        {"public_token": "pub-late"}]}}]}))
            if request.url.path == "/item/public_token/exchange":
                return httpx.Response(200, text=json.dumps(
                    {"item_id": iid, "access_token": "access-late"}))
            if request.url.path == "/item/remove":
                return httpx.Response(200, text=json.dumps(
                    {"request_id": "r"}))
            return httpx.Response(404, text="{}")

        def for_tenant(conn, transport=None):
            return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                                transport=httpx.MockTransport(handler))
        with mock.patch.object(plaid.Client, "for_tenant",
                               staticmethod(for_tenant)):
            word = plaid_links.finish_session(self.tid, self.lt)
        self.assertEqual(word, "blocked")
        self.assertIn("/item/public_token/exchange", calls)
        self.assertIn("/item/remove", calls,
                      "the bank made after the reset was left billing")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM items WHERE id=%s", (iid,)).fetchone(),
            "the bank went into the household just wiped")
        reply = plaid_links.load(self.lt, self.tid)["reply"]
        self.assertEqual(reply["kind"], "add_failed")
        self.assertEqual(reply["error"], reset.RESET_REFUSAL)

    def test_the_full_reset_retries_the_queue_and_names_what_is_left(self):
        from oikonome.jobs import plaid_links, reaper
        handed, retried, out = [], [], []
        with mock.patch.object(reset, "all_tenants",
                               lambda: [(self.tid, "owner@example.dev")]), \
                mock.patch.object(plaid_links, "hand_back_open_sessions",
                                  lambda tid, *a, **k:
                                  handed.append(str(tid)) or {}), \
                mock.patch.object(plaid, "release_tenant_items",
                                  lambda *a, **k: 0), \
                mock.patch.object(mx, "release_tenant_user",
                                  lambda *a, **k: "none"), \
                mock.patch.object(reaper, "retry_queued_releases",
                                  lambda *a, **k: retried.append(1) or {}), \
                mock.patch("builtins.print",
                           lambda *a, **kw: out.append(" ".join(map(str, a)))):
            code = reset.main(["reset", "--release-links"])
        self.assertEqual(code, 1, "the caller was told everything released")
        self.assertEqual(handed, [self.tid])
        self.assertEqual(retried, [1])
        text = "\n".join(out)
        self.assertIn(self.queued, text)
        self.assertIn(self.held, text,
                      "a link held mid-exchange died with the database "
                      "unnamed")
        self.assertIn("NOT RELEASED", text)

    def test_the_full_reset_passes_when_the_retry_clears_the_queue(self):
        from oikonome.jobs import reaper

        def retry_releases_all(*a, **k):
            admin = tenancy.admin_connect()
            try:
                admin.execute("DELETE FROM plaid_release_queue")
            finally:
                admin.close()
            return {}
        with mock.patch.object(reset, "all_tenants",
                               lambda: [(self.tid, "owner@example.dev")]), \
                mock.patch.object(plaid, "release_tenant_items",
                                  lambda *a, **k: 0), \
                mock.patch.object(mx, "release_tenant_user",
                                  lambda *a, **k: "none"), \
                mock.patch.object(reaper, "retry_queued_releases",
                                  retry_releases_all), \
                mock.patch("builtins.print", lambda *a, **kw: None):
            self.assertEqual(reset.main(["reset", "--release-links"]), 0)


if __name__ == "__main__":
    unittest.main()
