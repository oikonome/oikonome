"""The emailed way to ANSWER the human check.

A user the Turnstile script cannot reach — JavaScript off, ad blocker,
corporate MITM proxy, privacy browser — can be handed a challenge they are
physically unable to complete, on `/login` AND on `/forgot`. Proving control
of the account's mailbox answers the same question more strongly and needs
no JavaScript.

These tests are mostly about what it must NOT become: a magic link, a way
to launder the per-IP gate, or a mail cannon.
"""

import unittest
import uuid

from oikonome.auth import login_unlock
from oikonome.db import tenancy

from .util import make_db


def _ctl():
    return tenancy.control_connect()


def ensure_schema():
    """Migrations only. These tests never read tenant data, so holding a
    pooled tenant connection per test would starve the pool for nothing."""
    make_db().close()


def make_user(email: str):
    """A control-plane user with its own tenant — enough for the unlock
    tables, which never touch tenant data."""
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, f"ul-{uuid.uuid4().hex[:8]}")
        row = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s, %s, %s) RETURNING id", (tid, email, "x")).fetchone()
        return row["id"]
    finally:
        admin.close()


class UnlockTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def setUp(self):
        self.email = f"unlock-test-{uuid.uuid4().hex[:8]}@example.com"
        self.user_id = make_user(self.email)

    def tearDown(self):
        with _ctl() as c:
            c.execute("DELETE FROM login_unlocks WHERE user_id=%s",
                      (self.user_id,))

    def test_minting_alone_unlocks_nothing(self):
        """The exemption opens when the link is FOLLOWED, not when it is
        sent — otherwise merely asking would clear the challenge, and anyone
        could clear it for any address they can name."""
        with _ctl() as c:
            login_unlock.create(c, self.user_id)
            self.assertFalse(login_unlock.is_unlocked(c, self.email))

    def test_following_the_link_unlocks_the_account(self):
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            self.assertIsNotNone(login_unlock.follow(c, tok))
            self.assertTrue(login_unlock.is_unlocked(c, self.email))

    def test_the_link_works_exactly_once(self):
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            self.assertIsNotNone(login_unlock.follow(c, tok))
            self.assertIsNone(login_unlock.follow(c, tok),
                              "a replayed unlock link opened a second window")

    def test_exemption_is_spent_by_one_sign_in(self):
        """Not a ten-minute window for unlimited guessing."""
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            self.assertTrue(login_unlock.consume(c, self.email))
            self.assertFalse(login_unlock.consume(c, self.email))
            self.assertFalse(login_unlock.is_unlocked(c, self.email))

    def test_refund_rearms_a_just_spent_exemption(self):
        """The two-step sign-in posts twice, so the password step spends
        the account's ONE exemption and the code step could never pass —
        a 2FA account could not use its own unlock at all. The doors
        refund the spend when the outcome is totp_required /
        passkey_required (password verified, flow unfinished)."""
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            self.assertTrue(login_unlock.consume(c, self.email))   # step 1
            login_unlock.refund(c, self.email)                     # totp_required
            self.assertTrue(login_unlock.is_unlocked(c, self.email))
            self.assertTrue(login_unlock.consume(c, self.email),   # step 2
                            "the refunded exemption did not cover the "
                            "code step of the same sign-in")
            self.assertFalse(login_unlock.consume(c, self.email))

    def test_refund_covers_one_retry_not_a_cycle(self):
        """One unlock buys ONE refund. The two-step form needs exactly
        that: the password post spends and is refunded, the code post
        spends for good. If every empty-code post were refunded, a
        consume-refund cycle could hold the exemption open for unlimited
        password-verified posts across the whole window — turning a
        one-attempt spend into a ten-minute pass."""
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            self.assertTrue(login_unlock.consume(c, self.email))
            login_unlock.refund(c, self.email)     # the form's second post
            self.assertTrue(login_unlock.consume(c, self.email))
            login_unlock.refund(c, self.email)     # a replayed empty-code post
            self.assertFalse(login_unlock.is_unlocked(c, self.email),
                             "a second refund re-armed the same unlock")
            self.assertFalse(login_unlock.consume(c, self.email))

    def test_a_fresh_link_refunds_even_after_an_earlier_one_was_spent(self):
        """The refund stamp is per unlock row, not per account — a NEW
        emailed link starts with its own refund, so a user who burned one
        unlock yesterday still gets the two-step allowance today."""
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            self.assertTrue(login_unlock.consume(c, self.email))
            login_unlock.refund(c, self.email)
            self.assertTrue(login_unlock.consume(c, self.email))
            # a fresh link: its refund allowance is its own
            tok2 = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok2)
            self.assertTrue(login_unlock.consume(c, self.email))
            login_unlock.refund(c, self.email)
            self.assertTrue(login_unlock.is_unlocked(c, self.email),
                            "the new unlock's one refund was refused")

    def test_refund_does_not_resurrect_an_old_spend(self):
        """The refund window is tight — a spend from minutes ago (a failed
        attempt someone is retrying) stays spent."""
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            self.assertTrue(login_unlock.consume(c, self.email))
            c.execute("UPDATE login_unlocks SET consumed_at = now() - "
                      "interval '5 minutes' WHERE user_id=%s",
                      (self.user_id,))
            login_unlock.refund(c, self.email)
            self.assertFalse(login_unlock.is_unlocked(c, self.email))

    def test_refund_without_a_spend_is_a_noop(self):
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            login_unlock.refund(c, self.email)   # nothing consumed yet
            self.assertTrue(login_unlock.consume(c, self.email))
            self.assertFalse(login_unlock.consume(c, self.email))

    def test_expired_link_is_dead(self):
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            c.execute("UPDATE login_unlocks SET expires_at = now() - "
                      "interval '1 minute' WHERE user_id=%s", (self.user_id,))
            self.assertIsNone(login_unlock.follow(c, tok))

    def test_expired_exemption_does_not_unlock(self):
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            c.execute("UPDATE login_unlocks SET unlocked_until = now() - "
                      "interval '1 second' WHERE user_id=%s", (self.user_id,))
            self.assertFalse(login_unlock.is_unlocked(c, self.email))
            self.assertFalse(login_unlock.consume(c, self.email))

    def test_minting_burns_older_live_links(self):
        """The reset rule: five clicks must not leave five working
        keys under the mat."""
        with _ctl() as c:
            first = login_unlock.create(c, self.user_id)
            login_unlock.create(c, self.user_id)
            self.assertIsNone(login_unlock.follow(c, first))

    def test_token_is_not_stored_in_the_clear(self):
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            row = c.execute("SELECT token_hash FROM login_unlocks "
                            "WHERE user_id=%s", (self.user_id,)).fetchone()
        self.assertNotEqual(row["token_hash"], tok)
        self.assertNotIn(tok, row["token_hash"])

    def test_send_cap_is_per_address(self):
        """The anti-mail-bomb control, and the reason this door needs no
        human check of its own: a botnet's many IPs buy it nothing, because
        the cap is keyed on the mailbox being flooded."""
        with _ctl() as c:
            for _ in range(login_unlock.SEND_MAX_PER_HOUR):
                self.assertIsNotNone(login_unlock.create(c, self.user_id))
            self.assertIsNone(login_unlock.create(c, self.user_id),
                              "the per-address hourly cap did not hold")

    def test_concurrent_mints_cannot_exceed_the_per_address_cap(self):
        """The cap is the load-bearing anti-mail-bomb control, so it must
        hold against parallel requests on separate connections — the shape
        a botnet across many IPs actually takes. A bare count-then-insert
        would let each read under the cap before any committed; the
        per-user advisory lock serialises the check-and-mint."""
        import threading

        n = login_unlock.SEND_MAX_PER_HOUR + 6
        minted = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def one():
            barrier.wait()                    # fire all at once
            with _ctl() as c:
                tok = login_unlock.create(c, self.user_id)
            with lock:
                minted.append(tok)

        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        got = sum(1 for t in minted if t is not None)
        self.assertEqual(got, login_unlock.SEND_MAX_PER_HOUR,
                         f"minted {got} links, cap is "
                         f"{login_unlock.SEND_MAX_PER_HOUR}")

    def test_unlocking_one_account_does_not_unlock_another(self):
        other = f"unlock-other-{uuid.uuid4().hex[:8]}@example.com"
        other_id = make_user(other)
        try:
            with _ctl() as c:
                tok = login_unlock.create(c, self.user_id)
                login_unlock.follow(c, tok)
                self.assertTrue(login_unlock.is_unlocked(c, self.email))
                self.assertFalse(login_unlock.is_unlocked(c, other))
        finally:
            with _ctl() as c:
                c.execute("DELETE FROM login_unlocks WHERE user_id=%s",
                          (other_id,))


class UnlockDoesNotLaunderTheGateTests(unittest.TestCase):
    """The attack that killed the obvious design. An emailed proof must
    exempt ONE ACCOUNT, never the IP: otherwise anyone holding one account
    they control unlocks their own, wipes the per-IP counter, and resumes
    guessing at a victim — exactly the hole closed in
    security.clear_challenge_risk."""

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def setUp(self):
        self.email = f"unlock-launder-{uuid.uuid4().hex[:8]}@example.com"
        self.user_id = make_user(self.email)
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        with _ctl() as c:
            c.execute("DELETE FROM login_unlocks WHERE user_id=%s",
                      (self.user_id,))

    def test_an_unlock_leaves_the_ip_bucket_standing(self):
        from oikonome.web import security
        ip = "203.0.113.77"
        for _ in range(security._RISK_IP_MAX):
            security.record_challenge_risk(ip, self.email)
        self.assertTrue(security.challenge_warranted(ip))
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            login_unlock.consume(c, self.email)
        self.assertTrue(
            security.challenge_warranted(ip),
            "an emailed unlock cleared the per-IP gate — an attacker can "
            "now launder their address through an account they own")

    def test_an_unlock_leaves_the_site_bucket_standing(self):
        from oikonome.web import security
        for i in range(security._RISK_SITE_MAX):
            security.record_challenge_risk(f"198.51.100.{i % 250}",
                                           f"v{i}@example.com")
        self.assertTrue(security.challenge_warranted("203.0.113.90"))
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
            login_unlock.consume(c, self.email)
        self.assertTrue(security.challenge_warranted("203.0.113.91"),
                        "one unlocked account disarmed the instance burst gate")


class UnlockAnswersTheCheckTests(unittest.TestCase):
    """The point of the feature: turnstile.verify_login accepts the emailed
    proof when no token can be produced."""

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def setUp(self):
        self.email = f"unlock-verify-{uuid.uuid4().hex[:8]}@example.com"
        self.user_id = make_user(self.email)
        from oikonome.web import security
        security._limiter._hits.clear()
        import os
        self._env = dict(os.environ)
        os.environ["OIKONOME_TURNSTILE_SITE_KEY"] = "site"
        os.environ["OIKONOME_TURNSTILE_SECRET"] = "secret"

    def tearDown(self):
        import os
        os.environ.clear()
        os.environ.update(self._env)
        with _ctl() as c:
            c.execute("DELETE FROM login_unlocks WHERE user_id=%s",
                      (self.user_id,))

    def _arm(self, ip):
        from oikonome.web import security
        for _ in range(security._RISK_IP_MAX):
            security.record_challenge_risk(ip, self.email)

    def test_challenged_attempt_with_no_token_is_refused_without_an_unlock(self):
        from oikonome.web import turnstile
        ip = "203.0.113.60"
        self._arm(ip)
        self.assertFalse(turnstile.verify_login("", ip, self.email))

    def test_an_unlocked_account_passes_the_check_with_no_token(self):
        from oikonome.web import turnstile
        ip = "203.0.113.61"
        self._arm(ip)
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
        self.assertTrue(turnstile.verify_login("", ip, self.email))

    def test_the_unlock_covers_one_attempt_only(self):
        from oikonome.web import turnstile
        ip = "203.0.113.62"
        self._arm(ip)
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
        self.assertTrue(turnstile.verify_login("", ip, self.email))
        self.assertFalse(turnstile.verify_login("", ip, self.email),
                         "the exemption survived the attempt it was for")

    def test_an_unlock_for_one_account_does_not_pass_another(self):
        from oikonome.web import turnstile
        ip = "203.0.113.63"
        self._arm(ip)
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
        self.assertFalse(turnstile.verify_login("", ip, "someone@else.com"))

    def test_a_clean_attempt_never_spends_an_unlock(self):
        """No challenge is being asked for, so the exemption must still be
        sitting there afterwards."""
        from oikonome.web import turnstile
        with _ctl() as c:
            tok = login_unlock.create(c, self.user_id)
            login_unlock.follow(c, tok)
        self.assertTrue(turnstile.verify_login("", "198.51.100.200",
                                               self.email))
        with _ctl() as c:
            self.assertTrue(login_unlock.is_unlocked(c, self.email))


if __name__ == "__main__":
    unittest.main()
