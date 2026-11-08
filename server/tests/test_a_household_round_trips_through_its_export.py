"""A household exported and restored comes back whole, and a second
restore of the same archive writes nothing.

The fixture fills every table the restore loads (invented names and
amounts throughout). Three promises are checked against it:

* every archived row ends in exactly one place: written, or under the one
  outcome that says why not (restore.REFUSALS), so the counters cannot
  drift from what the database did;
* the destination holds what the source held, table by table;
* restoring the same archive again writes no row and reports every row
  as already present, with no other outcome: a re-restore that "orphans"
  or "repairs" rows it already wrote is a counter lying.

And an archive written before the manifest existed (every archive written
until now) restores the same way.
"""

import csv
import io
import json
import unittest
import uuid
import zipfile

from oikonome.db import tenancy
from oikonome.engine import merchant_identity, splits
from oikonome.engine.compat import jsonb
from oikonome.sync import archive_upgrades, export, restore

from .util import TODAY, _ensure_db, add_txn, make_db, write_config

# what a receipt cell holds; the restore checks the encoding and the size,
# and the type is the mime the row names
IMAGE = b"\x89PNG\r\n\x1a\n" + b"invented receipt bytes"

# members a row count cannot be compared on: the settings document (one
# row, merged over the destination's), the control-plane recipients
# (written over the admin connection, tested on their own), and the name
# bills had before they were bills
NOT_ROWS = {"tenant_settings", "recipient_invites", "recurring"}


def fill_every_table(conn) -> None:
    """One or two rows in every table the restore loads."""
    write_config(conn)
    ent = str(uuid.uuid4())
    m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
    t1 = add_txn(conn, TODAY.replace(day=3), 42.5, "INVENTED GROCER",
                 primary="FOOD_AND_DRINK", account="card", txn_id="rt-t1")
    t2 = add_txn(conn, TODAY.replace(day=4), -42.5, "INVENTED REFUND",
                 account="chk", txn_id="rt-t2")
    t3 = add_txn(conn, TODAY.replace(day=5), 100.0, "INVENTED HARDWARE",
                 primary="GENERAL_MERCHANDISE", account="card",
                 txn_id="rt-t3")
    x = conn.execute
    x("""INSERT INTO merchants (id, name, name_source, kind)
         VALUES (%s, 'Invented Grocer', 'layer1', 'merchant'),
                (%s, 'Invented Grocer Co', 'layer1', 'merchant')""",
      (m1, m2))
    x("UPDATE transactions SET merchant_id = %s WHERE id = %s", (m1, t1))
    x("UPDATE transactions SET merchant_id = %s WHERE id = %s", (m2, t2))
    x("""INSERT INTO business_entity (id, name, structure, status)
         VALUES (%s, 'Invented Works LLC', 'llc', 'active')""", (ent,))
    x("""INSERT INTO bills (id, type, payee, amount, frequency,
                            monthly_amount, active, raw)
         VALUES ('rec:invented-power', 'bill', 'Invented Power', 80,
                 'MONTHLY', 80, 1, '{}'::jsonb)""")
    x("""INSERT INTO bill_proposals (id, kind, bill_type, payee, amount,
                                     frequency, status)
         VALUES ('prop-1', 'bill', 'occurrence', 'Invented Water', 30,
                 'MONTHLY', 'rejected')""")
    x("""INSERT INTO import_batches (id, source, account_id, filename,
                                     row_count)
         VALUES ('batch-1', 'csv', 'card', 'invented.csv', 2)""")
    x("""INSERT INTO manual_categories (transaction_id, category)
         VALUES (%s, 'FOOD_AND_DRINK')""", (t1,))
    x("""INSERT INTO reimbursements (expense_id, reimburse_id, partial,
                                     amount)
         VALUES (%s, %s, 0, 42.5)""", (t1, t2))
    x("INSERT INTO reimburse_flags (txn_id, partial, expected)"
      " VALUES (%s, 1, 10)", (t3,))
    x("INSERT INTO business_flags (txn_id) VALUES (%s)", (t3,))
    x("""INSERT INTO account_links (group_id, account_id, home_rank)
         VALUES (%s, 'chk', 0)""", (str(uuid.uuid4()),))
    x("""INSERT INTO account_adoptions (account_id, old_account_id, item_id,
                                        adopted_at, reconciled_at)
         VALUES ('chk', 'chk-before', 'it1', now(), now())""")
    x("""INSERT INTO account_sources (door, source_hmac, account_id)
         VALUES ('manual', 'n:' || repeat('ab', 32), 'chk')""")
    rid = str(uuid.uuid4())
    x("""INSERT INTO receipts (id, txn_id, image, mime, kind, status)
         VALUES (%s, %s, %s, 'image/png', 'receipt', 'uploaded')""",
      (rid, t1, IMAGE))
    x("""INSERT INTO receipt_items (receipt_id, line, description, qty,
                                    amount, tag)
         VALUES (%s, 1, 'invented apples', 1, 42.5, '')""", (rid,))
    x("""INSERT INTO entity_membership (id, entity_id, member_name,
                                        ownership_pct, is_manager)
         VALUES (%s, %s, 'Pat Example', 100, true)""",
      (str(uuid.uuid4()), ent))
    x("""INSERT INTO equity_movement (id, entity_id, kind, amount, date)
         VALUES (%s, %s, 'draw', 250, %s)""", (str(uuid.uuid4()), ent, TODAY))
    x("""INSERT INTO business_txn_class (txn_id, bucket)
         VALUES (%s, 'operating')""", (t3,))
    x("""INSERT INTO compliance_obligation (id, entity_id, title, due_date)
         VALUES (%s, %s, 'Invented annual report', %s)""",
      (str(uuid.uuid4()), ent, TODAY))
    x("""INSERT INTO mileage_log (id, entity_id, date, miles, purpose)
         VALUES (%s, %s, %s, 12.5, 'invented site visit')""",
      (str(uuid.uuid4()), ent, TODAY))
    x("""INSERT INTO vendor_1099 (id, entity_id, merchant, reportable,
                                  merchant_id)
         VALUES (%s, %s, 'Invented Grocer', true, %s)""",
      (str(uuid.uuid4()), ent, m1))
    x("INSERT INTO transaction_notes (txn_id, note)"
      " VALUES (%s, 'an invented note')", (t1,))
    splits.set_split(conn, t3, [{"category": "GENERAL_MERCHANDISE",
                                 "amount": 60},
                                {"category": "HOME_IMPROVEMENT",
                                 "amount": 40}])
    x("""INSERT INTO activity_log (id, actor, kind, action, label, summary)
         VALUES (%s, 'pat@example.test', 'category', 'set',
                 'Invented Grocer', 'set a category')""", (str(uuid.uuid4()),))
    # a dismissed alert: the one part of the alert log that travels
    x("""INSERT INTO alerts_log (kind, severity, message, first_seen,
                                 last_seen, active, dismissed)
         VALUES ('low_balance', 'warn', 'Checking is low', %s, %s, 1, 1)""",
      (TODAY, TODAY))
    x("INSERT INTO liabilities (account_id, raw) VALUES ('card', '{}')")
    x("""INSERT INTO holdings (account_id, symbol, name, quantity, price,
                               value)
         VALUES ('chk', 'INVT', 'Invented Fund', 2, 50, 100)""")
    x("""INSERT INTO crypto_holdings (account_id, currency, quantity,
                                      native_usd)
         VALUES ('chk', 'XYZ', 1.5, 30)""")
    x("INSERT INTO networth_recorded (month, total, source)"
      " VALUES ('2026-06', 5000, 'manual')")
    x("INSERT INTO networth_snapshot (date, total) VALUES (%s, 5000)",
      (TODAY,))
    x("""INSERT INTO budget_snapshots (year, month, config, source)
         VALUES (2026, 6, %s, 'close')""", (jsonb({"food_monthly": 900, "other_monthly": 700}),))
    x("INSERT INTO income_annual (year, total_income, joint)"
      " VALUES (2025, 60000, 0)")
    x("""INSERT INTO income_documents (id, year, form, owner, payer,
                                       primary_amount)
         VALUES (1, 2025, 'W-2', 'primary', 'Invented Employer', 60000)""")
    x("""INSERT INTO merchant_canonical (raw_merchant, canonical, method)
         VALUES ('INVENTED GROCER #12', 'Invented Grocer', 'manual')""")
    x("""INSERT INTO merchant_renames (raw_merchant, from_canonical,
                                       to_canonical, at)
         VALUES ('INVENTED GROCER #12', 'Invented Grocer #12',
                 'Invented Grocer', now())""")
    x("""INSERT INTO merchant_categories (merchant, category_primary, source)
         VALUES ('Invented Grocer', 'FOOD_AND_DRINK', 'user')""")
    x("""INSERT INTO merchant_merge_proposals (id, from_merchant_id,
                                               into_merchant_id, status)
         VALUES ('merge-1', %s, %s, 'rejected')""", (m2, m1))
    x("""INSERT INTO amazon_orders (dedup_key, account, date, amount, payee,
                                    is_refund, items_json)
         VALUES ('amz-1', 'main', %s, 42.5, 'Amazon', 0, '[]')""", (TODAY,))
    x("INSERT INTO amazon_matches (transaction_id, dedup_key)"
      " VALUES (%s, 'amz-1')", (t1,))
    x("INSERT INTO amazon_summaries (dedup_key, summary)"
      " VALUES ('amz-1', 'invented apples')")
    x("""INSERT INTO costco_receipts (dedup_key, account, date, amount,
                                      items_json)
         VALUES ('cst-1', 'main', %s, 100, '[]')""", (TODAY,))
    x("INSERT INTO costco_matches (transaction_id, dedup_key)"
      " VALUES (%s, 'cst-1')", (t3,))


def fresh_tenant():
    _ensure_db()
    admin = tenancy.admin_connect()
    tid = tenancy.create_tenant(admin, f"rt-{uuid.uuid4().hex[:8]}")
    admin.close()
    return tenancy.tenant_connect(tid)


def member_rows(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    out = {}
    for name in z.namelist():
        if name.endswith(".csv") and name[:-4] in restore.RESTORED_MEMBERS:
            text = z.read(name).decode("utf-8")
            out[name[:-4]] = sum(1 for _ in csv.DictReader(io.StringIO(text)))
    return out


def without(data: bytes, member: str) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            if info.filename != member:
                z.writestr(info.filename, src.read(info.filename))
    return buf.getvalue()


def counted(conn, tables) -> dict:
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            for t in tables}


class RoundTripTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        src = make_db()
        try:
            fill_every_table(src)
            # what the restore runs once its rows have landed, run here
            # first: the source is a household the nightly has already seen
            merchant_identity.resolve(src)
            cls.data = export.build_zip(src)
            cls.held = counted(src, restore.RESTORED_MEMBERS - NOT_ROWS)
        finally:
            src.close()
        cls.rows = member_rows(cls.data)

    def test_the_fixture_fills_every_restored_table(self):
        empty = sorted(t for t, n in self.held.items() if not n)
        self.assertEqual(empty, [], "a restored table the fixture leaves "
                                    "empty is a table this test never checks")

    def _every_row_has_one_outcome(self, out):
        for table, n in self.rows.items():
            if table in NOT_ROWS:
                continue
            refused = sum(v for (t, _), v in out.refused.items()
                          if t == table)
            self.assertEqual(out.written.get(table, 0) + refused, n,
                             f"{table}: {n} archived rows, "
                             f"{out.written.get(table, 0)} written, "
                             f"{refused} refused")

    def _restores_whole_then_writes_nothing(self, data):
        dest = fresh_tenant()
        try:
            _, out = restore.restore_outcomes(dest, data)
            self._every_row_has_one_outcome(out)
            self.assertEqual(out.refused, {}, "a clean archive into an "
                                              "empty household refuses nothing")
            self.assertEqual(counted(dest, self.held), self.held)

            again, out2 = restore.restore_outcomes(dest, data)
            self._every_row_has_one_outcome(out2)
            self.assertEqual({t: n for t, n in out2.written.items()
                              if n and t not in NOT_ROWS}, {},
                             "a second restore wrote rows")
            self.assertEqual({k: v for k, v in out2.refused.items()
                              if k[1] != "present"}, {},
                             "a second restore reported more than "
                             "'already present'")
            self.assertEqual(out2.repaired, {})
            self.assertEqual(
                again.get("already_present"),
                sum(n for t, n in self.rows.items() if t not in NOT_ROWS))
            self.assertEqual(counted(dest, self.held), self.held)
        finally:
            dest.close()

    def test_a_restore_brings_the_household_back_and_a_second_writes_nothing(self):
        self._restores_whole_then_writes_nothing(self.data)

    def test_an_archive_without_a_manifest_still_restores_whole(self):
        old = without(self.data, archive_upgrades.MANIFEST)
        # its rows still say how new it is, so nothing that would undo a
        # choice its household made runs over it
        self.assertEqual(archive_upgrades.archive_schema(
            zipfile.ZipFile(io.BytesIO(old))),
            archive_upgrades.FLOOR_COLUMNS[-1][0])
        self._restores_whole_then_writes_nothing(old)

    def test_the_export_says_which_schema_its_rows_are_in(self):
        z = zipfile.ZipFile(io.BytesIO(self.data))
        doc = json.loads(z.read(archive_upgrades.MANIFEST))
        self.assertEqual(doc["schema"], archive_upgrades.migration_names()[-1])
        self.assertIn("transactions", doc["tables"])
        self.assertEqual(archive_upgrades.archive_schema(z), doc["schema"])

    def test_a_manifest_from_a_newer_install_reads_as_newer(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(archive_upgrades.MANIFEST, json.dumps(
                {"format": 1, "schema": "999_from_the_future.sql"}))
        schema = archive_upgrades.archive_schema(
            zipfile.ZipFile(io.BytesIO(buf.getvalue())))
        self.assertEqual(schema, "999_from_the_future.sql")
        self.assertFalse(any(archive_upgrades.older_than(schema, n)
                             for n in archive_upgrades.migration_names()))

    def test_a_manifest_this_install_cannot_read_reads_as_the_oldest(self):
        newest = archive_upgrades.migration_names()[-1]
        for doc in ({"format": 1, "schema": "not a migration"},
                    {"format": 2, "schema": newest},
                    "not an object", None):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr(archive_upgrades.MANIFEST, json.dumps(doc))
            self.assertIsNone(archive_upgrades.archive_schema(
                zipfile.ZipFile(io.BytesIO(buf.getvalue()))), doc)
