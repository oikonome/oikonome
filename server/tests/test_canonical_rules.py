"""Category rules key on the CANONICAL merchant, not the raw
descriptor. A correction (or cached category) on any variant of a merchant
applies to every variant, past and future; conflicting rules for one
canonical are left split; the flow-guard stays intact.

Remainder (migration 044 + canonical-keyed selection): pre-existing llm
rules cached under raw descriptors are re-keyed/merged to their canonical —
unanimity-gated, conflicted canonicals left split — and pending_merchants
groups by canonical so NEW rules are stored under the canonical key."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from oikonome.db import migrate
from oikonome.engine import llm_categorize
from oikonome.web import data

from .test_llm_categorize import ENV, add_raw_txn, cache, reply_transport
from .util import make_db

REKEY_SQL = (Path(migrate.__file__).parent / "migrations"
             / "044_llm_rule_canonical_rekey.sql").read_text()


def canon(conn, raw, canonical):
    conn.execute(
        "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
        "VALUES (%s,%s,'llm') ON CONFLICT (tenant_id, raw_merchant) "
        "DO UPDATE SET canonical=EXCLUDED.canonical", (raw, canonical))


def cat(conn, tid):
    return conn.execute(
        "SELECT category_primary FROM transactions WHERE id=%s",
        (tid,)).fetchone()["category_primary"]


class CanonicalRuleTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_rule_under_one_variant_applies_to_all_variants(self):
        # the 3 descriptors collapse to one canonical merchant
        for raw in ("NORTHWIND COFFEE", "SQ *NRTHWND 4SPRINGFIELD NV",
                    "NRTHWND SPR POS"):
            canon(self.conn, raw, "Northwind Coffee")
        # a cached (llm) rule exists under only ONE variant…
        cache(self.conn, "NORTHWIND COFFEE", "ENTERTAINMENT")
        # …and generic-bucket transactions arrive under the OTHER variants
        add_raw_txn(self.conn, "a", "2025-07-01", 10, "SQ *NRTHWND 4SPRINGFIELD NV",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category": {"primary": "GENERAL_MERCHANDISE"}})
        add_raw_txn(self.conn, "b", "2025-07-02", 12, "NRTHWND SPR POS",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "b"), "ENTERTAINMENT")

    def test_conflicting_rules_for_one_canonical_stay_split(self):
        canon(self.conn, "SKYPORT CAFE GATE B", "Skyport Cafe")
        canon(self.conn, "SKYPORT CAFE DELI", "Skyport Cafe")
        cache(self.conn, "SKYPORT CAFE GATE B", "TRAVEL")
        cache(self.conn, "SKYPORT CAFE DELI", "FOOD_AND_DRINK")
        add_raw_txn(self.conn, "a", "2025-07-01", 10, "SKYPORT CAFE GATE B",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        llm_categorize.apply(self.conn)
        # rules disagree for the canonical → left split (generic unchanged)
        self.assertEqual(cat(self.conn, "a"), "OTHER")

    def test_user_correction_on_one_variant_covers_all_incl_sharp(self):
        for raw in ("NORTHWIND COFFEE", "SQ *NRTHWND SPR"):
            canon(self.conn, raw, "Northwind Coffee")
        # a SHARP wrong aggregator category on the other variant
        add_raw_txn(self.conn, "sharp", "2025-07-01", 10, "SQ *NRTHWND SPR",
                    primary="PERSONAL_CARE",
                    raw={"personal_finance_category": {"primary": "PERSONAL_CARE"}})
        # user corrects a DIFFERENT variant
        fix = add_raw_txn(self.conn, "fix", "2025-07-02", 12, "NORTHWIND COFFEE",
                          primary="OTHER",
                          raw={"personal_finance_category": {"primary": "OTHER"}})
        data.set_category(self.conn, fix, "ENTERTAINMENT", scope="all")
        # the sharp row on the OTHER descriptor is repaired merchant-wide
        self.assertEqual(cat(self.conn, "sharp"), "ENTERTAINMENT")
        # the rule was stored under the CANONICAL, not the raw descriptor
        rule = self.conn.execute(
            "SELECT merchant FROM merchant_categories WHERE source='user'"
        ).fetchone()
        self.assertEqual(rule["merchant"], "Northwind Coffee")

    def test_flow_guard_intact(self):
        for raw in ("ACME BANK XFER", "ACME TRANSFER"):
            canon(self.conn, raw, "Acme Transfer")
        cache(self.conn, "ACME BANK XFER", "ENTERTAINMENT")   # bogus rule
        # a flow row (spend-excluded) under a sibling variant
        add_raw_txn(self.conn, "flow", "2025-07-01", 500, "ACME TRANSFER",
                    primary="TRANSFER_OUT",
                    raw={"personal_finance_category": {"primary": "TRANSFER_OUT"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "flow"), "TRANSFER_OUT")

    def test_undeduped_merchant_unaffected(self):
        # no canonical mapping → each string is its own canonical, so a rule
        # under one string must NOT leak to an unrelated string
        cache(self.conn, "BODEGA LUZ", "FOOD_AND_DRINK")
        add_raw_txn(self.conn, "other", "2025-07-01", 10, "TOTALLY UNRELATED",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "other"), "OTHER")


class ScopedApplyTests(unittest.TestCase):
    """apply(canon=…) limits the sweep to one canonical merchant — the
    interactive correction path (set_category / set_merchant_category) must
    not pay for a full-table sweep, which on a large ledger takes minutes."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _two_merchants(self):
        canon(self.conn, "COFFEE HUT #1", "Coffee Hut")
        canon(self.conn, "COFFEE HUT POS", "Coffee Hut")
        cache(self.conn, "COFFEE HUT #1", "FOOD_AND_DRINK")
        cache(self.conn, "TOY WORLD", "ENTERTAINMENT")
        add_raw_txn(self.conn, "hut", "2025-07-01", 8, "COFFEE HUT POS",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        add_raw_txn(self.conn, "toy", "2025-07-02", 30, "TOY WORLD",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})

    def test_scoped_apply_touches_only_the_given_canonical(self):
        self._two_merchants()
        llm_categorize.apply(self.conn, canon="Coffee Hut")
        self.assertEqual(cat(self.conn, "hut"), "FOOD_AND_DRINK")
        self.assertEqual(cat(self.conn, "toy"), "OTHER")   # out of scope

    def test_full_apply_still_sweeps_everything(self):
        self._two_merchants()
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "hut"), "FOOD_AND_DRINK")
        self.assertEqual(cat(self.conn, "toy"), "ENTERTAINMENT")

    def test_scoped_apply_covers_every_variant_of_the_canonical(self):
        for raw in ("MAPLE SPRINGS", "MAPLE SPRINGS POS", "SQ *MAPLE SPR"):
            canon(self.conn, raw, "Maple Springs")
        cache(self.conn, "MAPLE SPRINGS", "ENTERTAINMENT")
        add_raw_txn(self.conn, "v1", "2025-07-01", 5, "MAPLE SPRINGS POS",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        add_raw_txn(self.conn, "v2", "2025-07-02", 6, "SQ *MAPLE SPR",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_MERCHANDISE"}})
        llm_categorize.apply(self.conn, canon="Maple Springs")
        self.assertEqual(cat(self.conn, "v1"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "v2"), "ENTERTAINMENT")

    def test_scoped_apply_on_undeduped_merchant(self):
        # an un-deduped merchant is its own canonical
        cache(self.conn, "BODEGA LUZ", "FOOD_AND_DRINK")
        add_raw_txn(self.conn, "bod", "2025-07-01", 4, "BODEGA LUZ",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        llm_categorize.apply(self.conn, canon="BODEGA LUZ")
        self.assertEqual(cat(self.conn, "bod"), "FOOD_AND_DRINK")

    def test_set_category_still_propagates_merchant_wide(self):
        # the interactive door now passes canon= — behavior must be unchanged
        canon(self.conn, "CAROUSEL LLC", "Carousel")
        canon(self.conn, "SQ *CAROUSEL", "Carousel")
        add_raw_txn(self.conn, "c1", "2025-07-01", 3, "CAROUSEL LLC",
                    primary="PERSONAL_CARE",
                    raw={"personal_finance_category":
                         {"primary": "PERSONAL_CARE"}})
        add_raw_txn(self.conn, "c2", "2025-07-02", 3, "SQ *CAROUSEL",
                    primary="PERSONAL_CARE",
                    raw={"personal_finance_category":
                         {"primary": "PERSONAL_CARE"}})
        data.set_category(self.conn, "c1", "ENTERTAINMENT", scope="all")
        # c1 carries the override; c2 followed via the merchant-wide rule
        self.assertEqual(cat(self.conn, "c2"), "ENTERTAINMENT")


def rule(conn, merchant, category, source="llm", disabled=False):
    conn.execute(
        "INSERT INTO merchant_categories "
        "(merchant, category_primary, source, disabled) "
        "VALUES (%s,%s,%s,%s)", (merchant, category, source, disabled))


def rules(conn):
    return {r["merchant"]: (r["category_primary"], r["source"], r["disabled"])
            for r in conn.execute(
                "SELECT merchant, category_primary, source, disabled "
                "FROM merchant_categories")}


class RekeyMigrationTests(unittest.TestCase):
    """Migration 044: llm rules cached under raw descriptors are merged to
    one canonical row — only when the whole group is unanimous (category AND
    disabled flag); a conflicted canonical is left entirely split, and user
    or seed rows are never deleted."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _migrate(self):
        self.conn.execute(REKEY_SQL)

    def test_agreeing_raw_rules_merge_to_one_canonical_row(self):
        canon(self.conn, "SQ *POINTE D'AMOUR CARO", "Pointe Amour Caro")
        canon(self.conn, "NYX POINTE AMOUR CARO", "Pointe Amour Caro")
        rule(self.conn, "SQ *POINTE D'AMOUR CARO", "ENTERTAINMENT")
        rule(self.conn, "NYX POINTE AMOUR CARO", "ENTERTAINMENT")
        self._migrate()
        self.assertEqual(rules(self.conn), {
            "Pointe Amour Caro": ("ENTERTAINMENT", "llm", False)})

    def test_single_raw_variant_is_rekeyed(self):
        canon(self.conn, "SQ *NRTHWND SPR", "Northwind Coffee")
        rule(self.conn, "SQ *NRTHWND SPR", "ENTERTAINMENT")
        self._migrate()
        self.assertEqual(list(rules(self.conn)), ["Northwind Coffee"])

    def test_conflicting_raw_rules_stay_split(self):
        # when two raw rules under one canonical disagree, classified_at
        # cannot discriminate, so no winner is invented — everything stays
        canon(self.conn, "SKYPORT CAFE GATE B", "Skyport Cafe")
        canon(self.conn, "SKYPORT CAFE DELI", "Skyport Cafe")
        rule(self.conn, "SKYPORT CAFE GATE B", "TRAVEL")
        rule(self.conn, "SKYPORT CAFE DELI", "FOOD_AND_DRINK")
        self._migrate()
        self.assertEqual(set(rules(self.conn)),
                         {"SKYPORT CAFE GATE B", "SKYPORT CAFE DELI"})

    def test_user_rule_at_canonical_absorbs_agreeing_raws(self):
        canon(self.conn, "SQ *MAPLE SPR", "Maple Springs")
        rule(self.conn, "Maple Springs", "ENTERTAINMENT", source="user")
        rule(self.conn, "SQ *MAPLE SPR", "ENTERTAINMENT")
        self._migrate()
        # raw llm duplicate gone; the user rule survives untouched
        self.assertEqual(rules(self.conn), {
            "Maple Springs": ("ENTERTAINMENT", "user", False)})

    def test_user_rule_conflicting_with_raws_left_alone(self):
        canon(self.conn, "SQ *MAPLE SPR", "Maple Springs")
        rule(self.conn, "Maple Springs", "ENTERTAINMENT", source="user")
        rule(self.conn, "SQ *MAPLE SPR", "FOOD_AND_DRINK")
        self._migrate()
        # disagreement → nothing moves (apply()'s user pass already wins)
        self.assertEqual(set(rules(self.conn)),
                         {"Maple Springs", "SQ *MAPLE SPR"})

    def test_disabled_flag_mismatch_left_alone(self):
        canon(self.conn, "HUT ONE", "Coffee Hut")
        canon(self.conn, "HUT TWO", "Coffee Hut")
        rule(self.conn, "HUT ONE", "FOOD_AND_DRINK")
        rule(self.conn, "HUT TWO", "FOOD_AND_DRINK", disabled=True)
        self._migrate()
        self.assertEqual(set(rules(self.conn)), {"HUT ONE", "HUT TWO"})

    def test_all_disabled_merge_stays_disabled(self):
        canon(self.conn, "HUT ONE", "Coffee Hut")
        canon(self.conn, "HUT TWO", "Coffee Hut")
        rule(self.conn, "HUT ONE", "FOOD_AND_DRINK", disabled=True)
        rule(self.conn, "HUT TWO", "FOOD_AND_DRINK", disabled=True)
        self._migrate()
        self.assertEqual(rules(self.conn), {
            "Coffee Hut": ("FOOD_AND_DRINK", "llm", True)})

    def test_seed_and_undeduped_rows_untouched(self):
        canon(self.conn, "TST* CAFE LUZ", "Cafe Luz")
        rule(self.conn, "TST* CAFE LUZ", "FOOD_AND_DRINK", source="seed")
        rule(self.conn, "BODEGA LUZ", "FOOD_AND_DRINK")   # no mapping
        self._migrate()
        self.assertEqual(set(rules(self.conn)),
                         {"TST* CAFE LUZ", "BODEGA LUZ"})

    def test_idempotent(self):
        canon(self.conn, "SQ *MAPLE SPR", "Maple Springs")
        rule(self.conn, "SQ *MAPLE SPR", "ENTERTAINMENT")
        self._migrate()
        self._migrate()
        self.assertEqual(rules(self.conn), {
            "Maple Springs": ("ENTERTAINMENT", "llm", False)})

    def test_merge_then_apply_covers_a_third_variant(self):
        # the point of the re-key: post-merge, a generic-bucket row under a
        # variant that never had its own rule is filled by apply()
        for raw in ("SQ *CAROUSEL", "CAROUSEL LLC", "NYX CAROUSEL"):
            canon(self.conn, raw, "Carousel")
        rule(self.conn, "SQ *CAROUSEL", "ENTERTAINMENT")
        rule(self.conn, "CAROUSEL LLC", "ENTERTAINMENT")
        self._migrate()
        add_raw_txn(self.conn, "v3", "2025-07-01", 9, "NYX CAROUSEL",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "v3"), "ENTERTAINMENT")

    def test_flow_guard_intact_after_migration(self):
        canon(self.conn, "ACME XFER RAW", "Acme Transfer")
        rule(self.conn, "ACME XFER RAW", "ENTERTAINMENT")   # bogus rule
        self._migrate()
        add_raw_txn(self.conn, "flow", "2025-07-01", 500, "Acme Transfer",
                    primary="TRANSFER_OUT",
                    raw={"personal_finance_category":
                         {"primary": "TRANSFER_OUT"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "flow"), "TRANSFER_OUT")


class PendingCanonicalTests(unittest.TestCase):
    """pending_merchants groups by canonical: one business = one pending
    merchant, a rule under any variant hides the whole canonical, and new
    rules (llm + seed) are stored under the canonical key."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _generic(self, tid, merchant, primary="OTHER"):
        add_raw_txn(self.conn, tid, "2025-07-01", 10, merchant,
                    primary=primary,
                    raw={"personal_finance_category": {"primary": primary}})

    def test_variants_collapse_to_one_pending_canonical(self):
        canon(self.conn, "NORTHWIND COFFEE", "Northwind Coffee")
        canon(self.conn, "SQ *NRTHWND SPR", "Northwind Coffee")
        self._generic("a", "NORTHWIND COFFEE")
        self._generic("b", "SQ *NRTHWND SPR", primary="GENERAL_MERCHANDISE")
        pend = llm_categorize.pending_merchants(self.conn)
        self.assertEqual([p["merchant"] for p in pend],
                         ["Northwind Coffee"])
        self.assertEqual(pend[0]["n"], 2)

    def test_rule_under_any_variant_hides_the_canonical(self):
        # a rule still keyed to a RAW variant (pre-044 state) must hide
        # every sibling variant, or raw-keyed llm rules re-offer them
        canon(self.conn, "NORTHWIND COFFEE", "Northwind Coffee")
        canon(self.conn, "SQ *NRTHWND SPR", "Northwind Coffee")
        cache(self.conn, "NORTHWIND COFFEE", "ENTERTAINMENT")
        self._generic("b", "SQ *NRTHWND SPR")
        self.assertEqual(llm_categorize.pending_merchants(self.conn), [])

    def test_new_llm_rule_stored_under_canonical(self):
        canon(self.conn, "SQ *MAPLE SPR", "Maple Springs")
        self._generic("a", "SQ *MAPLE SPR")
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.categorize_new(
                self.conn, transport=reply_transport({"1": "ENTERTAINMENT"}))
        self.assertEqual(stats["merchants_classified"], 1)
        row = self.conn.execute(
            "SELECT merchant FROM merchant_categories").fetchone()
        self.assertEqual(row["merchant"], "Maple Springs")
        # …and the cached rule was applied to the raw-variant row
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")

    def test_seed_prefix_matches_via_raw_descriptor(self):
        # canonicalization strips the TST* prefix; the seed rule must still
        # fire off the raw descriptor, cached under the canonical
        canon(self.conn, "TST* CAFE LUZ", "Cafe Luz")
        self._generic("a", "TST* CAFE LUZ", primary="GENERAL_SERVICES")
        llm_categorize.apply_seed(self.conn)
        row = self.conn.execute(
            "SELECT merchant, category_primary, source "
            "FROM merchant_categories").fetchone()
        self.assertEqual((row["merchant"], row["category_primary"],
                          row["source"]),
                         ("Cafe Luz", "FOOD_AND_DRINK", "seed"))

    def test_flow_raw_variant_never_reaches_the_llm(self):
        # a canonical mapping must not launder a flow/mechanics descriptor
        # into the categorizer's view
        canon(self.conn, "ONLINE TRANSFER TO SAVINGS", "Savings Move")
        self._generic("a", "ONLINE TRANSFER TO SAVINGS")
        self.assertEqual(llm_categorize.pending_merchants(self.conn), [])


if __name__ == "__main__":
    unittest.main()
