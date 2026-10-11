"""A hosted instance says how it bills, or does not start.

A hosted image built without the billing add-on used to come up in the
documented no-add-on shape and bill nobody, with nothing to tell the
operator. Now a hosted instance with no add-on must say
OIKONOME_HOSTED_BILLING=none; anything else refuses at import. Self-host
is untouched, and a registered add-on that failed to load stays the
fail-closed gate's business."""

import os
import unittest
from unittest import mock

from oikonome import ext


class HostedBillingStartupTests(unittest.TestCase):

    def setUp(self):
        ext._group.cache_clear()
        ext._LOAD_FAILED.discard("oikonome.gate")
        self.addCleanup(ext._group.cache_clear)
        self.addCleanup(ext._LOAD_FAILED.discard, "oikonome.gate")

    def _check(self, *, hosted, eps, billing=None):
        env = {"OIKONOME_HOSTED": "1" if hosted else "",
               ext.HOSTED_BILLING_VAR: billing or ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ext, "entry_points", return_value=eps):
            ext._group.cache_clear()
            ext.require_hosted_billing()

    def test_a_hosted_instance_with_no_add_on_refuses_to_start(self):
        with self.assertRaises(SystemExit) as cm:
            self._check(hosted=True, eps=[])
        self.assertIn(ext.HOSTED_BILLING_VAR, str(cm.exception))

    def test_saying_none_lets_it_run(self):
        self._check(hosted=True, eps=[], billing="none")

    def test_any_other_word_still_refuses(self):
        with self.assertRaises(SystemExit):
            self._check(hosted=True, eps=[], billing="later")

    def test_an_installed_add_on_satisfies_it(self):
        class _Ep:
            name = "hosted"
            def load(self):
                return ext.Gate
        self._check(hosted=True, eps=[_Ep()])

    def test_self_host_never_asks(self):
        self._check(hosted=False, eps=[])

    def test_a_broken_add_on_is_the_fail_closed_gates_business(self):
        class _Ep:
            name = "hosted"
            def load(self):
                raise ImportError("half-built")
        # the load failure is recorded; the fail-closed gate takes over
        # and startup is not refused on top of it
        self._check(hosted=True, eps=[_Ep()])
        self.assertIn("oikonome.gate", ext._LOAD_FAILED)


if __name__ == "__main__":
    unittest.main()
