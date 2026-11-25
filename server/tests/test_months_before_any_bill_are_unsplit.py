"""Spend in months no bill schedule reached is shown apart, not as
variable.

Fixed is what the household's bills matched, so before its first bill a
month's whole spend read as variable, and the longer the window the more
"variable" swelled while "fixed" shrank. The picture now draws three
bands: fixed, the variable spend of the months a schedule reached, and
the months none did, "unsplit", with a caption. `variable` keeps its old
meaning (every dollar not fixed) for installed clients."""

import datetime as dt
import unittest

from oikonome.engine import bills, reporting

from .util import add_bill, add_txn, freeze_month, make_db, write_config

TODAY = dt.date(2026, 11, 17)


def _months(first: dt.date, last: dt.date):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield dt.date(y, m, 1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class RentFromLastNovember(unittest.TestCase):
    """$500 of groceries every month from Nov 2023; rent of $1,500 a
    month, a bill, charged from Nov 2025; pay of $4,000 into checking."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for d in _months(dt.date(2023, 11, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=12), 500.0, "GROCER")
            add_txn(self.conn, d.replace(day=2), -4000.0, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
            if d >= dt.date(2025, 11, 1):
                add_txn(self.conn, d.replace(day=1), 1500.0,
                        "FABRIKAM PROPERTIES", account="chk")
        add_bill(self.conn, "FABRIKAM PROPERTIES", 1500.0,
                 next_due=dt.date(2026, 12, 1))

    def tearDown(self):
        self.conn.close()

    def test_a_long_window_draws_the_months_before_any_bill_apart(self):
        f = reporting.flow_breakdown(self.conn, TODAY, "3y")
        out = f["out"]
        self.assertEqual(out["total"], 36 * 500.0 + 12 * 1500.0)
        self.assertEqual(out["fixed"], 12 * 1500.0)
        # the old field keeps its meaning
        self.assertEqual(out["variable"], 36 * 500.0)
        self.assertEqual(out["variable_scheduled"], 12 * 500.0)
        self.assertEqual(out["unsplit"], 24 * 500.0)
        self.assertEqual(out["unsplit_text"],
                         "$12,000 went out in 24 months before your "
                         "first bill — not split into fixed and "
                         "variable. In the other 12 months the charges your "
                         "bills matched are 75% of what went out.")
        shown = {n["key"]: n["dollars"] for n in out["split_shown"]}
        self.assertEqual((shown["fixed"], shown["variable"],
                          shown["unsplit"]), (18000, 6000, 12000))
        # the older clients' nodes are unchanged
        self.assertNotIn("unsplit", {n["key"] for n in out["shown"]})

    def test_a_window_the_schedule_covers_has_nothing_unsplit(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "1y")["out"]
        self.assertEqual(out["unsplit"], 0.0)
        self.assertIsNone(out["unsplit_text"])
        self.assertEqual(out["variable_scheduled"], out["variable"])


class AnEnvelopeCapIsNotAPlanForEveryMonth(RentFromLastNovember):
    """The same household with a cap-only envelope for fuel, used from
    Nov 2025. Its monthly share is today's cap, carried into every month
    the ledger has; it reaches a month only by being used there. Counted
    as a plan, it made every month read as scheduled, so nothing was ever
    unsplit and two years of groceries read as variable."""

    def setUp(self):
        super().setUp()
        for d in _months(dt.date(2025, 11, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=20), 60.0, "CONTOSO FUEL",
                    account="chk")
        add_bill(self.conn, "CONTOSO FUEL", 100.0, bill_type="envelope",
                 next_due=dt.date(2026, 12, 1))

    def test_a_long_window_draws_the_months_before_any_bill_apart(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(out["fixed"], 12 * (1500.0 + 60.0))
        self.assertEqual(out["unsplit"], 24 * 500.0)
        self.assertEqual(out["variable_scheduled"], 12 * 500.0)
        self.assertEqual(out["unsplit_text"],
                         "$12,000 went out in 24 months before your "
                         "first bill — not split into fixed and "
                         "variable. In the other 12 months the charges your "
                         "bills matched are 76% of what went out.")

    def test_a_month_before_every_bills_start_is_unsplit_even_when_a_charge_matches(self):
        # one fill-up at the envelope's merchant in May 2024, long before
        # its run began: May is not split by it
        add_txn(self.conn, dt.date(2024, 5, 20), 60.0, "CONTOSO FUEL",
                account="chk")
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(out["fixed"], 12 * (1500.0 + 60.0))
        self.assertEqual(out["unsplit"], 24 * 500.0 + 60.0)
        self.assertEqual(out["variable_scheduled"], 12 * 500.0)

    def test_the_split_adds_up_to_what_went_out_with_pre_start_matches(self):
        add_txn(self.conn, dt.date(2024, 5, 20), 60.0, "CONTOSO FUEL",
                account="chk")
        add_txn(self.conn, dt.date(2024, 7, 1), 1500.0,
                "FABRIKAM PROPERTIES", account="chk")
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertAlmostEqual(
            out["fixed"] + out["variable_scheduled"] + out["unsplit"],
            out["total"], places=2)
        self.assertEqual(out["unsplit"], 24 * 500.0 + 60.0 + 1500.0)


def _since(conn, payee):
    return conn.execute("SELECT raw->>'scheduled_since' AS s FROM bills "
                        "WHERE payee = %s", (payee,)).fetchone()["s"]


def _set_since(conn, payee, value):
    conn.execute(
        "UPDATE bills SET raw = raw || jsonb_build_object("
        "'scheduled_since', %s::text) WHERE payee = %s", (value, payee))


class EachBillStartsAtItsFirstPayment(RentFromLastNovember):
    """The start is stored on the bill (raw.scheduled_since): the first
    payment ever matched, kept through every edit."""

    def test_a_new_bill_starts_at_its_current_run_of_payments(self):
        self.assertEqual(_since(self.conn, "FABRIKAM PROPERTIES"),
                         "2025-11-01")

    def test_a_bill_reaches_months_from_its_scheduled_since(self):
        _set_since(self.conn, "FABRIKAM PROPERTIES", "2026-05-01")
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        # November to April were paid, but the bill had not started
        self.assertEqual(out["fixed"], 6 * 1500.0)
        self.assertEqual(out["unsplit"], 30 * 500.0 + 6 * 1500.0)
        self.assertEqual(out["variable_scheduled"], 6 * 500.0)

    def test_cash_flow_cache_misses_when_a_bills_start_moves(self):
        before = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(before["unsplit"], 24 * 500.0)
        _set_since(self.conn, "FABRIKAM PROPERTIES", "2026-05-01")
        after = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(after["unsplit"], 30 * 500.0 + 6 * 1500.0)

    def test_a_frozen_months_bills_reach_it_whatever_their_start(self):
        _set_since(self.conn, "FABRIKAM PROPERTIES", "2026-05-01")
        # January 2026 was budgeted under this bill: its frozen schedule
        # reaches it
        freeze_month(self.conn, 2026, 1)
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(out["fixed"], 7 * 1500.0)
        self.assertEqual(out["unsplit"], 29 * 500.0 + 5 * 1500.0)

    def test_editing_a_bill_keeps_its_scheduled_since(self):
        _set_since(self.conn, "FABRIKAM PROPERTIES", "2025-12-01")
        add_bill(self.conn, "FABRIKAM PROPERTIES", 1550.0,
                 next_due=dt.date(2026, 12, 1))
        self.assertEqual(_since(self.conn, "FABRIKAM PROPERTIES"),
                         "2025-12-01")

    def test_the_fill_never_moves_a_start_later_and_a_second_run_changes_nothing(self):
        self.conn.execute("UPDATE bills SET raw = raw - 'scheduled_since'")
        self.assertEqual(bills.fill_scheduled_since(self.conn, today=TODAY), 1)
        self.assertEqual(_since(self.conn, "FABRIKAM PROPERTIES"),
                         "2025-11-01")
        self.assertEqual(bills.fill_scheduled_since(self.conn, today=TODAY), 0)
        # an earlier start than the ledger shows (a person's, or a history
        # since trimmed) is kept
        _set_since(self.conn, "FABRIKAM PROPERTIES", "2025-06-01")
        self.assertEqual(bills.fill_scheduled_since(self.conn, today=TODAY), 0)
        self.assertEqual(_since(self.conn, "FABRIKAM PROPERTIES"),
                         "2025-06-01")
        # a value that is not a date reads as none, and is read again
        _set_since(self.conn, "FABRIKAM PROPERTIES", "soon")
        self.assertEqual(bills.fill_scheduled_since(self.conn, today=TODAY), 1)
        self.assertEqual(_since(self.conn, "FABRIKAM PROPERTIES"),
                         "2025-11-01")

    def test_a_bill_starts_at_its_first_payment_before_a_break(self):
        # an older stretch of rent, ended by two unpaid months: the bill
        # was the household's from its first payment
        for d in _months(dt.date(2024, 1, 1), dt.date(2025, 8, 1)):
            add_txn(self.conn, d.replace(day=1), 1500.0,
                    "FABRIKAM PROPERTIES", account="chk")
        self.conn.execute("UPDATE bills SET raw = raw - 'scheduled_since'")
        bills.fill_scheduled_since(self.conn, today=TODAY)
        self.assertEqual(_since(self.conn, "FABRIKAM PROPERTIES"),
                         "2024-01-01")

    def test_an_archive_without_scheduled_since_is_filled_after_restore(self):
        import csv
        import io
        import json
        import zipfile

        from oikonome.sync import restore
        raw = {"source": "manual", "bill_type": "occurrence",
               "dueOn": "2026-12-01", "lastDueOn": None,
               "recurrence": {"frequency": "MONTHLY", "interval": 1,
                              "byMonthDay": [1]}}
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["id", "type", "payee", "amount", "frequency",
                    "monthly_amount", "due_on", "active", "raw"])
        w.writerow(["rec:northwind-water", "BILL", "NORTHWIND WATER", "-40",
                    "MONTHLY", "-40", "2026-12-01", "1", json.dumps(raw)])
        for d in _months(dt.date(2026, 2, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=1), 40.0, "NORTHWIND WATER",
                    account="chk")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("bills.csv", sio.getvalue())
        restore.restore_zip(self.conn, buf.getvalue())
        self.assertIsNone(_since(self.conn, "NORTHWIND WATER"))
        # the nightly supplies it
        bills.fill_scheduled_since(self.conn, today=TODAY)
        self.assertEqual(_since(self.conn, "NORTHWIND WATER"), "2026-02-01")


class AnEnvelopeReachesAMonthOnlyFromItsStart(unittest.TestCase):
    """A household whose one bill is a fuel envelope, used every month
    from Nov 2023 beside $500 of groceries."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for d in _months(dt.date(2023, 11, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=12), 500.0, "GROCER")
            add_txn(self.conn, d.replace(day=20), 60.0, "CONTOSO FUEL",
                    account="chk")
            add_txn(self.conn, d.replace(day=2), -4000.0, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
        add_bill(self.conn, "CONTOSO FUEL", 100.0, bill_type="envelope",
                 next_due=dt.date(2026, 12, 1))

    def tearDown(self):
        self.conn.close()

    def test_an_envelope_reaches_a_month_only_from_its_start(self):
        self.assertEqual(_since(self.conn, "CONTOSO FUEL"), "2023-11-20")
        _set_since(self.conn, "CONTOSO FUEL", "2024-06-01")
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        # Nov 2023 to May 2024: seven months the envelope had not started
        self.assertEqual(out["unsplit"], 7 * 560.0)
        self.assertEqual(out["fixed"], 29 * 60.0)


class BillsPaidBeforeTodaysBigBillsKeepTheirMonthsSplit(RentFromLastNovember):
    """A $12 subscription paid every month from Nov 2024, a year before the
    rent began, beside $500 of groceries since Nov 2023. The household had
    a bill schedule from the subscription's first payment on: those months
    are split, although the subscription is a small part of today's plan;
    only the year before it, when no bill of the household's had begun,
    is unsplit."""

    def setUp(self):
        super().setUp()
        for d in _months(dt.date(2024, 11, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=5), 12.0, "LITWARE STREAMING",
                    account="chk")
        add_bill(self.conn, "LITWARE STREAMING", 12.0,
                 next_due=dt.date(2026, 12, 5))

    def test_a_long_window_draws_the_months_before_any_bill_apart(self):
        # months a bill was paid in stay split though bigger bills began
        # later
        self.assertEqual(_since(self.conn, "LITWARE STREAMING"), "2024-11-05")
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(out["fixed"], 24 * 12.0 + 12 * 1500.0)
        self.assertEqual(out["variable_scheduled"], 24 * 500.0)
        self.assertEqual(out["unsplit"], 12 * 500.0)

    def test_the_months_before_the_first_bill_are_unsplit_and_said_to_be(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(out["unsplit_text"],
                         "$6,000 went out in 12 months before your first "
                         "bill — not split into fixed and variable. In the "
                         "other 24 months the charges your bills matched "
                         "are 60% of what went out.")

    def test_a_month_after_the_first_bill_with_nothing_paid_is_still_split(self):
        # one subscription payment missed in March 2025: the run goes on,
        # and the month is the household's, with nothing fixed in it
        self.conn.execute("DELETE FROM transactions WHERE date = %s",
                          (dt.date(2025, 3, 5),))
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        self.assertEqual(out["unsplit"], 12 * 500.0)
        self.assertEqual(out["fixed"], 23 * 12.0 + 12 * 1500.0)

    def test_a_window_the_schedule_covers_has_nothing_unsplit(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "1y")["out"]
        self.assertEqual(out["unsplit"], 0.0)


def _every_unsplit_month_is_before_the_first_bill(test, conn, out):
    """The caption's promise: fixed + variable + unsplit is what went out,
    and every month counted unsplit ends before the earliest start of any
    bill."""
    test.assertAlmostEqual(out["fixed"] + out["variable_scheduled"]
                           + out["unsplit"], out["total"], places=2)
    first = conn.execute(
        "SELECT min((raw->>'scheduled_since')::date) AS d FROM bills"
        ).fetchone()["d"]
    test.assertIsNotNone(first)
    return first


class AMonthFrozenBeforeTheBillsWereSetUp(unittest.TestCase):
    """Rent of $1,500 paid every month from Nov 2025 beside $500 of
    groceries since Nov 2023. June to August 2026 were closed and frozen
    before the household set up any bill (an empty frozen schedule); the
    rent bill was approved later. Those months are after the rent's first
    payment, so they are split like every month after it."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for d in _months(dt.date(2023, 11, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=12), 500.0, "GROCER")
            add_txn(self.conn, d.replace(day=2), -4000.0, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
            if d >= dt.date(2025, 11, 1):
                add_txn(self.conn, d.replace(day=1), 1500.0,
                        "FABRIKAM PROPERTIES", account="chk")
        for m in (6, 7, 8):
            freeze_month(self.conn, 2026, m)
        add_bill(self.conn, "FABRIKAM PROPERTIES", 1500.0,
                 next_due=dt.date(2026, 12, 1))

    def tearDown(self):
        self.conn.close()

    def test_an_empty_frozen_month_after_the_first_bill_is_split(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        _every_unsplit_month_is_before_the_first_bill(self, self.conn, out)
        self.assertEqual(out["unsplit"], 24 * 500.0)
        # the frozen months were budgeted with no bill, so their rent is
        # theirs as the Month lens reads it: variable, not fixed
        self.assertEqual(out["fixed"], 9 * 1500.0)
        self.assertEqual(out["variable_scheduled"], 12 * 500.0 + 3 * 1500.0)
        out = reporting.flow_breakdown(self.conn, TODAY, "1y")["out"]
        self.assertEqual(out["unsplit"], 0.0)
        self.assertIsNone(out["unsplit_text"])


class ABillsFirstPaymentIsItsStartThroughABreak(unittest.TestCase):
    """Rent of $1,500 paid every month from Nov 2023 except March and
    April 2025, beside $500 of groceries. The two skipped months break
    the run, but the household had this bill from its first payment: no
    month after Nov 2023 is "before your first bill"."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for d in _months(dt.date(2023, 11, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=12), 500.0, "GROCER")
            add_txn(self.conn, d.replace(day=2), -4000.0, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
            if d not in (dt.date(2025, 3, 1), dt.date(2025, 4, 1)):
                add_txn(self.conn, d.replace(day=1), 1500.0,
                        "FABRIKAM PROPERTIES", account="chk")
        add_bill(self.conn, "FABRIKAM PROPERTIES", 1500.0,
                 next_due=dt.date(2026, 12, 1))

    def tearDown(self):
        self.conn.close()

    def test_the_start_is_the_first_payment_ever_matched(self):
        self.assertEqual(_since(self.conn, "FABRIKAM PROPERTIES"),
                         "2023-11-01")

    def test_months_before_a_break_in_the_payments_stay_split(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        _every_unsplit_month_is_before_the_first_bill(self, self.conn, out)
        self.assertEqual(out["unsplit"], 0.0)
        self.assertEqual(out["fixed"], 34 * 1500.0)
        self.assertIsNone(out["unsplit_text"])


def _back(months: int, day: int) -> dt.date:
    """The `day` of the month `months` before this one, by the real day
    (the share is measured against the bills started by the household's
    own today)."""
    d = dt.date.today().replace(day=1)
    for _ in range(months):
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return d.replace(day=day)


class ABillThatHasNotStartedYetIsNotPartOfThePlan(unittest.TestCase):
    """Two small bills paid every month for two years; a $3,000 bill
    entered by hand today, first due next month, with no payment yet. It
    has not started, so it is no part of the plan an old month is measured
    against: the months the small bills paid stay split."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # paid on the 1st and 2nd, this month included
        for n in range(24, -1, -1):
            add_txn(self.conn, _back(n, 12), 300.0, "GROCER")
            add_txn(self.conn, _back(n, 1), 50.0, "CONTOSO POWER",
                    account="chk")
            add_txn(self.conn, _back(n, 2), 40.0, "NORTHWIND WATER",
                    account="chk")
        first_of_next = (dt.date.today().replace(day=1)
                         + dt.timedelta(days=32)).replace(day=1)
        add_bill(self.conn, "CONTOSO POWER", 50.0, next_due=first_of_next)
        add_bill(self.conn, "NORTHWIND WATER", 40.0,
                 next_due=first_of_next.replace(day=2))
        nxt = (dt.date.today().replace(day=1)
               + dt.timedelta(days=40)).replace(day=3)
        add_bill(self.conn, "FABRIKAM PROPERTIES", 3000.0, next_due=nxt,
                 source="manual")

    def tearDown(self):
        self.conn.close()

    def test_a_big_bill_due_next_month_leaves_the_paid_months_split(self):
        out = reporting.flow_breakdown(self.conn, dt.date.today(), "1y")["out"]
        self.assertEqual(out["fixed"], 12 * 90.0)
        self.assertEqual(out["unsplit"], 0.0)

    def test_a_bill_first_due_next_month_changes_no_figure(self):
        today = dt.date.today()
        with_it = {k: reporting.flow_breakdown(self.conn, today, k)["out"]
                   for k in ("1y", "3y", "all")}
        self.conn.execute("DELETE FROM bills WHERE payee = %s",
                          ("FABRIKAM PROPERTIES",))
        for k, out in with_it.items():
            without = reporting.flow_breakdown(self.conn, today, k)["out"]
            self.assertEqual(
                {f: out[f] for f in ("fixed", "unsplit", "unsplit_text",
                                     "variable_scheduled")},
                {f: without[f] for f in ("fixed", "unsplit", "unsplit_text",
                                         "variable_scheduled")}, k)

    def test_the_month_in_progress_is_reached(self):
        from oikonome.engine import budget
        st = budget.month_status(self.conn, dt.date.today())
        self.assertTrue(st["buckets"]["fixed"]["reached"])


class AStartMovesEarlierWhenHistoryArrives(unittest.TestCase):
    """A bill saved with one month of history; twelve earlier payments
    arrive later (a bank's historical backfill, a statement import). The
    nightly moves its start back to the first of them, and never forward."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, _back(1, 6), 50.0, "CONTOSO POWER", account="chk")
        add_bill(self.conn, "CONTOSO POWER", 50.0,
                 next_due=dt.date.today().replace(day=1)
                 + dt.timedelta(days=36))

    def tearDown(self):
        self.conn.close()

    def test_history_that_arrives_later_moves_the_start_back(self):
        self.assertEqual(_since(self.conn, "CONTOSO POWER"),
                         _back(1, 6).isoformat())
        for n in range(13, 1, -1):
            add_txn(self.conn, _back(n, 6), 50.0, "CONTOSO POWER",
                    account="chk")
        self.assertEqual(bills.fill_scheduled_since(self.conn), 1)
        self.assertEqual(_since(self.conn, "CONTOSO POWER"),
                         _back(13, 6).isoformat())
        # read again with nothing new: nothing to write
        self.assertEqual(bills.fill_scheduled_since(self.conn), 0)

    def test_a_start_never_moves_later(self):
        _set_since(self.conn, "CONTOSO POWER", _back(30, 1).isoformat())
        self.assertEqual(bills.fill_scheduled_since(self.conn), 0)
        self.assertEqual(_since(self.conn, "CONTOSO POWER"),
                         _back(30, 1).isoformat())

    def test_an_edit_keeps_the_stored_start(self):
        _set_since(self.conn, "CONTOSO POWER", _back(30, 1).isoformat())
        add_bill(self.conn, "CONTOSO POWER", 55.0,
                 next_due=dt.date.today().replace(day=1)
                 + dt.timedelta(days=36))
        self.assertEqual(_since(self.conn, "CONTOSO POWER"),
                         _back(30, 1).isoformat())



if __name__ == "__main__":
    unittest.main()
