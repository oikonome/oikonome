"""OIKONOME_AUTOBAN is on by default and read like every other on/off knob
once a value is written: only 1/true/yes/on keep it on. The failure this
protects against: an operator writes `disabled` (or `none`, `n`) to stop the
app banning their own proxy, and bans keep happening because the switch
only recognised four spellings of "off"."""

import os
import unittest
from unittest import mock

from oikonome.web import security


class AutobanSwitchSpelling(unittest.TestCase):
    def _on(self, value):
        env = dict(os.environ)
        env.pop("OIKONOME_AUTOBAN", None)
        if value is not None:
            env["OIKONOME_AUTOBAN"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return security.autoban_enabled()

    def test_unset_and_empty_keep_the_default_on(self):
        self.assertTrue(self._on(None))
        self.assertTrue(self._on(""))
        self.assertTrue(self._on("  "))

    def test_truthy_spellings_are_on(self):
        for v in ("1", "true", "YES", "On"):
            self.assertTrue(self._on(v), v)

    def test_any_other_written_value_is_off(self):
        for v in ("0", "false", "no", "off", "disabled", "none", "n"):
            self.assertFalse(self._on(v), v)


if __name__ == "__main__":
    unittest.main()
