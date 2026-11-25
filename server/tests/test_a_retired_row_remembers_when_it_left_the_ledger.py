"""A retired transaction remembers when it left the ledger.

`transactions.removed_at` is stamped by one trigger whenever a row goes
from live to retired, by any door, and cleared when the row comes back. It
is the clock every "has this row been gone long enough" wait reads: the
receipt release, the withdrawn-pair release and the fronted-cost release.
Before it, the receipt wait was counted from the charge's own date and the
day the receipt was attached, both of which come before the retirement, so
an old charge retired today had its receipt taken off that night.

An explicit value (a writer copying a row whole, an archive) is kept; an archive with none
lands its retired rows with the restore's own time, so they wait a fresh
window; the migration stamps the rows already retired by the same rule.
Every amount and name is invented (specs/receipts.md, "When a row left the
ledger").
"""

import csv
import datetime as dt
import io
import pathlib
import unittest
import zipfile

from oikonome.engine import budget, receipt_match, receipts
from oikonome.sync import adopt, coinbase_push, plan_csv, restore
from oikonome.engine.compat import jsonb

from .util import add_txn, make_db, write_config

MIGRATION = (pathlib.Path(restore.__file__).parent.parent / "db" /
             "migrations" /
             "230_a_retired_row_remembers_when_it_left_the_ledger.sql")

PNG = b"\x89PNG..."
# a charge from well before the reconcile window
OLD = dt.date.today() - dt.timedelta(days=adopt.RECONCILE_DAYS + 40)


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _stamp(self, tid):
        return self.conn.execute(
            "SELECT removed, removed_at FROM transactions WHERE id = %s",
            (tid,)).fetchone()

    def _age(self, tid, days):
        self.conn.execute(
            "UPDATE transactions SET removed_at = now() - "
            "make_interval(days => %s) WHERE id = %s", (days, tid))


class TheStamp(_Base):

    def test_retiring_a_row_stamps_when_and_a_second_retire_keeps_it(self):
        tid = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        self.assertIsNone(self._stamp(tid)["removed_at"])
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (tid,))
        first = self._stamp(tid)["removed_at"]
        self.assertIsNotNone(first)
        self._age(tid, 3)
        aged = self._stamp(tid)["removed_at"]
        # a door re-retiring a row already gone does not restart its wait
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (tid,))
        self.assertEqual(self._stamp(tid)["removed_at"], aged)

    def test_a_row_coming_back_live_forgets_when_it_was_retired(self):
        tid = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (tid,))
        self.conn.execute("UPDATE transactions SET removed = 0 WHERE id = %s",
                          (tid,))
        self.assertIsNone(self._stamp(tid)["removed_at"])
        # nor can anything leave a time on a live row
        self.conn.execute("UPDATE transactions SET removed_at = now() "
                          "WHERE id = %s", (tid,))
        self.assertIsNone(self._stamp(tid)["removed_at"])

    def test_retiring_a_row_again_restarts_its_wait(self):
        tid = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (tid,))
        self._age(tid, 30)
        self.conn.execute("UPDATE transactions SET removed = 0 WHERE id = %s",
                          (tid,))
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (tid,))
        fresh = self.conn.execute(
            "SELECT removed_at > now() - interval '1 minute' AS fresh "
            "FROM transactions WHERE id = %s", (tid,)).fetchone()["fresh"]
        self.assertTrue(fresh)

    def test_an_explicit_removed_at_from_a_whole_row_copy_or_restore_is_kept(self):
        tid = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        when = dt.datetime(2026, 3, 4, 5, 6, tzinfo=dt.timezone.utc)
        # a writer that copies a row whole writes its time with it
        self.conn.execute("UPDATE transactions SET removed = 1, "
                          "removed_at = %s WHERE id = %s", (when, tid))
        self.assertEqual(self._stamp(tid)["removed_at"], when)

    def test_a_row_inserted_retired_is_stamped_unless_it_says_when(self):
        when = dt.datetime(2026, 3, 4, 5, 6, tzinfo=dt.timezone.utc)
        for tid, removed, at in (("ins-gone", 1, None), ("ins-said", 1, when),
                                 ("ins-live", 0, when)):
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, "
                "name, pending, removed, raw, removed_at) VALUES "
                "(%s, 'card', %s, 25, 'TAILSPIN BAKERY', 0, %s, '{}', %s)",
                (tid, OLD, removed, at))
        fresh = self.conn.execute(
            "SELECT removed_at > now() - interval '1 minute' AS f "
            "FROM transactions WHERE id = 'ins-gone'").fetchone()["f"]
        self.assertTrue(fresh)
        self.assertEqual(self._stamp("ins-said")["removed_at"], when)
        self.assertIsNone(self._stamp("ins-live")["removed_at"])
        # an upsert that finds the row live and retires it is an update
        tid = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed, raw) VALUES (%s, 'card', %s, 25, 'X', 0, 1, "
            "'{}') ON CONFLICT (tenant_id, id) DO UPDATE SET removed = 1",
            (tid, OLD))
        self.assertIsNotNone(self._stamp(tid)["removed_at"])

    def test_the_dedups_undo_leaves_a_live_row_unstamped(self):
        lose = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        keep = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        # the survivor stopped being live after the scan chose it
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (keep,))
        adopt._retire_twin(self.conn, keep, lose)
        row = self._stamp(lose)
        self.assertEqual((row["removed"], row["removed_at"]), (0, None))


class TheMigration(_Base):

    _n = 0

    def _retired_without_a_time(self, raw, retired_at=None):
        # what a row retired before the column existed looks like: an
        # explicit NULL on a row already retired is kept
        TheMigration._n += 1
        tid = f"pre-{TheMigration._n}"
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " pending, removed, raw, retired_at)"
            " VALUES (%s, 'card', %s, 25, 'TAILSPIN BAKERY', 0, 1, %s, %s)",
            (tid, OLD, jsonb(raw), retired_at))
        self.conn.execute("UPDATE transactions SET removed_at = NULL "
                          "WHERE id = %s", (tid,))
        return tid

    def test_the_retired_at_migration_keeps_the_feeds_own_stamp(self):
        feed = self._retired_without_a_time(
            {"feed_removed_at": "2026-01-02T03:04:05+00:00"})
        decided = self._retired_without_a_time(
            {}, retired_at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        garbled = self._retired_without_a_time({"feed_removed_at": "yes"})
        unknown = self._retired_without_a_time({})
        live = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        sql = MIGRATION.read_text()
        self.conn.execute(sql)
        self.assertEqual(self._stamp(feed)["removed_at"],
                         dt.datetime(2026, 1, 2, 3, 4, 5,
                                     tzinfo=dt.timezone.utc))
        self.assertEqual(self._stamp(decided)["removed_at"],
                         dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        for tid in (garbled, unknown):
            fresh = self.conn.execute(
                "SELECT removed_at > now() - interval '1 minute' AS f "
                "FROM transactions WHERE id = %s", (tid,)).fetchone()["f"]
            self.assertTrue(fresh, tid)
        self.assertIsNone(self._stamp(live)["removed_at"])
        # a second run writes nothing
        self.assertEqual(self.conn.execute(sql).rowcount, 0)


def _receipt_on(conn, tid):
    """A slip attached from the charge itself, long ago."""
    rid = receipts.add(conn, tid, PNG, "image/png")
    conn.execute("UPDATE receipts SET created_at = now() - interval '60 days'"
                 " WHERE id = %s::uuid", (rid,))
    return rid


def _on(conn, rid):
    return conn.execute("SELECT txn_id FROM receipts WHERE id = %s::uuid",
                        (rid,)).fetchone()["txn_id"]


class AnOldChargeRetiredTodayKeepsItsReceipt(_Base):
    """Each door that retires a row: the receipt waits the reconcile window
    counted from the retirement, then goes back to waiting."""

    def _assert_waits_then_released(self, tid, rid):
        self.assertEqual(self._stamp(tid)["removed"], 1)
        self.assertEqual(receipt_match.release_unplaced(self.conn), 0)
        self.assertEqual(_on(self.conn, rid), tid)
        self._age(tid, adopt.RECONCILE_DAYS + 1)
        self.assertEqual(receipt_match.release_unplaced(self.conn), 1)
        self.assertIsNone(_on(self.conn, rid))

    def test_an_old_charge_retired_today_keeps_its_receipt_for_the_reconcile_window(self):
        tid = add_txn(self.conn, OLD, 25.0, "TAILSPIN BAKERY")
        rid = _receipt_on(self.conn, tid)
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (tid,))
        self._assert_waits_then_released(tid, rid)

    def test_by_a_plan_csv_restatement(self):
        head = ('"Trade Date","Investments","Ticker","Transaction",'
                '"Transaction Amount","Share Price","Total shares"\n')
        day = OLD.strftime("%m/%d/%Y")
        both = head + "".join(
            f'"{day}","Fund {f}","FUND{f}","Contribution","{a}","10.00",'
            f'"{s}"\n' for f, a, s in (("A", "100.00", "10.000"),
                                       ("B", "200.00", "20.000")))
        only_a = head + (f'"{day}","Fund A","FUNDA","Contribution",'
                         '"100.00","10.00","10.000"\n')
        plan_csv.import_csv(self.conn, "403B", both)
        tid = self.conn.execute(
            "SELECT id FROM transactions WHERE raw->>'fund' = 'Fund B'"
        ).fetchone()["id"]
        rid = _receipt_on(self.conn, tid)
        plan_csv.import_csv(self.conn, "403B", only_a)
        self._assert_waits_then_released(tid, rid)

    def test_by_a_coinbase_full_replace_push(self):
        push = {"items": [{"id": "coinbase-test",
                           "institution_name": "Coinbase"}],
                "accounts": [{"id": "coinbase-test",
                              "item_id": "coinbase-test",
                              "name": "Coinbase (test)", "type": "investment",
                              "subtype": "crypto", "balance_current": 0}],
                "full_replace": True,
                "transactions": [{"id": "coinbase:fee-1",
                                  "account_id": "coinbase-test",
                                  "date": OLD.isoformat(), "amount": 25.0,
                                  "name": "NETWORK FEE"}]}
        coinbase_push.import_payload(self.conn, push)
        rid = _receipt_on(self.conn, "coinbase:fee-1")
        coinbase_push.import_payload(self.conn, dict(push, transactions=[]))
        self._assert_waits_then_released("coinbase:fee-1", rid)


class WithdrawnPairsWaitFromTheRetirement(_Base):

    def test_withdrawn_pairs_are_released_by_when_the_row_was_retired(self):
        from oikonome.sync import base
        from oikonome.web import data as d
        charge = add_txn(self.conn, OLD, 120.0, "GARDEN CENTER")
        refund = add_txn(self.conn, OLD, -120.0, "GARDEN CENTER REFUND",
                         account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, charge, refund)
        self.assertEqual(base.mark_removed(self.conn, [refund]), 1)
        # the feed's own stamp says long ago; the retirement was today
        self.conn.execute(
            "UPDATE transactions SET raw = raw || jsonb_build_object("
            "%s::text, (now() - interval '90 days')::text) WHERE id = %s",
            (budget.FEED_REMOVED_KEY, refund))
        self.assertEqual(budget.release_withdrawn_pairs(
            self.conn, [refund], older_than_days=adopt.RECONCILE_DAYS), 0)
        self._age(refund, adopt.RECONCILE_DAYS + 1)
        self.assertEqual(budget.release_withdrawn_pairs(
            self.conn, [refund], older_than_days=adopt.RECONCILE_DAYS), 1)


class AnArchiveWithoutRemovedAt(_Base):

    def _restore(self, rows):
        header = ["id", "account_id", "date", "amount", "name", "pending",
                  "removed", "raw", "removed_at"]
        sio = io.StringIO()
        w = csv.DictWriter(sio, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("transactions.csv", sio.getvalue())
        restore.restore_zip(self.conn, buf.getvalue())

    def test_an_archive_without_removed_at_restores_rows_that_wait_a_fresh_window(self):
        kept = "2026-03-04T05:06:00+00:00"
        self._restore([
            {"id": "arch-gone", "account_id": "card",
             "date": OLD.isoformat(), "amount": "25", "name": "TAILSPIN",
             "pending": "0", "removed": "1", "raw": "{}", "removed_at": ""},
            {"id": "arch-kept", "account_id": "card",
             "date": OLD.isoformat(), "amount": "25", "name": "TAILSPIN",
             "pending": "0", "removed": "1", "raw": "{}",
             "removed_at": kept},
            {"id": "arch-live", "account_id": "card",
             "date": OLD.isoformat(), "amount": "25", "name": "TAILSPIN",
             "pending": "0", "removed": "0", "raw": "{}",
             "removed_at": kept}])
        fresh = self.conn.execute(
            "SELECT removed_at > now() - interval '1 minute' AS f "
            "FROM transactions WHERE id = 'arch-gone'").fetchone()["f"]
        self.assertTrue(fresh)
        self.assertEqual(self._stamp("arch-kept")["removed_at"],
                         dt.datetime(2026, 3, 4, 5, 6,
                                     tzinfo=dt.timezone.utc))
        self.assertIsNone(self._stamp("arch-live")["removed_at"])


if __name__ == "__main__":
    unittest.main()
