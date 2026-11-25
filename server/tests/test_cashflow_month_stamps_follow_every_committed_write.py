"""The Cash Flow picture keeps each household's per-month stamps in
memory and serves them again while a probe says no dated ledger row was
written. A write and its undo must never look like nothing happened: the
probe is the set of transactions that wrote the household's dated rows,
which every committed write moves and no undo moves back. Reading the
picture writes nothing, one household's writes never move another's
probe, and the restore's bulk landing moves it like any other write."""

import datetime as dt
import os
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import reporting
from oikonome.sync import restore

from .util import TEST_DB, _admin_dsn, add_txn, make_db, write_config

APP_DSN = os.environ.get(
    "OIKONOME_TEST_DSN",
    f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
DATED = ("transactions", "transaction_splits", "manual_categories",
         "reimbursements")


class MonthStampsFollowEveryCommittedWrite(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        reporting._FIXED_STAMPS_MEMO.clear()
        self.addCleanup(reporting._FIXED_STAMPS_MEMO.clear)
        self.rent = {}
        for m in (dt.date(2024, 1, 1), dt.date(2024, 2, 1),
                  dt.date(2024, 3, 1)):
            self.rent[m] = add_txn(self.conn, m.replace(day=10), 1200.0,
                                   "RENT", primary="RENT_AND_UTILITIES",
                                   account="chk")
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def peer(self, tid=None):
        """Another request's connection to a household: what it commits,
        someone else committed."""
        other = tenancy.tenant_connect(tid or self.tid, APP_DSN)
        self.addCleanup(other.close)
        return other

    def split(self, conn, month):
        rid = self.rent[month]
        conn.execute(
            "INSERT INTO transaction_splits (txn_id, line, category, amount)"
            " VALUES (%s, 1, 'RENT_AND_UTILITIES', 800), "
            "(%s, 2, 'FOOD_AND_DRINK', 400)", (rid, rid))

    def unsplit(self, conn, month):
        conn.execute("DELETE FROM transaction_splits WHERE txn_id = %s",
                     (self.rent[month],))

    def marks(self, conn):
        return conn.execute(
            "SELECT count(*) AS n FROM ledger_writes").fetchone()["n"]

    def test_a_write_and_its_undo_across_the_stamp_pass_are_never_served(self):
        """A split filed just before the month stamps are computed, and
        removed just after, leaves stamps that include it. Filed under a
        probe the undo restored, they would be served to every later
        request in place of the household's real months."""
        peer = self.peer()
        old = dt.date(2024, 2, 1)
        real = reporting._fixed_month_stamps_pass

        def racing(conn):
            self.split(peer, old)
            try:
                return real(conn)
            finally:
                self.unsplit(peer, old)

        with mock.patch.object(reporting, "_fixed_month_stamps_pass", racing):
            reporting._fixed_month_stamps(self.conn)
        self.assertEqual(reporting._fixed_month_stamps(self.conn),
                         real(self.conn))

    def test_a_write_and_its_undo_in_one_transaction_move_the_probe(self):
        before = reporting._fixed_month_probe(self.conn)
        peer = self.peer()
        with peer.transaction():
            add_txn(peer, dt.date(2024, 2, 20), 30.0, "COFFEE",
                    txn_id="gone")
            peer.execute("DELETE FROM transactions WHERE id = 'gone'")
        self.assertNotEqual(reporting._fixed_month_probe(self.conn), before)

    def test_a_plain_write_to_each_dated_table_moves_the_probe(self):
        rid = self.rent[dt.date(2024, 1, 1)]
        dep = add_txn(self.conn, dt.date(2024, 1, 20), -600.0, "LANDLORD",
                      primary="TRANSFER_IN", account="chk")
        writes = [
            ("transactions", "UPDATE transactions SET amount = 1250 "
                             "WHERE id = %s", (rid,)),
            ("transaction_splits",
             "INSERT INTO transaction_splits (txn_id, line, category, amount)"
             " VALUES (%s, 1, 'RENT_AND_UTILITIES', 1250)", (rid,)),
            ("manual_categories",
             "INSERT INTO manual_categories (transaction_id, category) "
             "VALUES (%s, 'FOOD_AND_DRINK')", (rid,)),
            ("reimbursements",
             "INSERT INTO reimbursements (expense_id, reimburse_id, partial,"
             " amount) VALUES (%s, %s, 1, 600)", (rid, dep)),
        ]
        self.assertEqual({w[0] for w in writes}, set(DATED))
        for table, sql, params in writes:
            before = reporting._fixed_month_probe(self.conn)
            self.conn.execute(sql, params)
            self.assertNotEqual(reporting._fixed_month_probe(self.conn),
                                before, f"a write to {table} went unseen")

    def test_a_statement_that_writes_no_row_leaves_the_probe(self):
        before = reporting._fixed_month_probe(self.conn)
        self.conn.execute("UPDATE transactions SET amount = 1 "
                          "WHERE id = 'no-such-row'")
        self.assertEqual(reporting._fixed_month_probe(self.conn), before)

    def test_reading_the_picture_writes_nothing(self):
        reporting.flow_breakdown(self.conn, dt.date(2024, 4, 15), "all")
        before = reporting._fixed_month_probe(self.conn)
        marks = self.marks(self.conn)
        reporting.flow_breakdown(self.conn, dt.date(2024, 4, 15), "all")
        reporting._fixed_month_stamps(self.conn)
        self.assertEqual(reporting._fixed_month_probe(self.conn), before)
        self.assertEqual(self.marks(self.conn), marks)

    def test_one_households_writes_never_move_anothers_probe(self):
        other = make_db()
        self.addCleanup(other.close)
        add_txn(other, dt.date(2024, 2, 2), 10.0, "TEA")
        theirs = reporting._fixed_month_probe(other)
        mine = reporting._fixed_month_probe(self.conn)
        for _ in range(3):
            add_txn(self.conn, dt.date(2024, 2, 3), 10.0, "TEA")
        self.assertEqual(reporting._fixed_month_probe(other), theirs)
        self.assertNotEqual(reporting._fixed_month_probe(self.conn), mine)
        # and neither household can see the other's marks
        otid = other.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM ledger_writes WHERE tenant_id = %s",
            (otid,)).fetchone()["n"], 0)

    def test_the_set_stays_small_however_many_writes_land(self):
        for i in range(25):
            add_txn(self.conn, dt.date(2024, 3, 2), 1.0 + i, "GUM")
        self.assertEqual(self.marks(self.conn), 1)

    def test_the_restores_bulk_landing_moves_the_probe_once(self):
        """The restore lands rows one statement per row through
        _Outcomes.land; on a tenant connection or the admin one, the
        household whose rows landed is the one marked, and the batch
        leaves one mark, not one per row."""
        sql = ("INSERT INTO transactions (id, account_id, date, amount, name,"
               " removed) VALUES (%s, 'chk', %s, %s, 'RESTORED', 0)"
               " ON CONFLICT (tenant_id, id) DO NOTHING RETURNING id")
        before = reporting._fixed_month_probe(self.conn)
        out = restore._Outcomes()
        with self.conn.transaction():
            out.land(self.conn, "transactions", sql,
                     [(f"r{i}", dt.date(2023, 6, 1), 5.0) for i in range(50)])
        self.assertNotEqual(reporting._fixed_month_probe(self.conn), before)
        self.assertEqual(self.marks(self.conn), 1)
        before = reporting._fixed_month_probe(self.conn)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        with admin.transaction():
            admin.execute(
                "INSERT INTO transactions (tenant_id, id, account_id, date,"
                " amount, name, removed) VALUES (%s, 'adm', 'chk', %s, 5,"
                " 'RESTORED', 0)", (self.tid, dt.date(2023, 7, 1)))
        self.assertNotEqual(reporting._fixed_month_probe(self.conn), before)

    def test_erasing_a_household_leaves_no_mark_behind(self):
        other = make_db()
        otid = other.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        add_txn(other, dt.date(2024, 2, 2), 10.0, "TEA")
        other.close()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        with admin.transaction():
            tenancy.delete_tenant_rows(admin, otid)
        self.assertEqual(admin.execute(
            "SELECT count(*) AS n FROM ledger_writes WHERE tenant_id = %s",
            (otid,)).fetchone()["n"], 0)


    def test_the_app_can_write_the_ledger_before_the_migrations_finish(self):
        """An older database is upgraded with schema.sql first, while the
        running app keeps writing, and the blanket table grant comes only
        after every migration. schema.sql creates the triggers, so it must
        also grant the table they write, or each write to a dated table
        fails for as long as the migrations take."""
        from pathlib import Path

        from oikonome.db import migrate
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        self.addCleanup(admin.execute,
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ledger_writes"
                        " TO oikonome_app, oikonome_admin")
        admin.execute("REVOKE ALL ON ledger_writes"
                      " FROM oikonome_app, oikonome_admin")
        migrate._apply_schema(
            admin, (Path(migrate.HERE) / "schema.sql").read_text())
        before = reporting._fixed_month_probe(self.conn)
        add_txn(self.conn, dt.date(2024, 3, 3), 4.0, "GUM")
        self.assertNotEqual(reporting._fixed_month_probe(self.conn), before)

if __name__ == "__main__":
    unittest.main()
