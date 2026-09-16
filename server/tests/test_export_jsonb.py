"""Export JSONB serialization: dict/list cells must travel as real JSON.
Python repr (single quotes) is rejected by json.loads on the restore side,
so raw/by_class would silently round-trip as NULL through product→product
export/restore."""

import io
import json
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine.compat import as_dict, jsonb
from oikonome.sync import restore

from .util import _ensure_db, make_db, seed_accounts, write_config
from .export_ticket import export_get

RAW = {"plaid": {"authorized_date": "2026-07-10", "counterparties":
                 [{"name": "Safeway", "type": "merchant"}]},
       "note": 'quotes "inside" and \'single\''}


class ExportJsonbTests(unittest.TestCase):
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
            "email": f"ex-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed, raw)
                   VALUES ('exj-1','chk','2026-07-10',42.5,'SAFEWAY',0,0,%s)""",
                (jsonb(RAW),))
            conn.execute(
                """INSERT INTO holdings (account_id, symbol, name, quantity,
                       price, value, as_of, raw)
                   VALUES ('chk','VTI','Vanguard',10,250,2500,now(),%s)""",
                (jsonb({"security_id": "s1"}),))
        finally:
            conn.close()

    def _export_zip(self) -> zipfile.ZipFile:
        r = export_get(self.client, "/export")
        self.assertEqual(r.status_code, 200)
        return zipfile.ZipFile(io.BytesIO(r.content))

    def test_zip_membership_is_schema_driven_and_rls_only(self):
        """The ZIP's table set derives from the schema (every RLS-enabled
        tenant_id table minus the reasoned EXPORT_SKIP set), not a
        hand-maintained list, which forgets new tables. And RLS-only is
        load-bearing: tables
        carrying tenant_id with NO row-level security (users, invites,
        api_tokens) would SELECT every tenant's rows over the app role,
        so they must never ride this ZIP; nor may the skipped
        operational/secret tables.

        The exception is `export.CONTROL_TABLES`: control-plane members
        that discovery cannot see, each read by a hand-written query that
        names the tenant itself over the admin connection, and each one
        classified by `test_control_plane_export_coverage`."""
        from oikonome.sync.export import CONTROL_TABLES
        from oikonome.tenant_export import rls_tenant_tables
        from oikonome.web.pages import EXPORT_SKIP
        conn = tenancy.tenant_connect(self.tid)
        try:
            discovered = set(rls_tenant_tables(conn))
        finally:
            conn.close()
        names = set(self._export_zip().namelist())
        expected = ({f"{t}.csv" for t in discovered - EXPORT_SKIP}
                    | {f"{t}.csv" for t in CONTROL_TABLES})
        self.assertEqual(names - {"README.txt"}, expected)
        for forbidden in ("users.csv", "invites.csv", "api_tokens.csv",
                          "sessions.csv", "tenant_keys.csv",
                          "sync_log.csv", "job_progress.csv"):
            self.assertNotIn(forbidden, names)

    def test_readme_maps_every_file_in_the_zip(self):
        """The export is 30+ bare CSVs — the README is the data map, and
        it must describe every file actually present (a new table added
        to TABLES without a DATA_MAP entry ships a blank line)."""
        z = self._export_zip()
        names = set(z.namelist())
        self.assertIn("README.txt", names)
        readme = z.read("README.txt").decode()
        for n in sorted(names - {"README.txt"}):
            self.assertIn(f"  {n} — ", readme, f"{n} missing from the map")
            line = next(l for l in readme.splitlines()
                        if l.startswith(f"  {n} — "))
            self.assertGreater(len(line.split("—", 1)[1].strip()), 3,
                               f"{n} has no description")
        self.assertIn("POSITIVE = money out", readme)

    def test_jsonb_cells_are_real_json(self):
        z = self._export_zip()
        import csv as _csv
        rows = list(_csv.DictReader(
            io.StringIO(z.read("transactions.csv").decode())))
        cell = next(r["raw"] for r in rows if r["id"] == "exj-1")
        self.assertEqual(json.loads(cell), RAW)  # str(dict) would raise here
        hrows = list(_csv.DictReader(
            io.StringIO(z.read("holdings.csv").decode())))
        self.assertEqual(json.loads(hrows[0]["raw"]), {"security_id": "s1"})

    def test_formula_injection_neutralized_in_export_csv(self):
        """A counterparty-controlled payee beginning with = + - @ must be
        quote-prefixed so Excel/Calc treat it as text, not a formula — the
        same neutralization api._csv_safe applies elsewhere."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, merchant_name, pending, removed)
                   VALUES ('exj-inj','chk','2026-07-11',9.0,'x',
                       %s,0,0)""",
                ('=cmd|\'/c calc\'!A1',))
        finally:
            conn.close()
        z = self._export_zip()
        import csv as _csv
        rows = list(_csv.DictReader(
            io.StringIO(z.read("transactions.csv").decode())))
        cell = next(r["merchant_name"] for r in rows if r["id"] == "exj-inj")
        self.assertTrue(cell.startswith("'="),
                        f"formula not neutralized: {cell!r}")

    def test_business_tables_survive_round_trip(self):
        """A tenant's business data (entity, members, equity,
        classification, compliance, mileage, 1099, notes) and the entity
        assignments on accounts/transactions must survive export→restore;
        a hardcoded table list that falls behind the schema drops them
        silently. EIN ciphertext must NOT ride the export (ein_last4
        carries)."""
        from oikonome.engine import entities
        conn = tenancy.tenant_connect(self.tid)
        try:
            ent = entities.create_entity(
                conn, name="RT LLC", structure="single_member_llc",
                # EIN is synthetic, not a real one (demo.py convention)
                ein="00-0001234")
            eid = ent["id"]
            conn.execute("INSERT INTO accounts (id,name,type) "
                         "VALUES ('rtbiz','Biz','depository')")
            conn.execute("INSERT INTO transactions (id,account_id,date,amount,"
                         "name,pending,removed) VALUES "
                         "('rt-biz-1','rtbiz','2026-07-12',30.0,'CONTRACTOR',0,0)")
            conn.execute("INSERT INTO entity_membership (entity_id,member_name,"
                         "ownership_pct,is_manager) VALUES (%s,'Owner',100,true)",
                         (eid,))
            conn.execute("INSERT INTO equity_movement (entity_id,kind,amount,"
                         "date) VALUES (%s,'contribution',500,'2026-07-12')",
                         (eid,))
            conn.execute("INSERT INTO business_txn_class (txn_id,bucket) "
                         "VALUES ('rt-biz-1','operating')")
            conn.execute("INSERT INTO compliance_obligation (entity_id,title,"
                         "due_date) VALUES (%s,'Annual report','2027-07-31')",
                         (eid,))
            conn.execute("INSERT INTO mileage_log (entity_id,date,miles,purpose)"
                         " VALUES (%s,'2026-07-15',100,'client visit')", (eid,))
            conn.execute("INSERT INTO vendor_1099 (entity_id,merchant) "
                         "VALUES (%s,'Contractor Co')", (eid,))
            conn.execute("INSERT INTO transaction_notes (txn_id,note) "
                         "VALUES ('rt-biz-1','deductible')")
            entities.assign_account(conn, "rtbiz", eid)
            entities.assign_transaction(conn, "rt-biz-1", eid)
        finally:
            conn.close()

        r = export_get(self.client, "/export")
        dest = make_db()
        try:
            counts = restore.restore_zip(dest, r.content)
            for tbl in ("business_entity", "entity_membership",
                        "equity_movement", "business_txn_class",
                        "compliance_obligation", "mileage_log", "vendor_1099",
                        "transaction_notes"):
                self.assertGreaterEqual(counts.get(tbl, 0), 1,
                                        f"{tbl} did not restore")
            # entity assignments survived
            acct = dest.execute("SELECT entity_id FROM accounts "
                                "WHERE id='rtbiz'").fetchone()
            self.assertEqual(str(acct["entity_id"]), eid)
            txn = dest.execute("SELECT entity_id FROM transactions "
                               "WHERE id='rt-biz-1'").fetchone()
            self.assertEqual(str(txn["entity_id"]), eid)
            # EIN: last4 carried, ciphertext did NOT
            e = dest.execute("SELECT ein_last4, ein_enc FROM business_entity "
                             "WHERE id=%s", (eid,)).fetchone()
            self.assertEqual(e["ein_last4"], "1234")
            self.assertIsNone(e["ein_enc"])
        finally:
            dest.close()

    def test_export_restores_into_fresh_tenant_with_raw_intact(self):
        """The product→product round trip: export from one tenant, restore
        into another, raw survives byte-for-byte."""
        r = export_get(self.client, "/export")
        dest = make_db()
        try:
            counts = restore.restore_zip(dest, r.content)
            self.assertGreaterEqual(counts["transactions"], 1)
            row = dest.execute("SELECT raw FROM transactions "
                               "WHERE id='exj-1'").fetchone()
            self.assertEqual(as_dict(row["raw"]), RAW)
            h = dest.execute("SELECT raw, value FROM holdings "
                             "WHERE symbol='VTI'").fetchone()
            self.assertEqual(as_dict(h["raw"]), {"security_id": "s1"})
            self.assertEqual(h["value"], 2500.0)
        finally:
            dest.close()


class ExportShadowAndReceiptTests(unittest.TestCase):
    """account_links (the shadow set) and receipts/receipt_items (images
    + the Schedule-C line-item tags) must survive the export→restore round
    trip — on hosted this ZIP is the only door out, so anything missing
    from it is simply lost."""

    IMG = b"\xff\xd8\xff\xe0FAKEJPEG\x00\x01\x02" * 40

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
            "email": f"exr-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        cls.rid = str(uuid.uuid4())
        cls.grp = str(uuid.uuid4())
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # a second source of the same real account, linked + shadowed
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('chk2','it1','Checking (SimpleFIN)','depository',"
                "'checking',5000)")
            conn.execute(
                "INSERT INTO account_links (group_id, account_id, home_rank)"
                " VALUES (%s,'chk',0), (%s,'chk2',1)", (cls.grp, cls.grp))
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed)
                   VALUES ('exr-1','chk','2026-07-10',80.0,'NORTHWIND CLUB',0,0)""")
            conn.execute(
                """INSERT INTO receipts (id, txn_id, image, mime, kind,
                       status) VALUES (%s,'exr-1',%s,'image/jpeg','receipt',
                       'parsed')""", (cls.rid, cls.IMG))
            conn.execute(
                """INSERT INTO receipt_items (receipt_id, line, description,
                       qty, amount, tag)
                   VALUES (%s, 1, 'printer paper', 2, 24.99, 'business')""",
                (cls.rid,))
        finally:
            conn.close()

    def test_round_trip_keeps_links_and_receipts(self):
        r = export_get(self.client, "/export")
        self.assertEqual(r.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        for member in ("account_links.csv", "receipts.csv",
                       "receipt_items.csv"):
            self.assertIn(member, names)
        dest = make_db()
        try:
            counts = restore.restore_zip(dest, r.content)
            self.assertGreaterEqual(counts.get("account_links", 0), 2)
            self.assertGreaterEqual(counts.get("receipts", 0), 1)
            self.assertGreaterEqual(counts.get("receipt_items", 0), 1)
            links = dest.execute(
                "SELECT account_id, home_rank FROM account_links "
                "ORDER BY home_rank").fetchall()
            self.assertEqual([(l["account_id"], l["home_rank"])
                              for l in links],
                             [("chk", 0), ("chk2", 1)],
                             "the shadow set did not survive — every money "
                             "aggregate now double-counts the linked pair")
            rec = dest.execute(
                "SELECT image, mime FROM receipts WHERE id=%s",
                (self.rid,)).fetchone()
            self.assertEqual(bytes(rec["image"]), self.IMG,
                             "receipt image did not survive byte-for-byte")
            item = dest.execute(
                "SELECT description, tag, amount FROM receipt_items "
                "WHERE receipt_id=%s", (self.rid,)).fetchone()
            self.assertEqual((item["description"], item["tag"],
                              item["amount"]),
                             ("printer paper", "business", 24.99))
        finally:
            dest.close()

    def test_orphan_receipt_rows_are_skipped_not_fatal(self):
        """A receipt whose transaction is absent from the archive must not
        abort the all-or-nothing restore."""
        r = export_get(self.client, "/export")
        z_in = zipfile.ZipFile(io.BytesIO(r.content))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z_out:
            for n in z_in.namelist():
                if n == "transactions.csv":
                    continue                     # drop the parent rows
                z_out.writestr(n, z_in.read(n))
        dest = make_db()
        try:
            counts = restore.restore_zip(dest, buf.getvalue())
            self.assertEqual(counts.get("receipts", 0), 0)
            self.assertEqual(counts.get("receipt_items", 0), 0)
        finally:
            dest.close()


if __name__ == "__main__":
    unittest.main()
