"""An account can have at most ONE live cooling-off clock.

The factor-clearing reset is bearable only because the account can stop it:
one click on the mailed link, or any sign-in. Two live requests make that
promise false — the person cancels the clock they were told about, and
another one they never heard of runs on to its own date and clears the
factors for whoever asked. Recording a request supersedes any earlier live
one, but that is check-then-act, so what is pinned here is the behaviour
under CONCURRENCY, plus the database rule underneath it.

Also pinned: how far a cancel link reaches. It has to hold for a link a
later request superseded, or a restart would strand whoever opens the
older mail — and it has to STOP holding, because these tokens carry no
expiry and an unbounded reach makes every link ever mailed a permanent
veto over every future recovery for that account.
"""

import threading
import time
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


class _PausingConn:
    """A real connection that stops at the instant the race turns on:
    after the supersede has run and before the new row exists. Everything
    else is the connection itself, so the code under test is the real
    code on a real second connection — only the interleaving is chosen
    rather than hoped for."""

    _SUPERSEDE = ("UPDATE factor_resets SET cancelled_at=now(), "
                  "cancel_reason='superseded'")

    def __init__(self, conn, at_supersede):
        self._conn = conn
        self._at = at_supersede

    def execute(self, sql, params=None):
        cur = self._conn.execute(sql, params)
        if sql.startswith(self._SUPERSEDE):
            self._at()
        return cur

    def transaction(self):
        return self._conn.transaction()


class OneClockPerAccount(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        from oikonome.auth import passwords
        self.conn = _control()
        self.addCleanup(self.conn.close)
        admin = self.admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        tid = tenancy.create_tenant(admin, f"clock-{uuid.uuid4().hex[:8]}")
        self.uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,%s) RETURNING id",
            (tid, f"clock-{uuid.uuid4().hex[:6]}@example.dev",
             passwords.hash_password("correct-horse-battery"))).fetchone()["id"]

    def _age_the_clock(self, days: int = 400) -> None:
        """Wind this account's clock back so a token really is old — the
        app role may not write these columns, which is why it takes the
        admin connection."""
        self.admin.execute(
            f"UPDATE factor_resets SET requested_at = requested_at - "
            f"interval '{days} days', lands_at = lands_at - "
            f"interval '{days} days' WHERE user_id=%s", (self.uid,))

    def _live(self) -> list[dict]:
        return self.conn.execute(
            "SELECT id, lands_at, cancel_reason FROM factor_resets "
            "WHERE user_id=%s AND cancelled_at IS NULL AND consumed_at IS NULL"
            " ORDER BY requested_at", (self.uid,)).fetchall()

    def test_two_requests_at_once_leave_only_one_clock_running(self):
        """The one that matters: two requests in flight together — a
        double-submitted form, a retried POST, two tabs. Each opens its own
        transaction, so neither can see the other's uncommitted row; unless
        they are serialized on the account, both supersede nothing and both
        commit a clock, and cancelling the one the person was told about
        leaves the other to land."""
        second: dict = {}
        threads: list[threading.Thread] = []
        started = threading.Event()

        def run_second():
            conn = _control()
            started.set()
            try:
                second["row"], _ = factor_reset.request(conn, self.uid)
            except Exception as exc:                     # noqa: BLE001
                second["error"] = exc
            finally:
                conn.close()

        def at_supersede():
            # the first request is mid-transaction and has cancelled
            # nothing (there was nothing to cancel); start the second here
            t = threading.Thread(target=run_second)
            threads.append(t)
            t.start()
            started.wait(5)
            # let it reach the database and, when the fix is in place,
            # block there waiting for this transaction to commit
            time.sleep(0.5)

        racer = _control()
        self.addCleanup(racer.close)
        first, _ = factor_reset.request(
            _PausingConn(racer, at_supersede), self.uid)
        for t in threads:
            t.join(20)
            self.assertFalse(t.is_alive(), "the second request never finished")
        self.assertNotIn("error", second, f"second request failed: "
                                          f"{second.get('error')!r}")

        live = self._live()
        self.assertEqual(len(live), 1,
                         f"{len(live)} clocks are running, not 1")
        # the survivor is the later request, and the earlier one says why
        # it is gone — the same state two requests one after the other
        # would have produced
        self.assertEqual(live[0]["id"], second["row"]["id"])
        gone = self.conn.execute(
            "SELECT cancel_reason FROM factor_resets WHERE id=%s",
            (first["id"],)).fetchone()
        self.assertEqual(gone["cancel_reason"], "superseded")

    def test_the_database_refuses_a_second_live_request(self):
        """The rule does not depend on every caller remembering it: a live
        row is unique per account in the schema, so a path written later
        that inserts one directly is refused rather than quietly granting
        the account a second clock."""
        factor_reset.request(self.conn, self.uid)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO factor_resets (user_id, lands_at, "
                "cancel_token_hash) VALUES (%s, now() + interval '7 days', %s)",
                (self.uid, uuid.uuid4().hex))
        self.assertEqual(len(self._live()), 1)

    def test_a_superseded_cancel_link_still_stops_the_clock(self):
        """Every cancel link reached the same proven inbox, and asking
        again restarts the wait, so the person may well click the older
        mail. Answering "this link has already done its work" while the
        account's clock kept running is the promise broken by a
        technicality — an older link cancels what is live now."""
        _, old_link = factor_reset.request(self.conn, self.uid)
        factor_reset.request(self.conn, self.uid)      # supersedes it
        self.assertIsNotNone(factor_reset.lookup_cancel(self.conn, old_link))
        self.assertIsNotNone(factor_reset.cancel_by_token(self.conn, old_link))
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))
        self.assertEqual(self._live(), [])
        # and it is spent: nothing is left for a second click to cancel
        self.assertIsNone(factor_reset.cancel_by_token(self.conn, old_link))

    def test_a_cancel_link_past_its_own_window_stops_nothing(self):
        """The reach of an older link is bounded by the wait it was minted
        for. A cancel token never expires on its own, so an unbounded reach
        would make every link ever mailed a standing veto over every future
        recovery — an old mailbox, a forwarded message or a screenshot would
        deny the account the only door back in, for good. Inside its own
        window the link is a restart's cancel; past it, it is spent."""
        _, ancient = factor_reset.request(self.conn, self.uid)
        self._age_the_clock()
        _, fresh = factor_reset.request(self.conn, self.uid)   # supersedes it
        self.assertIsNone(factor_reset.lookup_cancel(self.conn, ancient))
        self.assertIsNone(factor_reset.cancel_by_token(self.conn, ancient))
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))
        # and the link the account was actually just told about still works
        self.assertIsNotNone(factor_reset.cancel_by_token(self.conn, fresh))
        self.assertEqual(self._live(), [])

    def test_a_landed_clock_is_still_its_own_links_to_cancel(self):
        """The window bounds how far a link reaches PAST its own request,
        never whether it can stop that request. A clock that has landed and
        not yet been spent is the most dangerous state there is — the next
        reset clears the factors — so the mailed link has to stop it however
        long ago the wait ran out."""
        _, link = factor_reset.request(self.conn, self.uid)
        self._age_the_clock(factor_reset.LANDED_TTL.days - 1)
        self.assertIsNotNone(factor_reset.matured(self.conn, self.uid))
        self.assertIsNotNone(factor_reset.lookup_cancel(self.conn, link))
        self.assertIsNotNone(factor_reset.cancel_by_token(self.conn, link))
        self.assertEqual(self._live(), [])

    def test_a_clock_gone_stale_is_still_its_own_links_to_cancel(self):
        """A landed request stops granting the recovery-code exemption
        after `LANDED_TTL`, but
        the row is still live and the person holding the mail is still
        asking for it to stop. Telling them the link has done its work,
        when it has not, is the cancel promise broken on a technicality."""
        _, link = factor_reset.request(self.conn, self.uid)
        self._age_the_clock()
        self.assertIsNone(factor_reset.matured(self.conn, self.uid))
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))
        self.assertIsNotNone(factor_reset.cancel_by_token(self.conn, link))
        self.assertEqual(self._live(), [])

    def test_a_spent_cancel_link_is_not_a_veto_on_the_next_clock(self):
        """Clicking the link is what makes it spent, and spent is final.
        Only a later request that RESTARTED the wait lets an older link
        answer for a clock it was not minted for; a link that already did
        its work reaches nothing, even a request made minutes later and
        well inside its own window."""
        _, link = factor_reset.request(self.conn, self.uid)
        self.assertIsNotNone(factor_reset.cancel_by_token(self.conn, link))
        factor_reset.request(self.conn, self.uid)      # a brand new clock
        self.assertIsNone(factor_reset.lookup_cancel(self.conn, link))
        self.assertIsNone(factor_reset.cancel_by_token(self.conn, link))
        self.assertEqual(len(self._live()), 1)

    def test_a_sign_in_cancelled_link_is_not_a_veto_either(self):
        """Same rule from the other cancel door: a sign-in ends the clock a
        link belonged to, and the link ends with it. Whoever asked next got
        their own mail, with their own link, to the same proven inbox."""
        _, link = factor_reset.request(self.conn, self.uid)
        self.assertEqual(factor_reset.cancel_for_user(self.conn, self.uid), 1)
        factor_reset.request(self.conn, self.uid)
        self.assertIsNone(factor_reset.cancel_by_token(self.conn, link))
        self.assertEqual(len(self._live()), 1)

    def test_a_cancel_link_from_another_account_stops_nothing(self):
        """Widening the link to the account must not widen it past the
        account: a token minted for someone else is not a cancel for this
        household's clock."""
        from oikonome.auth import passwords
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        tid = tenancy.create_tenant(admin, f"clock-{uuid.uuid4().hex[:8]}")
        other = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,%s) RETURNING id",
            (tid, f"clock-{uuid.uuid4().hex[:6]}@example.dev",
             passwords.hash_password("correct-horse-battery"))).fetchone()["id"]
        _, theirs = factor_reset.request(self.conn, other)
        factor_reset.request(self.conn, self.uid)
        self.assertIsNone(factor_reset.lookup_cancel(self.conn, "not-a-token"))
        self.assertIsNotNone(factor_reset.cancel_by_token(self.conn, theirs))
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))


if __name__ == "__main__":
    unittest.main()
