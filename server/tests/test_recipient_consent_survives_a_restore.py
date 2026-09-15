"""An acceptance is a fact about a mailbox, and only this instance's own
invite flow may create one.

Moving a household between instances must not silently unsubscribe the
people it shares its daily email with — the address list is ordinary config
and has always survived an export/restore, while the ANSWER each recipient
gave lives in a control-plane table that neither export door can discover.
Carry the list and not the answers and the invite gate runs backwards:
Settings still lists the partner or the accountant and the mail they agreed
to receive stops.

But the archive is untrusted input. Nobody signs it, nothing proves this
instance wrote it, and the person uploading it is the person whose proposal
the invite exists to check — so a restore that copies `accepted_at` out of a
CSV hands whoever can upload a ZIP a way to enrol any mailbox in a
household's finances. The answers that travel are therefore the ones this
instance can be safe about: a DECLINE always (forging one only stops mail),
and an ACCEPTANCE only for an address that already holds an account on this
tenant — a fact checked here, not read from the file. Everyone else has to
be asked again, and the restore says how many that is.

A decision recorded HERE is never overturned by a file. A merely pending
invite is not a decision: it is the row an ordinary settings save writes, and
discarding the archive's answers against it is what made the migration this
feature exists for lose exactly what it was built to keep.
"""

import datetime as dt
import io
import os
import unittest
import uuid
import zipfile
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import recipient_invites
from oikonome.sync import export, restore

from .util import TEST_DB, _admin_dsn, make_db, write_config


def _tid(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]


def _addr(who: str) -> str:
    return f"{who}-{uuid.uuid4().hex[:8]}@example.test"


def _admin():
    return tenancy.admin_connect(_admin_dsn(TEST_DB))


def _invite_rows(tenant_id) -> dict[str, dict]:
    admin = _admin()
    try:
        return {r["email"]: r for r in admin.execute(
            "SELECT * FROM recipient_invites WHERE tenant_id = %s",
            (tenant_id,)).fetchall()}
    finally:
        admin.close()


def _add_user(tenant_id, email: str, *, verified: bool = True) -> None:
    """Give this address an account on the tenant — the one fact the restore
    is allowed to check for itself."""
    admin = _admin()
    try:
        admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role, "
            "verified_at) VALUES (%s, %s, 'x', 'member', %s)",
            (tenant_id, email.lower(),
             dt.datetime.now(dt.timezone.utc) if verified else None))
    finally:
        admin.close()


def _rewrite_member(data: bytes, name: str, body: str) -> bytes:
    """The archive with one CSV replaced — an owner editing the export and
    zipping it back up, which is all the attack takes."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            out.writestr(item.filename,
                         body if item.filename == name else src.read(item))
    return buf.getvalue()


class RecipientConsentRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.src = make_db()
        self.dest = make_db()
        self.addCleanup(self.src.close)
        self.addCleanup(self.dest.close)
        self.accepted = _addr("accepted")
        self.declined = _addr("declined")
        self.pending = _addr("pending")

    def _seed_source(self) -> None:
        tid = _tid(self.src)
        write_config(self.src, email_recipients=[
            self.accepted, self.declined, self.pending])
        recipient_invites.accept(recipient_invites.invite(tid, self.accepted))
        recipient_invites.decline(recipient_invites.invite(tid, self.declined))
        recipient_invites.invite(tid, self.pending)

    def _round_trip(self) -> dict:
        self._seed_source()
        data = export.build_zip(self.src)
        return restore.restore_zip(self.dest, data)

    # ---- an acceptance the file cannot manufacture --------------------

    def test_an_acceptance_in_the_file_does_not_enrol_a_stranger(self):
        """The archive says this mailbox agreed. This instance never asked
        it, and the mailbox does not answer to anyone here — so the daily
        email does not start."""
        self._round_trip()
        dest_tid = _tid(self.dest)
        self.assertEqual(
            recipient_invites.accepted(dest_tid, [self.accepted]), set(),
            "the worker's gate must not read consent that only ever existed "
            "in an uploaded file")
        self.assertNotIn(
            self.accepted, _invite_rows(dest_tid),
            "an address nobody here has asked is 'not invited', which is "
            "what Settings must be able to say about it")

    def test_a_hand_edited_archive_cannot_add_a_recipient(self):
        """The concrete attack: the owner exports, types an extra line into
        recipient_invites.csv with a timestamp in it, re-imports."""
        self._seed_source()
        victim = _addr("victim")
        data = _rewrite_member(
            export.build_zip(self.src), "recipient_invites.csv",
            "email,invited_by_email,created_at,expires_at,accepted_at,"
            "declined_at,last_sent_at,send_count\r\n"
            f"{victim},,2026-01-01T00:00:00+00:00,2026-02-01T00:00:00+00:00,"
            "2026-01-02T00:00:00+00:00,,,1\r\n")
        restore.restore_zip(self.dest, data)
        dest_tid = _tid(self.dest)
        self.assertEqual(
            recipient_invites.accepted(dest_tid, [victim]), set())
        self.assertNotIn(victim, _invite_rows(dest_tid))

    # ---- the one exception: an address this household already knows ----

    def test_an_address_with_an_account_here_keeps_its_consent(self):
        """A household member's acceptance is the one the destination can
        vouch for without the file: the mail tells them nothing they cannot
        read by signing in, and they can mute themselves in Settings."""
        _add_user(_tid(self.dest), self.accepted)
        self._round_trip()
        self.assertEqual(
            recipient_invites.accepted(_tid(self.dest), [self.accepted]),
            {self.accepted},
            "moving the server did not un-consent somebody who holds an "
            "account on this very household")

    def test_a_member_acceptance_lands_on_an_invite_already_pending_here(self):
        """The ordinary shape of the migration this feature exists for: sign
        up on the destination, type your recipients (which mints PENDING
        rows), then restore. An unanswered row is not a decision, so it must
        not swallow the answer the archive carries."""
        dest_tid = _tid(self.dest)
        _add_user(dest_tid, self.accepted)
        write_config(self.dest, email_recipients=[self.accepted])
        recipient_invites.invite(dest_tid, self.accepted)
        self.assertEqual(
            recipient_invites.states(dest_tid, [self.accepted]
                                     )[self.accepted]["state"], "invited")
        self._round_trip()
        self.assertEqual(
            recipient_invites.accepted(dest_tid, [self.accepted]),
            {self.accepted},
            "they said yes years ago; a question this instance happened to "
            "ask again is not an answer that outranks it")

    def test_hosted_needs_the_mailbox_proven_here_before_it_carries(self):
        """On hosted, a user row is proof of mailbox control only once
        verification has been answered — the same test the owner has to pass
        in the worker's own recipient gate."""
        _add_user(_tid(self.dest), self.accepted, verified=False)
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            self._round_trip()
            self.assertEqual(
                recipient_invites.accepted(_tid(self.dest), [self.accepted]),
                set())

    # ---- declines ------------------------------------------------------

    def test_a_declined_recipient_stays_declined(self):
        self._round_trip()
        state = recipient_invites.states(
            _tid(self.dest), [self.declined])[self.declined]
        self.assertEqual(state["state"], "declined")
        self.assertIsNone(
            recipient_invites.invite(_tid(self.dest), self.declined),
            "re-adding somebody who said no must not ask them again")

    def test_a_decline_reaches_an_address_this_instance_only_invited(self):
        """A decline is the fail-safe direction, so it lands even over a live
        pending invite — the alternative is mailing somebody who has already
        said no on the instance they were asked from."""
        dest_tid = _tid(self.dest)
        write_config(self.dest, email_recipients=[self.declined])
        recipient_invites.invite(dest_tid, self.declined)
        self._round_trip()
        self.assertEqual(
            recipient_invites.states(dest_tid, [self.declined]
                                     )[self.declined]["state"], "declined")

    def test_a_decision_made_here_is_never_overturned_by_an_archive(self):
        dest_tid = _tid(self.dest)
        _add_user(dest_tid, self.accepted)
        recipient_invites.decline(
            recipient_invites.invite(dest_tid, self.accepted))
        self._round_trip()
        self.assertEqual(
            recipient_invites.states(dest_tid, [self.accepted]
                                     )[self.accepted]["state"],
            "declined",
            "this instance was told no by the person themselves; a file "
            "cannot undo that, member or not")

    # ---- what an unanswered invite is worth ----------------------------

    def test_an_unanswered_invite_carries_neither_a_row_nor_a_link(self):
        """It is a question, not an answer. The archive's copy has no live
        link here — the raw token is in that person's mailbox and points at
        the source instance — and a row with no token would read in Settings
        as "invite sent, waiting for them", leaving the owner waiting for a
        confirmation that can never arrive. No row means "not invited", which
        is true and has an Invite button next to it."""
        self._round_trip()
        dest_tid = _tid(self.dest)
        rows = _invite_rows(dest_tid)
        self.assertNotIn(self.pending, rows)
        self.assertEqual(recipient_invites.accepted(dest_tid, [self.pending]),
                         set())
        for email, r in rows.items():
            self.assertIsNone(
                r["token_hash"],
                f"the invite link for {email} must not be redeemable "
                f"against a restored copy")

    def test_the_archives_send_history_does_not_travel(self):
        """`last_sent_at` and `send_count` feed the live resend cooldown and
        the hourly per-tenant ceiling, so an archive full of fresh send stamps
        could throttle the invites this instance is trying to mint."""
        self._round_trip()
        row = _invite_rows(_tid(self.dest))[self.declined]
        self.assertIsNone(row["last_sent_at"])
        self.assertEqual(row["send_count"], 0)

    # ---- what the restore says it did ----------------------------------

    def test_the_restore_says_how_many_recipients_must_answer_again(self):
        counts = self._round_trip()
        note = " ".join(counts.get("_notes") or [])
        self.assertIn("the archive listed 3 recipient(s)", note)
        self.assertIn("2 must accept a fresh invite", note,
                      "the accepted stranger and the pending invite both "
                      "have to be asked here; saying so is the difference "
                      "between a migration and a silent unsubscribe")
        self.assertIn("1 had declined", note)

    def test_a_restore_that_lands_no_rows_still_reports_the_recipients(self):
        """A summary that only counts successes says nothing at all about a
        restore that wrote no rows — and that silence is the failure."""
        dest_tid = _tid(self.dest)
        recipient_invites.decline(
            recipient_invites.invite(dest_tid, self.declined))
        counts = self._round_trip()
        self.assertNotIn("recipient_invites", counts,
                         "nothing was written — every answer the archive "
                         "carried was already settled here")
        self.assertIn("2 must accept a fresh invite",
                      " ".join(counts.get("_notes") or []))

    def test_the_export_carries_the_answers_but_never_the_token(self):
        self._seed_source()
        z = zipfile.ZipFile(io.BytesIO(export.build_zip(self.src)))
        body = z.read("recipient_invites.csv").decode()
        header = body.splitlines()[0]
        self.assertNotIn("token", header)
        self.assertIn("accepted_at", header)
        for who in (self.accepted, self.declined, self.pending):
            self.assertIn(who, body)

    def test_restoring_the_same_archive_twice_changes_nothing(self):
        self._seed_source()
        data = export.build_zip(self.src)
        restore.restore_zip(self.dest, data)
        again = restore.restore_zip(self.dest, data)
        self.assertNotIn("recipient_invites", again)


if __name__ == "__main__":
    unittest.main()
