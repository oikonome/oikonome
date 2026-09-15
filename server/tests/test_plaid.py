"""Plaid connector: no sign flip, cursor persisted only after full
pagination, removed-flagging, error status. Mocked HTTP throughout."""

import json
import unittest

import httpx

from oikonome.sync import base as sync_base
from oikonome.sync import plaid

from .util import add_txn, make_db, write_config

ACCOUNTS = {"accounts": [
    {"account_id": "p-chk", "name": "Plaid Checking", "type": "depository",
     "subtype": "checking", "mask": "1232",
     "balances": {"current": 3000.0, "available": 2900.0,
                  "iso_currency_code": "USD"}}]}

PAGE1 = {"added": [
    {"transaction_id": "pt1", "account_id": "p-chk", "date": "2026-07-10",
     "amount": 42.5, "name": "SAFEWAY", "merchant_name": "Safeway",
     "pending": False,
     "personal_finance_category": {"primary": "FOOD_AND_DRINK",
                                   "detailed": "FOOD_AND_DRINK_GROCERIES"}}],
    "modified": [], "removed": [], "has_more": True, "next_cursor": "c1"}
PAGE2 = {"added": [
    {"transaction_id": "pt2", "account_id": "p-chk", "date": "2026-07-11",
     "amount": -2000.0, "name": "PAYROLL", "pending": False}],
    "modified": [], "removed": [{"transaction_id": "pt1"}],
    "has_more": False, "next_cursor": "c2"}


def _transport(pages, fail_on_page: int | None = None):
    state = {"n": 0}

    def handler(request):
        path = request.url.path
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(ACCOUNTS))
        if path == "/transactions/sync":
            i = state["n"]
            state["n"] += 1
            if fail_on_page is not None and i == fail_on_page:
                return httpx.Response(500, text=json.dumps(
                    {"error_code": "INTERNAL_SERVER_ERROR",
                     "error_type": "API_ERROR", "error_message": "boom"}))
            return httpx.Response(200, text=json.dumps(pages[min(i, len(pages) - 1)]))
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": "plaid-item-1", "access_token": "access-prod-abc"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class PlaidTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    def test_link_then_sync_no_sign_flip(self):
        item = plaid.link_item(self.conn, "public-xyz", "Demo Bank",
                               transport=_transport([PAGE1, PAGE2]))
        self.assertEqual(item, "plaid-item-1")
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, amount, removed, category_primary FROM transactions "
            "WHERE id LIKE 'pt%'").fetchall()}
        # Plaid signs are ALREADY engine signs: 42.5 stays +42.5 (money out)
        self.assertEqual(rows["pt1"]["amount"], 42.5)
        self.assertEqual(rows["pt2"]["amount"], -2000.0)
        self.assertEqual(rows["pt1"]["category_primary"], "FOOD_AND_DRINK")
        # pt1 was removed by page 2 (pending → posted replacement pattern)
        self.assertEqual(rows["pt1"]["removed"], 1)
        it = self.conn.execute(
            "SELECT tx_cursor, status FROM items WHERE id='plaid-item-1'"
        ).fetchone()
        self.assertEqual(it["tx_cursor"], "c2")
        self.assertEqual(it["status"], "ok")
        bal = self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='p-chk'").fetchone()
        self.assertEqual(bal["balance_current"], 3000.0)

    def test_cursor_survives_mid_pagination_failure(self):
        """A failure on page 2 must leave the cursor UNTOUCHED (full re-run
        next sync — Plaid's mutation-recovery rule). Pages are applied as
        they arrive so the setup wizard's import bar moves, so page 1's
        rows ARE on disk after the failure — and the re-run from the old
        cursor must converge on exactly the full set: upserts by id, no
        duplicates, page 2's removal honoured."""
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")
        self.conn.execute("UPDATE items SET tx_cursor='c0' WHERE id='it-p'")
        with self.assertRaises(plaid.PlaidError):
            plaid.sync(self.conn, "it-p",
                       transport=_transport([PAGE1, PAGE2], fail_on_page=1))
        it = self.conn.execute(
            "SELECT tx_cursor, status FROM items WHERE id='it-p'").fetchone()
        self.assertEqual(it["tx_cursor"], "c0")          # unchanged
        self.assertTrue(it["status"].startswith("error:"))
        err = self.conn.execute(
            "SELECT error FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn("INTERNAL_SERVER_ERROR", err["error"])
        # the retry replays from c0 and lands the full, deduplicated set
        plaid.sync(self.conn, "it-p", transport=_transport([PAGE1, PAGE2]))
        rows = self.conn.execute(
            "SELECT id, COALESCE(removed,0) AS removed FROM transactions "
            "WHERE id LIKE 'pt%' ORDER BY id").fetchall()
        self.assertEqual([(r["id"], r["removed"]) for r in rows],
                         [("pt1", 1), ("pt2", 0)])
        self.assertEqual(self.conn.execute(
            "SELECT tx_cursor FROM items WHERE id='it-p'"
        ).fetchone()["tx_cursor"], "c2")

    def test_resync_idempotent(self):
        plaid.link_item(self.conn, "public-xyz", "Demo Bank",
                        transport=_transport([PAGE1, PAGE2]))
        plaid.sync(self.conn, "plaid-item-1",
                   transport=_transport([PAGE2]))       # replays fine
        n = self.conn.execute("SELECT COUNT(*) AS n FROM transactions "
                              "WHERE id LIKE 'pt%'").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_import_rows_suppress_overlapping_plaid_rows(self):
        """Plaid side: a charge already present from a
        file import (on a DIFFERENT, manual account) must not re-insert via
        the aggregator; genuinely new rows in the same pull still land."""
        add_txn(self.conn, "2026-07-10", 42.5, "Safeway Store",
                account="chk", txn_id="csv:feedbeef01")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")
        r = plaid.sync(self.conn, "it-p",
                       transport=_transport([PAGE1, PAGE2]))
        self.assertEqual(r["skipped_import_duplicates"], 1)
        ids = {row["id"] for row in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'pt%'").fetchall()}
        self.assertNotIn("pt1", ids)             # covered by the csv row
        self.assertIn("pt2", ids)                # payroll: no import match
        # hourly replay: still no creep
        r = plaid.sync(self.conn, "it-p", transport=_transport([PAGE1, PAGE2]))
        n = self.conn.execute("SELECT COUNT(*) AS n FROM transactions "
                              "WHERE id LIKE 'pt%'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_one_import_row_covers_one_plaid_row_across_pages(self):
        """Pages are applied as they arrive, but the import-overlap guard's
        one-to-one consumption must span the whole pull: one legacy CSV
        row covers ONE aggregator row per sync, even when two genuine
        same-amount charges land on either side of a page boundary. A
        fresh guard per page re-offers the consumed row to the next page,
        and both charges are dropped."""
        add_txn(self.conn, "2026-07-10", 42.5, "Safeway Store",
                account="chk", txn_id="csv:feedbeef02")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")
        pa = {"added": [dict(PAGE1["added"][0], transaction_id="ptA",
                             date="2026-07-09")],
              "modified": [], "removed": [], "has_more": True,
              "next_cursor": "cA"}
        pb = {"added": [dict(PAGE1["added"][0], transaction_id="ptB",
                             date="2026-07-11")],
              "modified": [], "removed": [], "has_more": False,
              "next_cursor": "cB"}
        r = plaid.sync(self.conn, "it-p", transport=_transport([pa, pb]))
        self.assertEqual(r["skipped_import_duplicates"], 1)
        ids = {row["id"] for row in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'pt%'").fetchall()}
        self.assertEqual(len(ids & {"ptA", "ptB"}), 1)   # one kept, one covered

    def test_settlement_swap_keeps_user_state(self):
        """A posted transaction REPLACES its pending twin under a new id,
        with the old id in pending_transaction_id. Anything the user did to
        the pending row — recategorize, owner, a note — must survive the
        swap, or it dies with the removed row a day or two after they did
        it: an override quietly reverting to the aggregator's own category
        at settlement."""
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")
        pending_page = {"added": [
            {"transaction_id": "pt-pend", "account_id": "p-chk",
             "date": "2026-08-01", "amount": 40.00,
             "name": "BRIGHT PATH SITTING", "pending": True,
             "personal_finance_category": {"primary": "PERSONAL_CARE"}}],
            "modified": [], "removed": [],
            "has_more": False, "next_cursor": "cA"}
        plaid.sync(self.conn, "it-p", transport=_transport([pending_page]))
        # the user's word, while the row is still pending
        self.conn.execute(
            "UPDATE transactions SET category_override='CHILD CARE', "
            "owner_override='sam' WHERE id='pt-pend'")
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) "
            "VALUES ('pt-pend', 'sitter for friday')")
        settle_page = {"added": [
            {"transaction_id": "pt-post", "account_id": "p-chk",
             "date": "2026-08-02", "amount": 40.00,
             "name": "BRIGHT PATH SITTING", "pending": False,
             "pending_transaction_id": "pt-pend",
             "personal_finance_category": {"primary": "PERSONAL_CARE"}}],
            "modified": [], "removed": [{"transaction_id": "pt-pend"}],
            "has_more": False, "next_cursor": "cB"}
        plaid.sync(self.conn, "it-p", transport=_transport([settle_page]))
        row = self.conn.execute(
            "SELECT category_override, owner_override, removed, "
            "pending_transaction_id FROM transactions WHERE id='pt-post'"
        ).fetchone()
        self.assertEqual(row["category_override"], "CHILD CARE")
        self.assertEqual(row["owner_override"], "sam")
        self.assertEqual(row["pending_transaction_id"], "pt-pend")
        self.assertEqual(row["removed"], 0)
        old = self.conn.execute(
            "SELECT removed FROM transactions WHERE id='pt-pend'").fetchone()
        self.assertEqual(old["removed"], 1)
        note = self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id='pt-post'"
        ).fetchone()
        self.assertEqual(note["note"], "sitter for friday")

    def test_settlement_swap_never_overwrites_the_posted_rows_own_state(self):
        """COALESCE, not copy: if the user has ALREADY recategorized the
        posted row, a replayed sync must not clobber it with the dead
        pending row's older word."""
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")
        settle_page = {"added": [
            {"transaction_id": "pt-post2", "account_id": "p-chk",
             "date": "2026-08-02", "amount": 12.0,
             "name": "COFFEE", "pending": False,
             "pending_transaction_id": "pt-gone",
             "personal_finance_category": {"primary": "FOOD_AND_DRINK"}}],
            "modified": [], "removed": [],
            "has_more": False, "next_cursor": "cC"}
        plaid.sync(self.conn, "it-p", transport=_transport([settle_page]))
        self.conn.execute(
            "UPDATE transactions SET category_override='BUSINESS MEALS' "
            "WHERE id='pt-post2'")
        # pending twin appears late (out-of-order replay) with its own word
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed, category_override) VALUES "
            "('pt-gone','p-chk','2026-08-01',12.0,'COFFEE',1,1,'SNACKS')")
        plaid.sync(self.conn, "it-p", transport=_transport([settle_page]))
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id='pt-post2'"
        ).fetchone()
        self.assertEqual(row["category_override"], "BUSINESS MEALS")

    def test_missing_credentials(self):
        write_config(self.conn)                          # wipes plaid_* keys
        with self.assertRaises(plaid.PlaidError) as cm:
            plaid.Client.for_tenant(self.conn)
        self.assertEqual(cm.exception.code, "NO_CREDENTIALS")


if __name__ == "__main__":
    unittest.main()


class HostedLinkSessionStoreTests(unittest.TestCase):
    """One tenant's link flows must not evict another's.

    `_PLAID_LINK` had a GLOBAL 20-entry cap and evicted in insertion order,
    so on a busy hosted box one household starting link sessions could push
    out a different household's in-flight one — whose link page then
    redirected to /accounts with nothing said. The cap is per tenant now,
    and abandoned sessions age out instead of surviving as the oldest entry.
    """

    def setUp(self):
        from oikonome.web import pages
        self.pages = pages
        self._saved = dict(pages._PLAID_LINK)
        pages._PLAID_LINK.clear()

    def tearDown(self):
        self.pages._PLAID_LINK.clear()
        self.pages._PLAID_LINK.update(self._saved)

    def _fill(self, tid, n, at=None):
        import time as _t
        for i in range(n):
            self.pages._PLAID_LINK[f"{tid}-tok{i}"] = {
                "tenant_id": tid, "at": at if at is not None else _t.time(),
                "item_id": None, "kind": "add", "institution": None,
                "url": "u", "done": False}

    def test_the_cap_counts_only_the_tenants_own_sessions(self):
        self._fill("tenant-a", self.pages._PLAID_LINK_MAX)
        self._fill("tenant-b", 3)
        # tenant-b's three must survive a tenant-a overflow
        surviving_b = [k for k, v in self.pages._PLAID_LINK.items()
                       if v["tenant_id"] == "tenant-b"]
        self.assertEqual(len(surviving_b), 3)

    def test_expired_sessions_are_dropped_by_age(self):
        import time as _t
        self._fill("tenant-a", 1, at=_t.time() - self.pages._PLAID_LINK_TTL - 1)
        stale = list(self.pages._PLAID_LINK)
        now = _t.time()
        for k in [k for k, v in self.pages._PLAID_LINK.items()
                  if now - v.get("at", now) > self.pages._PLAID_LINK_TTL]:
            self.pages._PLAID_LINK.pop(k)
        self.assertTrue(stale)
        self.assertEqual(self.pages._PLAID_LINK, {})

    def test_the_store_records_when_a_session_started(self):
        """Without `at` the age-based eviction has nothing to read."""
        import inspect
        src = inspect.getsource(self.pages._start_plaid_session)
        self.assertIn('"at": now', src)
        self.assertIn('v["tenant_id"] == tid', src)
