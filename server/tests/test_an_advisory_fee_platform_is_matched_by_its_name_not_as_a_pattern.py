"""The advisory-fee estimate finds a platform by the name the household
typed, read literally: a `%` or `_` in it matches only an institution whose
name holds that character.

The typed fragment was the body of a LIKE pattern, so a platform typed as
"%" matched every investment institution and the estimate charged the fee
on all of them, and a "_" matched any one character.
"""

import unittest
from unittest import mock

from oikonome.engine import budget, reporting

from .util import TODAY, make_db

INSTITUTIONS = {"it-robo": "Robo_Advisor 100%", "it-plain": "Plain Brokerage"}


class AFeePlatformIsANameNotAPattern(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        for item, name in INSTITUTIONS.items():
            self.conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token) VALUES (%s, 'test', %s, 'tok')", (item, name))
            self.conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype, "
                "balance_current) VALUES (%s, %s, 'Managed', 'investment', "
                "'brokerage', 1000)", (f"acct-{item}", item))

    def _matched(self, token: str) -> set[str]:
        with budget.config_txn(self.conn) as cfg:
            cfg["advisory_fees"] = {token: 0.0025}
        seen: set[str] = set()

        def fee(conn, ids, rate, today=None):
            seen.update(ids)
            return {"2026": 1.0}
        with mock.patch.object(reporting, "_managed_fee_by_year", fee):
            reporting._advisory_fee_estimate(self.conn, TODAY)
        return seen

    def test_a_wildcard_character_matches_only_a_name_that_holds_it(self):
        self.assertEqual(self._matched("%"), {"acct-it-robo"})
        self.assertEqual(self._matched("_"), {"acct-it-robo"})
        self.assertEqual(self._matched("e_a"), set())   # not "era"
        self.assertEqual(self._matched("\\"), set())

    def test_an_ordinary_fragment_still_matches_as_before(self):
        self.assertEqual(self._matched("plain"), {"acct-it-plain"})
        self.assertEqual(self._matched("ROBO"), {"acct-it-robo"})


if __name__ == "__main__":
    unittest.main()
