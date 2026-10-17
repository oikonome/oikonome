"""Credential redaction over error text must stay bounded whatever the
text's length, and must redact the same spans it always did.

The text it runs over comes from exceptions a syncer raised — some of it
written by a third-party or tenant-chosen server — so a pattern that is
quadratic on a long run of word characters is a way for that server to
stall a worker with one reply. The text only ever reaches a log line or an
error cell, so only its head is scanned and kept, and a credential the cut
falls inside is dropped rather than half-kept."""

import time
import unittest

from oikonome.sync.base import REDACT_SCAN_CHARS, redact_url_credentials


class RedactionIsLinear(unittest.TestCase):
    def test_a_long_word_run_is_handled_quickly(self):
        t0 = time.monotonic()
        out = redact_url_credentials("0" * 200_000)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertLess(len(out), REDACT_SCAN_CHARS + 100)

    def test_adversarial_text_of_any_length_returns_in_bounded_time(self):
        shapes = ("a://" + "b" * 2_000_000,
                  "a://" * 500_000,
                  ("x" * 50 + "://") * 40_000,
                  " a://" + ":" * 2_000_000 + "@",
                  "-a://-" * 400_000)
        for text in shapes:
            with self.subTest(head=text[:12]):
                t0 = time.monotonic()
                out = redact_url_credentials(text)
                self.assertLess(time.monotonic() - t0, 0.25)
                self.assertLess(len(out), REDACT_SCAN_CHARS + 100)

    def test_a_credential_the_cut_falls_inside_is_not_half_kept(self):
        head = "e" * (REDACT_SCAN_CHARS - 12) + " "
        out = redact_url_credentials(
            head + "https://user:hunter2secret@bridge.example/a b")
        self.assertNotIn("user", out)
        self.assertNotIn("hunter", out)
        self.assertTrue(out.startswith(head))

    def test_short_text_is_kept_whole(self):
        text = "e" * (REDACT_SCAN_CHARS - 1)
        self.assertEqual(redact_url_credentials(text), text)

    def test_redacts_the_same_spans(self):
        cases = {
            "x https://a:b@h/p and ftp://u@h": "x https://***@h/p and ftp://***@h",
            "no creds https://host/p": "no creds https://host/p",
            "ConnectError: https://user:pw@bridge.example/simplefin/accounts":
                "ConnectError: https://***@bridge.example/simplefin/accounts",
            "path https://h/a@b stays": "path https://h/a@b stays",
            "": "",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(redact_url_credentials(src), want)
        self.assertIsNone(redact_url_credentials(None))
