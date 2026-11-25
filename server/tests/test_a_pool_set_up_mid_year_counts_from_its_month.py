"""A yearly pool set up during the year counts the year's spending at
its merchant from the month it was set up (bills.created_on, migration
235; specs/today-figures.md, "Envelopes are their own row"). Spending
before it was everyday spending in its own months, and a pool with no
day (every pool stored before the day was kept) counts from January 1
as before.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.db import migrate, tenancy
from oikonome.engine import budget
from oikonome.sync import export, restore
from oikonome.web import lenses

from .util import (TEST_DB, _admin_dsn, add_bill, add_txn, freeze_month,
                   make_db, write_config)

MIGRATION = (migrate.HERE / "migrations"
             / "235_a_pool_set_up_mid_year_counts_from_its_month.sql")


def _household(created_on):
    """A $780 yearly garden pool, $310 spent at its merchant in March and
    $55 in September; the pool set up on `created_on` (None: not
    known)."""
    conn = make_db()
    write_config(conn, food_monthly=500, other_monthly=500,
                 budgeted_income_monthly=4000)
    add_bill(conn, "Garden Supply", 780.0, bill_type="envelope",
             frequency=None, interval=12, merchant="garden supply",
             created_on=created_on)
    add_txn(conn, dt.date(2026, 3, 12), 310.0, "GARDEN SUPPLY",
            primary="GENERAL_MERCHANDISE", txn_id="garden-1")
    add_txn(conn, dt.date(2026, 9, 8), 55.0, "GARDEN SUPPLY",
            primary="GENERAL_MERCHANDISE", txn_id="garden-2")
    return conn


def _pool(conn, day):
    st = budget.month_status(conn, day)
    return next(e for e in st["buckets"]["fixed"]["envelopes"]
                if e["payee"] == "Garden Supply")


class APoolCountsFromItsMonthTests(unittest.TestCase):

    def test_a_pool_set_up_in_august_does_not_count_march(self):
        conn = _household(dt.date(2026, 8, 14))
        self.addCleanup(conn.close)
        sep = _pool(conn, dt.date(2026, 9, 20))
        self.assertEqual((sep["used"], sep["period_used"],
                          sep["period_left"]), (55.0, 55.0, 725.0))

    def test_a_pool_with_no_day_counts_from_january_as_before(self):
        conn = _household(None)
        self.addCleanup(conn.close)
        sep = _pool(conn, dt.date(2026, 9, 20))
        self.assertEqual((sep["period_used"], sep["period_left"]),
                         (365.0, 415.0))

    def test_a_pool_set_up_in_an_earlier_year_counts_from_january(self):
        conn = _household(dt.date(2025, 11, 2))
        self.addCleanup(conn.close)
        self.assertEqual(_pool(conn, dt.date(2026, 9, 20))["period_left"],
                         415.0)

    def test_a_day_before_the_pool_was_set_up_has_no_pool(self):
        """A past day read under a pool set up since: no pool that day."""
        conn = _household(dt.date(2026, 10, 3))
        self.addCleanup(conn.close)
        st = budget.month_status(conn, dt.date(2026, 9, 20))
        self.assertEqual(st["buckets"]["fixed"]["envelopes"], [])

    def test_the_month_it_was_set_up_counts_from_its_first_day(self):
        conn = _household(dt.date(2026, 3, 25))
        self.addCleanup(conn.close)
        sep = _pool(conn, dt.date(2026, 9, 20))
        self.assertEqual(sep["period_left"], 415.0)

    def test_a_frozen_month_keeps_the_day(self):
        conn = _household(dt.date(2026, 8, 14))
        self.addCleanup(conn.close)
        freeze_month(conn, 2026, 9)
        conn.commit()
        rows = budget.snapshot_bill_rows(conn, 2026, 9)
        self.assertEqual(rows[0]["created_on"], "2026-08-14")
        st = lenses.month_state(conn, 2026, 9, dt.date(2026, 10, 5))
        env = st["buckets"]["fixed"]["envelopes"][0]
        self.assertEqual(env["period_left"], 725.0)

    def test_a_new_bill_is_dated_and_an_edit_keeps_its_day(self):
        from oikonome.engine import bills
        conn = make_db()
        self.addCleanup(conn.close)
        bills.save_bill(conn, payee="Garden Supply", amount=780.0,
                        bill_type="envelope", interval=12)
        made = conn.execute("SELECT created_on FROM bills").fetchone()[
            "created_on"]
        self.assertIsNotNone(made)
        conn.execute("UPDATE bills SET created_on = '2026-02-03'")
        bills.save_bill(conn, payee="Garden Supply", amount=800.0,
                        bill_type="envelope", interval=12)
        self.assertEqual(conn.execute("SELECT created_on FROM bills")
                         .fetchone()["created_on"], dt.date(2026, 2, 3))


class BeforeItsDayThePoolIsNotThereTests(unittest.TestCase):
    """A $680 yearly pool set up on Jul 22, its merchant charged $85 on
    the 6th of every month since January."""

    def _household(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=500, other_monthly=500,
                     budgeted_income_monthly=4000)
        add_bill(conn, "Bike Workshop", 680.0, bill_type="envelope",
                 frequency=None, interval=12, merchant="bike workshop",
                 created_on=dt.date(2026, 7, 22))
        for m in range(1, 8):
            add_txn(conn, dt.date(2026, m, 6), 85.0, "BIKE WORKSHOP",
                    primary="GENERAL_MERCHANDISE", txn_id=f"bike-{m}")
        return conn

    def test_a_day_before_it_was_set_up_has_no_pool(self):
        conn = self._household()
        st = budget.month_status(conn, dt.date(2026, 7, 12))
        self.assertEqual(st["buckets"]["fixed"]["envelopes"], [])
        # its merchant's charge is ordinary spending, never past a pool
        other = st["buckets"]["other"]["rows"]
        self.assertIn(85.0, [r["amount"] for r in other
                             if "BIKE" in (r["payee"] or "")])
        self.assertFalse([r for r in other
                          if r.get("category") == "envelope overflow"])

    def test_from_its_day_it_counts_from_its_month(self):
        conn = self._household()
        env = _pool_named(conn, dt.date(2026, 7, 27), "Bike Workshop")
        self.assertEqual((env["used"], env["period_left"]), (85.0, 595.0))

    def test_the_budget_suggestion_reads_the_months_before_it_as_spending(
            self):
        """The suggestion's window reads each month as the month read then
        had it: before the pool's month, the charge is everyday spending."""
        conn = self._household()
        prior = budget._envelope_prior_used(
            conn, budget._envelope_bills(conn), dt.date(2026, 4, 1))
        self.assertEqual(prior, {"Bike Workshop": 0.0})


def _pool_named(conn, day, payee):
    st = budget.month_status(conn, day)
    return next(e for e in st["buckets"]["fixed"]["envelopes"]
                if e["payee"] == payee)


class TwoPoolsOneMerchantTests(unittest.TestCase):
    """Two pools that claim one merchant's charges: the older pool is
    tried first on every read, whatever order the database returns the
    rows in."""

    def _household(self, first, second):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=500, other_monthly=500,
                     budgeted_income_monthly=4000)
        for payee, made in ((first, dt.date(2026, 3, 2)),
                            (second, dt.date(2026, 2, 9))):
            add_bill(conn, payee, 70.0, bill_type="envelope", frequency=None,
                     interval=1, merchant="corner florist", created_on=made)
        add_txn(conn, dt.date(2026, 7, 6), 35.0, "CORNER FLORIST",
                primary="GENERAL_MERCHANDISE", txn_id="fl-1")
        return conn

    def _used(self, conn):
        st = budget.month_status(conn, dt.date(2026, 7, 15))
        return {e["payee"]: e["used"]
                for e in st["buckets"]["fixed"]["envelopes"]}

    def test_the_older_pool_claims_whichever_row_comes_first(self):
        for first, second in (("Flowers A", "Flowers B"),
                              ("Flowers B", "Flowers A")):
            with self.subTest(saved_first=first):
                conn = self._household(first, second)
                # the pool set up in February is the older one
                self.assertEqual(self._used(conn),
                                 {second: 35.0, first: 0.0})
                # a rewrite moves a row on disk; the order holds
                conn.execute("UPDATE bills SET synced_at = now() "
                             "WHERE payee = %s", (second,))
                self.assertEqual(self._used(conn),
                                 {second: 35.0, first: 0.0})


class ARestoreKeepsTheDayTests(unittest.TestCase):

    def test_a_restored_bill_keeps_its_day_and_an_unknown_one_stays_unknown(
            self):
        src = _household(dt.date(2026, 8, 14))
        self.addCleanup(src.close)
        add_bill(src, "Pet Clinic", 90.0, bill_type="envelope",
                 frequency=None, interval=1)
        src.commit()
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, export.build_zip(src))
        got = {r["payee"]: r["created_on"] for r in dest.execute(
            "SELECT payee, created_on FROM bills")}
        self.assertEqual(got, {"Garden Supply": dt.date(2026, 8, 14),
                               "Pet Clinic": None})

    def test_an_archive_from_before_the_day_lands_none(self):
        import csv
        import io
        import zipfile
        src = _household(dt.date(2026, 8, 14))
        self.addCleanup(src.close)
        src.commit()
        data = export.build_zip(src)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as zin, \
                zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                body = zin.read(info)
                if info.filename.endswith("bills.csv"):
                    rows = list(csv.DictReader(io.StringIO(body.decode())))
                    assert "created_on" in rows[0], "the export writes it"
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, [k for k in rows[0]
                                             if k != "created_on"],
                                       lineterminator="\n")
                    w.writeheader()
                    w.writerows({k: v for k, v in r.items()
                                 if k != "created_on"} for r in rows)
                    body = buf.getvalue().encode()
                zout.writestr(info, body)
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, out.getvalue())
        self.assertIsNone(dest.execute("SELECT created_on FROM bills")
                          .fetchone()["created_on"])


class TheMigrationTests(unittest.TestCase):
    def test_stored_bills_read_unknown_new_ones_take_the_day_and_it_runs_twice(
            self):
        """On a table from before the column, inside one transaction
        rolled back: the stored bills read NULL, the table is not
        rewritten, a row written after takes the day, and a second run
        changes nothing."""
        conn = make_db()
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        conn.close()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        sql = MIGRATION.read_text()
        try:
            with admin.transaction():
                admin.execute("ALTER TABLE bills DROP COLUMN created_on")
                admin.execute(
                    "INSERT INTO bills (tenant_id, id, payee, amount)"
                    " VALUES (%s, 'rec:invented-1', 'Invented 1', -50)",
                    (tid,))
                before = admin.execute(
                    "SELECT pg_relation_filenode('bills') AS f").fetchone()
                admin.execute(sql)
                after = admin.execute(
                    "SELECT pg_relation_filenode('bills') AS f").fetchone()
                self.assertEqual(before["f"], after["f"])
                self.assertIsNone(admin.execute(
                    "SELECT created_on FROM bills WHERE tenant_id = %s",
                    (tid,)).fetchone()["created_on"])
                admin.execute(sql)
                admin.execute(
                    "INSERT INTO bills (tenant_id, id, payee, amount)"
                    " VALUES (%s, 'rec:invented-2', 'Invented 2', -50)",
                    (tid,))
                self.assertIsNotNone(admin.execute(
                    "SELECT created_on FROM bills WHERE tenant_id = %s"
                    " AND id = 'rec:invented-2'", (tid,)).fetchone()[
                        "created_on"])
                nullable = admin.execute(
                    "SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_name = 'bills' AND column_name ="
                    " 'created_on'").fetchone()["is_nullable"]
                self.assertEqual(nullable, "YES")
                raise _Rollback
        except _Rollback:
            pass

    def test_schema_sql_gives_a_fresh_install_the_column(self):
        sql = (migrate.HERE / "schema.sql").read_text()
        table = sql[sql.index("CREATE TABLE IF NOT EXISTS bills ("):]
        table = table[:table.index("\n);")]
        self.assertIn("created_on     DATE DEFAULT CURRENT_DATE", table)


class _Rollback(Exception):
    pass


if __name__ == "__main__":
    unittest.main()
