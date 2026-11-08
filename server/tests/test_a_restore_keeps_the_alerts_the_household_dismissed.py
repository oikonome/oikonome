"""A restore keeps the alerts the household dismissed dismissed.

The alert log is this instance's operating state, rebuilt by the next
Today load, so it mostly stays behind. The one thing in it that is the
household's own is the dismissal: dropped, every alert the person muted
lights up again on the destination the next time it is built. So a
dismissed row travels (its kind, severity, message, dates and whether it
was still active), a row the destination already holds is left alone,
the never-dismissed rows stay behind, and the restored row is stamped as
no view of the world, so the next snapshot decides it as usual."""

import datetime as dt
import unittest

from oikonome.engine import alerts
from oikonome.sync import export, restore

from .util import TODAY, make_db, write_config


def _add(conn, kind, message, *, dismissed, active=1, last=None):
    conn.execute(
        """INSERT INTO alerts_log (kind, severity, message, first_seen,
                                   last_seen, active, dismissed)
           VALUES (%s, 'warn', %s, %s, %s, %s, %s)""",
        (kind, message, TODAY - dt.timedelta(days=9), last or TODAY,
         active, dismissed))


def _rows(conn):
    return {(r["kind"], r["message"]): r for r in conn.execute(
        "SELECT kind, severity, message, first_seen, last_seen, active,"
        "       dismissed, seen_at FROM alerts_log").fetchall()}


class ARestoreKeepsDismissedAlerts(unittest.TestCase):
    def setUp(self):
        self.src = make_db()
        write_config(self.src)
        _add(self.src, "low_balance", "Checking is below $100", dismissed=1)
        _add(self.src, "stale_feed", "Invented Bank stopped syncing",
             dismissed=1, active=0)
        _add(self.src, "over_budget", "Dining is over budget", dismissed=0)
        self.zip = export.build_zip(self.src)
        self.src.close()

    def test_a_dismissed_alert_stays_dismissed_after_a_restore(self):
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, self.zip)
        got = _rows(dest)
        self.assertEqual(set(got), {
            ("low_balance", "Checking is below $100"),
            ("stale_feed", "Invented Bank stopped syncing")})
        low = got[("low_balance", "Checking is below $100")]
        self.assertEqual((low["dismissed"], low["active"], low["severity"]),
                         (1, 1, "warn"))
        self.assertIsNone(low["seen_at"])
        self.assertEqual(
            got[("stale_feed", "Invented Bank stopped syncing")]["active"], 0)
        # what the Today strip shows: the muted alert stays muted
        built = [{"kind": "low_balance", "severity": "warn",
                  "message": "Checking is below $100"},
                 {"kind": "over_budget", "severity": "warn",
                  "message": "Dining is over budget"}]
        self.assertEqual([a["kind"] for a in alerts.visible(dest, built)],
                         ["over_budget"])
        # and the next snapshot of a still-active alert keeps the mute
        alerts.log(dest, built, TODAY)
        self.assertEqual(
            _rows(dest)[("low_balance", "Checking is below $100")]
            ["dismissed"], 1)

    def test_every_archived_alert_has_one_outcome(self):
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        _, out = restore.restore_outcomes(dest, self.zip)
        self.assertEqual(out.written.get("alerts_log"), 2)
        self.assertEqual(out.refused.get(("alerts_log", "rebuilt")), 1)
        _, out = restore.restore_outcomes(dest, self.zip)
        self.assertEqual(out.refused.get(("alerts_log", "present")), 2)

    def test_a_crafted_future_date_is_held_to_today(self):
        import csv
        import io
        import zipfile
        src = zipfile.ZipFile(io.BytesIO(self.zip))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in src.namelist():
                data = src.read(name)
                if name == "alerts_log.csv":
                    rows = list(csv.DictReader(io.StringIO(data.decode())))
                    for r in rows:
                        r["last_seen"] = "2999-01-01"
                    s = io.StringIO()
                    w = csv.DictWriter(s, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
                    data = s.getvalue().encode()
                z.writestr(name, data)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, buf.getvalue())
        today = dt.date.today()
        for r in _rows(dest).values():
            self.assertLessEqual(r["last_seen"], today)
            self.assertLessEqual(r["first_seen"], r["last_seen"])

    def test_an_alert_raised_here_since_takes_the_mute_and_keeps_its_own_state(self):
        """A restore is a merge: the same alert the destination raised since
        (its feeds connected first) keeps its dates and state, and takes the
        person's dismissal, so a muted alert does not come back lit."""
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        here = TODAY - dt.timedelta(days=1)
        _add(dest, "low_balance", "Checking is below $100", dismissed=0,
             active=0, last=here)
        restore.restore_zip(dest, self.zip)
        row = _rows(dest)[("low_balance", "Checking is below $100")]
        self.assertEqual((row["dismissed"], row["active"], row["last_seen"]),
                         (1, 0, here))

    def test_an_alert_the_person_unmuted_here_stays_unmuted(self):
        """An older archive's mute does not override an un-mute the person
        made on this instance."""
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        _add(dest, "low_balance", "Checking is below $100", dismissed=1)
        dest.execute("UPDATE alerts_log SET dismissed = 0")
        self.assertIsNotNone(dest.execute(
            "SELECT mute_changed_at FROM alerts_log").fetchone()
            ["mute_changed_at"])
        restore.restore_zip(dest, self.zip)
        self.assertEqual(
            _rows(dest)[("low_balance", "Checking is below $100")]
            ["dismissed"], 0)


if __name__ == "__main__":
    unittest.main()
