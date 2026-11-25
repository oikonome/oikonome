"""The first paycheck of a year follows last year's dip (specs/today-
figures.md, "A paycheck follows last year's pattern"): when the
household's own history shows the first check of last year smaller than
the December ones (benefits and taxes that reset with the year), a
January payday walks at that ratio and is labelled an estimate, though
the dip lasted one check. A deposit far smaller than the series' checks
(a reimbursement the payer sent beside them) is no paycheck: it neither
breaks a step that held nor stands in as one. A noise-sized wobble at the
year's start, or a December payday whose year-ago neighbour is a January
check, walks the current amount. Figures and names are invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import forecast

D = dt.date
TODAY_ = D(2026, 11, 26)


def biweekly(first: D, last: D, amount_on):
    out, d = [], first
    while d <= last:
        out.append({"date": d, "amount": float(amount_on(d))})
        d += dt.timedelta(days=14)
    return out


def dip_once(share):
    # the first check of 2026 lower by `share`, every other at the level
    return lambda d: 2000.0 * (1 - share) if d == D(2026, 1, 5) else 2000.0


class TheYearsFirstCheck(unittest.TestCase):
    amount = staticmethod(forecast.paycheck_pattern_amount)

    def test_a_one_check_dip_at_the_years_start_is_walked(self):
        deps = biweekly(D(2025, 1, 6), D(2026, 11, 20), dip_once(0.06))
        self.assertEqual(self.amount(2100.0, deps, D(2027, 1, 4), TODAY_),
                         (round(2100.0 * 0.94, 2), True))
        # the check after it is back at the level
        self.assertEqual(self.amount(2100.0, deps, D(2027, 1, 18), TODAY_),
                         (2100.0, False))

    def test_a_wobble_at_the_years_start_is_no_dip(self):
        deps = biweekly(D(2025, 1, 6), D(2026, 11, 20), dip_once(0.015))
        self.assertEqual(self.amount(2100.0, deps, D(2027, 1, 4), TODAY_),
                         (2100.0, False))

    def test_a_december_payday_does_not_take_januarys_dip(self):
        deps = biweekly(D(2025, 1, 6), D(2026, 11, 20), dip_once(0.06))
        # a year before 12/31/26 is 12/31/25; its nearest check is 01/05/26
        self.assertEqual(self.amount(2100.0, deps, D(2026, 12, 31), TODAY_),
                         (2100.0, False))

    def test_a_small_deposit_beside_a_step_does_not_break_it(self):
        # last year $2,000 a check until November and $2,100 after; two
        # small reimbursements from the same payer around one check
        deps = biweekly(D(2025, 1, 2), D(2026, 10, 9),
                        lambda d: 2100.0 if d.month >= 11 else 2000.0)
        deps += [{"date": D(2025, 11, 19), "amount": 140.0},
                 {"date": D(2025, 11, 21), "amount": 160.0}]
        self.assertEqual(
            self.amount(2300.0, deps, D(2026, 11, 20), D(2026, 10, 16)),
            (round(2300.0 * 2100 / 2000, 2), True))



def _noisy(seed: int, step: int, cv: float):
    """A paycheck with no year-start effect at all: each deposit the level
    plus independent noise (hours that vary)."""
    r = random.Random(seed)
    d, out = D(2024, 10, 14), []
    while d <= D(2025, 11, 17):
        out.append({"date": d, "amount": round(
            1000.0 * max(0.2, 1 + r.gauss(0, cv)), 2)})
        d += dt.timedelta(days=step)
    return out


# January paydays given a pattern estimate by the rule before the
# year-start step existed, over exactly the series below: the year-start
# step may add almost nothing to them on noise
BEFORE_WEEKLY, BEFORE_BIWEEKLY = 26, 108
NOISE_MARGIN = 6


class NoiseIsNoDip(unittest.TestCase):
    amount = staticmethod(forecast.paycheck_pattern_amount)
    asof = D(2025, 11, 24)

    def test_variable_pay_gets_no_more_estimates_than_before(self):
        weekly = sum(self.amount(1000.0, _noisy(s, 7, 0.15), D(2026, 1, 5),
                                 self.asof)[1] for s in range(300))
        biweekly = sum(self.amount(1000.0, _noisy(s, 14, 0.06), pd,
                                   self.asof)[1]
                       for s in range(300)
                       for pd in (D(2026, 1, 5), D(2026, 1, 19)))
        self.assertLessEqual(weekly, BEFORE_WEEKLY + NOISE_MARGIN)
        self.assertLessEqual(biweekly, BEFORE_BIWEEKLY + NOISE_MARGIN)

    def test_a_one_off_january_cut_is_no_reset(self):
        # a $2,000 check that was $1,200 once last January (unpaid leave)
        deps = biweekly(D(2024, 10, 14), D(2025, 11, 17),
                        lambda d: 1200.0 if d == D(2025, 1, 6) else 2000.0)
        self.assertEqual(self.amount(2000.0, deps, D(2026, 1, 5), self.asof),
                         (2000.0, False))

    def test_a_small_reset_on_a_steady_salary_still_counts(self):
        deps = biweekly(D(2024, 10, 14), D(2025, 11, 17),
                        lambda d: 1920.0 if d == D(2025, 1, 6) else 2000.0)
        self.assertEqual(self.amount(2000.0, deps, D(2026, 1, 5), self.asof),
                         (1920.0, True))


class LastYearOnAClosedAccount(unittest.TestCase):
    def test_januarys_first_check_walks_last_years_dip(self):
        from .util import add_bill, add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            conn.execute("UPDATE accounts SET balance_available=5000, "
                         "balance_current=5000 WHERE id='chk'")
            # last year's deposits on a checking account since closed,
            # kept as a hand-made account of its imported statements (a
            # HIDDEN account is left out of every figure by design)
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token) VALUES ('manual', 'manual', 'Manual', '') "
                "ON CONFLICT DO NOTHING")
            conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype, "
                "balance_current) VALUES ('manual:old-checking-closed', "
                "'manual', 'Old Checking', 'depository', 'checking', 0)")
            last = None
            for dep in biweekly(D(2025, 1, 6), D(2026, 11, 20),
                                dip_once(0.06)):
                acct = "manual:old-checking-closed" if dep["date"] < D(2026, 3, 1) else "chk"
                add_txn(conn, dep["date"], -dep["amount"],
                        "LANTERN WORKS PAYROLL", account=acct,
                        primary="INCOME", detailed="INCOME_WAGES")
                last = dep["date"]
            add_bill(conn, "LANTERN WORKS PAYROLL", 2000.0, income=True,
                     frequency="WEEKLY", interval=2,
                     next_due=last + dt.timedelta(days=14), last_seen=last)
            f = forecast.build(conn, today=TODAY_)
        finally:
            conn.close()
        pays = [(d0, a, p) for d0, a, p in f["events"]
                if p.startswith("LANTERN WORKS PAYROLL")]
        est = [x for x in pays if x[2].endswith(
            "(estimate, from last year's pattern)")]
        # the first payday of 2027 alone
        self.assertEqual([round(a, 2) for _d, a, _p in est],
                         [round(2000.0 * 0.94, 2)])
        self.assertEqual(est[0][0], min(d0 for d0, _a, _p in pays
                                        if d0 >= "2027-01-01"))


if __name__ == "__main__":
    unittest.main()
