"""SMTP STARTTLS stays on unless the operator explicitly turns it off.

The invariant: every spelling an operator would reasonably write for
"yes" leaves transport encryption ON, and only a real "off" value turns
it off. The failure mode this protects against is silent and invisible
from the config file — reading the knob as `== "1"` meant
`OIKONOME_SMTP_STARTTLS=true` DISABLED STARTTLS, so mail (password
resets, invitations, the daily verdict) went to the relay in the clear
while the configuration said the opposite.

A compose-forwarded but unset variable arrives as an empty string, which
must also read as on rather than crash or disable.
"""

import unittest
from unittest import mock

from oikonome.web import report

VAR = "OIKONOME_SMTP_STARTTLS"


class StarttlsFlagTests(unittest.TestCase):
    def _starttls(self, value=None):
        env = {} if value is None else {VAR: value}
        with mock.patch.dict("os.environ", env, clear=False):
            if value is None:
                import os
                os.environ.pop(VAR, None)
            return report._env_smtp()["starttls"]

    def test_unset_or_empty_keeps_encryption_on(self):
        self.assertTrue(self._starttls(None))
        self.assertTrue(self._starttls(""))
        self.assertTrue(self._starttls("   "))

    def test_every_truthy_spelling_keeps_encryption_on(self):
        for v in ("1", "true", "TRUE", "yes", "on", " True "):
            self.assertTrue(self._starttls(v), v)

    def test_only_an_explicit_off_disables_encryption(self):
        for v in ("0", "false", "FALSE", "no", "off"):
            self.assertFalse(self._starttls(v), v)

    def test_an_empty_port_falls_back_to_the_default(self):
        """compose declares every key it forwards, so an unset port
        arrives as "" — `int("")` at call time is a mail send that dies
        on a knob nobody set."""
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_PORT": ""}):
            self.assertEqual(report._env_smtp()["port"], 587)


if __name__ == "__main__":
    unittest.main()
