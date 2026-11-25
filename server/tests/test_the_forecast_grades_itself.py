"""The forecast grades itself (specs/forecast-grading.md, "How a forecast
is graded"): a night's prediction for day t is compared with night t's own
starting balance, only when both are fresh, the checking account is the
same and no restore lies between them; a missing night is rebuilt from the
next within three days and the posted rows between. The words round up to
plain figures, a lean is said only past its thresholds, and the band is
the misses' own 10th to 90th percentiles. Figures are invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import forecast_score as fs

from . import props

D = dt.date
H = fs.HORIZONS
DAY0 = D(2026, 1, 1)


def _night(day, start, preds=None, *, fresh=True, restored=False,
           acct="chk", model="w3"):
    at = [round(p * 100) for p in (preds or [start] * len(H))]
    return {"as_of": day, "model": model, "checking_id": acct,
            "start_checking": float(start), "fresh": fresh,
            "restored": restored,
            "walks": {"usual": {"at": at, "cards_out": [0] * 5},
                      "now": {"at": at, "cards_out": [0] * 5},
                      "due": {"at": at, "cards_out": [0] * 5}}}


def _history(days, rng, *, miss=0.0, spread=50.0):
    """Nights whose every prediction is the real balance h days later
    plus `miss` and a draw of noise: error = actual − predicted."""
    bal = {DAY0 + dt.timedelta(days=k): 3000 + 10 * k for k in range(days + 61)}
    out = []
    for k in range(days):
        d = DAY0 + dt.timedelta(days=k)
        preds = [round(bal[d + dt.timedelta(days=h)] - miss
                       + rng.uniform(-spread, spread), 2) for h in H]
        out.append(_night(d, bal[d], preds))
    return out


class EachPairIsGradedAgainstItsOwnNight(unittest.TestCase):
    def test_error_is_the_target_nights_start_less_the_prediction(self):
        nights = [_night(DAY0, 1000, [900, 800, 700, 600, 500])]
        nights += [_night(DAY0 + dt.timedelta(days=h), 1000 + h)
                   for h in H]
        pairs, skips = fs.graded_pairs(nights, DAY0 + dt.timedelta(days=61))
        mine = {p.h: p for p in pairs if p.as_of == DAY0}
        self.assertEqual(sorted(mine), list(H))
        self.assertEqual(mine[7].error, 1007 - 900)
        self.assertEqual(mine[60].actual, 1060)
        # the later nights' own targets were never recorded
        days = {r["as_of"] for r in nights}
        last = DAY0 + dt.timedelta(days=60)
        targets_missing = sum(
            1 for r in nights[1:] for h in H
            if (t := r["as_of"] + dt.timedelta(days=h)) <= last
            and t not in days)
        # a night never kept made no forecasts: its horizons count too
        sources_missing = sum(
            1 for k in range(61) for h in H
            if (d := DAY0 + dt.timedelta(days=k)) not in days
            and d + dt.timedelta(days=h) <= last)
        self.assertEqual(skips["offline"], targets_missing + sources_missing)
        self.assertEqual(skips["stale"] + skips["restore"]
                         + skips["account_changed"], 0)

    def test_not_due_until_the_target_night_has_been(self):
        nights = [_night(DAY0, 1000), _night(DAY0 + dt.timedelta(days=7), 9)]
        pairs, _ = fs.graded_pairs(nights, DAY0 + dt.timedelta(days=7))
        self.assertEqual(pairs, [])
        pairs, _ = fs.graded_pairs(nights, DAY0 + dt.timedelta(days=8))
        self.assertEqual([p.h for p in pairs], [7])

    def test_each_skip_rule(self):
        t = DAY0 + dt.timedelta(days=7)
        today = DAY0 + dt.timedelta(days=70)
        cases = {
            "stale": [_night(DAY0, 1, fresh=False), _night(t, 1)],
            "stale_target": [_night(DAY0, 1), _night(t, 1, fresh=False)],
            "account_changed": [_night(DAY0, 1), _night(t, 1, acct="sav")],
            "restore": [_night(DAY0, 1, restored=True), _night(t, 1)],
            "offline": [_night(DAY0, 1)],
        }
        for name, nights in cases.items():
            pairs, skips = fs.graded_pairs(nights, today)
            seven = [p for p in pairs if p.as_of == DAY0 and p.h == 7]
            self.assertEqual(seven, [], name)
            want = "stale" if name.startswith("stale") else name
            self.assertGreaterEqual(skips[want], 1, (name, skips))

    def test_a_missing_night_is_not_graded(self):
        """A night not kept is never rebuilt from the next one: that
        night's balance holds charges still pending that post dated after
        the missing day, so walking it back by the posted rows between is
        wrong whenever a hold straddles the gap."""
        t = DAY0 + dt.timedelta(days=7)
        for gap in (1, 3):
            nights = [_night(DAY0, 1000, [900] * 5),
                      _night(t + dt.timedelta(days=gap), 800)]
            pairs, skips = fs.graded_pairs(
                nights, DAY0 + dt.timedelta(days=90),
                moves=lambda *a: 25.0)
            self.assertEqual([p for p in pairs if p.as_of == DAY0 and
                              p.h == 7], [], gap)
            self.assertGreaterEqual(skips["offline"], 1)

    def test_restored_rows_are_graded_against_each_other(self):
        t = DAY0 + dt.timedelta(days=7)
        nights = [_night(DAY0, 1, [5] * 5, restored=True),
                  _night(t, 9, restored=True)]
        pairs, _ = fs.graded_pairs(nights, DAY0 + dt.timedelta(days=90))
        self.assertEqual([(p.h, p.error) for p in pairs], [(7, 4.0)])


class GradingIsAPureFunctionOfTheRows(unittest.TestCase):
    def test_generated_histories(self):
        for seed in props.seeds(6):
            rng = random.Random(seed)
            nights = _history(rng.randint(40, 160), rng,
                              miss=rng.uniform(-200, 200))
            for r in nights:
                if rng.random() < 0.1:
                    r["fresh"] = False
            today = nights[-1]["as_of"] + dt.timedelta(days=1)
            pairs, _ = fs.graded_pairs(nights, today)
            by = {r["as_of"]: r for r in nights}
            for p in pairs:
                self.assertEqual(p.error, round(p.actual - p.predicted, 2))
                self.assertEqual(p.actual, by[p.target]["start_checking"])
            base = {(p.as_of, p.h): p for p in pairs}
            # a stale or restored night added changes no other pair
            extra = _night(today - dt.timedelta(days=200), 1,
                           fresh=rng.random() < 0.5, restored=True)
            more, _ = fs.graded_pairs(nights + [extra], today)
            self.assertEqual({(x.as_of, x.h): x for x in more
                              if x.as_of != extra["as_of"]}, base)
            # dropping the oldest rows changes only the pairs that used them
            cut = nights[10:]
            few, _ = fs.graded_pairs(cut, today)
            gone = {r["as_of"] for r in nights[:10]}
            self.assertEqual(
                {(x.as_of, x.h): x for x in few},
                {k: v for k, v in base.items()
                 if k[0] not in gone and v.target not in gone})


class TheWords(unittest.TestCase):
    def test_plain_figures_round_up(self):
        for x, want in ((0.0, 10.0), (1.0, 10.0), (190.0, 190.0),
                        (191.0, 200.0), (201.0, 250.0), (999.0, 1000.0),
                        (1001.0, 1100.0)):
            self.assertEqual(fs.plain_up(x), want, x)

    def test_a_frequency_only_where_the_stretches_are_separate(self):
        """Ninety days of 30-day misses are four separate stretches: the
        frequency ("4 times in 5") is said for the longest horizon with
        INDEPENDENT_MIN of them (14 days here), and the 30-day forecast's
        typical (median) miss beside it."""
        rng = random.Random(3)
        nights = _history(130, rng, spread=100)
        today = nights[-1]["as_of"] + dt.timedelta(days=1)
        pairs, _ = fs.graded_pairs(nights, today)
        sc = fs.summarise(pairs, today)
        self.assertEqual(sc["horizon"], 14)
        self.assertGreaterEqual(sc["independent"], fs.INDEPENDENT_MIN)
        win0 = today - dt.timedelta(days=fs.WINDOW_DAYS)
        w14 = sorted(abs(p.error) for p in pairs if p.h == 14
                     and p.target > win0)
        self.assertEqual(sc["within"],
                         fs.plain_up(w14[int(0.8 * len(w14) + 0.5) - 1]))
        self.assertEqual(sc["typical_horizon"], 30)
        w30 = sorted(abs(p.error) for p in pairs if p.h == 30
                     and p.target > win0)
        self.assertEqual(sc["typical"], fs.plain_up(w30[len(w30) // 2]))
        self.assertLess(sc["typical_independent"], fs.INDEPENDENT_MIN)

    def test_the_span_rule_holds_back_a_single_month(self):
        rng = random.Random(4)
        nights = _history(70, rng)
        today = nights[-1]["as_of"] + dt.timedelta(days=1)
        pairs, _ = fs.graded_pairs(nights, today)
        sc = fs.summarise(pairs, today)
        # 30-day targets span only 40 days: no 30-day figure at all; the
        # 7-day horizon has the separate stretches for a frequency
        self.assertEqual(sc["horizon"], 7)
        self.assertEqual(sc["typical_horizon"], 14)
        short = fs.summarise(pairs[:0], today)
        self.assertIsNone(short["horizon"])
        self.assertIsNone(short["typical_horizon"])

    def test_a_lean_is_said_only_past_its_thresholds(self):
        for miss, said in ((300.0, True), (60.0, False), (-300.0, True)):
            rng = random.Random(5)
            nights = _history(130, rng, miss=miss, spread=40)
            today = nights[-1]["as_of"] + dt.timedelta(days=1)
            pairs, _ = fs.graded_pairs(nights, today)
            sc = fs.summarise(pairs, today, income=2000.0)
            self.assertEqual(sc["bias"] is not None, said, miss)
            if said:
                self.assertEqual(sc["bias"]["median"] > 0, miss > 0)
        # 5 % of a large income raises the bar
        nights = _history(130, random.Random(5), miss=300.0, spread=40)
        today = nights[-1]["as_of"] + dt.timedelta(days=1)
        pairs, _ = fs.graded_pairs(nights, today)
        self.assertIsNone(fs.summarise(pairs, today, income=9000.0)["bias"])

    def test_transfers_that_explain_the_misses_are_taken_out(self):
        rng = random.Random(6)
        nights = _history(130, rng, miss=0.0, spread=10)
        today = nights[-1]["as_of"] + dt.timedelta(days=1)
        pairs, _ = fs.graded_pairs(nights, today)
        # every night $500 went to savings the forecast did not plan
        moved = [fs.Pair(p.as_of, p.h, p.target, p.predicted,
                         p.actual - 500, round(p.error - 500, 2), False,
                         p.model) for p in pairs]
        tr = {(p.as_of, p.h): 500.0 for p in moved}
        plain = fs.summarise(moved, today)
        out = fs.summarise(moved, today, transfers=tr)
        self.assertGreaterEqual(plain["within"], 500)
        self.assertTrue(out["transfers_out"])
        self.assertLessEqual(out["within"], 20)


class OnlySeparateStretchesCount(unittest.TestCase):
    def test_a_lean_is_named_at_a_horizon_with_separate_stretches(self):
        rng = random.Random(5)
        nights = _history(130, rng, miss=300.0, spread=40)
        today = nights[-1]["as_of"] + dt.timedelta(days=1)
        pairs, _ = fs.graded_pairs(nights, today)
        sc = fs.summarise(pairs, today, income=2000.0)
        # sixty days hold about three separate 30-day stretches, nine
        # 7-day ones: the lean is the 7-day forecast's
        self.assertEqual(sc["bias"]["horizon"], 7)
        only30 = [p for p in pairs if p.h == 30]
        self.assertIsNone(fs.summarise(only30, today, income=2000.0)["bias"])

    def test_a_dropped_week_counts_every_forecast_it_lost(self):
        rng = random.Random(8)
        nights = _history(100, rng)
        gone = {DAY0 + dt.timedelta(days=40 + k) for k in range(7)}
        kept = [r for r in nights if r["as_of"] not in gone]
        today = nights[-1]["as_of"] + dt.timedelta(days=1)
        full, _ = fs.graded_pairs(nights, today)
        some, skips = fs.graded_pairs(kept, today)
        _all, base = fs.graded_pairs(nights, today)
        lost = len(full) - len(some)
        self.assertEqual(skips["offline"] - base["offline"], lost)
        sc = fs.summarise(some, today, first_night=DAY0,
                          recorded=len(kept))
        self.assertEqual(sc["history_days"], 93)


class MoneyMovedBetweenOwnAccounts(unittest.TestCase):
    def test_only_a_paired_transfer_is_the_households_own(self):
        from .util import add_txn, make_db
        conn = make_db()
        try:
            conn.execute("INSERT INTO accounts (id,item_id,name,type,"
                         "subtype,balance_current) VALUES ('sav','it1',"
                         "'Rainy Day Savings','depository','savings',100)")
            d = D(2026, 7, 10)
            # a payment to a friend, and an unrelated refund on the card of
            # the opposite amount: not money moved between own accounts
            add_txn(conn, d, 60.0, "TO A FRIEND", account="chk",
                    primary="TRANSFER_OUT", txn_id="friend")
            add_txn(conn, d, -60.0, "SHOP REFUND", account="card",
                    primary="GENERAL_MERCHANDISE", txn_id="refund")
            # to savings: both legs, one to one
            add_txn(conn, d, 500.0, "TO SAVINGS", account="chk",
                    primary="TRANSFER_OUT", txn_id="out-1")
            add_txn(conn, d + dt.timedelta(days=1), 500.0, "TO SAVINGS",
                    account="chk", primary="TRANSFER_OUT", txn_id="out-2")
            add_txn(conn, d + dt.timedelta(days=1), -500.0, "FROM CHECKING",
                    account="sav", primary="TRANSFER_IN", txn_id="in-1")
            own = fs.own_transfer_ids(conn, "chk", d - dt.timedelta(days=1),
                                      d + dt.timedelta(days=5))
            sums = fs._day_sums(conn, "chk", d - dt.timedelta(days=1),
                                d + dt.timedelta(days=5))
        finally:
            conn.close()
        # one savings leg pairs one checking leg (the nearer day)
        self.assertEqual(own, {"out-2"})
        self.assertEqual(sums[d][1], 0.0)
        self.assertEqual(sums[d + dt.timedelta(days=1)][1], 500.0)


class TheBand(unittest.TestCase):
    """The band pools every horizon's misses divided by the household's
    own growth h^a and takes their 5th and 95th percentiles, the last 90
    days counted twice (specs/forecast-grading.md, "The band")."""

    @staticmethod
    def _pairs(today, a=0.5, k=1.0, n=60, recent=0):
        """Misses that grow as k·h^a, alternating sign, n per horizon."""
        out = []
        for h in H:
            for j in range(n):
                t = today - dt.timedelta(days=1 + j)
                e = (1 if j % 2 else -1) * k * h ** a
                if j < recent:
                    e *= 3
                out.append(fs.Pair(t - dt.timedelta(days=h), h, t, 0.0, e,
                                   e, False, "w3"))
        return out

    def test_the_growth_is_the_households_own(self):
        today = D(2026, 7, 1)
        for a in (0.3, 0.5, 0.8, 1.0):
            self.assertAlmostEqual(fs.growth_power(self._pairs(today, a)),
                                   a, places=6)
        self.assertEqual(fs.growth_power(self._pairs(today, 1.6)), 1.0)
        self.assertEqual(fs.growth_power([]), fs.BAND_POWER_DEFAULT)

    def test_the_pool_scales_back_by_the_growth(self):
        today = D(2026, 7, 1)
        pool = fs.band_pool(self._pairs(today, 0.5, k=100.0), today)
        self.assertAlmostEqual(pool["z"][0], -100.0, places=6)
        self.assertAlmostEqual(pool["z"][1], 100.0, places=6)
        q = fs.band_quantiles(self._pairs(today, 0.5, k=100.0), today)
        self.assertEqual(q["30"], [round(-100 * 30 ** 0.5, 2),
                                   round(100 * 30 ** 0.5, 2)])
        offs = fs.offsets({"band": pool}, 60)
        self.assertAlmostEqual(offs[59][1], 100 * 60 ** 0.5, places=3)
        self.assertAlmostEqual(offs[0][0], -100.0, places=3)

    def test_recent_misses_count_twice(self):
        today = D(2026, 7, 1)
        # the newest third of the misses are three times as large
        plain = fs.band_pool(self._pairs(today, 0.5, n=60), today)
        recent = fs.band_pool(self._pairs(today, 0.5, n=60, recent=20),
                              today)
        self.assertGreater(recent["z"][1], plain["z"][1])

    def test_too_few_misses_keep_the_heavier_line(self):
        today = D(2026, 7, 1)
        few = self._pairs(today, n=5)
        self.assertIsNone(fs.band_pool(few, today))
        self.assertIsNone(fs.offsets({"band": None}, 60))
        self.assertIsNone(fs.offsets({"quantiles": {"30": [-1, 1]}}, 60))
        series = [((today + dt.timedelta(days=k)).isoformat(), 100.0)
                  for k in range(0, 61)]
        b = fs.band(series, {"band": {"z": [-10.0, 5.0], "power": 0.5}})
        self.assertEqual(b["lo"][4], (series[4][0], round(100 - 20.0, 2)))
        self.assertEqual(b["hi"][4], (series[4][0], round(100 + 10.0, 2)))


class TheBandsFloorWhileHistoryIsShort(unittest.TestCase):
    """While a horizon has fewer than BAND_FLOOR_INDEPENDENT separate
    stretches of history behind it, the band is never narrower than the
    heavier-month line's distance below the walk there (and as far above);
    past that, the household's own misses alone."""

    def test_the_floor(self):
        today = D(2026, 7, 1)
        series = [((today + dt.timedelta(days=k)).isoformat(), 1000.0)
                  for k in range(0, 61)]
        heavy = [(d, v - 10.0 * i) for i, (d, v) in enumerate(series)]
        pool = {"z": [-1.0, 1.0], "power": 0.5, "n": 300}
        # 60 days of history: day 5 has 11 stretches behind it, day 30 has 1
        b = fs.band(series, {"band": pool, "history_days": 60}, heavy=heavy)
        lo5, hi5 = b["lo"][5][1], b["hi"][5][1]
        self.assertAlmostEqual(lo5, 1000 - 5 ** 0.5, places=2)
        self.assertAlmostEqual(hi5, 1000 + 5 ** 0.5, places=2)
        self.assertEqual(b["lo"][30][1], 1000 - 300.0)
        self.assertEqual(b["hi"][30][1], 1000 + 300.0)
        # a long history: the misses alone at every day
        b = fs.band(series, {"band": pool, "history_days": 400}, heavy=heavy)
        self.assertAlmostEqual(b["lo"][30][1], 1000 - 30 ** 0.5, places=2)
        # no heavier line: the misses alone
        b = fs.band(series, {"band": pool, "history_days": 60})
        self.assertAlmostEqual(b["lo"][30][1], 1000 - 30 ** 0.5, places=2)


class TheBandStartsAtTheWalksStart(unittest.TestCase):
    """The walk's series starts at today, a balance already known: the
    band has no width there, and day k ahead is drawn with day k's
    width."""

    def test_today_has_no_width_and_day_k_is_day_k(self):
        today = D(2026, 7, 1)
        series = [((today + dt.timedelta(days=k)).isoformat(), 80.0)
                  for k in range(0, 61)]
        pool = {"z": [-100.0, 100.0], "power": 0.5, "n": 300}
        b = fs.band(series, {"band": pool, "history_days": 4000})
        self.assertEqual(b["lo"][0], (series[0][0], 80.0))
        self.assertEqual(b["hi"][0], (series[0][0], 80.0))
        self.assertEqual(b["lo"][1][1], -20.0)
        self.assertEqual(b["hi"][7][1], round(80.0 + 100 * 7 ** 0.5, 2))
        self.assertEqual(len(b["lo"]), len(series))


class TheBandsEdgesAreExact(unittest.TestCase):
    def test_edges_are_computed_unrounded_and_rounded_once(self):
        today = D(2026, 7, 1)
        pool = {"z": [-13.371234567, 21.123456789], "power": 0.61234567,
                "n": 300}
        series = [((today + dt.timedelta(days=k)).isoformat(), 2500.0)
                  for k in range(0, 61)]
        b = fs.band(series, {"band": pool, "history_days": 4000})
        for k in (1, 30, 60):
            self.assertEqual(b["lo"][k][1],
                             round(2500.0 + pool["z"][0] * k ** pool["power"],
                                   2))
            self.assertEqual(b["hi"][k][1],
                             round(2500.0 + pool["z"][1] * k ** pool["power"],
                                   2))

    def test_misses_that_never_moved_give_the_heavier_lines_width(self):
        today = D(2026, 7, 1)
        series = [((today + dt.timedelta(days=k)).isoformat(), 900.0)
                  for k in range(0, 61)]
        heavy = [(d, v - 5.0 * i) for i, (d, v) in enumerate(series)]
        b = fs.band(series, {"band": {"z": [0.0, 0.0], "power": 0.5,
                                      "n": 300}, "history_days": 4000},
                    heavy=heavy)
        self.assertEqual(b["lo"][10][1], 850.0)
        self.assertEqual(b["hi"][10][1], 950.0)

    def test_a_payee_saying_savings_is_a_bill(self):
        self.assertEqual(fs._event_kind("Lakeside Savings Bank", -40.0),
                         "bill")
        self.assertEqual(fs._event_kind("Rainy day (savings plan)", -100.0),
                         "sweep")
        self.assertEqual(fs._event_kind("Rainy day (sweep savings)", -60.0),
                         "sweep")


class TheStoredScore(unittest.TestCase):
    """The record grades as of its night and stores it; a night missing
    between two kept ones is rebuilt from the checking account's posted
    rows; the card payments that really left are compared with each
    scenario's."""

    def test_kept_nights_in_the_database(self):
        import json
        from .util import add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            rng = random.Random(9)
            nights = _history(120, rng, spread=60)
            gone = nights[50]["as_of"]
            for r in nights:
                if r["as_of"] == gone:
                    continue
                for k in ("usual", "now", "due"):
                    r["walks"][k]["cards_out"] = [0, 0, 40000, 40000, 40000]
                conn.execute(
                    "INSERT INTO forecast_nights (as_of, model, checking_id,"
                    " start_checking, fresh, walks, pace) VALUES "
                    "(%s, 'w3', 'chk', %s, true, %s::jsonb, '{}')",
                    (r["as_of"], r["start_checking"], json.dumps(r["walks"])))
            # the next night's balance less $40 spent the day after the
            # missing one: the rebuilt balance is $40 higher
            add_txn(conn, gone + dt.timedelta(days=1), 40.0, "CORNER SHOP",
                    account="chk")
            for k in range(0, 120, 7):
                add_txn(conn, DAY0 + dt.timedelta(days=k), 50.0,
                        "PAYMENT THANK YOU", account="chk",
                        primary="LOAN_PAYMENTS",
                        detailed=fs.CARD_PAYMENT)
            today = nights[-1]["as_of"] + dt.timedelta(days=1)
            sc = fs.compute_score(conn, today)
            self.assertEqual(sc["horizon"], 14)
            self.assertEqual(sc["typical_horizon"], 30)
            self.assertLessEqual(sc["within"], 70.0)
            self.assertIsNotNone(sc["quantiles"]["30"])
            self.assertIsNotNone(sc["card_paying"])
            # real card payments ($50 a week) ran under the usual $400
            self.assertLess(sc["card_paying"]["usual_gap"], 0)
            led = fs._Ledger(conn, nights, today)
            pairs, _ = fs.graded_pairs(rows_from(conn), today,
                                       moves=led.moves)
            # the night not kept grades nothing that points at it
            self.assertEqual([p for p in pairs if p.target == gone], [])
            # the newest night's stored score is what current() reads
            conn.execute("UPDATE forecast_nights SET score = %s::jsonb "
                         "WHERE as_of = %s",
                         (json.dumps(dict(sc, as_of=today.isoformat())),
                          nights[-1]["as_of"]))
            self.assertEqual(fs.current(conn, today)["within"], sc["within"])
        finally:
            conn.close()


def rows_from(conn):
    return fs.rows(conn)


if __name__ == "__main__":
    unittest.main()
