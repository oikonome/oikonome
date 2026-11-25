"""Every test outlives the threads the product started during it.

A door that hands its work to a thread of its own (the categorize pass
after an import, the restore job a ZIP import starts, a bulk import's
nudge) answers before the work is done, and the work holds a pooled
connection: inside a transaction, or holding the household's sync lock.
Left running past its test it would still hold them when the next test
began, which would then meet another test's lock on the pool
(specs/invariants-and-fuzzing.md, L6 and F5). So the suite joins them at the end of every test
(`util.join_product_threads`), whatever the test itself does.

OIKONOME_THREAD_SURVEY=<file> appends one line per test that left such a
thread running, with how long the join waited: the way to see which
tests start them.
"""

from __future__ import annotations

import os
import threading
import time
import unittest

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    from .util import join_product_threads
    survey = os.environ.get("OIKONOME_THREAD_SURVEY")
    orig_run = unittest.TestCase.run

    def run(self, result=None):
        before = set(threading.enumerate())
        try:
            return orig_run(self, result)
        finally:
            if survey:
                left = [t.name for t in threading.enumerate()
                        if t not in before and getattr(t, "_target", None)
                        and (getattr(t._target, "__module__", "") or ""
                             ).startswith("oikonome.") and t.is_alive()]
                t0 = time.monotonic()
            join_product_threads(before)
            if survey and left:
                still = [t.name for t in threading.enumerate()
                         if t not in before and t.is_alive()
                         and getattr(t, "_target", None)
                         and (getattr(t._target, "__module__", "") or ""
                              ).startswith("oikonome.")]
                with open(survey, "a") as fh:
                    fh.write(f"{self.id()}\t{len(left)}\t"
                             f"{time.monotonic() - t0:.2f}s\t"
                             f"still={still}\n")

    unittest.TestCase.run = run
