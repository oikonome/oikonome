"""A dated savings goal is on pace when what is saved is at least what the
straight line from its plan day to its target date asked for 38 days ago
(`specs/savings-pace.md`). The verdict reads the saved total on each day and
nothing about how the deposits arrive, so no rhythm, mixture, moved deposit
or calendar quirk may change it.

Every saver here is invented. Each one's plan is started on every day from
2024-12-31 to 2027-12-30 and read on every day up to its target date, so
every weekday, month length and leap position meets every phase of every
stream. A saver "on plan" has no slack at all: the last deposit before the
date is the one that finishes the goal.

A mail is counted the way the nightly sweep sends one: on a night the
verdict is off pace and the night before it was not."""

import calendar
import datetime as dt
import unittest

from oikonome.engine import savings

D = dt.date
LO, HI = D(2023, 1, 1), D(2030, 12, 31)
GRACE = savings.PACE_GRACE_DAYS
PLAN_DAYS = [D(2024, 12, 31) + dt.timedelta(days=i) for i in range(3 * 365)]


# ---- invented deposit streams: {date: amount} ----

def _months():
    y, m = LO.year, LO.month
    while (y, m) <= (HI.year, HI.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _shift(d, move):
    if move == "next" and d.weekday() >= 5:
        return d + dt.timedelta(days=7 - d.weekday())
    if move == "prev" and d.weekday() >= 5:
        return d - dt.timedelta(days=d.weekday() - 4)
    return d


def monthly(day, amount, move=None, every=1):
    out = {}
    for i, (y, m) in enumerate(_months()):
        if i % every:
            continue
        d = _shift(D(y, m, min(day, calendar.monthrange(y, m)[1])), move)
        out[d] = out.get(d, 0) + amount
    return out


def every_n(n, amount, skip=None, moved=None):
    out, d, i = {}, D(2023, 1, 6), 0           # a Friday
    while d <= HI:
        if not (skip and skip(i)):
            day = moved(d) if moved else d
            out[day] = out.get(day, 0) + amount
        d += dt.timedelta(days=n)
        i += 1
    return out


def nth_weekday(n, weekday, amount):
    out = {}
    for y, m in _months():
        d = D(y, m, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
        out[d] = amount
    return out


def merge(*streams):
    out = {}
    for s in streams:
        for d, v in s.items():
            out[d] = out.get(d, 0) + v
    return out


def jitter(stream, k):
    """Every deposit up to k days either side of its day, in a fixed
    pattern rather than a seeded one so the stream is the same anywhere."""
    out = {}
    for i, d in enumerate(sorted(stream)):
        day = d + dt.timedelta(days=(i * 5) % (2 * k + 1) - k)
        out[day] = out.get(day, 0) + stream[d]
    return out


def _holiday(d):
    moved = {(12, 25), (12, 26), (1, 1), (1, 2), (7, 3), (7, 4),
             (11, 27), (11, 28)}
    return d - dt.timedelta(days=2) if (d.month, d.day) in moved else d


def lumpy():
    out, d, i = {}, LO + dt.timedelta(days=5), 0
    sizes = (200.0, 300.0, 400.0, 500.0, 650.0, 800.0)
    while d <= HI:
        out[d] = sizes[(i * 5) % 6]
        d += dt.timedelta(days=20 + (i * 13) % 21)
        i += 1
    return out


FIRST = monthly(1, 500)
REGULAR = {
    "monthly on the 1st": FIRST,
    "monthly on the 30th": monthly(30, 500),
    "monthly on the last day": monthly(31, 500),
    "the 1st, weekends moved to Monday": monthly(1, 500, "next"),
    "the 1st, weekends moved to Friday": monthly(1, 500, "prev"),
    "the last day, weekends moved to Monday": monthly(31, 500, "next"),
    "the last day, weekends moved to Friday": monthly(31, 500, "prev"),
    "weekly": every_n(7, 100),
    "every other week": every_n(14, 250),
    "the 1st and the 15th": merge(monthly(1, 250), monthly(15, 250)),
    "weekly and monthly on the 1st": merge(every_n(7, 50), monthly(1, 500)),
    "weekly and monthly on the first Friday": merge(
        every_n(7, 50), nth_weekday(1, 4, 500)),
    "the fourth Thursday": nth_weekday(4, 3, 1000),
    "every other week and the 15th": merge(
        every_n(14, 200), monthly(15, 150)),
    "weekly, holiday weeks two days early": every_n(7, 100, moved=_holiday),
    "weekly, a day either side": jitter(every_n(7, 100), 1),
    "monthly, two days either side": jitter(monthly(3, 500), 2),
    "every other week, a day either side": jitter(every_n(14, 250), 1),
    "weekly with four of every fourteen skipped": every_n(
        7, 100, skip=lambda i: i % 14 in (3, 4, 9, 12)),
    "weekly on, on, off": every_n(7, 100, skip=lambda i: i % 3 == 2),
}
SLOWER = {
    "quarterly": monthly(1, 1500, every=3),
    "twice a year": monthly(1, 3000, every=6),
    "irregular": lumpy(),
}


# ---- the plan a stream meets exactly, and the days read under it ----

def on_plan(stream, plan_day, horizon=330):
    """(start, target, target_date): the finishing deposit lands on the
    target date."""
    limit = plan_day + dt.timedelta(days=horizon)
    td = max(d for d in stream if plan_day < d <= limit and stream[d] > 0)
    start = 1000.0
    return start, start + sum(v for d, v in stream.items()
                              if plan_day < d <= td), td


def read(nets, plan_day, start, target, td, until):
    """[(day, verdict)] for every day after the plan day through `until`."""
    rows = {d: v for d, v in nets.items() if d > plan_day}
    days = (until - plan_day).days
    verdicts = savings.schedule_days(rows, start, plan_day, start, target,
                                     td, days)
    return [(plan_day + dt.timedelta(days=n + 1), v)
            for n, v in enumerate(verdicts)]


def mails(days):
    out, prev = [], None
    for day, v in days:
        if v is False and prev is not False:
            out.append(day)
        prev = v
    return out


def changes(days):
    """Each day the verdict changed between on and off, with the new one;
    a day with no verdict is not a change."""
    out, prev = [], None
    for day, v in days:
        if v is not None and prev is not None and v != prev:
            out.append((day, v))
        if v is not None:
            prev = v
    return out


def tally(stream, share=1.0):
    """(percent of days read off pace, mails per plan) over every plan day,
    for a plan that needs `share` of what the stream saves."""
    read_days = off = sent = 0
    for a in PLAN_DAYS:
        start, target, td = on_plan(stream, a)
        target = start + (target - start) * share
        days = read({d: v for d, v in stream.items() if d <= td}, a, start,
                    target, td, td)
        read_days += len(days)
        off += sum(1 for _, v in days if v is False)
        sent += len(mails(days))
    return round(100 * off / read_days, 1), round(sent / len(PLAN_DAYS), 2)


def each_plan(case, build, check):
    for a in PLAN_DAYS:
        nets, start, target, td, until, info = build(a)
        days = read(nets, a, start, target, td, until)
        problem = check(a, days, mails(days), changes(days), info)
        if problem:
            case.fail(f"plan set {a}: {problem}")


class RegularSaversStayOnPaceTests(unittest.TestCase):
    """Twenty rhythms, each exactly on plan, never read off pace: not on a
    weekend, a short February, a five-week month or the night before the
    deposit that finishes the goal."""

    def _always_on(self, name, stream, extra=None):
        for a in PLAN_DAYS:
            start, target, td = on_plan(stream, a)
            nets = dict(stream)
            if extra:
                extra(nets, a, td)
            days = read({d: v for d, v in nets.items() if d <= td}, a,
                        start, target, td, td)
            saved = start
            for day, v in days:
                saved += nets.get(day, 0)
                funded = saved >= target - 0.005
                want = (None if funded or (day - a).days <= GRACE else True)
                if v is not want:
                    self.fail(f"{name}: plan set {a}, read {day}: "
                              f"{v!r}, expected {want!r}")
            self.assertEqual(mails(days), [], f"{name}: plan set {a}")
            self.assertIsNone(days[-1][1], f"{name}: funded on its date")

    def test_no_regular_rhythm_is_ever_off_pace(self):
        for name, stream in REGULAR.items():
            with self.subTest(saver=name):
                self._always_on(name, stream)

    def test_extra_money_never_turns_a_saver_off_pace(self):
        def add(at, times):
            def extra(nets, a, td):
                d = a + dt.timedelta(days=int((td - a).days * at))
                nets[d] = nets.get(d, 0) + 500 * times
            return extra

        def ahead_then_skips(nets, a, td):
            add(.2, 3)(nets, a, td)
            half = a + dt.timedelta(days=(td - a).days // 2)
            nets[min(d for d in FIRST if half < d <= td)] -= 500

        for name, extra in (("one extra deposit of 1.5 times", add(.4, 1.5)),
                            ("one extra deposit of 3 times", add(.4, 3)),
                            ("one windfall of 6 times", add(.3, 6)),
                            ("far ahead, then one skipped", ahead_then_skips)):
            with self.subTest(saver=name):
                self._always_on(name, FIRST, extra)

    def test_a_days_reading_does_not_depend_on_later_rows(self):
        a = D(2026, 3, 10)
        start, target, td = on_plan(FIRST, a)
        nets = {d: v for d, v in FIRST.items() if a < d <= td}
        del nets[sorted(nets)[4]]                    # goes off pace mid-way
        whole = read(nets, a, start, target, td, td)
        self.assertIn(False, [v for _, v in whole])
        for cut in range(40, len(whole), 17):
            day = whole[cut][0]
            part = read({d: v for d, v in nets.items() if d <= day}, a,
                        start, target, td, day)
            self.assertEqual(part[-1], whole[cut])


class SaversWhoFallBehindAreToldOnceTests(unittest.TestCase):
    """Each way of falling behind turns the goal off pace once, with one
    mail, on a day the schedule's grace ran out or money left."""

    @staticmethod
    def _stops_at(share):
        def build(a):
            start, target, td = on_plan(FIRST, a)
            cut = a + dt.timedelta(days=int((td - a).days * share))
            nets = {d: v for d, v in FIRST.items() if a < d <= cut}
            return (nets, start, target, td, td + dt.timedelta(days=10),
                    (max(nets) if nets else a, td))
        return build

    def test_a_saver_who_stops_with_most_left_is_told_within_ten_weeks(self):
        def check(a, days, sent, flips, info):
            last, _ = info
            if len(sent) != 1 or [v for _, v in flips] != [False]:
                return f"mails {sent}, changes {flips}"
            late = (sent[0] - last).days
            if not GRACE - 3 <= late <= GRACE + 31:
                return f"told {late} days after the last deposit"
        each_plan(self, self._stops_at(.4), check)

    def test_a_saver_who_stops_near_the_end_is_told_when_the_line_passes(self):
        def check(a, days, sent, flips, info):
            last, td = info
            if len(sent) != 1 or [v for _, v in flips] != [False]:
                return f"mails {sent}, changes {flips}"
            if ((sent[0] - last).days < GRACE - 3
                    and (sent[0] - td).days != savings.PACE_POSTING_DAYS + 1):
                return "told inside the grace"
        each_plan(self, self._stops_at(.92), check)

    @staticmethod
    def _skips_one(doubles_next):
        def build(a):
            start, target, td = on_plan(FIRST, a)
            ds = sorted(d for d in FIRST if a < d <= td)
            k = len(ds) // 2
            nets = {d: FIRST[d] for d in ds}
            del nets[ds[k]]
            if doubles_next:
                nets[ds[k + 1]] += 500
            return (nets, start, target, td, td + dt.timedelta(days=10),
                    (ds[k], ds[k + 1]))
        return build

    def test_a_skipped_deposit_never_made_up_is_one_mail(self):
        def check(a, days, sent, flips, info):
            if len(sent) != 1 or [v for _, v in flips] != [False]:
                return f"mails {sent}, changes {flips}"
            if (sent[0] - info[0]).days < 4:
                return "told within four days of the missed deposit"
        each_plan(self, self._skips_one(False), check)

    def test_a_skipped_deposit_made_up_is_on_pace_the_day_it_lands(self):
        def check(a, days, sent, flips, info):
            if len(sent) != 1:
                return f"mails {sent}"
            if flips != [(sent[0], False), (info[1], True)]:
                return f"changes {flips}, catch-up on {info[1]}"
        each_plan(self, self._skips_one(True), check)

    def test_a_slow_start_is_off_pace_until_it_catches_up(self):
        # $500 then $1,000 from half way, finishing exactly: behind the
        # line for most of the plan, told once, back only on a deposit
        def build(a):
            start, _, td = on_plan(FIRST, a)
            ds = sorted(d for d in FIRST if a < d <= td)
            nets = {d: (500.0 if i < len(ds) // 2 else 1000.0)
                    for i, d in enumerate(ds)}
            return nets, start, start + sum(nets.values()), td, td, nets

        def check(a, days, sent, flips, nets):
            if len(sent) > 1 or sum(1 for _, v in flips if not v) > 1:
                return f"mails {sent}, changes {flips}"
            if any(v and day not in nets for day, v in flips):
                return "back on pace on a day nothing arrived"
        each_plan(self, build, check)

    def test_a_fast_start_and_a_change_of_rhythm_stay_on_pace(self):
        weekly = every_n(7, 115)

        def fast_start(a):
            start, _, td = on_plan(FIRST, a)
            ds = sorted(d for d in FIRST if a < d <= td)
            nets = {d: (1000.0 if i < len(ds) // 2 else 500.0)
                    for i, d in enumerate(ds)}
            return nets, start, start + sum(nets.values()), td, td, None

        def monthly_then_weekly(a):
            start, _, td = on_plan(FIRST, a)
            mid = a + dt.timedelta(days=(td - a).days // 2)
            nets = {d: v for d, v in FIRST.items() if a < d <= mid}
            nets.update({d: v for d, v in weekly.items() if mid < d <= td})
            td = max(nets)
            return nets, start, start + sum(nets.values()), td, td, None

        def check(a, days, sent, flips, info):
            if sent or any(v is False for _, v in days):
                return f"off pace: mails {sent}"
        each_plan(self, fast_start, check)
        each_plan(self, monthly_then_weekly, check)

    def test_a_saver_just_short_every_month_is_told_once_in_two_years(self):
        def build(a):
            start, target, td = on_plan(FIRST, a, horizon=700)
            nets = {d: 485.0 for d in FIRST if a < d <= td}
            return nets, start, target, td, td + dt.timedelta(days=10), None

        def check(a, days, sent, flips, info):
            if len(sent) != 1 or [v for _, v in flips] != [False]:
                return f"mails {sent}, changes {flips}"
        each_plan(self, build, check)

    def test_a_new_goal_has_no_verdict_for_its_first_cycle(self):
        # 0 to 3 monthly deposits of $500 toward $6,000 in a year, then
        # nothing: no verdict for 38 days, then on pace for as long as the
        # line takes to pass what was put in, then one mail
        for count in range(4):
            def build(a, count=count):
                nets = {a + dt.timedelta(days=5 + 30 * i): 500.0
                        for i in range(count)}
                return (nets, 0.0, 6000.0, a + dt.timedelta(days=365),
                        a + dt.timedelta(days=200), None)

            def check(a, days, sent, flips, info, count=count):
                if any(v is not None for day, v in days
                       if (day - a).days <= GRACE):
                    return "a verdict inside the first cycle"
                if len(sent) != 1:
                    return f"mails {sent}"
                want = GRACE + 1 + int(500 * count / 6000 * 365)
                if abs((sent[0] - a).days - want) > 1:
                    return (f"told on day {(sent[0] - a).days}, "
                            f"expected {want}")
            with self.subTest(deposits=count):
                each_plan(self, build, check)

    @staticmethod
    def _withdraws(amount):
        def build(a):
            start, target, td = on_plan(FIRST, a)
            nets = {d: v for d, v in FIRST.items() if a < d <= td}
            day = a + dt.timedelta(days=(td - a).days // 2 + 9)
            nets[day] = nets.get(day, 0) - amount
            return (nets, start, target, td, td + dt.timedelta(days=10),
                    day)
        return build

    def test_a_small_withdrawal_is_one_mail_on_or_after_its_day(self):
        def check(a, days, sent, flips, taken):
            if len(sent) != 1 or [v for _, v in flips] != [False]:
                return f"mails {sent}, changes {flips}"
            if sent[0] < taken:
                return "told before the withdrawal"
        each_plan(self, self._withdraws(300), check)

    def test_a_large_withdrawal_is_off_pace_the_day_it_posts(self):
        def check(a, days, sent, flips, taken):
            if sent != [taken] or flips != [(taken, False)]:
                return f"mails {sent}, changes {flips}, taken {taken}"
        each_plan(self, self._withdraws(3000), check)

    def test_only_withdrawals_is_off_pace_after_the_first_cycle(self):
        def build(a):
            td = a + dt.timedelta(days=400)
            nets = {d: -200.0 for d in FIRST if a < d <= td}
            return nets, 5000.0, 10000.0, td, td + dt.timedelta(days=10), None

        def check(a, days, sent, flips, info):
            if sent != [a + dt.timedelta(days=GRACE + 1)] or flips:
                return f"mails {sent}, changes {flips}"
        each_plan(self, build, check)


class TheTargetDateTests(unittest.TestCase):

    @staticmethod
    def _last_deposit(days_late):
        def build(a):
            start, target, td = on_plan(FIRST, a)
            nets = {d: v for d, v in FIRST.items() if a < d <= td}
            amount = nets.pop(td)
            if days_late is not None:
                nets[td + dt.timedelta(days=days_late)] = amount
            return (nets, start, target, td, td + dt.timedelta(days=20), td)
        return build

    def test_a_finishing_deposit_two_days_late_is_never_off_pace(self):
        def check(a, days, sent, flips, td):
            if sent or any(v is False for _, v in days):
                return f"off pace: mails {sent}"
        each_plan(self, self._last_deposit(2), check)

    def test_a_missed_date_is_off_pace_from_the_fourth_day_after(self):
        def check(a, days, sent, flips, td):
            fourth = td + dt.timedelta(days=savings.PACE_POSTING_DAYS + 1)
            if sent != [fourth] or flips != [(fourth, False)]:
                return f"mails {sent}, changes {flips}"
        each_plan(self, self._last_deposit(None), check)

    def test_a_date_at_the_end_of_the_calendar_does_not_overflow(self):
        def build(a):
            return ({a + dt.timedelta(days=10): 500.0}, 100.0, 1e6,
                    D(9999, 12, 31), a + dt.timedelta(days=400), None)

        def check(a, days, sent, flips, info):
            if sent or any(v is False for _, v in days):
                return "off pace"
            if days[-1][1] is not True:
                return f"reads {days[-1][1]!r} after the first cycle"
        each_plan(self, build, check)

    def test_a_date_already_past_when_set_is_off_pace_after_three_days(self):
        a = D(2026, 5, 4)
        days = read({}, a, 100.0, 5000.0, a - dt.timedelta(days=30),
                    a + dt.timedelta(days=5))
        self.assertEqual([v for _, v in days], [False] * 5)
        days = read({}, a, 100.0, 5000.0, a, a + dt.timedelta(days=5))
        self.assertEqual([v for _, v in days],
                         [None, None, None, False, False])

    def test_a_met_goal_has_no_verdict_until_money_is_taken_back_out(self):
        def build(a):
            nets = {a + dt.timedelta(days=10): 6500.0,
                    a + dt.timedelta(days=100): -2000.0}
            return (nets, 0.0, 6000.0, a + dt.timedelta(days=365),
                    a + dt.timedelta(days=380), None)

        def check(a, days, sent, flips, info):
            if any(v is not None for day, v in days
                   if 10 <= (day - a).days < 100):
                return "a verdict on a funded day"
            if len(sent) != 1 or sent[0] < a + dt.timedelta(days=100):
                return f"mails {sent}"
        each_plan(self, build, check)


class StatedLimitationsTests(unittest.TestCase):
    """What the schedule gets wrong, pinned so a change is deliberate: a
    saver who deposits less often than monthly, or at irregular sizes and
    gaps, is behind the straight line between deposits. Each entry is
    (percent of days read off pace, off-pace mails per plan of about eleven
    months) over every plan day, for a plan that needs the given share of
    what the saver puts in."""

    PINNED = {
        ("quarterly", 1.0): (31.7, 2.95),
        ("quarterly", 0.9): (18.1, 2.63),
        ("quarterly", 0.8): (8.7, 1.43),
        ("quarterly", 0.7): (6.1, 0.85),
        ("twice a year", 1.0): (54.0, 1.59),
        ("twice a year", 0.9): (49.3, 1.59),
        ("twice a year", 0.8): (43.2, 1.59),
        ("twice a year", 0.7): (35.5, 1.59),
        ("irregular", 1.0): (9.0, 1.3),
        ("irregular", 0.9): (1.3, 0.41),
        ("irregular", 0.8): (0.5, 0.19),
        ("irregular", 0.7): (0.2, 0.08),
    }

    def test_savers_slower_or_less_even_than_monthly(self):
        for (name, share), want in self.PINNED.items():
            with self.subTest(saver=name, plan_needs=share):
                self.assertEqual(tally(SLOWER[name], share), want)


if __name__ == "__main__":
    unittest.main()
