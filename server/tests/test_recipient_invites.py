"""Nobody joins a household's financial mail without saying yes.

Adding an address to Settings → Email must not enrol it outright: the next
morning that mailbox would receive real balances, spending and a verdict,
and the first say the person on the other end had in it would come after
reading one. A typo'd address announces itself no better — the mail goes
somewhere, and the owner has no way to tell "they read it every day" from
"a stranger has been getting our finances for a month".

The contract these tests pin, in the order it has to hold:

* saving a new recipient INVITES them — it does not enrol them;
* an unanswered invite receives nothing else, ever, and the worker skips
  the address entirely;
* accepting enrols; declining is final and survives a re-add;
* the emailed link only PEEKS on GET — a mail scanner must not be able to
  consent on a person's behalf;
* a token is spent when it is answered, and a resend kills the old one;
* both settings doors invite, because a policy that lives in one door
  drifts out of the other;
* the owner is never invited to their own mail;
* the migration's backfill leaves working mail working.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.notify import recipient_invites

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db, write_config

PW = "correct-horse-battery"


def _tenant():
    conn = make_db()
    tid = str(conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
    return conn, tid


def _addr(prefix="invitee"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev"


def _row(tid, email):
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        return admin.execute(
            "SELECT * FROM recipient_invites WHERE tenant_id=%s AND email=%s",
            (tid, email.lower())).fetchone()
    finally:
        admin.close()


def _signed_in_client():
    """A real signed-in session, as the settings doors see it."""
    import oikonome.web.app as appmod
    from oikonome.web import security
    security._limiter._hits.clear()          # signup limit is 5/h per IP
    appmod.DEV_MODE = True
    client = TestClient(appmod.app)
    email = f"owner-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    tid = client.get("/api/me").json()["tenant_id"]
    return client, tid, email


class InviteLifecycleTests(unittest.TestCase):
    """The module itself, without any HTTP in the way."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn, self.tid = _tenant()
        self.addCleanup(self.conn.close)
        self.email = _addr()

    def test_a_fresh_invite_is_pending_and_not_accepted(self):
        token = recipient_invites.invite(self.tid, self.email)
        self.assertTrue(token)
        self.assertEqual(
            recipient_invites.states(self.tid, [self.email])[self.email]
            ["state"], "invited")
        self.assertEqual(recipient_invites.accepted(self.tid, [self.email]),
                         set(),
                         "an invitation nobody has answered must not enrol "
                         "anybody — this is the whole feature")

    def test_lookup_does_not_answer_the_invite(self):
        """A mail scanner prefetching the link must
        not be able to consent to somebody's financial mail for them."""
        token = recipient_invites.invite(self.tid, self.email)
        got = recipient_invites.lookup(token)
        self.assertEqual(got["email"], self.email.lower())
        self.assertEqual(
            recipient_invites.states(self.tid, [self.email])[self.email]
            ["state"], "invited")
        # and the token still works afterwards
        self.assertIsNotNone(recipient_invites.accept(token))

    def test_accepting_enrols_and_spends_the_token(self):
        token = recipient_invites.invite(self.tid, self.email)
        self.assertIsNotNone(recipient_invites.accept(token))
        self.assertEqual(recipient_invites.accepted(self.tid, [self.email]),
                         {self.email.lower()})
        self.assertIsNone(recipient_invites.accept(token),
                          "a one-use link is one use — a double submit from "
                          "two tabs must not succeed twice")
        self.assertIsNone(recipient_invites.lookup(token))

    def test_declining_is_final(self):
        token = recipient_invites.invite(self.tid, self.email)
        self.assertIsNotNone(recipient_invites.decline(token))
        self.assertEqual(
            recipient_invites.states(self.tid, [self.email])[self.email]
            ["state"], "declined")
        self.assertEqual(recipient_invites.accepted(self.tid, [self.email]),
                         set())
        self.assertIsNone(recipient_invites.accept(token),
                          "the same link must not be able to undo a no")

    def test_re_adding_someone_who_declined_does_not_ask_again(self):
        """The one answer that has to survive being re-added. Otherwise a
        recipient who said no gets asked again every time the owner touches
        their settings, which is a nuisance dressed up as a choice."""
        recipient_invites.decline(recipient_invites.invite(self.tid,
                                                           self.email))
        self.assertIsNone(recipient_invites.invite(self.tid, self.email))
        self.assertEqual(
            recipient_invites.states(self.tid, [self.email])[self.email]
            ["state"], "declined")

    def test_re_adding_someone_who_accepted_does_not_ask_again(self):
        """Consent is not revoked by being taken off a list and put back."""
        recipient_invites.accept(recipient_invites.invite(self.tid,
                                                          self.email))
        self.assertIsNone(recipient_invites.invite(self.tid, self.email))
        self.assertEqual(recipient_invites.accepted(self.tid, [self.email]),
                         {self.email.lower()})

    def _age_last_send(self):
        """Push the row past the re-send cooldown — the shape
        of a real Resend click later on."""
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "UPDATE recipient_invites SET last_sent_at = now() - "
                "interval '11 minutes' WHERE tenant_id=%s AND email=%s",
                (self.tid, self.email.lower()))
        finally:
            admin.close()

    def test_resending_kills_the_previous_link(self):
        first = recipient_invites.invite(self.tid, self.email)
        self._age_last_send()
        second = recipient_invites.invite(self.tid, self.email)
        self.assertNotEqual(first, second)
        self.assertIsNone(recipient_invites.accept(first),
                          "one row per address means the old link stops "
                          "working by construction, not by remembering to "
                          "burn it")
        self.assertIsNotNone(recipient_invites.accept(second))

    def test_rapid_reinvite_is_cooled_down_and_keeps_the_live_link(self):
        """Without a cooldown an unanswered address is re-tokenised and
        re-MAILED on every settings save that carries it — toggle the list,
        one mail per request, from doors with no limiter. Inside the
        cooldown the mint returns None (nothing sent) and the outstanding
        link keeps working."""
        first = recipient_invites.invite(self.tid, self.email)
        self.assertIsNone(recipient_invites.invite(self.tid, self.email),
                          "the immediate re-mint should be cooled down")
        self.assertIsNotNone(recipient_invites.accept(first),
                             "the cooldown must not kill the live link")

    def test_an_expired_invite_is_reported_and_unusable(self):
        token = recipient_invites.invite(self.tid, self.email)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "UPDATE recipient_invites SET expires_at = now() - "
                "interval '1 day' WHERE tenant_id=%s AND email=%s",
                (self.tid, self.email.lower()))
        finally:
            admin.close()
        self.assertEqual(
            recipient_invites.states(self.tid, [self.email])[self.email]
            ["state"], "expired")
        self.assertIsNone(recipient_invites.accept(token))
        self.assertIsNone(recipient_invites.lookup(token))

    def test_an_invite_is_scoped_to_its_household(self):
        """Accepting for one tenant must not enrol the same address on
        another — two households can each invite the same person, and each
        answer is theirs alone."""
        other_conn, other_tid = _tenant()
        self.addCleanup(other_conn.close)
        recipient_invites.accept(recipient_invites.invite(self.tid,
                                                          self.email))
        self.assertEqual(recipient_invites.accepted(other_tid, [self.email]),
                         set())

    def test_address_matching_is_case_insensitive(self):
        token = recipient_invites.invite(self.tid, self.email.upper())
        recipient_invites.accept(token)
        self.assertEqual(
            recipient_invites.accepted(self.tid, [self.email.upper()]),
            {self.email.lower()})

    def test_an_unknown_address_has_no_state(self):
        self.assertEqual(recipient_invites.states(self.tid, [_addr()]), {})
        self.assertIsNone(recipient_invites.lookup("not-a-real-token"))
        self.assertIsNone(recipient_invites.accept(""))


class WorkerGateTests(unittest.TestCase):
    """`_recipients_raw` is where the invitation actually bites."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn, self.tid = _tenant()
        self.addCleanup(self.conn.close)
        self.owner = f"owner-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'x','owner',now())",
                (self.tid, self.owner))
        finally:
            admin.close()

    def _recips(self):
        from oikonome.jobs.worker import _recipients
        return _recipients(self.conn, self.tid)

    def test_an_unanswered_invite_receives_nothing(self):
        guest = _addr()
        write_config(self.conn, email_recipients=[guest])
        recipient_invites.invite(self.tid, guest)
        got = self._recips()
        self.assertNotIn(guest, [e.lower() for e in got],
                         "an address that has not agreed must not be mailed "
                         "a household's balances")
        self.assertIn(self.owner, got,
                      "and the owner keeps receiving their own verdict "
                      "regardless — they are on the list by construction")

    def test_accepting_starts_the_mail(self):
        guest = _addr()
        write_config(self.conn, email_recipients=[guest])
        recipient_invites.accept(recipient_invites.invite(self.tid, guest))
        self.assertIn(guest, [e.lower() for e in self._recips()])

    def test_a_never_invited_address_receives_nothing(self):
        """The restore path, and any config that reaches a tenant without
        going through the invite door: on the list, no invitation row at
        all."""
        guest = _addr()
        write_config(self.conn, email_recipients=[guest])
        self.assertNotIn(guest, [e.lower() for e in self._recips()])

    def test_declining_stops_the_mail(self):
        guest = _addr()
        write_config(self.conn, email_recipients=[guest])
        recipient_invites.decline(recipient_invites.invite(self.tid, guest))
        self.assertNotIn(guest, [e.lower() for e in self._recips()])

    def test_the_owner_is_never_gated_by_an_invitation(self):
        """Nobody invites you to your own household's mail. The owner is
        resolved from `users`, so listing themselves must not put them
        behind a link they would have to click."""
        write_config(self.conn, email_recipients=[self.owner])
        self.assertIn(self.owner, self._recips())


class SettingsDoorTests(unittest.TestCase):
    """Saving the list is what sends the invitation — on BOTH doors."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.client, self.tid, self.owner = _signed_in_client()
        self.sent = []
        # the mail itself is _deliver_recipient_invite's job (covered by its
        # own tests); here we care that the door reaches for it exactly when
        # it should
        patch = mock.patch("oikonome.web.mailguard.invite_added",
                           side_effect=lambda user, emails:
                           self.sent.extend(emails))
        patch.start()
        self.addCleanup(patch.stop)

    def _save(self, recipients):
        return self.client.post("/api/settings",
                                json={"email_recipients": recipients})

    def test_saving_a_new_recipient_invites_them(self):
        guest = _addr()
        r = self._save([guest])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.sent, [guest])

    def test_saving_the_same_list_again_does_not_re_invite(self):
        """An unrelated settings save must not re-mail everyone on the list —
        both doors write the WHOLE list every time."""
        guest = _addr()
        self._save([guest])
        self.sent.clear()
        self._save([guest])
        self.assertEqual(self.sent, [])

    def test_only_the_newly_added_address_is_invited(self):
        first, second = _addr(), _addr()
        self._save([first])
        self.sent.clear()
        self._save([first, second])
        self.assertEqual(self.sent, [second])

    def test_the_jinja_door_invites_too(self):
        """The transitional form writes the same config key, so a policy
        that lives in only one door has already drifted."""
        guest = _addr()
        r = self.client.post("/settings/email",
                             data={"email_recipients": guest,
                                   "email_send_hour_utc": 14},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(self.sent, [guest])

    def test_removing_a_recipient_invites_nobody(self):
        guest = _addr()
        self._save([guest])
        self.sent.clear()
        self._save([])
        self.assertEqual(self.sent, [])


class RecipientStatusTests(unittest.TestCase):
    """What Settings shows — the answer to "did they get it, and did they
    say yes", which is the question the old comma-separated box could not
    answer at all."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.client, self.tid, self.owner = _signed_in_client()

    def _status(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        return {row["email"].lower(): row
                for row in r.json().get("email_recipient_status") or []}

    def test_an_invited_recipient_reads_as_invited(self):
        guest = _addr()
        with mock.patch("oikonome.web.mailguard.invite_added",
                        side_effect=lambda u, e: [
                            recipient_invites.invite(self.tid, x) for x in e]):
            self.client.post("/api/settings",
                             json={"email_recipients": [guest]})
        self.assertEqual(self._status()[guest.lower()]["invite"], "invited")

    def test_an_accepted_recipient_reads_as_accepted(self):
        guest = _addr()
        with mock.patch("oikonome.web.mailguard.invite_added",
                        side_effect=lambda u, e: [
                            recipient_invites.accept(
                                recipient_invites.invite(self.tid, x))
                            for x in e]):
            self.client.post("/api/settings",
                             json={"email_recipients": [guest]})
        self.assertEqual(self._status()[guest.lower()]["invite"], "accepted")

    def test_the_owner_row_carries_no_invite_state(self):
        """The owner can never need an invitation, so a badge claiming one
        would be a control that lies about itself."""
        row = self._status()[self.owner.lower()]
        self.assertTrue(row["owner"])
        self.assertIsNone(row["invite"])

    def test_resend_refuses_an_address_that_is_not_a_saved_recipient(self):
        """Otherwise this door is a way to mail arbitrary people from
        somebody else's instance."""
        r = self.client.post("/api/settings/recipients/resend",
                             json={"email": _addr()})
        self.assertEqual(r.status_code, 400)


class InviteDoorTests(unittest.TestCase):
    """The recipient's own two pages."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        appmod.DEV_MODE = True
        self.client = TestClient(appmod.app)
        self.conn, self.tid = _tenant()
        self.addCleanup(self.conn.close)
        self.email = _addr()
        self.token = recipient_invites.invite(self.tid, self.email)

    def test_get_shows_the_offer_without_accepting_it(self):
        r = self.client.get(f"/recipient-invite?token={self.token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.email, r.text)
        self.assertEqual(recipient_invites.accepted(self.tid, [self.email]),
                         set(),
                         "a prefetched GET must never enrol anybody")

    def test_post_accepts(self):
        r = self.client.post("/recipient-invite",
                             data={"token": self.token, "answer": "accept"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(recipient_invites.accepted(self.tid, [self.email]),
                         {self.email.lower()})

    def test_declining_asks_first_because_it_cannot_be_undone(self):
        """`invite()` refuses to re-ask a declined address, on purpose — so
        a misclick here locks the person AND the household out with no way
        back. Accepting is reversible and gets no such step."""
        r = self.client.post("/recipient-invite",
                             data={"token": self.token, "answer": "decline"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Just to be sure", r.text)
        self.assertIn("This is final", r.text)
        state = recipient_invites.states(self.tid, [self.email])
        self.assertNotEqual(state.get(self.email, {}).get("state"),
                            "declined",
                            "the first click must not decide anything")

    def test_post_declines(self):
        # the confirm step, then the answer
        self.client.post("/recipient-invite",
                         data={"token": self.token, "answer": "decline"})
        r = self.client.post(
            "/recipient-invite",
            data={"token": self.token, "answer": "decline_confirmed"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            recipient_invites.states(self.tid, [self.email])[self.email]
            ["state"], "declined")

    def test_a_bad_token_is_a_400_that_explains_itself(self):
        # The copy deliberately omits "or already answered": an ANSWERED
        # link never reaches this branch (it keeps its hash so the page can
        # say what you already chose), so claiming it here would describe a
        # case that cannot land here. What is pinned is the invariant — an
        # unusable link gets a 400 that says why.
        for token in ("", "nonsense"):
            r = self.client.get(f"/recipient-invite?token={token}")
            self.assertEqual(r.status_code, 400)
            self.assertIn("invalid or expired", r.text)
            r = self.client.post("/recipient-invite",
                                 data={"token": token, "answer": "accept"})
            self.assertEqual(r.status_code, 400)

    def test_accepting_clears_a_stale_bounce_on_the_address(self):
        """The invite is the FIRST mail this address ever got from us, so a
        bounce recorded against it would pause the very cadence they just
        agreed to."""
        from oikonome.notify import delivery
        delivery.record_failure(self.email, state="bouncing",
                                bounce_type="HardBounce", reason="typo")
        self.assertIsNotNone(delivery.state_for(self.email))
        self.client.post("/recipient-invite",
                         data={"token": self.token, "answer": "accept"})
        self.assertIsNone(delivery.state_for(self.email))


class InviteMailTests(unittest.TestCase):
    """The message itself — the only mail Oikonome sends to somebody who has
    no account and may want nothing to do with one."""

    def test_it_says_who_is_asking_and_what_arrives(self):
        from oikonome.web.app import _recipient_invite_body
        plain, html = _recipient_invite_body(
            "https://oik.example/recipient-invite?token=abc", "sam@x.dev")
        for body in (plain, html):
            self.assertIn("sam@x.dev", body)
            self.assertIn("recipient-invite?token=abc", body)
            self.assertIn("each morning", body)
        self.assertIn("14 days", plain)

    def test_it_works_without_a_named_inviter(self):
        from oikonome.web.app import _recipient_invite_body
        plain, html = _recipient_invite_body("https://oik.example/x", None)
        self.assertNotIn("None", plain)
        self.assertNotIn("None", html)

    def test_it_promises_nothing_else_arrives_until_they_answer(self):
        """The promise the gate actually keeps — worth pinning, because a
        later edit that drops the line would be claiming less than the code
        does, and one that drops the GATE would make it a lie."""
        from oikonome.web.app import _recipient_invite_body
        plain, _ = _recipient_invite_body("https://oik.example/x", None)
        self.assertIn("nothing else is sent", plain.lower())


class BackfillTests(unittest.TestCase):
    """Migration 074's backfill: upgrading must not pause mail that works.

    A migration that silently stops a working daily email is the exact
    failure the consent gate exists to prevent; adding that gate is no
    licence to commit it on the way past.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_existing_recipients_are_grandfathered_in(self):
        import pathlib
        conn, tid = _tenant()
        self.addCleanup(conn.close)
        legacy = _addr("legacy")
        write_config(conn, email_recipients=[legacy])
        sql = (pathlib.Path(__file__).resolve().parents[1]
               / "oikonome" / "db" / "migrations"
               / "074_recipient_invites.sql").read_text()
        # The migration's OWN statement, narrowed to this tenant. Running it
        # unscoped would backfill every tenant in the shared test database
        # and could silently grant acceptance to a fixture some other test
        # expects to find un-invited. The added predicate is the only edit —
        # if the WHERE clause it keys on ever changes, this fails loudly
        # instead of quietly testing nothing.
        marker = "WHERE trim(r) <> ''"
        self.assertIn(marker, sql, "migration 074's backfill changed shape")
        sql = sql.replace(marker, marker + f" AND s.tenant_id = '{tid}'::uuid")
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(sql)              # idempotent: IF NOT EXISTS + DO NOTHING
        finally:
            admin.close()
        self.assertEqual(recipient_invites.accepted(tid, [legacy]),
                         {legacy.lower()},
                         "an address that was already receiving the daily "
                         "email keeps receiving it")
        row = _row(tid, legacy)
        self.assertIsNone(row["token_hash"],
                          "a grandfathered row must stay distinguishable "
                          "from a link somebody actually opened")




class RecipientFallbackAndCapTests(unittest.TestCase):
    """The membership filter must never widen to the whole household, and
    every recipient list is capped and de-duplicated."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.conn, self.tid = _tenant()
        self.addCleanup(self.conn.close)
        self.owner = f"owner-{uuid.uuid4().hex[:8]}@example.dev"
        self.member = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            for email, role in ((self.owner, "owner"), (self.member, "viewer")):
                admin.execute(
                    "INSERT INTO users (tenant_id, email, password_hash, role,"
                    " verified_at) VALUES (%s,%s,'x',%s,now())",
                    (self.tid, email, role))
        finally:
            admin.close()

    def _recips(self):
        from oikonome.jobs.worker import _recipients
        return [e.lower() for e in _recipients(self.conn, self.tid)]

    # ---- a pending invite must narrow the list, never widen it

    def test_a_pending_invite_does_not_mail_the_whole_household(self):
        """Filtering the configured list down to nothing must not fall
        through to "every verified user": adding ONE external guest would
        then start mailing the household's balances to a member who was
        never on the list and never invited."""
        guest = _addr("guest")
        write_config(self.conn, email_recipients=[guest])
        recipient_invites.invite(self.tid, guest)          # sent, unanswered
        got = self._recips()
        self.assertIn(self.owner, got, "the owner still gets their own mail")
        self.assertNotIn(guest, got, "an unanswered invite receives nothing")
        self.assertNotIn(
            self.member, got,
            "a household member who was never configured and never invited "
            "must NOT start receiving the daily balances because somebody "
            "else's invitation is pending")

    def test_a_declined_recipient_does_not_widen_the_list_either(self):
        guest = _addr("guest")
        write_config(self.conn, email_recipients=[guest])
        recipient_invites.decline(recipient_invites.invite(self.tid, guest))
        got = self._recips()
        self.assertEqual(got, [self.owner],
                         "configured-then-filtered-empty means the owner, "
                         "never everyone")

    def test_no_configured_list_still_means_every_member(self):
        """The fallback that SHOULD still fire — nobody configured anything,
        so the household's members are the list."""
        write_config(self.conn)
        got = self._recips()
        self.assertIn(self.owner, got)
        self.assertIn(self.member, got)

    # ---- the recipient list is capped and de-duplicated

    def test_the_recipient_list_is_capped(self):
        """Uncapped, one POST spawns a thread and an unpooled admin
        connection per address, with no bound on the array."""
        from oikonome.web import mailguard
        got = mailguard.parse_recipients(
            [f"r{i}@example.dev" for i in range(500)])
        self.assertEqual(len(got), mailguard.MAX_RECIPIENTS)

    def test_one_address_repeated_is_one_recipient(self):
        """A 100k-entry list of ONE legitimate address passes the
        membership check, so a cap alone is not enough."""
        from oikonome.web import mailguard
        self.assertEqual(
            mailguard.parse_recipients(["a@x.dev", "A@X.dev", " a@x.dev "]),
            ["a@x.dev"])
        self.assertEqual(
            mailguard.added_recipients([], ["b@x.dev"] * 5000), ["b@x.dev"])

    def test_added_recipients_is_capped_too(self):
        from oikonome.web import mailguard
        got = mailguard.added_recipients(
            [], [f"n{i}@example.dev" for i in range(500)])
        self.assertEqual(len(got), mailguard.MAX_RECIPIENTS)

    def test_a_restore_cannot_plant_an_unbounded_list(self):
        from oikonome.web import mailguard
        kept = mailguard.filter_recipients(
            self.conn, [f"z{i}@example.dev" for i in range(9000)])
        self.assertLessEqual(len(kept), mailguard.MAX_RECIPIENTS)

    # ---- a spent link must not tell a declined recipient the wrong thing

    def test_reopening_a_declined_link_says_you_already_said_no(self):
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        appmod.DEV_MODE = True
        client = TestClient(appmod.app)
        email = _addr()
        token = recipient_invites.invite(self.tid, email)
        client.post("/recipient-invite",
                    data={"token": token, "answer": "decline_confirmed"})
        r = client.get(f"/recipient-invite?token={token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("already said no", r.text)
        self.assertNotIn("that sends a fresh link", r.text,
                         "invite() refuses to re-ask a declined address, so "
                         "promising a fresh link strands both parties")

    def test_reopening_an_accepted_link_says_you_already_said_yes(self):
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        appmod.DEV_MODE = True
        client = TestClient(appmod.app)
        email = _addr()
        token = recipient_invites.invite(self.tid, email)
        client.post("/recipient-invite",
                    data={"token": token, "answer": "accept"})
        r = client.get(f"/recipient-invite?token={token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("already said yes", r.text)

    def test_a_spent_link_still_cannot_change_the_answer(self):
        """Keeping the hash must not make the token usable again."""
        email = _addr()
        token = recipient_invites.invite(self.tid, email)
        recipient_invites.decline(token)
        self.assertIsNone(recipient_invites.accept(token))
        self.assertIsNone(recipient_invites.lookup(token))
        self.assertEqual(
            recipient_invites.states(self.tid, [email])[email]["state"],
            "declined")

    def test_a_never_real_token_is_still_a_400(self):
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        appmod.DEV_MODE = True
        client = TestClient(appmod.app)
        r = client.get("/recipient-invite?token=nonsense")
        self.assertEqual(r.status_code, 400)
        self.assertIn("invalid or expired", r.text)


class BouncedRecipientCanBeReinvitedTests(unittest.TestCase):
    """Accepting an invite clears a recorded bounce, so re-inviting IS the
    recovery for a failing recipient. A Settings row that hides its Resend
    button on invite == "accepted" — the state a bounced-but-accepted
    recipient is in — takes the button away from the one address that needs
    it. Pinned in the SPA source, because the condition is where it lives."""

    def test_resend_is_offered_to_a_failing_recipient(self):
        import pathlib
        import oikonome
        spa = (pathlib.Path(oikonome.__file__).parent.parent.parent
               / "webapp" / "src" / "pages" / "Settings.tsx")
        if not spa.exists():
            self.skipTest("webapp/ not present")
        src = spa.read_text()
        i = src.find("s.invite !== \"accepted\"")
        self.assertGreater(i, 0, "the Resend guard moved — update this test")
        window = src[max(0, i - 400):i]
        self.assertIn("s.delivery", window,
                      "the Resend button must also render when delivery is "
                      "failing, or a bounced accepted recipient has no way "
                      "back")


class InviteFanOutTests(unittest.TestCase):
    """Adding several recipients at once must not take a pooled connection
    per address.

    `invite_added` used to spawn one daemon thread per invited address, up
    to MAX_RECIPIENTS. Each opened a tenant connection out of the pool and
    then sat in an SMTP conversation, so a single settings save could hold
    twenty of them at once and starve every other request. They are all one
    tenant's invites, so one thread walks them and the SMTP settings are
    resolved once."""

    def test_a_batch_uses_one_thread_and_one_smtp_resolution(self):
        import threading
        from oikonome.web import mailguard

        emails = [f"r{i}@example.dev" for i in range(6)]
        threads: list[str] = []
        real_thread = threading.Thread

        class CountingThread(real_thread):          # type: ignore[misc]
            def start(self):
                threads.append(getattr(self, "name", "?"))
                return super().start()

        delivered: list[str] = []
        resolves: list[object] = []

        def fake_deliver(email, token, tenant_id, inviter=None,
                         smtp=None, resolved=False):
            delivered.append(email)
            # the batch resolves for the tenant and passes the answer down
            self_assert = resolved
            if not self_assert:
                raise AssertionError("each invite re-resolved SMTP")

        def fake_resolve(tenant_id):
            resolves.append(tenant_id)
            return None

        with mock.patch("threading.Thread", CountingThread), \
             mock.patch("oikonome.web.app._deliver_recipient_invite",
                        fake_deliver), \
             mock.patch("oikonome.web.app._tenant_smtp", fake_resolve), \
             mock.patch("oikonome.notify.recipient_invites.invite",
                        side_effect=[f"tok{i}" for i in range(6)]):
            mailguard.invite_added(
                {"tenant_id": "t-1", "user_id": "u-1",
                 "email": "owner@example.dev"}, emails)

        # the thread is daemonised and short; give it a moment to finish
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=5)

        self.assertEqual(len(threads), 1,
                         f"one thread for the batch, got {len(threads)}")
        self.assertEqual(len(resolves), 1,
                         "SMTP resolved once per batch, not once per address")
        self.assertEqual(sorted(delivered), sorted(emails))

    def test_nothing_is_started_when_every_address_was_already_answered(self):
        """invite() returns None for an address that already accepted or
        declined — a batch of only those must not start a thread at all."""
        import threading
        from oikonome.web import mailguard

        started: list[int] = []
        real_thread = threading.Thread

        class CountingThread(real_thread):          # type: ignore[misc]
            def start(self):
                started.append(1)
                return super().start()

        with mock.patch("threading.Thread", CountingThread), \
             mock.patch("oikonome.notify.recipient_invites.invite",
                        return_value=None):
            mailguard.invite_added(
                {"tenant_id": "t-2", "user_id": "u-2",
                 "email": "owner@example.dev"},
                ["a@example.dev", "b@example.dev"])
        self.assertEqual(started, [])


class InviteLinkRefusalTests(unittest.TestCase):
    """Refusing to build a Host-derived link must not silence the invitee.

    An install with SMTP configured but no OIKONOME_BASE_URL cannot build a
    safe link, so the invite is logged instead of mailed — correct. But it
    was logged SILENTLY: Settings kept showing "invited", the owner was told
    the invitation went out, and the only copy of the link sat in a server
    log nobody reads on a mail-configured box. The person could never accept
    and never learn why."""

    def test_a_link_that_cannot_be_built_is_recorded_as_undelivered(self):
        from oikonome.web import app as webapp

        recorded = {}

        def fake_record(email, **kw):
            recorded["email"] = email
            recorded.update(kw)

        old_base = os.environ.pop("OIKONOME_BASE_URL", None)
        old_dev = webapp.DEV_MODE
        try:
            webapp.DEV_MODE = False
            with mock.patch("oikonome.notify.delivery.record_failure",
                            fake_record), \
                 mock.patch("oikonome.web.app._log_link"), \
                 mock.patch("oikonome.web.spamdefense.recipient_blocked",
                            return_value=False):
                webapp._deliver_recipient_invite(
                    "nobody@example.dev", "tok", "t-1",
                    smtp={"configured": True}, resolved=True)
        finally:
            webapp.DEV_MODE = old_dev
            if old_base is not None:
                os.environ["OIKONOME_BASE_URL"] = old_base

        self.assertEqual(recorded.get("email"), "nobody@example.dev")
        self.assertEqual(recorded.get("state"), "bouncing")
        self.assertIn("OIKONOME_BASE_URL", recorded.get("reason", ""))

    def test_a_buildable_link_records_nothing(self):
        from oikonome.web import app as webapp

        recorded = []
        old_base = os.environ.get("OIKONOME_BASE_URL")
        old_dev = webapp.DEV_MODE
        os.environ["OIKONOME_BASE_URL"] = "https://example.dev"
        try:
            webapp.DEV_MODE = False
            with mock.patch("oikonome.notify.delivery.record_failure",
                            lambda *a, **k: recorded.append(1)), \
                 mock.patch("oikonome.web.report.send"), \
                 mock.patch("oikonome.web.spamdefense.recipient_blocked",
                            return_value=False):
                webapp._deliver_recipient_invite(
                    "nobody@example.dev", "tok", "t-1",
                    smtp={"configured": True}, resolved=True)
        finally:
            webapp.DEV_MODE = old_dev
            if old_base is None:
                os.environ.pop("OIKONOME_BASE_URL", None)
            else:
                os.environ["OIKONOME_BASE_URL"] = old_base
        self.assertEqual(recorded, [])


class HourlyCapRaceTests(unittest.TestCase):
    """The per-tenant hourly ceiling holds under concurrent sends.

    The ceiling is a tenant-wide count read before the mint, but the only
    row lock taken is on the one (tenant, email) row — and a FIRST invite
    has no row to lock. Concurrent invites to different fresh addresses
    must serialize across the count-to-insert window, or each reads the
    same pre-insert count, all pass, and the tenant mails past its cap.
    The window is pinned open with a barrier right after the count read;
    serialized, only one sender can be inside it, so the barrier times out.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_concurrent_first_invites_cannot_pass_the_cap_together(self):
        import contextlib
        import threading

        conn, tid = _tenant()
        self.addCleanup(conn.close)

        # one send already on the clock, cap of two: exactly one slot left
        seeded = recipient_invites.invite(tid, _addr("seed"))
        self.assertTrue(seeded)

        barrier = threading.Barrier(2)
        real_conn = recipient_invites._conn

        class _PauseAfterCount:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **kw):
                cur = self._inner.execute(sql, *a, **kw)
                if "last_sent_at > %s" in sql:
                    with contextlib.suppress(threading.BrokenBarrierError):
                        barrier.wait(timeout=3.0)
                return cur

            def __getattr__(self, name):
                return getattr(self._inner, name)

        results = []

        def send(addr):
            results.append(recipient_invites.invite(tid, addr))

        with mock.patch.object(recipient_invites, "_HOURLY_TENANT_CAP", 2), \
             mock.patch.object(recipient_invites, "_conn",
                               lambda: _PauseAfterCount(real_conn())):
            threads = [threading.Thread(target=send, args=(_addr(f"r{i}"),))
                       for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        minted = [r for r in results if r]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(minted), 1,
                         "two concurrent sends both passed the last slot")
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            sent = admin.execute(
                "SELECT count(*) AS n FROM recipient_invites "
                "WHERE tenant_id=%s AND last_sent_at > "
                "now() - interval '1 hour'", (tid,)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(sent, 2, "the hourly ceiling was overshot")

if __name__ == "__main__":
    unittest.main()
