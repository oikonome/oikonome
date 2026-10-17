"""A payee the feed starts calling something new stays the same merchant.

An aggregator's merchant name is not stable: month to month the same card
line can come back as a longer or shorter name for the same business
("Northwind Hardware Su", then "Northwind Hardware Supply"). The resolver keys
on that name, so a new spelling is a new key with no alias, and minting a
merchant for it splits the payee in two — a bill attached to the merchant
then stops seeing its own charges.

Pending holds make this worse: the network prints the purchase's city and
country after the payee on the hold ("… RIVERTON USA") and the posted
charge drops them, so the only rows ever carrying the hold's line are the
holds that have already settled and been retired.

These tests pin the rules: the line a payee was filed under, a settled hold
included, carries its merchant to a renamed row when the line still reads
as the new name; a hold's line that extends a known posted line by a
location tail does too; a posted charge follows the hold it settled. And
the limits: a name the line does not spell, a line with two payees in its
history, and a person's own rename all keep their own answer."""

import unittest

from oikonome.engine import merchant_identity, merchant_split_repair

from .util import jsonb, make_db

HOLD = "NORTHWIND HARDWARE SU RIVERTON USA"
POSTED = "NORTHWIND HARDWARE SU"


class _Ledger:
    def __init__(self, conn):
        self.conn = conn

    def row(self, txn_id, date, amount, name, merchant_name, *, pending=0,
            raw=None):
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, pending, removed, raw)
               VALUES (%s,'card',%s,%s,%s,%s,'GENERAL_MERCHANDISE',%s,0,%s)""",
            (txn_id, date, amount, name, merchant_name, pending,
             jsonb(raw or {})))
        return txn_id

    def settle(self, hold_id, posted_id):
        """The feed retires the hold and links the posted charge to it."""
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          (hold_id,))
        self.conn.execute(
            "UPDATE transactions SET pending_transaction_id=%s WHERE id=%s",
            (hold_id, posted_id))

    def mid(self, txn_id):
        return self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["merchant_id"]

    def resolve(self):
        merchant_identity.resolve(self.conn)

    def month(self, n, *, name="Northwind Hardware Su"):
        """One month of a store account: the hold, then its posted charge."""
        h = self.row(f"h{n}", f"2025-0{n}-03", 140, HOLD, name, pending=1)
        self.resolve()
        p = self.row(f"p{n}", f"2025-0{n}-04", 140, POSTED, name)
        self.settle(h, p)
        self.resolve()
        return h, p

    def lantern(self):
        """A hold resolved to the feed's entity, then a posted charge under
        another line with a plain name and no entity."""
        ent = {"merchant_entity_id": "ent-lantern", "merchant_name": "Lantern"}
        h = self.row("j1", "2025-05-06", 73.10, "LANTERN.COM", "Lantern",
                     pending=1, raw=ent)
        self.resolve()
        p = self.row("j2", "2025-05-07", 73.10, "LANTERN JOBS", "Lantern Jobs")
        self.settle(h, p)
        self.resolve()
        return h, p


class RenamedFeedNameTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.l = _Ledger(self.conn)

    def _live_merchants(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE merged_into IS NULL"
        ).fetchone()["n"]

    # -- the shapes that must keep their merchant ---------------------------
    def test_a_renamed_hold_under_a_settled_holds_line_keeps_the_merchant(self):
        _h, p = self.l.month(5)
        self.l.month(6)
        new = self.l.row("h7", "2025-07-03", 140, HOLD,
                         "Northwind Hardware Supply", pending=1)
        self.l.resolve()
        self.assertEqual(self.l.mid(new), self.l.mid(p))
        self.assertEqual(self._live_merchants(), 1, "no twin was minted")

    def test_its_posted_charge_follows_it_once_it_settles(self):
        _h, p = self.l.month(5)
        self.l.row("h6", "2025-06-03", 140, HOLD, "Northwind Hardware Supply",
                   pending=1)
        self.l.resolve()
        p6 = self.l.row("p6", "2025-06-04", 140, POSTED, "Northwind Hardware Supply")
        self.l.settle("h6", p6)
        self.l.resolve()
        self.assertEqual(self.l.mid(p6), self.l.mid(p))

    def test_a_hold_extending_a_posted_line_by_its_location_uses_that_line(self):
        """No hold has ever settled under this line: the posted line it
        extends by "RIVERTON USA" is the evidence."""
        p = self.l.row("p1", "2025-05-04", 140, POSTED, "Northwind Hardware Su")
        self.l.resolve()
        named = self.l.row("h2", "2025-06-03", 140, HOLD,
                           "Northwind Hardware Supply", pending=1)
        bare = self.l.row("h3", "2025-06-05", 15, HOLD, None, pending=1)
        self.l.resolve()
        self.assertEqual(self.l.mid(named), self.l.mid(p))
        self.assertEqual(self.l.mid(bare), self.l.mid(p))

    def test_a_truncated_name_growing_its_missing_words_keeps_the_merchant(self):
        for i, d in enumerate(("2025-03-02", "2025-04-02")):
            self.l.row(f"b{i}", d, 35, "BRAMBLE OAK BAKEH", "Bramble Oak Bakeh")
        self.l.resolve()
        new = self.l.row("b9", "2025-05-02", 45, "BRAMBLE OAK BAKEH",
                         "Bramble Oak Bakehouse Cafe", pending=1)
        self.l.resolve()
        self.assertEqual(self.l.mid(new), self.l.mid("b0"))

    def test_a_name_dropping_a_word_the_line_still_spells_keeps_the_merchant(self):
        line = "Copperline Bike Cyc RIVERTON USA"
        self.l.row("k1", "2025-05-08", 88.40, line, "Copperline Bike Cyc", pending=1)
        self.l.resolve()
        p = self.l.row("k2", "2025-05-09", 88.40, "COPPERLINE BIKE CYC",
                       "Copperline Bike Cyc")
        self.l.settle("k1", p)
        self.l.resolve()
        new = self.l.row("k3", "2025-06-08", 51.25, line, "Copperline Cycles",
                         pending=1)
        self.l.resolve()
        self.assertEqual(self.l.mid(new), self.l.mid(p))

    def test_a_posted_charge_follows_the_entity_its_hold_was_resolved_to(self):
        h, p = self.l.lantern()
        self.assertEqual(self.l.mid(p), self.l.mid(h))

    # -- the limits ----------------------------------------------------------
    def test_a_name_the_line_does_not_spell_gets_its_own_merchant(self):
        """A line answers for a new name only when it NAMES the payee it
        has carried. Here it never did: the feed called the earlier charge
        something the line does not spell either, so the line is a
        witness to nothing, and the second name keeps its own merchant.
        (A line that does name its earlier payee outranks a name it does
        not contain — the overrule's tests.)"""
        _h, p = self.l.month(5, name="Granite Supply Co")
        other = self.l.row("x1", "2025-06-03", 40, HOLD, "Corner Market",
                           pending=1)
        self.l.resolve()
        self.assertNotEqual(self.l.mid(other), self.l.mid(p))

    def test_a_line_with_two_payees_in_its_history_decides_nothing(self):
        self.l.row("c1", "2025-04-01", 10, "NORTHWIND GARAGE", "Northwind Garage")
        self.l.row("c2", "2025-04-02", 10, "NORTHWIND GARAGE",
                   "Northwind Garage Parts")
        self.l.resolve()
        before = {self.l.mid("c1"), self.l.mid("c2")}
        new = self.l.row("c3", "2025-05-01", 10, "NORTHWIND GARAGE",
                         "Northwind Garage Group")
        self.l.resolve()
        self.assertNotIn(self.l.mid(new), before)

    def test_a_hold_link_between_two_unrelated_payees_is_not_followed(self):
        h = self.l.row("q1", "2025-05-06", 6.50, "JUNIPER COFFEE", "Juniper Coffee",
                       pending=1)
        self.l.resolve()
        p = self.l.row("q2", "2025-05-07", 6.50, "MARLOW BAKERY", "Marlow Bakery")
        self.l.settle(h, p)
        self.l.resolve()
        self.assertNotEqual(self.l.mid(p), self.l.mid(h))
        self.assertFalse([m for m in merchant_split_repair.repair(
            self.conn, apply=False)["merges"]
            if m.get("kind") == "renamed-feed-name"])

    def test_a_line_without_a_country_tail_is_not_cut(self):
        p = self.l.row("p1", "2025-05-04", 140, POSTED, "Northwind Hardware Su")
        self.l.resolve()
        new = self.l.row("h2", "2025-06-03", 140, POSTED + " RIVERTON",
                         "Northwind Hardware Supply", pending=1)
        self.l.resolve()
        self.assertNotEqual(self.l.mid(new), self.l.mid(p))

    def test_a_persons_rename_of_the_new_name_wins(self):
        _h, p = self.l.month(5)
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, as_of)"
            " VALUES ('Northwind Hardware Supply', 'Northwind Tool Rental',"
            " 'manual', now())")
        new = self.l.row("h6", "2025-06-03", 140, HOLD,
                         "Northwind Hardware Supply", pending=1)
        self.l.resolve()
        name = self.conn.execute("SELECT name FROM merchants WHERE id=%s",
                                 (self.l.mid(new),)).fetchone()["name"]
        self.assertEqual(name, "Northwind Tool Rental")
        self.assertNotEqual(self.l.mid(new), self.l.mid(p))


class RenamedFeedTwinRepairTests(unittest.TestCase):
    """A twin the resolver minted before it knew better is merged back."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.l = _Ledger(self.conn)

    def _twin(self, txn_id, name):
        """Re-file a row under a freshly minted merchant, the way the
        resolver used to for a renamed feed name."""
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source, created_at)"
            " VALUES (%s, 'layer1', now() + interval '1 minute') RETURNING id",
            (name,)).fetchone()["id"]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (mid, txn_id))
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method,"
            " as_of, merchant_id) VALUES (%s,%s,'layer1',now(),%s)"
            " ON CONFLICT (tenant_id, raw_merchant) DO UPDATE"
            " SET merchant_id = EXCLUDED.merchant_id", (name, name, mid))
        return mid

    def _twins(self, got):
        return [m for m in got["merges"] if m.get("kind") == "renamed-feed-name"]

    def _hardware(self):
        _h, p = self.l.month(5)
        self.l.month(6)
        new = self.l.row("h7", "2025-07-03", 140, HOLD,
                         "Northwind Hardware Supply", pending=1)
        self.l.resolve()
        return self.l.mid(p), self._twin(new, "Northwind Hardware Supply"), new

    def test_a_dry_run_names_the_merge_and_writes_nothing(self):
        _old, twin, new = self._hardware()
        recs = self._twins(merchant_split_repair.repair(self.conn, apply=False))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["merge"], ["Northwind Hardware Supply"])
        self.assertEqual(self.l.mid(new), twin)

    def test_apply_moves_the_rows_and_merges_the_twin(self):
        old, twin, new = self._hardware()
        merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(self.l.mid(new), old)
        gone = self.conn.execute("SELECT merged_into FROM merchants WHERE id=%s",
                                 (twin,)).fetchone()
        self.assertEqual(gone["merged_into"], old)
        self.assertFalse(self._twins(
            merchant_split_repair.repair(self.conn, apply=False)))
        # and the nightly re-resolve does not mint it again
        self.l.resolve()
        self.assertEqual(self.l.mid(new), old)

    def test_a_posted_twin_of_its_holds_entity_merges_into_it(self):
        h, p = self.l.lantern()
        twin = self._twin(p, "Lantern Jobs")
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method,"
            " as_of, merchant_id) VALUES ('LANTERN JOBS','Lantern Jobs',"
            " 'layer1', now(), %s)", (twin,))
        merchant_split_repair.repair(self.conn, apply=True)
        self.assertEqual(self.l.mid(p), self.l.mid(h))
        gone = self.conn.execute("SELECT merged_into FROM merchants WHERE id=%s",
                                 (twin,)).fetchone()
        self.assertEqual(gone["merged_into"], self.l.mid(h))

    def test_a_merchant_with_ordinary_posted_rows_is_never_a_twin(self):
        self.l.month(5)
        r = self.l.row("z1", "2025-06-05", 140, POSTED, "Northwind Hardware Supply")
        self.l.resolve()
        twin = self._twin(r, "Northwind Hardware Supply")
        self.assertFalse(self._twins(
            merchant_split_repair.repair(self.conn, apply=False)))
        self.assertEqual(self.l.mid(r), twin)

    def _rule(self, mid, cat, source, *, upper=False):
        """A category rule under the merchant's name. `upper` stores it in
        the feed's capitals: a rule reaches a merchant by its name in any
        case, but by an alias only when the strings are equal, so such a
        rule hangs on the name alone."""
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, source)"
            " SELECT CASE WHEN %s THEN upper(name) ELSE name END, %s, %s"
            "   FROM merchants WHERE id=%s", (upper, cat, source, mid))

    def _cat(self, txn_id):
        return self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["category_primary"]

    def test_the_twins_category_rule_comes_with_it(self):
        """The person taught the twin a category; the survivor only carries
        a machine guess. After the merge the person's rule files the
        survivor's charges, the next one included."""
        from oikonome.engine import llm_categorize
        old, twin, new = self._hardware()
        self._rule(twin, "HOME_IMPROVEMENT", "user", upper=True)
        self._rule(old, "GENERAL_SERVICES", "llm")
        merchant_split_repair.repair(self.conn, apply=True)
        nxt = self.l.row("p8", "2025-08-04", 140, POSTED, "Northwind Hardware Su")
        self.l.resolve()
        llm_categorize.apply(self.conn)
        self.assertEqual(self.l.mid(nxt), old)
        self.assertEqual(self._cat(new), "HOME_IMPROVEMENT")
        self.assertEqual(self._cat(nxt), "HOME_IMPROVEMENT")

    def test_twins_the_person_taught_different_categories_are_not_merged(self):
        old, twin, new = self._hardware()
        self._rule(twin, "HOME_IMPROVEMENT", "user")
        self._rule(old, "ENTERTAINMENT", "user")
        got = merchant_split_repair.repair(self.conn, apply=True)
        self.assertFalse(self._twins(got))
        self.assertIn("rules-disagree", [r["reason"] for r in got["refused"]])
        self.assertEqual(self.l.mid(new), twin)

    def test_the_sweep_reads_the_ledger_once_for_every_candidate(self):
        """The nightly sweep judges every candidate on one grouped read of
        the ledger's evidence, not one whole-ledger read per candidate: it
        runs inside the identity lock every sync's resolve waits on."""
        from unittest import mock
        old, twin, new = self._hardware()
        h, p = self.l.lantern()
        lantern_twin = self._twin(p, "Lantern Jobs")
        real = merchant_identity._descriptor_evidence
        with mock.patch.object(merchant_identity, "_descriptor_evidence",
                               side_effect=real) as ev, \
                mock.patch.object(merchant_identity, "_base_evidence",
                                  side_effect=merchant_identity._base_evidence
                                  ) as base:
            recs = self._twins(merchant_split_repair.repair(
                self.conn, apply=False))
        self.assertEqual(sorted(r["merge"][0] for r in recs),
                         ["Lantern Jobs", "Northwind Hardware Supply"])
        self.assertEqual(ev.call_count, 1)
        self.assertEqual(base.call_count, 1)
        self.assertEqual(self.l.mid(new), twin)
        self.assertEqual(self.l.mid(p), lantern_twin)
        self.assertNotEqual(old, twin)


if __name__ == "__main__":
    unittest.main()
