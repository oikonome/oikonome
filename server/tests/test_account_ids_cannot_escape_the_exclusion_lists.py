"""An account id can never slip out of the per-connection exclusion lists.

The accounts every money figure ignores (linked duplicates, hidden
accounts) and the accounts that belong to a business entity travel to the
SQL as comma-joined session settings, split back on ','. An id holding a
comma would split into pieces that match nothing and be counted again, so
the database refuses such an id outright, from whatever source it comes.
"""

import csv
import io
import json
import os
import pathlib
import unittest
import uuid
import zipfile

import httpx
import psycopg

from oikonome.db import tenancy
from oikonome.engine.compat import jsonb
from oikonome.sync import coinbase, coinbase_push, restore, simplefin

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db, write_config

RENAME = (pathlib.Path(tenancy.__file__).parent / "migrations" /
          "149_account_ids_lose_their_comma.sql")
RENAME_TAKEN = (pathlib.Path(tenancy.__file__).parent / "migrations" /
                "151_account_ids_lose_their_comma_when_taken.sql")
ODD = "acct-a,b"            # an id a source handed over with a comma
FIXED = "acct-a-b"          # the name every door gives it instead


class AccountIdCommaTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_an_id_with_a_comma_is_refused(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO accounts (id, name, type) "
                "VALUES ('acct-a,acct-b', 'Odd id', 'depository')")

    def test_a_hidden_account_is_in_the_list_the_sql_reads(self):
        self.conn.execute(
            "INSERT INTO accounts (id, name, type, user_removed_at) "
            "VALUES ('acct-hidden', 'Hidden', 'depository', now())")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        fresh = tenancy.tenant_connect(tid)
        try:
            ids = fresh.execute(
                "SELECT string_to_array(current_setting('app.shadow_ids', "
                "true), ',') AS a").fetchone()["a"]
        finally:
            fresh.close()
        self.assertIn("acct-hidden", ids)



class _Rollback(Exception):
    pass


def _export_tenant(conn, tid, tables) -> bytes:
    """An export archive of one household, read over an admin connection
    (which sees every household, hence the filter)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl in tables:
            rows = conn.execute(f"SELECT * FROM {tbl} WHERE tenant_id=%s",
                                (tid,)).fetchall()
            s = io.StringIO()
            if rows:
                cols = [c for c in rows[0].keys()
                        if c not in ("tenant_id", "access_token")]
                w = csv.DictWriter(s, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({k: (json.dumps(r[k])
                                    if isinstance(r[k], (dict, list))
                                    else r[k]) for k in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


class StoredCommaIdTests(unittest.TestCase):
    """An install that already held a comma id when the rule arrived.

    The rule went on NOT VALID there, and PostgreSQL checks a NOT VALID
    constraint on every insert and update — so the account was not
    grandfathered but frozen: each sync upsert of it raised, and the whole
    connection stopped updating. The rename migration moves such an
    account to its comma-free id, carrying every row and settings key that
    names it, and then enforces the rule for every row."""

    def setUp(self):
        _ensure_db()
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(self.admin.close)
        self.tid = tenancy.create_tenant(
            self.admin, f"comma-{uuid.uuid4().hex[:8]}")

    def _plant(self, *, taken=False):
        """The pre-rule state, inside the caller's transaction: the rule
        NOT VALID over a live comma id with children and settings."""
        a = self.admin
        a.execute("SELECT set_config('app.tenant_id', %s, true)", (self.tid,))
        a.execute("ALTER TABLE accounts DROP CONSTRAINT "
                  "accounts_id_has_no_comma")
        a.execute("INSERT INTO items (id, aggregator) VALUES ('it1','test')")
        for aid in ("chk", ODD) + ((FIXED,) if taken else ()):
            a.execute("INSERT INTO accounts (id, item_id, name, type) "
                      "VALUES (%s, 'it1', %s, 'depository')", (aid, aid))
        a.execute("INSERT INTO transactions (id, account_id, date, amount, "
                  "name) VALUES ('tx-1', %s, current_date, 5, 'ROW')",
                  (ODD,))
        a.execute("INSERT INTO holdings (account_id, symbol, quantity) "
                  "VALUES (%s, 'FUND', 1)", (ODD,))
        cfg = {"excluded_accounts": [ODD, "chk"],
               "checking_account_id": ODD,
               "savings_goals": [{"name": "Trip", "account_id": ODD}],
               "link_dismissed": [f"chk|{ODD}"],
               "debt_plan": {"debts": {ODD: {"apr": 9.5}}}}
        a.execute("INSERT INTO tenant_settings (config) VALUES (%s) "
                  "ON CONFLICT (tenant_id) DO UPDATE SET config = "
                  "EXCLUDED.config", (jsonb(cfg),))
        a.execute("INSERT INTO budget_snapshots (year, month, config) "
                  "VALUES (2001, 1, %s)", (jsonb(cfg),))
        a.execute("ALTER TABLE accounts ADD CONSTRAINT "
                  "accounts_id_has_no_comma CHECK (strpos(id, ',') = 0) "
                  "NOT VALID")

    def _one(self, sql, *args):
        return self.admin.execute(sql, args).fetchone()

    def test_a_not_valid_rule_freezes_the_row_it_was_meant_to_spare(self):
        """The failure the rename exists for, pinned so the reasoning
        stays true: an upsert of the existing row is refused."""
        try:
            with self.admin.transaction():
                self._plant()
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with self.admin.transaction():
                        self.admin.execute(
                            "INSERT INTO accounts (id, name) VALUES (%s, 'x') "
                            "ON CONFLICT (tenant_id, id) DO UPDATE "
                            "SET name = EXCLUDED.name", (ODD,))
                raise _Rollback
        except _Rollback:
            pass

    def test_the_account_moves_with_everything_that_names_it(self):
        try:
            with self.admin.transaction():
                self._plant()
                self.admin.execute(RENAME.read_text())
                self.admin.execute(RENAME.read_text())      # idempotent
                t = self.tid
                self.assertIsNone(self._one(
                    "SELECT 1 FROM accounts WHERE tenant_id=%s AND id=%s",
                    t, ODD))
                self.assertEqual(self._one(
                    "SELECT name FROM accounts WHERE tenant_id=%s AND id=%s",
                    t, FIXED)["name"], ODD)
                self.assertEqual(self._one(
                    "SELECT account_id FROM transactions "
                    "WHERE tenant_id=%s AND id='tx-1'", t)["account_id"],
                    FIXED)
                self.assertEqual(self._one(
                    "SELECT account_id FROM holdings WHERE tenant_id=%s",
                    t)["account_id"], FIXED)
                want = {"excluded_accounts": [FIXED, "chk"],
                        "checking_account_id": FIXED,
                        "savings_goals": [{"name": "Trip",
                                           "account_id": FIXED}],
                        "link_dismissed": [f"{FIXED}|chk"],
                        "debt_plan": {"debts": {FIXED: {"apr": 9.5}}}}
                live = self._one("SELECT config FROM tenant_settings "
                                 "WHERE tenant_id=%s", t)["config"]
                self.assertEqual({k: live[k] for k in want}, want)
                self.assertEqual(self._one(
                    "SELECT config FROM budget_snapshots WHERE tenant_id=%s",
                    t)["config"], want)
                self.assertTrue(self._one(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conname='accounts_id_has_no_comma'")
                    ["convalidated"])
                # and the account syncs again
                self.admin.execute(
                    "INSERT INTO accounts (id, name) VALUES (%s, 'x') "
                    "ON CONFLICT (tenant_id, id) DO UPDATE "
                    "SET name = EXCLUDED.name", (FIXED,))
                raise _Rollback
        except _Rollback:
            pass

    def test_a_taken_name_keeps_the_account_syncing(self):
        """When the comma-free id already belongs to another account the
        migration does not merge the two, and it does not leave the frozen
        rule in place either."""
        try:
            with self.admin.transaction():
                self._plant(taken=True)
                self.admin.execute(RENAME.read_text())
                self.assertIsNone(self._one(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname='accounts_id_has_no_comma'"))
                self.admin.execute(
                    "INSERT INTO accounts (id, name) VALUES (%s, 'x') "
                    "ON CONFLICT (tenant_id, id) DO UPDATE "
                    "SET name = EXCLUDED.name", (ODD,))
                raise _Rollback
        except _Rollback:
            pass

    def test_a_taken_name_moves_the_account_to_a_name_of_its_own(self):
        """When the plain rename belongs to a different account, the
        account moves to the name derived from its original id — the
        neighbour is untouched, the comma row's children follow it, and the
        rule is enforced again. Left in place, the comma row never syncs
        again and its pulls land on the neighbour."""
        from oikonome.sync.adopt import account_id_when_taken
        moved = account_id_when_taken(ODD)
        try:
            with self.admin.transaction():
                self._plant(taken=True)
                self.admin.execute(RENAME.read_text())
                self.admin.execute(RENAME_TAKEN.read_text())
                self.admin.execute(RENAME_TAKEN.read_text())  # idempotent
                t = self.tid
                self.assertIsNone(self._one(
                    "SELECT 1 FROM accounts WHERE tenant_id=%s AND id=%s",
                    t, ODD))
                self.assertEqual(self._one(
                    "SELECT name FROM accounts WHERE tenant_id=%s AND id=%s",
                    t, moved)["name"], ODD)
                self.assertEqual(self._one(
                    "SELECT name FROM accounts WHERE tenant_id=%s AND id=%s",
                    t, FIXED)["name"], FIXED)
                self.assertEqual(self._one(
                    "SELECT account_id FROM transactions "
                    "WHERE tenant_id=%s AND id='tx-1'", t)["account_id"],
                    moved)
                live = self._one("SELECT config FROM tenant_settings "
                                 "WHERE tenant_id=%s", t)["config"]
                self.assertEqual(live["checking_account_id"], moved)
                self.assertTrue(self._one(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conname='accounts_id_has_no_comma'")
                    ["convalidated"])
                raise _Rollback
        except _Rollback:
            pass

    def test_the_taken_name_is_the_one_the_ingest_doors_look_up(self):
        from oikonome.sync.adopt import account_id_when_taken
        self.assertIn("new_id || '-' || left(md5(r.id), 8)",
                      RENAME_TAKEN.read_text())
        want = self.admin.execute(
            "SELECT replace(%s::text, ',', '-') || '-' "
            "|| left(md5(%s::text), 8) AS v", (ODD, ODD)).fetchone()["v"]
        self.assertEqual(account_id_when_taken(ODD), want)

    def test_the_rename_is_the_one_the_ingest_doors_apply(self):
        from oikonome.sync.adopt import account_id_without_comma
        self.assertIn("replace(r.id, ',', '-')", RENAME.read_text())
        self.assertEqual(account_id_without_comma(ODD), FIXED)


class IncomingCommaIdTests(unittest.TestCase):
    """Every door an account id comes in by either renames a comma away or
    refuses it with a clear answer — never a database error mid-sync."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def test_a_simplefin_account_with_a_comma_syncs(self):
        payload = {"errors": [], "accounts": [
            {"id": ODD, "name": "Everyday Checking", "currency": "USD",
             "balance": "10.00", "org": {"name": "Demo Bank"},
             "transactions": [{"id": "t1", "posted": 1752537600,
                               "amount": "-1.00",
                               "description": "DEMO STORE"}]}]}
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, text=json.dumps(payload)))
        for _ in range(2):                    # the resync lands on the row
            simplefin.sync(self.conn, "sfin-demo",
                           "https://u:p@bridge.test/simplefin",
                           transport=transport)
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE id LIKE 'acct-a%'").fetchall()]
        self.assertEqual(ids, [FIXED])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id=%s",
            (FIXED,)).fetchone()["n"], 1)

    def test_a_coinbase_label_with_a_comma_is_refused_at_link(self):
        with self.assertRaises(ValueError):
            coinbase.link(self.conn, "a,b", "key-name", "not-a-key")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM items WHERE id LIKE 'coinbase-%'").fetchone())

    def test_a_coinbase_push_with_a_comma_id_is_a_bad_payload(self):
        payload = {
            "items": [{"id": "coinbase-a,b"}],
            "accounts": [{"id": "coinbase-a,b", "item_id": "coinbase-a,b",
                          "name": "Coinbase", "balance_current": 1.0}],
            "transactions": []}
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, payload)

    def test_an_archive_with_a_comma_id_restores_under_the_new_name(self):
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            data = None
            try:
                with admin.transaction():
                    admin.execute("SELECT set_config('app.tenant_id', %s, "
                                  "true)", (tid,))
                    admin.execute("ALTER TABLE accounts DROP CONSTRAINT "
                                  "accounts_id_has_no_comma")
                    admin.execute(
                        "INSERT INTO accounts (id, item_id, name, type) "
                        "VALUES (%s, 'it1', 'Odd', 'depository')", (ODD,))
                    admin.execute(
                        "INSERT INTO transactions (id, account_id, date, "
                        "amount, name) VALUES ('tx-odd', %s, current_date, "
                        "3, 'ROW')", (ODD,))
                    admin.execute(
                        "UPDATE tenant_settings SET config = config || %s",
                        (jsonb({"excluded_accounts": [ODD]}),))
                    data = _export_tenant(admin, tid, [
                        "items", "accounts", "transactions",
                        "tenant_settings"])
                    raise _Rollback
            except _Rollback:
                pass
        finally:
            admin.close()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            dest = tenancy.create_tenant(admin, "restore-comma-ids")
        finally:
            admin.close()
        b = tenancy.tenant_connect(dest)
        self.addCleanup(b.close)
        restore.restore_zip(b, data)
        self.assertEqual(b.execute(
            "SELECT account_id FROM transactions WHERE id='tx-odd'"
        ).fetchone()["account_id"], FIXED)
        self.assertIsNotNone(b.execute(
            "SELECT 1 FROM accounts WHERE id=%s", (FIXED,)).fetchone())
        cfg = b.execute("SELECT config FROM tenant_settings").fetchone()
        self.assertEqual(cfg["config"]["excluded_accounts"], [FIXED])


class RenamedAccountKeepsSyncingTests(unittest.TestCase):
    """After the migration renamed an account, every source that still
    names it with the comma reaches the renamed row: the pull neither fails
    the no-comma rule nor mints a second account, and when the plain name
    belongs to a different account the pull never lands on that one."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _account_ids(self, like):
        return sorted(r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE id LIKE %s", (like,)).fetchall())

    def test_a_coinbase_key_linked_under_a_comma_label_keeps_syncing(self):
        from oikonome.sync.base import upsert_item
        from .test_coinbase import KEY_NAME, _PEM, _transport
        iid = "coinbase-Sample, main"          # linked before the rule
        upsert_item(self.conn, iid, "coinbase", "Coinbase",
                    json.dumps({"name": KEY_NAME, "privateKey": _PEM}))
        # the migration's rename of the account the old link created
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) "
            "VALUES ('coinbase-Sample- main', %s, 'Coinbase (Sample, main)', "
            "'investment', 'crypto')", (iid,))
        out = coinbase.sync(self.conn, iid, transport=_transport())
        self.assertGreater(out["transactions"], 0)
        self.assertEqual(self._account_ids("coinbase-%"),
                         ["coinbase-Sample- main"])
        row = self.conn.execute(
            "SELECT balance_current FROM accounts "
            "WHERE id='coinbase-Sample- main'").fetchone()
        self.assertIsNotNone(row["balance_current"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions "
            "WHERE account_id='coinbase-Sample- main'").fetchone()["n"],
            out["transactions"])

    def test_every_aggregator_upsert_stores_a_comma_id_renamed(self):
        from oikonome.sync.base import (Account, Transaction,
                                        upsert_accounts, upsert_item,
                                        upsert_transactions)
        import datetime as dt
        upsert_item(self.conn, "it-agg", "test", "Demo Bank", None)
        upsert_accounts(self.conn, "it-agg", [Account(
            id=ODD, name="Everyday", type="depository", subtype="checking",
            mask=None, balance_current=1.0, balance_available=None,
            currency="USD")])
        upsert_transactions(self.conn, [Transaction(
            id="agg-t1", account_id=ODD, date=dt.date(2001, 1, 2),
            amount=1.0, name="DEMO STORE", merchant_name=None,
            pending=False)])
        self.assertEqual(self._account_ids("acct-a%"), [FIXED])
        self.assertEqual(self.conn.execute(
            "SELECT account_id FROM transactions WHERE id='agg-t1'"
        ).fetchone()["account_id"], FIXED)

    def test_a_pull_reaches_the_account_moved_off_a_taken_name(self):
        from oikonome.sync.adopt import account_id_when_taken
        moved = account_id_when_taken(ODD)
        self.conn.execute("INSERT INTO items (id, aggregator) "
                          "VALUES ('it-other', 'test')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type) VALUES "
            "(%s, 'it-other', 'Neighbour', 'depository')", (FIXED,))
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type) VALUES "
            "(%s, 'it-other', 'Moved', 'depository')", (moved,))
        payload = {"errors": [], "accounts": [
            {"id": ODD, "name": "Everyday Checking", "currency": "USD",
             "balance": "10.00", "org": {"name": "Demo Bank"},
             "transactions": [{"id": "t1", "posted": 1752537600,
                               "amount": "-1.00",
                               "description": "DEMO STORE"}]}]}
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, text=json.dumps(payload)))
        simplefin.sync(self.conn, "sfin-demo",
                       "https://u:p@bridge.test/simplefin",
                       transport=transport)
        self.assertEqual(self._account_ids("acct-a%"), sorted([FIXED, moved]))
        self.assertEqual(self.conn.execute(
            "SELECT account_id FROM transactions WHERE id=%s",
            (f"sfin:{ODD}:t1",)).fetchone()["account_id"], moved)
        self.assertIsNone(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id=%s",
            (FIXED,)).fetchone()["balance_current"])

    def test_an_archive_holding_both_spellings_restores_two_accounts(self):
        from oikonome.sync.adopt import account_id_when_taken
        moved = account_id_when_taken(ODD)
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            data = None
            try:
                with admin.transaction():
                    admin.execute("SELECT set_config('app.tenant_id', %s, "
                                  "true)", (tid,))
                    admin.execute("ALTER TABLE accounts DROP CONSTRAINT "
                                  "accounts_id_has_no_comma")
                    admin.execute("INSERT INTO items (id, aggregator) "
                                  "VALUES ('it1', 'test') "
                                  "ON CONFLICT DO NOTHING")
                    for aid in (ODD, FIXED):
                        admin.execute(
                            "INSERT INTO accounts (id, item_id, name, type) "
                            "VALUES (%s, 'it1', %s, 'depository')",
                            (aid, aid))
                        admin.execute(
                            "INSERT INTO transactions (id, account_id, date, "
                            "amount, name) VALUES (%s, %s, current_date, 3, "
                            "'ROW')", ("tx:" + aid, aid))
                    data = _export_tenant(admin, tid, [
                        "items", "accounts", "transactions",
                        "tenant_settings"])
                    raise _Rollback
            except _Rollback:
                pass
        finally:
            admin.close()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            dest = tenancy.create_tenant(admin, "restore-both-spellings")
        finally:
            admin.close()
        b = tenancy.tenant_connect(dest)
        self.addCleanup(b.close)
        restore.restore_zip(b, data)
        self.assertEqual({r["id"]: r["name"] for r in b.execute(
            "SELECT id, name FROM accounts WHERE id LIKE 'acct-a%'"
        ).fetchall()},
            {FIXED: FIXED, moved: ODD})
        self.assertEqual(b.execute(
            "SELECT account_id FROM transactions WHERE id=%s",
            ("tx:" + ODD,)).fetchone()["account_id"], moved)
        self.assertEqual(b.execute(
            "SELECT account_id FROM transactions WHERE id=%s",
            ("tx:" + FIXED,)).fetchone()["account_id"], FIXED)


class CoinbaseLinkDoorTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"cb-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text

    def test_a_label_with_a_comma_is_a_400(self):
        r = self.client.post("/api/accounts/coinbase/link", json={
            "label": "a,b", "key_name": "k", "private_key": "p"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("comma", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
