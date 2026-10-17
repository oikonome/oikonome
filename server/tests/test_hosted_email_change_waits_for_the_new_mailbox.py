"""A hosted login-email change moves the account only once the NEW
mailbox confirms it.

The email is the login identifier, and on hosted one namespace shared by
every household. A change that moved the account at once let any verified
account hold any address it could type: the row sat on it unverified, and
the address's real owner met "already has an account" at signup with no
door that would hand it back. So the change is a request, its link goes to
the new address, and only the confirm button behind that link (a POST, so
no mail scanner presses it) moves the account. Until then the address is
nobody's, and whoever proves it first keeps it.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class _HostedChange(unittest.TestCase):
    """Hosted mode, mail captured, and the helpers every test here uses."""
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.real_confirm = staticmethod(appmod._deliver_email_change_confirm)

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.confirms: list[tuple] = []
        self.notices: list[tuple] = []
        for name, sink in (("_deliver_email_change_confirm", self.confirms),
                           ("_deliver_email_change_notice", self.notices)):
            p = mock.patch.object(self.appmod, name,
                                  lambda *a, _s=sink: _s.append(a))
            p.start()
            self.addCleanup(p.stop)

    # ---- helpers ---------------------------------------------------------

    def _signup(self, email=None, password=PW):
        from oikonome.auth import signup_invites
        email = email or f"chg-{uuid.uuid4().hex[:10]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            invite = signup_invites.mint(admin, email)
        finally:
            admin.close()
        c = TestClient(self.appmod.app)
        r = c.post("/api/signup", data={"email": email, "password": password,
                                        "invite": invite})
        assert r.status_code == 200, r.text
        return c, email

    def _verified(self, email):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET verified_at=now(), "
                          "first_verified_at=now() WHERE email=%s", (email,))
        finally:
            admin.close()

    def _user(self, uid=None, email=None):
        admin = tenancy.admin_connect()
        try:
            if uid is not None:
                return admin.execute(
                    "SELECT id, email, verified_at FROM users WHERE id=%s",
                    (uid,)).fetchone()
            return admin.execute(
                "SELECT id, email, verified_at FROM users WHERE email=%s",
                (email,)).fetchone()
        finally:
            admin.close()

    def _ask(self, c, new):
        r = c.post("/api/email/change", data={"new_email": new,
                                              "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json(), self.confirms[-1][1]

    def _new(self):
        return f"new-{uuid.uuid4().hex[:10]}@example.dev"



class HostedEmailChangeTests(_HostedChange):
    # ---- the request ------------------------------------------------------

    def test_the_request_moves_nothing_and_mails_only_the_new_address(self):
        c, old = self._signup()
        uid = self._user(email=old)["id"]
        new = self._new()
        body, _ = self._ask(c, new)
        self.assertEqual(body["email"], old)
        self.assertEqual(body["pending_email"], new)
        self.assertEqual(self._user(uid)["email"], old)
        self.assertEqual([a[0] for a in self.confirms], [new])
        self.assertEqual(self.notices, [], "nothing moved, nothing to undo")
        self.assertEqual(c.get("/api/me").json()["pending_email"], new)

    def test_a_pending_change_never_tells_an_app_to_reverify_the_address_it_keeps(
            self):
        """The installed store app reads `verify_sent` as "the sign-in
        address moved and is unverified": it marks the cached account
        unverified and shows a banner about the OLD address, whose Resend
        then answers "already verified". On a pending change the sign-in
        address stands, verified, so the answer must not say a
        verification went to it — neither the first ask nor asking again."""
        c, old = self._signup()
        self._verified(old)
        new = self._new()
        body, _ = self._ask(c, new)
        self.assertEqual(body["email"], old)
        self.assertFalse(body.get("verify_sent"))
        from oikonome.web import security
        security._limiter._hits.clear()
        again = c.post("/api/email/change", data={"new_email": new,
                                                  "password": PW})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertTrue(again.json()["resent"])
        self.assertEqual(again.json()["email"], old)
        self.assertFalse(again.json().get("verify_sent"))
        resend = c.post("/api/email/change/resend")
        self.assertEqual(resend.status_code, 200, resend.text)
        self.assertFalse(resend.json().get("verify_sent"))
        self.assertIsNotNone(self._user(email=old)["verified_at"])

    def test_a_pending_address_never_blocks_its_owners_signup(self):
        """The squat: a request for a stranger's address must leave that
        address free, and the request then finds it taken."""
        c, old = self._signup()
        uid = self._user(email=old)["id"]
        victim = self._new()
        _, token = self._ask(c, victim)
        # the address's real owner signs up while the request waits
        self._signup(victim, password="the-victims-own-password")
        r = TestClient(self.appmod.app).post("/account/email-change",
                                             data={"token": token})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._user(uid)["email"], old)
        # and the refusal spent the link: pressing again cannot retry
        r = TestClient(self.appmod.app).post("/account/email-change",
                                             data={"token": token})
        self.assertEqual(r.status_code, 400)

    # ---- the link -----------------------------------------------------------

    def test_following_the_link_moves_nothing_and_the_button_moves_it(self):
        c, old = self._signup()
        self._verified(old)
        uid = self._user(email=old)["id"]
        new = self._new()
        _, token = self._ask(c, new)
        anon = TestClient(self.appmod.app)
        g = anon.get(f"/account/email-change?token={token}")
        self.assertEqual(g.status_code, 200)
        self.assertIn("Confirm the new email", g.text)
        self.assertEqual(self._user(uid)["email"], old,
                         "a scanner's GET must not move the account")
        r = anon.post("/account/email-change", data={"token": token})
        self.assertEqual(r.status_code, 200, r.text)
        moved = self._user(uid)
        self.assertEqual(moved["email"], new)
        self.assertIsNotNone(moved["verified_at"],
                             "the press proved the new mailbox")
        # the address left behind is told, with its way back
        self.assertEqual(len(self.notices), 1)
        gone, to, _tid, revert = self.notices[0]
        self.assertEqual((gone, to), (old, new))
        self.assertIsNotNone(revert)
        # and the revert link still undoes it, as for any change
        rv = anon.post("/account/email-revert", data={"token": revert})
        self.assertEqual(rv.status_code, 200, rv.text)
        self.assertEqual(self._user(uid)["email"], old)

    def test_an_address_never_proved_gets_no_way_back(self):
        c, old = self._signup()           # never verified
        uid = self._user(email=old)["id"]
        _, token = self._ask(c, self._new())
        TestClient(self.appmod.app).post("/account/email-change",
                                         data={"token": token})
        self.assertNotEqual(self._user(uid)["email"], old)
        self.assertIsNone(self.notices[0][3])

    # ---- what ends a request ---------------------------------------------

    def test_a_newer_request_ends_the_earlier_one(self):
        c, old = self._signup()
        uid = self._user(email=old)["id"]
        _, first = self._ask(c, self._new())
        second_addr = self._new()
        _, second = self._ask(c, second_addr)
        anon = TestClient(self.appmod.app)
        self.assertEqual(anon.post("/account/email-change",
                                   data={"token": first}).status_code, 400)
        self.assertEqual(anon.post("/account/email-change",
                                   data={"token": second}).status_code, 200)
        self.assertEqual(self._user(uid)["email"], second_addr)

    def test_signing_out_the_session_that_asked_ends_the_request(self):
        """A request speaks for the credential that made it: a reset, a
        sign-out everywhere or a password change from another session all
        delete it, and the link must stop with it."""
        c, old = self._signup()
        uid = self._user(email=old)["id"]
        _, token = self._ask(c, self._new())
        c.post("/api/logout")
        r = TestClient(self.appmod.app).post("/account/email-change",
                                             data={"token": token})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._user(uid)["email"], old)

    def test_a_request_dies_when_the_account_leaves_the_address_it_was_made_from(
            self):
        c, old = self._signup()
        uid = self._user(email=old)["id"]
        _, token = self._ask(c, self._new())
        elsewhere = self._new()
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET email=%s WHERE id=%s",
                          (elsewhere, uid))
        finally:
            admin.close()
        r = TestClient(self.appmod.app).post("/account/email-change",
                                             data={"token": token})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._user(uid)["email"], elsewhere)

    def test_the_account_can_withdraw_its_request(self):
        c, old = self._signup()
        _, token = self._ask(c, self._new())
        r = c.post("/api/email/change/cancel")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cancelled"])
        self.assertIsNone(c.get("/api/me").json()["pending_email"])
        self.assertEqual(TestClient(self.appmod.app).post(
            "/account/email-change", data={"token": token}).status_code, 400)

    def test_the_app_role_cannot_read_a_requests_token(self):
        import psycopg
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT token_hash FROM email_change_requests")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE email_change_requests SET used_at=NULL")


class TheMoveEndsRecoveriesMailedToTheOldAddressTests(_HostedChange):
    """While a change waits for its confirm, the address being left still
    names the account, so the reset form can mail a link to it — and a
    cooling-off factor reset can start its clock. The move must end both:
    an address that never proved its mailbox gets no way back by design,
    and a reset link minted to it would be exactly that way back."""

    def _press(self, token):
        r = TestClient(self.appmod.app).post("/account/email-change",
                                             data={"token": token})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_reset_link_to_the_old_address_dies_with_the_move(self):
        from oikonome.auth import factor_reset, reset as reset_mod
        c, old = self._signup()
        uid = self._user(email=old)["id"]
        _, token = self._ask(c, self._new())
        admin = tenancy.admin_connect()
        try:
            link = reset_mod.create_reset(admin, uid)
            factor_reset.request(admin, uid)
            self.assertIsNotNone(reset_mod.lookup_reset(admin, link))
        finally:
            admin.close()
        self._press(token)
        admin = tenancy.admin_connect()
        try:
            self.assertIsNone(reset_mod.lookup_reset(admin, link))
            self.assertIsNone(factor_reset.pending(admin, uid))
        finally:
            admin.close()



class TheGateHearsAboutTheMoveTests(_HostedChange):
    """A hosted add-on may keep a record keyed by address, so it is told
    inside the moving transaction which address is being left, while the
    row still holds it, and whenever an address is stamped verified."""

    def setUp(self):
        super().setUp()
        from oikonome import ext
        self.calls: list[tuple] = []
        calls, test = self.calls, self

        class Recording(ext.Gate):
            def on_login_email_moved(self, admin, tenant_id, user_id,
                                     old_email, new_email):
                held = admin.execute("SELECT email FROM users WHERE id=%s",
                                     (user_id,)).fetchone()["email"]
                calls.append(("moving", user_id, old_email, new_email, held))

            def on_email_verified(self, admin, tenant_id, user_id):
                row = admin.execute(
                    "SELECT email, verified_at IS NOT NULL AS v FROM users "
                    "WHERE id=%s", (user_id,)).fetchone()
                test.assertTrue(row["v"])
                calls.append(("verified", user_id, row["email"]))

        ext.set_gate(Recording())
        self.addCleanup(ext.set_gate, None)

    def _press(self, token):
        return TestClient(self.appmod.app).post("/account/email-change",
                                                data={"token": token})

    def test_a_confirmed_move_is_told_before_the_row_changes(self):
        c, old = self._signup()
        self._verified(old)
        uid = self._user(email=old)["id"]
        new = self._new()
        _, token = self._ask(c, new)
        self.assertEqual(self.calls, [], "a request moves nothing")
        self.assertEqual(self._press(token).status_code, 200)
        self.assertEqual(self.calls, [("moving", uid, old, new, old),
                                      ("verified", uid, new)])

    def test_clicking_the_verification_link_is_told_once(self):
        from oikonome.auth import email_verify
        _, email = self._signup()
        uid = self._user(email=email)["id"]
        admin = tenancy.admin_connect()
        try:
            t1 = email_verify.create(admin, uid)
            t2 = email_verify.create(admin, uid)
            r1 = email_verify.lookup(admin, t1)
            self.assertTrue(email_verify.consume(admin, r1["id"], uid))
            r2 = email_verify.lookup(admin, t2)
            self.assertTrue(email_verify.consume(admin, r2["id"], uid))
        finally:
            admin.close()
        self.assertEqual(self.calls, [("verified", uid, email)])

    def test_an_unavailable_gate_moves_nothing_and_keeps_the_link(self):
        from oikonome import ext
        c, old = self._signup()
        uid = self._user(email=old)["id"]
        new = self._new()
        _, token = self._ask(c, new)
        ext.set_gate(ext._FailClosedGate())
        r = self._press(token)
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(self._user(uid)["email"], old)
        ext.set_gate(None)
        self.assertEqual(self._press(token).status_code, 200)
        self.assertEqual(self._user(uid)["email"], new)


class UndoingAMoveNeverWaitsOnTheGateTests(_HostedChange):
    """The revert link is how an owner takes the account back from whoever
    moved it. Telling the add-on which address the revert leaves is a
    courtesy to the add-on: a hook that fails — even with a database error
    that would poison the transaction — is logged, and the account still
    goes back."""

    def test_a_hook_failing_in_the_database_does_not_stop_the_revert(self):
        from oikonome import ext
        c, old = self._signup()
        self._verified(old)
        uid = self._user(email=old)["id"]
        new = self._new()
        _, token = self._ask(c, new)
        anon = TestClient(self.appmod.app)
        self.assertEqual(anon.post("/account/email-change",
                                   data={"token": token}).status_code, 200)
        revert = self.notices[-1][3]

        class Failing(ext.Gate):
            def on_login_email_moved(self, admin, tenant_id, user_id,
                                     old_email, new_email):
                admin.execute("SELECT 1/0")

        ext.set_gate(Failing())
        self.addCleanup(ext.set_gate, None)
        with mock.patch.object(self.appmod, "_deliver_reset"), \
                mock.patch.object(self.appmod,
                                  "_deliver_email_reverted_notice"):
            r = anon.post("/account/email-revert", data={"token": revert})
        self.assertEqual(r.status_code, 200, r.text)
        back = self._user(uid)
        self.assertEqual(back["email"], old)
        self.assertIsNotNone(back["verified_at"])


class OperatorClearEndsRevertLinksTests(unittest.TestCase):
    """A revert ends in a password reset to the address the account left.
    On an account the operator has just left with no factor, that reset
    asks for no recovery code, so a live revert link would hand the
    account to whoever reads the old inbox."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_an_operator_factor_clear_burns_every_revert_link(self):
        from oikonome.auth import email_revert, factor_reset, passwords
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"oc-{uuid.uuid4().hex[:8]}")
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s,%s,%s,now()) RETURNING id",
                (tid, f"oc-{uuid.uuid4().hex[:8]}@x.dev",
                 passwords.hash_password(PW))).fetchone()["id"]
            token = email_revert.create(admin, uid, "left@x.dev",
                                        "now@x.dev")
            self.assertIsNotNone(email_revert.lookup(admin, token))
            factor_reset.clear_second_factor(admin, uid)
            self.assertIsNone(email_revert.lookup(admin, token))
        finally:
            admin.close()


class _CappedMail(_HostedChange):
    """Helpers for the confirmation-mail caps and the resend door."""

    def _ask_from(self, c, new, *, legacy=True):
        from oikonome.web import security
        security._limiter._hits.clear()       # the per-address cap is not
        data = {"new_email": new}             # what is under test here
        if legacy:
            data["password"] = PW
        else:
            r = c.post("/api/auth/elevate", json={"password": PW})
            self.assertEqual(r.status_code, 200, r.text)
        return c.post("/api/email/change", data=data)

    def _second_session(self, email):
        other = TestClient(self.appmod.app)
        r = other.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return other

    def _age_mail(self, address, hours):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE email_change_requests SET mail_sent_at = mail_sent_at"
                " - make_interval(hours => %s) WHERE lower(new_email)=%s",
                (hours, address))
        finally:
            admin.close()

    def _fill_hour(self, victim):
        for _ in range(3):
            r = self._ask_from(self._signup()[0], victim)
            self.assertEqual(r.status_code, 200, r.text)

class TheConfirmationMailIsCappedTests(_CappedMail):
    """The confirmation goes to an address the caller typed, so it is
    capped per recipient and per account — counting only mail that was
    actually sent, and never the caller's own mail against the recipient.
    Past a cap the answer is an honest 429 with the wait; past the
    recipient's cap the request is recorded, but nothing is rotated."""

    def test_one_inbox_gets_at_most_three_confirmations_an_hour(self):
        victim = self._new()
        self._fill_hour(victim)
        c, email = self._signup()
        other = self._second_session(email)
        r = self._ask_from(c, victim, legacy=False)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn("none was sent", r.json()["detail"])
        self.assertIn("ask again in", r.json()["detail"])
        self.assertTrue(0 < int(r.headers["Retry-After"]) <= 3600)
        self.assertEqual([a[0] for a in self.confirms], [victim] * 3)
        self.assertEqual(other.get("/api/me").status_code, 200,
                         "nothing was rotated for a mail never sent")

    def test_a_withheld_request_is_not_shown_as_waiting_for_a_link(self):
        """No mail and no link went out for a request the recipient cap
        held back, so the settings card must not say "waiting for
        confirmation, the link works until…", and the refusal must not
        send the person to an inbox holding nothing of theirs: the
        recipient cap counts only other accounts' mail."""
        victim = self._new()
        self._fill_hour(victim)
        c, _ = self._signup()
        r = self._ask_from(c, victim, legacy=False)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertNotIn("check that inbox", r.json()["detail"])
        me = c.get("/api/me").json()
        self.assertIsNone(me["pending_email"])
        self.assertIsNone(me["pending_email_expires_at"])
        # asking again once the cap allows is the plain change, mailed
        self._age_mail(victim, 2)
        r = self._ask_from(c, victim, legacy=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.confirms[-1][0], victim)
        self.assertEqual(c.get("/api/me").json()["pending_email"], victim)

    def test_a_pending_change_is_read_without_an_admin_connection(self):
        """/api/me runs on every page load: whether the pending link was
        mailed is read on the pooled connection, never on a fresh admin
        one opened per call."""
        c, _ = self._signup()
        new = self._new()
        self._ask(c, new)
        with mock.patch.object(self.appmod.tenancy, "admin_connect",
                               side_effect=AssertionError(
                                   "admin connection on /api/me")):
            r = c.get("/api/me")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pending_email"], new)

    def test_the_app_role_may_read_whether_a_link_was_mailed(self):
        import psycopg
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            conn.execute("SELECT mail_sent_at FROM email_change_requests"
                         " LIMIT 1")

    def _backdate(self, new, *, before_column: bool):
        """Clear a request's mail stamp and place its creation either
        before or after the migration that added the column."""
        from oikonome.auth import email_change
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE email_change_requests SET mail_sent_at = NULL,"
                " created_at = (SELECT applied_at FROM schema_migrations"
                "                WHERE name = %s)"
                "   + CASE WHEN %s THEN interval '-1 minute'"
                "          ELSE interval '1 minute' END"
                " WHERE lower(new_email) = %s",
                (email_change._MAIL_SENT_MIGRATION, before_column, new))
        finally:
            admin.close()

    def test_a_request_from_before_the_mail_stamp_still_shows_its_link(self):
        """Rows written before `mail_sent_at` existed carry NULL although
        their link went out (a withheld mail recorded nothing then): the
        card must not hide a link that is sitting in the new inbox."""
        c, _ = self._signup()
        new = self._new()
        self._ask(c, new)
        self._backdate(new, before_column=True)
        self.assertEqual(c.get("/api/me").json()["pending_email"], new)

    def test_an_unstamped_request_after_the_column_is_not_shown(self):
        c, _ = self._signup()
        new = self._new()
        self._ask(c, new)
        self._backdate(new, before_column=False)
        self.assertIsNone(c.get("/api/me").json()["pending_email"])

    def test_a_store_app_held_by_the_recipient_cap_spends_nothing(self):
        victim = self._new()
        self._fill_hour(victim)
        c, _ = self._signup()
        r = self._ask_from(c, victim)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIsNone(c.get("/api/me").json()["pending_email"])

    def test_one_account_mails_at_most_five_addresses_a_day(self):
        c, _ = self._signup()
        targets = [self._new() for _ in range(6)]
        for t in targets[:5]:
            self.assertEqual(self._ask_from(c, t).status_code, 200)
        r = self._ask_from(c, targets[5], legacy=False)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn("this account has asked", r.json()["detail"])
        self.assertGreater(int(r.headers["Retry-After"]), 23 * 3600)
        self.assertEqual([a[0] for a in self.confirms], targets[:5])
        self.assertEqual(c.get("/api/me").json()["pending_email"],
                         targets[4], "nothing recorded past the account cap")

    def test_the_callers_own_mail_never_counts_against_the_recipient(self):
        """The owner whose first mail went to spam asks again: their own
        mail is the account cap's business, not the address's."""
        c, _ = self._signup()
        new = self._new()
        self.assertEqual(self._ask_from(c, new).status_code, 200)
        for _ in range(3):
            from oikonome.web import security
            security._limiter._hits.clear()
            r = c.post("/api/email/change/resend")
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([a[0] for a in self.confirms], [new] * 4)

    def test_requests_whose_mail_was_withheld_fill_no_cap(self):
        """Another household cannot silence an address with requests that
        never mailed it."""
        from oikonome.auth import email_change
        victim = self._new()
        admin = tenancy.admin_connect()
        try:
            for _ in range(6):
                _c, email = self._signup()
                email_change.request(admin, self._user(email=email)["id"],
                                     email, victim, mail_sent=False)
        finally:
            admin.close()
        c, _ = self._signup()
        self.assertEqual(self._ask_from(c, victim).status_code, 200)
        self.assertEqual([a[0] for a in self.confirms], [victim])

    def test_the_intake_spam_gate_runs_before_the_mail(self):
        from oikonome.web import spamdefense
        with mock.patch.object(spamdefense, "check",
                               return_value={"blocked": True,
                                             "reason": "blocked"}) as chk, \
             mock.patch.object(self.appmod, "_smtp_for") as smtp:
            self.real_confirm("x@spam.example", "tok", None)
        chk.assert_called_once_with("email_change", "x@spam.example")
        smtp.assert_not_called()


class TheResendDoorTests(_CappedMail):
    """A mail that went to spam is fixed by asking for it again, not by a
    second change that costs the owner their passkeys and devices."""

    def test_a_resend_mails_a_fresh_link_and_rotates_nothing(self):
        c, email = self._signup()
        new = self._new()
        self.assertEqual(self._ask_from(c, new).status_code, 200)
        first = self.confirms[-1][1]
        other = self._second_session(email)    # signed in after the change
        r = c.post("/api/email/change/resend")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["resent"])
        self.assertEqual(r.json()["passkeys_removed"], 0)
        self.assertEqual(other.get("/api/me").status_code, 200)
        second = self.confirms[-1][1]
        anon = TestClient(self.appmod.app)
        self.assertEqual(anon.post("/account/email-change",
                                   data={"token": first}).status_code, 400,
                         "the newest mail is the one that works")
        self.assertEqual(anon.post("/account/email-change",
                                   data={"token": second}).status_code, 200)

    def test_changing_to_the_same_address_again_is_a_resend(self):
        c, email = self._signup()
        new = self._new()
        self.assertEqual(self._ask_from(c, new).status_code, 200)
        other = self._second_session(email)
        r = self._ask_from(c, new)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["resent"])
        self.assertEqual(other.get("/api/me").status_code, 200,
                         "a second full rotation for the same request")
        self.assertEqual([a[0] for a in self.confirms], [new, new])

    def test_with_nothing_pending_a_resend_is_refused(self):
        c, _ = self._signup()
        self.assertEqual(c.post("/api/email/change/resend").status_code, 409)

    def test_only_the_credential_that_asked_may_resend(self):
        c, email = self._signup()
        self.assertEqual(self._ask_from(c, self._new()).status_code, 200)
        other = self._second_session(email)
        self.assertEqual(other.post("/api/email/change/resend").status_code,
                         409)

    def test_a_resend_is_held_to_the_account_cap(self):
        c, _ = self._signup()
        for _ in range(4):
            self.assertEqual(self._ask_from(c, self._new()).status_code, 200)
        last = self._new()
        self.assertEqual(self._ask_from(c, last).status_code, 200)
        r = c.post("/api/email/change/resend")
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn("this account has asked", r.json()["detail"])

    def test_a_withheld_request_is_mailed_and_rotated_once_the_cap_allows(
            self):
        victim = self._new()
        self._fill_hour(victim)
        c, email = self._signup()
        self.assertEqual(self._ask_from(c, victim, legacy=False).status_code,
                         429)
        self.assertEqual(c.post("/api/email/change/resend").status_code, 429,
                         "still capped")
        self._age_mail(victim, 2)          # the hour's mail has aged out
        other = self._second_session(email)
        r = c.post("/api/email/change/resend")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.confirms[-1][0], victim)
        self.assertEqual(other.get("/api/me").status_code, 401,
                         "the rotation the withheld request skipped ran now")


if __name__ == "__main__":
    unittest.main()
