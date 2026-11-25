"""The Envelopes row is one breakdown of the month's pools
(specs/today-figures.md, "Envelopes are their own row";
web/envelope_breakdown.py): a pool gauge, never red from a pool, "over"
only past the pool, and the Year read from the pool itself.

Every figure here is made up; the month entries are the shape
month_status gives `fixed.envelopes`.
"""

from __future__ import annotations

import random
import unittest

from oikonome.engine.reporting import whole_dollars
from oikonome.web import envelope_breakdown as eb

from . import props


def env(payee, pool, months=12, *, used=0.0, prior=0.0, overflow=0.0,
        uid=None):
    """One pool's month: the matcher caps `used` at what is left of the
    pool after `prior` (the year's earlier draws), the rest is
    overflow."""
    return {"payee": payee, "uid": uid or f"uid-{payee.lower()}",
            "pool": pool, "period_months": months, "monthly": pool / months,
            "used": used, "overflow": overflow,
            "period_used": prior + used,
            "period_left": max(0.0, pool - prior - used),
            "show_today": False}


def fixed(envs, occ_budget=0.0):
    return {"envelopes": envs, "occ_budget": occ_budget,
            "month_budget": occ_budget + sum(e["monthly"] for e in envs)}


def month(envs, kind="closed", m=6, *, occ=0.0, reserve=0):
    return eb.month(fixed(envs, occ), kind=kind, y=2026, m=m,
                    reserve=reserve)


def printed(bd):
    """The row as a client prints it: (left figure, right figure, words,
    tone, caption)."""
    r = eb.row(bd, to=None)
    st = r["status"] or {}
    return (r["actual"], r["budget"], st.get("text"), st.get("tone"),
            r["caption"])


class AMonthTests(unittest.TestCase):

    def test_an_untouched_yearly_pool_keeps_its_whole_pool(self):
        left, right, words, _tone, cap = printed(
            month([env("Garden", 960.0)]))
        self.assertEqual((left, right, words), (0.0, 960.0, None))
        # the plan's set-aside is the list's total line, not the caption
        # nothing still expected: no caption
        self.assertIsNone(cap)
        self.assertEqual(eb.table(month([env("Garden", 960.0)]))["total"]
                         ["figure"], "$80")

    def test_an_untouched_pool_mid_year_says_nothing(self):
        bd = month([env("Garden", 960.0)], kind="running", m=7)
        self.assertEqual(printed(bd)[:3], (0.0, 960.0, None))

    def test_a_month_that_draws_heavily_is_a_full_bar_never_over(self):
        bd = month([env("Garden", 960.0, used=410.0)], m=5)
        left, right, words, tone, _ = printed(bd)
        self.assertEqual((left, right, words), (410.0, 960.0, None))
        nxt = month([env("Garden", 960.0, prior=410.0)], m=6)
        self.assertEqual(printed(nxt)[:2], (0.0, 550.0))

    def test_a_pool_paid_in_december_is_never_over(self):
        bd = month([env("Garden", 960.0, used=960.0)], m=12)
        left, right, words, tone, _ = printed(bd)
        self.assertEqual((left, right, words), (960.0, 960.0, None))

    def test_spend_past_the_pool_is_spending_and_the_only_over(self):
        bd = month([env("Garden", 960.0, used=60.0, prior=900.0,
                        overflow=90.0)], m=12)
        left, right, words, tone, _ = printed(bd)
        self.assertEqual((left, right), (60.0, 60.0))
        # the row's period said: a closed month's own month
        self.assertEqual(words,
                         "$90 past the pool in December, counted in spending")
        self.assertEqual(tone, "neg")
        self.assertEqual(eb.row(bd, to=None)["status"]["kind"], "over")

    def test_a_monthly_pool_left_unspent_is_said_once_the_month_closes(self):
        closed = month([env("Pet care", 85.0, months=1, used=62.0)])
        self.assertEqual(printed(closed)[:3], (62.0, 85.0, "$23 unspent"))
        run = month([env("Pet care", 85.0, months=1, used=62.0)],
                     kind="running", reserve=23)
        # still expected to month end: the caption's one figure
        self.assertEqual(printed(run)[:3], (62.0, 85.0, None))
        self.assertEqual(printed(run)[4], "$23 still expected this month · "
                                          "not counted in the verdict")

    def test_two_pools_one_spent_one_untouched_are_one_row_of_both(self):
        bd = month([env("Garden", 960.0, used=410.0),
                    env("Pet care", 85.0, months=1)])
        self.assertEqual(printed(bd)[:2], (410.0, 1045.0))
        rows = eb.table(bd)["rows"]
        self.assertEqual([r["label"] for r in rows],
                         ["Garden (yearly pool $960)",
                          "Pet care (monthly pool $85)"])
        self.assertEqual(rows[0]["text"],
                         "used $410 of $960 by the end of June · $410 in "
                         "June · $550 left")
        self.assertEqual(eb.table(bd)["total"]["figure"], "$165")

    def test_the_set_aside_and_the_bills_plan_add_up_to_the_plan(self):
        f = fixed([env("Garden", 1000.0)], 120.30)
        # 120.30 + 83.333… = 203.633…: the plan prints $204, the bills
        # plan $120, so the set-aside is the rest as printed
        self.assertEqual(eb.set_aside_dollars(f), 84)

    def test_the_set_aside_reads_the_plan_in_the_cents_it_is_sent_in(self):
        """A plan whose binary sum lands on a half cent (3943.495 times
        100 is exactly 394349.5) is sent as 3943.49 and printed $3,943:
        the set-aside makes the Month card's whole plan that figure, not
        a dollar more."""
        f = {"envelopes": [], "occ_budget": 3713.01,
             "month_budget": 3943.495}
        self.assertEqual(3713 + eb.set_aside_dollars(f),
                         whole_dollars(round(f["month_budget"], 2)))

    def test_a_named_pool_on_a_month_says_only_its_own_month(self):
        """On a month surface a pool prints main's pair, its period said,
        and only the month's own spill: no other month is read."""
        run = month([env("Gifts", 1000.0, used=0.0, prior=961.0,
                         overflow=0.0)], kind="running", m=7)
        self.assertEqual(eb.pool_line(run["items"][0]),
                         "used $961 of $1,000 this year")
        over = month([env("Gifts", 1000.0, used=39.0, prior=961.0,
                          overflow=15.0)], kind="running", m=7)
        self.assertEqual(eb.pool_line(over["items"][0]),
                         "used $1,000 of $1,000 this year · $15 past the "
                         "pool this month, counted in spending")
        june = month([env("Gifts", 1000.0, used=24.0, prior=800.0,
                          overflow=15.0)], kind="closed", m=6)
        self.assertEqual(eb.pool_line(june["items"][0]),
                         "used $824 of $1,000 by the end of June · $15 past "
                         "the pool in June, counted in spending")
        self.assertEqual(printed(june)[2],
                         "$15 past the pool in June, counted in spending")
        pet = month([env("Pet care", 85.0, months=1, used=40.0)],
                    kind="running", m=7)
        self.assertEqual(eb.pool_line(pet["items"][0]),
                         "$40 of $85 this month")

    def test_a_pool_cut_mid_year_is_the_years_own_resize_line(self):
        """$1,000 a year cut to $300 in May, $150 a month at its merchant:
        the Year reads its own months — $1,050 spent, May to July's $450
        counted in spending — and the resize is its own signed line."""
        ms = []
        for m in range(1, 8):
            pool = 1000.0 if m < 5 else 300.0
            prior = 150.0 * (m - 1)
            take = min(150.0, max(0.0, pool - prior))
            ms.append(month([env("Gifts", pool, used=take, prior=prior,
                                 overflow=150.0 - take)],
                            kind="running" if m == 7 else "closed", m=m))
        july = ms[-1]
        # main's pair (this month's spill is not in its used figure)
        self.assertEqual(eb.pool_line(july["items"][0]),
                         "used $900 of $300 this year · $150 past the "
                         "pool this month, counted in spending")
        yr = eb.year(ms)
        self.assertEqual(eb.pool_line(yr["items"][0]),
                         "$1,050 spent in 2026 · pool $300 · $450 past the "
                         "pool in 2026, counted in spending")
        self.assertEqual(printed(yr)[2],
                         "$450 past the pool in 2026, counted in spending")
        self.assertIn("Gifts: pool resized during the year",
                      [o["label"] for o in eb.table(yr)["outside"]])
        self.assertNotIn("was", " ".join(r["text"] for r in
                                         eb.table(yr)["rows"]))

    def test_a_pool_made_again_is_a_line_of_its_own_on_the_year(self):
        """Deleted in April and made again in May under the same name: a
        different pool (another id), never the old one's months."""
        ms = [month([env("Gifts", 400.0, used=130.0, prior=130.0 * (m - 1),
                         uid="old")], m=m) for m in range(1, 4)]
        ms.append(month([], m=4))
        ms += [month([env("Gifts", 500.0, used=130.0, prior=130.0 * (m - 5),
                          uid="new")], kind="running" if m == 6
                     else "closed", m=m) for m in (5, 6)]
        yr = eb.year(ms)
        lines = {it["uid"]: eb.pool_line(it) for it in yr["items"]}
        self.assertEqual(lines, {
            "old": "$390 spent in 2026 · pool $400",
            "new": "$260 spent in 2026 · pool $500"})
        self.assertEqual(sum(it["drawn"] for it in yr["items"]), 650)

    def test_several_pools_past_their_amounts_are_counted_by_pool(self):
        bd = month([env("Trips", 600.0, months=1, used=600.0,
                        overflow=40.0),
                    env("Tools", 90.0, months=1, used=90.0, overflow=25.0),
                    env("Books", 50.0, months=1, used=10.0)],
                   kind="running")
        self.assertEqual(printed(bd)[2],
                         "2 pools past their amounts this month, $65 "
                         "counted in spending")

    def test_a_closed_months_unspent_is_its_monthly_pools(self):
        bd = month([env("Garden", 960.0, used=100.0),
                    env("Pet care", 85.0, months=1, used=60.0),
                    env("Books", 50.0, months=1, used=20.0)], m=6)
        self.assertEqual(printed(bd)[2], "$55 of monthly pools unspent")

    def test_the_plan_splits_to_the_cent_and_as_printed(self):
        # a plan of $1,234.50 whose bills are $1,000.50: whole dollars
        # alone (1,000 + 234) miss the cents and print 1,234 — not the
        # plan's printed $1,234
        b, e = eb.plan_split(1234.50, 1000, 234, 1000.50)
        self.assertEqual(round(b + e, 2), 1234.50)
        self.assertEqual((whole_dollars(b), whole_dollars(e)), (1000, 234))
        for planned, occ in ((812.37, 400.49), (100.5, 99.5), (0.0, 0.0),
                             (3943.495, 3000.0), (50.0, 0.0)):
            bd = whole_dollars(occ)
            sd = whole_dollars(round(planned, 2)) - bd
            b, e = eb.plan_split(planned, bd, sd, occ)
            with self.subTest(planned=planned, occ=occ):
                self.assertEqual(int(round(b * 100)) + int(round(e * 100)),
                                 int(round(planned * 100)))
                self.assertEqual((whole_dollars(b), whole_dollars(e)),
                                 (bd, sd))

    def test_a_month_with_no_pool_has_no_row(self):
        self.assertFalse(eb.has_row(month([])))


def _year(months, past=None):
    return eb.year(months, past=past)


class TheYearTests(unittest.TestCase):

    def test_a_finished_year_with_an_untouched_pool_reads_it_unspent(self):
        ms = [month([env("Garden", 960.0)], m=m) for m in range(1, 13)]
        bd = _year(ms, past=True)
        self.assertEqual(printed(bd)[:3], (0.0, 960.0, "$960 unspent"))
        self.assertNotIn("plan", printed(bd)[2])

    def test_a_pool_spent_in_march_reads_its_spend_and_the_rest(self):
        ms = []
        for m in range(1, 13):
            ms.append(month([env("Garden", 960.0,
                                 used=315.0 if m == 3 else 0.0,
                                 prior=315.0 if m > 3 else 0.0)], m=m))
        bd = _year(ms, past=True)
        self.assertEqual(printed(bd)[:3], (315.0, 960.0, "$645 unspent"))

    def test_a_running_year_shows_what_is_left(self):
        ms = [month([env("Garden", 960.0, used=210.0 if m == 2 else 0.0,
                         prior=210.0 if m > 2 else 0.0)], m=m)
              for m in range(1, 7)]
        ms.append(month([env("Garden", 960.0, prior=210.0)],
                        kind="running", m=7, reserve=24))
        bd = _year(ms, past=False)
        self.assertEqual(printed(bd)[:3], (210.0, 960.0, None))
        self.assertTrue(printed(bd)[4].startswith(
            "$24 still expected this month"))

    def test_a_pool_spent_in_full_reads_full_never_over_by_rounding(self):
        # a share of $1,000 / 12 rounds in every month: the Year reads the
        # pool, not the rounded shares
        ms = [month([env("Garden", 1000.0, used=1000.0 if m == 8 else 0.0,
                         prior=1000.0 if m > 8 else 0.0)], m=m)
              for m in range(1, 13)]
        bd = _year(ms, past=True)
        left, right, words, tone, cap = printed(bd)
        self.assertEqual((left, right, words), (1000.0, 1000.0, None))
        # nothing still expected: no caption
        self.assertIsNone(cap)

    def test_spend_past_the_pool_on_the_year_is_the_months_overflow(self):
        ms = [month([env("Garden", 960.0, used=960.0 if m == 6 else 0.0,
                         prior=960.0 if m > 6 else 0.0,
                         overflow=140.0 if m == 9 else 0.0)], m=m)
              for m in range(1, 13)]
        bd = _year(ms, past=True)
        self.assertEqual(printed(bd)[:4],
                         (960.0, 960.0,
                          "$140 past the pool in 2026, counted in spending",
                          "neg"))

    def test_a_plan_begun_mid_year_reads_its_own_months(self):
        ms = [month([env("Garden", 960.0, used=150.0 if m == 9 else 0.0,
                         prior=330.0 + (150.0 if m > 9 else 0.0))], m=m)
              for m in range(7, 13)]
        bd = _year(ms, past=True)
        self.assertEqual(printed(bd)[:2], (150.0, 630.0))
        out = eb.table(bd)["outside"]
        self.assertEqual([(o["label"], o["figure"]) for o in out],
                         [("Garden: spent before the plan's months", "+$330")])
        self.assertNotIn("outside", printed(bd)[2] or "")

    def test_a_pool_kept_then_spent_settles_with_no_over(self):
        ms = [month([env("Garden", 960.0,
                         used=960.0 if m == 11 else 0.0,
                         prior=960.0 if m == 12 else 0.0)], m=m)
              for m in range(1, 13)]
        self.assertEqual(printed(_year(ms, past=True))[:3],
                         (960.0, 960.0, None))

    def test_a_deleted_pool_has_nothing_left_and_says_so(self):
        ms = [month([env("Garden", 960.0, used=105.0 if m == 1 else 0.0,
                         prior=105.0 if m > 1 else 0.0)], m=m)
              for m in range(1, 4)]
        ms += [month([], m=m) for m in range(4, 7)]
        bd = _year(ms, past=True)
        self.assertEqual(printed(bd)[:2], (105.0, 105.0))
        self.assertIn("(no longer tracked)", eb.table(bd)["rows"][0]["label"])

    def test_a_pool_cut_below_what_it_drew_has_nothing_left(self):
        ms = [month([env("Garden", 960.0, used=510.0)], m=1),
              month([env("Garden", 430.0, prior=510.0)], m=2)]
        bd = _year(ms, past=True)
        self.assertEqual(printed(bd)[:2], (510.0, 510.0))
        out = eb.table(bd)["outside"]
        self.assertEqual([(o["label"], o["figure"]) for o in out],
                         [("Garden: pool resized during the year", "−$80")])

    def test_a_pool_removed_says_it_was_left_unspent_never_used(self):
        """$420 pool set up in March, $57 drawn, deleted in April: the
        $363 it still held is said as left unspent, never as spent."""
        ms = [month([], m=m) for m in (1, 2)]
        ms.append(month([env("Shed", 420.0, used=57.0)], m=3))
        ms += [month([], m=m) for m in range(4, 13)]
        bd = _year(ms, past=True)
        self.assertEqual([(o["label"], o["figure"])
                          for o in eb.table(bd)["outside"]],
                         [("Shed: left unspent in a pool that was removed",
                           "+$363")])
        self.assertEqual(printed(bd)[:2], (57.0, 57.0))

    def test_a_pool_tracked_late_says_it_spent_before_it_was_set_up(self):
        ms = [month([], m=m) for m in (1, 2, 3)]
        ms.append(month([env("Shed", 420.0, used=20.0, prior=110.0)], m=4))
        bd = _year(ms, past=True)
        self.assertEqual([(o["label"], o["figure"])
                          for o in eb.table(bd)["outside"]],
                         [("Shed: spent before the pool was set up", "+$110")])

    def test_a_month_outside_the_plan_between_two_is_said_so(self):
        ms = [month([env("Shed", 420.0, used=20.0)], m=1),
              month([env("Shed", 420.0, used=10.0, prior=95.0)], m=3)]
        bd = _year(ms, past=True)
        self.assertEqual([(o["label"], o["figure"])
                          for o in eb.table(bd)["outside"]],
                         [("Shed: spent in months outside the plan", "+$75")])

    def test_a_monthly_pool_on_the_year_says_each_months_spill(self):
        """A monthly pool's Year line: its months' spend, their pools
        (the running month's said apart) and the spill the months
        actually counted — a month under its pool never nets another
        month's spill away."""
        spend = {1: 120.0, 2: 50.0, 3: 60.0, 4: 40.0, 5: 30.0, 6: 30.0}
        ms = [month([env("Snacks", 60.0, months=1, used=min(60.0, c),
                         overflow=max(0.0, c - 60.0))],
                    kind="running" if m == 6 else "closed", m=m)
              for m, c in spend.items()]
        yr = eb.year(ms)
        it = yr["items"][0]
        self.assertEqual(eb.pool_line(it),
                         "$330 spent over 6 months · pools $300 through May"
                         " + $60 this month · $60 of it past a month's pool,"
                         " counted in spending")
        self.assertEqual(printed(yr)[2],
                         "$60 past the pool in 2026, counted in spending")

    def test_a_pool_named_alone_prints_its_own_pair(self):
        y = month([env("Garden", 960.0, used=40.0, prior=370.0)], m=6)
        self.assertEqual(eb.pool_line(y["items"][0]),
                         "used $410 of $960 by the end of June")
        mo = month([env("Pet care", 85.0, months=1, used=40.0)], m=6)
        self.assertEqual(eb.pool_line(mo["items"][0]),
                         "$40 of $85 in June")

    def test_a_row_with_nothing_in_it_is_not_drawn(self):
        self.assertFalse(eb.has_row(month([env("Garden", 960.0,
                                                prior=960.0)])))

    def test_a_renamed_pool_is_one_pool_by_its_id(self):
        a = env("Garden", 960.0, used=105.0, uid="u-1")
        b = dict(env("Garden plot", 960.0, prior=105.0, uid="u-1"))
        bd = _year([month([a], m=1), month([b], m=2)], past=True)
        self.assertEqual(len(bd["items"]), 1)
        self.assertEqual(printed(bd)[:2], (105.0, 960.0))

    def test_a_month_frozen_without_ids_matches_by_name(self):
        a = dict(env("Garden", 960.0, used=105.0), uid=None)
        b = env("Garden", 960.0, prior=105.0, uid="u-9")
        bd = _year([month([a], m=1), month([b], m=2)], past=True)
        self.assertEqual(len(bd["items"]), 1)


# ---- properties over simulated years ---------------------------------------

def simulate(rng: random.Random):
    """A year of 1–4 pools under the matcher's rule (each charge takes
    what the pool has left, the rest is overflow; a yearly pool's prior
    use is the year's earlier claims, uncapped), over a plan that may
    begin late, with pools set up, resized or deleted mid-year, read on
    a running month or after the year. Returns (month breakdowns, the
    pools' truth)."""
    n_pools = rng.randint(1, 4)
    run_m = rng.choice((None, rng.randint(1, 12)))
    first = rng.choice((1, 1, rng.randint(1, 12)))
    last = run_m or 12
    first = min(first, last)
    pools = []
    for i in range(n_pools):
        months = rng.choice((1, 12))
        p = {"payee": f"Pool {i}", "uid": f"u{i}", "months": months,
             "pool": round(rng.uniform(20, 3000), rng.choice((0, 2))),
             "from": rng.choice((1, 1, rng.randint(1, 12))),
             "resize": rng.choice((None, None, rng.randint(1, 12))),
             "gone": rng.choice((None, None, None, rng.randint(2, 12))),
             "prior": 0.0, "used_months": {}, "pool_at": {},
             "charged_to": {}, "charged_in": {}, "hist": [],
             "over_in": {}}
        p["new"] = round(p["pool"] * rng.uniform(0.2, 1.8), 2)
        pools.append(p)
    out = []
    for m in range(1, last + 1):
        envs = []
        for p in pools:
            if m < p["from"] or (p["gone"] and m >= p["gone"]):
                continue
            pool = p["new"] if p["resize"] and m >= p["resize"] else p["pool"]
            prior = p["prior"] if p["months"] > 1 else 0.0
            used = over = 0.0
            p["charged_in"][m] = 0.0
            for _ in range(rng.choice((0, 0, 1, 2, 3))):
                c = round(rng.uniform(1, pool * rng.choice((0.2, 0.7, 1.5))),
                          2)
                take = min(c, max(0.0, pool - prior - used))
                used += take
                over += c - take
                p["prior"] += c
                p["charged_in"][m] += c
            envs.append(dict(env(p["payee"], pool, p["months"], used=used,
                                 prior=prior, overflow=over, uid=p["uid"])))
            p["hist"].append({"m": m, "pool": pool, "used": used,
                              "overflow": over})
            p["over_in"][m] = over
            p["used_months"][m] = used
            p["pool_at"][m] = pool
            # everything the pool's year has charged so far
            p["charged_to"][m] = p["prior"]
        if m >= first:
            kind = "running" if m == run_m else "closed"
            bd = eb.month(fixed(envs, rng.choice((0.0, 812.37))), kind=kind,
                          y=2026, m=m,
                          reserve=rng.randint(0, 90) if kind == "running"
                          else 0)
            out.append(bd)
    return out, pools, run_m


def _older_phone_red(row) -> bool:
    """The store phone's Track: an unjudged row is red past its budget."""
    return row["actual"] > row["budget"] + 0.5


class PropertyTests(unittest.TestCase):

    def test_never_red_unless_overflow_and_the_row_is_its_list(self):
        n = 30_000 if props.LONG else 3_000
        base = props.run_seed() or 7
        for i in range(n):
            rng = random.Random(base * 1_000_003 + i)
            months, _pools, run_m = simulate(rng)
            bds = [bd for bd in months if eb.has_row(bd)]
            yr = eb.year(months, past=run_m is None)
            for bd in bds + ([yr] if yr else []):
                r = eb.row(bd, to=None)
                with self.subTest(i=i, kind=bd["kind"], m=bd.get("m")):
                    self.assertFalse(_older_phone_red(r))
                    neg = (r["status"] or {}).get("tone") == "neg"
                    self.assertEqual(neg, bd["overflow"] >= 1)
                    self.assertEqual(r["actual"],
                                     sum(it["drawn"] for it in bd["items"]))
                    self.assertEqual(r["budget"],
                                     sum(it["cap"] for it in bd["items"]))
                    for word in ("under plan", "over plan", "ahead"):
                        self.assertNotIn(word, (r["status"] or {}).get(
                            "text") or "")

    def test_a_named_pool_on_a_month_prints_that_months_spill(self):
        """On a month surface: main's pair (envelope_shown), its period
        said, and "past the pool" exactly the month's own overflow for the
        pool — nothing when there was none; on the Year a yearly pool's
        spill is its months' overflow summed. A row is its pools."""
        n = 30_000 if props.LONG else 3_000
        base = props.run_seed() or 13
        for i in range(n):
            rng = random.Random(base * 1_000_003 + i)
            months, pools, run_m = simulate(rng)
            for bd in months:
                for it in bd["items"]:
                    p = next(p for p in pools if p["uid"] == it["uid"])
                    words = eb.pool_line(it)
                    when = ("this month" if bd["kind"] == "running"
                            else f"in {eb._MONTH_NAMES[bd['m'] - 1]}")
                    with self.subTest(i=i, m=bd["m"], pool=it["payee"]):
                        self.assertEqual(
                            (it["pair_spent"], it["pair_of"]),
                            (it["shown_used"], it["shown_of"]))
                        self.assertEqual(it["pair_past"], whole_dollars(
                            p["over_in"][bd["m"]]))
                        self.assertEqual("past the pool" in words,
                                         it["pair_past"] > 0)
                        if it["pair_past"] > 0:
                            self.assertIn(f"${it['pair_past']:,} past the "
                                          f"pool {when}", words)
                        for w in ("this year" if bd["kind"] != "running"
                                  else None, "was $", "until"):
                            if w:
                                self.assertNotIn(w, words)
                self.assertEqual(bd["overflow"],
                                 sum(it["overflow"] for it in bd["items"]))
            yr = eb.year(months, past=run_m is None)
            for it in (yr or {}).get("items") or ():
                p = next(p for p in pools if p["uid"] == it["uid"])
                if it["period_months"] > 1:
                    shown = [mo for mo in p["hist"]
                             if any(x["uid"] == it["uid"]
                                    for b2 in months if b2["m"] == mo["m"]
                                    for x in b2["items"])]
                    with self.subTest(i=i, year=it["payee"]):
                        self.assertEqual(it["pair_past"], whole_dollars(
                            sum(mo["overflow"] for mo in shown)))

    def test_the_year_reads_the_pool(self):
        n = 30_000 if props.LONG else 3_000
        base = props.run_seed() or 11
        for i in range(n):
            rng = random.Random(base * 1_000_003 + i)
            months, pools, run_m = simulate(rng)
            yr = eb.year(months, past=run_m is None)
            if yr is None:
                continue
            first = min(bd["m"] for bd in months)
            for it in yr["items"]:
                p = next(p for p in pools if p["uid"] == it["uid"])
                used = sum(u for m, u in p["used_months"].items()
                           if m >= first)
                with self.subTest(i=i, pool=it["payee"]):
                    self.assertEqual(it["drawn"], whole_dollars(used))
                    # what the Year did not draw is said by cause, the
                    # causes adding up to it as printed
                    self.assertEqual(sum(d for _l, d in it["causes"]),
                                     it["outside"])
                    if it["gone"] and it["causes"]:
                        self.assertNotIn(eb.BEFORE_SET_UP, [
                            lab for lab, d in it["causes"] if d < 0])
                    if it["period_months"] > 1:
                        latest = p["pool_at"][max(m for m in p["pool_at"]
                                                  if m >= first)]
                        self.assertEqual(it["drawn"] + it["left"]
                                         + it["outside"],
                                         whole_dollars(latest))
                        steady = (p["resize"] is None and p["from"] <= first
                                  and not p["gone"])
                        if steady and abs(used - p["pool"]) < 0.005:
                            self.assertNotIn("over", yr["text"] or "")
                            self.assertEqual(it["cap"], it["drawn"])


if __name__ == "__main__":
    unittest.main()
