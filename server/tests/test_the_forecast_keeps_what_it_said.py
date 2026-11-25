"""The forecast keeps what it said each night (forecast_nights;
specs/forecast-grading.md, "What is kept each night"): one row per
household-local day, written once, never back-filled; the predicted
balances are the walk's own to the cent; no checking (or an unset
balance) is no row; `fresh` follows the checking account's last update;
the keep trims inputs after 120 nights and rows after 400. Figures and
names are invented."""

import datetime as dt
import json
import unittest

from oikonome.engine import forecast, forecast_score

from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


def _household(cards: int = 1, bills: int = 2):
    conn = make_db()
    write_config(conn, food_monthly=700, other_monthly=500)
    # balances pulled just now, as a sync stamps them
    conn.execute("UPDATE accounts SET updated_at = now()")
    for k in range(2, cards + 1):
        conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES (%s,'it1',%s,'credit','credit card',%s)",
            (f"card{k}", f"Willow card {k}", 40.0 * k))
    d = TODAY - dt.timedelta(days=120)
    n = 0
    while d < TODAY:
        n += 1
        add_txn(conn, d, 15.0 + n % 23, f"SHOP {n % 7}",
                primary="FOOD_AND_DRINK")
        if (TODAY - d).days % 14 == 0:
            add_txn(conn, d, -1900.0, "OAKFIELD PAYROLL", account="chk",
                    primary="INCOME", detailed="INCOME_WAGES")
        d += dt.timedelta(days=1)
    # a pool drawn on the card, dripped by the walk
    add_bill(conn, "Garden Fund", 90.0, bill_type="envelope", frequency=None,
             interval=1, merchant="GARDEN SHOP")
    for back in (40, 70, 100):
        add_txn(conn, TODAY - dt.timedelta(days=back), 30.0, "GARDEN SHOP")
    add_bill(conn, "OAKFIELD PAYROLL", 1900.0, income=True,
             next_due=TODAY + dt.timedelta(days=14), last_seen=TODAY,
             frequency="WEEKLY", interval=2)
    for b in range(bills):
        add_bill(conn, f"UTILITY {b}", 30.0 + b,
                 next_due=D(2026, 7, 16 + b % 12),
                 last_seen=D(2026, 6, 16 + b % 12),
                 frequency="WEEKLY" if b % 2 else "MONTHLY")
    return conn


def _night(conn, day=TODAY):
    return conn.execute("SELECT * FROM forecast_nights WHERE as_of = %s",
                        (day,)).fetchone()


class OneRowPerNight(unittest.TestCase):
    def setUp(self):
        self.conn = _household()
        forecast.forget_learned()

    def tearDown(self):
        self.conn.close()

    def test_the_first_record_of_a_day_stands(self):
        self.assertTrue(forecast_score.record(self.conn, TODAY))
        first = _night(self.conn)
        self.conn.execute("UPDATE accounts SET balance_available = 1234, "
                          "balance_current = 1234 WHERE id = 'chk'")
        self.assertFalse(forecast_score.record(self.conn, TODAY))
        again = _night(self.conn)
        self.assertEqual(again["start_checking"], first["start_checking"])
        self.assertEqual(again["walks"], first["walks"])
        self.assertTrue(forecast_score.record(
            self.conn, TODAY + dt.timedelta(days=1)))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM forecast_nights").fetchone()["n"], 2)

    def test_the_kept_balances_are_the_walks_own(self):
        forecast_score.record(self.conn, TODAY)
        row = _night(self.conn)
        fc = forecast.build(self.conn, today=TODAY)
        self.assertEqual(row["checking_id"], "chk")
        self.assertEqual(row["start_checking"], fc["checking"])
        self.assertEqual(row["model"], forecast.FORECAST_MODEL)
        for key, walk in (("usual", "pace_usual"), ("now", "pace_today"),
                          ("due", "pace_due"), ("low", "pace_low"),
                          ("after_cards", "pace_now")):
            by = dict(fc[walk]["series"])
            for i, h in enumerate(forecast_score.HORIZONS):
                want = round(by[(TODAY + dt.timedelta(days=h)).isoformat()]
                             * 100)
                self.assertEqual(row["walks"][key]["at"][i], want, (key, h))
            self.assertEqual(row["walks"][key]["min"],
                             round(fc[walk]["min"] * 100))
        outs = fc["pace_usual"]["outflows"]
        h30 = TODAY + dt.timedelta(days=30)
        self.assertEqual(row["walks"]["usual"]["cards_out"][2], round(sum(
            a for d, a, _c in outs if D.fromisoformat(d) <= h30) * 100))
        # "pay all cards today" keeps that walk's own card payments: the
        # one payoff, then the usual pattern's
        paid = [(D.fromisoformat(d), a) for d, a, _c in
                fc["pace_today"]["outflows"]]
        self.assertTrue(paid)
        for i, h in enumerate(forecast_score.HORIZONS):
            end = TODAY + dt.timedelta(days=h)
            self.assertEqual(row["walks"]["now"]["cards_out"][i], round(
                sum(a for d, a in paid if d <= end) * 100))
        self.assertTrue(row["fresh"])
        self.assertFalse(row["restored"])
        self.assertIsNotNone(row["inputs"])

    def test_no_checking_or_an_unset_balance_keeps_nothing(self):
        self.conn.execute("UPDATE accounts SET balance_available = NULL, "
                          "balance_current = NULL WHERE id = 'chk'")
        self.assertFalse(forecast_score.record(self.conn, TODAY))
        self.conn.execute("DELETE FROM accounts WHERE id = 'chk'")
        forecast.forget_learned()
        self.assertFalse(forecast_score.record(self.conn, TODAY))
        self.assertIsNone(_night(self.conn))

    def test_fresh_follows_the_checking_accounts_last_update(self):
        self.conn.execute("UPDATE accounts SET updated_at = now() - "
                          "interval '3 days'")
        forecast_score.record(self.conn, TODAY)
        self.assertFalse(_night(self.conn)["fresh"])
        # a successful pull of its connection makes the next night fresh
        self.conn.execute("INSERT INTO sync_log (item_id, added) "
                          "VALUES ('it1', 0)")
        forecast_score.record(self.conn, TODAY + dt.timedelta(days=1))
        self.assertTrue(_night(self.conn, TODAY + dt.timedelta(days=1))
                        ["fresh"])
        # a failed one does not
        self.conn.execute("DELETE FROM sync_log")
        self.conn.execute("INSERT INTO sync_log (item_id, error) "
                          "VALUES ('it1', 'bank down')")
        forecast_score.record(self.conn, TODAY + dt.timedelta(days=2))
        self.assertFalse(_night(self.conn, TODAY + dt.timedelta(days=2))
                         ["fresh"])
        # an update by hand, a push or an import stamps the account
        self.conn.execute("UPDATE accounts SET updated_at = now()")
        forecast_score.record(self.conn, TODAY + dt.timedelta(days=3))
        self.assertTrue(_night(self.conn, TODAY + dt.timedelta(days=3))
                        ["fresh"])

    def test_the_keep(self):
        forecast_score.record(self.conn, TODAY)
        old = json.dumps({"usual": {"at": [1, 2, 3, 4, 5]}})
        for back in (forecast_score.SCORE_INPUTS_NIGHTS + 1,
                     forecast_score.NIGHTS_KEPT + 1):
            self.conn.execute(
                "INSERT INTO forecast_nights (as_of, model, checking_id, "
                "start_checking, fresh, walks, pace, inputs) VALUES "
                "(%s, 'w3', 'chk', 10, true, %s::jsonb, '{}', '{}')",
                (TODAY - dt.timedelta(days=back), old))
        forecast_score.trim(self.conn, TODAY)
        got = {r["as_of"]: r["inputs"] for r in self.conn.execute(
            "SELECT as_of, inputs FROM forecast_nights").fetchall()}
        self.assertEqual(set(got), {
            TODAY, TODAY - dt.timedelta(
                days=forecast_score.SCORE_INPUTS_NIGHTS + 1)})
        self.assertIsNone(got[TODAY - dt.timedelta(
            days=forecast_score.SCORE_INPUTS_NIGHTS + 1)])
        self.assertIsNotNone(got[TODAY])


class TheNightsTravelWithTheHousehold(unittest.TestCase):
    def test_an_export_carries_them_and_a_restore_marks_them(self):
        import uuid
        from oikonome.db import tenancy
        from oikonome.sync import export, restore
        from .util import _ensure_db
        src = _household()
        try:
            forecast.forget_learned()
            forecast_score.record(src, TODAY)
            src.execute("UPDATE forecast_nights SET score = '{\"n\": 1}'")
            data = export.build_zip(src)
            kept = _night(src)
        finally:
            src.close()
        _ensure_db()
        admin = tenancy.admin_connect()
        tid = tenancy.create_tenant(admin, f"fn-{uuid.uuid4().hex[:8]}")
        admin.close()
        dest = tenancy.tenant_connect(tid)
        try:
            restore.restore_outcomes(dest, data)
            got = _night(dest)
            self.assertIsNotNone(got)
            self.assertTrue(got["restored"])
            self.assertIsNone(got["score"])
            for k in ("checking_id", "start_checking", "fresh", "walks",
                      "pace", "inputs", "model"):
                self.assertEqual(got[k], kept[k], k)
            # a night this instance kept itself wins over an archived one
            dest.execute("UPDATE forecast_nights SET restored = false, "
                         "start_checking = 1")
            restore.restore_outcomes(dest, data)
            self.assertEqual(_night(dest)["start_checking"], 1)
        finally:
            dest.close()


class ARowIsSmall(unittest.TestCase):
    def test_six_cards_and_many_bills_fit_four_kilobytes(self):
        conn = _household(cards=6, bills=40)
        try:
            forecast.forget_learned()
            forecast_score.record(conn, TODAY)
            row = conn.execute(
                "SELECT pg_column_size(walks) + pg_column_size(pace) "
                "+ pg_column_size(inputs) AS n, "
                "jsonb_array_length(inputs->'events') AS events "
                "FROM forecast_nights").fetchone()
        finally:
            conn.close()
        self.assertGreaterEqual(row["events"], 80)
        self.assertLess(row["n"], 4096)


class TheNightlyStepRecords(unittest.TestCase):
    def test_the_step_keeps_the_households_night(self):
        import os
        from unittest import mock
        from oikonome import localtime
        from oikonome.db import tenancy
        from oikonome.engine import budget
        from oikonome.jobs import worker
        from .util import TEST_DB
        conn = _household()
        try:
            tid = forecast._tenant(conn)
            # the household's clock past the hour the night is kept at
            five = (lambda cfg: dt.datetime.now(localtime.tenant_tz(cfg))
                    .replace(hour=5, minute=0))
            day = five(budget.load_config(conn)).date()
            # tonight's detection has run (the step waits for it)
            from oikonome.engine import alerts as _alerts
            _alerts.heartbeat(conn, "nightly-detect", "")
            conn.execute("UPDATE job_runs SET ran_at = now() + interval "
                         "'1 day' WHERE job = 'nightly-detect'")
            dsn = os.environ.get(
                "OIKONOME_TEST_DSN",
                f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
            real = tenancy.tenant_connect
            with mock.patch.object(worker.tenancy, "tenant_connect",
                                   side_effect=lambda t: real(t, dsn)), \
                 mock.patch.object(localtime, "now_local", five):
                out = worker.forecast_tenant(tid)
                again = worker.forecast_tenant(tid)
            self.assertTrue(out["recorded"], out)
            self.assertEqual(again["_status"], "done")
            self.assertIsNotNone(_night(conn, day))
        finally:
            conn.close()


class EachHouseholdIsKeptAtItsOwnEarlyMorning(unittest.TestCase):
    """The night is kept once per household-local day, the first hourly
    tick after its clock passes worker.FORECAST_LOCAL_HOUR: in zones west
    and east of the instance, across both daylight-saving changes — no day
    doubled, none skipped, the same local hour every night (give or take
    the tick)."""

    def _ticks(self, zone, first_utc, hours):
        import os
        from unittest import mock
        from oikonome import localtime
        from oikonome.db import tenancy
        from oikonome.engine import budget
        from oikonome.jobs import worker
        from .util import TEST_DB
        conn = _household()
        try:
            cfg = budget.load_config(conn)
            budget.save_config(conn, dict(cfg, timezone=zone))
            tid = forecast._tenant(conn)
            dsn = os.environ.get(
                "OIKONOME_TEST_DSN",
                f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
            real = tenancy.tenant_connect
            clock = {"utc": first_utc}
            kept = []
            rec = forecast_score.record

            def spy(c, day, **k):
                kept.append((day, localtime.now_local(
                    budget.load_config(c))))
                return rec(c, day, **k)
            # the instance on a named zone whose nightly (and the hours it
            # waits for detection) is far from every household's early
            # morning here (a process zone is a fixed offset, the wrong one
            # six months from now)
            with mock.patch.dict(os.environ,
                                 {"OIKONOME_TZ": "Asia/Bangkok"}), \
                 mock.patch.object(worker.tenancy, "tenant_connect",
                                   side_effect=lambda t: real(t, dsn)), \
                 mock.patch.object(
                     localtime, "now_local",
                     lambda c: clock["utc"].astimezone(
                         localtime.tenant_tz(c))), \
                 mock.patch.object(forecast_score, "record", spy):
                for k in range(hours):
                    clock["utc"] = first_utc + dt.timedelta(hours=k)
                    worker.forecast_tenant(tid)
            days = [r["as_of"] for r in conn.execute(
                "SELECT as_of FROM forecast_nights ORDER BY as_of")]
        finally:
            conn.close()
        return days, kept

    def _check(self, zone, first_utc, days_n):
        days, kept = self._ticks(zone, first_utc, 24 * days_n)
        self.assertEqual(len(days), len(set(days)))
        for a, b in zip(days, days[1:]):
            self.assertEqual((b - a).days, 1, days)
        self.assertGreaterEqual(len(days), days_n - 1)
        # the first local day the ticks start in is caught up at once (no
        # row for today yet, past the hour); every day after is kept at
        # the first tick past 04:00 local
        self.assertEqual([d for d, _ in kept], days)
        for day, at in kept:
            self.assertEqual(at.date(), day)
        for day, at in kept[1:]:
            self.assertEqual(at.hour, 4, (zone, day, at))

    def test_west_and_east_of_the_instance(self):
        utc = dt.timezone.utc
        for zone in ("America/Los_Angeles", "Asia/Tokyo", "Pacific/Auckland"):
            with self.subTest(zone=zone):
                self._check(zone, dt.datetime(2026, 7, 13, 0, 53,
                                              tzinfo=utc), 3)

    def test_across_both_daylight_saving_changes(self):
        utc = dt.timezone.utc
        for first in (dt.datetime(2026, 3, 6, 0, 53, tzinfo=utc),
                      dt.datetime(2026, 10, 30, 0, 53, tzinfo=utc)):
            for zone in ("America/New_York", "Europe/Berlin"):
                with self.subTest(zone=zone, first=first):
                    self._check(zone, first, 5)


def _a_zone_past_the_hour() -> str:
    """A zone whose clock reads between 06:00 and 22:00 right now, so a
    household set to it is due whatever the time the test runs."""
    from zoneinfo import ZoneInfo
    for z in ("Europe/London", "America/New_York", "Asia/Tokyo",
              "America/Los_Angeles", "Asia/Kolkata", "Pacific/Honolulu",
              "Australia/Sydney", "America/Sao_Paulo", "Pacific/Auckland"):
        if 6 <= dt.datetime.now(ZoneInfo(z)).hour <= 22:
            return z
    raise AssertionError("no zone past the hour")


def _in_zone(conn, zone):
    from oikonome.engine import budget
    budget.save_config(conn, dict(budget.load_config(conn), timezone=zone))


def _detected_tonight(conn):
    """Tonight's bill detection has run for the household (a heartbeat
    newer than any nightly start), so the step never waits for it whatever
    the instance's clock reads when the test runs."""
    from oikonome.engine import alerts
    alerts.heartbeat(conn, "nightly-detect", "")
    conn.execute("UPDATE job_runs SET ran_at = now() + interval '1 day' "
                 "WHERE job = 'nightly-detect'")


class TheTickIsCheapAndBounded(unittest.TestCase):
    """The forecast tick finds the due households in one query, visits
    each once per local day (one with no checking account too), and stops
    inside its budget, the next tick going on from where it stopped."""

    def _patched(self):
        import os
        from unittest import mock
        from oikonome.db import tenancy
        from oikonome.jobs import worker
        from .util import TEST_DB
        dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
        real = tenancy.tenant_connect
        return mock.patch.object(worker.tenancy, "tenant_connect",
                                 side_effect=lambda t: real(t, dsn))

    def test_an_idle_tick_is_one_query(self):
        from unittest import mock
        from oikonome import localtime
        from oikonome.db import tenancy
        from oikonome.engine import alerts, budget
        from oikonome.jobs import worker
        zone = _a_zone_past_the_hour()
        ids = []
        for _ in range(55):
            c = make_db()
            _in_zone(c, zone)
            day = localtime.now_local(budget.load_config(c)).date()
            alerts.heartbeat(c, "forecast", day.isoformat())
            ids.append(forecast._tenant(c))
            c.close()
        queries = []
        real_admin = tenancy.admin_connect

        class Counted:
            def __init__(self, a):
                self._a = a

            def execute(self, *a, **k):
                queries.append(a[0])
                return self._a.execute(*a, **k)

            def __getattr__(self, k):
                return getattr(self._a, k)
        with mock.patch.object(worker.tenancy, "admin_connect",
                               side_effect=lambda *a: Counted(real_admin())), \
             mock.patch.object(worker.tenancy, "tenant_connect",
                               side_effect=AssertionError("visited")):
            out = worker.forecast_sweep(among=ids)
        self.assertEqual(out, {"done": 0, "left": 0})
        self.assertEqual(len(queries), 1)
        # one not marked yet is the one due
        with mock.patch.object(worker.tenancy, "admin_connect",
                               side_effect=lambda *a: real_admin()):
            c = make_db()
            _in_zone(c, zone)
            ids.append(forecast._tenant(c))
            c.close()
            admin = real_admin()
            try:
                self.assertEqual(worker._due_forecast_tenants(admin, ids),
                                 ids[-1:])
            finally:
                admin.close()

    def test_a_household_with_no_checking_is_built_once_a_day(self):
        from unittest import mock
        from oikonome.jobs import worker
        conn = make_db()
        try:
            _in_zone(conn, _a_zone_past_the_hour())
            conn.execute("DELETE FROM accounts WHERE id = 'chk'")
            _detected_tonight(conn)
            tid = forecast._tenant(conn)
            built = []
            real = forecast.build

            def spy(*a, **k):
                built.append(1)
                return real(*a, **k)
            with self._patched(), mock.patch.object(forecast, "build", spy):
                for _ in range(4):
                    worker.forecast_sweep(among=[tid])
            self.assertEqual(len(built), 1)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM forecast_nights").fetchone())
        finally:
            conn.close()

    def test_a_slow_tick_stops_inside_its_budget_and_the_next_goes_on(self):
        import time
        from unittest import mock
        from oikonome.jobs import worker
        zone = _a_zone_past_the_hour()
        conns = [_household() for _ in range(5)]
        for c in conns:
            _in_zone(c, zone)
            _detected_tonight(c)
        ids = [forecast._tenant(c) for c in conns]
        try:
            real = worker.forecast_tenant

            def slow(tid):
                time.sleep(0.3)
                return real(tid)
            with self._patched(), \
                 mock.patch.object(worker, "forecast_tenant", slow):
                t0 = time.monotonic()
                first = worker.forecast_sweep(budget_s=0.5, among=ids)
                took = time.monotonic() - t0
                second = worker.forecast_sweep(budget_s=60, among=ids)
            # it starts no household past its budget: at most the one in
            # hand runs over
            self.assertLess(took, 0.5 + 0.3 + 2.0)
            self.assertGreater(first["left"], 0)
            self.assertEqual(first["done"] + second["done"], 5, (first,
                                                                second))
            self.assertEqual(second["left"], 0)
        finally:
            for c in conns:
                c.close()


if __name__ == "__main__":
    unittest.main()
