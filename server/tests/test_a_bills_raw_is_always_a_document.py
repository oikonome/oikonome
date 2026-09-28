"""`raw` on a bill and on a connection can only ever be a document.

Every writer of those columns EXTENDS the value it finds: `raw || '{...}'`
to merge keys, `jsonb_set(raw, ...)` to set one. Both are defined only when
the left side is an object. Hand `||` a scalar and Postgres raises, which
aborts the statement — and these writes run inside the nightly pass over
every bill in the household, so one wrongly shaped row stops the pass for
all of them, the same way every night. Hand `||` an ARRAY and there is no
error at all: the patch is APPENDED as an element, the keys that were meant
to be written are nowhere, and the row silently stops carrying its own
state.

Guarding each writer covers only the writers that exist when the guard is
written; the column itself can be made incapable of holding anything else,
and then every writer inherits it — the ones here today, a collector's push
path, the next importer.

NULL stays legal: it already means "no document yet" to every reader, and
the writers COALESCE it — which is the second thing pinned here, because a
bare `raw || '{...}'` over a NULL evaluates to NULL, so the write lands as
a no-op and the pass that "succeeded" wrote nothing.
"""

import json
import unittest
from pathlib import Path

import psycopg

from oikonome.db import tenancy
from oikonome.engine import bills

from . import util
from .util import TODAY, add_bill, make_db

MIGRATION = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
             / "migrations" / "128_raw_is_an_object.sql")

PINNED = (("bills", "bills_raw_is_an_object"),
          ("items", "items_raw_is_an_object"))


class TheColumnRefusesAnythingButADocument(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_non_document_raw_cannot_be_stored(self):
        for table, cols, vals in (
                ("bills", "(id, payee, raw)", "('rec:x','Zenith Fibre'"),
                ("items", "(id, aggregator, raw)", "('it9','test'")):
            for shape in ("'[]'::jsonb", "'5'::jsonb", '\'"x"\'::jsonb'):
                with self.subTest(table=table, shape=shape):
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        with self.conn.transaction():
                            self.conn.execute(
                                f"INSERT INTO {table} {cols} VALUES "
                                f"{vals}, {shape})")

    def test_a_missing_document_is_still_allowed(self):
        # NULL is not a wrong shape — it is the absence of one, and every
        # reader already treats it as an empty document
        self.conn.execute(
            "INSERT INTO items (id, aggregator, raw) VALUES ('it8','test',NULL)")
        self.assertIsNone(self.conn.execute(
            "SELECT raw FROM items WHERE id='it8'").fetchone()["raw"])


class ALedgerThatAlreadyHoldsABadRow(unittest.TestCase):
    """A household that was poisoned before the column was pinned is
    repaired by the migration rather than refused by it — a constraint that
    cannot be applied to the data that exists is not a fix."""

    def setUp(self):
        self.conn = make_db()
        self.tenant_id = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.admin = tenancy.admin_connect(util._admin_dsn(util.TEST_DB))

    def tearDown(self):
        try:
            self.admin.execute(
                "DELETE FROM bills WHERE id = 'rec:legacy-bad'")
            self.admin.execute("DELETE FROM items WHERE id = 'it-legacy-bad'")
            self._repin()
        finally:
            self.admin.close()
            self.conn.close()

    def _unpin(self):
        for table, name in PINNED:
            self.admin.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")

    def _repin(self):
        self.admin.execute(MIGRATION.read_text())

    def _pinned(self) -> set:
        return {r["conname"] for r in self.admin.execute(
            "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
            ([n for _t, n in PINNED],)).fetchall()}

    def test_the_migration_repairs_a_legacy_row_and_then_pins_the_column(self):
        self._unpin()
        self.assertEqual(set(), self._pinned())
        # reach past the constraint the way an older instance's data does:
        # the rows predate it
        self.admin.execute(
            """INSERT INTO bills (tenant_id, id, type, payee, amount, active,
                                  raw)
               VALUES (%s, 'rec:legacy-bad', 'BILL', 'Legacy Bill', -40, 1,
                       %s::jsonb)""",
            (self.tenant_id, json.dumps(["dueOn", "2026-01-01"])))
        self.admin.execute(
            """INSERT INTO items (tenant_id, id, aggregator, institution_name,
                                  raw)
               VALUES (%s, 'it-legacy-bad', 'test', 'Test Bank', %s::jsonb)""",
            (self.tenant_id, json.dumps(7)))

        self._repin()

        self.assertEqual({n for _t, n in PINNED}, self._pinned())
        # the rows keep everything else they say about themselves and lose
        # only the part nothing could read
        bill = self.conn.execute(
            "SELECT payee, amount, raw FROM bills WHERE id='rec:legacy-bad'"
        ).fetchone()
        self.assertEqual(bill["payee"], "Legacy Bill")
        self.assertEqual(bill["amount"], -40.0)
        self.assertEqual(bill["raw"], {})
        item = self.conn.execute(
            "SELECT institution_name, raw FROM items WHERE id='it-legacy-bad'"
        ).fetchone()
        self.assertEqual(item["institution_name"], "Test Bank")
        self.assertEqual(item["raw"], {})

    def test_the_migration_leaves_a_good_document_alone(self):
        self._unpin()
        self.admin.execute(
            """INSERT INTO bills (tenant_id, id, type, payee, amount, active,
                                  raw)
               VALUES (%s, 'rec:legacy-bad', 'BILL', 'Legacy Bill', -40, 1,
                       %s::jsonb)""",
            (self.tenant_id, json.dumps({"dueOn": "2026-01-01"})))
        self._repin()
        self.assertEqual(
            self.conn.execute("SELECT raw FROM bills WHERE id='rec:legacy-bad'"
                              ).fetchone()["raw"],
            {"dueOn": "2026-01-01"})


class ExtendingAnAbsentDocumentWritesIt(unittest.TestCase):
    """`raw || '{...}'` over a NULL is NULL: the UPDATE reports a row
    changed and changes nothing. The bill below then arrives at every
    nightly pass with no identity, is bootstrapped again, and the write is
    lost again — forever."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_bootstrapping_a_bill_with_no_document_gives_it_one(self):
        # the nightly identity pass counts this bill as bootstrapped: the
        # count and the row have to agree, or every following night does
        # the same work and reports the same success
        add_bill(self.conn, "Zenith Fibre", 80, next_due=TODAY)
        self.conn.execute("UPDATE bills SET raw = NULL WHERE payee=%s",
                          ("Zenith Fibre",))
        self.assertEqual(
            1, bills.reconcile_bill_merchants(
                self.conn, TODAY)["bootstrapped"])
        raw = self.conn.execute(
            "SELECT raw FROM bills WHERE payee='Zenith Fibre'"
        ).fetchone()["raw"]
        self.assertIsNotNone(raw)
        self.assertIn("merchant_refs", raw)


if __name__ == "__main__":
    unittest.main()
