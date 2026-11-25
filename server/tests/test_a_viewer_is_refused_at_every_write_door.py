"""A VIEWER is refused at every write door, with a 403, and never a 5xx
or a write (specs/invariants-and-fuzzing.md, F2, F4 and F5 for the viewer
role; tests/body_fuzz.py, tests/fuzz_checks.py).

The write doors are every POST/PUT/PATCH/DELETE route whose handler reads
the session, the page forms included. Two kinds are the viewer's own and
are not refused: the doors to the caller's own account and devices
(`permissions.SELF_WRITE_OK`) and the reads that arrive as a POST
(`permissions.READ_POST_OK`, the assistant). Those get every hostile
value and whole-body class instead, held to the same checks: no 5xx, no
row of any household table written on a refusal, nothing of a second
household written or shown, a clean pool. The same doors sent the second
household's ids are in
test_no_reply_to_one_household_carries_another_to_a_viewer.py.
"""

import unittest

from . import body_fuzz as fuzz
from .fuzz_checks import FuzzCase, viewers_own


class AViewerIsRefusedAtEveryWriteDoor(FuzzCase, unittest.TestCase):
    ROLE = "viewer"

    def test_every_household_write_door_answers_403(self):
        doors = fuzz.write_doors(self.app)
        self.assertGreater(len(doors), 150)

        def refused(route, resp):
            if viewers_own(route.path) or resp.status_code == 403:
                return None
            return "a viewer was not refused"
        self.assertEqual(fuzz.run_plain(self.household, doors, self.checks,
                                        expect=refused), [])
        self.assert_other_household_untouched()

    def test_the_viewers_own_doors_take_hostile_bodies(self):
        own = [r for r in fuzz.doors(self.app) if viewers_own(r.path)]
        self.assertTrue(own)
        self.assertEqual(fuzz.run(self.household, own, self.checks,
                                  text_classes=False), [])
        self.assert_other_household_untouched()


if __name__ == "__main__":
    unittest.main()
