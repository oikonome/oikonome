"""Round-trip migration: export from tenant A → restore into tenant B.
The engine must produce the SAME verdict on the restored data — the real
definition of 'migration works'."""

import io
import unittest
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import restore

from .util import TODAY, _ensure_db, add_bill, add_txn, make_db, write_config


def _export_zip(conn, tables) -> bytes:
    """Build a restore ZIP the way /export does — SELECT * per table, CSV
    per member — without needing the exporting tenant's HTTP session."""
    import csv as _c
    import json as _json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl in tables:
            rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()
            s = io.StringIO()
            if rows:
                cols = [c for c in rows[0].keys()
                        if c not in ("tenant_id", "access_token")]
                w = _c.DictWriter(s, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({k: (_json.dumps(r[k])
                                    if isinstance(r[k], (dict, list))
                                    else r[k]) for k in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


def _fresh_tenant():
    """A brand-new empty tenant connection — no fixture accounts, so a
    restore must recreate everything itself."""
    import uuid
    _ensure_db()
    admin = tenancy.admin_connect()
    tid = tenancy.create_tenant(admin, f"restore-{uuid.uuid4().hex[:8]}")
    admin.close()
    return tenancy.tenant_connect(tid)


class RestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os, uuid
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"restore-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_export_restore_round_trip_same_verdict(self):
        # tenant A: data + bills + config
        a = make_db()
        write_config(a, food_monthly=500, other_monthly=500)
        add_bill(a, "Rent", 1500.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(a, TODAY.replace(day=1), 1500.0, "RENT",
                primary="RENT_AND_UTILITIES")
        add_txn(a, TODAY, 80.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(a, TODAY, 40.0, "TARGET", override="ENTERTAINMENT")
        st_a = budget.month_status(a, TODAY)

        # export A's data the same way /export does (reuse the route's logic
        # via HTTP would need A's session; build the zip directly instead)
        import csv as _c
        TABLES = ["items", "accounts", "transactions", "bills",
                  "manual_categories", "tenant_settings"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for tbl in TABLES:
                rows = a.execute(f"SELECT * FROM {tbl}").fetchall()
                s = io.StringIO()
                if rows:
                    cols = [c for c in rows[0].keys()
                            if c not in ("tenant_id", "access_token")]
                    w = _c.DictWriter(s, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    import json as _json
                    for r in rows:
                        w.writerow({k: (_json.dumps(r[k])
                                        if isinstance(r[k], (dict, list))
                                        else r[k]) for k in cols})
                z.writestr(f"{tbl}.csv", s.getvalue())
        a.close()

        # tenant B: restore (fresh tenant, NO fixture accounts — restore
        # must recreate items/accounts itself)
        admin = tenancy.admin_connect()
        tid_b = tenancy.create_tenant(admin, "restore-b")
        admin.close()
        b = tenancy.tenant_connect(tid_b)
        try:
            counts = restore.restore_zip(b, buf.getvalue())
            self.assertGreaterEqual(counts["transactions"], 3)
            self.assertGreaterEqual(counts["bills"], 1)
            st_b = budget.month_status(b, TODAY)
            # the verdict math survives the migration exactly
            self.assertEqual(st_a["verdict"], st_b["verdict"])
            self.assertEqual(st_a["variable_actual"], st_b["variable_actual"])
            self.assertEqual(st_a["buckets"]["fixed"]["actual"],
                             st_b["buckets"]["fixed"]["actual"])
            # overrides travel
            row = b.execute("SELECT category_override FROM transactions "
                            "WHERE name='TARGET'").fetchone()
            self.assertEqual(row["category_override"], "ENTERTAINMENT")
            # idempotent re-restore
            restore.restore_zip(b, buf.getvalue())
            n = b.execute("SELECT COUNT(*) AS n FROM transactions"
                          ).fetchone()["n"]
            self.assertEqual(n, 3)
        finally:
            b.close()




class RoundTripKeepsEveryColumnTests(unittest.TestCase):
    """Restore's fixed column lists must cover every column the export ships.

    The export is SELECT * against the LIVE table, so a column added by an
    ALTER TABLE migration rides the ZIP the moment the migration runs. A
    guard that derives the expected set from schema.sql's CREATE TABLE block
    never sees ALTER TABLE columns, and would stay green while restore
    silently dropped them. The live
    migrated database is the only honest source: what SELECT * reads there
    is exactly what restore must write back.
    """

    # Columns that legitimately do not round-trip, each with its reason.
    # Anything NOT here that exists on the live table must be restored.
    SKIP = {
        "transactions": {
            # ambient via RLS — never travels in the ZIP, never restored
            "tenant_id",
        },
        "accounts": {
            "tenant_id",       # ambient via RLS, same as above
            # when THIS instance's sync loop last wrote the row. The source
            # instance's stamp would claim a sync that never happened here;
            # restored accounts are honestly "never synced here" (NULL).
            "updated_at",
        },
    }

    @staticmethod
    def _live_columns(conn, table: str) -> set:
        # generated columns (is_generated) are the database's own — never
        # exported, never restorable — so they are not "live" for this check
        return {r["column_name"] for r in conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='public' AND table_name=%s
                 AND is_generated = 'NEVER'""",
            (table,)).fetchall()}

    @staticmethod
    def _listed_columns(stmt: str) -> set:
        """Column names inside the INSERT's parenthesised column list."""
        import re as _re
        return set(_re.findall(r"[a-z_]+",
                               stmt.split("(", 1)[1].split(")", 1)[0]))

    def test_restore_covers_every_live_column_export_ships(self):
        import inspect

        from oikonome.sync import restore as _restore

        # the body, not the public wrapper: restore_zip() is now a thin
        # shell that installs the progress callback and delegates, so
        # reading its source would find no SQL at all — and this test
        # would pass by finding nothing, which is the one way it must
        # never pass
        src = inspect.getsource(_restore._restore_zip)
        self.assertIn("INSERT INTO accounts", src)
        listed = {
            "transactions": self._listed_columns(
                src[src.index("_TXN_SQL = "):src.index("CHUNK = 1000")]),
            "accounts": self._listed_columns(
                src[src.index("INSERT INTO accounts"):]),
        }

        conn = make_db()
        try:
            for table, skip in self.SKIP.items():
                live = self._live_columns(conn, table)
                self.assertTrue(live, f"no live columns found for {table}")
                missing = live - skip - listed[table]
                self.assertEqual(
                    missing, set(),
                    f"restore silently drops {table} column(s) "
                    f"{sorted(missing)} that the SELECT * export ships — "
                    "either restore them or add them to SKIP with a reason")
        finally:
            conn.close()


class RestoreKeepsOwnershipAndOutletTests(unittest.TestCase):
    """The household attribution columns and the outlet brand must survive
    a round trip: accounts.owner is the yours/mine/ours default, a
    transaction's owner_override is the per-row exception, and
    merchant_outlet drives payee display/search and the rename screen.
    All three ride the SELECT * export."""

    def test_owner_and_outlet_survive_a_round_trip(self):
        a = make_db()
        a.execute("UPDATE accounts SET owner='alex' WHERE id='chk'")
        tid = add_txn(a, TODAY, 12.5, "SHELL 1234", account="chk")
        a.execute("UPDATE transactions SET owner_override='sam', "
                  "merchant_outlet='Shell' WHERE id=%s", (tid,))
        data = _export_zip(a, ["items", "accounts", "transactions"])
        a.close()

        b = _fresh_tenant()
        try:
            restore.restore_zip(b, data)
            acct = b.execute(
                "SELECT owner FROM accounts WHERE id='chk'").fetchone()
            self.assertEqual(acct["owner"], "alex")
            txn = b.execute(
                "SELECT owner_override, merchant_outlet FROM transactions "
                "WHERE id=%s", (tid,)).fetchone()
            self.assertEqual(txn["owner_override"], "sam")
            self.assertEqual(txn["merchant_outlet"], "Shell")
        finally:
            b.close()


class RestoreDefaultsCanonicalMethodTests(unittest.TestCase):
    """A merchant_canonical row restored with no method would be frozen
    forever: the layer1 recompute only rewrites method='layer1' rows and
    the preservation logic only recognises 'llm'/'manual', so NULL is
    invisible to both. A methodless CSV row (hand-edited, or from before
    the column) must land as 'manual' — never recomputed over."""

    def test_a_methodless_mapping_restores_as_manual(self):
        conn = make_db()
        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("merchant_canonical.csv",
                           "raw_merchant,canonical,method,as_of\n"
                           "SQ *COFFEE HUT,Coffee Hut,,\n")
            restore.restore_zip(conn, buf.getvalue())
            row = conn.execute(
                "SELECT method FROM merchant_canonical "
                "WHERE raw_merchant='SQ *COFFEE HUT'").fetchone()
            self.assertEqual(row["method"], "manual")
        finally:
            conn.close()


class RestoreNeverRewindsRenameSequenceTests(unittest.TestCase):
    """merchant_renames ids come from ONE sequence shared by every tenant,
    while a restore only sees its own tenant's rows through RLS. Advancing
    the sequence to this tenant's MAX(id) can therefore move it BACKWARD
    below another tenant's ids — after which that tenant's next rename
    collides on the PK and ON CONFLICT DO NOTHING silently swallows the
    journal row. A restore must never rewind the shared sequence."""

    def test_restore_of_low_ids_does_not_rewind_the_shared_sequence(self):
        b = make_db()
        a = _fresh_tenant()
        try:
            # tenant B accumulates renames at the sequence's current ids
            for i in range(5):
                b.execute(
                    "INSERT INTO merchant_renames (raw_merchant, to_canonical)"
                    " VALUES (%s,%s)", (f"RAW {i}", f"Nice {i}"))
            before = b.execute("SELECT last_value FROM merchant_renames_id_seq"
                               ).fetchone()["last_value"]

            # tenant A restores a backup whose journal carries low ids
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("merchant_renames.csv",
                           "id,raw_merchant,from_canonical,to_canonical,at\n"
                           "1,OLD RAW,,Old Nice,\n"
                           "2,OLD RAW 2,,Old Nice 2,\n")
            restore.restore_zip(a, buf.getvalue())

            after = a.execute("SELECT last_value FROM merchant_renames_id_seq"
                              ).fetchone()["last_value"]
            self.assertGreaterEqual(
                after, before,
                "restore rewound the shared merchant_renames id sequence")

            # and tenant B's next rename still lands (DO NOTHING is the app
            # path, so a collision here would vanish without an error)
            cur = b.execute(
                "INSERT INTO merchant_renames (raw_merchant, to_canonical) "
                "VALUES ('RAW AFTER','Nice After') ON CONFLICT DO NOTHING")
            self.assertEqual(cur.rowcount, 1)
        finally:
            a.close()
            b.close()


class RestoreKeepsDestinationSecretsTests(unittest.TestCase):
    """A restore ZIP is scrubbed of secrets at export, so applying one
    must keep EVERY secret already configured at the destination —
    losing the MX/LLM/SMTP keys silently broke sync and mail."""

    def test_restore_keeps_mx_llm_smtp_secrets(self):
        import io
        import zipfile
        from oikonome.sync import restore
        conn = make_db()
        try:
            # destination already has a full spread of secrets
            budget.save_config(conn, {
                "plaid_secret": "ps", "mx_api_key": "mx", "llm_api_key": "llm",
                "smtp_password": "smtp", "food_monthly": 1, "other_monthly": 1})
            # a restore ZIP whose tenant_settings.csv was scrubbed of secrets
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("tenant_settings.csv",
                           'config\n"{""food_monthly"": 999}"\n')
            restore.restore_zip(conn, buf.getvalue())
            cfg = budget.load_config(conn)
            self.assertEqual(cfg["mx_api_key"], "mx")     # kept
            self.assertEqual(cfg["llm_api_key"], "llm")   # kept
            self.assertEqual(cfg["smtp_password"], "smtp")  # kept
            self.assertEqual(cfg["plaid_secret"], "ps")   # kept
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
