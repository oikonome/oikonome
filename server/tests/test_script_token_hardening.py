"""Script-token blast radius.

Script tokens are long-lived credentials sitting in env files on the
collector host, so their power must stay strictly push-only:

- Restore: the import hub treats a ``.zip`` upload as a FULL-LEDGER RESTORE
  (``restore.restore_zip``). A leaked collector token must not be able to
  merge an attacker's export into the tenant — the hub route itself stays
  token-reachable (community scripts push CSVs through it), but restore
  content is refused for token principals.
- Namespacing: taking client-supplied account/item ids and a free-form
  stale-removal ``prefix`` at the Coinbase push door would let a token
  re-home a SimpleFIN account onto its item, overwrite balances, and mass
  soft-delete another source's rows (``prefix: "sfin:"``). Everything is
  namespaced server-side.
- Rate limit: script-token requests share a generous per-IP budget
  (collectors are hourly cron jobs).
"""

import csv
import datetime as dt
import io
import unittest
import uuid
import time
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import coinbase_push

from .util import _ensure_db, make_db, seed_accounts, write_config

TODAY = dt.date.today()


def _payload(**over) -> dict:
    """A legit collector payload (mirrors community-scripts/coinbase)."""
    p = {
        "prefix": "coinbase:",
        "items": [{"id": "coinbase-test", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                      "name": "Coinbase (test)", "type": "investment",
                      "subtype": "crypto", "balance_current": 1234.56}],
        "transactions": [
            {"id": "coinbase:t1", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": 100.0, "name": "BUY BTC"},
            {"id": "coinbase:t2", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": -25.0, "name": "SELL ETH"},
        ],
    }
    p.update(over)
    return p


class CoinbasePushNamespaceTests(unittest.TestCase):
    """The door only touches coinbase-namespaced rows."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # a foreign aggregator's item + account + txn — the door must
        # never be able to touch any of it
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES ('sfin-main','simplefin','SFIN Bank')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,balance_current) "
            "VALUES ('sfin:acct1','sfin-main','SFIN Checking',"
            "'depository',5000)")
        self.conn.execute(
            "INSERT INTO transactions (id,account_id,date,amount,name,"
            "removed,raw) VALUES ('sfin:t1','sfin:acct1',%s,10.0,"
            "'GROCERY',0,'{}'::jsonb)", (TODAY,))

    def tearDown(self):
        self.conn.close()

    def _sfin_state(self):
        acct = self.conn.execute(
            "SELECT item_id, balance_current FROM accounts "
            "WHERE id='sfin:acct1'").fetchone()
        txn = self.conn.execute(
            "SELECT removed FROM transactions WHERE id='sfin:t1'").fetchone()
        return acct["item_id"], acct["balance_current"], txn["removed"]

    def _assert_sfin_untouched(self):
        item_id, bal, removed = self._sfin_state()
        self.assertEqual(item_id, "sfin-main")
        self.assertEqual(bal, 5000)
        self.assertEqual(removed, 0)

    def test_cannot_rebind_a_foreign_account(self):
        # unguarded, upsert_accounts SET item_id=EXCLUDED.item_id re-homes
        # ANY existing account onto the coinbase item and overwrites its
        # balances
        p = _payload(accounts=[{"id": "sfin:acct1",
                                "item_id": "coinbase-test",
                                "name": "pwned", "type": "investment",
                                "balance_current": 0.01}],
                     transactions=[])
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, p)
        self._assert_sfin_untouched()

    def test_free_form_prefix_is_ignored_not_honored(self):
        # a soft-delete running `id LIKE prefix%` with the REQUEST's
        # prefix would let "sfin:" mass-retire another source's rows. The
        # prefix is DERIVED server-side ("coinbase:"), so a request prefix
        # is simply ignored: the push still succeeds and the foreign
        # source is never touched.
        out = coinbase_push.import_payload(self.conn, _payload(prefix="sfin:"))
        self.assertTrue(out["ok"])
        # only the pushed-account's own rows can ever be retired
        self.assertEqual(out["stale_removed"], 0)
        self._assert_sfin_untouched()

    def test_txn_ids_must_carry_the_coinbase_namespace(self):
        p = _payload(transactions=[
            {"id": "sfin:t1", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": 1.0, "name": "x"}])
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, p)
        self._assert_sfin_untouched()

    def test_txn_account_must_be_one_of_the_pushed_accounts(self):
        p = _payload(transactions=[
            {"id": "coinbase:evil", "account_id": "sfin:acct1",
             "date": TODAY.isoformat(), "amount": 1.0, "name": "x"}])
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, p)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='coinbase:evil'").fetchone())

    def test_item_ids_must_carry_the_coinbase_namespace(self):
        p = _payload(items=[{"id": "sfin-evil",
                             "institution_name": "Coinbase"}])
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, p)
        p = _payload(accounts=[{"id": "coinbase-test",
                                "item_id": "sfin-main",
                                "name": "x", "type": "investment"}],
                     transactions=[])
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, p)

    def test_cannot_hijack_namespaced_account_of_a_live_aggregator(self):
        # an id can LOOK namespaced and still belong to a live aggregator
        # (e.g. the built-in coinbase API connector is fine, plaid is not)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES ('coinbase-imposter','plaid','Real Bank')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,balance_current) "
            "VALUES ('coinbase-imposter','coinbase-imposter','Real',"
            "'depository',777)")
        p = _payload(accounts=[{"id": "coinbase-imposter",
                                "item_id": "coinbase-test",
                                "name": "pwned", "type": "investment",
                                "balance_current": 0.01}],
                     transactions=[])
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, p)
        row = self.conn.execute(
            "SELECT item_id, balance_current FROM accounts "
            "WHERE id='coinbase-imposter'").fetchone()
        self.assertEqual(row["item_id"], "coinbase-imposter")
        self.assertEqual(row["balance_current"], 777)

    def test_legit_push_still_round_trips(self):
        out = coinbase_push.import_payload(self.conn, _payload())
        self.assertTrue(out["ok"])
        self.assertEqual(out["pushed"], 2)
        self.assertEqual(out["accounts"]["coinbase-test"]["after"], 2)
        self.assertEqual(out["accounts"]["coinbase-test"]["balance"], 1234.56)
        # window-replace still works with the server-derived prefix
        p2 = _payload(transactions=_payload()["transactions"][:1])
        out = coinbase_push.import_payload(self.conn, p2)
        self.assertEqual(out["stale_removed"], 1)
        self._assert_sfin_untouched()

    def test_prefix_is_optional_and_server_derived(self):
        p = _payload()
        del p["prefix"]
        out = coinbase_push.import_payload(self.conn, p)
        self.assertTrue(out["ok"])
        self.assertEqual(out["pushed"], 2)


def _export_zip() -> bytes:
    """A minimal export ZIP the way /export writes one."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(
            s, fieldnames=["id", "account_id", "date", "amount", "name"])
        w.writeheader()
        w.writerow({"id": "zres-1", "account_id": "chk",
                    "date": "2026-07-01", "amount": "12.5",
                    "name": "RESTORED ROW"})
        z.writestr("transactions.csv", s.getvalue())
    return buf.getvalue()


CSV = b"Date,Description,Amount\n2026-07-01,GROCERY MART,-42.50\n"


class HubZipGuardTests(unittest.TestCase):
    """A script token cannot ZIP-restore through the import hub."""

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
            "email": f"zg-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        minted = cls.client.post("/api/tokens",
                                 json={"name": "zip-guard", "password": "correct-horse-battery"}).json()
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def _count_restored(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT count(*) n FROM transactions "
                "WHERE id='zres-1'").fetchone()["n"]
        finally:
            conn.close()

    def test_script_token_zip_restore_is_403(self):
        bare = TestClient(self.client.app)
        r = bare.post("/api/import", headers=self.hdr,
                      data={"account_id": "", "amount_sign": "bank"},
                      files={"file": ("export.zip", _export_zip(),
                                      "application/zip")})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._count_restored(), 0)

    def test_script_token_csv_push_still_works(self):
        # a community script pushes CSVs through this exact route with a
        # token — the restore guard must not close it
        bare = TestClient(self.client.app)
        r = bare.post("/api/import", headers=self.hdr,
                      data={"account_id": "chk", "amount_sign": "bank"},
                      files={"file": ("bank-export.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((r.json().get("result") or {}).get("error"))

    def test_zowner_session_zip_restore_still_works(self):
        # (name z-ordered after the 403 test so the restored row can't
        # mask a guard failure)
        r = self.client.post("/api/import",
                             data={"account_id": "", "amount_sign": "bank"},
                             files={"file": ("export.zip", _export_zip(),
                                             "application/zip")})
        self.assertEqual(r.status_code, 200, r.text)
        body = (r.json().get("result") or {})
        self.assertFalse(body.get("error"), body)
        # a session owner's restore now STARTS a job rather than finishing
        # inside the upload (a real archive outlives the edge timeout) —
        # the door is still open to them, which is what this protects
        self.assertTrue(body.get("restore_started"), body)
        for _ in range(100):
            if self.client.get("/api/restore/progress").json()["state"] \
                    in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(self._count_restored(), 1)


class ScriptTokenRateLimitTests(unittest.TestCase):
    """Script-token requests share a per-IP budget."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.appmod = appmod
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"rl-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        minted = cls.client.post("/api/tokens",
                                 json={"name": "rate", "password": "correct-horse-battery"}).json()
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def test_over_budget_is_429(self):
        from oikonome.web import security
        orig = self.appmod.SCRIPT_TOKEN_RATE
        self.appmod.SCRIPT_TOKEN_RATE = (3, 60.0)
        security._limiter._hits.clear()
        try:
            bare = TestClient(self.client.app)
            codes = [bare.post("/api/accounts/balance", headers=self.hdr,
                               json={"account_id": "nope"}).status_code
                     for _ in range(4)]
            self.assertEqual(codes[:3], [404, 404, 404])
            self.assertEqual(codes[3], 429)
        finally:
            self.appmod.SCRIPT_TOKEN_RATE = orig
            security._limiter._hits.clear()

    def test_session_requests_are_not_budgeted(self):
        # the budget is for tokens; a signed-in owner's requests don't
        # burn it (sessions have their own protections)
        from oikonome.web import security
        orig = self.appmod.SCRIPT_TOKEN_RATE
        self.appmod.SCRIPT_TOKEN_RATE = (2, 60.0)
        security._limiter._hits.clear()
        try:
            for _ in range(4):
                r = self.client.post("/api/accounts/balance",
                                     json={"account_id": "nope"})
                self.assertEqual(r.status_code, 404)
        finally:
            self.appmod.SCRIPT_TOKEN_RATE = orig
            security._limiter._hits.clear()


if __name__ == "__main__":
    unittest.main()
