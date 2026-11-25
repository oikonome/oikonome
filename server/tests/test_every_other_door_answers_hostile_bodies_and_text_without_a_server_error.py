"""Every documented body door the two other fuzz modules do not cover, and
the path and query text of every GET door, answer hostile input with a 4xx
or a success, never a server error (tests/body_fuzz.py;
specs/invariants-and-fuzzing.md, F1).

Also here: every product door that takes a JSON body documents it, so the
fuzz (which reads the documented model) reaches every one.

Each reply is also held to F2, F4 and F5 (tests/fuzz_checks.py): a
refusal writes no row of any household table, nothing of a second
household is written or shown, and the pool is left clean."""

import unittest

from . import body_fuzz as fuzz
from .fuzz_checks import FuzzCase


class EveryDoorAnswersWithoutAServerError(FuzzCase, unittest.TestCase):
    """As the household's owner, holding a second household's ids."""

    def test_every_json_body_door_documents_its_body(self):
        undocumented = [f"{fuzz.method_of(r)} {r.path}"
                        for r in fuzz.json_body_routes(self.app)
                        if fuzz.body_of(r.endpoint) is None]
        self.assertEqual(undocumented, [])
        self.assertEqual(len(fuzz.json_body_routes(self.app)),
                         len(fuzz.doors(self.app)))

    def test_no_hostile_body_answers_5xx(self):
        routes = fuzz.group(self.app, None, rest_of=fuzz.NAMED_GROUPS)
        self.assertTrue(routes)
        self.assertEqual(fuzz.run(self.household, routes, self.checks), [])
        self.assert_other_household_untouched()

    def test_no_hostile_path_or_query_text_answers_5xx(self):
        routes = fuzz.text_routes(self.app)
        self.assertTrue(routes)
        self.assertEqual(fuzz.run_text(self.household, routes, self.checks),
                         [])
        self.assert_other_household_untouched()


if __name__ == "__main__":
    unittest.main()
