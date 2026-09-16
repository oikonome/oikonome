"""A payee split across two merchants converges back onto one.

Uneven enrichment keys one payee two ways — the tidy merchant string on the
rows an aggregator resolved, the raw bank descriptor on the rest — and the
household is left with two merchants for one payee. The resolver no longer
mints such a pair, but it cannot heal one that already exists: descriptor
adoption only runs where no alias exists, and a resolved row is never
revisited. These tests protect the repair sweep that retracts the split,
and the refusals that keep it from fusing payees that merely share a
string.
"""

import unittest

from oikonome.engine import (merchant_dedup, merchant_identity,
                             merchant_split_repair)

from .util import add_txn, as_date, jsonb, make_db

DESCRIPTOR = "EXAMPLE TELECOM SPRINGFIELD"


class SplitMerchantRepairTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    # ---- fixtures -------------------------------------------------------

    def _unenriched(self, date, amount, name, txn_id):
        """A row the aggregator gave no merchant name — what a refund and a
        late-arriving charge usually look like."""
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES (%s,'card',%s,%s,%s,NULL,'GENERAL_MERCHANDISE',0,0,%s)""",
            (txn_id, as_date(date), amount, name, jsonb({})))
        return txn_id

    def _merchant_of(self, txn_id):
        return self.conn.execute(
            "SELECT m.id, m.name, m.name_source FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id=%s",
            (txn_id,)).fetchone()

    def _split_off(self, *txn_ids, descriptor=DESCRIPTOR):
        """Key these rows on the bank descriptor and give that key its own
        merchant — what the resolver did before it learned to bridge a
        descriptor, and the state every already-split household is in.

        The alias row is the whole reason the resolver cannot heal this
        itself: descriptor adoption runs only where no alias exists, and a
        row that already has a merchant is never revisited."""
        name = merchant_dedup.canonical_merchant(
            descriptor, merchant_dedup.cities_for(self.conn))
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
            "RETURNING id", (name,)).fetchone()["id"]
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "                                as_of, merchant_id) "
            "VALUES (%s,%s,'layer1',now(),%s) "
            "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET "
            "canonical=EXCLUDED.canonical, merchant_id=EXCLUDED.merchant_id",
            (descriptor, name, mid))
        self.conn.execute("UPDATE transactions SET merchant_id=%s "
                          " WHERE id = ANY(%s)", (mid, list(txn_ids)))
        return mid

    def _split_pair(self, *, enriched_first=True):
        """Two rows of ONE payee that resolved to two merchants.

        Which half reached the ledger first decides which merchant is the
        older row and which name the descriptor group meets first, so both
        orders are worth protecting. Unenriched-first still splits under
        the current resolver — an enriched group is keyed on the
        aggregator's string and never consults the descriptor — while
        enriched-first is the shape only a pre-repair ledger carries."""
        if enriched_first:
            add_txn(self.conn, "2026-07-01", 25.00, DESCRIPTOR,
                    merchant="example telecom", txn_id="chg")
            merchant_identity.resolve(self.conn)
            self._unenriched("2026-07-02", -25.00, DESCRIPTOR, "ref")
            self._split_off("ref")
        else:
            self._unenriched("2026-07-01", -25.00, DESCRIPTOR, "ref")
            merchant_identity.resolve(self.conn)
            add_txn(self.conn, "2026-07-02", 25.00, DESCRIPTOR,
                    merchant="example telecom", txn_id="chg")
            merchant_identity.resolve(self.conn)
        a, b = self._merchant_of("chg"), self._merchant_of("ref")
        self.assertNotEqual(a["id"], b["id"],
                            "fixture did not produce a split")
        return a, b

    # ---- the repair -----------------------------------------------------

    def test_a_split_payee_converges_onto_one_merchant(self):
        self._split_pair()
        merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(self._merchant_of("ref")["id"],
                         self._merchant_of("chg")["id"],
                         "the two halves of one payee still show as two")

    def test_convergence_does_not_depend_on_which_half_arrived_first(self):
        """The unenriched half arriving first is the ordinary case — a
        refund, a late row — so the repair must not be a function of the
        order the ledger happened to fill in."""
        self._split_pair(enriched_first=False)
        merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(self._merchant_of("ref")["id"],
                         self._merchant_of("chg")["id"])

    def test_the_survivor_is_the_merchant_with_the_stronger_name(self):
        """Authority, not row count: the aggregator-named merchant carries
        the entity id and the logo, so it is the name the history joins."""
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES ('p1','card',%s,25.00,%s,'Example Telecom',
                       'GENERAL_MERCHANDISE',0,0,%s)""",
            (as_date("2026-07-01"), DESCRIPTOR,
             jsonb({"merchant_entity_id": "ENT-EXAMPLETELECOM",
                    "merchant_name": "Example Telecom",
                    "counterparties": [
                        {"name": "Example Telecom", "type": "merchant",
                         "entity_id": "ENT-EXAMPLETELECOM",
                         "confidence_level": "VERY_HIGH"}]})))
        merchant_identity.resolve(self.conn)
        # two unenriched rows, so the layer-1 twin has the MORE history
        self._unenriched("2026-07-02", -25.00, DESCRIPTOR, "u1")
        self._unenriched("2026-07-03", -5.00, DESCRIPTOR, "u2")
        self._split_off("u1", "u2")
        self.assertNotEqual(self._merchant_of("u1")["id"],
                            self._merchant_of("p1")["id"])

        merchant_split_repair.repair(self.conn, apply=True)
        got = self._merchant_of("u1")
        self.assertEqual(got["id"], self._merchant_of("p1")["id"])
        self.assertEqual(got["name_source"], "plaid",
                         "the merge kept the weaker of the two names")

    def test_running_it_twice_changes_nothing(self):
        self._split_pair()
        merchant_split_repair.repair(self.conn, apply=True)
        merchants = self.conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE merged_into IS NULL"
        ).fetchone()["n"]
        journal = self.conn.execute(
            "SELECT count(*) AS n FROM merchant_renames").fetchone()["n"]

        again = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(again["merges"], [])
        self.assertEqual(
            self.conn.execute("SELECT count(*) AS n FROM merchants "
                              "WHERE merged_into IS NULL").fetchone()["n"],
            merchants)
        self.assertEqual(
            self.conn.execute("SELECT count(*) AS n FROM merchant_renames"
                              ).fetchone()["n"], journal,
            "a second run journalled a change it did not need to make")

    def test_a_dry_run_reports_without_writing(self):
        charge, refund = self._split_pair()
        p = merchant_split_repair.repair(self.conn)
        self.assertEqual(len(p["merges"]), 1)
        self.assertEqual(p["merges"][0]["descriptor"], DESCRIPTOR)
        self.assertEqual(p["merges"][0]["survivor"], charge["name"])
        self.assertEqual(p["merges"][0]["merge"], [refund["name"]])
        self.assertFalse(p["applied"])
        self.assertNotEqual(self._merchant_of("ref")["id"],
                            self._merchant_of("chg")["id"],
                            "the dry run moved rows")
        self.assertEqual(
            self.conn.execute("SELECT count(*) AS n FROM merchant_renames"
                              ).fetchone()["n"], 0)

    def test_a_merge_can_be_undone(self):
        """The repair is journalled exactly like a person's rename, so it
        is reversible by the same door — and the household is not left with
        a correction it cannot take back."""
        charge, refund = self._split_pair()
        got = merchant_split_repair.repair(self.conn, apply=True)
        changes = got["merges"][0]["change_ids"]
        self.assertTrue(changes)
        for cid in reversed(changes):
            merchant_dedup.undo(self.conn, cid)
        self.assertNotEqual(self._merchant_of("ref")["id"],
                            self._merchant_of("chg")["id"],
                            "undoing the merge did not restore the split")
        self.assertEqual(self._merchant_of("chg")["name"], charge["name"])
        self.assertEqual(self._merchant_of("ref")["name"], refund["name"])

    # ---- the refusals ---------------------------------------------------

    def test_a_generic_descriptor_never_collapses_two_payees(self):
        """Some banks write a constant where the payee belongs. The rows
        under it are unrelated purchases, and merging them would fuse a
        household's whole card into one merchant — an act nothing in the
        data could undo."""
        generic = "POS DEBIT PURCHASE"
        add_txn(self.conn, "2026-07-01", 12.0, generic,
                merchant="corner market", txn_id="g1")
        add_txn(self.conn, "2026-07-02", 30.0, generic,
                merchant="example hardware", txn_id="g2")
        merchant_identity.resolve(self.conn)
        first, second = self._merchant_of("g1"), self._merchant_of("g2")
        self.assertNotEqual(first["id"], second["id"],
                            "fixture should give two payees")

        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(got["merges"], [])
        self.assertEqual([r["reason"] for r in got["refused"]],
                         ["descriptor-does-not-name-them-all"])
        self.assertNotEqual(self._merchant_of("g1")["id"],
                            self._merchant_of("g2")["id"])

    def test_a_processor_prefix_shared_by_two_payees_is_refused(self):
        """A payment terminal's prefix is in front of every payee it
        serves, so it names none of them."""
        add_txn(self.conn, "2026-07-01", 12.0, "SQ *", merchant="corner market",
                txn_id="q1")
        add_txn(self.conn, "2026-07-02", 30.0, "SQ *",
                merchant="example roasters", txn_id="q2")
        merchant_identity.resolve(self.conn)
        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(got["merges"], [])
        self.assertNotEqual(self._merchant_of("q1")["id"],
                            self._merchant_of("q2")["id"])

    def test_a_hand_named_merchant_is_left_alone(self):
        """A person's answer outranks the sweep in both directions: it is
        never merged away, and nothing is merged into it on the sweep's own
        initiative."""
        charge, refund = self._split_pair()
        merchant_dedup.rename(self.conn, refund["name"], "Telecom, The Old One")
        renamed = self._merchant_of("ref")
        self.assertEqual(renamed["name_source"], "manual")

        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(got["merges"], [])
        self.assertEqual(self._merchant_of("ref")["id"], renamed["id"])
        self.assertEqual(self._merchant_of("ref")["name"],
                         "Telecom, The Old One")
        self.assertEqual(self._merchant_of("chg")["id"], charge["id"])

    def test_an_undone_merge_is_not_made_again(self):
        """Reversing the sweep has to mean something: a person who undid a
        merge must not find it back the next night."""
        self._split_pair()
        got = merchant_split_repair.repair(self.conn, apply=True)
        for cid in reversed(got["merges"][0]["change_ids"]):
            merchant_dedup.undo(self.conn, cid)
        split_again = self._merchant_of("ref")["id"]

        merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(self._merchant_of("ref")["id"], split_again)
        self.assertNotEqual(self._merchant_of("ref")["id"],
                            self._merchant_of("chg")["id"])

    def test_an_outlet_is_not_merged_into_its_brand(self):
        """A chain's fuel arm is its own merchant on purpose — its own
        prices, its own category — and it shares the brand's descriptor by
        design."""
        add_txn(self.conn, "2026-07-01", 40.0, "EXAMPLE WAREHOUSE GAS",
                merchant="example warehouse", txn_id="b1")
        merchant_identity.resolve(self.conn)
        brand = self._merchant_of("b1")
        outlet = self.conn.execute(
            "INSERT INTO merchants (name, name_source, parent_id) "
            "VALUES ('Example Warehouse Gas','layer1',%s) RETURNING id",
            (brand["id"],)).fetchone()["id"]
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,merchant_outlet,merchant_id,category_primary,
                   pending,removed,raw)
               VALUES ('b2','card',%s,38.0,'EXAMPLE WAREHOUSE GAS',
                       'example warehouse','Example Warehouse Gas',%s,
                       'TRANSPORTATION',0,0,%s)""",
            (as_date("2026-07-02"), outlet, jsonb({})))

        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(got["merges"], [])
        self.assertEqual([r["reason"] for r in got["refused"]],
                         ["outlet-of-a-brand"])
        self.assertEqual(self._merchant_of("b2")["id"], outlet)

    def test_a_counterparty_rail_keeps_its_people_apart(self):
        """Two people paid over one rail share a descriptor without being
        one payee, and the name the app built for each IS the identity."""
        for n, (txn, who) in enumerate((("z1", "Casey Example"),
                                        ("z2", "Robin Example")), start=1):
            self.conn.execute(
                """INSERT INTO transactions (id,account_id,date,amount,name,
                       merchant_name,category_primary,pending,removed,raw)
                   VALUES (%s,'card',%s,%s,'PAYMENT RAIL TRANSFER',%s,
                           'TRANSFER_OUT',0,0,%s)""",
                (txn, as_date(f"2026-07-0{n}"), 20.0 * n,
                 f"Payment Rail — {who}", jsonb({})))
        merchant_identity.resolve(self.conn)
        first, second = self._merchant_of("z1"), self._merchant_of("z2")
        self.assertNotEqual(first["id"], second["id"])

        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(got["merges"], [])
        self.assertEqual(self._merchant_of("z1")["id"], first["id"])
        self.assertEqual(self._merchant_of("z2")["id"], second["id"])

    def test_the_work_one_run_does_is_bounded(self):
        """A first run on a long-neglected ledger must not hold the
        household's identity lock for as long as the backlog is."""
        for n in range(3):
            d = f"EXAMPLE STORE {n} PORTLAND USA"
            add_txn(self.conn, "2026-07-01", 10.0 + n, d,
                    merchant=f"example store {n}", txn_id=f"e{n}")
            merchant_identity.resolve(self.conn)
            self._unenriched("2026-07-02", -1.0 - n, d, f"u{n}")
            self._split_off(f"u{n}", descriptor=d)

        got = merchant_split_repair.repair(self.conn, apply=True, limit=1)
        self.assertEqual(len(got["merges"]), 1)
        self.assertEqual(got["deferred"], 2)
        # and the rest converge on later runs
        for _ in range(3):
            merchant_split_repair.repair(self.conn, apply=True, limit=1)
        for n in range(3):
            self.assertEqual(self._merchant_of(f"u{n}")["id"],
                             self._merchant_of(f"e{n}")["id"])

    def test_a_loser_with_rows_under_another_descriptor_is_left_alone(self):
        """The evidence for a merge is one shared descriptor, but a rename
        moves every row the loser displays. A loser that is also the only
        merchant under a second descriptor is a payee of its own, and the
        sweep must not carry those rows along on no evidence."""
        charge, refund = self._split_pair()
        self._unenriched("2026-07-03", 9.99, "EXAMPLE TELECOM PLAN", "plan")
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id='plan'",
                          (refund["id"],))

        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(got["merges"], [])
        self.assertIn("loser-rows-under-other-descriptors",
                      [r["reason"] for r in got["refused"]])
        self.assertEqual(self._merchant_of("ref")["id"], refund["id"])
        self.assertEqual(self._merchant_of("plan")["id"], refund["id"])

    def test_a_hand_named_merchant_stops_the_whole_group(self):
        """Three merchants under one descriptor, one of them hand-named: the
        two others are not merged around the person's answer either."""
        charge, refund = self._split_pair()
        self._unenriched("2026-07-04", -5.00, DESCRIPTOR, "ref2")
        third = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Example Telecom Ny', 'layer1') RETURNING id").fetchone()["id"]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id='ref2'",
                          (third,))
        # hand-named in place (a rename would re-point every row under the
        # shared descriptor, which is the very thing this fixture keeps apart)
        self.conn.execute(
            "UPDATE merchants SET name='Telecom, The Old One', "
            "name_source='manual' WHERE id=%s", (refund["id"],))

        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(got["merges"], [])
        self.assertIn("hand-named", [r["reason"] for r in got["refused"]])
        self.assertEqual(self._merchant_of("ref2")["id"], third)
        self.assertEqual(self._merchant_of("chg")["id"], charge["id"])

    def test_a_rename_takes_the_household_lock_before_its_row_locks(self):
        """The nightly repair holds the household identity lock while it
        renames; a rename from the API that locked its rows first and then
        waited for that lock would deadlock against it. The lock therefore
        comes first in every rename."""
        charge, refund = self._split_pair()
        seen = []
        real = self.conn.execute

        def spy(sql, *a, **k):
            seen.append(sql if isinstance(sql, str) else str(sql))
            return real(sql, *a, **k)

        self.conn.execute = spy
        try:
            with self.conn.transaction():
                merchant_dedup.rename(self.conn, refund["name"], "Renamed Telecom")
        finally:
            del self.conn.execute
        lock = next(i for i, q in enumerate(seen) if "pg_advisory_xact_lock" in q)
        first_row_lock = next(i for i, q in enumerate(seen) if "FOR UPDATE" in q)
        self.assertLess(lock, first_row_lock)


if __name__ == "__main__":
    unittest.main()
