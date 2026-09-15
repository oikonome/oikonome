"""The merchant graph must survive pointers that lie.

Three ways an id stops meaning what it said, and what each must NOT do:

  * an alias (or a transaction) naming a merchant row that is not there —
    a restored archive that carried the aliases but not the merchants.
    Resolution must fall back to the name, never crash: the nightly pass
    resolves every tenant, so one bad pointer would take that household's
    merchant identity down every night until someone looked.
  * a merged-into CHAIN (A merged into B, B later merged into C) and a
    merged-into CYCLE. Resolution must land on the terminal survivor and
    must terminate.
  * two syncs resolving the same new Plaid entity at once. The unique
    index makes one of them the loser; the loser must adopt the winner's
    merchant, not raise and abort the pass.
"""

import unittest
import uuid

from oikonome.engine import merchant_identity

from .util import add_txn, make_db


def _mid(conn, name):
    return conn.execute("SELECT id FROM merchants WHERE name=%s",
                        (name,)).fetchone()["id"]


def _merchant_of(conn, txn_id):
    row = conn.execute(
        "SELECT m.name FROM transactions t JOIN merchants m ON m.id = t.merchant_id "
        "WHERE t.id=%s", (txn_id,)).fetchone()
    return row["name"] if row else None


class DanglingPointers(unittest.TestCase):
    def test_alias_naming_a_missing_merchant_resolves_by_name(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = add_txn(conn, "2026-07-01", 42.0, "COSTCO WHSE #1023",
                      merchant="Costco Wholesale")
        merchant_identity.resolve(conn)
        # the shape a restore leaves behind: the alias kept its merchant_id,
        # the merchant row it names never arrived
        ghost = str(uuid.uuid4())
        conn.execute("UPDATE merchant_canonical SET method='manual', "
                     "canonical='Costco', merchant_id=%s", (ghost,))
        conn.execute("UPDATE transactions SET merchant_id=NULL")

        merchant_identity.resolve(conn, only_unresolved=False)

        self.assertEqual(_merchant_of(conn, tid), "Costco")
        self.assertIsNone(conn.execute(
            "SELECT 1 AS x FROM merchant_canonical WHERE merchant_id=%s",
            (ghost,)).fetchone())

    def test_transaction_pointing_at_a_missing_merchant_is_re_resolved(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = add_txn(conn, "2026-07-02", 11.0, "TRADER JOES #55",
                      merchant="Trader Joes")
        conn.execute("UPDATE transactions SET merchant_id=%s",
                     (str(uuid.uuid4()),))

        merchant_identity.resolve(conn, only_unresolved=False)

        self.assertIsNotNone(_merchant_of(conn, tid))


class ScopedResolve(unittest.TestCase):
    """The ingest hook resolves a handful of rows. It loads only the alias
    rows for THOSE rows' keys — a dozen new transactions have no business
    reading a household's whole alias table — and must still honour every
    layer the full pass honours for the keys it does read."""

    def test_a_scoped_resolve_still_honours_the_persons_own_name(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = add_txn(conn, "2026-07-05", 15.0, "SQ *BLUE BOTTLE",
                      merchant="SQ *BLUE BOTTLE")
        other = add_txn(conn, "2026-07-05", 25.0, "PEETS #12",
                        merchant="PEETS #12")
        conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES ('SQ *BLUE BOTTLE','Blue Bottle Coffee','manual')")

        merchant_identity.resolve(conn, txn_ids=[tid])

        self.assertEqual(_merchant_of(conn, tid), "Blue Bottle Coffee")
        # the row outside the scope was not touched
        self.assertIsNone(_merchant_of(conn, other))


class MergeChains(unittest.TestCase):
    def _chain(self, conn):
        """A merged into B merged into C — C is the only live merchant."""
        for n in ("A", "B", "C"):
            conn.execute("INSERT INTO merchants (name) VALUES (%s)", (n,))
        conn.execute("UPDATE merchants SET merged_into=%s WHERE name='A'",
                     (_mid(conn, "B"),))
        conn.execute("UPDATE merchants SET merged_into=%s WHERE name='B'",
                     (_mid(conn, "C"),))

    def test_alias_on_a_two_hop_merge_lands_on_the_survivor(self):
        conn = make_db()
        self.addCleanup(conn.close)
        self._chain(conn)
        a, c = _mid(conn, "A"), _mid(conn, "C")
        tid = add_txn(conn, "2026-07-03", 9.0, "RAW A", merchant="RAW A")
        conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "merchant_id) VALUES ('RAW A','A','manual',%s)", (a,))
        conn.execute("UPDATE transactions SET merchant_id=NULL")

        merchant_identity.resolve(conn, only_unresolved=False)

        # the row lands on the SURVIVOR, not on the merged-away merchant
        # two hops back (the manual branch then names it what the person
        # typed, so identity is the id, not the string)
        self.assertEqual(conn.execute(
            "SELECT merchant_id FROM transactions WHERE id=%s",
            (tid,)).fetchone()["merchant_id"], c)

    def test_plaid_entity_on_a_two_hop_merge_lands_on_the_survivor(self):
        conn = make_db()
        self.addCleanup(conn.close)
        self._chain(conn)
        conn.execute("UPDATE merchants SET plaid_entity_id='ENT-A' WHERE name='A'")
        got = merchant_identity._upsert_plaid_merchant(
            conn, {"entity_id": "ENT-A", "name": "A"}, {})
        self.assertEqual(got, _mid(conn, "C"))

    def test_a_merge_cycle_terminates(self):
        conn = make_db()
        self.addCleanup(conn.close)
        for n in ("A", "B"):
            conn.execute("INSERT INTO merchants (name) VALUES (%s)", (n,))
        a, b = _mid(conn, "A"), _mid(conn, "B")
        conn.execute("UPDATE merchants SET merged_into=%s WHERE id=%s", (b, a))
        conn.execute("UPDATE merchants SET merged_into=%s WHERE id=%s", (a, b))
        tid = add_txn(conn, "2026-07-04", 7.0, "RAW A", merchant="RAW A")
        conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "merchant_id) VALUES ('RAW A','A','manual',%s)", (a,))
        conn.execute("UPDATE transactions SET merchant_id=NULL")

        merchant_identity.resolve(conn, only_unresolved=False)

        self.assertIn(conn.execute(
            "SELECT merchant_id FROM transactions WHERE id=%s",
            (tid,)).fetchone()["merchant_id"], (a, b))


class PlaidEntityRace(unittest.TestCase):
    def test_two_creates_for_one_plaid_entity_share_the_merchant(self):
        conn = make_db()
        self.addCleanup(conn.close)
        first = merchant_identity._create(conn, "Delta", "plaid",
                                          entity="ENT-DELTA")
        # the loser of the race: it, too, found no merchant for the entity
        second = merchant_identity._create(conn, "Delta Air Lines", "plaid",
                                           entity="ENT-DELTA")
        self.assertEqual(first, second)
        self.assertEqual(conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE plaid_entity_id='ENT-DELTA'"
        ).fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()
