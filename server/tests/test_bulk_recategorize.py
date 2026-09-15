"""Bulk actions on an explicit selection, and an honest merchant-wide report.

Labelling a payee "for all" moves only some of its rows, and unless the
write says so nothing accounts for the rest. The guard is right —
_BULK_FLOW_SQL keeps a merchant rule from rewriting transfers and income as
spending, which would double-count money movement everywhere — but refusing
silently makes a correct write look like a broken one.

Two changes, and the distinction between them is the point: a merchant-wide
write INFERS a rule from one correction and stays conservative; an explicit
selection is consent to exactly those rows, writes no rule, and may therefore
touch anything — while still reporting what that means.
"""

import unittest

from oikonome.web import data

from .util import (TODAY, _ensure_db, add_bill, add_txn, make_db,
                   write_config)


class MerchantWideReportsItsRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_preview_names_the_transfers_it_will_move_and_the_overrides_it_wont(self):
        add_txn(self.conn, TODAY, 40.0, "VENMO", primary="GENERAL_MERCHANDISE")
        add_txn(self.conn, TODAY, 800.0, "VENMO", primary="TRANSFER_OUT")
        add_txn(self.conn, TODAY, -50.0, "VENMO", primary="TRANSFER_IN")
        add_txn(self.conn, TODAY, 25.0, "VENMO", override="RENT_AND_UTILITIES")
        p = data.merchant_category_preview(self.conn, "VENMO",
                                           "GENERAL_MERCHANDISE")
        # a user rule reaches bank-labelled transfers (a Venmo payment to a
        # person IS the merchant) — counted in, and named so the budget
        # consequence is consented to rather than discovered
        self.assertEqual(p["count"], 2)       # the two transfers (the 40 already is)
        self.assertEqual(p["flow_rows"], 2,
                         "moved transfers must be named, not moved quietly")
        self.assertEqual(p["skipped_override"], 1,
                         "a hand-made per-row decision is still a refusal")

    def test_a_clean_merchant_reports_no_refusals(self):
        add_txn(self.conn, TODAY, 40.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        p = data.merchant_category_preview(self.conn, "SAFEWAY",
                                           "GENERAL_MERCHANDISE")
        self.assertEqual(p["flow_rows"], 0)
        self.assertEqual(p["skipped_override"], 0)


class BulkApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def test_an_explicit_selection_may_recategorize_a_transfer(self):
        """The $800 Venmo rows that are really rent. A merchant RULE must not
        do this; picking the rows by hand is a different act."""
        a = add_txn(self.conn, TODAY, 800.0, "VENMO", primary="TRANSFER_OUT")
        b = add_txn(self.conn, TODAY, 800.0, "VENMO", primary="TRANSFER_OUT")
        out = data.bulk_apply(self.conn, [a, b], "category",
                              "RENT_AND_UTILITIES")
        self.assertEqual(out["applied"], 2)
        self.assertEqual(out["flow"], 2,
                         "it must SAY these were transfers — they now count "
                         "as spending")
        self.assertEqual(self._cat(a), "RENT_AND_UTILITIES")
        self.assertEqual(self._cat(b), "RENT_AND_UTILITIES")

    def test_it_writes_no_merchant_rule(self):
        """The whole reason an explicit selection is allowed to be broader:
        it teaches nothing, so a future VENMO transfer stays a transfer."""
        a = add_txn(self.conn, TODAY, 800.0, "VENMO", primary="TRANSFER_OUT")
        data.bulk_apply(self.conn, [a], "category", "RENT_AND_UTILITIES")
        rule = self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant ILIKE %s",
            ("%venmo%",)).fetchone()
        self.assertIsNone(rule, "a hand-picked set must not become a rule")

    def test_missing_ids_are_reported_not_swallowed(self):
        a = add_txn(self.conn, TODAY, 10.0, "SAFEWAY")
        out = data.bulk_apply(self.conn, [a, "does-not-exist"], "category",
                              "FOOD_AND_DRINK")
        self.assertEqual(out["applied"], 1)
        self.assertEqual(out["missing"], 1)

    def test_business_and_reimbursement_flags_round_trip(self):
        a = add_txn(self.conn, TODAY, 10.0, "OFFICE DEPOT")
        data.bulk_apply(self.conn, [a], "biz_on")
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM business_flags WHERE txn_id=%s", (a,)).fetchone())
        data.bulk_apply(self.conn, [a], "biz_off")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM business_flags WHERE txn_id=%s", (a,)).fetchone())
        data.bulk_apply(self.conn, [a], "reimb_flag")
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM reimburse_flags WHERE txn_id=%s", (a,)).fetchone())
        data.bulk_apply(self.conn, [a], "reimb_unflag")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM reimburse_flags WHERE txn_id=%s", (a,)).fetchone())

    def test_duplicate_ids_are_applied_once(self):
        a = add_txn(self.conn, TODAY, 10.0, "SAFEWAY")
        out = data.bulk_apply(self.conn, [a, a, a], "category",
                              "FOOD_AND_DRINK")
        self.assertEqual(out["applied"], 1)

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            data.bulk_apply(self.conn, ["x"], "delete_everything")


class FlowListHasOneSourceTests(unittest.TestCase):
    """Three places name the flow categories: the SQL guard that stops a
    MACHINE rule rewriting them, the preview's moved-transfer count, and the
    bulk path's consequence count. They must agree, or the dialog promises
    one thing and the write does another — so they are derived, not
    repeated."""

    def test_the_sql_guard_is_built_from_the_tuple(self):
        for cat in data._BULK_FLOW:
            self.assertIn(f"'{cat}'", data._BULK_FLOW_SQL)
        self.assertEqual(data._BULK_FLOW_SQL.count(","),
                         len(data._BULK_FLOW) - 1)

    def test_it_still_matches_the_categorizer_guard(self):
        """The pre-existing warning above _BULK_FLOW_SQL: if this list is
        broader than llm_categorize's, apply() rewrites rows the preview
        never counted and the undo snapshot cannot restore."""
        from oikonome.engine import llm_categorize
        for cat in data._BULK_FLOW:
            self.assertIn(f"'{cat}'", llm_categorize._FLOW_SQL)


class MerchantPageScopeIsTheMerchant(unittest.TestCase):
    """The history page's bulk write covers what the page shows — the
    merchant's own rows — never every descriptor that happens to CONTAIN
    the merchant's name. A substring scope would sweep in every descriptor
    containing "care" — "Northwind Manufacturing", "Carevane Systems",
    "Borealis Health Care" — and the write behind it would turn payroll
    into child care."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, TODAY, 59.0, "CAREWELL.COM 555-010-311SPRINGFIELD",
                merchant="Care", primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 39.0, "CAREWELL.COM", merchant="Care",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, -5250.00, "NORTHWIND MFG PAYROLL~ DDIR",
                merchant="Northwind Manufacturing", primary="INCOME")
        add_txn(self.conn, TODAY, 120.0, "CAREVANE SYSTEMS DIRECT PAY",
                merchant="CAREVANE SYSTEMS DIRECT PAY", primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 80.0, "BOREALIS HEALTH CARE",
                merchant="Borealis Health Care", primary="MEDICAL")

    def tearDown(self):
        self.conn.close()

    def test_the_family_is_the_merchants_own_strings(self):
        self.assertEqual(data._token_family(self.conn, "Care"), ["Care"])

    def test_preview_counts_only_the_merchant(self):
        p = data.merchant_category_preview(self.conn, "Care", "CHILD CARE")
        self.assertEqual(p["count"], 2)
        self.assertEqual(set(p.get("variants") or []), {"Care"})
        self.assertEqual(p.get("flow_rows", 0), 0)

    def test_the_write_moves_only_the_merchant(self):
        res = data.set_merchant_category(self.conn, "Care", "CHILD CARE")
        self.assertEqual(res["count"], 2)
        rows = {r["name"]: r["category_primary"] for r in self.conn.execute(
            "SELECT name, category_primary FROM transactions").fetchall()}
        self.assertEqual(rows["CAREWELL.COM"], "CHILD CARE")
        self.assertEqual(rows["CAREWELL.COM 555-010-311SPRINGFIELD"], "CHILD CARE")
        self.assertEqual(rows["NORTHWIND MFG PAYROLL~ DDIR"], "INCOME")
        self.assertEqual(rows["CAREVANE SYSTEMS DIRECT PAY"], "GENERAL_SERVICES")
        self.assertEqual(rows["BOREALIS HEALTH CARE"], "MEDICAL")


class MerchantFamilyMatchesThePagesOwnPredicate(unittest.TestCase):
    """The descriptors a bulk write covers are the ones the Bills page's
    own match covers — a phrase merchant refuses the lookalike ('Bay
    Mobile' is not T-Mobile), a rebrand's alternatives cover both names,
    and a token merchant needs every token as a WHOLE word ('maplehurst'
    is not 'maplehursts'). Where that match is evaluated — row by row in Python,
    or once in the database — must never change which rows it names."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _family(self, payee):
        return set(data._token_family(self.conn, payee))

    def test_a_phrase_merchant_refuses_the_lookalike(self):
        add_bill(self.conn, "Bay Mobile", 35.0, merchant="bay mobile")
        add_txn(self.conn, TODAY, 35.0, "BAY MOBILE 844-555-0147",
                merchant="BAY MOBILE PMT")
        add_txn(self.conn, TODAY, 90.0, "T-MOBILE #4432", merchant="T-Mobile")
        add_txn(self.conn, TODAY, 12.0, "PLUS MOBILE", merchant="Plus Mobile")
        self.assertEqual(self._family("Bay Mobile"), {"BAY MOBILE PMT"})

    def test_alternatives_cover_a_rebrand_and_nothing_adjacent(self):
        add_bill(self.conn, "Volari", 20.0, merchant="volari|vantis")
        add_txn(self.conn, TODAY, 20.0, "VANTIS SYSTEMS 415-555-1212",
                merchant="VANTIS SYSTEMS")
        add_txn(self.conn, TODAY, 20.0, "VOLARI AI SUB", merchant="VOLARI AI SUB")
        add_txn(self.conn, TODAY, 75.0, "VANTAGE MINING CO",
                merchant="Vantage Mining")
        self.assertEqual(self._family("Volari"),
                         {"VANTIS SYSTEMS", "VOLARI AI SUB"})

    def test_a_token_is_a_whole_word_not_a_prefix(self):
        add_txn(self.conn, TODAY, 8.0, "MAPLEHURST BAKERY #12",
                merchant="MAPLEHURST BAKERY #12")
        add_txn(self.conn, TODAY, 8.0, "MAPLEHURSTS BAKERY",
                merchant="MAPLEHURSTS BAKERY")
        add_txn(self.conn, TODAY, 8.0, "MAPLEHURST FARMS",
                merchant="MAPLEHURST FARMS")
        add_txn(self.conn, TODAY, 8.0, "CITY BAKERY", merchant="CITY BAKERY")
        self.assertEqual(self._family("Maplehurst Bakery"),
                         {"MAPLEHURST BAKERY #12"})

    def test_the_family_agrees_with_the_page_matcher_row_by_row(self):
        """The bills page tests one row at a time with
        budget.merchant_matcher; the ledger-wide answer must be the set of
        rows that predicate accepts, with nothing added or dropped."""
        from oikonome.engine import budget
        add_bill(self.conn, "Bay Mobile", 35.0, merchant="bay mobile")
        for name, merch in [("BAY MOBILE 844-555-0147", "BAY MOBILE PMT"),
                            ("T-MOBILE #4432", "T-Mobile"),
                            ("BAYMOBILE.COM", "BAYMOBILE.COM"),
                            ("BAY MOBILE STORE", "BAY MOBILE STORE"),
                            ("PLUS MOBILE", "Plus Mobile")]:
            add_txn(self.conn, TODAY, 35.0, name, merchant=merch)
        match = budget.merchant_matcher("bay mobile",
                                        budget._key_token("Bay Mobile"))
        expect = {r["m"] for r in self.conn.execute(
            "SELECT COALESCE(merchant_name, name) AS m, name "
            "FROM transactions WHERE removed = 0").fetchall()
            if match(f"{r['m'] or ''} {r['name'] or ''}".lower())}
        self.assertEqual(self._family("Bay Mobile"), expect)
        self.assertIn("BAY MOBILE PMT", expect)

    def test_a_payee_with_no_matchable_tokens_covers_only_itself(self):
        add_txn(self.conn, TODAY, 8.0, "CITY BAKERY", merchant="CITY BAKERY")
        self.assertEqual(self._family("A&W"), {"A&W"})


class _CountingConn:
    """A connection that remembers how many rows its queries handed back —
    the difference between filtering in SQL and filtering in Python, stated
    as a number instead of a stopwatch."""

    def __init__(self, conn):
        self._conn = conn
        self.rows = 0

    def execute(self, *args, **kwargs):
        cur = self._conn.execute(*args, **kwargs)
        self.rows += max(cur.rowcount, 0)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


class MerchantFamilyIsResolvedByTheDatabase(unittest.TestCase):
    """Naming a merchant's family is a question about the ledger, so the
    ledger answers it. Reading every distinct descriptor into the app and
    re-tokenizing each one there costs a whole-ledger transfer on every
    bulk preview — the preview a person waits on before each merchant-wide
    write, and the one query in it that grows with the ledger rather than
    with the merchant."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_wide_ledger_is_not_read_into_the_app(self):
        add_bill(self.conn, "Maplehurst Bakery", 8.0,
                 merchant="maplehurst bakery")
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, pending, removed, raw)
               SELECT 'wide' || g, 'card', %s, 8.0,
                      'SQ *STORE ' || g || ' PURCHASE AUTHORIZED CARD 7734',
                      'Sq Store Northwest Trading ' || g,
                      'GENERAL_MERCHANDISE', 0, 0, '{}'::jsonb
                 FROM generate_series(1, 20000) g""", (TODAY,))
        add_txn(self.conn, TODAY, 8.0, "MAPLEHURST BAKERY #12",
                merchant="MAPLEHURST BAKERY #12")
        counting = _CountingConn(self.conn)
        fam = data._token_family(counting, "Maplehurst Bakery")
        self.assertEqual(fam, ["MAPLEHURST BAKERY #12"])
        self.assertLess(counting.rows, 100,
                        f"{counting.rows} ledger rows crossed into Python to "
                        f"name one merchant's family")
