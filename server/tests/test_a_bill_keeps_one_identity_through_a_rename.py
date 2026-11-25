"""A bill keeps one identity, bills.uid, through a rename: its id is its
payee's slug and a rename rewrites it, so a month frozen before the rename
names the bill by an id the bill no longer has. Every month frozen records
each bill's uid beside its id; a restore keeps it, and an archived bill
whose uid a bill of the household holds is that bill, renamed.

Every name, date and amount here is invented.
"""

import datetime as dt
import unittest

from oikonome.db import migrate, tenancy
from oikonome.engine import bills, budget
from oikonome.sync import export, restore

from .test_the_bills_row_is_one_breakdown import Reads, _parts, _table
from .util import (TEST_DB, _admin_dsn, add_bill, add_txn, freeze_month,
                   make_db, write_config)

MIGRATION = (migrate.HERE / "migrations"
             / "234_a_bill_keeps_one_identity_through_a_rename.sql")


def _uid(conn, payee):
    row = conn.execute("SELECT uid FROM bills WHERE payee = %s",
                       (payee,)).fetchone()
    return str(row["uid"]) if row else None


def _a_month(conn):
    """A household with a budget of its own."""
    write_config(conn, food_monthly=600, other_monthly=400,
                 budgeted_income_monthly=5200)


def _june(conn):
    """June frozen with a yearly bill left unpaid on Jun 29 and the rent
    paid; read in July."""
    _a_month(conn)
    add_bill(conn, "Harbor Lofts", 1475.0, next_due=dt.date(2026, 7, 7),
             last_seen=dt.date(2026, 6, 7))
    add_txn(conn, dt.date(2026, 6, 7), 1475.0, "HARBOR LOFTS",
            account="chk", primary="RENT_AND_UTILITIES")
    add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 29),
             frequency="YEARLY", last_seen=dt.date(2025, 6, 29))
    freeze_month(conn, 2026, 6)


def _rename_and_redate(conn, due):
    assert bills.rename_bill(conn, "Kestrel Power", "Kestrel Energy")
    add_bill(conn, "Kestrel Energy", 135.0, next_due=due,
             frequency="YEARLY", last_seen=dt.date(2025, 6, 29))


class TheStableIdTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_every_bill_has_its_own_uid_and_a_rename_keeps_it(self):
        add_bill(self.conn, "Kestrel Power", 135.0,
                 next_due=dt.date(2026, 6, 29))
        add_bill(self.conn, "Harbor Lofts", 1475.0,
                 next_due=dt.date(2026, 7, 7))
        before = _uid(self.conn, "Kestrel Power")
        self.assertIsNotNone(before)
        self.assertNotEqual(before, _uid(self.conn, "Harbor Lofts"))
        self.assertTrue(bills.rename_bill(self.conn, "Kestrel Power",
                                          "Kestrel Energy"))
        row = self.conn.execute("SELECT id, uid FROM bills WHERE payee = %s",
                                ("Kestrel Energy",)).fetchone()
        self.assertEqual(row["id"], "rec:kestrel-energy")
        self.assertEqual(str(row["uid"]), before)
        # a re-save of the renamed bill keeps it too
        add_bill(self.conn, "Kestrel Energy", 140.0,
                 next_due=dt.date(2026, 7, 2))
        self.assertEqual(_uid(self.conn, "Kestrel Energy"), before)

    def test_the_frozen_schedule_carries_each_bills_uid(self):
        _june(self.conn)
        rows = budget.snapshot_bill_rows(self.conn, 2026, 6)
        self.assertEqual(
            {r["payee"]: r.get("uid") for r in rows},
            {p: _uid(self.conn, p) for p in ("Harbor Lofts", "Kestrel Power")})
        # and the schedule read from that frozen month carries it too
        loaded = budget._recurring_bills_load(self.conn, None, None, False,
                                              rows)
        self.assertEqual({b["payee"]: b["uid"] for b in loaded},
                         {r["payee"]: r["uid"] for r in rows})
        # a row frozen before bills had one reads as having none
        bare = [{k: v for k, v in r.items() if k != "uid"} for r in rows]
        self.assertTrue(all(
            b["uid"] is None for b in budget._recurring_bills_load(
                self.conn, None, None, False, bare)))


class ARenamedBillOnTheYearTests(unittest.TestCase):
    """A bill renamed after its month closed is the same bill on the
    Year: its unpaid June date is counted once."""

    def _year(self, due):
        conn = make_db()
        self.addCleanup(conn.close)
        _june(conn)
        _rename_and_redate(conn, due)
        return Reads(conn, dt.date(2026, 7, 2)).year_row

    def test_a_renamed_bill_late_from_its_month_is_counted_once(self):
        row = self._year(dt.date(2026, 6, 25))
        # this month's rent and the June bill, one still-due figure
        self.assertEqual(row["status"]["text"], "$1,610 still due")
        t = _table(row)
        self.assertEqual(_parts(t)["late"], 135)
        self.assertNotIn("not_paid", _parts(t))
        self.assertEqual(t["outside"], [])

    def test_a_renamed_bill_moved_into_this_month_is_counted_once(self):
        t = _table(self._year(dt.date(2026, 7, 1)))
        self.assertEqual(_parts(t)["moved"], 135)
        self.assertNotIn("not_paid", _parts(t))


class AMonthFrozenBeforeTheUidTests(unittest.TestCase):
    def test_it_is_matched_by_the_id_it_recorded(self):
        """A schedule frozen before bills had a uid names each bill by its
        old id; a late bill re-dated inside its month still matches it."""
        conn = make_db()
        self.addCleanup(conn.close)
        _june(conn)
        conn.execute(
            "UPDATE budget_snapshots SET bills = jsonb_set(bills, '{rows}',"
            " (SELECT jsonb_agg(r - 'uid') FROM jsonb_array_elements("
            "bills->'rows') r)) WHERE year = 2026 AND month = 6")
        self.assertTrue(all(r.get("uid") is None for r in
                            budget.snapshot_bill_rows(conn, 2026, 6)))
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 6, 25),
                 frequency="YEARLY", last_seen=dt.date(2025, 6, 29))
        row = Reads(conn, dt.date(2026, 7, 2)).year_row
        self.assertEqual(row["status"]["text"], "$1,610 still due")
        t = _table(row)
        self.assertEqual(_parts(t)["late"], 135)
        self.assertNotIn("not_paid", _parts(t))
        self.assertEqual(t["outside"], [])


    def test_a_payee_deleted_and_made_again_is_never_read_as_moved(self):
        """Without a uid in the frozen month, a bill deleted and made again
        under the same payee is not known to be the one the month planned:
        its new date is never that month's plan moved into this one, and
        the month's unpaid date stays not paid."""
        conn = make_db()
        self.addCleanup(conn.close)
        _june(conn)
        conn.execute(
            "UPDATE budget_snapshots SET bills = jsonb_set(bills, '{rows}',"
            " (SELECT jsonb_agg(r - 'uid') FROM jsonb_array_elements("
            "bills->'rows') r)) WHERE year = 2026 AND month = 6")
        bills.delete_bill(conn, "Kestrel Power")
        add_bill(conn, "Kestrel Power", 135.0, next_due=dt.date(2026, 7, 1),
                 frequency="YEARLY", last_seen=dt.date(2025, 7, 1))
        yb = Reads(conn, dt.date(2026, 7, 2)).year_row["breakdown"]
        self.assertEqual((yb["moved"], yb["not_paid"]), (0, 135))


class ARestoreKeepsTheUidTests(unittest.TestCase):
    def _source(self):
        src = make_db()
        self.addCleanup(src.close)
        _june(src)
        src.commit()
        return src

    def test_a_restored_bill_keeps_its_archived_uid(self):
        src = self._source()
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, export.build_zip(src))
        for payee in ("Harbor Lofts", "Kestrel Power"):
            self.assertEqual(_uid(dest, payee), _uid(src, payee), payee)
        # and the restored frozen month still names the same bills
        rows = budget.snapshot_bill_rows(dest, 2026, 6)
        self.assertEqual({r["payee"]: r["uid"] for r in rows},
                         {p: _uid(dest, p) for p in ("Harbor Lofts",
                                                     "Kestrel Power")})

    def test_a_bill_renamed_since_the_archive_is_the_same_bill(self):
        """The archive restored after a rename names the bill by its old
        id; its uid says it is the renamed bill, so it is counted present
        and no second bill comes back under the old name."""
        src = self._source()
        data = export.build_zip(src)
        held = _uid(src, "Kestrel Power")
        self.assertTrue(bills.rename_bill(src, "Kestrel Power",
                                          "Kestrel Energy"))
        src.commit()
        restore.restore_zip(src, data)
        self.assertEqual(_uid(src, "Kestrel Energy"), held)
        self.assertIsNone(_uid(src, "Kestrel Power"))
        self.assertEqual(sorted(r["payee"] for r in src.execute(
            "SELECT payee FROM bills")), ["Harbor Lofts", "Kestrel Energy"])


    def test_an_archive_from_before_the_uid_draws_one_per_bill(self):
        src = self._source()
        held = {p: _uid(src, p) for p in ("Harbor Lofts", "Kestrel Power")}
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, _without_bill_uids(export.build_zip(src)))
        got = {p: _uid(dest, p) for p in held}
        self.assertNotIn(None, got.values())
        self.assertEqual(len(set(got.values())), 2)
        self.assertFalse(set(got.values()) & set(held.values()))


def _without_bill_uids(data: bytes) -> bytes:
    """The archive as a build from before bills had a uid wrote it."""
    import csv
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            body = zin.read(info)
            if info.filename.endswith("bills.csv"):
                rows = list(csv.DictReader(io.StringIO(body.decode())))
                assert rows and "uid" in rows[0], "the export writes uid"
                buf = io.StringIO()
                w = csv.DictWriter(buf, [k for k in rows[0] if k != "uid"],
                                   lineterminator="\n")
                w.writeheader()
                w.writerows({k: v for k, v in r.items() if k != "uid"}
                            for r in rows)
                body = buf.getvalue().encode()
            zout.writestr(info, body)
    return out.getvalue()


class TheMigrationTests(unittest.TestCase):
    def test_it_gives_every_stored_bill_its_own_uid_and_runs_twice(self):
        """On a table from before the column: each stored bill draws its
        own uid, new rows draw one, two bills of a household never share
        one, and a second run changes nothing. Inside one transaction,
        rolled back."""
        import psycopg
        conn = make_db()
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        conn.close()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        sql = MIGRATION.read_text()
        try:
            with admin.transaction():
                admin.execute("DROP INDEX IF EXISTS bills_by_uid")
                admin.execute("ALTER TABLE bills DROP COLUMN uid")
                for n in (1, 2):
                    admin.execute(
                        "INSERT INTO bills (tenant_id, id, payee, amount)"
                        " VALUES (%s, %s, %s, -50)",
                        (tid, f"rec:invented-{n}", f"Invented {n}"))
                admin.execute(sql)
                uids = [r["uid"] for r in admin.execute(
                    "SELECT uid FROM bills WHERE tenant_id = %s", (tid,))]
                self.assertEqual(len(uids), 2)
                self.assertNotIn(None, uids)
                self.assertEqual(len(set(uids)), 2)
                admin.execute(sql)
                self.assertEqual(sorted(r["uid"] for r in admin.execute(
                    "SELECT uid FROM bills WHERE tenant_id = %s", (tid,))),
                    sorted(uids))
                admin.execute(
                    "INSERT INTO bills (tenant_id, id, payee, amount)"
                    " VALUES (%s, 'rec:invented-3', 'Invented 3', -50)",
                    (tid,))
                with self.assertRaises(psycopg.errors.UniqueViolation):
                    with admin.transaction():
                        admin.execute(
                            "UPDATE bills SET uid = %s WHERE tenant_id = %s"
                            " AND id = 'rec:invented-3'", (uids[0], tid))
                raise _Rollback
        except _Rollback:
            pass

    def test_schema_sql_gives_a_fresh_install_the_column(self):
        sql = (migrate.HERE / "schema.sql").read_text()
        table = sql[sql.index("CREATE TABLE IF NOT EXISTS bills ("):]
        table = table[:table.index("\n);")]
        self.assertIn("uid            UUID NOT NULL DEFAULT gen_random_uuid()",
                      table)
        # the index is the migration's alone
        self.assertNotIn("bills_by_uid", sql)


class _Rollback(Exception):
    pass


if __name__ == "__main__":
    unittest.main()
