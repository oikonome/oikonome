"""The background worker refuses a hosted instance that does not say how it
bills, exactly as the web process does.

The check used to run only when the web app was imported. On a hosted
image built without the billing add-on and with OIKONOME_HOSTED_BILLING
unset, the web container then refused to start while the worker came up
under the permissive no-add-on gate: polling the aggregators (billed per
connection), mailing and pushing households, and running the nightly
sweeps for an instance that bills nobody. Each case runs in a fresh
interpreter, because the check runs at import.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]

# no add-on discoverable, whatever this environment has installed
_IMPORT_WORKER = (
    "from unittest import mock\n"
    "from oikonome import ext\n"
    "mock.patch.object(ext, 'entry_points', return_value=[]).start()\n"
    "ext._group.cache_clear()\n"
    "import oikonome.jobs.worker\n"
    "print('worker-imported')\n")


def _import_worker(**env):
    full = {k: v for k, v in os.environ.items()
            if k not in ("OIKONOME_HOSTED", "OIKONOME_HOSTED_BILLING")}
    full.update(env)
    return subprocess.run([sys.executable, "-c", _IMPORT_WORKER],
                          cwd=_SERVER, env=full, capture_output=True,
                          text=True, timeout=120)


class HostedWorkerBillingTests(unittest.TestCase):

    def test_a_hosted_worker_with_no_add_on_refuses_to_start(self):
        r = _import_worker(OIKONOME_HOSTED="1")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("worker-imported", r.stdout)
        self.assertIn("OIKONOME_HOSTED_BILLING", r.stderr)

    def test_saying_none_lets_the_worker_run(self):
        r = _import_worker(OIKONOME_HOSTED="1",
                           OIKONOME_HOSTED_BILLING="none")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("worker-imported", r.stdout)

    def test_a_self_hosted_worker_never_asks(self):
        r = _import_worker()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("worker-imported", r.stdout)


if __name__ == "__main__":
    unittest.main()
