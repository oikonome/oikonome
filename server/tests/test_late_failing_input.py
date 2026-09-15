"""Input accepted at the door that only becomes a 500, or a wrong number,
much later.

These are grouped together because they share one shape: input that is
accepted at the door and only becomes a 500 (or a wrong number) much later,
somewhere the user cannot connect to what they typed.
"""

import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import compat, compliance, entities, equity, selfemploy

from .util import TEST_DB, _admin_dsn, _ensure_db


class InputBoundsTests(unittest.TestCase):
    """Bounds belong at the door, not at the DB driver."""

    def test_absurd_year_is_rejected_on_the_way_in(self):
        """An entity formed in year 9999 was accepted, then crashed the
        compliance calendar later with an uncaught ValueError from
        `date(year+1, ...)` — a 500 on a page the user never linked to the
        value they typed."""
        with self.assertRaises(ValueError):
            compat.as_date("9999-01-01")
        with self.assertRaises(ValueError):
            compat.as_date("0001-01-01")
        # ordinary dates still pass, including a date object
        self.assertEqual(compat.as_date("2026-07-28"), dt.date(2026, 7, 28))
        self.assertEqual(compat.as_date(dt.date(1999, 1, 1)),
                         dt.date(1999, 1, 1))
        self.assertIsNone(compat.as_date(None))


class EquityGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"equity-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="single_member_llc")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_absurd_amount_is_a_400_not_a_driver_error(self):
        """NUMERIC(14,2) overflows into psycopg's NumericValueOutOfRange —
        a 500 where the sibling 'must be positive' check gives a 400."""
        with self.assertRaises(ValueError):
            equity.record_movement(self.conn, self.ent["id"], kind="contribution",
                                   amount=10 ** 15, date="2026-07-01")

    def test_malformed_member_id_is_a_400_not_a_driver_error(self):
        with self.assertRaises(ValueError):
            equity.record_movement(self.conn, self.ent["id"], kind="contribution",
                                   amount=10.0, date="2026-07-01",
                                   member_id="not-a-uuid")

    def test_home_office_sqft_has_an_upper_bound(self):
        with self.assertRaises(ValueError):
            entities.update_entity(self.conn, self.ent["id"],
                                   home_office_sqft=10 ** 12)

    def test_absurd_mileage_is_a_400_not_a_driver_error(self):
        """miles is NUMERIC(10,1): anything from a billion up overflows in
        the driver, so the bound has to be checked at the door alongside
        'must be positive' or the user gets a 500 for a typed number."""
        with self.assertRaises(ValueError):
            selfemploy.add_trip(self.conn, self.ent["id"], date="2026-08-01",
                                miles=99_999_999_999)
        # the largest value the column can hold is still accepted
        selfemploy.add_trip(self.conn, self.ent["id"], date="2026-08-01",
                            miles=999_999_999.9)

    def test_absurd_obligation_fee_is_a_400_not_a_driver_error(self):
        """fee is NUMERIC(10,2) — same shape, same door."""
        with self.assertRaises(ValueError):
            compliance.add_obligation(self.conn, self.ent["id"], title="x",
                                      due_date="2026-08-01",
                                      fee=9_999_999_999)
        compliance.add_obligation(self.conn, self.ent["id"], title="x",
                                  due_date="2026-08-01", fee=99_999_999.99)


class PurgeAndIdempotencyTests(unittest.TestCase):
    """Migration 067: transaction children, and one movement per txn+kind."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        from .util import add_txn, seed_accounts
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"purge-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="single_member_llc")
        add_txn(self.conn, "2026-07-10", 60.0, "SUPPLIES", account="card",
                txn_id="t1")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_purging_a_transaction_takes_its_annotations_with_it(self):
        """Eight tables keyed on a transaction id were left pointing at rows
        that no longer existed — silent, and a future id collision would
        re-attach someone's old note to a new transaction."""
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES ('t1','mine')")
        self.conn.execute(
            "INSERT INTO business_flags (txn_id) VALUES ('t1')")
        self.conn.execute("DELETE FROM transactions WHERE id='t1'")
        for table in ("transaction_notes", "business_flags"):
            n = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE txn_id='t1'"
            ).fetchone()["n"]
            self.assertEqual(n, 0, f"{table} orphan survived the purge")

    def test_equity_movement_survives_its_transaction(self):
        """SET NULL, not CASCADE: a contribution is a real accounting entry.
        Deleting someone's books as a side effect of removing an account
        would be worse than the orphan bug."""
        equity.record_movement(self.conn, self.ent["id"], kind="contribution",
                               amount=60.0, date="2026-07-10", txn_id="t1")
        self.conn.execute("DELETE FROM transactions WHERE id='t1'")
        rows = equity.list_movements(self.conn, self.ent["id"])
        self.assertEqual(len(rows), 1, "the accounting entry was destroyed")
        self.assertIsNone(rows[0]["txn_id"])       # only the link went

    def test_double_submit_does_not_double_owner_equity(self):
        a = equity.contribute_expense(self.conn, self.ent["id"], "t1")
        b = equity.contribute_expense(self.conn, self.ent["id"], "t1")
        self.assertEqual(a["id"], b["id"], "a second click created a second "
                                           "contribution — owner equity doubles")
        self.assertEqual(
            len(equity.list_movements(self.conn, self.ent["id"])), 1)


class SyncSingleFlightTests(unittest.TestCase):
    """Four doors reach sync_tenant with nothing stopping two at once."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_second_sync_for_the_same_tenant_is_refused(self):
        """Overlapping runs clobber items.tx_cursor — each writes the cursor
        it STARTED from, so one run's progress is lost — and both observe the
        same status transition, so the connection-break email goes out twice."""
        from oikonome.db import tenancy as _t
        from oikonome.jobs import worker
        admin = _t.admin_connect(_admin_dsn(TEST_DB))
        tid = str(_t.create_tenant(admin, f"syncflight-{uuid.uuid4().hex[:8]}"))
        # hold the lock exactly as an in-flight sync would
        holder = _t.tenant_connect(tid)
        try:
            got = holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{tid}",)).fetchone()["ok"]
            self.assertTrue(got)
            r = worker.sync_tenant(tid)
            self.assertEqual(r.get("_status"), "already-running",
                             "a concurrent sync ran anyway")
        finally:
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                           (f"oikonome:sync:{tid}",))
            holder.close()
            admin.close()

    def test_the_lock_is_released_so_the_next_sync_can_run(self):
        """A session-level advisory lock outlives its transaction; a pooled
        connection handed back still holding it would lock that tenant out of
        syncing until the backend died."""
        from oikonome.db import tenancy as _t
        from oikonome.jobs import worker
        admin = _t.admin_connect(_admin_dsn(TEST_DB))
        tid = str(_t.create_tenant(admin, f"synclock-{uuid.uuid4().hex[:8]}"))
        try:
            worker.sync_tenant(tid)          # no items; returns immediately
            probe = _t.tenant_connect(tid)
            try:
                free = probe.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    (f"oikonome:sync:{tid}",)).fetchone()["ok"]
                self.assertTrue(free, "sync_tenant leaked its advisory lock")
                probe.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                              (f"oikonome:sync:{tid}",))
            finally:
                probe.close()
        finally:
            admin.close()


class ReferralCapTests(unittest.TestCase):
    """Signups are cheap to manufacture; the reward was unbounded."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()



class AttentionCountTests(unittest.TestCase):
    """A count is not a page of results."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_unclassified_count_is_not_capped_by_the_suggestion_limit(self):
        from oikonome.engine import books

        from .util import add_txn, seed_accounts
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        tid = str(tenancy.create_tenant(admin, f"cnt-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            ent = entities.create_entity(
                conn, name="Acme LLC", structure="single_member_llc")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('biz','it1','Biz','depository','checking',1)")
            entities.assign_account(conn, "biz", ent["id"])
            for i in range(12):
                add_txn(conn, "2026-07-10", 5.0, f"VENDOR {i}",
                        account="biz", txn_id=f"u{i}")
            capped = books.suggest_lines(conn, ent["id"], limit=5)
            self.assertEqual(len(capped), 5)                  # the page
            self.assertEqual(books.unclassified_count(conn, ent["id"]), 12)
        finally:
            conn.close()
            admin.close()


class MigrationOverExistingDuplicatesTests(unittest.TestCase):
    """Migration 067 must survive the very data the bug it fixes produced.

    Without a de-dup step before it, the unique index meets the duplicate
    pair a tenant who double-clicked already has — and CREATE UNIQUE INDEX
    over duplicates raises, rolls back
    the whole migration (one transaction per file), never stamps
    schema_migrations, and re-fails on every boot. The instance does not start
    until an operator hand-deletes rows.
"""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_migration_dedups_instead_of_wedging_the_instance(self):
        from oikonome.db import migrate

        from .util import add_txn, seed_accounts
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        tid = str(tenancy.create_tenant(admin, f"dup-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            ent = entities.create_entity(
                conn, name="Acme LLC", structure="single_member_llc")
            add_txn(conn, "2026-07-10", 50.0, "SUPPLIES", account="card",
                    txn_id="dup1")
            # drop the guard, plant the duplicate the live bug would create
            admin.execute("DROP INDEX IF EXISTS equity_movement_txn_kind_uniq")
            for _ in range(2):
                conn.execute(
                    """INSERT INTO equity_movement (entity_id, kind, amount,
                           date, txn_id)
                       VALUES (%s,'contribution',50.0,'2026-07-10','dup1')""",
                    (ent["id"],))
            self.assertEqual(self._n(conn), 2, "precondition: duplicates exist")
            admin.execute("DELETE FROM schema_migrations WHERE name LIKE '067%'")

            migrate.run(_admin_dsn(TEST_DB))          # must not raise

            self.assertEqual(self._n(conn), 1,
                             "migration left duplicates behind")
            stamped = admin.execute(
                "SELECT 1 FROM schema_migrations WHERE name LIKE '067%'"
            ).fetchone()
            self.assertIsNotNone(stamped, "migration rolled back — every "
                                          "subsequent boot re-fails")
        finally:
            conn.close()
            admin.close()

    @staticmethod
    def _n(conn):
        return conn.execute(
            "SELECT COUNT(*) AS n FROM equity_movement WHERE txn_id='dup1' "
            "AND kind='contribution'").fetchone()["n"]


if __name__ == "__main__":
    unittest.main()
