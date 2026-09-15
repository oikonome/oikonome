"""A landed factor-clearing reset does not stay open forever.

The cooling-off door is the way back in for someone who has lost the
password, the authenticator AND every recovery code. It is bearable only
because of what surrounds it: seven days of delay, the account told on
every channel it has, one click or any sign-in to stop it. Once those
seven days pass the request has LANDED, and the next reset link then
clears every factor with no recovery code asked for.

`matured()` had a floor and no ceiling, so that exemption never expired. A
cooling-off started and forgotten — by someone who then found their
authenticator, by a hijacker who lost interest, on an account nobody
opened again — left a standing permission, for good. Whoever reached that
mailbox a year later cleared every factor with no code and none of the
week of warning the door is built on: the warning had been served and
forgotten long before.

So a landed request goes stale `LANDED_TTL` after it matured, and the
account falls back to the ordinary rule — ask again, wait the seven days,
be told about it on every channel all over again. Asking again is free and
restarts the clock, which is precisely the protection the stale request
had quietly stopped giving.
"""

import datetime as dt
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

from oikonome.auth import factor_reset
from oikonome.db import tenancy

from .util import _ensure_db


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class ALandedResetGoesStaleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        from oikonome.auth import passwords
        self.conn = _control()
        self.addCleanup(self.conn.close)
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        tid = tenancy.create_tenant(admin, f"stale-{uuid.uuid4().hex[:8]}")
        self.uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,%s) RETURNING id",
            (tid, f"stale-{uuid.uuid4().hex[:6]}@example.dev",
             passwords.hash_password("correct-horse-battery"))).fetchone()["id"]

    def _landed(self, days_ago: int):
        """A request whose cooling-off ran out `days_ago` days back. A
        negative cooling-off is the honest way to say that: `request`
        stamps lands_at = now + cooling_off, so the whole rule under test
        is exercised through the real door."""
        return factor_reset.request(
            self.conn, self.uid, cooling_off=-dt.timedelta(days=days_ago))

    def test_a_reset_that_landed_a_month_ago_no_longer_skips_the_code(self):
        self._landed(factor_reset.LANDED_TTL.days + 1)
        self.assertIsNone(
            factor_reset.matured(self.conn, self.uid),
            "a forgotten cooling-off must not be a permanent exemption "
            "from the recovery-code rule")

    def test_a_reset_that_landed_inside_the_window_still_works(self):
        """The door still has to open for the person it was built for —
        someone who waited out the week and is finishing the recovery."""
        self._landed(factor_reset.LANDED_TTL.days - 1)
        self.assertIsNotNone(factor_reset.matured(self.conn, self.uid))

    def test_a_reset_that_landed_today_still_works(self):
        self._landed(0)
        self.assertIsNotNone(factor_reset.matured(self.conn, self.uid))

    def test_a_clock_still_running_has_not_landed(self):
        """The floor is still there: the seven days are the whole point."""
        factor_reset.request(self.conn, self.uid)
        self.assertIsNone(factor_reset.matured(self.conn, self.uid))
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))

    def test_asking_again_after_it_went_stale_opens_a_fresh_clock(self):
        """The fallback has to be a door, not a wall: a stale request
        leaves the account exactly where it started, free to ask again and
        wait the week — with the account told about it all over again."""
        self._landed(factor_reset.LANDED_TTL.days + 1)
        row, _ = factor_reset.request(self.conn, self.uid)
        self.assertIsNone(factor_reset.matured(self.conn, self.uid),
                          "the new clock has to run before it lands")
        self.assertEqual(factor_reset.pending(self.conn, self.uid)["id"],
                         row["id"])
        live = self.conn.execute(
            "SELECT count(*) AS c FROM factor_resets WHERE user_id=%s "
            "AND cancelled_at IS NULL AND consumed_at IS NULL",
            (self.uid,)).fetchone()
        self.assertEqual(live["c"], 1, "the stale one was superseded")

    def test_a_stale_request_is_still_visible_and_still_cancellable(self):
        """`pending` keeps no ceiling on purpose — it answers "did a clock
        run", which stays true — and the mailed cancel link goes on
        working, so nobody is told a link they hold is meaningless."""
        _, token = self._landed(factor_reset.LANDED_TTL.days + 5)
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))
        self.assertIsNotNone(factor_reset.cancel_by_token(self.conn, token))
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))


if __name__ == "__main__":
    unittest.main()
