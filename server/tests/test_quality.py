"""Categorization quality: curated seed rules (deterministic first pass,
works with no LLM at all), and user corrections teaching the merchant map
(restricted to standard spend primaries — flow-safe)."""

import os
import unittest
from unittest.mock import patch

from oikonome.engine import llm_categorize, merchant_seed
from oikonome.web import data

from .test_llm_categorize import add_raw_txn, cache
from .util import make_db


class SeedMatchTests(unittest.TestCase):
    def test_common_chains(self):
        self.assertEqual(merchant_seed.match("TRADER JOES #451"),
                         "FOOD_AND_DRINK")
        self.assertEqual(merchant_seed.match("Trader Joe's #08"),
                         "FOOD_AND_DRINK")
        self.assertEqual(merchant_seed.match("SHELL OIL 5723"),
                         "TRANSPORTATION")
        self.assertEqual(merchant_seed.match("WAL-MART #1234"),
                         "GENERAL_MERCHANDISE")
        self.assertEqual(merchant_seed.match("NETFLIX.COM"),
                         "ENTERTAINMENT")

    def test_word_boundaries_prevent_false_positives(self):
        # "SHELL" must not fire inside "SHELLY'S"
        self.assertIsNone(merchant_seed.match("SHELLYS CAFE"))
        self.assertIsNone(merchant_seed.match("MARSHELL CONSULTING"))

    def test_processor_prefixes(self):
        self.assertEqual(merchant_seed.match("TST* BURGER BARN"),
                         "FOOD_AND_DRINK")
        self.assertEqual(merchant_seed.match("DOORDASH*SUSHI PLACE"),
                         "FOOD_AND_DRINK")

    def test_unknown_stays_unclaimed(self):
        self.assertIsNone(merchant_seed.match("BODEGA LUZ 42"))
        self.assertIsNone(merchant_seed.match(None))


class SeedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_seed_categorizes_with_no_llm_configured(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            add_raw_txn(self.conn, "t1", "2025-07-01", 63, "SHELL OIL 5723",
                        raw={})
            stats = llm_categorize.categorize_new(self.conn)
        self.assertFalse(stats["configured"])
        self.assertEqual(stats["seeded"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t1'"
        ).fetchone()["category_primary"], "TRANSPORTATION")

    def test_seed_never_overwrites_existing_cache(self):
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary) "
            "VALUES ('SHELL OIL 5723', 'TRAVEL')")
        add_raw_txn(self.conn, "t1", "2025-07-01", 63, "SHELL OIL 5723",
                    raw={})
        llm_categorize.apply_seed(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant='SHELL OIL 5723'").fetchone()["category_primary"],
            "TRAVEL")


class FlowMechanicsTests(unittest.TestCase):
    """On category-less sources, bank-mechanics rows (card payments,
    transfers, ATM) must be flow-classified BEFORE any categorizer — an
    LLM filing 'Chase Credit Card' under spend double-counts every card
    payment."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _run_unconfigured(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            return llm_categorize.categorize_new(self.conn)

    def test_card_payment_becomes_loan_payment(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 850,
                    "American Express Credit Card", raw={})
        stats = self._run_unconfigured()
        self.assertEqual(stats["flow_classified"], 1)
        row = self.conn.execute(
            "SELECT category_primary, category_detailed FROM transactions "
            "WHERE id='t1'").fetchone()
        self.assertEqual(
            (row["category_primary"], row["category_detailed"]),
            ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))

    def test_transfer_direction_follows_sign(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 500,
                    "Bank of America Transfer", raw={})
        add_raw_txn(self.conn, "t2", "2025-07-01", -500,
                    "Bank of America Transfer", raw={})
        add_raw_txn(self.conn, "t3", "2025-07-01", 200,
                    "ATM Withdrawal", raw={})
        self._run_unconfigured()
        rows = {r["id"]: r["category_primary"] for r in self.conn.execute(
            "SELECT id, category_primary FROM transactions")}
        self.assertEqual(rows["t1"], "TRANSFER_OUT")
        self.assertEqual(rows["t2"], "TRANSFER_IN")
        self.assertEqual(rows["t3"], "TRANSFER_OUT")

    def test_miscached_flow_string_is_purged_and_fixed(self):
        # the damage this prevents: a prior LLM pass caches the payment
        # string as spend and apply() spreads it
        cache(self.conn, "Chase Credit Card", "GENERAL_MERCHANDISE")
        add_raw_txn(self.conn, "t1", "2025-07-01", 900, "Chase Credit Card",
                    primary="GENERAL_MERCHANDISE", raw={})
        self._run_unconfigured()
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories "
            "WHERE merchant='Chase Credit Card'").fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t1'"
        ).fetchone()["category_primary"], "LOAN_PAYMENTS")

    def test_sharp_source_rows_are_never_touched(self):
        # a Plaid-categorized row whose NAME matches a mechanics pattern
        # keeps the aggregator's word (raw category present → not sparse)
        from .test_llm_categorize import PLAID_SHARP
        add_raw_txn(self.conn, "t1", "2025-07-01", 30, "Transfer Cafe",
                    primary="FOOD_AND_DRINK", raw=PLAID_SHARP)
        self._run_unconfigured()
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t1'"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_mechanics_never_reach_the_llm(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 850,
                    "Chase Credit Card", raw={})
        add_raw_txn(self.conn, "t2", "2025-07-01", 120,
                    "Check Paid #995360", raw={})
        add_raw_txn(self.conn, "t3", "2025-07-01", 42,
                    "BODEGA LUZ 42", raw={})
        pend = {m["merchant"]
                for m in llm_categorize.pending_merchants(self.conn)}
        self.assertEqual(pend, {"BODEGA LUZ 42"})

    def test_user_override_stays_sacred(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 850,
                    "Chase Credit Card", raw={}, override="Business card")
        self._run_unconfigured()
        row = self.conn.execute(
            "SELECT category_primary, category_override FROM transactions "
            "WHERE id='t1'").fetchone()
        self.assertIsNone(row["category_primary"])
        self.assertEqual(row["category_override"], "Business card")


class CorrectionTeachesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_correction_updates_past_rows_merchant_wide(self):
        # even rows with a SHARP aggregator category — the user knows their
        # merchant better than the aggregator does
        from .test_llm_categorize import PLAID_SHARP
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "BODEGA LUZ 42",
                    raw={})
        add_raw_txn(self.conn, "t2", "2025-06-01", 15, "BODEGA LUZ 42",
                    primary="FOOD_AND_DRINK", raw=PLAID_SHARP)
        data.set_category(self.conn, "t1", "ENTERTAINMENT", scope="all")
        row = self.conn.execute(
            "SELECT category_primary, source FROM merchant_categories "
            "WHERE merchant='BODEGA LUZ 42'").fetchone()
        self.assertEqual((row["category_primary"], row["source"]),
                         ("ENTERTAINMENT", "user"))
        # the sibling row updated IMMEDIATELY, sharp category and all
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t2'"
        ).fetchone()["category_primary"], "ENTERTAINMENT")

    def test_custom_category_propagates_too(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "BODEGA LUZ 42",
                    raw={})
        add_raw_txn(self.conn, "t2", "2025-06-01", 15, "BODEGA LUZ 42",
                    raw={})
        data.set_category(self.conn, "t1", "Hobby Farm", scope="all")
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='t2'"
        ).fetchone()["category_primary"], "Hobby Farm")

    def test_flow_categories_never_poison_map(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "BODEGA LUZ 42",
                    raw={})
        data.set_category(self.conn, "t1", "TRANSFER_OUT", scope="all")   # flow-guard
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant='BODEGA LUZ 42'"
        ).fetchone())

    def test_a_rule_naming_a_flow_category_is_never_applied(self):
        # defence in depth behind the refusing doors: a user rule that
        # somehow names a transfer (an old row, a hand edit) must not turn
        # the merchant's spend into transfers on the next sync — the user
        # pass writes over the aggregator's label, so the guard has to be
        # on the rule itself
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "BODEGA LUZ 42",
                    primary="FOOD_AND_DRINK", raw={})
        add_raw_txn(self.conn, "t2", "2025-06-01", 15, "BODEGA LUZ 42",
                    raw={})
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('BODEGA LUZ 42', 'TRANSFER_OUT', 'user')")
        llm_categorize.apply(self.conn)
        rows = {r["id"]: r["category_primary"] for r in self.conn.execute(
            "SELECT id, category_primary FROM transactions")}
        self.assertEqual(rows["t1"], "FOOD_AND_DRINK")
        self.assertIsNone(rows["t2"])

    def test_category_rename_cannot_fold_rules_into_a_flow_category(self):
        # the rename door rewrites merchant rules too, so it may fold a
        # custom label into a spend primary but never into a transfer
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "BODEGA LUZ 42",
                    raw={})
        data.set_category(self.conn, "t1", "Allowance", scope="all")
        with self.assertRaises(ValueError):
            data.rename_category(self.conn, "Allowance", "TRANSFER_OUT")
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant='BODEGA LUZ 42'").fetchone()["category_primary"],
            "Allowance")
        # a spend primary is still a fine target
        r = data.rename_category(self.conn, "Allowance", "ENTERTAINMENT")
        self.assertEqual(r["rules"], 1)

    def test_user_rule_reaches_flow_rows_but_never_overrides(self):
        # a "teach all" correction is the person's merchant rule: it reaches
        # a sibling row the aggregator labelled as a transfer (a Venmo payment
        # to a person is TRANSFER_OUT to the bank and whatever the household
        # says it is), but never a row carrying its own per-row override
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "BODEGA LUZ 42",
                    raw={})
        add_raw_txn(self.conn, "t2", "2025-06-01", 500, "BODEGA LUZ 42",
                    primary="TRANSFER_OUT", raw={})
        add_raw_txn(self.conn, "t3", "2025-05-01", 9, "BODEGA LUZ 42",
                    raw={}, override="Gift")
        data.set_category(self.conn, "t1", "ENTERTAINMENT", scope="all")
        rows = {r["id"]: (r["category_primary"], r["category_override"])
                for r in self.conn.execute(
                    "SELECT id, category_primary, category_override "
                    "FROM transactions")}
        self.assertEqual(rows["t2"][0], "ENTERTAINMENT")    # user rule reaches it
        self.assertEqual(rows["t3"][1], "Gift")             # override sacred


if __name__ == "__main__":
    unittest.main()
