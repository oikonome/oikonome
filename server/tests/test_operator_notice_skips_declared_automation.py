"""An instance a test harness walks must not mail its operator about the
households that harness mints.

Each harness run signs up a fresh address, and every one of those raises a
"new signup" notice — enough runs bury the operator's inbox in reports of
people who do not exist. The notice itself is worth
keeping, so the operator names the addresses that are their own automation
and only those are muted."""

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from .util import _ensure_db

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "compose.yaml"


class OperatorNoticeMutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def _muted(self, addr, skip):
        from oikonome.web.app import operator_notice_muted
        with mock.patch.dict(os.environ,
                             {"OIKONOME_OPERATOR_NOTICE_SKIP": skip}):
            return operator_notice_muted(addr)

    def test_unset_mutes_nothing(self):
        self.assertFalse(self._muted("someone@example.com", ""))
        self.assertFalse(self._muted("bot+ci-run1@example.net", ""))

    def test_glob_matches_the_harness_pool(self):
        pat = "bot+*@example.net"
        for addr in ("bot+ci-run1@example.net", "bot+qa2-run3@example.net",
                     "BOT+Ci-Run1@EXAMPLE.NET"):
            self.assertTrue(self._muted(addr, pat), addr)

    def test_a_real_household_is_never_muted(self):
        pat = "bot+*@example.net"
        for addr in ("person@example.com", "bot@example.net",
                     "notbot+ci@example.net", "bot+ci@example.com"):
            self.assertFalse(self._muted(addr, pat), addr)

    def test_several_patterns_and_whitespace(self):
        pat = " bot+*@example.net , *@test.invalid "
        self.assertTrue(self._muted("bot+ci-run1@example.net", pat))
        self.assertTrue(self._muted("whoever@test.invalid", pat))
        self.assertFalse(self._muted("whoever@example.com", pat))

    def test_muted_address_sends_no_operator_mail(self):
        """The mute is wired into the notice, not merely available."""
        import oikonome.web.app as appmod
        sent = []
        with mock.patch.dict(os.environ, {
                "OIKONOME_OPERATOR_EMAIL": "op@example.com",
                "OIKONOME_OPERATOR_NOTICE_SKIP": "bot+*@example.net"}), \
             mock.patch.object(appmod, "_notify_operator_signup",
                               side_effect=lambda *a, **k: sent.append(a)):
            appmod._operator_signup_notice("bot+ci-run1@example.net", "t-1")
            self.assertEqual(sent, [])

    def test_the_mute_list_reaches_the_process_that_reads_it(self):
        """A knob compose never declares is not a setting, it is a comment.

        The mute is read at the signup door, so the web service's own
        environment block has to carry it. Without that an operator can put
        the globs in their .env, restart, and be mailed about every
        household their harness mints — the setting looks obeyed and does
        nothing, which is the failure mode with no error message."""
        text = COMPOSE.read_text()
        start = text.index("\n  app:\n")
        block = text[start + 1:]
        end = re.search(r"\n  \S", block)
        block = block[:end.start()] if end else block
        self.assertIn("OIKONOME_OPERATOR_NOTICE_SKIP:", block)

    def test_ordinary_address_still_notifies(self):
        import threading

        import oikonome.web.app as appmod
        started = []
        with mock.patch.dict(os.environ, {
                "OIKONOME_OPERATOR_EMAIL": "op@example.com",
                "OIKONOME_OPERATOR_NOTICE_SKIP": "bot+*@example.net"}), \
             mock.patch.object(threading, "Thread") as T:
            T.side_effect = lambda *a, **k: started.append(k) or mock.MagicMock()
            appmod._operator_signup_notice("person@example.com", "t-2")
        self.assertEqual(len(started), 1)


