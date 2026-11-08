"""The hourly demo job runs inside the worker process. When the demo reset
refuses (no tenant yet, a second tenant, a household that is not a demo),
the refusal must come back as the job's result, not escape as a
process-ending exception: a SystemExit raised inside a job ends the whole
worker, and every job in flight with it, once an hour."""

import asyncio
import os
import unittest
from unittest import mock


class DemoResetJobRefusalTests(unittest.TestCase):

    def setUp(self):
        self._saved = dict(os.environ)
        os.environ["OIKONOME_DEMO"] = "1"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_a_refused_reset_is_reported_not_raised(self):
        from oikonome import demo
        from oikonome.jobs import worker
        for why in ("demo-reset needs exactly one tenant (found 0)",
                    "this instance is not a demo — refusing to reset"):
            with mock.patch.object(demo, "reset",
                                   side_effect=SystemExit(why)):
                try:
                    out = asyncio.run(worker.demo_reset_all(None))
                except BaseException as e:   # noqa: BLE001
                    self.fail(f"the job raised {type(e).__name__}: {e}")
            self.assertIn(why, out)


if __name__ == "__main__":
    unittest.main()
