"""A paycheck series leaves the cash forecast only when the ledger says
the money stopped, not that we stopped seeing it
(forecast.stopped_paychecks):

- the accounts its checks landed in are still being read (a posted row
  within PAYCHECK_FEED_FRESH_DAYS of yesterday) — a bank feed that went
  quiet, or a household that imports a statement once a month, is no
  evidence about the paycheck;
- at least two of its paydays have passed since its last check, each
  with other activity on those accounts after it;
- nothing that could be the same check under another name or amount
  arrived there since (an unclaimed deposit of about its size, or a
  smaller one near a payday — hours cut);
- its own history shows no gap of that length at this time of year (a
  school-year salary).

An old job's check projected for months after the job ended is the
largest rosy error a job change makes; a live paycheck dropped from the
walk is a false "you run out". When a series does leave, the reply says
so once, and `next_paycheck` never names it. Figures and names are
invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import forecast
from oikonome.web import todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

PAYER = "Quillfield Payroll"
DEP = "QUILLFIELD PAYROLL DIRECT DEP"


def D(n: int) -> dt.date:
    return TODAY - dt.timedelta(days=n)


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)

    def tearDown(self):
        self.conn.close()

    def activity(self, since: int, until: int = 1, every: int = 3,
                 account: str = "chk"):
        """Everyday spending on the account from `since` to `until` days
        ago: the feed is alive."""
        for k in range(since, until - 1, -every):
            add_txn(self.conn, D(k), 23.0, "CORNER GROCER", account=account,
                    primary="FOOD_AND_DRINK")

    def biweekly(self, last: int, n: int = 6, amount: float = 1500.0,
                 name: str = DEP):
        for i in range(n):
            add_txn(self.conn, D(last + 14 * i), -amount, name,
                    account="chk", primary="INCOME")
        nxt = D(last) + dt.timedelta(days=14)
        while nxt <= TODAY:
            nxt += dt.timedelta(days=14)
        add_bill(self.conn, PAYER, amount, frequency="WEEKLY", interval=2,
                 next_due=nxt, income=True, last_seen=D(last))

    def walked(self, payer: str = PAYER):
        f = forecast.build(self.conn, today=TODAY, days=60)
        return [a for _d, a, p in f["events"] if p.startswith(payer)]


class StoppedPaycheck(_Base):
    def test_a_paycheck_gone_two_paydays_on_a_live_account_is_not_walked(
            self):
        self.biweekly(last=40)
        self.activity(39)
        self.assertEqual(self.walked(), [])

    def test_a_paycheck_that_landed_last_cycle_is_walked(self):
        self.biweekly(last=12)
        self.activity(11)
        self.assertGreaterEqual(len(self.walked()), 4)

    def test_one_missed_payday_is_not_yet_stopped(self):
        self.biweekly(last=26)
        self.activity(25)
        self.assertGreaterEqual(len(self.walked()), 4)

    def test_a_paycheck_never_deposited_yet_is_walked(self):
        add_bill(self.conn, PAYER, 1500.0, frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=5), income=True)
        self.activity(30)
        self.assertGreaterEqual(len(self.walked()), 4)

    def test_a_check_dated_today_does_not_restart_it_before_tomorrow(self):
        # what the forecast learned is as of the end of yesterday
        self.biweekly(last=40)
        self.activity(39)
        add_txn(self.conn, TODAY, -1500.0, DEP, account="chk",
                primary="INCOME")
        self.assertEqual(self.walked(), [])

    def test_a_monthly_check_gone_two_months_is_not_walked(self):
        for k in (70, 100, 130, 160):
            add_txn(self.conn, D(k), -3000.0, DEP, account="chk",
                    primary="INCOME")
        add_bill(self.conn, PAYER, 3000.0, frequency="MONTHLY",
                 next_due=TODAY + dt.timedelta(days=10), income=True,
                 last_seen=D(70))
        self.activity(69)
        self.assertEqual(self.walked(), [])

    def test_the_old_job_leaves_once_the_new_one_is_a_series(self):
        # the new payer's checks land in the same account, claimed by its
        # own series: they are not the old check under another name
        self.biweekly(last=40)
        self.activity(39)
        for k in (31, 17, 3):
            add_txn(self.conn, D(k), -1700.0, "BRAMBLE WORKS PAYROLL",
                    account="chk", primary="INCOME")
        add_bill(self.conn, "Bramble Works Payroll", 1700.0,
                 frequency="WEEKLY", interval=2, next_due=D(3)
                 + dt.timedelta(days=14), income=True, last_seen=D(3))
        self.assertEqual(self.walked(), [])
        self.assertTrue(self.walked("Bramble Works Payroll"))


class SilenceIsNotStopping(_Base):
    def test_a_quiet_bank_feed_keeps_the_paycheck(self):
        # weekly pay; nothing at all posted for three weeks
        for i in range(8):
            add_txn(self.conn, D(21 + 7 * i), -800.0, DEP, account="chk",
                    primary="INCOME")
        nxt = D(21)
        while nxt <= TODAY:
            nxt += dt.timedelta(days=7)
        add_bill(self.conn, PAYER, 800.0, frequency="WEEKLY",
                 next_due=nxt, income=True, last_seen=D(21))
        self.activity(60, until=21)
        self.assertGreaterEqual(len(self.walked()), 8)

    def test_a_monthly_statement_import_keeps_the_paycheck(self):
        # the 15th and the last day, imported through the end of May
        for d in (dt.date(2026, 3, 13), dt.date(2026, 3, 31),
                  dt.date(2026, 4, 15), dt.date(2026, 4, 30),
                  dt.date(2026, 5, 15), dt.date(2026, 5, 29)):
            add_txn(self.conn, d, -2100.0, DEP, account="chk", primary="")
        add_bill(self.conn, PAYER, 2100.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 7, 31), income=True,
                 last_seen=dt.date(2026, 5, 29), by_month_day=[15, -1])
        for k in range(77, 47, -4):
            add_txn(self.conn, D(k), 23.0, "CORNER GROCER", account="chk",
                    primary="")
        self.assertGreaterEqual(len(self.walked()), 3)

    def test_a_payer_the_bank_renamed_keeps_the_paycheck(self):
        self.biweekly(last=40)
        self.activity(39)
        for k in (26, 12):
            add_txn(self.conn, D(k), -1500.0, "QFLD HOLDINGS PR DIR DEP",
                    account="chk", primary="INCOME")
        self.assertGreaterEqual(len(self.walked()), 4)

    def test_short_checks_after_hours_were_cut_keep_the_paycheck(self):
        # the last two checks came in under the deposit floor
        self.biweekly(last=40, amount=450.0)
        self.activity(39)
        for k in (26, 12):
            add_txn(self.conn, D(k), -190.0, DEP, account="chk",
                    primary="INCOME")
        self.assertGreaterEqual(len(self.walked()), 4)

    def test_a_rename_onto_a_word_another_series_keys_on_keeps_it(self):
        # the spouse's series claims the renamed deposits by its word,
        # but they land between its paydays: not its own checks
        self.biweekly(last=40)
        self.activity(39)
        for i in range(8):
            add_txn(self.conn, D(33 + 14 * i), -2000.0,
                    "GUSTO PAY 88123 BRAMBLE", account="chk",
                    primary="INCOME")
        add_bill(self.conn, "Gusto Pay", 2000.0, frequency="WEEKLY",
                 interval=2, next_due=D(5) + dt.timedelta(days=14),
                 income=True, last_seen=D(5))
        for k in (26, 12):
            add_txn(self.conn, D(k), -1500.0, "GUSTO PAY 99001",
                    account="chk", primary="INCOME")
        self.assertGreaterEqual(len(self.walked()), 4)

    def test_a_renamed_check_filed_as_a_transfer_in_keeps_it(self):
        self.biweekly(last=40)
        self.activity(39)
        for k in (26, 12):
            add_txn(self.conn, D(k), -1500.0, "QFLD HOLDINGS PR DIR DEP",
                    account="chk", primary="TRANSFER_IN")
        self.assertGreaterEqual(len(self.walked()), 4)

    def test_a_refund_or_an_own_transfer_is_not_the_check(self):
        self.biweekly(last=40)
        self.activity(39)
        add_txn(self.conn, D(26), -1500.0, "SOFA OUTLET REFUND",
                account="chk", primary="GENERAL_MERCHANDISE")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES ('sav','it1',"
            "'Test Savings','depository','savings',4000,4000)")
        add_txn(self.conn, D(12), -1500.0, "FROM SAVINGS", account="chk",
                primary="TRANSFER_IN")
        add_txn(self.conn, D(12), 1500.0, "TO CHECKING", account="sav",
                primary="TRANSFER_OUT")
        self.assertEqual(self.walked(), [])

    def test_a_school_year_salary_is_kept_through_its_summer(self):
        # paid the 15th and the 28th from September to May, two
        # years running; today is mid-July
        days = []
        for y in (2024, 2025, 2026):
            for m in list(range(1, 6)) + list(range(9, 13)):
                for d in (15, 28):
                    day = dt.date(y, m, d)
                    if D(730) < day < dt.date(2026, 7, 1):
                        days.append(day)
        for day in days:
            add_txn(self.conn, day, -2400.0, DEP, account="chk",
                    primary="INCOME")
        add_bill(self.conn, PAYER, 2400.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 7, 31), income=True,
                 last_seen=max(days), by_month_day=[15, -1])
        self.activity(60)
        self.assertTrue(self.walked())


class SaidOnce(_Base):
    def test_a_stopped_paycheck_is_said_and_never_named_next(self):
        self.biweekly(last=40)
        self.activity(39)
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual(f["stopped_paychecks"],
                         [{"payee": PAYER, "last": D(40).isoformat()}])
        note = todayview.stopped_paychecks_note(f)
        self.assertIn(PAYER, note)
        self.assertIn(D(40).strftime("%m/%d/%y"), note)
        self.assertIn("Bills page", note)
        self.assertIsNone(forecast.next_paycheck(self.conn, TODAY))

    def test_nothing_is_said_while_every_paycheck_is_walked(self):
        self.biweekly(last=12)
        self.activity(11)
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual(f["stopped_paychecks"], [])
        self.assertIsNone(todayview.stopped_paychecks_note(f))
        self.assertIsNotNone(forecast.next_paycheck(self.conn, TODAY))


class LearnedOnce(_Base):
    def test_a_today_request_judges_the_paychecks_once(self):
        self.biweekly(last=40)
        self.activity(39)
        forecast.forget_learned()
        calls = []
        real = forecast._stopped_paychecks

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        forecast._stopped_paychecks = counting
        try:
            forecast.build(self.conn, today=TODAY, days=60)
            forecast.next_paycheck(self.conn, TODAY)
        finally:
            forecast._stopped_paychecks = real
        self.assertEqual(len(calls), 1)


class NeverDropsALivePaycheck(_Base):
    """Seeded households whose paycheck never stopped, each disturbed one
    way: the series is never left out. And a job that really ended is
    left out within about two cycles of its last check."""

    N = 60

    def _stopped(self):
        return forecast.stopped_paychecks(
            self.conn, forecast.budget.load_config(self.conn), TODAY)

    def _reset(self):
        self.conn.execute("DELETE FROM transactions")
        self.conn.execute("DELETE FROM bills")
        self.conn._memo = None
        forecast.forget_learned()

    def _live(self, rng, cycle, amount, *, until=1, name=DEP, cat="INCOME",
              short_after=None, gap_summers=False, since=400):
        """A live series' checks every `cycle` days from `since` days ago
        to `until` days ago."""
        last = None
        k = since - rng.randrange(cycle)
        while k >= until:
            day = D(k)
            if not (gap_summers and day.month in (6, 7, 8)):
                amt = amount * rng.uniform(0.97, 1.03)
                if short_after is not None and k <= short_after:
                    amt = amount * rng.uniform(0.3, 0.38)
                add_txn(self.conn, day, -round(amt, 2), name, account="chk",
                        primary=cat)
                last = day
            k -= cycle
        return last

    def _bill(self, cycle, amount, last):
        nxt = last + dt.timedelta(days=cycle)
        while nxt <= TODAY:
            nxt += dt.timedelta(days=cycle)
        add_bill(self.conn, PAYER, amount, frequency="WEEKLY",
                 interval=cycle // 7, next_due=nxt, income=True,
                 last_seen=last)

    def _spend(self, rng, until=1, since=400):
        k = since
        while k >= until:
            add_txn(self.conn, D(k), round(rng.uniform(5, 90), 2),
                    "CORNER GROCER", account="chk", primary="FOOD_AND_DRINK")
            k -= rng.choice((1, 2))

    def test_a_live_paycheck_is_never_dropped(self):
        dropped = []
        for kind in ("feed_silent", "monthly_import", "renamed", "short",
                     "summer_gap"):
            for n in range(self.N):
                rng = random.Random(f"{kind}-{n}")
                self._reset()
                cycle = rng.choice((7, 14))
                amount = rng.choice((600.0, 1100.0, 1900.0, 2600.0))
                if kind == "feed_silent":
                    quiet = rng.randrange(cycle + 6, 40)
                    last = self._live(rng, cycle, amount, until=quiet)
                    self._spend(rng, until=quiet)
                elif kind == "monthly_import":
                    cut = rng.randrange(20, 50)
                    last = self._live(rng, cycle, amount, until=cut)
                    self._spend(rng, until=cut)
                elif kind == "renamed":
                    at = rng.randrange(cycle * 2 + 6, 90)
                    last = self._live(rng, cycle, amount, until=at)
                    k = (TODAY - last).days - cycle
                    while k >= 1:
                        add_txn(self.conn, D(k),
                                -round(amount * rng.uniform(0.9, 1.1), 2),
                                "QFLD HOLDINGS PR DIR DEP", account="chk",
                                primary=rng.choice(("INCOME", "")))
                        k -= cycle
                    self._spend(rng)
                elif kind == "short":
                    amount = rng.choice((450.0, 520.0))
                    last_full = rng.randrange(cycle * 2 + 6, 70)
                    self._live(rng, cycle, amount, short_after=last_full)
                    self._spend(rng)
                    last = max(r["date"] for r in self.conn.execute(
                        "SELECT date FROM transactions WHERE amount <= -200 "
                        "AND name = %s ORDER BY date", (DEP,)).fetchall())
                else:
                    if TODAY.month not in (6, 7, 8):
                        self.skipTest("the summer case needs a summer today")
                    # last summer's gap is in the history read
                    last = self._live(rng, cycle, amount, gap_summers=True,
                                      since=700)
                    self._spend(rng)
                self._bill(cycle, amount, last)
                if self._stopped():
                    dropped.append(f"{kind}-{n}")
        self.assertEqual(dropped, [])

    def test_a_job_that_ended_is_dropped_within_about_two_cycles(self):
        late = []
        for n in range(20):
            rng = random.Random(f"ended-{n}")
            self._reset()
            cycle = rng.choice((7, 14))
            amount = rng.choice((900.0, 2200.0))
            ended = rng.randrange(1, 4 * cycle)
            last = self._live(rng, cycle, amount, until=ended + 1)
            self._spend(rng)
            self._bill(cycle, amount, last)
            silent = (TODAY - last).days
            stopped = bool(self._stopped())
            # two paydays and the grace, then a few days more for the
            # activity that shows the second one passed
            due = 2 * cycle + forecast.PAYCHECK_STOPPED_GRACE_DAYS + 3
            if silent > due and not stopped:
                late.append(f"ended-{n}: silent {silent}, not dropped")
            if silent <= cycle + forecast.PAYCHECK_STOPPED_GRACE_DAYS \
                    and stopped:
                late.append(f"ended-{n}: silent {silent}, dropped early")
        self.assertEqual(late, [])


if __name__ == "__main__":
    unittest.main()
