"""A bank line that already has its merchant keeps it on the day the feed
first attaches an entity id to that line's charges.

The id is new to the household, so no merchant carries it. Minting one
splits a terminal's history at the day the feed changed its mind: the new
merchant holds this week's charges, the old one every year before, and the
person is offered a merge to undo it. The line's own history answers
instead — when it has agreed with the feed about a payee, names the
entity's name, and every other row under it belongs to one ordinary
merchant with no entity of its own. That merchant takes the entity. A
twin minted before the rule existed is folded back by the nightly sweep,
unless a person has said anything about either side.
"""
import unittest

from oikonome.engine import merchant_identity, merchant_merge, merchant_split_repair
from oikonome.engine.compat import jsonb

from .util import make_db

LINE = "TAPPAY JUNIPER'S MKTRIVERTON OR"
USUAL = "Juniper's Mkt"
FEED_NAME = "Juniper's"
ENTITY = "ent-junipers"


def _ent(name=FEED_NAME, entity=ENTITY):
    return {"merchant_entity_id": entity, "merchant_name": name,
            "counterparties": [{"name": name, "type": "merchant",
                                "entity_id": entity,
                                "confidence_level": "HIGH"}]}


class _Ledger:
    def __init__(self, conn):
        self.conn = conn

    def row(self, txn_id, date, name, merchant_name, raw=None, amount=18.0):
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, pending, removed, raw)
               VALUES (%s,'card',%s,%s,%s,%s,'FOOD_AND_DRINK',0,0,%s)""",
            (txn_id, date, amount, name, merchant_name, jsonb(raw or {})))
        return txn_id

    def history(self, n=4, line=LINE, name=USUAL):
        for d in range(n):
            self.row(f"old{d}-{line[:6]}", f"2026-03-{d + 1:02d}", line, name)
        merchant_identity.resolve(self.conn)
        return self.mid(f"old0-{line[:6]}")

    def mid(self, txn_id):
        return self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["merchant_id"]

    def merchant(self, mid):
        return self.conn.execute(
            "SELECT name, name_source, plaid_entity_id, merged_into"
            "  FROM merchants WHERE id=%s", (mid,)).fetchone()

    def live(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE merged_into IS NULL"
        ).fetchone()["n"]


class TheLineKeepsItsMerchantTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.l = _Ledger(self.conn)
        self.home = self.l.history()
        self.before = self.l.live()

    def test_the_first_charge_carrying_an_entity_joins_the_lines_merchant(self):
        new = self.l.row("new1", "2026-04-02", LINE, FEED_NAME, _ent())
        merchant_identity.resolve(self.conn, txn_ids=[new])
        self.assertEqual(self.l.mid(new), self.home)
        self.assertEqual(self.l.live(), self.before)
        home = self.l.merchant(self.home)
        # the merchant keeps its name; the entity is now its own
        self.assertEqual(home["plaid_entity_id"], ENTITY)
        self.assertNotEqual(home["name"], FEED_NAME)

    def test_the_answer_holds_for_every_later_pass(self):
        new = self.l.row("new1", "2026-04-02", LINE, FEED_NAME, _ent())
        merchant_identity.resolve(self.conn, txn_ids=[new])
        # the entity's next charge, under a line with no history at all
        other = self.l.row("new2", "2026-04-09", "JUNIPERS ONLINE ORDER",
                           FEED_NAME, _ent())
        merchant_identity.resolve(self.conn, txn_ids=[other])
        self.assertEqual(self.l.mid(other), self.home)
        merchant_identity.resolve(self.conn, only_unresolved=False)
        self.assertEqual(self.l.mid(new), self.home)
        self.assertEqual(self.l.mid(other), self.home)
        self.assertEqual(self.l.live(), self.before)

    def test_history_and_the_new_charge_in_one_pass_agree_with_two(self):
        # a restore or a first import delivers everything unresolved: the
        # rows of the pass are not evidence for each other, the entity
        # mints, and nothing depends on the order they are walked in
        conn = make_db()
        self.addCleanup(conn.close)
        l = _Ledger(conn)
        for d in range(4):
            l.row(f"old{d}", f"2026-03-{d + 1:02d}", LINE, USUAL)
        new = l.row("new1", "2026-04-02", LINE, FEED_NAME, _ent())
        merchant_identity.resolve(conn)
        first = l.mid(new)
        merchant_identity.resolve(conn, only_unresolved=False)
        self.assertEqual(l.mid(new), first)

    def test_an_entity_the_line_does_not_spell_gets_its_own_merchant(self):
        new = self.l.row("new1", "2026-04-02", LINE, "Harbor Lights",
                         _ent("Harbor Lights", "ent-harbor"))
        merchant_identity.resolve(self.conn, txn_ids=[new])
        self.assertNotEqual(self.l.mid(new), self.home)
        self.assertIsNone(self.l.merchant(self.home)["plaid_entity_id"])

    def test_a_name_the_feed_uses_under_two_lines_keeps_its_own_merchant(self):
        a = self.l.row("new1", "2026-04-02", LINE, FEED_NAME, _ent())
        b = self.l.row("new2", "2026-04-03", "JUNIPERS DELI 0042 ASHBURY",
                       FEED_NAME, _ent())
        merchant_identity.resolve(self.conn, txn_ids=[a, b])
        self.assertNotEqual(self.l.mid(a), self.home)
        self.assertEqual(self.l.mid(a), self.l.mid(b))

    def test_a_line_two_merchants_share_decides_nothing(self):
        stray = self.l.row("s1", "2026-03-20", LINE, "Juniper's Mktplace")
        merchant_identity.resolve(self.conn, txn_ids=[stray])
        self.conn.execute(
            "INSERT INTO merchants (id, name, name_source) VALUES"
            " ('00000000-0000-4000-8000-000000000001', 'Juniper Annex',"
            "  'layer1')")
        self.conn.execute(
            "UPDATE transactions SET merchant_id ="
            " '00000000-0000-4000-8000-000000000001' WHERE id = 's1'")
        new = self.l.row("new1", "2026-04-02", LINE, FEED_NAME, _ent())
        merchant_identity.resolve(self.conn, txn_ids=[new])
        self.assertNotEqual(self.l.mid(new), self.home)

    def test_a_line_whose_merchant_has_an_entity_already_keeps_both(self):
        self.conn.execute(
            "UPDATE merchants SET plaid_entity_id = 'ent-other' WHERE id=%s",
            (self.home,))
        new = self.l.row("new1", "2026-04-02", LINE, FEED_NAME, _ent())
        merchant_identity.resolve(self.conn, txn_ids=[new])
        self.assertNotEqual(self.l.mid(new), self.home)
        self.assertEqual(self.l.merchant(self.home)["plaid_entity_id"],
                         "ent-other")

    def test_a_bank_constant_never_lends_its_merchant(self):
        # the line's rows were never named by the feed: nothing has agreed
        conn = make_db()
        self.addCleanup(conn.close)
        l = _Ledger(conn)
        const = "CARD PURCHASE JUNIPERS"
        for d in range(3):
            l.row(f"c{d}", f"2026-03-{d + 1:02d}", const, None)
        merchant_identity.resolve(conn)
        new = l.row("new1", "2026-04-02", const, FEED_NAME, _ent())
        merchant_identity.resolve(conn, txn_ids=[new])
        self.assertNotEqual(l.mid(new), l.mid("c0"))


class TheTwinAlreadyMintedTests(unittest.TestCase):
    """The state a household is in when the twin was minted before the
    resolver asked the line."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.l = _Ledger(self.conn)
        self.home = self.l.history()
        self.rows = [self.l.row(f"new{n}", f"2026-04-0{n + 2}", LINE,
                                FEED_NAME, _ent()) for n in range(2)]
        self.twin = self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id,"
            " created_at) VALUES (%s, 'plaid', %s,"
            " now() + interval '1 minute') RETURNING id",
            (FEED_NAME, ENTITY)).fetchone()["id"]
        self.conn.execute(
            "UPDATE transactions SET merchant_id=%s WHERE id = ANY(%s)",
            (self.twin, self.rows))
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method,"
            " as_of, merchant_id) VALUES (%s,%s,'layer1',now(),%s)",
            (FEED_NAME, FEED_NAME, self.twin))

    def _mine(self, got):
        return [m for m in got["merges"]
                if m.get("kind") == "first-entity-name"]

    def test_a_dry_run_names_the_fold_and_writes_nothing(self):
        recs = self._mine(merchant_split_repair.repair(self.conn, apply=False))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["merge"], [FEED_NAME])
        self.assertEqual(recs[0]["rows"], 2)
        self.assertEqual(self.l.mid(self.rows[0]), self.twin)

    def test_the_sweep_folds_the_twin_into_the_lines_merchant_once(self):
        name = self.l.merchant(self.home)["name"]
        merchant_split_repair.repair(self.conn, apply=True)
        for r in self.rows:
            self.assertEqual(self.l.mid(r), self.home)
        self.assertEqual(self.l.merchant(self.twin)["merged_into"], self.home)
        self.assertEqual(self.l.merchant(self.home)["name"], name)
        again = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(again["merges"], [])
        # the entity's next charge finds the survivor, and so does a
        # whole-ledger re-resolve
        nxt = self.l.row("new9", "2026-04-20", LINE, FEED_NAME, _ent())
        merchant_identity.resolve(self.conn, txn_ids=[nxt])
        self.assertEqual(self.l.mid(nxt), self.home)
        merchant_identity.resolve(self.conn, only_unresolved=False)
        self.assertEqual(self.l.mid(self.rows[0]), self.home)

    def test_a_twin_a_person_named_is_left_alone(self):
        self.conn.execute(
            "UPDATE merchants SET name_source='manual' WHERE id=%s",
            (self.twin,))
        self.assertEqual(self._mine(
            merchant_split_repair.repair(self.conn, apply=True)), [])
        self.assertEqual(self.l.mid(self.rows[0]), self.twin)

    def test_a_pair_a_person_called_two_businesses_is_left_alone(self):
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id,"
            " into_merchant_id, status) VALUES (%s, %s, %s, 'rejected')",
            (merchant_merge._sig(str(self.twin), str(self.home)),
             self.twin, self.home))
        self.assertEqual(self._mine(
            merchant_split_repair.repair(self.conn, apply=True)), [])
        self.assertEqual(self.l.mid(self.rows[0]), self.twin)

    def test_a_twin_with_charges_under_another_line_is_left_alone(self):
        self.l.row("far", "2026-04-11", "JUNIPERS DELI 0042 ASHBURY",
                   FEED_NAME, _ent())
        self.conn.execute(
            "UPDATE transactions SET merchant_id=%s WHERE id='far'",
            (self.twin,))
        self.assertEqual(self._mine(
            merchant_split_repair.repair(self.conn, apply=True)), [])


if __name__ == "__main__":
    unittest.main()
