"""Every documented body door that is not a session, household, ledger,
bill, business or account door (the module beside this one takes those)
answers a household MEMBER's hostile bodies with a 4xx or a success,
never a server error, and a refusal writes nothing (tests/body_fuzz.py,
tests/fuzz_checks.py; specs/invariants-and-fuzzing.md, F1, F2, F4 and F5,
all roles).

A member reaches the owner-only doors only to be refused, and the money
doors as the owner does. Every value class and whole-body class reaches
every door; the large text bodies, which cost most of the owner's run and
test the same parsing a member's body meets, are the owner modules' (the
role decides who may write, not how a body is read). Each reply is held
to the same checks as the owner's: no row written on a refusal, nothing
of a second household written or shown, a clean pool."""

import unittest

from . import body_fuzz as fuzz
from .fuzz_checks import FuzzCase


class EveryDoorAnswersAMember(FuzzCase, unittest.TestCase):
    ROLE = "member"

    def test_no_hostile_body_answers_5xx_or_writes_on_a_refusal(self):
        routes = fuzz.group(self.app, None, rest_of=fuzz.NAMED_GROUPS)
        self.assertTrue(routes)
        self.assertEqual(fuzz.run(self.household, routes, self.checks,
                                  text_classes=False), [])
        self.assert_other_household_untouched()


if __name__ == "__main__":
    unittest.main()
