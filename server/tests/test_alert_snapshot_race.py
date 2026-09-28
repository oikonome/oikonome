"""An alert the household cleared stays cleared when a slow writer lands.

A full-snapshot alerts write is a READ of the world (build the list) followed
by a WRITE of the whole active set, and the two can be far apart — the Today
page's build takes a moment, the hourly sync's takes longer. So two writers
are routinely in flight at once with different views of the world, and the
one that COMMITS last is not necessarily the one that LOOKED last.

What must never happen is the late arrival of an old view undoing a newer
one: an alert the user watched clear coming back by itself, with the
dismissal they had put on it reset, so dismissing it again does not stick
either. Two mechanisms, both pinned here — a per-tenant lock so snapshots
apply whole, and `last_seen` as a monotonic stamp so an older writer asserts
nothing over a newer one's work.
"""

import datetime as dt
import threading
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import alerts

from .util import _ensure_db, make_db

OLD = dt.date(2026, 7, 14)          # what the slow writer still believes
NEW = dt.date(2026, 7, 15)          # what the writer behind it has seen

ALERT = {"kind": "anomaly", "severity": "warn",
         "message": "Groceries are 3x the usual this week."}


class AlertSnapshotRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def _row(self):
        return self.conn.execute(
            "SELECT active, dismissed, last_seen FROM alerts_log "
            "WHERE kind=%s AND message=%s",
            (ALERT["kind"], ALERT["message"])).fetchone()

    # ---- the stamp: an older view asserts nothing --------------------

    def _cleared_after_a_newer_writer_saw_it(self):
        """The state a slow writer arrives into: the alert was still there
        on NEW (so the row carries that stamp), the user dismissed it, and
        a later snapshot on NEW found the condition gone."""
        alerts.log(self.conn, [ALERT], OLD)
        alerts.log(self.conn, [ALERT], NEW)
        alerts.dismiss(self.conn, ALERT["kind"], ALERT["message"])
        alerts.log(self.conn, [], NEW)
        self.assertEqual(self._row()["active"], 0)

    def test_a_stale_writer_cannot_resurrect_a_cleared_alert(self):
        self._cleared_after_a_newer_writer_saw_it()

        alerts.log(self.conn, [ALERT], OLD)            # the slow writer lands
        row = self._row()
        self.assertEqual(row["active"], 0,
                         "an older snapshot reactivated a cleared alert")
        self.assertEqual(row["dismissed"], 1,
                         "an older snapshot reset the user's dismissal")
        self.assertEqual(row["last_seen"], NEW,
                         "last_seen walked backwards")

    def test_a_stale_writer_cannot_clear_what_a_newer_one_raised(self):
        """The mirror: a snapshot built before an alert existed does not
        know about it, so it must not treat it as gone."""
        alerts.log(self.conn, [ALERT], NEW)
        alerts.log(self.conn, [], OLD)
        self.assertEqual(self._row()["active"], 1,
                         "an older snapshot cleared a newer writer's alert")

    def test_a_recurrence_after_a_clearance_still_resets_the_dismissal(self):
        """The guard above must not cost the real behaviour: when the
        condition comes back, that IS news and the dismissal resets."""
        alerts.log(self.conn, [ALERT], OLD)
        alerts.dismiss(self.conn, ALERT["kind"], ALERT["message"])
        alerts.log(self.conn, [], OLD)
        alerts.log(self.conn, [ALERT], NEW)
        row = self._row()
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["dismissed"], 0)

    # ---- the same day: the view's instant orders the writers ----------

    def test_a_same_day_stale_writer_cannot_resurrect_a_cleared_alert(self):
        """Two writers in flight at once carry the same day, so the day
        cannot tell them apart. The one that began reading EARLIER still
        holds the older view even when it lands last, and must not bring
        back an alert the later one cleared, nor reset its dismissal."""
        early = dt.datetime(2026, 7, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
        late = early + dt.timedelta(seconds=5)
        alerts.log(self.conn, [ALERT], NEW, as_of=early - dt.timedelta(hours=1))
        alerts.dismiss(self.conn, ALERT["kind"], ALERT["message"])
        alerts.log(self.conn, [], NEW, as_of=late)      # fresh view: gone
        self.assertEqual(self._row()["active"], 0)
        alerts.log(self.conn, [ALERT], NEW, as_of=early)  # slow writer lands
        row = self._row()
        self.assertEqual(row["active"], 0,
                         "an older same-day view reactivated a cleared alert")
        self.assertEqual(row["dismissed"], 1,
                         "an older same-day view reset the user's dismissal")

    def test_a_same_day_stale_writer_cannot_clear_what_a_newer_one_raised(self):
        early = dt.datetime(2026, 7, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
        alerts.log(self.conn, [ALERT], NEW, as_of=early + dt.timedelta(seconds=5))
        alerts.log(self.conn, [], NEW, as_of=early)
        self.assertEqual(self._row()["active"], 1,
                         "an older same-day view cleared a newer alert")

    def test_a_same_day_newer_writer_still_raises_and_clears(self):
        """The guard costs nothing in order: a later view clears, and a
        later view after that raises again as news."""
        t0 = dt.datetime(2026, 7, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
        alerts.log(self.conn, [ALERT], NEW, as_of=t0)
        alerts.dismiss(self.conn, ALERT["kind"], ALERT["message"])
        alerts.log(self.conn, [], NEW, as_of=t0 + dt.timedelta(seconds=1))
        self.assertEqual(self._row()["active"], 0)
        alerts.log(self.conn, [ALERT], NEW, as_of=t0 + dt.timedelta(seconds=2))
        row = self._row()
        self.assertEqual((row["active"], row["dismissed"]), (1, 0))

    def test_a_stale_writer_that_changes_nothing_raises_no_event(self):
        """The alert.raised event is the edge the row actually took. A
        same-day writer holding an older view leaves a cleared row cleared,
        so it must not tell a webhook receiver the alert came back — the
        receiver would announce an alert every page shows as gone."""
        early = dt.datetime(2026, 7, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
        alerts.log(self.conn, [ALERT], NEW, as_of=early - dt.timedelta(hours=1))
        alerts.log(self.conn, [], NEW, as_of=early + dt.timedelta(seconds=5))
        with mock.patch("oikonome.notify.webhooks.emit") as emit:
            alerts.log(self.conn, [ALERT], NEW, as_of=early)   # stale view
        self.assertEqual(self._row()["active"], 0)
        self.assertEqual(emit.call_count, 0,
                         "a no-op stale upsert emitted alert.raised")
        # and a genuinely newer view still raises it, once
        with mock.patch("oikonome.notify.webhooks.emit") as emit:
            alerts.log(self.conn, [ALERT, ALERT], NEW,
                       as_of=early + dt.timedelta(seconds=10))
        self.assertEqual(self._row()["active"], 1)
        self.assertEqual(emit.call_count, 1)

    # ---- the lock: two connections, and the newer clear wins ----------

    def test_the_newer_clear_wins_over_a_concurrent_stale_resurrect(self):
        """Two real writers, two connections, released together: whichever
        order they reach the table in, the newer view is what stands."""
        alerts.log(self.conn, [ALERT], NEW)   # a newer writer stamped it…
        alerts.dismiss(self.conn, ALERT["kind"], ALERT["message"])
        start = threading.Barrier(2, timeout=10)
        errors: list[Exception] = []

        def write(snapshot, today):
            conn = tenancy.tenant_connect(self.tid)
            try:
                start.wait()
                alerts.log(conn, snapshot, today)
            except Exception as e:                    # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=write, args=([], NEW), daemon=True),
                   threading.Thread(target=write, args=([ALERT], OLD),
                                    daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "a snapshot writer never returned")
        self.assertEqual(errors, [], f"a snapshot writer failed: {errors}")
        row = self._row()
        self.assertEqual(row["active"], 0,
                         "the stale resurrect beat the newer clear")
        self.assertEqual(row["dismissed"], 1,
                         "the user's dismissal was reset from under them")


    # ---- retire: a queue emptied by hand is a clearance like any other ---

    def test_a_writer_that_looked_before_a_retire_cannot_raise_it_back(self):
        """Working the last item off a queue retires its alert at once,
        outside any snapshot. A Today build that began reading before that
        still carries the alert, and lands after. The retire is the newer
        view, so the late build must not bring the alert back, reset the
        dismissal on it, or announce it to a webhook receiver."""
        today = dt.date.today()
        began = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        alerts.log(self.conn, [ALERT], today,
                   as_of=began - dt.timedelta(minutes=5))
        alerts.dismiss(self.conn, ALERT["kind"], ALERT["message"])
        with self.conn.transaction():
            self.assertEqual(alerts.retire(self.conn, ALERT["kind"]), 1)
        with mock.patch("oikonome.notify.webhooks.emit") as emit:
            alerts.log(self.conn, [ALERT], today, as_of=began)
        row = self._row()
        self.assertEqual(row["active"], 0,
                         "a build older than the retire raised it back")
        self.assertEqual(row["dismissed"], 1,
                         "a build older than the retire reset the dismissal")
        self.assertEqual(emit.call_count, 0)
        # a build that looked after the retire is news, as ever
        alerts.log(self.conn, [ALERT], today,
                   as_of=dt.datetime.now(dt.timezone.utc)
                   + dt.timedelta(seconds=1))
        self.assertEqual(self._row()["active"], 1)

    def test_retire_waits_for_a_snapshot_in_progress(self):
        """A retire landing in the middle of a snapshot's upserts would be
        overwritten by the rest of it; it takes the same per-tenant lock,
        so it applies before or after a snapshot, never inside one."""
        alerts.log(self.conn, [ALERT], NEW)
        holder = tenancy.tenant_connect(self.tid)
        self.addCleanup(holder.close)
        holder.execute("BEGIN")
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtext(COALESCE("
            "current_setting('app.tenant_id', true), '')), "
            "hashtext('alerts-log'))")
        done = threading.Event()

        def run():
            conn = tenancy.tenant_connect(self.tid)
            try:
                with conn.transaction():
                    alerts.retire(conn, ALERT["kind"])
            finally:
                conn.close()
                done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertFalse(done.wait(1.0),
                         "retire ran inside another writer's snapshot")
        holder.execute("COMMIT")
        self.assertTrue(done.wait(10), "retire never finished")
        self.assertEqual(self._row()["active"], 0)


if __name__ == "__main__":
    unittest.main()
