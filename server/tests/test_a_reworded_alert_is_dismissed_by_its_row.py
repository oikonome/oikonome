"""A reworded alert is dismissed and muted by its row, whatever words the
request carries.

A line logged in an older build's words ("on 03/14", no year) is reworded
in place the next time the alert is raised. A dismiss from a tab or phone
still showing the old words, or an emailed mute minted over them, names a
row whose text has since changed — or one about to change. Each is one
decision under the alerts lock: the row is resolved to its id with the
two-way old-words match and updated by that id, so the mute or dismissal
lands on the alert the person saw, and a tap that changes nothing never
answers "Muted". A row left in old words beside its reworded twin (an
older archive restored) is merged into one, keeping the dismissal and the
mute of either."""

import datetime as dt
import threading
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import alerts
from oikonome.notify import mailact

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db, write_config

OLD = "Fee charged: ANNUAL MEMBERSHIP FEE $95 on 03/14 (Test Card)"
NEW = "Fee charged: ANNUAL MEMBERSHIP FEE $95 on 03/14/26 (Test Card)"
DAY = dt.date(2026, 3, 14)


def _a(message):
    return [{"kind": "anomaly", "severity": "warn", "message": message}]


class RewordedAlertRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def _rows(self):
        return self.conn.execute(
            "SELECT message, dismissed, active, first_seen, mute_changed_at "
            "FROM alerts_log WHERE kind='anomaly' ORDER BY id").fetchall()

    def _owner(self) -> str:
        email = f"owner-{uuid.uuid4().hex[:6]}@example.dev"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','owner',now())",
                (self.tid, email))
        finally:
            admin.close()
        return email

    # ---- a dismiss carrying either wording ---------------------------

    def test_a_dismiss_in_the_old_words_lands_on_the_reworded_row(self):
        """A phone showing the line from before the reword dismisses it."""
        alerts.log(self.conn, _a(OLD), DAY)
        alerts.log(self.conn, _a(NEW), DAY)          # reworded in place
        self.assertEqual(alerts.dismiss(self.conn, "anomaly", OLD), 1)
        self.assertEqual([(r["message"], r["dismissed"])
                          for r in self._rows()], [(NEW, 1)])
        self.assertEqual(alerts.restore(self.conn, "anomaly", OLD), 1)
        self.assertEqual(self._rows()[0]["dismissed"], 0)

    def test_a_dismiss_in_the_new_words_lands_on_a_row_not_yet_reworded(self):
        alerts.log(self.conn, _a(OLD), DAY)
        self.assertEqual(alerts.dismiss(self.conn, "anomaly", NEW), 1)
        alerts.log(self.conn, _a(NEW), DAY)
        self.assertEqual([(r["message"], r["dismissed"])
                          for r in self._rows()], [(NEW, 1)])

    # ---- the emailed mute, raced by a reword ----------------------------

    def _mute(self, value, between):
        """mailact.apply for a mute on `value`, running `between` right
        after the tap has found its row."""
        tok = mailact.parse(mailact.token(self.tid, self._owner(), "mute",
                                          "anomaly", value))
        found = mailact._find_alert

        def hooked(conn, t):
            r = found(conn, t)
            between()
            return r
        with mock.patch.object(mailact, "_find_alert", hooked):
            return mailact.apply(tok)

    def test_a_mute_raced_by_the_reword_still_mutes_the_alert(self):
        alerts.log(self.conn, _a(OLD), DAY)
        done = threading.Event()

        def reword():
            other = tenancy.tenant_connect(self.tid)
            try:
                alerts.log(other, _a(NEW), DAY)
            finally:
                other.close()
                done.set()
        worker = threading.Thread(target=reword)

        def between():
            worker.start()
            done.wait(1.5)        # it waits on the tap's lock, or it ran
        got = self._mute(OLD, between)
        worker.join(10)
        self.assertEqual(got["state"], "done", got)
        self.assertEqual([(r["message"], r["dismissed"])
                          for r in self._rows()], [(NEW, 1)])

    def test_a_mute_whose_row_went_away_does_not_say_muted(self):
        alerts.log(self.conn, _a(NEW), DAY)

        def gone():
            other = tenancy.tenant_connect(self.tid)
            try:
                other.execute("DELETE FROM alerts_log WHERE kind='anomaly'")
            finally:
                other.close()
        # the delete waits behind nothing the tap holds, so the tap's
        # update by id finds no row
        with mock.patch.object(alerts, "_lock", lambda conn: None):
            got = self._mute(NEW, gone)
        self.assertNotEqual(got["state"], "done", got)
        self.assertIn(got["state"], ("already", "unknown"))

    # ---- both wordings on file -----------------------------------------

    def test_an_old_row_beside_its_reworded_twin_is_merged(self):
        alerts.log(self.conn, _a(OLD), DAY)
        alerts.dismiss(self.conn, "anomaly", OLD)
        muted_at = self._rows()[0]["mute_changed_at"]
        # the reworded twin landed beside it (an older archive restored)
        self.conn.execute(
            "INSERT INTO alerts_log (kind, severity, message, first_seen, "
            "last_seen, active) VALUES ('anomaly','warn',%s,%s,%s,1)",
            (NEW, DAY, DAY))
        with mock.patch("oikonome.notify.webhooks.emit") as emit:
            alerts.log(self.conn, _a(NEW), DAY)
        self.assertEqual(emit.call_count, 0)
        rows = self._rows()
        self.assertEqual([(r["message"], r["dismissed"], r["first_seen"],
                           r["mute_changed_at"]) for r in rows],
                         [(NEW, 1, DAY, muted_at)])
        self.assertEqual(len([r for r in alerts.history_lines(self.conn)
                              if r["kind"] == "anomaly"]), 1)

    def test_a_restore_after_both_wordings_were_dismissed_survives_the_merge(self):
        """The person's latest choice wins the merge: both rows dismissed,
        then the alert restored, stays restored."""
        alerts.log(self.conn, _a(OLD), DAY)
        alerts.dismiss(self.conn, "anomaly", OLD)
        self.conn.execute(
            "INSERT INTO alerts_log (kind, severity, message, first_seen, "
            "last_seen, active, dismissed) "
            "VALUES ('anomaly','warn',%s,%s,%s,1,1)", (NEW, DAY, DAY))
        self.assertEqual(alerts.restore(self.conn, "anomaly", NEW), 1)
        alerts.log(self.conn, _a(NEW), DAY)
        self.assertEqual([(r["message"], r["dismissed"])
                          for r in self._rows()], [(NEW, 0)])

    def test_a_line_reworded_among_many_alike_is_still_found(self):
        """More rows sharing the line's opening words than any window: the
        one reworded from the words given is found."""
        for i in range(60):
            day = DAY - dt.timedelta(days=400 + i)
            self.conn.execute(
                "INSERT INTO alerts_log (kind, severity, message, first_seen,"
                " last_seen, active) VALUES ('anomaly','warn',%s,%s,%s,0)",
                (f"Fee charged: ANNUAL MEMBERSHIP FEE $95 on "
                 f"{day:%m/%d/%y} (Card {i})", day, day))
        self.conn.execute(
            "INSERT INTO alerts_log (kind, severity, message, first_seen, "
            "last_seen, active) VALUES ('anomaly','warn',%s,%s,%s,0)",
            (NEW, DAY - dt.timedelta(days=900), DAY - dt.timedelta(days=900)))
        self.assertEqual(alerts.dismiss(self.conn, "anomaly", OLD), 1)
        self.assertEqual(self.conn.execute(
            "SELECT dismissed FROM alerts_log WHERE message=%s",
            (NEW,)).fetchone()["dismissed"], 1)


if __name__ == "__main__":
    unittest.main()
