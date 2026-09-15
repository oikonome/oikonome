"""A bill pays its merchants, not any row whose letters resemble them.

A bill's merchant used to be stored as text and matched by tokens
("northwind headquarters" needs both words in the row). When the aggregator
later resolves the payee to an entity and starts sending plain "Northwind",
or the person merges two spellings on the Merchants page, the new charges
display under the same merchant as the old ones but no longer carry the
bill's tokens — and the bill silently stopped matching: history stopped at
the last old-spelled charge, the Bills page called the month unpaid, the
ledger lost its ⟳ pill, while the Merchants page counted the charge.

The invariants now:

* A bill carries an IDENTITY — the merchants it pays, by merchant id
  (raw.merchant_refs, budget.bill_merchant_ids) — and every surface that
  asks "does this row pay this bill?" answers by that identity alone: the
  row's merchant_id is one of the bill's. Text is the rule only for a bill
  that has no identity and whose list no person has written; an identity
  that resolves to nothing matches nothing, never the letters again.
* A new bill's identity is bootstrapped from the ledger (budget.bill_displays:
  the merchants its words name on the ledger's own authority); the nightly
  pass OFFERS a name the ledger newly files the payee under as an approve-
  gated proposal, so a rename never silently breaks a bill and never
  silently widens it.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget
from oikonome.web import data
from oikonome.web.api import _month_bill_stats, _stamp_recurring

from .util import TODAY, add_bill, add_txn, make_db, write_config

PAYEE = "Northwind Headquarters"
OLD_NAME = "NORTHWIND* SUITE SUB"
NEW_NAME = "NORTHWIND* SUITE SUB SEATTLE USA"


def _bill(conn, **kw):
    # the bill's merchant is its full canonical text, as detection and the
    # feed backfill store it: both words are required of a row
    return add_bill(conn, PAYEE, 120.0, frequency="MONTHLY",
                    next_due=TODAY - dt.timedelta(days=4),
                    last_seen=TODAY - dt.timedelta(days=34),
                    merchant=PAYEE.lower(), **kw)


def _google_bill(conn, **kw):
    # a one-word bill: its words are in every longer sibling's rows too
    return add_bill(conn, "Google", 20.0, frequency="MONTHLY",
                    next_due=TODAY - dt.timedelta(days=4),
                    merchant="google", **kw)


def _names(conn, payee=PAYEE):
    return conn.execute("SELECT raw FROM bills WHERE payee=%s",
                        (payee,)).fetchone()["raw"].get("merchant_names")


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # two charges spelled the old way, an even older spelling, then the
        # renamed one on the due date: same card, same amount, aggregator
        # now says "Northwind"
        self.old = [
            add_txn(self.conn, TODAY - dt.timedelta(days=64), 120.0, OLD_NAME,
                    merchant=PAYEE, primary="GENERAL_SERVICES"),
            add_txn(self.conn, TODAY - dt.timedelta(days=34), 120.0, OLD_NAME,
                    merchant=PAYEE, primary="GENERAL_SERVICES"),
        ]
        self.older = [
            add_txn(self.conn, TODAY - dt.timedelta(days=94 + 30 * i), 60.0,
                    "SUITEHUB SUBSCRIPTION", merchant="Suitehub",
                    primary="GENERAL_SERVICES")
            for i in range(4)]
        self.new = add_txn(self.conn, TODAY - dt.timedelta(days=4), 120.0,
                           NEW_NAME, merchant="Northwind",
                           primary="GENERAL_SERVICES")
        # a look-alike the ledger shows as its own merchant, whose text
        # carries every word of the bill
        self.other = add_txn(self.conn, TODAY - dt.timedelta(days=3), 120.0,
                             "NORTHWIND HEADQUARTERS CAFE",
                             merchant="Northwind Labs", primary="FOOD_AND_DRINK")
        # every row has a merchant row (the resolver gives it one on ingest):
        # the old spelling is its own layer1 merchant until the merge
        self.hq = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Northwind Headquarters','layer1') RETURNING id::text AS id"
        ).fetchone()["id"]
        self.conn.execute(
            "UPDATE transactions SET merchant_id=%s WHERE id = ANY(%s)",
            (self.hq, self.old))

    def tearDown(self):
        self.conn.close()

    def _merge(self):
        """After the rename: the old rows' merchant renamed to "Northwind"
        by hand (a manual merchant row) and the aggregator's "Northwind"
        entity attached to that same row (the resolver adopts a same-named
        manual merchant rather than minting a twin), the new row resolved
        to it. All DISPLAY as "Northwind"; the look-alike is its own
        merchant."""
        manual = self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id) VALUES "
            "('Northwind','manual','ent-northwind') RETURNING id").fetchone()["id"]
        labs = self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id) VALUES "
            "('Northwind Labs','plaid','ent-labs') RETURNING id").fetchone()["id"]
        self.conn.execute(
            "UPDATE transactions SET merchant_id=%s WHERE id = ANY(%s)",
            (manual, self.old + self.older + [self.new]))
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (labs, self.other))


class BillIdentityIsTheRule(_Ledger):
    def setUp(self):
        super().setUp()
        self._merge()
        _bill(self.conn)

    def test_a_new_bill_is_bootstrapped_with_its_merchants(self):
        self.assertEqual(_names(self.conn), ["Northwind"])

    def test_history_lists_the_merchants_rows_and_nothing_else(self):
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        ids = {t["txn_id"] for t in hist["txns"]}
        self.assertIn(self.new, ids)
        self.assertTrue(set(self.old) <= ids)
        # every word of the bill is in this row's text; it is another
        # merchant's, so it is not the bill's
        self.assertNotIn(self.other, ids)

    def test_health_sees_the_renamed_charge(self):
        h = bills.analyze_bills(self.conn, TODAY, payee=PAYEE)[PAYEE]
        self.assertEqual(h["last_match"], TODAY - dt.timedelta(days=4))

    def test_month_verdict_counts_it_paid(self):
        cfg = budget.load_config(self.conn)
        st = _month_bill_stats(self.conn, cfg, TODAY)[PAYEE]
        self.assertEqual(st["paid"], 1)
        self.assertAlmostEqual(st["paid_amount"], 120.0, places=2)

    def test_ledger_pill_follows_the_identity(self):
        mid = {r["id"]: r["merchant_id"] for r in self.conn.execute(
            "SELECT id, merchant_id::text AS merchant_id FROM transactions "
            "WHERE id IN (%s, %s)", (self.new, self.other)).fetchall()}
        rows = [{"id": self.new, "payee": "Northwind", "amount": 120.0,
                 "merchant_id": mid[self.new]},
                {"id": self.other, "payee": "Northwind Labs", "amount": 120.0,
                 "merchant_id": mid[self.other]}]
        _stamp_recurring(self.conn, rows)
        self.assertEqual(rows[0]["recurring_bill"], PAYEE)
        self.assertIsNone(rows[1]["recurring_bill"])

    def test_bill_category_stamps_its_merchants_rows(self):
        _bill(self.conn, txn_category="ENTERTAINMENT")
        bills.apply_txn_categories(self.conn)
        got = {r["id"]: r["category_override"] for r in self.conn.execute(
            "SELECT id, category_override FROM transactions").fetchall()}
        self.assertEqual(got[self.new], "ENTERTAINMENT")
        self.assertEqual(got[self.old[0]], "ENTERTAINMENT")
        self.assertIsNone(got[self.other])

    def test_bulk_recategorize_scope_matches_the_page(self):
        fam = data._token_family(self.conn, PAYEE)
        self.assertIn("Northwind", fam)
        self.assertIn(PAYEE, fam)
        self.assertNotIn("Northwind Labs", fam)

    def test_a_list_the_person_emptied_is_not_refilled(self):
        _bill(self.conn, merchants=[])
        self.assertEqual(_names(self.conn), [])
        # none on purpose: nothing matches, not even the letters
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertEqual(hist["txns"], [])
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["bootstrapped"], 0)
        self.assertEqual(_names(self.conn), [])
        # the ledger may still OFFER; the person decides
        self.assertEqual(st["proposed_merchant"], 1)

    def test_a_same_named_twin_is_not_the_bill(self):
        """Two live merchants both named "Northwind": the bill's identity
        is one id; the twin's rows are not its charges on any surface —
        history, its SQL narrowing, the ledger pill, the category stamp,
        the bulk-recategorize scope."""
        twin = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Northwind','layer1') RETURNING id::text AS id").fetchone()["id"]
        stray = add_txn(self.conn, TODAY - dt.timedelta(days=1), 120.0,
                        "NORTHWIND* SUITE SUB", merchant="Northwind",
                        primary="GENERAL_SERVICES")
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (twin, stray))
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        ids = {t["txn_id"] for t in hist["txns"]}
        self.assertIn(self.new, ids)
        self.assertNotIn(stray, ids)
        rows = [{"id": stray, "payee": "Northwind", "amount": 120.0,
                 "merchant_id": twin}]
        _stamp_recurring(self.conn, rows)
        self.assertIsNone(rows[0]["recurring_bill"])
        _bill(self.conn, txn_category="ENTERTAINMENT")
        bills.apply_txn_categories(self.conn)
        got = {r["id"]: r["category_override"] for r in self.conn.execute(
            "SELECT id, category_override FROM transactions").fetchall()}
        self.assertEqual(got[self.new], "ENTERTAINMENT")
        self.assertIsNone(got[stray])
        self.assertNotIn(stray, {r["id"] for r in self.conn.execute(
            "SELECT t.id FROM transactions t WHERE "
            + data._page_match_sql(self.conn, PAYEE)[0],
            data._page_match_sql(self.conn, PAYEE)[1]).fetchall()})

    def test_the_person_sets_the_identity_on_the_form(self):
        _bill(self.conn, merchants=["Northwind", "Northwind Labs"])
        self.assertEqual(_names(self.conn), ["Northwind", "Northwind Labs"])
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertIn(self.other, {t["txn_id"] for t in hist["txns"]})
        # an edit that says nothing about merchants keeps them
        _bill(self.conn, txn_category="ENTERTAINMENT")
        self.assertEqual(_names(self.conn), ["Northwind", "Northwind Labs"])


class IdentityFollowsTheMerchantRow(_Ledger):
    """The stored id is the key: a rename or merge of the merchant on the
    Merchants page carries the bill without an offer; the name is only
    the fallback for a bill that arrived from another instance."""

    def setUp(self):
        super().setUp()
        self._merge()
        _bill(self.conn)
        self.manual = self.conn.execute(
            "SELECT id::text AS id FROM merchants WHERE name_source='manual'"
        ).fetchone()["id"]

    def test_the_bootstrap_stores_the_id_beside_the_name(self):
        raw = self.conn.execute("SELECT raw FROM bills WHERE payee=%s",
                                (PAYEE,)).fetchone()["raw"]
        self.assertEqual(raw["merchant_refs"],
                         [{"id": self.manual, "name": "Northwind"}])
        self.assertEqual(raw["merchant_names"], ["Northwind"])

    def test_a_rename_in_place_carries_the_bill(self):
        self.conn.execute("UPDATE merchants SET name='Northwind PBC' WHERE id=%s",
                          (self.manual,))
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertTrue(set(self.old) <= {t["txn_id"] for t in hist["txns"]})
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual((st["refreshed"], st["proposed_merchant"]), (1, 0))
        self.assertEqual(_names(self.conn), ["Northwind PBC"])

    def test_a_merge_follows_to_the_survivor(self):
        survivor = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Northwind, PBC','manual') RETURNING id::text AS id").fetchone()["id"]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE merchant_id=%s",
                          (survivor, self.manual))
        self.conn.execute("UPDATE merchants SET merged_into=%s WHERE id=%s",
                          (survivor, self.manual))
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertTrue(set(self.old) <= {t["txn_id"] for t in hist["txns"]})
        bills.reconcile_bill_merchants(self.conn, TODAY)
        raw = self.conn.execute("SELECT raw FROM bills WHERE payee=%s",
                                (PAYEE,)).fetchone()["raw"]
        self.assertEqual(raw["merchant_refs"],
                         [{"id": survivor, "name": "Northwind, PBC"}])

    def test_a_foreign_id_falls_back_to_the_name_and_is_resolved_here(self):
        """A bill mirrored from another instance carries that instance's
        ids; the name still finds the merchant here, and the nightly pass
        writes the local id in."""
        self.conn.execute(
            """UPDATE bills SET raw = raw || %s::jsonb WHERE payee=%s""",
            ('{"merchant_refs": [{"id": "00000000-0000-4000-8000-000000000001", '
             '"name": "Northwind"}]}', PAYEE))
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertIn(self.new, {t["txn_id"] for t in hist["txns"]})
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["refreshed"], 1)
        raw = self.conn.execute("SELECT raw FROM bills WHERE payee=%s",
                                (PAYEE,)).fetchone()["raw"]
        self.assertEqual(raw["merchant_refs"][0]["id"], self.manual)


class RenameIsOfferedNotAssumed(_Ledger):
    """The bill was made while the ledger still spelled the payee the old
    way; then the rename lands."""

    def setUp(self):
        super().setUp()
        _bill(self.conn)

    def test_identity_is_what_the_ledger_showed_at_creation(self):
        self.assertEqual(_names(self.conn), [PAYEE])
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        ids = {t["txn_id"] for t in hist["txns"]}
        self.assertTrue(set(self.old) <= ids)
        self.assertNotIn(self.new, ids)

    def test_nightly_offers_the_new_name_and_approval_attaches_it(self):
        self._merge()
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["proposed_merchant"], 1)
        p = [p for p in bills.pending_proposals(self.conn)
             if p["kind"] == "merchant"][0]
        self.assertEqual((p["payee"], p["evidence"]["bill_payee"]),
                         ("Northwind", PAYEE))
        # not yet: an offer is not an attachment
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertNotIn(self.new, {t["txn_id"] for t in hist["txns"]})
        bills.apply_proposal(self.conn, p["id"], "approve")
        self.assertEqual(_names(self.conn), ["Northwind", PAYEE])
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertIn(self.new, {t["txn_id"] for t in hist["txns"]})
        # and the offer is not repeated
        self.assertEqual(
            bills.reconcile_bill_merchants(self.conn, TODAY)["proposed_merchant"], 0)

    def test_renaming_the_bill_carries_its_merchant_offers(self):
        self._merge()
        bills.reconcile_bill_merchants(self.conn, TODAY)
        p = [p for p in bills.pending_proposals(self.conn)
             if p["kind"] == "merchant"][0]
        self.assertEqual(bills.rename_bill(self.conn, PAYEE, "Northwind Sub"), 1)
        out = bills.apply_proposal(self.conn, p["id"], "approve")
        self.assertEqual(out.get("kind"), "merchant", out)
        self.assertEqual(_names(self.conn, "Northwind Sub"), ["Northwind", PAYEE])

    def test_a_rejected_offer_is_not_made_again(self):
        self._merge()
        bills.reconcile_bill_merchants(self.conn, TODAY)
        p = [p for p in bills.pending_proposals(self.conn)
             if p["kind"] == "merchant"][0]
        bills.apply_proposal(self.conn, p["id"], "reject")
        self.assertEqual(
            bills.reconcile_bill_merchants(self.conn, TODAY)["proposed_merchant"], 0)
        self.assertEqual(_names(self.conn), [PAYEE])

    def test_a_bill_from_before_identities_is_bootstrapped_once(self):
        self._merge()
        self.conn.execute(
            "UPDATE bills SET raw = raw - 'merchant_names' - 'merchant_refs'")
        self.assertIsNone(_names(self.conn))
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual((st["bootstrapped"], st["proposed_merchant"]), (1, 0))
        self.assertEqual(_names(self.conn), ["Northwind"])

    def test_an_empty_identity_is_filled_from_the_ledger_not_offered(self):
        """A bill made before its charges existed bootstrapped to nothing;
        the nightly takes the ledger's word rather than offering the bill
        the merchant its text already claims."""
        self.conn.execute(
            """UPDATE bills SET raw = raw
                   || '{"merchant_names": [], "merchant_refs": []}'::jsonb""")
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual((st["bootstrapped"], st["proposed_merchant"]), (1, 0))
        self.assertEqual(_names(self.conn), [PAYEE])

    def test_a_split_leaves_the_bill_unpaid_until_the_new_name_is_approved(self):
        """The rows moved to a merchant the bill does not know (a split on
        the Merchants page). The bill's identity is intact but its merchant
        now has no rows: nothing matches — never the letters, which would
        take look-alikes too — the nightly offers the new name, and
        approving it makes those rows the bill's and pins the list."""
        self.assertEqual(_names(self.conn), [PAYEE])
        split = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Northwind PBC','manual') RETURNING id").fetchone()["id"]
        self.conn.execute(
            "UPDATE transactions SET merchant_id=%s WHERE id = ANY(%s)",
            (split, self.old))
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertFalse(set(self.old) & {t["txn_id"] for t in hist["txns"]})
        self.assertNotIn(self.other, {t["txn_id"] for t in hist["txns"]})
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["proposed_merchant"], 1)
        p = [p for p in bills.pending_proposals(self.conn)
             if p["kind"] == "merchant"][0]
        self.assertEqual(p["payee"], "Northwind PBC")
        bills.apply_proposal(self.conn, p["id"], "approve")
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        self.assertTrue(set(self.old) <= {t["txn_id"] for t in hist["txns"]})
        raw = self.conn.execute("SELECT raw FROM bills WHERE payee=%s",
                                (PAYEE,)).fetchone()["raw"]
        self.assertTrue(raw.get("merchant_pinned"))

    def test_a_bill_with_no_identity_matches_by_text(self):
        self.conn.execute(
            """UPDATE bills SET raw = raw
                   || '{"merchant_names": [], "merchant_refs": []}'::jsonb""")
        hist = bills.merchant_history(self.conn, PAYEE, today=TODAY)
        ids = {t["txn_id"] for t in hist["txns"]}
        self.assertTrue(set(self.old) <= ids)
        self.assertIn(self.other, ids)          # the letters match
        self.assertNotIn(self.new, ids)


class Discovery(_Ledger):
    """budget.bill_displays: what the ledger says a bill pays."""

    def test_names_the_merchant_as_the_ledger_shows_it(self):
        self._merge()
        names = budget.bill_displays(
            self.conn, [{"payee": PAYEE, "merchant": PAYEE.lower()}], TODAY)[PAYEE]
        self.assertEqual(names, frozenset({"Northwind"}))

    def test_a_processor_prefix_does_not_offer_the_merchants_other_rows(self):
        """"GOOGLE *SMASHBURGER" is Smashburger paid through Google: the
        word hits the descriptor, not the merchant's name."""
        sb = self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id) VALUES "
            "('Smashburger','plaid','ent-sb') RETURNING id").fetchone()["id"]
        ids = [add_txn(self.conn, TODAY - dt.timedelta(days=d), 14.0, name,
                       merchant="Smashburger", primary="FOOD_AND_DRINK")
               for d, name in ((2, "GOOGLE *SMASHBURGER"), (9, "SMASHBURGER 1234"),
                               (16, "SMASHBURGER 1234"))]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id = ANY(%s)",
                          (sb, ids))
        names = budget.bill_displays(
            self.conn, [{"payee": "Google", "merchant": "google"}], TODAY)
        self.assertNotIn("Smashburger", names["Google"])

    def test_a_string_clean_grouping_needs_the_majority(self):
        sb = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Smashburger','layer1') RETURNING id").fetchone()["id"]
        ids = [add_txn(self.conn, TODAY - dt.timedelta(days=d), 14.0, name,
                       merchant=m, primary="FOOD_AND_DRINK")
               for d, name, m in ((2, "GOOGLE SMASHBURGER", "Google Smashburger"),
                                  (9, "SMASHBURGER 1234", "Smashburger"),
                                  (16, "SMASHBURGER 1234", "Smashburger"))]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id = ANY(%s)",
                          (sb, ids))
        q = [{"payee": "Google", "merchant": "google"}]
        self.assertNotIn("Smashburger", budget.bill_displays(self.conn, q, TODAY)["Google"])
        # the same grouping by a person's hand IS authority
        self.conn.execute("UPDATE merchants SET name_source='manual' WHERE id=%s", (sb,))
        self.assertIn("Smashburger", budget.bill_displays(self.conn, q, TODAY)["Google"])

    def test_the_payees_own_name_is_its_bucket(self):
        # no words in common with the rows' text, but the ledger displays a
        # merchant by exactly this name
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 9.0, "XYZ 4471",
                merchant="The Corner Shop")
        names = budget.bill_displays(
            self.conn, [{"payee": "The Corner Shop", "merchant": None}], TODAY)
        self.assertIn("The Corner Shop", names["The Corner Shop"])


class AttachBoundary(unittest.TestCase):
    """bills.attachable: what the ledger offered that may become identity."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _merchant(self, name, parent=None):
        return self.conn.execute(
            "INSERT INTO merchants (name, name_source, parent_id) VALUES "
            "(%s,'layer1',%s) RETURNING id", (name, parent)).fetchone()["id"]

    def test_a_brand_bill_never_takes_its_fuel_arm(self):
        costco = self._merchant("Costco")
        self._merchant("Costco Gas", parent=costco)
        self.assertEqual(bills.attachable(self.conn, "Costco", "costco",
                                          {"Costco", "Costco Gas"}), ["Costco"])
        # a bill that names the pump is the pump's
        self.assertEqual(bills.attachable(self.conn, "Costco Gas", "costco gas",
                                          {"Costco", "Costco Gas"}),
                         ["Costco Gas"])

    def test_a_one_word_bill_offered_siblings_keeps_only_the_shortening(self):
        for n in ("Google", "Google Store", "Google One"):
            self._merchant(n)
        self.assertEqual(bills.attachable(self.conn, "Google", "google",
                                          {"Google", "Google Store", "Google One"}),
                         ["Google"])
        # two siblings and no shortening: nothing, and the person decides
        for n in ("Riverton Health", "Riverton County Service"):
            self._merchant(n)
        self.assertEqual(bills.attachable(self.conn, "Riverton", "riverton",
                                          {"Riverton Health", "Riverton County Service"}),
                         [])

    def test_a_single_offer_stands_when_nothing_shorter_exists(self):
        self._merchant("Progressive Insurance")
        self.assertEqual(bills.attachable(self.conn, "Progressive Ins Mayfield",
                                          "progressive", {"Progressive Insurance"}),
                         ["Progressive Insurance"])

    def test_a_lone_sibling_is_not_a_rename_when_the_shortening_exists(self):
        """Discovery looks back a fixed window; the bill's own merchant can
        age out of it and leave a longer sibling as the only offer. The
        sibling is never taken silently — every one of its charges would
        pay the bill — but the nightly may put it to the person."""
        for n in ("Google", "Google Store"):
            self._merchant(n)
        self.assertEqual(bills.attachable(self.conn, "Google", "google", {"Google Store"}), [])
        self.assertEqual(bills.attachable(self.conn, "Google", "google", {"Google Store"},
                                          offer=True), ["Google Store"])
        # the same shape under a manual name and under an envelope
        for n in ("Smashburger", "Smashburger Riverton"):
            self._merchant(n)
        self.assertEqual(bills.attachable(self.conn, "Smashburger", "smashburger",
                                          {"Smashburger Riverton"}), [])
        for n in ("Riverton", "Riverton Health"):
            self._merchant(n)
        self.assertEqual(bills.attachable(self.conn, "Riverton", "riverton",
                                          {"Riverton Health"}), [])

    def test_a_name_no_merchant_has_is_not_an_identity(self):
        self.assertEqual(bills.attachable(self.conn, "Nova Mobile", "nova mobile",
                                          {"NOVA MOBILE PREPAID"}), [])


class ALoneSiblingIsOfferedAndTheBillWaits(unittest.TestCase):
    """The ledger's only offer is a name the bill may not take silently.

    Discovery looks back a fixed window, so a "Google" bill whose plain
    Google rows aged out of it is offered Google Store alone — the
    shortening's absence, not a rename (bills.attachable). The nightly may
    not bootstrap that, and it may not leave the bill matching by TEXT
    either: every word of "Google" is in every Google Store row, so the
    letters would pay the bill with exactly the rows the offer is asking
    about. The bill is held at an empty identity — it matches nothing —
    until the person answers.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.store = self._merchant("Google Store", "ent-store")
        self.plain = self._merchant("Google", "ent-google")
        # the sibling's charges are recent; the bill's own merchant last
        # paid longer ago than discovery looks back
        self.store_txns = [self._txn(d, "GOOGLE STORE", self.store)
                           for d in (4, 34)]
        self.aged = self._txn(500, "GOOGLE", self.plain)
        _google_bill(self.conn)

    def tearDown(self):
        self.conn.close()

    def _merchant(self, name, entity):
        return self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id) VALUES "
            "(%s,'plaid',%s) RETURNING id::text AS id",
            (name, entity)).fetchone()["id"]

    def _txn(self, days_ago, name, merchant_id):
        tid = add_txn(self.conn, TODAY - dt.timedelta(days=days_ago), 20.0, name,
                      merchant=name.title(), primary="GENERAL_MERCHANDISE")
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (merchant_id, tid))
        return tid

    def _identity(self):
        row = self.conn.execute(
            "SELECT payee, merchant, raw FROM bills WHERE payee='Google'").fetchone()
        return budget.bill_merchant_ids(self.conn, [row])["Google"]

    def _hist(self):
        return {t["txn_id"] for t in
                bills.merchant_history(self.conn, "Google", today=TODAY)["txns"]}

    def test_the_sibling_is_offered_and_pays_nothing_until_approved(self):
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual((st["bootstrapped"], st["proposed_merchant"]), (0, 1))
        p = [p for p in bills.pending_proposals(self.conn)
             if p["kind"] == "merchant"][0]
        self.assertEqual((p["payee"], p["evidence"]["bill_payee"]),
                         ("Google Store", "Google"))
        # an identity that is empty, NOT the text fallback: the sibling's
        # rows are what the offer is about, so they are not the bill's yet
        self.assertEqual(self._identity(), frozenset())
        self.assertFalse(self._hist() & set(self.store_txns))
        # and the nightly asks once, not every night
        self.assertEqual(
            bills.reconcile_bill_merchants(self.conn, TODAY)["proposed_merchant"], 0)
        bills.apply_proposal(self.conn, p["id"], "approve")
        self.assertEqual(_names(self.conn, "Google"), ["Google Store"])
        self.assertEqual(self._identity(), frozenset({self.store}))
        self.assertTrue(set(self.store_txns) <= self._hist())
        raw = self.conn.execute(
            "SELECT raw FROM bills WHERE payee='Google'").fetchone()["raw"]
        self.assertNotIn("merchant_offered", raw)

    def test_a_refused_offer_leaves_the_bill_matching_nothing(self):
        bills.reconcile_bill_merchants(self.conn, TODAY)
        p = [p for p in bills.pending_proposals(self.conn)
             if p["kind"] == "merchant"][0]
        bills.apply_proposal(self.conn, p["id"], "reject")
        # they said the store is not this bill; the letters must not say
        # otherwise on the next pass
        self.assertEqual(
            bills.reconcile_bill_merchants(self.conn, TODAY)["proposed_merchant"], 0)
        self.assertEqual(self._identity(), frozenset())
        self.assertFalse(self._hist() & set(self.store_txns))

    def test_an_edit_that_settles_nothing_does_not_restore_text_matching(self):
        bills.reconcile_bill_merchants(self.conn, TODAY)
        _google_bill(self.conn, txn_category="ENTERTAINMENT")
        self.assertEqual(self._identity(), frozenset())
        self.assertFalse(self._hist() & set(self.store_txns))

    def test_the_bills_own_merchant_returning_bootstraps_it(self):
        """The plain Google rows are back inside the window: the shortening
        stands on the ledger's own authority and the wait is over."""
        bills.reconcile_bill_merchants(self.conn, TODAY)
        back = self._txn(2, "GOOGLE", self.plain)
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["bootstrapped"], 1)
        self.assertEqual(_names(self.conn, "Google"), ["Google"])
        self.assertEqual(self._identity(), frozenset({self.plain}))
        self.assertIn(back, self._hist())
        raw = self.conn.execute(
            "SELECT raw FROM bills WHERE payee='Google'").fetchone()["raw"]
        self.assertNotIn("merchant_offered", raw)


class AStaleNightlyNeverOverwritesAPin(_Ledger):
    """reconcile_bill_merchants reads every bill once, unlocked, and then
    works through them. A person saving the bill form in that window holds
    the row lock and wins: the nightly's writes re-check, in the UPDATE
    itself, the facts they were decided on."""

    def setUp(self):
        super().setUp()
        self._merge()
        _bill(self.conn)

    def _save_mid_loop(self, **kw):
        """Save the bill from inside the loop, the first time the nightly
        asks what the ledger offers — i.e. after its snapshot was read."""
        real, seen = bills.attachable, []

        def patched(*a, **k):
            if not seen:
                seen.append(1)
                _bill(self.conn, **kw)
            return real(*a, **k)
        bills.attachable = patched
        self.addCleanup(setattr, bills, "attachable", real)

    def test_a_list_emptied_mid_pass_is_not_refilled(self):
        self.conn.execute(
            """UPDATE bills SET raw = raw
                   || '{"merchant_names": [], "merchant_refs": []}'::jsonb""")
        self._save_mid_loop(merchants=[])
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["bootstrapped"], 0)
        self.assertEqual(_names(self.conn), [])
        raw = self.conn.execute("SELECT raw FROM bills WHERE payee=%s",
                                (PAYEE,)).fetchone()["raw"]
        self.assertTrue(raw["merchant_pinned"])

    def test_a_list_pinned_mid_pass_is_not_reverted_by_the_refresh(self):
        """The refresh is bookkeeping on the refs the snapshot held; those
        refs are gone, so it has nothing to write."""
        self.conn.execute("UPDATE merchants SET name='Northwind PBC' "
                          "WHERE name_source='manual'")
        self._save_mid_loop(merchants=["Northwind Labs"])
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["refreshed"], 0)
        self.assertEqual(_names(self.conn), ["Northwind Labs"])


class RefsNoMerchantAnswersTo(_Ledger):
    """A bill mirrored or restored from another instance carries that
    instance's ids. The NAME is the fallback and resolves here
    (IdentityFollowsTheMerchantRow). When neither resolves the bill has an
    identity that matches nothing — never the letters, which is the whole
    point of an identity — and the nightly offers it the local name."""

    def setUp(self):
        super().setUp()
        self._merge()
        _bill(self.conn)
        self.conn.execute(
            """UPDATE bills SET raw = raw || %s::jsonb WHERE payee=%s""",
            ('{"merchant_refs": [{"id": "00000000-0000-4000-8000-000000000009",'
             ' "name": "Utility Of Another Instance"}],'
             ' "merchant_names": ["Utility Of Another Instance"]}', PAYEE))

    def test_they_match_nothing_and_the_local_name_is_offered(self):
        self.assertEqual(bills.merchant_history(
            self.conn, PAYEE, today=TODAY)["txns"], [])
        st = bills.reconcile_bill_merchants(self.conn, TODAY)
        self.assertEqual(st["proposed_merchant"], 1)
        p = [p for p in bills.pending_proposals(self.conn)
             if p["kind"] == "merchant"][0]
        self.assertEqual(p["payee"], "Northwind")
        bills.apply_proposal(self.conn, p["id"], "approve")
        self.assertIn(self.new, {t["txn_id"] for t in bills.merchant_history(
            self.conn, PAYEE, today=TODAY)["txns"]})


class MatcherSemantics(unittest.TestCase):
    def test_text_forms_are_unchanged_without_an_identity(self):
        m = budget.merchant_matcher("northwind headquarters", "northwind")
        self.assertTrue(m("northwind headquarters northwind* suite sub"))
        self.assertFalse(m("northwind northwind* suite sub seattle"))
        self.assertFalse(m("northwind suite sub", None, "m1"))

    def test_an_identity_is_the_whole_rule(self):
        m = budget.merchant_matcher("northwind headquarters", "northwind", {"m1"})
        self.assertTrue(m("northwind suite sub", None, "m1"))
        self.assertFalse(m("northwind suite sub", None, "m2"))
        # the letters alone no longer count
        self.assertFalse(m("northwind headquarters suite sub", None, "m2"))
        self.assertFalse(m("northwind headquarters suite sub", None, None))
        # an identity that resolves to nothing matches nothing, not text
        m = budget.merchant_matcher("northwind headquarters", "northwind", frozenset())
        self.assertFalse(m("northwind headquarters suite sub", None, None))

    def test_tolerance_keeps_a_floor_no_wider_than_half_the_bill(self):
        self.assertAlmostEqual(budget.bill_tolerance(15), 7.5)
        self.assertAlmostEqual(budget.bill_tolerance(60), 30.0)
        self.assertAlmostEqual(budget.bill_tolerance(100), 30.0)
        self.assertAlmostEqual(budget.bill_tolerance(200), 50.0)
        self.assertAlmostEqual(budget.bill_tolerance(-200), 50.0)


if __name__ == "__main__":
    unittest.main()
