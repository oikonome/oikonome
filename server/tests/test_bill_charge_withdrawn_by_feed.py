"""A bill whose charge the bank feed withdrew.

A feed can take back rows it already posted (a "removed" delta). The bill
was charged, but the ledger no longer shows it, so the occurrence reads as
missed. When no live row pays the occurrence and a withdrawn POSTED row
would have, every surface says "withdrawn by the bank feed" (a warning)
instead of "overdue" — while the money stays exactly as unpaid as before,
because the charge is not in the ledger either way. A live matching charge
settles it; a withdrawal older than the occurrence's match window falls
back to overdue, because that charge is not coming back.
"""

import datetime as dt
import unittest
from pathlib import Path

from oikonome.engine import bills, budget
from oikonome.sync import adopt, base, coinbase_push, plan_csv
from oikonome.web import report, todayview
from oikonome.web.api import _month_bill_stats

from .util import TODAY, add_bill, add_txn, make_db, write_config

DUE = dt.date(2026, 7, 8)


def _withdraw(conn, txn_id, *, pending=0):
    """The bank feed's removal delta, as the Plaid sync applies it."""
    conn.execute("UPDATE transactions SET pending=%s WHERE id=%s",
                 (pending, txn_id))
    base.mark_removed(conn, [txn_id])


class WithdrawnChargeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "City Power", 100.0, next_due=DUE,
                 last_seen=DUE - dt.timedelta(days=31), show_today=True)
        self.gone = add_txn(self.conn, DUE, 100.0, "CITY POWER")
        _withdraw(self.conn, self.gone)

    def tearDown(self):
        self.conn.close()

    def _entry(self, today=TODAY):
        st = budget.month_status(self.conn, today)
        rows = [o for o in st["overdue_unpaid"] if o[0] == "City Power"]
        return st, rows

    def test_only_a_withdrawn_charge_reads_withdrawn_and_stays_unpaid(self):
        st, rows = self._entry()
        self.assertEqual(rows, [("City Power", 100.0, DUE.isoformat(),
                                 DUE.isoformat())])
        # the wording changes; the money does not
        self.assertAlmostEqual(st["buckets"]["fixed"]["unpaid_due"], 100.0)
        self.assertAlmostEqual(st["buckets"]["fixed"]["actual"], 0.0)
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 TODAY)["City Power"]
        self.assertEqual(page["paid"], 0)
        self.assertEqual(page["next_unpaid"], DUE.isoformat())
        self.assertEqual(page["withdrawn_on"], DUE.isoformat())

    def test_a_live_matching_charge_settles_it(self):
        add_txn(self.conn, DUE + dt.timedelta(days=1), 100.0, "CITY POWER")
        st, rows = self._entry()
        self.assertEqual(rows, [])
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 TODAY)["City Power"]
        self.assertEqual(page["paid"], 1)
        self.assertIsNone(page["withdrawn_on"])

    def test_a_removed_pending_row_is_not_a_withdrawn_charge(self):
        # a pending row removed when it posts is the ordinary swap
        _withdraw(self.conn, self.gone, pending=1)
        _, rows = self._entry()
        self.assertEqual(rows, [("City Power", 100.0, DUE.isoformat(), None)])

    def test_past_the_match_window_it_reads_overdue_again(self):
        late = DUE + dt.timedelta(days=17)      # monthly window is 15 days
        _, rows = self._entry(late)
        self.assertEqual(rows, [("City Power", 100.0, DUE.isoformat(), None)])
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 late)["City Power"]
        self.assertIsNone(page["withdrawn_on"])

    def test_pinned_card_warns_instead_of_overdue(self):
        d = report.gather(self.conn, TODAY)
        card = next(c for c in todayview._bill_cards(d)
                    if c["label"] == "City Power")
        self.assertEqual(card["status"],
                         "charge withdrawn by the bank feed (07/08)")
        self.assertEqual(card["status_tone"], "warn")
        self.assertFalse(card["over"])

    def test_daily_email_carries_the_withdrawn_line(self):
        d = report.gather(self.conn, TODAY)
        _, plain, html = report.build(d)
        self.assertIn("withdrawn by the bank feed: City Power $100 (07/08/26)",
                      plain)
        self.assertNotIn("Scheduled but not yet posted", plain)
        self.assertIn("withdrawn by the bank feed: City Power $100 (07/08/26)",
                      html)
        self.assertNotIn("Scheduled but not yet posted", html)

    def test_a_never_posted_bill_keeps_the_scheduled_line(self):
        self.conn.execute("DELETE FROM transactions WHERE id=%s", (self.gone,))
        d = report.gather(self.conn, TODAY)
        _, plain, html = report.build(d)
        self.assertIn("Scheduled but not yet posted: City Power $100", plain)
        self.assertNotIn("withdrawn by the bank feed", plain)
        self.assertNotIn("withdrawn by the bank feed", html)

    def test_an_excluded_accounts_withdrawn_charge_counts_nowhere(self):
        # the "excl" toggle drops the account's rows from every reader —
        # Today's line, the Bills row and the Bills health evidence alike
        write_config(self.conn, excluded_accounts=["card"])
        _, rows = self._entry()
        self.assertEqual(rows, [("City Power", 100.0, DUE.isoformat(), None)])
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 TODAY)["City Power"]
        self.assertIsNone(page["withdrawn_on"])
        self.assertEqual(bills._withdrawn_rows(self.conn, TODAY), [])

    def test_a_malformed_exclusion_setting_reads_as_nothing_excluded(self):
        write_config(self.conn, excluded_accounts=True)
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 TODAY)["City Power"]
        self.assertEqual(page["withdrawn_on"], DUE.isoformat())


class RetiredIsNotWithdrawnTests(unittest.TestCase):
    """Only the aggregator's removal delta is a withdrawal. A row retired
    because two sources were merged, a plan file restated its dates or an
    exchange's full re-sync no longer carried it was never taken back by
    the bank, and the bill it would have paid reads overdue."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "City Power", 100.0, next_due=DUE,
                 last_seen=DUE - dt.timedelta(days=31), show_today=True)

    def tearDown(self):
        self.conn.close()

    def _reads(self):
        st = budget.month_status(self.conn, TODAY)
        line = [o for o in st["overdue_unpaid"] if o[0] == "City Power"]
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 TODAY)["City Power"]
        return line, page["withdrawn_on"]

    def _assert_overdue_not_withdrawn(self):
        line, page = self._reads()
        self.assertEqual(line, [("City Power", 100.0, DUE.isoformat(), None)])
        self.assertIsNone(page)

    def test_the_feeds_removal_delta_reads_withdrawn(self):
        _withdraw(self.conn, add_txn(self.conn, DUE, 100.0, "CITY POWER"))
        line, page = self._reads()
        self.assertEqual(line, [("City Power", 100.0, DUE.isoformat(),
                                 DUE.isoformat())])
        self.assertEqual(page, DUE.isoformat())

    def test_a_donor_retired_by_a_merge_is_not_withdrawn(self):
        # the survivor sits on an account the budget excludes, so nothing
        # live pays the bill; the retired twin must not vouch for it
        write_config(self.conn, excluded_accounts=["chk"])
        keep = add_txn(self.conn, DUE, 100.0, "CITY POWER", account="chk")
        lose = add_txn(self.conn, DUE, 100.0, "CITY POWER")
        adopt._retire_twin(self.conn, keep, lose)
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id=%s",
            (lose,)).fetchone()["removed"], 1)
        self._assert_overdue_not_withdrawn()

    def test_a_removal_delta_naming_an_already_retired_row_is_not_withdrawn(self):
        # a merge retired the row first; the feed later naming its id in a
        # removal delta does not make the merge the bank's withdrawal
        write_config(self.conn, excluded_accounts=["chk"])
        keep = add_txn(self.conn, DUE, 100.0, "CITY POWER", account="chk")
        lose = add_txn(self.conn, DUE, 100.0, "CITY POWER")
        adopt._retire_twin(self.conn, keep, lose)
        base.mark_removed(self.conn, [lose])
        self.assertFalse(self.conn.execute(
            "SELECT raw ? %s AS stamped FROM transactions WHERE id=%s",
            (budget.FEED_REMOVED_KEY, lose)).fetchone()["stamped"])
        self._assert_overdue_not_withdrawn()

    def test_a_plan_file_restate_is_not_withdrawn(self):
        csv = ('"Trade Date","Investments","Ticker","Transaction",'
               '"Transaction Amount","Share Price","Total Shares"\n'
               f'"{DUE:%m/%d/%Y}","Fund A","FUNDA","Contribution",'
               '"50.00","10.00","5.000"\n')
        plan_csv.import_csv(self.conn, "401K", csv)
        aid = plan_csv.account_id(plan_csv.DEFAULT_PROVIDER, "401K")
        # a bill-shaped row the door wrote on a spending account type, so
        # only the retirement's kind decides the reading
        self.conn.execute("UPDATE accounts SET type='credit' WHERE id=%s",
                          (aid,))
        gone = add_txn(self.conn, DUE, 100.0, "CITY POWER", account=aid,
                       txn_id=f"{plan_csv.DEFAULT_PROVIDER}:city-power")
        plan_csv.import_csv(self.conn, "401K", csv)
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id=%s",
            (gone,)).fetchone()["removed"], 1)
        self._assert_overdue_not_withdrawn()

    def test_an_exchange_full_replace_is_not_withdrawn(self):
        acct = {"id": "coinbase-test", "item_id": "coinbase-test",
                "name": "Coinbase (test)", "type": "credit",
                "subtype": "credit card", "balance_current": 0,
                "balance_available": None}
        push = {"items": [{"id": "coinbase-test",
                           "institution_name": "Coinbase"}],
                "accounts": [acct], "full_replace": True,
                "transactions": [{"id": "coinbase:city-power",
                                  "account_id": "coinbase-test",
                                  "date": DUE.isoformat(), "amount": 100.0,
                                  "name": "CITY POWER"}]}
        coinbase_push.import_payload(self.conn, push)
        coinbase_push.import_payload(self.conn, dict(push, transactions=[]))
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id='coinbase:city-power'"
        ).fetchone()["removed"], 1)
        self._assert_overdue_not_withdrawn()


class LastMonthsTwinTests(unittest.TestCase):
    """A withdrawn twin of LAST month's live payment binds to last month's
    occurrence, not to this month's unpaid one."""

    def test_a_twin_of_last_months_payment_does_not_vouch_for_this_month(self):
        conn = make_db()
        try:
            write_config(conn)
            # due the 1st; February's bill was paid live on the 13th, and the
            # feed re-sent it a day later and then withdrew that copy. The
            # copy is within March 1st's match window, but nearer February's.
            add_bill(conn, "City Power", 100.0, next_due=dt.date(2026, 3, 1),
                     last_seen=dt.date(2026, 2, 13))
            add_txn(conn, dt.date(2026, 2, 13), 100.0, "CITY POWER")
            _withdraw(conn, add_txn(conn, dt.date(2026, 2, 14), 100.0,
                                    "CITY POWER"))
            today = dt.date(2026, 3, 1)
            st = budget.month_status(conn, today)
            self.assertEqual(
                [o for o in st["overdue_unpaid"] if o[0] == "City Power"],
                [("City Power", 100.0, "2026-03-01", None)])
            page = _month_bill_stats(conn, budget.load_config(conn),
                                     today)["City Power"]
            self.assertIsNone(page["withdrawn_on"])
        finally:
            conn.close()


class OverdueEntriesTests(unittest.TestCase):
    def test_a_three_field_entry_reads_as_never_posted(self):
        # a status stored or mirrored before the withdrawn slot existed
        st = {"overdue_unpaid": [["Water", 120.0, "2026-07-08"],
                                 ["Power", 100.0, "2026-07-08",
                                  "2026-07-08"]]}
        self.assertEqual(budget.overdue_entries(st),
                         [("Water", 120.0, "2026-07-08", None),
                          ("Power", 100.0, "2026-07-08", "2026-07-08")])
        self.assertEqual(budget.overdue_entries({}), [])

    def test_the_email_builds_from_a_three_field_status(self):
        conn = make_db()
        try:
            write_config(conn)
            add_bill(conn, "City Power", 100.0, next_due=DUE,
                     last_seen=DUE - dt.timedelta(days=31))
            d = report.gather(conn, TODAY)
        finally:
            conn.close()
        d["overdue_unpaid"] = [tuple(o[:3]) for o in d["overdue_unpaid"]]
        self.assertTrue(d["overdue_unpaid"])
        _, plain, _ = report.build(d)
        self.assertIn("Scheduled but not yet posted: City Power $100", plain)


class WithdrawnEvidenceMemoTests(unittest.TestCase):
    def test_a_removal_write_drops_the_remembered_evidence(self):
        """The checkout memo remembers the withdrawn rows like any ledger
        read; the removal delta writes `transactions`, so the next read on
        the same checkout must see the charge it just took back."""
        from oikonome.db import tenancy
        seed = make_db()
        try:
            tid = seed.execute(
                "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
            write_config(seed)
            txn = add_txn(seed, DUE, 100.0, "CITY POWER")
        finally:
            seed.close()
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
            since, until = DUE - dt.timedelta(days=5), TODAY
            self.assertEqual(budget.withdrawn_rows(conn, since, until, cfg), [])
            base.mark_removed(conn, [txn])
            self.assertEqual(
                [r["txn_id"] for r in
                 budget.withdrawn_rows(conn, since, until, cfg)], [txn])
        finally:
            conn.close()


class WithdrawnBeforeTheStampTests(unittest.TestCase):
    """Rows the feed's removal delta retired before the delta stamped them
    are recognised once, by a migration: posted, retired with no decision
    behind it, carrying the aggregator's own id on an aggregator
    connection, and with no live copy of the same charge beside it. Rows
    retired any other way stay unstamped, so the reader stays exact."""

    MIGRATION = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
                 / "migrations" / "157_withdrawn_charges_before_the_stamp.sql")

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "City Power", 100.0, next_due=DUE,
                 last_seen=DUE - dt.timedelta(days=31), show_today=True)
        self.conn.execute("UPDATE items SET aggregator='plaid' WHERE id='it1'")

    def tearDown(self):
        self.conn.close()

    def _retired(self, txn_id, *, pending=0, by_hand=False):
        self.conn.execute(
            "UPDATE transactions SET removed=1, pending=%s, retired_at=%s"
            " WHERE id=%s",
            (pending, dt.datetime.now(dt.timezone.utc) if by_hand else None,
             txn_id))
        return txn_id

    def _stamped(self):
        return sorted(r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE raw ? %s",
            (budget.FEED_REMOVED_KEY,)).fetchall())

    def test_a_feed_withdrawal_from_before_the_stamp_reads_withdrawn(self):
        gone = self._retired(add_txn(self.conn, DUE, 100.0, "CITY POWER",
                                     txn_id="LegacyFeedId01"))
        # retired some other way: none of these is the bank's withdrawal
        self._retired(add_txn(self.conn, DUE, 40.0, "HOLD", pending=1,
                              txn_id="LegacyHoldId02"), pending=1)
        self._retired(add_txn(self.conn, DUE, 41.0, "STUCK",
                              txn_id="LegacyHandId03"), by_hand=True)
        self._retired(add_txn(self.conn, DUE, 42.0, "FILE ROW",
                              txn_id="csv:legacy-04"))
        add_txn(self.conn, DUE, 43.0, "TWIN", txn_id="LegacyLiveId05")
        self._retired(add_txn(self.conn, DUE, 43.0, "TWIN",
                              txn_id="LegacyTwinId06"))
        self.conn.execute(self.MIGRATION.read_text())
        self.assertEqual(self._stamped(), [gone])
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(
            [o for o in st["overdue_unpaid"] if o[0] == "City Power"],
            [("City Power", 100.0, DUE.isoformat(), DUE.isoformat())])
        # running it again changes nothing
        self.conn.execute(self.MIGRATION.read_text())
        self.assertEqual(self._stamped(), [gone])

    def test_a_restored_archive_from_before_the_stamp_reads_withdrawn(self):
        import csv
        import io
        import zipfile
        from oikonome.sync import restore
        cols = ["id", "account_id", "date", "amount", "name",
                "merchant_name", "pending", "removed", "raw"]
        rows = [{"id": "LegacyFeedId01", "account_id": "card",
                 "date": DUE.isoformat(), "amount": "100.00",
                 "name": "CITY POWER", "merchant_name": "CITY POWER",
                 "pending": "0", "removed": "1", "raw": "{}"},
                {"id": "csv:legacy-04", "account_id": "card",
                 "date": DUE.isoformat(), "amount": "42.00",
                 "name": "FILE ROW", "pending": "0", "removed": "1",
                 "raw": "{}"}]
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("transactions.csv", s.getvalue())
        restore.restore_zip(self.conn, buf.getvalue())
        self.assertEqual(self._stamped(), ["LegacyFeedId01"])
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(
            [o for o in st["overdue_unpaid"] if o[0] == "City Power"],
            [("City Power", 100.0, DUE.isoformat(), DUE.isoformat())])

    def test_rows_on_a_connection_without_a_removal_delta_are_left(self):
        self.conn.execute("UPDATE items SET aggregator='test' WHERE id='it1'")
        self._retired(add_txn(self.conn, DUE, 100.0, "CITY POWER",
                              txn_id="LegacyFeedId01"))
        self.conn.execute(self.MIGRATION.read_text())
        self.assertEqual(self._stamped(), [])


if __name__ == "__main__":
    unittest.main()
