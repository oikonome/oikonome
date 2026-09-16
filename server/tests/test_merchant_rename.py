"""A person can correct what a merchant is called, and undo it.

Every automatic naming layer in this app is deliberately conservative, and
the reason was always the same: a wrong answer was unfixable by the person
looking at it. Correction is what makes the rest safe to relax.

Two properties carry the weight. A user's word outranks every automatic
layer — layer1 recomputes each sync and must not walk over it. And the
correction is itself undoable, restoring exactly what was there before,
including the case where nothing was.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import merchant_dedup

from .util import _ensure_db, add_txn, make_db, seed_accounts


class RenameAndMerge(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _displayed(self):
        rows = self.conn.execute(
            f"""SELECT {merchant_dedup.DISPLAY_MERCHANT} AS d, count(*) AS n
                  FROM transactions t {merchant_dedup.MC_JOIN}
                 WHERE t.removed=0 GROUP BY 1""").fetchall()
        return {r["d"]: r["n"] for r in rows}

    def test_renaming_moves_every_row_under_that_name(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        add_txn(self.conn, "2026-08-02", 30, "Cedar Lane Bakery")
        out = merchant_dedup.rename(self.conn, "Cedar Lane Bakery",
                                    "Cedar Lane Bakery Co")
        self.assertEqual(out, {"raws": 1, "rows": 2})
        self.assertEqual(self._displayed(), {"Cedar Lane Bakery Co": 2})

    def test_merging_is_the_same_act_as_renaming(self):
        # two spellings of one bakery, which is what an aggregator that
        # cannot resolve a small merchant produces over time
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        add_txn(self.conn, "2026-08-05", 40, "cedar lane bakery co")
        merchant_dedup.rename(self.conn, "cedar lane bakery co",
                              "Cedar Lane Bakery")
        self.assertEqual(self._displayed(), {"Cedar Lane Bakery": 2})

    def test_the_users_word_outranks_the_automatic_layers(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        merchant_dedup.rename(self.conn, "Cedar Lane Bakery", "Cedar Lane Bakehouse")
        row = self.conn.execute(
            "SELECT method FROM merchant_canonical "
            "WHERE raw_merchant='Cedar Lane Bakery'").fetchone()
        self.assertEqual(row["method"], "manual")
        # layer1 runs every sync and must not walk over a manual row
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._displayed(), {"Cedar Lane Bakehouse": 1})

    def test_an_empty_name_is_refused(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        with self.assertRaises(ValueError):
            merchant_dedup.rename(self.conn, "Cedar Lane Bakery", "   ")

    def test_renaming_something_that_is_not_there_moves_nothing(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        self.assertEqual(merchant_dedup.rename(self.conn, "Nobody", "X"),
                         {"raws": 0, "rows": 0})

    def test_renaming_to_an_existing_name_merges_regardless_of_case(self):
        # the UI promises that renaming to an existing name merges the two;
        # typing "northwind club" when "Northwind Club" exists must land in that bucket,
        # not fork a second one differing only in case
        add_txn(self.conn, "2026-08-01", 20, "Northwind Club")
        add_txn(self.conn, "2026-08-02", 30, "Tidewell")
        merchant_dedup.rename(self.conn, "Tidewell", "northwind club")
        self.assertEqual(self._displayed(), {"Northwind Club": 2})

    def test_ambiguous_existing_casings_keep_the_name_as_typed(self):
        # when several distinct casings already exist there is no right one
        # to guess, so the exact string the user typed wins
        add_txn(self.conn, "2026-08-01", 20, "Northwind Club")
        add_txn(self.conn, "2026-08-02", 30, "NORTHWIND CLUB")
        add_txn(self.conn, "2026-08-03", 40, "Tidewell")
        merchant_dedup.rename(self.conn, "Tidewell", "northwind club")
        self.assertEqual(self._displayed(),
                         {"Northwind Club": 1, "NORTHWIND CLUB": 1, "northwind club": 1})

    def test_recasing_a_merchant_keeps_the_casing_the_user_typed(self):
        # the merchant being renamed must not count as its own merge target,
        # or a deliberate re-case would snap back to the old spelling
        add_txn(self.conn, "2026-08-01", 20, "Northwind Club")
        merchant_dedup.rename(self.conn, "Northwind Club", "NORTHWIND CLUB")
        self.assertEqual(self._displayed(), {"NORTHWIND CLUB": 1})


class Undo(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _shown(self, raw):
        return self.conn.execute(
            "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s",
            (raw,)).fetchone()

    def _pinned(self, raw):
        """A person's mapping for this raw string, or None when layer1 owns
        it again (the resolver may hold a layer1 alias row for the merchant
        id — that is recomputed every sync, which is the point)."""
        return self.conn.execute(
            "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s "
            "AND method IN ('manual','llm')", (raw,)).fetchone()

    def _last_change(self):
        return self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]

    def test_undo_restores_a_previous_mapping(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery Downtown")
        merchant_dedup.rename(self.conn, "Cedar Lane Bakery Downtown", "Cedar Lane Bakery")
        merchant_dedup.rename(self.conn, "Cedar Lane Bakery", "Cedar Bakes")
        merchant_dedup.undo(self.conn, self._last_change())
        self.assertEqual(self._shown("Cedar Lane Bakery Downtown")["canonical"],
                         "Cedar Lane Bakery")

    def test_undoing_a_first_mapping_removes_the_row_entirely(self):
        # nothing had mapped this string, so the way back is no row at all —
        # writing an empty one would take the case away from layer1, which
        # owns it and recomputes it every sync
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        merchant_dedup.rename(self.conn, "Cedar Lane Bakery", "Cedar Bakes")
        merchant_dedup.undo(self.conn, self._last_change())
        self.assertIsNone(self._pinned("Cedar Lane Bakery"))

    def test_the_journal_records_both_ends_of_the_change(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        merchant_dedup.rename(self.conn, "Cedar Lane Bakery", "Cedar Bakes")
        row = self.conn.execute(
            "SELECT raw_merchant, from_canonical, to_canonical "
            "FROM merchant_renames").fetchone()
        self.assertEqual(row["raw_merchant"], "Cedar Lane Bakery")
        self.assertIsNone(row["from_canonical"])
        self.assertEqual(row["to_canonical"], "Cedar Bakes")

    def test_undoing_a_change_that_does_not_exist_is_an_error(self):
        with self.assertRaises(LookupError):
            merchant_dedup.undo(self.conn, 999999)

    def test_undoing_a_superseded_change_is_refused(self):
        # a journal entry is only reversible while it is still the live
        # mapping — undoing an old one would trample what a later rename
        # wrote. Newest-first the chain still unwinds completely.
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery Downtown")
        merchant_dedup.rename(self.conn, "Cedar Lane Bakery Downtown",
                              "Cedar Lane Bakery")
        first = self._last_change()
        merchant_dedup.rename(self.conn, "Cedar Lane Bakery", "Cedar Bakes")
        mid = self._last_change()
        merchant_dedup.rename(self.conn, "Cedar Bakes", "Cedar Pastry")
        last = self._last_change()
        for stale in (first, mid):
            with self.assertRaises(LookupError):
                merchant_dedup.undo(self.conn, stale)
        # the refusals left the live mapping untouched
        self.assertEqual(self._shown("Cedar Lane Bakery Downtown")["canonical"],
                         "Cedar Pastry")
        # undone newest-first, every entry becomes reversible in turn —
        # including the first-mapping delete, which may only remove the row
        # while its own write is still current
        merchant_dedup.undo(self.conn, last)
        self.assertEqual(self._shown("Cedar Lane Bakery Downtown")["canonical"],
                         "Cedar Bakes")
        merchant_dedup.undo(self.conn, mid)
        self.assertEqual(self._shown("Cedar Lane Bakery Downtown")["canonical"],
                         "Cedar Lane Bakery")
        merchant_dedup.undo(self.conn, first)
        self.assertIsNone(self._pinned("Cedar Lane Bakery Downtown"))


class Listing(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_merchant_carries_the_raw_strings_underneath_it(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        add_txn(self.conn, "2026-08-05", 40, "cedar lane bakery co")
        merchant_dedup.rename(self.conn, "cedar lane bakery co",
                              "Cedar Lane Bakery")
        row = [r for r in merchant_dedup.listing(self.conn)
               if r["display"] == "Cedar Lane Bakery"][0]
        self.assertEqual(row["rows"], 2)
        self.assertEqual(sorted(row["variants"]),
                         ["Cedar Lane Bakery", "cedar lane bakery co"])

    def test_search_treats_wildcards_as_literal_text(self):
        # "%" and "_" are LIKE metacharacters; a search for them must find
        # merchants containing those characters, not match everything
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        add_txn(self.conn, "2026-08-02", 30, "100% Juice")
        add_txn(self.conn, "2026-08-03", 40, "SNAP_FIT Gym")
        pct = merchant_dedup.listing(self.conn, q="%")
        self.assertEqual([r["display"] for r in pct], ["100% Juice"])
        und = merchant_dedup.listing(self.conn, q="_")
        self.assertEqual([r["display"] for r in und], ["SNAP_FIT Gym"])
        # plain substring search is unaffected
        sub = merchant_dedup.listing(self.conn, q="juice")
        self.assertEqual([r["display"] for r in sub], ["100% Juice"])

    def test_business_rows_are_out_of_the_money_but_the_merchant_is_listed(self):
        # the merchants screen is a personal-ledger surface for MONEY: rows
        # assigned to a business entity stay out of its counts and totals by
        # the same predicate every personal report uses. The merchant itself
        # is still listed — a name the catalog hides is a name nobody can
        # correct, while rename can still reach it.
        add_txn(self.conn, "2026-08-01", 20, "Office Depot")
        t2 = add_txn(self.conn, "2026-08-02", 500, "Office Depot")
        t3 = add_txn(self.conn, "2026-08-03", 900, "Acme Payroll")
        from oikonome.engine import entities
        eid = entities.create_entity(self.conn, name="Side Biz",
                                     structure="sole_prop")["id"]
        for t in (t2, t3):
            self.conn.execute(
                "UPDATE transactions SET entity_id=%s WHERE id=%s", (eid, t))
        rows = {r["display"]: r for r in merchant_dedup.listing(self.conn)}
        self.assertEqual(rows["Office Depot"]["rows"], 1)
        self.assertEqual(float(rows["Office Depot"]["total"]), 20.0)
        self.assertEqual(rows["Office Depot"]["business_rows"], 1)
        # a business-only merchant is visible, with no personal money on it
        self.assertIn("Acme Payroll", rows)
        self.assertEqual(rows["Acme Payroll"]["rows"], 0)
        self.assertEqual(float(rows["Acme Payroll"]["total"]), 0.0)
        self.assertEqual(rows["Acme Payroll"]["business_rows"], 1)

    def test_the_catalog_accounts_for_every_row_a_rename_would_move(self):
        # the seam this pins: a rename moves every row of a merchant, so the
        # count it reports must be reconstructable from the catalog. Personal
        # + business rows is that number — the catalog can never show fewer
        # than the rename claims without the difference being on screen.
        add_txn(self.conn, "2026-08-01", 20, "Office Depot")
        t2 = add_txn(self.conn, "2026-08-02", 500, "Office Depot")
        from oikonome.engine import entities
        eid = entities.create_entity(self.conn, name="Side Biz",
                                     structure="sole_prop")["id"]
        self.conn.execute(
            "UPDATE transactions SET entity_id=%s WHERE id=%s", (eid, t2))
        row = {r["display"]: r
               for r in merchant_dedup.listing(self.conn)}["Office Depot"]
        moved = merchant_dedup.rename(self.conn, "Office Depot", "Staples")
        self.assertEqual(moved["rows"], row["rows"] + row["business_rows"])
        # and the move really was global — nothing left behind
        self.assertEqual([r["display"] for r in
                          merchant_dedup.listing(self.conn)], ["Staples"])

    def test_alpha_order_reaches_the_tail_that_count_order_cuts(self):
        # count-order plus a cap cuts exactly the low-count merchants most
        # likely to need correction; alpha order walks them
        for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
            add_txn(self.conn, d, 20, "Zebra Coffee")
        add_txn(self.conn, "2026-08-04", 30, "Aardvark Books")
        by_count = merchant_dedup.listing(self.conn, limit=1)
        self.assertEqual([r["display"] for r in by_count], ["Zebra Coffee"])
        by_alpha = merchant_dedup.listing(self.conn, limit=1, order="alpha")
        self.assertEqual([r["display"] for r in by_alpha],
                         ["Aardvark Books"])
        with self.assertRaises(ValueError):
            merchant_dedup.listing(self.conn, order="rows; DROP TABLE")

    def test_search_matches_are_not_starved_by_the_limit(self):
        # the limit applies after the filter, so a search finds a long-tail
        # merchant even when busier ones would fill the cap
        for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
            add_txn(self.conn, d, 20, "Zebra Coffee")
        add_txn(self.conn, "2026-08-04", 30, "Aardvark Books")
        rows = merchant_dedup.listing(self.conn, q="aardvark", limit=1)
        self.assertEqual([r["display"] for r in rows], ["Aardvark Books"])

    def test_variants_are_capped_per_merchant(self):
        # a payee with thousands of raw descriptor strings must not bloat
        # the payload; the row still reports every transaction it covers
        mapping = {f"ACME STORE {i:03d}": "Acme" for i in range(60)}
        for i, raw in enumerate(mapping):
            add_txn(self.conn, "2026-08-01", 10, raw)
        merchant_dedup.load_llm_map(self.conn, mapping)
        row = [r for r in merchant_dedup.listing(self.conn)
               if r["display"] == "Acme"][0]
        self.assertEqual(row["rows"], 60)
        self.assertEqual(len(row["variants"]), 50)

    def test_the_variant_count_is_the_true_total_not_the_capped_list(self):
        # both clients print "{variants.length} source names" as a fact; at
        # the cap that sentence was a lie, so the true count travels with it
        mapping = {f"ACME STORE {i:03d}": "Acme" for i in range(60)}
        for raw in mapping:
            add_txn(self.conn, "2026-08-01", 10, raw)
        merchant_dedup.load_llm_map(self.conn, mapping)
        row = [r for r in merchant_dedup.listing(self.conn)
               if r["display"] == "Acme"][0]
        self.assertEqual(len(row["variants"]), 50)
        self.assertEqual(row["variant_count"], 60)

    def test_a_merchant_under_the_cap_counts_its_variants_exactly(self):
        add_txn(self.conn, "2026-08-01", 20, "Cedar Lane Bakery")
        add_txn(self.conn, "2026-08-05", 40, "cedar lane bakery co")
        merchant_dedup.rename(self.conn, "cedar lane bakery co",
                              "Cedar Lane Bakery")
        row = [r for r in merchant_dedup.listing(self.conn)
               if r["display"] == "Cedar Lane Bakery"][0]
        self.assertEqual(row["variant_count"], 2)
        self.assertEqual(row["variant_count"], len(row["variants"]))


class CatalogOverHttp(unittest.TestCase):
    """The sort the screen offers has to survive the trip to the database.

    The endpoint must pass `order` through to the engine; a dropped
    parameter leaves the screen's sort control silently inert. An
    engine-level test cannot see that, so this one goes through the door
    the client uses.
    """

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        email = f"merch-{uuid.uuid4().hex[:10]}@example.dev"
        r = cls.client.post("/api/signup",
                            data={"email": email,
                                  "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
                add_txn(conn, d, 20, "Zebra Coffee")
            add_txn(conn, "2026-08-04", 30, "Aardvark Books")
        finally:
            conn.close()

    def _names(self, **params):
        r = self.client.get("/api/merchants/catalog", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return [m["display"] for m in r.json()["merchants"]]

    def test_the_sort_order_reaches_the_query(self):
        # with the cap at one row the two orders cannot look alike, which is
        # what makes a dropped parameter visible
        self.assertEqual(self._names(limit=1), ["Zebra Coffee"])
        self.assertEqual(self._names(limit=1, order="count"),
                         ["Zebra Coffee"])
        self.assertEqual(self._names(limit=1, order="alpha"),
                         ["Aardvark Books"])

    def test_an_unknown_sort_order_is_refused_as_a_bad_request(self):
        # the engine raises ValueError on an unknown sort; unhandled that is
        # a 500 for what is plainly a bad request
        for bad in ("rows; DROP TABLE", "", "COUNT", "1"):
            r = self.client.get("/api/merchants/catalog",
                                params={"order": bad})
            self.assertEqual(r.status_code, 400, f"order={bad!r}: {r.text}")

    def test_the_catalog_carries_the_true_variant_count(self):
        row = [m for m in self.client.get("/api/merchants/catalog").json()
               ["merchants"] if m["display"] == "Zebra Coffee"][0]
        self.assertEqual(row["variant_count"], 1)
        self.assertEqual(row["rows"], 3)


if __name__ == "__main__":
    unittest.main()
