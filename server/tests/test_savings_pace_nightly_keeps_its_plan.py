"""The nightly sweep judges a dated savings goal against its schedule and
mails once when it falls off pace (`specs/savings-pace.md`). Here the real
functions run night after night on an invented ledger, with the stored state
carried from one night to the next the way the sweep carries it: the plan
each goal is judged by has to survive a rename, an undelivered mail and a
restore, start again on an edit, and begin without a mail for a goal that
was being saved toward before plans were kept."""

import datetime as dt
import re
import pathlib
import unittest
from unittest import mock

from oikonome.engine import budget, savings
from oikonome.jobs import worker
from oikonome.web import report

from .test_savings_pace_is_read_against_the_schedule import (
    FIRST, REGULAR, on_plan)
from .util import add_txn, make_db, write_config

D = dt.date
GRACE = savings.PACE_GRACE_DAYS
RELAY = {"host": "smtp.example.dev", "configured": True}


def days(n):
    return dt.timedelta(days=n)


class NightlyCase(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")

    def tearDown(self):
        self.conn.close()

    def goal(self, target, td, start=0.0, **over):
        g = {"name": "cushion", "target": target, "monthly_plan": 500.0,
             "account_id": "sav", "tokens": [], "start_balance": start,
             "target_date": td.isoformat()}
        g.update(over)
        return g

    def post(self, day, amount, name="TO SAVINGS"):
        """A transfer into the savings account (or out of it, negative)."""
        add_txn(self.conn, day, -amount, name, account="sav",
                primary="TRANSFER_IN" if amount > 0 else "TRANSFER_OUT")

    def nights(self, cfg, first, last, stream=None):
        """Run the sweep on each night first..last, posting the stream's
        rows on their own day. Returns [(day, stored verdict, off-pace
        messages)]; cfg carries the state forward."""
        out, day = [], first
        while day <= last:
            if stream and stream.get(day):
                self.post(day, stream[day])
            msgs, state = savings.check_milestones(self.conn, cfg, day)
            cfg["savings_goal_state"] = state
            out.append((day, state["cushion"]["on_pace"],
                        [m for m in msgs if "off pace" in m]))
            day += days(1)
        return out

    def on_plan_run(self, stream, plan_day, extra=None, until=None):
        start, target, td = on_plan(stream, plan_day)
        cfg = {"savings_goals": [self.goal(target, td, start)]}
        rows = {d: v for d, v in stream.items() if plan_day < d <= td}
        if extra:
            extra(rows, td)
        return cfg, td, self.nights(cfg, plan_day, until or td, rows)


class RegularSaversNightAfterNightTests(NightlyCase):

    def _never_off(self, stream, plan_day, extra=None):
        cfg, td, run = self.on_plan_run(stream, plan_day, extra)
        for day, verdict, msgs in run:
            self.assertEqual(msgs, [], day)
            self.assertIsNot(verdict, False, day)
            if (day - plan_day).days <= GRACE:
                self.assertIsNone(verdict, day)
        self.assertIs(run[-2][1], True, "the night before the finishing "
                                       "deposit")
        self.assertIsNone(run[-1][1])                       # funded
        p = savings.progress(self.conn, cfg, td)[0]
        self.assertEqual(p["pct"], 100.0)
        return run

    def test_monthly_on_the_first(self):
        run = self._never_off(REGULAR["monthly on the 1st"], D(2025, 1, 14))
        self.assertTrue(all(v is True for day, v, _ in run[GRACE + 1:-1]))

    def test_the_last_day_with_weekends_moved(self):
        self._never_off(REGULAR["the last day, weekends moved to Monday"],
                        D(2026, 1, 31))

    def test_weekly(self):
        self._never_off(REGULAR["weekly"], D(2027, 2, 3))

    def test_every_other_week(self):
        self._never_off(REGULAR["every other week"], D(2025, 6, 20))

    def test_the_first_and_the_fifteenth(self):
        self._never_off(REGULAR["the 1st and the 15th"], D(2026, 2, 27))

    def test_weekly_and_monthly_on_the_same_weekday(self):
        self._never_off(REGULAR["weekly and monthly on the first Friday"],
                        D(2027, 5, 9))

    def test_the_fourth_thursday(self):
        self._never_off(REGULAR["the fourth Thursday"], D(2025, 3, 30))

    def test_a_windfall_six_times_the_usual_deposit(self):
        def windfall(rows, td):
            rows[sorted(rows)[2] + days(9)] = 3000.0
        cfg, td, run = self.on_plan_run(FIRST, D(2026, 8, 5), windfall)
        self.assertTrue(all(not msgs and v is not False
                            for _, v, msgs in run))


class FallingBehindNightAfterNightTests(NightlyCase):

    def test_a_saver_who_stops_is_mailed_once(self):
        a = D(2025, 2, 10)

        def stop(rows, td):
            for d in sorted(rows)[4:]:
                del rows[d]
        cfg, td, run = self.on_plan_run(FIRST, a, stop)
        sent = [(day, msgs) for day, _, msgs in run if msgs]
        self.assertEqual(len(sent), 1)
        day, msgs = sent[0]
        last = sorted(d for d in FIRST if d > a)[3]
        self.assertTrue(GRACE - 3 <= (day - last).days <= GRACE + 31)
        self.assertRegex(msgs[0], r"^cushion: off pace for \d{4}-\d\d-\d\d — "
                                  r"\$3,000 saved; the straight line to "
                                  r"\$\d,\d{3} asked for \$[\d,]+ by now\. "
                                  r"\$[\d,]+/mo from here gets there$")
        self.assertTrue(all(v is False for d, v, _ in run if d >= day))

    def test_the_page_reads_what_the_mail_said_on_any_past_day(self):
        a = D(2026, 4, 2)

        def stop(rows, td):
            for d in sorted(rows)[3:]:
                del rows[d]
        cfg, td, run = self.on_plan_run(FIRST, a, stop,
                                        until=a + days(200))
        self.assertIn(False, [v for _, v, _ in run])
        for day, verdict, _ in run[1::9]:
            p = savings.progress(self.conn, cfg, day)[0]
            self.assertIs(p["on_pace"], verdict, day)
        # a day before the plan was set has no schedule to be read against
        self.assertIsNone(
            savings.progress(self.conn, cfg, a - days(3))[0]["on_pace"])

    def test_a_skipped_deposit_made_up_reads_on_pace_that_night(self):
        a = D(2027, 1, 1)
        box = {}

        def skip_then_double(rows, td):
            ds = sorted(rows)
            del rows[ds[4]]
            rows[ds[5]] += 500
            box["back"] = ds[5]
        cfg, td, run = self.on_plan_run(FIRST, a, skip_then_double)
        self.assertEqual(sum(len(m) for _, _, m in run), 1)
        off = [day for day, v, _ in run if v is False]
        self.assertEqual(off[-1] + days(1), box["back"])
        self.assertTrue(all(v is not False for day, v, _ in run
                            if day >= box["back"]))

    def test_a_large_withdrawal_is_mailed_the_night_it_posts(self):
        a = D(2025, 9, 12)
        taken = a + days(160)

        def withdraw(rows, td):
            rows[taken] = -3000.0
        cfg, td, run = self.on_plan_run(FIRST, a, withdraw)
        self.assertEqual([day for day, _, msgs in run if msgs], [taken])

    def test_a_new_goal_never_funded_is_mailed_on_its_39th_night(self):
        a = D(2026, 11, 20)
        cfg = {"savings_goals": [self.goal(6000.0, a + days(365))]}
        run = self.nights(cfg, a, a + days(120))
        self.assertEqual([day for day, _, msgs in run if msgs],
                         [a + days(GRACE + 1)])
        self.assertTrue(all(v is None for day, v, _ in run[:GRACE + 1]))

    def test_a_missed_date_is_mailed_on_the_fourth_night_after(self):
        a = D(2027, 3, 3)

        def miss_the_last(rows, td):
            del rows[td]
        cfg, td, run = self.on_plan_run(FIRST, a, miss_the_last,
                                        until=None)
        run += self.nights(cfg, td + days(1), td + days(12))
        sent = [(day, msgs) for day, _, msgs in run if msgs]
        self.assertEqual([day for day, _ in sent], [td + days(4)])
        self.assertEqual(sent[0][1], [
            f"cushion: off pace — {td.isoformat()} has passed with $500 "
            "still to go"])

    def test_a_finishing_deposit_two_days_late_is_not_mailed(self):
        a = D(2025, 5, 18)
        box = {}

        def late(rows, td):
            rows[td + days(2)] = rows.pop(td)
            box["rows"] = rows
        cfg, td, run = self.on_plan_run(FIRST, a, late)
        run += self.nights(cfg, td + days(1), td + days(10), box["rows"])
        self.assertTrue(all(not msgs and v is not False
                            for _, v, msgs in run))
        self.assertEqual(savings.progress(
            self.conn, cfg, td + days(10))[0]["pct"], 100.0)

    def test_a_sibling_goals_deposits_do_not_keep_this_one_on_pace(self):
        a = D(2026, 6, 1)
        trip = self.goal(9000.0, a + days(400), name="trip",
                         tokens=["vacation"])
        cfg = {"savings_goals": [self.goal(6000.0, a + days(365)), trip]}
        day, sent = a, []
        while day <= a + days(90):
            if day.day == 1 and day > a:
                self.post(day, 2000.0, "VACATION FUND")
            msgs, cfg["savings_goal_state"] = savings.check_milestones(
                self.conn, cfg, day)
            sent += [(day, m) for m in msgs if "off pace" in m]
            day += days(1)
        self.assertEqual([(d, m.split(":")[0]) for d, m in sent],
                         [(a + days(GRACE + 1), "cushion")])
        self.assertIs(cfg["savings_goal_state"]["trip"]["on_pace"], True)


class ThePlanIsKeptTests(NightlyCase):

    def _saving(self, a, nights=120):
        cfg, td, run = self.on_plan_run(FIRST, a, until=a + days(nights))
        return cfg, td, a + days(nights)

    def test_the_stored_state_carries_the_plan(self):
        a = D(2025, 4, 6)
        cfg, td, last = self._saving(a, 60)
        g = cfg["savings_goals"][0]
        self.assertEqual(cfg["savings_goal_state"], {"cushion": {
            "milestone": 25, "on_pace": True,
            "plan": {"day": a.isoformat(), "saved": 1000.0,
                     "for": [g["target"], td.isoformat(), "sav", [],
                             1000.0]}}})

    def test_a_renamed_goal_keeps_its_plan_and_its_verdict(self):
        a = D(2026, 3, 9)
        cfg, td, last = self._saving(a)
        plan = cfg["savings_goal_state"]["cushion"]["plan"]
        cfg["savings_goals"][0]["name"] = "rainy day"
        # the page, before the sweep has run under the new name
        self.assertIs(savings.progress(
            self.conn, cfg, last + days(1))[0]["on_pace"], True)
        msgs, state = savings.check_milestones(self.conn, cfg,
                                               last + days(1))
        self.assertEqual([m for m in msgs if "off pace" in m], [])
        self.assertEqual(state["rainy day"]["plan"], plan)
        self.assertIs(state["rainy day"]["on_pace"], True)
        self.assertNotIn("cushion", state)

    def test_an_edited_target_starts_a_new_plan_that_night(self):
        a = D(2027, 2, 1)
        cfg, td, last = self._saving(a)
        cfg["savings_goals"][0]["target"] += 4000.0
        edit = last + days(1)
        # until the sweep has drawn the new line there is none to read
        self.assertIsNone(savings.progress(self.conn, cfg,
                                           edit)[0]["on_pace"])
        saved = savings.progress(self.conn, cfg, edit)[0]["saved"]
        rows = {d: v for d, v in FIRST.items() if d > edit}
        run = self.nights(cfg, edit, edit + days(GRACE + 60), rows)
        plan = cfg["savings_goal_state"]["cushion"]["plan"]
        self.assertEqual((plan["day"], plan["saved"]),
                         (edit.isoformat(), saved))
        self.assertTrue(all(v is None and not m
                            for _, v, m in run[:GRACE + 1]))
        # the same deposits against a steeper line: late, one mail
        self.assertEqual(sum(len(m) for _, _, m in run), 1)

    def test_an_edited_date_starts_a_new_plan_too(self):
        a = D(2025, 7, 7)
        cfg, td, last = self._saving(a)
        cfg["savings_goals"][0]["target_date"] = (td + days(90)).isoformat()
        self.nights(cfg, last + days(1), last + days(1))
        self.assertEqual(cfg["savings_goal_state"]["cushion"]["plan"]["day"],
                         (last + days(1)).isoformat())

    def test_a_goal_restored_with_its_state_keeps_its_plan(self):
        a = D(2026, 9, 9)
        cfg, td, last = self._saving(a)
        restored = {"savings_goals": [dict(cfg["savings_goals"][0])],
                    "savings_goal_state": {
                        k: dict(v)
                        for k, v in cfg["savings_goal_state"].items()}}
        msgs, state = savings.check_milestones(self.conn, restored,
                                               last + days(1))
        self.assertEqual(state["cushion"]["plan"]["day"], a.isoformat())
        self.assertIs(state["cushion"]["on_pace"], True)

    def test_a_goal_restored_without_state_is_judged_from_its_ledger(self):
        a = D(2027, 4, 4)
        cfg, td, last = self._saving(a)
        restored = {"savings_goals": [dict(cfg["savings_goals"][0])]}
        night = last + days(1)
        rows = {d: v for d, v in FIRST.items() if night < d <= td}
        run = self.nights(restored, night, night + days(60), rows)
        plan = restored["savings_goal_state"]["cushion"]["plan"]
        self.assertEqual(plan["day"], (night - days(GRACE)).isoformat())
        self.assertTrue(all(not m for _, _, m in run))
        self.assertTrue(all(v is True for _, v, _ in run[1:]))


class TheFirstNightForAnExistingGoalTests(NightlyCase):
    """A goal saved toward before plans were kept has a stored verdict and
    no plan. Its line is drawn from 38 days back, so it is judged from the
    second night; the first night changes nothing, and no night mails until
    the goal has read on pace under that line once."""

    NIGHT = D(2026, 5, 20)

    def _existing(self, stored, saving=True):
        td = D(2027, 5, 1)
        for d in sorted(FIRST):
            if D(2025, 6, 1) <= d <= (self.NIGHT if saving
                                      else D(2026, 2, 1)):
                self.post(d, FIRST[d])
        saved = savings.progress(self.conn, {"savings_goals": [
            self.goal(1.0, td)]}, self.NIGHT)[0]["saved"]
        left = len([d for d in FIRST if self.NIGHT < d <= td]) * 500.0
        cfg = {"savings_goals": [self.goal(saved + left, td)]}
        if stored is not ...:
            cfg["savings_goal_state"] = {"cushion": {"milestone": 25,
                                                     "on_pace": stored}}
        rows = ({d: v for d, v in FIRST.items() if self.NIGHT < d <= td}
                if saving else None)
        return cfg, self.nights(cfg, self.NIGHT, self.NIGHT + days(75), rows)

    def test_an_on_plan_saver_read_on_pace_stays_on_pace_unmailed(self):
        cfg, run = self._existing(True)
        self.assertEqual(run[0][1:], (True, []))
        plan = cfg["savings_goal_state"]["cushion"]["plan"]
        self.assertEqual(plan["day"],
                         (self.NIGHT - days(GRACE)).isoformat())
        self.assertNotIn("quiet", plan)
        self.assertTrue(all(v is True and not m for _, v, m in run))

    def test_an_on_plan_saver_read_off_pace_turns_on_without_a_mail(self):
        # a verdict stored before plans were kept may read a saver exactly
        # on plan as off pace; the first plan night turns it on silently
        cfg, run = self._existing(False)
        self.assertEqual(run[0][1:], (False, []))
        self.assertTrue(all(v is True and not m for _, v, m in run[1:]))
        self.assertNotIn("quiet",
                         cfg["savings_goal_state"]["cushion"]["plan"])

    def test_a_stalled_goal_read_on_pace_turns_off_without_a_mail(self):
        cfg, run = self._existing(True, saving=False)
        self.assertEqual(run[0][1:], (True, []))
        self.assertTrue(all(v is False and not m for _, v, m in run[1:]))
        self.assertIs(cfg["savings_goal_state"]["cushion"]["plan"]["quiet"],
                      True)

    def _behind_its_new_line(self, saved_before):
        """A goal with money put in long before the first night and nothing
        since, stored as on pace: (cfg, the line's $10-a-day slope)."""
        self.post(self.NIGHT - days(200), saved_before)
        plan_day = self.NIGHT - days(GRACE)
        cfg = {"savings_goals": [self.goal(saved_before + 3650.0,
                                           plan_day + days(365))],
               "savings_goal_state": {"cushion": {"milestone": 25,
                                                  "on_pace": True}}}
        return cfg

    def test_a_saver_far_ahead_who_paused_is_not_mailed(self):
        # most of the goal already saved and a pause of a few months: the
        # line drawn on the first night starts from that balance and gives
        # no credit for it, so the page reads off pace and no mail goes out
        cfg = self._behind_its_new_line(30000.0)
        run = self.nights(cfg, self.NIGHT, self.NIGHT + days(59))
        self.assertEqual(len(run), 60)
        self.assertTrue(all(not m for _, _, m in run))
        self.assertTrue(all(v is False for _, v, _ in run[1:]))
        self.assertIs(savings.progress(
            self.conn, cfg, self.NIGHT + days(59))[0]["on_pace"], False)

    def test_behind_then_caught_up_then_behind_again_is_one_mail(self):
        # behind at the first night (silent), $1,000 on the tenth night
        # puts it back on pace, and the line passes that balance 101
        # nights in: the one mail is for the second fall
        cfg = self._behind_its_new_line(2000.0)
        caught_up = self.NIGHT + days(10)
        run = self.nights(cfg, self.NIGHT, self.NIGHT + days(130),
                          {caught_up: 1000.0})
        verdicts = {day: v for day, v, _ in run}
        self.assertIs(verdicts[caught_up - days(1)], False)
        self.assertIs(verdicts[caught_up], True)
        self.assertEqual([day for day, _, m in run if m],
                         [self.NIGHT + days(101)])
        self.assertTrue(all(v is False for day, v, _ in run
                            if day >= self.NIGHT + days(101)))

    def test_a_stalled_goal_already_read_off_pace_is_not_mailed_again(self):
        for stored in (False, None, ...):
            with self.subTest(stored=stored):
                cfg, run = self._existing(stored, saving=False)
                self.assertTrue(all(not m for _, _, m in run))
                self.assertIs(run[-1][1], False)
            self.tearDown()
            self.setUp()

    def test_a_first_goal_on_an_account_with_no_rows_starts_tonight(self):
        a = D(2025, 10, 2)
        cfg = {"savings_goals": [self.goal(6000.0, a + days(365))]}
        self.nights(cfg, a, a)
        self.assertEqual(cfg["savings_goal_state"]["cushion"]["plan"],
                         {"day": a.isoformat(), "saved": 0.0,
                          "for": [6000.0, (a + days(365)).isoformat(),
                                  "sav", [], 0.0]})


class UndeliveredNightTests(unittest.TestCase):
    """The milestone and a fall onto off pace wait for a mail that went
    out; the plan, and a verdict that mailed nothing, do not, or a relay
    that is down would move every new plan's starting day and balance to
    the night it came back, and a stale verdict would hide the next fall."""

    PLAN = {"day": "2026-03-02", "saved": 250.0,
            "for": [5000.0, "2027-03-02", "sav", [], 0.0]}

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def tearDown(self):
        self.conn.close()

    def _night(self, results, state):
        with mock.patch("oikonome.engine.savings.goals", return_value=[1]), \
             mock.patch("oikonome.engine.savings.check_milestones",
                        return_value=(["g1: 50% funded"], state)), \
             mock.patch("oikonome.web.report.resolve_smtp",
                        return_value=RELAY), \
             mock.patch("oikonome.jobs.worker._recipients",
                        return_value=["a@example.dev"]), \
             mock.patch("oikonome.web.report.send_each",
                        return_value=results):
            worker.nightly_tenant(self.tid)
        return budget.load_config(self.conn).get("savings_goal_state")

    def test_a_refused_mail_still_stores_the_plan_and_only_the_plan(self):
        state = {"g1": {"milestone": 50, "on_pace": None,
                        "plan": dict(self.PLAN)},
                 "g2": {"milestone": 0, "on_pace": None, "plan": None}}
        refused = [{"email": "a@example.dev", "ok": False,
                    "error": "relay down"}]
        self.assertEqual(self._night(refused, state),
                         {"g1": {"plan": self.PLAN}})
        self.assertEqual(self._night(
            [{"email": "a@example.dev", "ok": True, "error": None}], state),
            state)

    def test_a_refused_mail_keeps_the_stored_milestone_beside_the_plan(self):
        with budget.config_txn(self.conn) as live:
            live["savings_goal_state"] = {"g1": {"milestone": 25,
                                                 "on_pace": True}}
        state = {"g1": {"milestone": 50, "on_pace": False,
                        "plan": dict(self.PLAN)}}
        self.assertEqual(self._night(
            [{"email": "a@example.dev", "ok": False, "error": "relay down"}],
            state), {"g1": {"milestone": 25, "on_pace": True,
                            "plan": self.PLAN}})

    def test_a_refused_mail_stores_a_verdict_that_was_not_news(self):
        # the stored verdict came from an earlier rule; tonight's verdict
        # mails nothing, so holding the old one back for a retry would
        # only hide the next real fall (no edge from False to False)
        with budget.config_txn(self.conn) as live:
            live["savings_goal_state"] = {"g1": {"milestone": 25,
                                                 "on_pace": False}}
        state = {"g1": {"milestone": 50, "on_pace": True,
                        "plan": dict(self.PLAN)}}
        self.assertEqual(self._night(
            [{"email": "a@example.dev", "ok": False, "error": "relay down"}],
            state), {"g1": {"milestone": 25, "on_pace": True,
                            "plan": self.PLAN}})

    def test_a_refused_mail_holds_back_the_fall_it_was_carrying(self):
        with budget.config_txn(self.conn) as live:
            live["savings_goal_state"] = {"g1": {"milestone": 25,
                                                 "on_pace": None,
                                                 "plan": self.PLAN}}
        state = {"g1": {"milestone": 25, "on_pace": False,
                        "plan": dict(self.PLAN)}}
        self.assertEqual(self._night(
            [{"email": "a@example.dev", "ok": False, "error": "relay down"}],
            state), {"g1": {"milestone": 25, "on_pace": None,
                            "plan": self.PLAN}})


class NothingArrivedOrNothingNetTests(NightlyCase):
    """A rate at or below zero is worded two ways: nothing arrived in the
    window, or deposits arrived and withdrawals outweighed them."""

    TODAY = D(2026, 7, 15)

    def _goal(self):
        return {"savings_goals": [self.goal(0.0, self.TODAY, 5000.0,
                                            target_date=None)]}

    def test_a_deposit_outweighed_by_a_withdrawal_still_counts(self):
        self.post(self.TODAY - days(20), 500.0)
        self.post(self.TODAY - days(6), -900.0)
        p = savings.progress(self.conn, self._goal(), self.TODAY)[0]
        self.assertLess(p["rate_90d"], 0)
        self.assertIs(p["contributed_90d"], True)

    def test_only_withdrawals_is_no_contribution(self):
        self.post(self.TODAY - days(6), -900.0)
        p = savings.progress(self.conn, self._goal(), self.TODAY)[0]
        self.assertIs(p["contributed_90d"], False)

    def test_a_deposit_older_than_the_window_is_no_contribution(self):
        self.post(self.TODAY - days(120), 500.0)
        p = savings.progress(self.conn, self._goal(), self.TODAY)[0]
        self.assertEqual(p["rate_90d"], 0)
        self.assertIs(p["contributed_90d"], False)

    def test_a_positive_rate_is_a_contribution(self):
        self.post(self.TODAY - days(20), 500.0)
        p = savings.progress(self.conn, self._goal(), self.TODAY)[0]
        self.assertIs(p["contributed_90d"], True)

    def test_the_window_net_is_carried_beside_the_monthly_rate(self):
        # two monthly deposits in a full window: the net is what arrived,
        # the per-month rate is that over three months, unchanged
        self.post(self.TODAY - days(120), 100.0)
        self.post(self.TODAY - days(60), 500.0)
        self.post(self.TODAY - days(30), 500.0)
        p = savings.progress(self.conn, self._goal(), self.TODAY)[0]
        self.assertEqual(p["saved_90d"], 1000.0)
        self.assertEqual(p["rate_90d"], 333.33)


class TheGoalLineSaysTheSameWordsEverywhereTests(NightlyCase):
    """Today on the web, Today in the app and both renders of the daily
    email print one goal line. The email is rendered here; the two clients
    are held to the same phrases by their source."""

    TODAY = D(2026, 7, 15)
    PHRASES = ("no net saving in the last 3 months",
               "no contributions in 3 months", "off pace for ", "on pace for ",
               " at the current rate", " in the last 3 months")

    def _mail(self, goal, state=None):
        write_config(self.conn, today_view="detail", savings_goals=[goal],
                     savings_goal_state=state or {})
        _, plain, html = report.build(report.gather(self.conn, self.TODAY))
        # the chart rides inline as base64, whose letters can spell
        # anything ("/mo" included): the words are what is read
        html = re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", html)
        return plain, " ".join(html.split())

    def _open_ended(self):
        return self.goal(0.0, self.TODAY, 5000.0, target_date=None)

    def test_withdrawals_outweighing_deposits_is_not_no_contributions(self):
        self.post(self.TODAY - days(20), 500.0)
        self.post(self.TODAY - days(6), -900.0)
        for text in self._mail(self._open_ended()):
            self.assertIn("no net saving in the last 3 months", text)
            self.assertNotIn("no contributions", text)

    def test_an_open_ended_goal_prints_what_the_last_three_months_saved(self):
        self.post(self.TODAY - days(60), 500.0)
        self.post(self.TODAY - days(30), 500.0)
        for text in self._mail(self._open_ended()):
            self.assertIn("$1,000 in the last 3 months", text)
            self.assertNotIn("/mo", text)
            self.assertNotIn("saving $", text)

    def test_nothing_arriving_is_no_contributions(self):
        for text in self._mail(self._open_ended()):
            self.assertIn("no contributions in 3 months", text)

    def test_a_target_with_no_date_and_no_net_saving_says_so(self):
        self.post(self.TODAY - days(20), 500.0)
        self.post(self.TODAY - days(6), -900.0)
        goal = self.goal(9000.0, self.TODAY, 5000.0, target_date=None)
        for text in self._mail(goal):
            self.assertIn("no net saving in the last 3 months", text)

    def _dated(self, deposit):
        if deposit:
            self.post(self.TODAY - days(50), deposit)
        goal = self.goal(6000.0, self.TODAY + days(300))
        plan = {"day": (self.TODAY - days(60)).isoformat(), "saved": 0.0,
                "for": savings._plan_terms(goal)}
        return self._mail(goal, {"cushion": {"plan": plan}})

    def test_a_goal_on_its_schedule_reads_on_pace_for_its_date(self):
        plain, html = self._dated(1500.0)
        self.assertIn("— on pace", plain)
        self.assertIn("on pace for 05/11/27", html)

    def test_a_goal_behind_its_schedule_reads_off_pace_for_its_date(self):
        plain, html = self._dated(100.0)
        self.assertIn("— off pace", plain)
        self.assertIn("off pace for 05/11/27", html)

    def test_both_clients_and_the_email_carry_the_same_phrases(self):
        app = pathlib.Path(__file__).resolve().parents[2]
        sources = [app / "webapp/src/pages/Today.tsx",
                   app / "mobile/src/app/(tabs)/index.tsx",
                   app / "server/oikonome/web/templates/today_body.html"]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for phrase in self.PHRASES:
                self.assertIn(phrase, text, f"{path.name}: {phrase!r}")
        plain = (app / "server/oikonome/web/report.py").read_text(
            encoding="utf-8")
        for phrase in self.PHRASES[:2] + ("off pace", "on pace",
                                          " at the current rate"):
            self.assertIn(phrase, plain)


if __name__ == "__main__":
    unittest.main()
