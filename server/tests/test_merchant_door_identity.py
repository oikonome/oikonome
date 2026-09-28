"""A door that says "this merchant" means the merchant the ROW is filed
under — never a string retyped from the row's columns.

Every surface names a merchant through merchant_sql: the identity key is
outlet-first and the display name resolves through the merchant row and the
alias mirror. A door that instead spells COALESCE(merchant_name, name) for
itself reads a DIFFERENT merchant for exactly the rows identity work has
touched — a chain's fuel arm (the outlet is the key), a renamed merchant
(the display is the new name), an aliased spelling (the canonical is the
bucket). The failure is never visible on a plain row, which is why each
test below seeds one of those three.

What breaks when a door drifts: "apply to whole merchant" recategorizes a
sibling merchant's rows and writes a rule under a name no row displays; the
Rules page reports zero transactions for a rule that is quietly moving
rows; and typing the payee shown on screen into a search box finds
nothing.
"""

import unittest

from oikonome.db import tenancy
from oikonome.engine import llm_categorize, merchant_dedup, merchant_sql
from oikonome.web import data

from .util import _ensure_db, add_txn, make_db, seed_accounts


def _set_outlet(conn, txn_id, outlet):
    """The fuel-arm splitter writes merchant_outlet at sync time; a test
    sets it directly — the split itself is not the subject here."""
    conn.execute("UPDATE transactions SET merchant_outlet=%s WHERE id=%s",
                 (outlet, txn_id))


def _displayed_under(conn, display):
    """The rows a merchant's page shows — the display layer's own join."""
    return {r["id"] for r in conn.execute(
        f"SELECT t.id FROM transactions t {merchant_dedup.MC_JOIN} "
        f" WHERE t.removed = 0 AND {merchant_dedup.DISPLAY_MERCHANT} = %s",
        (display,)).fetchall()}


def _categories(conn):
    """Every row's category as the ledger holds it — the merchant-wide
    teach and the single-row correction both count as a change."""
    return {r["id"]: (r["category_primary"], r["category_override"])
            for r in conn.execute("SELECT id, category_primary, "
                                  "category_override FROM transactions"
                                  ).fetchall()}


class ApplyToWholeMerchantFollowsTheRowsOwnMerchant(unittest.TestCase):
    """The ledger row's "apply to whole merchant" covers the rows shown
    beside it, and only those.

    The pump row displays as "Fuel Stop" (its outlet was renamed) while its
    aggregator name still says "Northwind Club" — the brand, whose
    warehouse row is a different merchant on every screen. A door keyed on
    the retyped name would recategorize the warehouse, leave the pump
    alone, and teach a rule for a merchant the pump does not display as.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.pump = add_txn(self.conn, "2026-07-08", 60,
                            "NORTHWIND CLUB GAS #0555",
                            merchant="Northwind Club",
                            primary="GENERAL_MERCHANDISE")
        _set_outlet(self.conn, self.pump, "Northwind Club Gas")
        self.other_pump = add_txn(self.conn, "2026-07-10", 51,
                                  "NORTHWIND CLUB GAS #0912",
                                  merchant="Northwind Club",
                                  primary="GENERAL_MERCHANDISE")
        _set_outlet(self.conn, self.other_pump, "Northwind Club Gas")
        self.warehouse = add_txn(self.conn, "2026-07-09", 120,
                                 "NORTHWIND CLUB WHSE #0555",
                                 merchant="Northwind Club",
                                 primary="GENERAL_MERCHANDISE")
        merchant_dedup.rename(self.conn, "Northwind Club Gas", "Fuel Stop")

    def test_the_teach_moves_the_rows_the_page_shows(self):
        before = _categories(self.conn)
        data.set_category(self.conn, self.pump, "TRANSPORTATION", scope="all")
        after = _categories(self.conn)
        changed = {i for i in after if after[i] != before.get(i)}
        self.assertEqual(changed, _displayed_under(self.conn, "Fuel Stop"))
        self.assertEqual(changed, {self.pump, self.other_pump})

    def test_the_sibling_merchant_is_left_alone(self):
        data.set_category(self.conn, self.pump, "TRANSPORTATION", scope="all")
        row = self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (self.warehouse,)).fetchone()
        self.assertEqual(row["category_primary"], "GENERAL_MERCHANDISE")

    def test_the_rule_is_written_for_the_name_the_row_displays(self):
        out = data.set_category(self.conn, self.pump, "TRANSPORTATION",
                                scope="all")
        self.assertEqual(out["merchant"], "Fuel Stop")
        rules = {r["merchant"] for r in self.conn.execute(
            "SELECT merchant FROM merchant_categories "
            "WHERE source='user'").fetchall()}
        self.assertEqual(rules, {"Fuel Stop"})

    def test_the_undo_snapshot_covers_only_this_merchant(self):
        # the corrected row itself carries a per-row override now, so the
        # snapshot is the REST of its merchant — the other pump, never the
        # brand's warehouse row
        out = data.set_category(self.conn, self.pump, "TRANSPORTATION",
                                scope="all")
        self.assertEqual([a["id"] for a in out["undo"]["affected"]],
                         [self.other_pump])

    def test_the_preview_count_equals_what_the_teach_does(self):
        # the row door's preview (family=False) is asked for the payee the
        # client holds, which is the displayed one
        preview = data.merchant_category_preview(
            self.conn, "Fuel Stop", "TRANSPORTATION", family=False)
        before = _categories(self.conn)
        data.set_category(self.conn, self.pump, "TRANSPORTATION", scope="all")
        after = _categories(self.conn)
        changed = {i for i in after if after[i] != before.get(i)}
        self.assertEqual(changed, _displayed_under(self.conn, "Fuel Stop"))
        self.assertEqual(preview["count"], len(changed))
        # the descriptor variants the confirm names are this merchant's own
        # key, never the parent brand's name off the aggregator column
        self.assertIn("Northwind Club Gas", preview["variants"])
        self.assertNotIn("Northwind Club", preview["variants"])


class HistoryPageBulkSetFollowsTheDisplayedMerchant(unittest.TestCase):
    """The merchant page's bulk set covers the same bucket.

    Its scope is the token FAMILY — every canonical the page's charge list
    matches — and the family is resolved by looking the rows' identity keys
    up in the alias map. Read off the aggregator column instead, a fuel
    arm's rows resolve their parent brand's canonical, which puts the whole
    brand in the family: the confirm counts it, the write moves it, and the
    undo restores rows the person never saw."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.pump = add_txn(self.conn, "2026-07-08", 60,
                            "NORTHWIND CLUB GAS #0555",
                            merchant="Northwind Club",
                            primary="GENERAL_MERCHANDISE")
        _set_outlet(self.conn, self.pump, "Northwind Club Gas")
        self.warehouse = add_txn(self.conn, "2026-07-09", 120,
                                 "NORTHWIND CLUB WHSE #0555",
                                 merchant="Northwind Club",
                                 primary="GENERAL_MERCHANDISE")
        merchant_dedup.rename(self.conn, "Northwind Club Gas", "Fuel Stop")

    def test_the_family_is_this_merchants_own_keys(self):
        self.assertEqual(data._family_canons(self.conn, "Fuel Stop"),
                         ["Fuel Stop"])

    def test_the_write_moves_the_displayed_bucket(self):
        before = _categories(self.conn)
        res = data.set_merchant_category(self.conn, "Fuel Stop",
                                         "TRANSPORTATION")
        after = _categories(self.conn)
        changed = {i for i in after if after[i] != before.get(i)}
        self.assertEqual(changed, _displayed_under(self.conn, "Fuel Stop"))
        self.assertEqual(res["count"], len(changed))
        self.assertEqual([a["id"] for a in res["undo"]["affected"]],
                         [self.pump])

    def test_the_confirm_counts_what_the_write_moves(self):
        preview = data.merchant_category_preview(self.conn, "Fuel Stop",
                                                 "TRANSPORTATION")
        res = data.set_merchant_category(self.conn, "Fuel Stop",
                                         "TRANSPORTATION")
        self.assertEqual(preview["count"], res["count"])
        self.assertEqual(preview["variants"], ["Northwind Club Gas"])


class ApplyToWholeMerchantWithNoAliasOfItsOwn(unittest.TestCase):
    """A row whose aggregator name happens to be a real OTHER merchant's
    name must not hand that merchant's rows to the teach.

    "Harbor Lights" is the brand on the pump row's aggregator name and also
    a shop the household actually visits. The pump displays as its outlet;
    the shop displays as itself. One teach, one merchant."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.pump = add_txn(self.conn, "2026-07-08", 45,
                            "HARBOR LIGHTS FUEL 21",
                            merchant="Harbor Lights",
                            primary="GENERAL_MERCHANDISE")
        _set_outlet(self.conn, self.pump, "Harbor Lights Fuel")
        self.shop = add_txn(self.conn, "2026-07-11", 22, "HARBOR LIGHTS",
                            merchant="Harbor Lights",
                            primary="GENERAL_MERCHANDISE")

    def test_the_other_merchant_keeps_its_category(self):
        before = _categories(self.conn)
        data.set_category(self.conn, self.pump, "TRANSPORTATION", scope="all")
        after = _categories(self.conn)
        changed = {i for i in after if after[i] != before.get(i)}
        self.assertEqual(changed,
                         _displayed_under(self.conn, "Harbor Lights Fuel"))
        self.assertNotIn(self.shop, changed)


class RulesPageCountIsWhatTheRuleReaches(unittest.TestCase):
    """The per-rule transaction count is the rows the rule actually
    categorizes.

    A rule is matched to a row by the row's merchant — the alias mirror's
    canonical, the outlet key underneath it — so a count that re-derives
    the row's merchant from merchant_name reports zero for a rule that is
    moving rows, and the person has no way to tell a live rule from a dead
    one."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.pump = add_txn(self.conn, "2026-07-08", 60,
                            "JUNIPER MARKET FUEL 7",
                            merchant="Juniper Market",
                            primary="GENERAL_MERCHANDISE")
        _set_outlet(self.conn, self.pump, "Juniper Market Fuel")
        merchant_dedup.rename(self.conn, "Juniper Market Fuel", "Juniper Pump")

    def _count_for(self, merchant):
        rules = data.list_rules(self.conn)["rules"]
        return next(r["count"] for r in rules if r["merchant"] == merchant)

    def test_a_rule_on_a_renamed_outlet_counts_its_rows(self):
        llm_categorize.upsert_user_rule(self.conn, "Juniper Pump",
                                        "TRANSPORTATION")
        moved = llm_categorize.apply(self.conn, canon="Juniper Pump")
        self.assertEqual(moved, 1, "the rule must reach the pump row")
        self.assertEqual(self._count_for("Juniper Pump"), 1)

    def test_a_rule_keyed_on_a_raw_spelling_counts_the_merchants_rows(self):
        # a machine rule cached under one raw spelling of the merchant
        # still resolves through the alias mirror when it is applied, so
        # the count has to resolve it the same way
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES (%s,'TRANSPORTATION','llm')",
            ("Juniper Market Fuel",))
        moved = llm_categorize.apply(self.conn, canon="Juniper Pump")
        self.assertEqual(moved, 1, "the rule must reach the pump row")
        self.assertEqual(self._count_for("Juniper Market Fuel"), 1)

    def test_a_rule_no_row_answers_to_counts_nothing(self):
        llm_categorize.upsert_user_rule(self.conn, "Nobody Here", "MEDICAL")
        self.assertEqual(self._count_for("Nobody Here"), 0)


class ReimbursementSearchReadsTheDisplayedPayee(unittest.TestCase):
    """Typing what the screen says finds the row.

    The candidate list shows the display merchant, so the filter has to
    match it — a filter over the aggregator name answers nothing for a
    renamed merchant, and the person concludes the deposit is not there.
    The raw bank descriptor stays matchable too: a statement is the other
    thing people type."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.charge = add_txn(self.conn, "2026-07-01", 80.0, "CLINIC VISIT",
                              account="card", primary="MEDICAL")
        self.deposit = add_txn(self.conn, "2026-07-06", -80.0,
                               "HARBOR HEALTH REIMB CR",
                               merchant="Harbor Health", account="chk",
                               primary="INCOME")
        merchant_dedup.rename(self.conn, "Harbor Health", "Springfield Clinic")

    def _ids(self, q):
        _anchor, rows = data.reimbursement_candidates(self.conn, self.charge,
                                                      q=q)
        return [r["id"] for r in rows]

    def test_the_name_on_the_row_finds_it(self):
        self.assertEqual(self._ids("springfield"), [self.deposit])

    def test_the_bank_descriptor_still_finds_it(self):
        self.assertEqual(self._ids("reimb cr"), [self.deposit])

    def test_a_literal_percent_is_not_a_wildcard(self):
        self.assertEqual(self._ids("100%"), [])


class TheMatchedPairsListNamesBothSidesTheSameWay(unittest.TestCase):
    """The undo list is the only place a wrong match is visible, so both
    sides read as the ledger reads them — a deposit named by its raw
    descriptor is a pair the person cannot recognize."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.charge = add_txn(self.conn, "2026-07-01", 80.0, "CLINIC VISIT",
                              account="card", primary="MEDICAL")
        self.deposit = add_txn(self.conn, "2026-07-06", -80.0,
                               "HARBOR HEALTH REIMB CR",
                               merchant="Harbor Health", account="chk",
                               primary="INCOME")
        merchant_dedup.rename(self.conn, "Harbor Health", "Springfield Clinic")
        data.link_reimbursement(self.conn, self.charge, self.deposit)

    def test_the_deposit_is_named_as_the_ledger_names_it(self):
        pair = data.recent_reimbursement_pairs(self.conn)[0]
        self.assertEqual(pair["deposit_payee"], "Springfield Clinic")


class TheBillMerchantPickerOffersLedgerNames(unittest.TestCase):
    """A bill's merchant chip is checked against the merchants table, so
    the picker offers the names the ledger displays — a fuel arm under its
    own name, not its parent brand's, which the bill would never match."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid

        from fastapi.testclient import TestClient

        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"pick-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            pump = add_txn(conn, "2026-07-08", 60, "JUNIPER MARKET FUEL 7",
                           merchant="Juniper Market")
            _set_outlet(conn, pump, "Juniper Market Fuel")
            merchant_dedup.rename(conn, "Juniper Market Fuel", "Juniper Pump")
            # a row the identity pass has not settled yet: the alias mirror
            # names its merchant, no merchant row is attached. Such a row
            # displays through the mirror — which is keyed on the outlet,
            # so a picker keyed on the aggregator name offers the parent
            # brand instead.
            conn.execute("UPDATE transactions SET merchant_id=NULL "
                         "WHERE id=%s", (pump,))
        finally:
            conn.close()

    def test_the_outlet_is_offered_under_the_name_it_displays_as(self):
        names = self.client.get("/api/merchants").json()["merchants"]
        self.assertIn("Juniper Pump", names)
        self.assertNotIn("Juniper Market", names)


class IdentityKeyHasOneSpelling(unittest.TestCase):
    """The doors under test read the key through merchant_sql, so a change
    to what the key is reaches them; a retyped chain would not."""

    def test_the_key_is_outlet_first(self):
        self.assertTrue(
            merchant_sql.raw_key("t").startswith("COALESCE(t.merchant_outlet"))


if __name__ == "__main__":
    unittest.main()
