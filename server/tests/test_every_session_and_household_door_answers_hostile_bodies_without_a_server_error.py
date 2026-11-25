"""Every documented body door among the session, sign-in and household doors (web/app.py) answers
every hostile body with a 4xx or a success, never a server error
(tests/body_fuzz.py; specs/invariants-and-fuzzing.md, F1). A failure names
the door, the field and the class of value that broke it.

Each reply is also held to F2, F4 and F5 (tests/fuzz_checks.py): a
refusal writes no row of any household table, nothing of a second
household is written or shown, and the pool is left clean."""

import unittest

from . import body_fuzz as fuzz
from .fuzz_checks import FuzzCase


class EveryDoorAnswersWithoutAServerError(FuzzCase, unittest.TestCase):
    """As the household's owner, holding a second household's ids."""

    def test_no_hostile_body_answers_5xx(self):
        routes = fuzz.group(self.app, fuzz.GROUP_SESSION_AND_HOUSEHOLD)
        self.assertTrue(routes)
        self.assertEqual(fuzz.run(self.household, routes, self.checks), [])
        self.assert_other_household_untouched()


if __name__ == "__main__":
    unittest.main()
