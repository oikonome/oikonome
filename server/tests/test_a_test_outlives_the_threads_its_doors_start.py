"""Every test outlives the threads the product started during it
(tests/thread_hygiene.py; specs/invariants-and-fuzzing.md, L6 and F5).

A door that hands its work to a thread (the categorize pass after an
import) answers first. A test that ended there left the work running into
the next test, holding a pooled connection inside a transaction or under
the sync lock; the fuzz modules and the concurrency harness then had to
wait such backends out. The suite now joins those threads at the end of
every test, so the next one begins with none of them running.
"""

import threading
import time
import unittest


def _product_work(done: threading.Event):
    time.sleep(0.4)
    done.set()


# a thread the product starts runs a function of an oikonome module
_product_work.__module__ = "oikonome.web.api_imports"


class ATestOutlivesTheThreadsItsDoorsStart(unittest.TestCase):
    def test_the_work_a_door_handed_off_is_finished_when_its_test_ends(self):
        done = threading.Event()

        class StartsAThreadAndReturns(unittest.TestCase):
            def test_door(self):
                threading.Thread(target=_product_work, args=(done,),
                                 daemon=True).start()

        result = unittest.TestResult()
        StartsAThreadAndReturns("test_door").run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertTrue(done.is_set(),
                        "the thread was still running after its test ended")


if __name__ == "__main__":
    unittest.main()
