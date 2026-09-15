"""Which rule reaches which transaction, and — when more than one does —
which one wins.

A merchant_categories rule is keyed on the merchant ROW when its string
resolves to one, and on the canonical string otherwise. A transaction is
keyed the same way, but the two sides do not always resolve together: the
rule can name a merchant while the transaction is still unresolved (a row
that arrived after the last identity pass, or a fresh import). That row
must still take the rule — the alternative is a user writing a rule from a
transaction and watching it not apply to that transaction.

Once both routes exist a row can match two rules with different answers.
The choice must be a decision, not whatever the planner emitted: the
user's own rule first, then the merchant-keyed one.
"""

import unittest

from oikonome.engine import llm_categorize

from .util import add_txn, make_db


def _rule(conn, merchant, category, source="llm"):
    conn.execute(
        """INSERT INTO merchant_categories (merchant, category_primary, source)
           VALUES (%s,%s,%s)
           ON CONFLICT (tenant_id, merchant) DO UPDATE
               SET category_primary = EXCLUDED.category_primary,
                   source = EXCLUDED.source""", (merchant, category, source))


def _alias(conn, raw, canonical, merchant_id=None, method="layer1"):
    conn.execute(
        """INSERT INTO merchant_canonical (raw_merchant, canonical, method,
                                           merchant_id)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (tenant_id, raw_merchant) DO UPDATE
               SET canonical = EXCLUDED.canonical,
                   merchant_id = EXCLUDED.merchant_id""",
        (raw, canonical, method, merchant_id))


def _merchant(conn, name):
    return conn.execute(
        "INSERT INTO merchants (name) VALUES (%s) RETURNING id",
        (name,)).fetchone()["id"]


def _cat(conn, txn_id):
    return conn.execute(
        "SELECT category_primary, category_source FROM transactions WHERE id=%s",
        (txn_id,)).fetchone()


class RuleReachesUnresolvedRow(unittest.TestCase):

    def test_merchant_keyed_rule_applies_to_a_row_with_no_merchant_yet(self):
        conn = make_db()
        self.addCleanup(conn.close)
        mid = _merchant(conn, "Costco")
        # the rule resolves to the merchant ROW…
        _alias(conn, "COSTCO WHSE #1023", "Costco", mid)
        _rule(conn, "COSTCO WHSE #1023", "FOOD_AND_DRINK")
        # …while the transaction is not resolved yet, only deduped
        tid = add_txn(conn, "2026-07-01", 88.0, "COSTCO WHSE #1023",
                      merchant="COSTCO WHSE #1023", primary="GENERAL_MERCHANDISE")

        llm_categorize.apply(conn)

        self.assertEqual(_cat(conn, tid)["category_primary"], "FOOD_AND_DRINK")


class TwoRulesOneRow(unittest.TestCase):

    def _two_routes(self, conn, string_source="llm", merchant_source="llm"):
        """A row that both a merchant-keyed rule and a canonical-string rule
        can reach, each with its own answer."""
        mid = _merchant(conn, "Costco")
        _alias(conn, "COSTCO WHSE #1023", "Costco", mid)
        _rule(conn, "COSTCO WHSE #1023", "FOOD_AND_DRINK", merchant_source)
        # a second rule whose string carries no merchant of its own but
        # whose canonical is the same display name
        _alias(conn, "COSTCO FUEL", "Costco", None)
        _rule(conn, "COSTCO FUEL", "TRANSPORTATION", string_source)
        return add_txn(conn, "2026-07-02", 44.0, "COSTCO WHSE #1023",
                       merchant="COSTCO WHSE #1023",
                       primary="GENERAL_MERCHANDISE")

    def test_the_merchant_keyed_rule_wins_over_a_string_only_rule(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = self._two_routes(conn)
        llm_categorize.apply(conn)
        self.assertEqual(_cat(conn, tid)["category_primary"], "FOOD_AND_DRINK")

    def test_the_users_own_rule_wins_even_when_it_is_the_string_one(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = self._two_routes(conn, string_source="user",
                               merchant_source="model")
        llm_categorize.apply(conn)
        row = _cat(conn, tid)
        self.assertEqual(row["category_primary"], "TRANSPORTATION")
        self.assertEqual(row["category_source"], "rule_user")


class UserRuleSurvivesADisagreeingAliasRule(unittest.TestCase):
    """Two aliases of ONE merchant carrying rules that disagree cancel each
    other — that is the unanimity gate, and it is deliberate. It must never
    cancel the household's OWN answer: a seed or model rule on a second
    spelling cannot outvote what the user said about the merchant."""

    def test_a_model_rule_on_a_sibling_alias_does_not_cancel_the_user_rule(self):
        conn = make_db()
        self.addCleanup(conn.close)
        mid = _merchant(conn, "Costco")
        _alias(conn, "COSTCO WHSE #1023", "Costco", mid)
        _alias(conn, "COSTCO GAS #55", "Costco", mid)
        _rule(conn, "COSTCO WHSE #1023", "FOOD_AND_DRINK", "user")
        _rule(conn, "COSTCO GAS #55", "TRANSPORTATION", "model")
        tid = add_txn(conn, "2026-07-03", 30.0, "COSTCO WHSE #1023",
                      merchant="COSTCO WHSE #1023", primary="GENERAL_MERCHANDISE")
        conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                     (mid, tid))

        llm_categorize.apply(conn)

        row = _cat(conn, tid)
        self.assertEqual(row["category_primary"], "FOOD_AND_DRINK")
        self.assertEqual(row["category_source"], "rule_user")


if __name__ == "__main__":
    unittest.main()
