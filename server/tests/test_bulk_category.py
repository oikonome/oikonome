"""Bulk category override for a whole (canonical) merchant from its
history page — set once, every past + future transaction follows; count shown
before the write; flow rows never touched; undoable."""

import unittest

from oikonome.engine import llm_categorize
from oikonome.web import data

from .test_canonical_rules import canon, cat
from .test_llm_categorize import add_raw_txn
from .util import make_db


class BulkCategoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        for raw in ("NORTHWIND COFFEE", "SQ *NRTHWND 4SPRINGFIELD NV"):
            canon(self.conn, raw, "Northwind Coffee")
        # two descriptor variants of one merchant, both mis-categorized
        add_raw_txn(self.conn, "a", "2025-07-01", 10, "NORTHWIND COFFEE",
                    primary="PERSONAL_CARE",
                    raw={"personal_finance_category": {"primary": "PERSONAL_CARE"}})
        add_raw_txn(self.conn, "b", "2025-07-02", 12, "SQ *NRTHWND 4SPRINGFIELD NV",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category": {"primary": "GENERAL_MERCHANDISE"}})

    def tearDown(self):
        self.conn.close()

    def test_preview_counts_canonical_rows(self):
        # clicking EITHER variant previews the whole canonical merchant
        p = data.merchant_category_preview(self.conn, "SQ *NRTHWND 4SPRINGFIELD NV",
                                           "ENTERTAINMENT")
        self.assertEqual(p["merchant"], "Northwind Coffee")
        self.assertEqual(p["count"], 2)

    def test_apply_covers_all_variants_past_and_future(self):
        res = data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                         "ENTERTAINMENT")
        self.assertEqual(res["count"], 2)
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "b"), "ENTERTAINMENT")
        # a FUTURE row under a third variant follows via the merchant rule
        canon(self.conn, "NRTHWND SPR POS", "Northwind Coffee")
        add_raw_txn(self.conn, "c", "2025-08-01", 9, "NRTHWND SPR POS",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        from oikonome.engine import llm_categorize
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "c"), "ENTERTAINMENT")

    def test_undo_restores_prior_categories(self):
        res = data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                         "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")
        n = data.undo_merchant_category(self.conn, res["undo"])
        self.assertEqual(n, 2)
        self.assertEqual(cat(self.conn, "a"), "PERSONAL_CARE")
        self.assertEqual(cat(self.conn, "b"), "GENERAL_MERCHANDISE")
        # the merchant rule is gone (none existed before)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE source='user'").fetchone())

    def test_user_rule_reaches_flow_rows(self):
        # A person's merchant rule covers rows the aggregator labelled as a
        # transfer: a peer-to-peer payment to a contractor is TRANSFER_OUT
        # to the aggregator and home services to the household, and every new one must
        # land where the rule says — not as a transfer until hand-fixed.
        canon(self.conn, "NW XFER", "Northwind Coffee")   # sibling variant
        add_raw_txn(self.conn, "flow", "2025-07-03", 500, "NW XFER",
                    primary="TRANSFER_OUT",
                    raw={"personal_finance_category": {"primary": "TRANSFER_OUT"}})
        res = data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                         "ENTERTAINMENT")
        self.assertEqual(res["count"], 3)                    # flow row included
        self.assertEqual(cat(self.conn, "flow"), "ENTERTAINMENT")
        # …and a rule arriving AFTER the row (the sync order) applies too
        add_raw_txn(self.conn, "flow2", "2025-07-04", 500, "NW XFER",
                    primary="TRANSFER_OUT",
                    raw={"personal_finance_category": {"primary": "TRANSFER_OUT"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "flow2"), "ENTERTAINMENT")

    def test_flow_category_rejected(self):
        with self.assertRaises(ValueError):
            data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                       "TRANSFER_OUT")

    def test_undo_restores_a_previously_disabled_rule(self):
        # set_merchant_category re-enables
        # the rule (upsert forces disabled=false). Undo must put the rule back
        # to disabled=true, else the user's explicit disable is silently
        # reversed and the rule re-clobbers categories on the next apply().
        canon(self.conn, "ACME GYM", "Acme Gym")
        from oikonome.engine import llm_categorize
        llm_categorize.upsert_user_rule(self.conn, "Acme Gym", "PERSONAL_CARE")
        data.set_rule_disabled(self.conn, "Acme Gym", True)   # user disables it
        add_raw_txn(self.conn, "g", "2025-07-01", 10, "ACME GYM",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_MERCHANDISE"}})

        res = data.set_merchant_category(self.conn, "ACME GYM", "ENTERTAINMENT")
        # the bulk-set re-enabled the rule
        self.assertEqual(self.conn.execute(
            "SELECT disabled FROM merchant_categories WHERE merchant='Acme Gym'"
        ).fetchone()["disabled"], False)

        data.undo_merchant_category(self.conn, res["undo"])
        # undo restored BOTH the prior category and the disabled flag
        row = self.conn.execute(
            "SELECT category_primary, disabled FROM merchant_categories "
            "WHERE merchant='Acme Gym'").fetchone()
        self.assertEqual(row["category_primary"], "PERSONAL_CARE")
        self.assertTrue(row["disabled"])          # explicit disable preserved

    def test_variants_disclose_canonical_siblings(self):
        # The write is canonical-granular: previewing from a descriptor
        # whose tokens DON'T match its canonical sibling ("SQ *NRTHWND
        # 4SPRINGFIELD…" vs "NORTHWIND COFFEE") still moves the sibling's
        # rows via the shared canonical — the confirm must NAME that scope,
        # not just count it.
        p = data.merchant_category_preview(self.conn,
                                           "SQ *NRTHWND 4SPRINGFIELD NV",
                                           "ENTERTAINMENT")
        self.assertEqual(p["count"], 2)          # sibling row counted…
        self.assertIn("NORTHWIND COFFEE", p["variants"])   # …and named
        self.assertIn("SQ *NRTHWND 4SPRINGFIELD NV", p["variants"])

    def test_bank_fees_rows_are_tracked_by_preview_and_undo(self):
        # apply()'s flow-guard rewrites BANK_FEES rows, so the preview
        # count + undo snapshot MUST include them — excluded, they are
        # silently rewritten and un-undoable.
        canon(self.conn, "ACME BANK", "Acme Bank")
        add_raw_txn(self.conn, "gen", "2025-07-01", 10, "ACME BANK",
                    primary="GENERAL_SERVICES",
                    raw={"personal_finance_category": {"primary": "GENERAL_SERVICES"}})
        add_raw_txn(self.conn, "fee", "2025-07-02", 3, "ACME BANK",
                    primary="BANK_FEES",
                    raw={"personal_finance_category": {"primary": "BANK_FEES"}})
        p = data.merchant_category_preview(self.conn, "ACME BANK", "SHOPPING")
        self.assertEqual(p["count"], 2)                  # BANK_FEES now counted
        res = data.set_merchant_category(self.conn, "ACME BANK", "SHOPPING")
        self.assertEqual(cat(self.conn, "fee"), "SHOPPING")   # was rewritten
        data.undo_merchant_category(self.conn, res["undo"])
        self.assertEqual(cat(self.conn, "fee"), "BANK_FEES")  # now restorable


class TokenFamilyTests(unittest.TestCase):
    """The bulk write covers the history page's token-subset view — every
    canonical whose raw names the page's charge list matches — because
    truncated descriptors split one store across canonicals and the write
    must cover exactly what the page shows — confirming the change and then
    seeing the same charge unchanged is the failure this prevents."""

    def setUp(self):
        self.conn = make_db()
        # one store, split across DIFFERENT canonicals by truncation
        canon(self.conn, "ACME TOY", "Acme Toy")
        canon(self.conn, "ACME TOY XYZ", "Acme Toy Xyz")
        add_raw_txn(self.conn, "t1", "2025-07-01", 15, "ACME TOY",
                    primary="PERSONAL_CARE",
                    raw={"personal_finance_category":
                         {"primary": "PERSONAL_CARE"}})
        add_raw_txn(self.conn, "t2", "2025-07-02", 36, "ACME TOY XYZ",
                    primary="FOOD_AND_DRINK",
                    raw={"personal_finance_category":
                         {"primary": "FOOD_AND_DRINK"}})

    def tearDown(self):
        self.conn.close()

    def test_preview_spans_the_page_scope_and_names_variants(self):
        p = data.merchant_category_preview(self.conn, "ACME TOY",
                                           "ENTERTAINMENT")
        self.assertEqual(p["count"], 2)      # both canonicals' rows counted
        self.assertIn("ACME TOY", p["variants"])
        self.assertIn("ACME TOY XYZ", p["variants"])

    def test_preview_can_scope_to_the_single_canonical(self):
        # family=False previews the OTHER write — the ledger row's "apply
        # to whole merchant" (set_category scope="all"), which writes one
        # canonical's rule, not the page's token family. The confirm's
        # count must match the write behind it, and the family count here
        # (2) would overstate what that write moves (1).
        p = data.merchant_category_preview(self.conn, "ACME TOY",
                                           "ENTERTAINMENT", family=False)
        self.assertEqual(p["count"], 1)
        self.assertEqual(p["variants"], ["ACME TOY"])

    def test_apply_covers_every_canonical_the_page_matches(self):
        res = data.set_merchant_category(self.conn, "ACME TOY",
                                         "ENTERTAINMENT")
        self.assertEqual(res["count"], 2)
        self.assertEqual(cat(self.conn, "t1"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "t2"), "ENTERTAINMENT")
        # both canonicals carry the user rule (future rows on either follow)
        rules = {r["merchant"] for r in self.conn.execute(
            "SELECT merchant FROM merchant_categories "
            "WHERE source='user'").fetchall()}
        self.assertIn("Acme Toy", rules)
        self.assertIn("Acme Toy Xyz", rules)

    def test_scope_is_the_payees_tokens_not_the_reverse(self):
        # clicking the LONGER name must not sweep the shorter one's rows:
        # 'ACME TOY XYZ' tokens are not all present in 'ACME TOY'…
        # (they are — same token SET — so use a genuinely narrower payee)
        canon(self.conn, "ACME TOY GIFT SHOP", "Acme Toy Gift Shop")
        add_raw_txn(self.conn, "gift", "2025-07-03", 9,
                    "ACME TOY GIFT SHOP", primary="PERSONAL_CARE",
                    raw={"personal_finance_category":
                         {"primary": "PERSONAL_CARE"}})
        p = data.merchant_category_preview(self.conn,
                                           "ACME TOY GIFT SHOP",
                                           "ENTERTAINMENT")
        # the narrow payee's tokens don't all appear in the plain rows
        self.assertEqual(p["count"], 1)

    def test_undo_restores_rows_and_every_touched_rule(self):
        res = data.set_merchant_category(self.conn, "ACME TOY",
                                         "ENTERTAINMENT")
        n = data.undo_merchant_category(self.conn, res["undo"])
        self.assertEqual(n, 2)
        self.assertEqual(cat(self.conn, "t1"), "PERSONAL_CARE")
        self.assertEqual(cat(self.conn, "t2"), "FOOD_AND_DRINK")
        rules = self.conn.execute(
            "SELECT merchant FROM merchant_categories "
            "WHERE source='user'").fetchall()
        self.assertEqual(rules, [])          # neither rule existed before

    def test_flow_rows_across_the_family_count_apply_and_undo_alike(self):
        # One store split across two canonicals, each with a bank-labelled
        # transfer row. The preview's count, its flow disclosure, the
        # write's affected set and the undo must all see the SAME rows —
        # they derive from one merchant-scan predicate, so a change to the
        # scan cannot move rows the preview never counted.
        add_raw_txn(self.conn, "x1", "2025-07-03", 300, "ACME TOY",
                    primary="TRANSFER_OUT", raw={})
        add_raw_txn(self.conn, "x2", "2025-07-04", 120, "ACME TOY XYZ",
                    primary="TRANSFER_OUT", raw={})
        add_raw_txn(self.conn, "ov", "2025-07-05", 9, "ACME TOY XYZ",
                    primary="TRANSFER_OUT", raw={}, override="Gift")
        p = data.merchant_category_preview(self.conn, "ACME TOY",
                                           "ENTERTAINMENT")
        self.assertEqual(p["count"], 4)          # t1 t2 x1 x2, not ov
        self.assertEqual(p["flow_rows"], 2)      # one per canonical
        self.assertEqual(p["skipped_override"], 1)
        res = data.set_merchant_category(self.conn, "ACME TOY",
                                         "ENTERTAINMENT")
        self.assertEqual(res["count"], p["count"])
        self.assertEqual({a["id"] for a in res["undo"]["affected"]},
                         {"t1", "t2", "x1", "x2"})
        for tid in ("t1", "t2", "x1", "x2"):
            self.assertEqual(cat(self.conn, tid), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "ov"), "TRANSFER_OUT")   # override
        data.undo_merchant_category(self.conn, res["undo"])
        self.assertEqual(cat(self.conn, "x1"), "TRANSFER_OUT")
        self.assertEqual(cat(self.conn, "x2"), "TRANSFER_OUT")
        self.assertEqual(cat(self.conn, "t1"), "PERSONAL_CARE")

    def test_legacy_single_canon_undo_shape_still_works(self):
        res = data.set_merchant_category(self.conn, "ACME TOY",
                                         "ENTERTAINMENT")
        entry = res["undo"]["canons"][0]
        legacy = {"canon": entry["canon"],
                  "prior_rule": entry["prior_rule"],
                  "affected": [a for a in res["undo"]["affected"]]}
        n = data.undo_merchant_category(self.conn, legacy)
        self.assertEqual(n, 2)


class PageBoundaryTests(unittest.TestCase):
    """The bulk write must use the history page's EXACT matcher. A BILL's
    page matches via budget.merchant_matcher (phrase / alternatives forms),
    not the token subset — 'Ax Mobile' tokenizes to just {'mobile'} (a
    two-letter token is dropped), so a token-subset write would sweep
    Nova Mobile rows the page never showed. The write must never match looser
    than what the page displays."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _bill(self, payee: str, merchant: str):
        self.conn.execute(
            """INSERT INTO bills (id, type, payee, amount, frequency,
                                  monthly_amount, merchant, active,
                                  is_completed)
               VALUES (%s,'BILL',%s,-20,'MONTHLY',-20,%s,1,0)""",
            (payee.lower().replace(" ", "-"), payee, merchant))

    def test_phrase_bill_never_sweeps_lookalike_merchants(self):
        self._bill("Ax Mobile", "ax mobile")
        add_raw_txn(self.conn, "us", "2025-07-01", 25, "AX MOBILE",
                    primary="GENERAL_SERVICES",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_SERVICES"}})
        add_raw_txn(self.conn, "tmo", "2025-07-02", 80, "NOVA MOBILE",
                    primary="GENERAL_SERVICES",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_SERVICES"}})
        p = data.merchant_category_preview(self.conn, "Ax Mobile",
                                           "RENT_AND_UTILITIES")
        self.assertEqual(p["count"], 1)              # page shows Ax Mobile only
        self.assertNotIn("NOVA MOBILE", p["variants"])
        res = data.set_merchant_category(self.conn, "Ax Mobile",
                                         "RENT_AND_UTILITIES")
        self.assertEqual(res["count"], 1)
        self.assertEqual(cat(self.conn, "us"), "RENT_AND_UTILITIES")
        self.assertEqual(cat(self.conn, "tmo"), "GENERAL_SERVICES")
        # and no rule was written for the lookalike
        rules = {r["merchant"] for r in self.conn.execute(
            "SELECT merchant FROM merchant_categories").fetchall()}
        self.assertNotIn("NOVA MOBILE", rules)

    def test_alternatives_bill_covers_both_brands_like_the_page(self):
        # a rebrand bill ('nimbus|stratus') lists BOTH brands on its page;
        # the write must cover both, exactly as displayed
        self._bill("Nimbus", "nimbus|stratus")
        add_raw_txn(self.conn, "c1", "2025-07-01", 20, "NIMBUS SUBSCRIPTION",
                    primary="GENERAL_SERVICES",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_SERVICES"}})
        add_raw_txn(self.conn, "c2", "2025-07-02", 20, "STRATUS CLOUD INC",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_MERCHANDISE"}})
        p = data.merchant_category_preview(self.conn, "Nimbus",
                                           "ENTERTAINMENT")
        self.assertEqual(p["count"], 2)
        res = data.set_merchant_category(self.conn, "Nimbus", "ENTERTAINMENT")
        self.assertEqual(res["count"], 2)
        self.assertEqual(cat(self.conn, "c1"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "c2"), "ENTERTAINMENT")


class ResolveOnWriteTests(unittest.TestCase):
    """Resolve on write: a raw-keyed user rule that resolves to the same
    canonical with a DIFFERENT category would make apply()'s user-pass
    unanimity gate silently veto a merchant-wide recategorize while the API
    still reported success. The user's explicit choice must win; the
    reported count must reflect what actually moved."""

    def setUp(self):
        self.conn = make_db()
        # one real merchant under two descriptors, both -> one canonical
        for raw in ("SQ *POINTE DAMOUR CARO", "NYX POINTE AMOUR CARO"):
            canon(self.conn, raw, "Pointe Amour Caro")
        add_raw_txn(self.conn, "a", "2025-07-01", 10, "SQ *POINTE DAMOUR CARO",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_MERCHANDISE"}})
        add_raw_txn(self.conn, "b", "2025-07-02", 12, "NYX POINTE AMOUR CARO",
                    primary="GENERAL_MERCHANDISE",
                    raw={"personal_finance_category":
                         {"primary": "GENERAL_MERCHANDISE"}})
        # a RAW-keyed user rule with a CONFLICTING category (a 044 split
        # survivor) resolving to the same canonical -> breaks unanimity
        self.conn.execute(
            "INSERT INTO merchant_categories "
            "(merchant, category_primary, source, disabled) "
            "VALUES (%s,%s,'user',false)",
            ("SQ *POINTE DAMOUR CARO", "PERSONAL_CARE"))

    def tearDown(self):
        self.conn.close()

    def test_users_choice_wins_over_conflicting_variant_rule(self):
        res = data.set_merchant_category(self.conn, "NYX POINTE AMOUR CARO",
                                         "ENTERTAINMENT")
        # every variant's rows moved despite the pre-existing conflict…
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "b"), "ENTERTAINMENT")
        # …and the count is honest (2 rows actually changed, not 0-but-"ok")
        self.assertEqual(res["count"], 2)
        # the conflicting variant rule is gone — the canonical rule now
        # covers every spelling, and two rules for one merchant read as two
        # merchants on the Rules page. Undo still restores
        # it (next test).
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories "
            "WHERE merchant='SQ *POINTE DAMOUR CARO'").fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant='Pointe Amour Caro'").fetchone()[
                "category_primary"], "ENTERTAINMENT")

    def test_undo_restores_the_conflicting_rule(self):
        res = data.set_merchant_category(self.conn, "NYX POINTE AMOUR CARO",
                                         "ENTERTAINMENT")
        data.undo_merchant_category(self.conn, res["undo"])
        # the raw-keyed rule is back to its prior (conflicting) category…
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant='SQ *POINTE DAMOUR CARO'").fetchone()[
                "category_primary"], "PERSONAL_CARE")
        # …the canonical rule the write created is gone…
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories "
            "WHERE merchant='Pointe Amour Caro'").fetchone())
        # …and the rows returned to their prior category
        self.assertEqual(cat(self.conn, "a"), "GENERAL_MERCHANDISE")
        self.assertEqual(cat(self.conn, "b"), "GENERAL_MERCHANDISE")

    def test_single_row_teach_all_also_retires_the_duplicate_spelling(self):
        # the other door that teaches a merchant — "apply to whole
        # merchant" from one transaction — must leave the same single
        # canonical rule, or the Rules page lists one merchant as two and
        # the stale spelling's category blocks apply()'s unanimity gate
        data.set_category(self.conn, "b", "ENTERTAINMENT", scope="all")
        rules = {r["merchant"]: r["category_primary"] for r in self.conn.execute(
            "SELECT merchant, category_primary FROM merchant_categories "
            "WHERE source='user'").fetchall()}
        self.assertEqual(rules, {"Pointe Amour Caro": "ENTERTAINMENT"})
        # the sibling variant's row moved (the unanimity gate was not
        # vetoed); the corrected row itself carries the per-row override
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")
        self.assertEqual(self.conn.execute(
            "SELECT category_override FROM transactions WHERE id='b'"
        ).fetchone()["category_override"], "ENTERTAINMENT")

    def test_single_row_teach_all_returns_an_undo_for_the_rule_delete(self):
        # the teach deletes the conflicting duplicate spelling
        # (resolve-on-write); without an undo snapshot that delete was
        # silent and unrecoverable from this door — the history page's
        # bulk write already snapshotted it, so this door does the same
        res = data.set_category(self.conn, "b", "ENTERTAINMENT", scope="all")
        self.assertEqual(res["merchant"], "Pointe Amour Caro")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories "
            "WHERE merchant='SQ *POINTE DAMOUR CARO'").fetchone())
        # the snapshot round-trips through the same undo door
        data.undo_merchant_category(self.conn, res["undo"])
        row = self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant='SQ *POINTE DAMOUR CARO'").fetchone()
        self.assertEqual(row["category_primary"], "PERSONAL_CARE")
        # the canonical rule the teach created is gone (none existed before)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories "
            "WHERE merchant='Pointe Amour Caro'").fetchone())
        # the sibling row moved back; the corrected row keeps its per-row
        # override — undo reverses the merchant-wide teach, not the
        # user's direct answer on the row itself
        self.assertEqual(cat(self.conn, "a"), "GENERAL_MERCHANDISE")
        self.assertEqual(self.conn.execute(
            "SELECT category_override FROM transactions WHERE id='b'"
        ).fetchone()["category_override"], "ENTERTAINMENT")

    def test_single_row_teach_one_returns_no_undo(self):
        # scope="one" writes no rule and deletes nothing — nothing to undo
        self.assertIsNone(
            data.set_category(self.conn, "b", "ENTERTAINMENT", scope="one"))

    def test_disabled_conflicting_rule_is_left_disabled(self):
        # a DISABLED conflicting rule doesn't block the gate, so it must not be
        # re-enabled by resolve-on-write (that would reverse the user's disable)
        self.conn.execute(
            "UPDATE merchant_categories SET disabled=true "
            "WHERE merchant='SQ *POINTE DAMOUR CARO'")
        data.set_merchant_category(self.conn, "NYX POINTE AMOUR CARO",
                                   "ENTERTAINMENT")
        self.assertEqual(cat(self.conn, "b"), "ENTERTAINMENT")
        self.assertTrue(self.conn.execute(
            "SELECT disabled FROM merchant_categories "
            "WHERE merchant='SQ *POINTE DAMOUR CARO'").fetchone()["disabled"])


class PickerCategoryListTests(unittest.TestCase):
    """The "Categorize this merchant" picker is fed by
    ``categories.PLAID_SPEND`` + ``FLOW`` (via /api/categories). Free text
    there lets one-off spellings into the data, so the list has to stay
    exactly the Plaid primary set."""

    def test_picker_list_is_the_16_plaid_primaries(self):
        from oikonome.engine import categories as cat_mod
        self.assertEqual(len(cat_mod.PLAID_PRIMARIES), 16)
        # no value appears in both groups
        self.assertEqual(len(set(cat_mod.PLAID_PRIMARIES)), 16)
        self.assertEqual(set(cat_mod.PLAID_SPEND) & set(cat_mod.FLOW), set())

    def test_llm_categories_cannot_drift_from_the_picker(self):
        # llm_categorize may assign every spend primary EXCEPT bank fees
        from oikonome.engine import categories as cat_mod
        from oikonome.engine import llm_categorize
        self.assertEqual(set(llm_categorize.CATEGORIES) | {"BANK_FEES"},
                         set(cat_mod.PLAID_SPEND))


if __name__ == "__main__":
    unittest.main()
