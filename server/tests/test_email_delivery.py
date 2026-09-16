"""The app notices when its mail stops arriving.

The failure mode: an account is created with a mistyped domain
(`gmaill.com`, two l's). The first daily verdict HARD BOUNCES. The relay
then marks the address inactive, so the next sends are refused at
submission with a 406 and never leave the building. Several sends, zero
delivered, and not one signal reaches the person whose money it is about —
no banner, no settings row, nothing. The failure gets QUIETER over time,
because a suppressed address stops producing bounces.

So the tests are about the whole loop, in the order it has to hold:

  * the webhook hears the bounce and only acts on verdicts that mean the
    address is genuinely unreachable (a soft bounce must never pause mail);
  * the send path hears the 406 that comes AFTER suppression, which is the
    only remaining signal once the bounces stop;
  * scheduled mail is held while an address is broken, and transactional
    mail is NOT — the confirmation link is what a person needs while
    fixing it;
  * a successful send, or a fresh verification, clears the state;
  * the typo the whole thing starts with is caught at the door, as a
    suggestion that never blocks.
"""

import datetime as dt
import os
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.notify import delivery, emailhint

from .util import make_db


def ensure_schema():
    make_db().close()


def wipe(email: str):
    admin = tenancy.admin_connect()
    try:
        admin.execute("DELETE FROM email_delivery_state WHERE email=%s",
                      (email.lower(),))
    finally:
        admin.close()


def addr(prefix="bounce"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@gmaill.com"


class StateTests(unittest.TestCase):
    """The store itself: one row per address, present only while broken."""

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def test_unknown_address_has_no_state(self):
        self.assertIsNone(delivery.state_for(addr()))

    def test_record_then_clear(self):
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_failure(e, bounce_type="HardBounce",
                                reason="unknown user")
        row = delivery.state_for(e)
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "bouncing")
        self.assertEqual(row["bounce_type"], "HardBounce")
        self.assertEqual(row["reason"], "unknown user")
        # reactivate=False: no provider round-trip in a unit test
        delivery.clear(e, reactivate=False)
        self.assertIsNone(delivery.state_for(e))

    def test_repeat_failures_bump_the_count_not_the_row_count(self):
        e = addr()
        self.addCleanup(wipe, e)
        for _ in range(3):
            delivery.record_failure(e, bounce_type="HardBounce")
        row = delivery.state_for(e)
        self.assertEqual(row["fail_count"], 3)

    def test_later_report_fills_in_what_the_first_lacked(self):
        """The send path sees a 406 with no bounce id; the webhook that
        arrives later carries one. The id must survive — it is the only way
        to reactivate the address upstream when it is fixed."""
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_failure(e, bounce_type="HardBounce")
        delivery.record_failure(e, provider_id="12345", suppressed=True)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT provider_id, suppressed, bounce_type FROM "
                "email_delivery_state WHERE email=%s", (e,)).fetchone()
        finally:
            admin.close()
        self.assertEqual(row["provider_id"], "12345")
        self.assertTrue(row["suppressed"])
        # and the earlier detail is not lost to the later, vaguer report
        self.assertEqual(row["bounce_type"], "HardBounce")

    def test_case_is_not_a_different_address(self):
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_failure(e.upper())
        self.assertIsNotNone(delivery.state_for(e.lower()))


class GrantTests(unittest.TestCase):
    """The app role may READ this table and may not WRITE it.

    The asymmetry is the whole security design. A row here PAUSES an
    address's scheduled mail, so an injected app-role INSERT would be a way
    to silence another household's verdict email — while reading one exposes
    nothing that `users` does not already. Reads have to be cheap because
    /api/me is on every page load; writes have to be out of reach.
    """

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def test_app_role_can_read(self):
        conn = tenancy.control_connect()
        try:
            conn.execute("SELECT count(*) FROM email_delivery_state")
        finally:
            conn.close()

    def _refused(self, sql, *params):
        import psycopg
        conn = tenancy.control_connect()
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, params or None)
        finally:
            conn.close()

    def test_app_role_cannot_forge_a_bounce(self):
        self._refused("INSERT INTO email_delivery_state (email, state) "
                      "VALUES (%s,'bouncing')", "victim@example.com")

    def test_app_role_cannot_scrub_a_bounce(self):
        self._refused("DELETE FROM email_delivery_state")

    def test_app_role_cannot_rewrite_a_bounce(self):
        self._refused("UPDATE email_delivery_state SET state='bouncing'")


class SendFailureClassificationTests(unittest.TestCase):
    """What the SEND path is allowed to conclude from an exception.

    This is the half that matters most after suppression kicks in, and it is
    also the half most able to do damage: pausing someone's mail because a
    relay timed out once would be the same silence, self-inflicted.
    """

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def test_postmark_406_inactive_is_recorded_as_suppression(self):
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_send_failure(e, (
            "SMTPDataError: (406, \"You tried to send to recipient(s) that "
            "have been marked as inactive. Found inactive addresses: "
            f"{e}.\")"))
        row = delivery.state_for(e)
        self.assertIsNotNone(row)
        self.assertTrue(row["suppressed"])

    def test_permanent_5xx_is_recorded(self):
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_send_failure(
            e, "SMTPRecipientsRefused: 550 5.1.1 unknown user")
        self.assertIsNotNone(delivery.state_for(e))

    def test_a_greylist_450_is_not_a_hard_bounce(self):
        """Postfix's standard deferral — '450 4.7.1 <addr>: Recipient
        address rejected: ...' — carries a permanent-set PHRASE, so one
        greylist event would write a HardBounce row the hold then makes
        permanent (clear() only runs on a successful send, which the hold
        prevents). 4xx evidence vetoes the phrase branch."""
        e = addr()
        self.addCleanup(wipe, e)
        for reply in (
                "450 4.7.1 <%s>: Recipient address rejected: "
                "Greylisted, see http://postgrey.schweikert.ch" % e,
                "(450, b'4.1.1 <%s>: Recipient address rejected: "
                "unverified address')" % e):
            delivery.record_send_failure(e, reply)
            self.assertIsNone(
                delivery.state_for(e),
                f"a 4xx deferral was recorded as permanent: {reply!r}")
        # a genuine 5xx with the same phrase still records
        delivery.record_send_failure(
            e, "550 5.1.1 <%s>: Recipient address rejected: "
               "User unknown" % e)
        self.assertIsNotNone(delivery.state_for(e))

    def test_a_timeout_says_nothing_about_the_address(self):
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_send_failure(e, "TimeoutError: timed out")
        self.assertIsNone(delivery.state_for(e))

    def test_a_stray_three_digit_number_is_not_a_bounce(self):
        """The classifier must match reply CODES, not any number that looks
        like one. A false positive pauses a working address — the exact
        silence this feature exists to end — so it is the expensive
        direction to be wrong in."""
        for benign in ("TimeoutError: read timed out after 554 ms",
                       "OSError: wrote 550 of 1200 bytes",
                       "SMTPServerDisconnected: connection lost (port 587)"):
            e = addr()
            self.addCleanup(wipe, e)
            delivery.record_send_failure(e, benign)
            self.assertIsNone(delivery.state_for(e), benign)

    def test_smtplibs_own_refusal_shape_is_caught(self):
        """What `SMTPRecipientsRefused` actually stringifies to."""
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_send_failure(e, (
            "SMTPRecipientsRefused: {'x@y.com': (550, b'5.1.1 The email "
            "account that you tried to reach does not exist')}"))
        self.assertIsNotNone(delivery.state_for(e))

    def test_an_unsubscribe_is_not_reported_as_a_bounce(self):
        """We send no broadcast mail, so this cannot arrive — and if it ever
        did, telling someone who chose to unsubscribe that their email "isn't
        arriving" would be wrong and would nudge them to undo it."""
        self.assertNotIn("Unsubscribe", delivery.HARD_TYPES)
        self.assertNotIn("SubscriptionChange", delivery.HARD_TYPES)

    def test_a_connection_refused_says_nothing_about_the_address(self):
        """A dead relay breaks EVERY address at once. Recording that as a
        per-address bounce would pause the whole household's mail over an
        outage that has nothing to do with them."""
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_send_failure(
            e, "ConnectionRefusedError: [Errno 111] Connection refused")
        self.assertIsNone(delivery.state_for(e))


class HoldTests(unittest.TestCase):
    """Who still gets mail while an address is broken."""

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def test_broken_addresses_are_held_and_the_rest_still_send(self):
        bad, good = addr(), addr("fine")
        self.addCleanup(wipe, bad)
        self.addCleanup(wipe, good)
        delivery.record_failure(bad, bounce_type="HardBounce")
        send, held = delivery.hold([good, bad])
        self.assertEqual(send, [good])
        self.assertEqual(held, [bad])

    def test_nothing_known_broken_holds_nothing(self):
        a, b = addr("a"), addr("b")
        send, held = delivery.hold([a, b])
        self.assertEqual(send, [a, b])
        self.assertEqual(held, [])

    def test_hold_is_case_insensitive(self):
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_failure(e)
        send, held = delivery.hold([e.upper()])
        self.assertEqual(send, [])
        self.assertEqual(held, [e.upper()])


class CadenceHoldTests(unittest.TestCase):
    """Holding a dead address must not turn the daily job into a spinner.

    `emails_due` re-offers a cadence until its heartbeat says it ran today.
    Keying that stamp off the POST-hold recipient list would mean a household
    whose only address bounces never stamps — so the job comes due on every
    hourly sweep, decides not to send, and logs about it, 24 times a day
    forever. Held is a decision that was made, not work still pending.
    """

    def setUp(self):
        from .util import make_db, write_config
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.bad = addr()
        self.addCleanup(wipe, self.bad)
        self.addCleanup(self.conn.close)
        write_config(self.conn, email_recipients=[self.bad])
        # a configured address is only mailed once it has accepted
        # its invitation — these tests are about the BOUNCE hold, so put the
        # consent in place and let the hold be the only thing under test
        from .util import accept_recipient_invite
        accept_recipient_invite(self.tid, self.bad)

    def _run(self):
        from unittest import mock
        from oikonome.jobs import worker
        from oikonome.web import report
        with mock.patch.object(report, "send_each", return_value=[]) as m:
            worker.email_tenant(self.tid, send_it=True)
        return m

    def _heartbeat(self):
        return self.conn.execute(
            "SELECT ran_at FROM job_runs WHERE job='daily-email'").fetchone()

    def test_a_bouncing_recipient_is_not_mailed(self):
        delivery.record_failure(self.bad, bounce_type="HardBounce")
        self.assertFalse(self._run().called,
                         "a hard-bounced address must not be mailed again — "
                         "every send is refused and the sender's reputation "
                         "pays for it")

    def test_the_cadence_still_stamps_so_it_does_not_spin(self):
        """`emails_due` re-offers a cadence until the heartbeat says it ran.
        Keying that stamp off the POST-hold list would leave a household
        whose only address bounces coming due on every hourly sweep,
        deciding not to send, and logging about it — 24 times a day."""
        delivery.record_failure(self.bad, bounce_type="HardBounce")
        self._run()
        self.assertIsNotNone(self._heartbeat())

    def test_a_working_recipient_is_still_mailed(self):
        """The hold must be the exception, not the behaviour."""
        self.assertTrue(self._run().called)


class WebhookTests(unittest.TestCase):
    """The provider's report, and what we refuse to conclude from it."""

    @classmethod
    def setUpClass(cls):
        ensure_schema()
        from oikonome.web import postmark_webhook
        cls.wh = postmark_webhook

    def test_hard_bounce_is_recorded_with_the_providers_own_words(self):
        e = addr()
        self.addCleanup(wipe, e)
        self.wh._handle({"RecordType": "Bounce", "Type": "HardBounce",
                         "Email": e, "ID": 987,
                         "Description": "The server was unable to deliver "
                                        "your message (ex: unknown user)",
                         "Inactive": True})
        row = delivery.state_for(e)
        self.assertEqual(row["state"], "bouncing")
        self.assertIn("unknown user", row["reason"])
        self.assertTrue(row["suppressed"])

    def test_a_non_numeric_bounce_id_is_not_stored(self):
        """The stored id is later interpolated into the reactivation URL
        path with our server token attached — a crafted webhook ID must
        never become part of that request. Postmark ids are numeric."""
        e = addr()
        self.addCleanup(wipe, e)
        self.wh._handle({"RecordType": "Bounce", "Type": "HardBounce",
                         "Email": e, "ID": "987/activate?x=1",
                         "Inactive": True})
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT provider_id FROM email_delivery_state "
                "WHERE email=%s", (e,)).fetchone()
        finally:
            admin.close()
        self.assertIsNone(row["provider_id"])

    def test_spam_complaint_is_its_own_state(self):
        e = addr()
        self.addCleanup(wipe, e)
        self.wh._handle({"RecordType": "SpamComplaint",
                         "Type": "SpamNotification", "Email": e})
        self.assertEqual(delivery.state_for(e)["state"], "complained")

    def test_a_soft_bounce_never_pauses_anyones_mail(self):
        """"Mailbox full" is temporary and says nothing about whether the
        address is real. Acting on it would turn a full inbox into a
        permanently silenced account."""
        e = addr()
        self.addCleanup(wipe, e)
        self.wh._handle({"RecordType": "Bounce", "Type": "SoftBounce",
                         "Email": e, "Description": "mailbox full"})
        self.assertIsNone(delivery.state_for(e))

    def test_delivery_and_open_events_are_ignored(self):
        e = addr()
        self.addCleanup(wipe, e)
        for rt in ("Delivery", "Open", "Click"):
            out = self.wh._handle({"RecordType": rt, "Email": e})
            self.assertEqual(out.get("ignored"), rt)
        self.assertIsNone(delivery.state_for(e))

    def test_a_payload_with_no_recipient_is_a_no_op(self):
        self.assertEqual(self.wh._handle({"RecordType": "Bounce",
                                          "Type": "HardBounce"}).get("ignored"),
                         "no recipient")

    def test_a_non_string_field_is_ignored_not_raised(self):
        """A field that is a number/list/object is not an event we can act
        on, and it will not become one on a retry. Reading it must be
        total: an exception here is a 500, which the provider takes as an
        outage and retries forever for a payload that is permanently
        wrong."""
        for bad in ({"RecordType": "Bounce", "Type": "HardBounce",
                     "Email": 12345},
                    {"RecordType": "Bounce", "Type": "HardBounce",
                     "Recipient": ["a@example.com"]},
                    {"RecordType": "Bounce", "Type": {"x": 1},
                     "Email": "x@example.com"},
                    {"RecordType": 7, "Type": "HardBounce",
                     "Email": "x@example.com"},
                    {"RecordType": "Bounce", "Type": "HardBounce",
                     "Email": "x@example.com", "Description": 42}):
            with self.subTest(payload=bad):
                out = self.wh._handle(bad)
                self.assertTrue(out["ok"])
                self.assertEqual(out.get("ignored"), "malformed field")
        self.assertIsNone(delivery.state_for("x@example.com"))


class WebhookDoorTests(unittest.TestCase):
    """The endpoint is a shared secret or it is not there at all."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        ensure_schema()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        from fastapi.testclient import TestClient
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)

    def tearDown(self):
        os.environ.pop("OIKONOME_EMAIL_WEBHOOK_TOKEN", None)

    def _post(self, body, **kw):
        return self.client.post("/api/email/webhook", json=body, **kw)

    def test_unconfigured_instance_exposes_nothing(self):
        """A self-host install with a local relay has no webhook to receive.
        404, not 401 — an unauthenticated door that ADMITS it exists is a
        door worth attacking."""
        r = self._post({"RecordType": "Bounce", "Email": "x@example.com"})
        self.assertEqual(r.status_code, 404)

    def test_wrong_secret_is_refused(self):
        os.environ["OIKONOME_EMAIL_WEBHOOK_TOKEN"] = "s3cret"
        r = self._post({"RecordType": "Bounce", "Email": "x@example.com"},
                       params={"token": "not-it"})
        self.assertEqual(r.status_code, 401)

    def test_no_secret_at_all_is_refused(self):
        os.environ["OIKONOME_EMAIL_WEBHOOK_TOKEN"] = "s3cret"
        self.assertEqual(
            self._post({"RecordType": "Bounce",
                        "Email": "x@example.com"}).status_code, 401)

    def test_query_token_is_accepted(self):
        os.environ["OIKONOME_EMAIL_WEBHOOK_TOKEN"] = "s3cret"
        e = addr()
        self.addCleanup(wipe, e)
        r = self._post({"RecordType": "Bounce", "Type": "HardBounce",
                        "Email": e}, params={"token": "s3cret"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(delivery.state_for(e))

    def test_basic_auth_is_accepted(self):
        """Postmark's documented way to authenticate a receiver is HTTP Basic
        embedded in the webhook URL, and operators write the secret into
        either half of it."""
        os.environ["OIKONOME_EMAIL_WEBHOOK_TOKEN"] = "s3cret"
        e = addr()
        self.addCleanup(wipe, e)
        r = self.client.post("/api/email/webhook",
                             json={"RecordType": "Bounce",
                                   "Type": "HardBounce", "Email": e},
                             auth=("postmark", "s3cret"))
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(delivery.state_for(e))

    def test_a_garbage_typed_field_answers_200_not_500(self):
        """The authenticated door must not hand the provider a 500 (and a
        traceback into the feedback buffer) for a payload it can simply
        decline."""
        os.environ["OIKONOME_EMAIL_WEBHOOK_TOKEN"] = "s3cret"
        r = self._post({"RecordType": "Bounce", "Email": 12345,
                        "Type": "HardBounce"}, params={"token": "s3cret"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("ignored"), "malformed field")

    def test_an_oversized_body_is_refused(self):
        os.environ["OIKONOME_EMAIL_WEBHOOK_TOKEN"] = "s3cret"
        r = self.client.post("/api/email/webhook", params={"token": "s3cret"},
                             content=b"x" * (64 * 1024 + 10),
                             headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)


class TypoHintTests(unittest.TestCase):
    """A likely domain typo is caught before it is saved."""

    def test_the_doubled_letter_domain_typo(self):
        self.assertEqual(emailhint.suggest("someone@gmaill.com"),
                         "someone@gmail.com")

    def test_common_near_misses(self):
        for typo, want in (("a@gmial.com", "a@gmail.com"),
                           ("a@gmai.com", "a@gmail.com"),
                           ("a@yahooo.com", "a@yahoo.com"),
                           ("a@hotmial.com", "a@hotmail.com"),
                           ("a@gmail.con", "a@gmail.com"),
                           ("a@outlok.com", "a@outlook.com")):
            self.assertEqual(emailhint.suggest(typo), want, typo)

    def test_a_correct_address_is_never_second_guessed(self):
        for good in ("a@gmail.com", "a@proton.me", "a@icloud.com"):
            self.assertIsNone(emailhint.suggest(good), good)

    def test_a_real_unrelated_domain_is_left_alone(self):
        """The rule that keeps this honest: a company domain must never be
        told it looks like a typo of Gmail."""
        for good in ("owner@oikonome.com", "a@example.org", "a@stanford.edu",
                     "a@nhs.uk", "a@mail.ru", "a@qq.com"):
            self.assertIsNone(emailhint.suggest(good), good)

    def test_garbage_in_no_crash_out(self):
        for junk in ("", "no-at-sign", "two@at@signs.com", "@nolocal.com",
                     "trailing@"):
            self.assertIsNone(emailhint.suggest(junk), junk)


class SelfHostIsUnaffectedTests(unittest.TestCase):
    """None of this may become a thing a self-hosted install has to run."""

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def test_a_non_numeric_provider_id_is_never_sent_upstream(self):
        """Rows stored before the webhook validated the shape can still
        carry junk — the reactivation call must refuse to build a URL
        from it even with a token configured."""
        from unittest import mock
        os.environ["OIKONOME_POSTMARK_TOKEN"] = "tok-x"
        self.addCleanup(os.environ.pop, "OIKONOME_POSTMARK_TOKEN", None)
        with mock.patch("httpx.put",
                        side_effect=AssertionError("network")) as p:
            delivery._reactivate("someone@example.com", "987/activate?x=1")
        p.assert_not_called()

    def test_reactivation_is_a_no_op_without_a_provider_token(self):
        """No relay token in the environment ⇒ clear() still clears locally
        and makes no network call. (No token is set in the test env; if this
        ever tried to reach the network the suite would hang, which is the
        assertion.)"""
        e = addr()
        self.addCleanup(wipe, e)
        delivery.record_failure(e, provider_id="1", suppressed=True)
        for var in ("OIKONOME_POSTMARK_TOKEN", "OIKONOME_SMTP_HOST"):
            os.environ.pop(var, None)
        delivery.clear(e)
        self.assertIsNone(delivery.state_for(e))

    def test_the_provider_token_falls_back_to_the_smtp_credential(self):
        """Postmark uses the server token as the SMTP username, so the
        documented setup needs no second secret."""
        os.environ["OIKONOME_SMTP_HOST"] = "smtp.postmarkapp.com"
        os.environ["OIKONOME_SMTP_USER"] = "tok-123"
        self.addCleanup(os.environ.pop, "OIKONOME_SMTP_HOST", None)
        self.addCleanup(os.environ.pop, "OIKONOME_SMTP_USER", None)
        self.assertEqual(delivery._postmark_token(), "tok-123")

    def test_a_non_postmark_relay_yields_no_token(self):
        os.environ["OIKONOME_SMTP_HOST"] = "smtp.example.com"
        os.environ["OIKONOME_SMTP_USER"] = "someone"
        self.addCleanup(os.environ.pop, "OIKONOME_SMTP_HOST", None)
        self.addCleanup(os.environ.pop, "OIKONOME_SMTP_USER", None)
        self.assertIsNone(delivery._postmark_token())


class RecipientStatusTests(unittest.TestCase):
    """The Settings list: who receives the mail, and does it reach them.

    The control this replaces was a comma-separated text box, which could
    only ever tell you what you typed. Every assertion here is a fact it
    could not show.
    """

    @classmethod
    def setUpClass(cls):
        ensure_schema()
        from oikonome.web import api
        cls.api = api

    def _tenant(self, users):
        """users: [(email, role, verified)] — returns the tenant id."""
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"rs-{uuid.uuid4().hex[:8]}")
            for email, role, verified in users:
                admin.execute(
                    "INSERT INTO users (tenant_id, email, password_hash, "
                    "role, verified_at) VALUES (%s,%s,'x',%s,%s)",
                    (tid, email, role,
                     dt.datetime.now(dt.timezone.utc) if verified else None))
            return tid
        finally:
            admin.close()

    def test_the_owner_leads_the_list_and_cannot_be_removed(self):
        """The owner is mailed by construction. A list that omits them reads
        as the whole list, which is how filling the old field comes to be
        understood as REPLACING the owner."""
        o = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        tid = self._tenant([(o, "owner", True)])
        rows = self.api._recipient_status(tid, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], o)
        self.assertTrue(rows[0]["owner"])
        self.assertTrue(rows[0]["verified"])

    def test_an_unverified_member_is_reported_unverified(self):
        o = f"o-{uuid.uuid4().hex[:8]}@example.com"
        p = f"p-{uuid.uuid4().hex[:8]}@example.com"
        tid = self._tenant([(o, "owner", True), (p, "member", False)])
        rows = self.api._recipient_status(tid, [p])
        got = {r["email"]: r for r in rows}
        self.assertFalse(got[p]["verified"])
        self.assertTrue(got[p]["member"])

    def test_a_non_member_address_is_external_not_unverified(self):
        """Self-host allows free-form recipients. Calling a stranger's
        mailbox "unverified" claims a pending step that does not exist."""
        o = f"o-{uuid.uuid4().hex[:8]}@example.com"
        tid = self._tenant([(o, "owner", True)])
        rows = self.api._recipient_status(tid, ["outsider@example.net"])
        got = {r["email"]: r for r in rows}
        self.assertIsNone(got["outsider@example.net"]["verified"])
        self.assertFalse(got["outsider@example.net"]["member"])

    def test_a_bouncing_recipient_is_flagged_with_the_reason(self):
        o = f"o-{uuid.uuid4().hex[:8]}@example.com"
        bad = addr()
        self.addCleanup(wipe, bad)
        tid = self._tenant([(o, "owner", True)])
        delivery.record_failure(bad, bounce_type="HardBounce",
                                reason="unknown user")
        rows = self.api._recipient_status(tid, [bad])
        got = {r["email"]: r for r in rows}
        self.assertEqual(got[bad]["delivery"], "bouncing")
        self.assertIn("unknown user", got[bad]["delivery_reason"])

    def test_the_owner_is_not_listed_twice_when_also_configured(self):
        o = f"o-{uuid.uuid4().hex[:8]}@example.com"
        tid = self._tenant([(o, "owner", True)])
        rows = self.api._recipient_status(tid, [o.upper()])
        self.assertEqual(len(rows), 1)


class RecipientListParsingTests(unittest.TestCase):
    """The chip editor sends a LIST; every older caller sends a string."""

    def test_a_list_survives_intact(self):
        from oikonome.web import mailguard
        self.assertEqual(mailguard.parse_recipients(["a@x.com", " b@y.com "]),
                         ["a@x.com", "b@y.com"])

    def test_the_comma_string_still_works(self):
        from oikonome.web import mailguard
        self.assertEqual(mailguard.parse_recipients("a@x.com, b@y.com"),
                         ["a@x.com", "b@y.com"])

    def test_empties_are_dropped_from_both_shapes(self):
        from oikonome.web import mailguard
        self.assertEqual(mailguard.parse_recipients(["", "  "]), [])
        self.assertEqual(mailguard.parse_recipients(",,"), [])
        self.assertEqual(mailguard.parse_recipients(None), [])


class VerificationMailTests(unittest.TestCase):
    """The confirmation mail is one link and one sentence — the invite
    mail's shape. What the product sends and when is
    explained in the setup wizard, not in the mail."""

    def test_it_is_one_link_and_nothing_to_read(self):
        from oikonome.web.app import _verification_body
        plain, html = _verification_body("https://example.com/verify-email?t=x")
        for body in (plain, html):
            self.assertIn("https://example.com/verify-email?t=x", body)
            self.assertIn("Confirm your email", body)
            self.assertLess(len(body), 400)
            self.assertNotIn("What you'll get", body)
            self.assertNotIn("Settings", body)

    def test_it_is_the_link_and_nothing_else(self):
        """One line and the link — no "if this wasn't you" reassurance either,
        because the link is the whole task."""
        from oikonome.web.app import _verification_body
        for body in _verification_body("https://example.com/v"):
            self.assertNotIn("ignore this email", body)


class ErasureSweepTests(unittest.TestCase):
    """email_delivery_state has no tenant key, so the tenant_id-column
    discovery in delete_tenant_rows cannot see it — a CCPA erasure would
    leave the household's addresses and the provider's verbatim bounce
    records behind forever. The sweep deletes BY ADDRESS instead: every
    user of the tenant plus its configured extra recipients."""

    @classmethod
    def setUpClass(cls):
        ensure_schema()

    def test_tenant_erasure_takes_the_bounce_rows_with_it(self):

        from oikonome.engine.compat import jsonb
        owner, extra, bystander = addr("own"), addr("extra"), addr("other")
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"era-{uuid.uuid4().hex[:8]}")
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x')", (tid, owner))
            admin.execute(
                "INSERT INTO tenant_settings (tenant_id, config) "
                "VALUES (%s, %s) ON CONFLICT (tenant_id) DO UPDATE "
                "SET config = EXCLUDED.config",
                (tid, jsonb({"email_recipients": [extra]})))
            for e in (owner, extra, bystander):
                delivery.record_send_failure(
                    e, "550 5.1.1 recipient rejected: unknown user")
                self.assertIsNotNone(delivery.state_for(e))
            tenancy.delete_tenant_rows(admin, tid)
            self.assertIsNone(delivery.state_for(owner),
                              "erasure left the owner's bounce row behind")
            self.assertIsNone(delivery.state_for(extra),
                              "erasure left a recipient's bounce row behind")
            self.assertIsNotNone(
                delivery.state_for(bystander),
                "an unrelated household's suppression must survive")
        finally:
            wipe(bystander)
            admin.close()


if __name__ == "__main__":
    unittest.main()


class HealOnlyWhatWasBrokenTests(unittest.TestCase):
    """A successful send should not open a connection to delete nothing.

    `clear()` runs on an unpooled ADMIN connection because a row here pauses
    someone's mail. Called after EVERY successful send, a household with
    four recipients would pay four of those per cadence per day to delete
    rows that, on the normal path, do not exist.
    """

    def test_a_clean_send_clears_nothing(self):
        from unittest import mock
        from oikonome.web import report

        cleared = []
        with mock.patch("oikonome.web.report.send"), \
             mock.patch("oikonome.notify.delivery.failing",
                        return_value=set()), \
             mock.patch("oikonome.notify.delivery.clear",
                        side_effect=lambda e, **k: cleared.append(e)):
            out = report.send_each("s", "p", "h",
                                   ["a@example.dev", "b@example.dev"])
        self.assertTrue(all(r["ok"] for r in out))
        self.assertEqual(cleared, [], "nothing was failing, nothing to heal")

    def test_a_previously_failing_address_still_heals(self):
        from unittest import mock
        from oikonome.web import report

        cleared = []
        with mock.patch("oikonome.web.report.send"), \
             mock.patch("oikonome.notify.delivery.failing",
                        return_value={"b@example.dev"}), \
             mock.patch("oikonome.notify.delivery.clear",
                        side_effect=lambda e, **k: cleared.append(e)):
            report.send_each("s", "p", "h",
                             ["a@example.dev", "b@example.dev"])
        self.assertEqual(cleared, ["b@example.dev"])

    def test_a_refusal_is_still_recorded_for_any_address(self):
        from unittest import mock
        from oikonome.web import report

        recorded = []
        with mock.patch("oikonome.web.report.send",
                        side_effect=RuntimeError("550 nope")), \
             mock.patch("oikonome.notify.delivery.failing",
                        return_value=set()), \
             mock.patch("oikonome.notify.delivery.record_send_failure",
                        side_effect=lambda e, err: recorded.append(e)):
            out = report.send_each("s", "p", "h", ["a@example.dev"])
        self.assertFalse(out[0]["ok"])
        self.assertEqual(recorded, ["a@example.dev"])
