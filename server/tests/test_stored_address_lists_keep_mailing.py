"""Address lists written before every address was checked one by one must
keep reaching the mailboxes they name.

The send path refuses a recipient that is not one bare mailbox. Values
stored earlier — an operator variable holding `a,b`, a household list
entry `a;b` or `Name <a>`, a restored archive — were delivered before
that rule. Read as single addresses, each one would be refused: operator
alerts fall to a log line, a household member stops getting mail, and a
Settings save that re-posts the stored list is refused without saying which
entry is wrong. So lists are split into their mailboxes at the door that
reads them, only an entry that names no mailbox is dropped (and named), and
the rest of the message still goes."""

import os
import unittest
from unittest import mock

from fastapi import HTTPException

from oikonome.web import mailguard, report

RELAY = {"host": "relay.example.dev", "port": 587, "user": None,
         "password": None, "sender": "o@example.dev", "starttls": False,
         "configured": True}


def _sent_to(send):
    smtp_cls = mock.MagicMock()
    conn = smtp_cls.return_value.__enter__.return_value
    with mock.patch("smtplib.SMTP", smtp_cls), \
         mock.patch("oikonome.web.netguard.pinned_smtp_host",
                    side_effect=lambda h: h), \
         mock.patch.object(report, "_record_outcome", create=True):
        send()
    msgs = [c.args[0] for c in conn.send_message.call_args_list]
    return [m["To"] for m in msgs]


class SplitMailboxes(unittest.TestCase):
    def test_recoverable_shapes_become_their_mailboxes(self):
        good, bad = mailguard.split_mailboxes(
            ["a@x.example;b@y.example", "Some One <c@z.example>",
             '"Doe, Jane" <d@w.example>', "A@X.example"])
        self.assertEqual(good, ["a@x.example", "b@y.example", "c@z.example",
                                "d@w.example"])
        self.assertEqual(bad, [])

    def test_unrecoverable_entry_is_returned_not_guessed(self):
        good, bad = mailguard.split_mailboxes(
            "ok@x.example, a b@x.example, nothing-here")
        self.assertEqual(good, ["ok@x.example"])
        self.assertEqual(bad, ["a b@x.example", "nothing-here"])


class OperatorVariableIsAList(unittest.TestCase):
    def test_comma_list_reaches_every_operator_address(self):
        from oikonome.jobs import worker
        env = {"OIKONOME_OPERATOR_EMAIL":
                   "ops-one@example.dev, ops-two@example.dev",
               "OIKONOME_SMTP_HOST": "relay.example.dev"}
        with mock.patch.dict(os.environ, env):
            to = _sent_to(lambda: self.assertTrue(
                worker._mail_operator("s", "p", "<p>h</p>")))
        self.assertEqual(len(to), 1)
        self.assertIn("ops-one@example.dev", to[0])
        self.assertIn("ops-two@example.dev", to[0])

    def test_one_bad_operator_entry_does_not_silence_the_rest(self):
        with mock.patch.dict(os.environ, {"OIKONOME_OPERATOR_EMAIL":
                                          "ops@example.dev; not-an-address"}):
            self.assertEqual(mailguard.operator_addresses(),
                             ["ops@example.dev"])


class SendDropsOnlyTheBadRecipient(unittest.TestCase):
    def test_display_form_is_delivered_to_its_mailbox(self):
        to = _sent_to(lambda: report.send(
            "s", "p", "<p>h</p>", ["Some One <one@example.dev>"],
            smtp=RELAY, bcc=False))
        self.assertEqual(to, ["one@example.dev"])

    def test_bad_entry_is_skipped_and_the_rest_still_go(self):
        to = _sent_to(lambda: report.send(
            "s", "p", "<p>h</p>", ["one@example.dev", "not an address"],
            smtp=RELAY, bcc=False))
        self.assertEqual(to, ["one@example.dev"])

    def test_a_multi_mailbox_entry_is_still_never_split_at_send(self):
        """A login email that reads as two addresses would reach a mailbox
        nobody verified; only list doors split."""
        with self.assertRaises(ValueError):
            _sent_to(lambda: report.send(
                "s", "p", "<p>h</p>", ["me@mine.example,x@else.example"],
                smtp=RELAY, bcc=False))


class SettingsSaveAcceptsStoredShapes(unittest.TestCase):
    def test_stored_joined_and_display_entries_parse_to_mailboxes(self):
        recips = mailguard.parse_recipients(
            ["a@x.example;b@y.example", "Some One <c@z.example>"])
        self.assertEqual(recips, ["a@x.example", "b@y.example",
                                  "c@z.example"])
        with mock.patch.object(mailguard, "members_only", return_value=False):
            mailguard.check_recipients(None, recips)   # no 400

    def test_refusal_names_the_entry(self):
        recips = mailguard.parse_recipients(["ok@x.example", "a b@x.example"])
        with mock.patch.object(mailguard, "members_only", return_value=False):
            with self.assertRaises(HTTPException) as cm:
                mailguard.check_recipients(None, recips)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("a b@x.example", cm.exception.detail)

    def test_restore_drops_only_the_unrecoverable_entry(self):
        with mock.patch.object(mailguard, "members_only", return_value=False):
            kept = mailguard.filter_recipients(
                None, ["a@x.example;b@y.example", "junk entry"])
        self.assertEqual(kept, ["a@x.example", "b@y.example"])


class HouseholdListKeepsItsAgreedMailboxes(unittest.TestCase):
    def test_joined_entry_mails_each_mailbox_that_accepted(self):
        """A joined entry is split and each of its people who said yes is
        mailed; a display-form entry's recorded consent is its one
        mailbox's. One consent recorded under a joined entry is not both
        people's — that case is pinned in
        test_legacy_recipient_entries_keep_their_people."""
        from oikonome.jobs import worker
        joined = "a@x.example;b@y.example"
        cfg = {"email_recipients": [joined, "Some One <c@z.example>"]}
        admin = mock.MagicMock()
        admin.execute.return_value.fetchall.return_value = [
            {"email": "owner@example.dev"}]
        agreed = {"a@x.example", "b@y.example", "some one <c@z.example>"}
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}), \
             mock.patch("oikonome.engine.budget.load_config",
                        return_value=cfg), \
             mock.patch.object(worker.tenancy, "admin_connect",
                               return_value=admin), \
             mock.patch("oikonome.notify.recipient_invites.states",
                        side_effect=lambda t, es: {
                            e.lower(): {"state": "accepted"}
                            for e in es if e.lower() in agreed}):
            out = worker._recipients_raw(None, "t")
        self.assertEqual(out, ["owner@example.dev", "a@x.example",
                               "b@y.example", "c@z.example"])


if __name__ == "__main__":
    unittest.main()
