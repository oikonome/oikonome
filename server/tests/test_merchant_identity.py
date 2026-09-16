"""Merchant identity is a ROW, resolved once (engine/merchant_identity.py).

Invariants: Plaid's entity id is the identity when present (the identity
counterparty over the raw merchant, a processor is context); a non-Plaid
row attaches to a merchant of the exact same name and otherwise gets a
layer-1 merchant; a person's rename outranks everything and a whole-
merchant rename keeps the merchant (and its logo); a rename onto an
existing name is a merge and undo reverses it; ingest resolves new rows
immediately; the ledger carries the logo; export → restore keeps the ids."""

import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import merchant_dedup, merchant_identity
from oikonome.sync import plaid, restore

from .util import TODAY, add_txn, make_db, write_config

PINECREST = {
    "transaction_id": "px1", "account_id": "p-chk", "date": "2026-08-16",
    "amount": 120.00, "name": "Pinecrest Movers", "merchant_name": "Pinecrest Movers",
    "pending": False, "logo_url": "https://logos.example/pinecrest.png",
    "website": "pinecrest-movers.test", "merchant_entity_id": "ENT-PINECREST",
    "counterparties": [{"name": "Pinecrest Movers", "type": "merchant",
                        "entity_id": "ENT-PINECREST", "confidence_level": "VERY_HIGH",
                        "logo_url": "https://logos.example/pinecrest.png",
                        "website": "pinecrest-movers.test"}],
    "personal_finance_category": {"primary": "GENERAL_SERVICES",
                                  "detailed": "GENERAL_SERVICES_POSTAGE_AND_SHIPPING",
                                  "confidence_level": "VERY_HIGH"}}
DOORDASH = {
    "transaction_id": "px2", "account_id": "p-chk", "date": "2026-08-16",
    "amount": 20.00, "name": "DD *DOORDASH CORNER DELI", "merchant_name": "Doordeli",
    "pending": False, "merchant_entity_id": None,
    "counterparties": [
        {"name": "Doordeli", "type": "merchant", "entity_id": None,
         "confidence_level": "LOW"},
        {"name": "DoorDash", "type": "marketplace", "entity_id": "ENT-DD",
         "confidence_level": "VERY_HIGH",
         "logo_url": "https://logos.example/dd.png", "website": "doordash.com"}],
    "personal_finance_category": {"primary": "FOOD_AND_DRINK",
                                  "detailed": "FOOD_AND_DRINK_RESTAURANT",
                                  "confidence_level": "HIGH"}}
STREAMCO_VIA_PAYPAL = {
    "transaction_id": "px3", "account_id": "p-chk", "date": "2026-08-15",
    "amount": 30.00, "name": "PAYPAL *STREAMCO", "merchant_name": "Streamco",
    "pending": False,
    "counterparties": [
        {"name": "PayPal", "type": "payment_app", "entity_id": "ENT-PP",
         "confidence_level": "VERY_HIGH"},
        {"name": "Streamco", "type": "merchant", "entity_id": "ENT-STRM",
         "confidence_level": "VERY_HIGH", "logo_url": "https://logos.example/st.png"}],
    "personal_finance_category": {"primary": "ENTERTAINMENT",
                                  "detailed": "ENTERTAINMENT_MUSIC_AND_AUDIO",
                                  "confidence_level": "VERY_HIGH"}}
ACCOUNTS = {"accounts": [
    {"account_id": "p-chk", "name": "Plaid Checking", "type": "depository",
     "subtype": "checking", "mask": "1234",
     "balances": {"current": 3000.0, "available": 2900.0,
                  "iso_currency_code": "USD"}}]}
PAGE = {"added": [PINECREST, DOORDASH, STREAMCO_VIA_PAYPAL], "modified": [],
        "removed": [], "has_more": False, "next_cursor": "c1"}


def _transport():
    def handler(request):
        path = request.url.path
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(ACCOUNTS))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(PAGE))
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": "plaid-item-1", "access_token": "access-prod-abc"}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_1"}}))
        if path == "/institutions/get_by_id":
            body = json.loads(request.content or b"{}")
            inst = {"name": "Demo Bank", "institution_id": "ins_1"}
            if (body.get("options") or {}).get("include_optional_metadata"):
                inst.update({"logo": "iVBORw0KGgo=", "primary_color": "#123456",
                             "url": "https://demo.bank"})
            return httpx.Response(200, text=json.dumps({"institution": inst}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class PlaidIdentityTests(unittest.TestCase):
    def test_identity_counterparty_beats_the_raw_merchant(self):
        ident = merchant_identity.plaid_identity(DOORDASH)
        self.assertEqual(ident["entity_id"], "ENT-DD")
        self.assertEqual(ident["name"], "DoorDash")
        self.assertEqual(ident["kind"], "marketplace")

    def test_payment_app_is_context_not_identity(self):
        ident = merchant_identity.plaid_identity(STREAMCO_VIA_PAYPAL)
        self.assertEqual(ident["entity_id"], "ENT-STRM")
        self.assertEqual(ident["processor"], "PayPal")

    def test_entity_id_alone_is_enough(self):
        raw = dict(PINECREST, counterparties=[])
        self.assertEqual(merchant_identity.plaid_identity(raw)["entity_id"],
                         "ENT-PINECREST")

    def test_nothing_resolved_is_none(self):
        self.assertIsNone(merchant_identity.plaid_identity(
            {"name": "SIMPLEFIN ROW", "counterparties": []}))
        self.assertIsNone(merchant_identity.plaid_identity(None))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    def _merchant_of(self, txn_id):
        return self.conn.execute(
            """SELECT m.id, m.name, m.plaid_entity_id, m.logo_url, m.kind,
                      m.name_source
                 FROM transactions t JOIN merchants m ON m.id = t.merchant_id
                WHERE t.id=%s""", (txn_id,)).fetchone()

    def test_ingest_resolves_plaid_rows_with_logo_and_kind(self):
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        u = self._merchant_of("px1")
        self.assertEqual((u["name"], u["plaid_entity_id"], u["logo_url"]),
                         ("Pinecrest Movers", "ENT-PINECREST", "https://logos.example/pinecrest.png"))
        d = self._merchant_of("px2")
        self.assertEqual((d["name"], d["kind"]), ("DoorDash", "marketplace"))
        s = self._merchant_of("px3")
        self.assertEqual(s["name"], "Streamco")
        # the ledger row carries the logo, and the merchant page shows it
        from oikonome.web import data
        rows, *_ = data.search_transactions(self.conn, "pinecrest")
        self.assertEqual(rows[0]["merchant_logo"], "https://logos.example/pinecrest.png")
        self.assertEqual(rows[0]["merchant_id"], u["id"])
        # institution branding landed on the item
        it = self.conn.execute(
            "SELECT logo, brand_color, url FROM items WHERE id='plaid-item-1'").fetchone()
        self.assertEqual((it["brand_color"], it["url"]), ("#123456", "https://demo.bank"))

    def test_non_plaid_rows_attach_to_a_plaid_merchant_by_exact_name(self):
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        old = add_txn(self.conn, "2019-03-01", 80.0, "PINECREST MOVERS", merchant="Pinecrest Movers")
        other = add_txn(self.conn, "2019-03-02", 12.0, "HARVEST PANTRY MKT SPRINGFIELD")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(old)["plaid_entity_id"], "ENT-PINECREST",
                         "the imported Pinecrest row inherits the Plaid identity")
        self.assertEqual(self._merchant_of(old)["logo_url"],
                         "https://logos.example/pinecrest.png")
        p = self._merchant_of(other)
        self.assertIsNone(p["plaid_entity_id"])
        self.assertEqual(p["name_source"], "layer1")
        self.assertEqual(p["name"], "Harvest Pantry Mkt Springfield")

    def test_same_layer1_name_shares_one_merchant(self):
        a = add_txn(self.conn, "2026-08-01", 20, "SQ *COFFEE HOUSE")
        b = add_txn(self.conn, "2026-08-02", 21, "COFFEE HOUSE 123 MAIN ST, SPRINGFIELD, IL, US")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(a)["id"], self._merchant_of(b)["id"])
        self.assertEqual(self._merchant_of(a)["name"], "Coffee House")

    def test_resolve_is_idempotent(self):
        add_txn(self.conn, "2026-08-01", 20, "SQ *COFFEE HOUSE")
        s1 = merchant_identity.resolve(self.conn)
        s2 = merchant_identity.resolve(self.conn)
        self.assertEqual(s1["rows"], 1)
        self.assertEqual((s2["rows"], s2["created"]), (0, 0))

    def test_plaid_logo_lands_on_the_live_merchant_not_the_merged_alias(self):
        """Plaid's entity row may have been merged into another merchant.
        The logo/website refresh must reach the SURVIVOR (what the ledger
        shows), not the hidden alias — or the live merchant stays blank
        forever while a dead row fills up."""
        live = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES ('Pinecrest', 'manual')"
            " RETURNING id").fetchone()["id"]
        alias = self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id, "
            "merged_into) VALUES ('Pinecrest Movers', 'plaid', 'ENT-PINECREST', %s)"
            " RETURNING id", (live,)).fetchone()["id"]
        got = merchant_identity._upsert_plaid_merchant(self.conn, {
            "entity_id": "ENT-PINECREST", "name": "Pinecrest Movers",
            "logo_url": "https://logos.example/pinecrest.png",
            "website": "pinecrest-movers.test"}, {})
        self.assertEqual(got, live)
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, logo_url, website FROM merchants "
            "WHERE id IN (%s, %s)", (live, alias))}
        self.assertEqual(rows[live]["logo_url"], "https://logos.example/pinecrest.png")
        self.assertEqual(rows[live]["website"], "pinecrest-movers.test")
        self.assertIsNone(rows[alias]["logo_url"])

    def test_plaid_entity_attaches_to_a_manual_merchant_of_the_same_name(self):
        """A person merged a payee's spellings under one name before the
        aggregator resolved it; when the entity arrives under that exact
        name it must join that merchant — keeping the person's name and
        manual standing, gaining the entity, logo and site — not mint a
        same-named twin that only row-keyed surfaces can tell apart."""
        manual = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Example Labs', 'manual') RETURNING id").fetchone()["id"]
        got = merchant_identity._upsert_plaid_merchant(self.conn, {
            "entity_id": "ENT-EXLABS", "name": "Example Labs",
            "kind": "merchant",
            "logo_url": "https://logos.example/exlabs.png",
            "website": "example-labs.test"}, {})
        self.assertEqual(got, manual)
        row = self.conn.execute(
            "SELECT name, name_source, plaid_entity_id, logo_url, website "
            "FROM merchants WHERE id=%s", (manual,)).fetchone()
        self.assertEqual((row["name"], row["name_source"]),
                         ("Example Labs", "manual"))
        self.assertEqual(row["plaid_entity_id"], "ENT-EXLABS")
        self.assertEqual(row["logo_url"], "https://logos.example/exlabs.png")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE lower(name)='example labs'"
        ).fetchone()["n"], 1)
        # a second sight of the entity is the cache/row path, same merchant
        self.assertEqual(merchant_identity._upsert_plaid_merchant(self.conn, {
            "entity_id": "ENT-EXLABS", "name": "Example Labs"}, {}), manual)

    def test_nightly_folds_same_named_twins_into_the_plaid_row(self):
        """Two live rows of one name are one merchant shown twice; the
        nightly folds the manual (or older) row into the Plaid-backed one,
        moves its rows, aliases and facts, and leaves it pointing at the
        survivor so anything holding its id follows."""
        manual = self.conn.execute(
            "INSERT INTO merchants (name, name_source, website) VALUES "
            "('Example Labs','manual','example-labs.test') RETURNING id").fetchone()["id"]
        plaid = self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id, logo_url) VALUES "
            "('Example Labs','plaid','ENT-EXL','https://logos.example/exl.png') "
            "RETURNING id").fetchone()["id"]
        a = add_txn(self.conn, "2026-08-01", 20, "EXAMPLE LABS 1")
        b = add_txn(self.conn, "2026-08-02", 20, "EXAMPLE LABS 2")
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s", (manual, a))
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s", (plaid, b))
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, as_of, merchant_id) "
            "VALUES ('EXAMPLE LABS 1','Example Labs','manual',now(),%s)", (manual,))
        st = merchant_identity.fold_twins(self.conn)
        self.assertEqual((st["groups"], st["folded"]), (1, 1))
        rows = self.conn.execute(
            "SELECT id, merged_into, plaid_entity_id, website, logo_url FROM merchants "
            "WHERE lower(name)='example labs'").fetchall()
        by = {r["id"]: r for r in rows}
        self.assertEqual(by[manual]["merged_into"], plaid)
        self.assertIsNone(by[plaid]["merged_into"])
        self.assertEqual((by[plaid]["website"], by[plaid]["logo_url"]),
                         ("example-labs.test", "https://logos.example/exl.png"))
        self.assertEqual({r["merchant_id"] for r in self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE id IN (%s,%s)", (a, b))},
            {plaid})
        self.assertEqual(self.conn.execute(
            "SELECT merchant_id FROM merchant_canonical WHERE raw_merchant='EXAMPLE LABS 1'"
        ).fetchone()["merchant_id"], plaid)
        # idempotent, and two Plaid entities of one name are two businesses
        self.assertEqual(merchant_identity.fold_twins(self.conn)["folded"], 0)
        self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id) VALUES "
            "('Example Labs','plaid','ENT-EXL-2')")
        st = merchant_identity.fold_twins(self.conn)
        self.assertEqual((st["folded"], st["skipped_entities"]), (0, 1))

    def test_a_fold_yields_to_a_rename_that_lands_while_it_waits_for_the_lock(self):
        """The groups are read with no lock held. A rename or merge from the
        Merchants page can land in the gap — it moves rows and writes
        merged_into itself — and folding on the stale reading would
        overwrite that pointer and drag the rows back under a name their
        merchant no longer wears. The group re-forms on the next run."""
        from unittest import mock
        first = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Example Labs','layer1') RETURNING id").fetchone()["id"]
        second = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Example Labs','layer1') RETURNING id").fetchone()["id"]
        tx = add_txn(self.conn, "2026-08-02", 20, "EXAMPLE LABS 2")
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (second, tx))
        real, raced = merchant_identity._lock_identity, []

        def racing(conn):
            real(conn)
            if not raced:
                raced.append(1)
                conn.execute("UPDATE merchants SET name='Example Labs Downtown' "
                             "WHERE id=%s", (second,))

        with mock.patch.object(merchant_identity, "_lock_identity", racing):
            st = merchant_identity.fold_twins(self.conn)
        self.assertEqual((st["folded"], st["skipped_moved"]), (0, 1))
        self.assertIsNone(self.conn.execute(
            "SELECT merged_into FROM merchants WHERE id=%s",
            (second,)).fetchone()["merged_into"])
        self.assertEqual(self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE id=%s",
            (tx,)).fetchone()["merchant_id"], second)
        self.assertIsNone(self.conn.execute(
            "SELECT merged_into FROM merchants WHERE id=%s",
            (first,)).fetchone()["merged_into"])

    def test_whole_merchant_rename_keeps_the_merchant_and_its_logo(self):
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        before = self._merchant_of("px1")
        merchant_dedup.rename(self.conn, "Pinecrest Movers", "Pinecrest Storage")
        after = self._merchant_of("px1")
        self.assertEqual(after["id"], before["id"])
        self.assertEqual((after["name"], after["name_source"]),
                         ("Pinecrest Storage", "manual"))
        self.assertEqual(after["logo_url"], before["logo_url"])
        # a later sync must not put Plaid's name back
        plaid.sync(self.conn, "plaid-item-1", transport=_transport())
        self.assertEqual(self._merchant_of("px1")["name"], "Pinecrest Storage")

    def test_rename_onto_an_existing_name_merges_and_undo_splits_back(self):
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        a = add_txn(self.conn, "2026-08-01", 20, "STREAMCO USA")
        merchant_identity.resolve(self.conn)
        self.assertNotEqual(self._merchant_of(a)["id"], self._merchant_of("px3")["id"])
        merchant_dedup.rename(self.conn, "Streamco Usa", "Streamco")
        self.assertEqual(self._merchant_of(a)["id"], self._merchant_of("px3")["id"],
                         "renamed onto Streamco = merged into the Plaid merchant")
        self.assertEqual(self._merchant_of(a)["logo_url"], "https://logos.example/st.png")
        change = self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC LIMIT 1").fetchone()["id"]
        merchant_dedup.undo(self.conn, change)
        self.assertNotEqual(self._merchant_of(a)["id"], self._merchant_of("px3")["id"])
        self.assertEqual(self._merchant_of(a)["name"], "Streamco Usa")

    def test_llm_merge_does_not_outrank_plaid_identity(self):
        # a reviewed (llm) alias maps the Plaid row's raw string to some
        # other spelling; Plaid's identity still wins — the alias only
        # decides rows Plaid did not resolve
        merchant_dedup.load_llm_map(self.conn, {"Pinecrest Movers": "Pinecrest Moving Co"})
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        u = self._merchant_of("px1")
        self.assertEqual((u["name"], u["plaid_entity_id"]), ("Pinecrest Movers", "ENT-PINECREST"))
        self.assertEqual(u["logo_url"], "https://logos.example/pinecrest.png")

    def test_fuel_arm_outlet_stays_its_own_merchant_under_the_brand(self):
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        # a Northwind Club pump row: Plaid says Northwind Club (entity), the fuel arm says
        # the outlet is "Northwind Club Gas"
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, merchant_outlet, pending, removed, raw)
               VALUES ('pg1','p-chk','2026-08-10',47.10,'NORTHWIND CLUB GAS #0555',
                       'Northwind Club','Northwind Club Gas',0,0,%s)""",
            (__import__("oikonome.engine.compat", fromlist=["jsonb"]).jsonb(
                {"merchant_entity_id": "ENT-NWCLUB", "merchant_name": "Northwind Club",
                 "logo_url": "https://logos.example/nwclub.png",
                 "counterparties": [{"name": "Northwind Club", "type": "merchant",
                                     "entity_id": "ENT-NWCLUB",
                                     "confidence_level": "VERY_HIGH"}]}),))
        merchant_identity.resolve(self.conn)
        m = self._merchant_of("pg1")
        self.assertEqual(m["name"], "Northwind Club Gas")
        self.assertIsNone(m["plaid_entity_id"])
        parent = self.conn.execute(
            "SELECT p.name, p.plaid_entity_id FROM merchants c "
            "JOIN merchants p ON p.id = c.parent_id WHERE c.id=%s", (m["id"],)).fetchone()
        self.assertEqual((parent["name"], parent["plaid_entity_id"]),
                         ("Northwind Club", "ENT-NWCLUB"))

    def test_an_old_merge_of_the_outlet_into_the_brand_does_not_undo_the_split(self):
        # a reviewed map that says "Northwind Club Gas" IS
        # "Northwind Club"; the outlet stays its own merchant and the alias's own
        # spelling is left as the reviewer's record
        merchant_dedup.load_llm_map(self.conn, {"Northwind Club Gas": "Northwind Club"})
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, merchant_outlet, pending, removed, raw)
               VALUES ('pg2','chk','2026-08-10',47.10,'NORTHWIND CLUB GAS #0555',
                       'Northwind Club','Northwind Club Gas',0,0,'{}')""")
        merchant_identity.resolve(self.conn, only_unresolved=False)
        self.assertEqual(self._merchant_of("pg2")["name"], "Northwind Club Gas")
        row = self.conn.execute(
            "SELECT canonical, method FROM merchant_canonical "
            "WHERE raw_merchant='Northwind Club Gas'").fetchone()
        self.assertEqual((row["canonical"], row["method"]), ("Northwind Club", "llm"))

    def test_p2p_counterparty_name_beats_the_bank_counterparty(self):
        raw = {"merchant_name": "Zelle — Casey Example", "name": "Zelle payment",
               "counterparties": [{"name": "Acme Bank", "type": "financial_institution",
                                   "entity_id": "ENT-ACME", "confidence_level": "VERY_HIGH"}]}
        self.assertIsNone(merchant_identity.plaid_identity(raw))
        z = add_txn(self.conn, "2026-08-01", 25, "Zelle payment",
                    merchant="Zelle — Casey Example")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(z)["name"], "Zelle — Casey Example")

    def test_a_rule_reaches_every_spelling_of_the_merchant(self):
        """A user rule written from one raw spelling applies to a sibling
        spelling that resolves to the SAME merchant row even though their
        layer-1 canonicals differ — rules key on the merchant id."""
        from oikonome.engine import llm_categorize
        from oikonome.engine.compat import jsonb
        from oikonome.web import data
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        # a second DoorDash row whose raw spelling ("DoorDash") canonicalizes
        # differently from px2's ("Doordeli"); same Plaid entity → one merchant
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, pending, removed, raw)
               VALUES ('px2b','p-chk','2026-08-17',18.0,'DOORDASH*ORDER',
                       'DoorDash','GENERAL_SERVICES',0,0,%s)""",
            (jsonb({"merchant_entity_id": "ENT-DD", "merchant_name": "DoorDash",
                    "counterparties": [{"name": "DoorDash", "type": "marketplace",
                                        "entity_id": "ENT-DD",
                                        "confidence_level": "VERY_HIGH"}]}),))
        self.conn.execute("UPDATE transactions SET category_primary='GENERAL_SERVICES', "
                          "category_override=NULL WHERE id IN ('px2','px2b')")
        merchant_dedup.apply(self.conn)      # layer1 canonicals, then resolve
        self.assertEqual(self._merchant_of("px2")["id"], self._merchant_of("px2b")["id"])
        canon = {r["raw_merchant"]: r["canonical"] for r in self.conn.execute(
            "SELECT raw_merchant, canonical FROM merchant_canonical "
            "WHERE raw_merchant IN ('Doordeli','DoorDash')").fetchall()}
        self.assertNotEqual(canon["Doordeli"], canon["DoorDash"],
                            "the test needs two different canonicals")
        data.set_category(self.conn, "px2", "FOOD_AND_DRINK", scope="all")
        llm_categorize.apply(self.conn)
        cat = self.conn.execute(
            "SELECT COALESCE(category_override, category_primary) c "
            "FROM transactions WHERE id='px2b'").fetchone()["c"]
        self.assertEqual(cat, "FOOD_AND_DRINK",
                         "the sibling spelling under the same merchant takes the rule")

    def test_person_rename_outranks_plaid_on_a_fresh_resolve(self):
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        merchant_dedup.rename(self.conn, "Pinecrest Movers", "Storage Unit")
        self.conn.execute("UPDATE transactions SET merchant_id=NULL")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("px1")["name"], "Storage Unit")

    def test_export_restore_keeps_merchant_ids(self):
        plaid.link_item(self.conn, "public-xyz", "", transport=_transport())
        from .test_restore import _export_zip
        z = _export_zip(self.conn, ["items", "accounts", "merchants",
                                    "merchant_canonical", "transactions"])
        fresh = make_db()
        try:
            restore.restore_zip(fresh, z)
            m = fresh.execute(
                """SELECT m.name, m.logo_url FROM transactions t
                     JOIN merchants m ON m.id = t.merchant_id
                    WHERE t.id='px1'""").fetchone()
            self.assertEqual(m["name"], "Pinecrest Movers")
            self.assertEqual(m["logo_url"], "https://logos.example/pinecrest.png")
        finally:
            fresh.close()

    def test_stats(self):
        add_txn(self.conn, "2026-08-01", 20, "SQ *COFFEE HOUSE")
        merchant_identity.resolve(self.conn)
        st = merchant_identity.stats(self.conn)
        self.assertEqual((st["rows"], st["resolved"], st["merchants"]), (1, 1, 1))


class AbbreviatedDescriptorTests(unittest.TestCase):
    """A bank descriptor that abbreviates a Plaid-named merchant resolves
    to that merchant instead of minting a second one: an abbreviated
    insurer descriptor and the aggregator's full insurer name are one
    merchant, and a household must not see two of them."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_two_letter_initialism_can_still_adopt(self):
        """A name that BEGINS with an initialism must be able to adopt too:
        the string clean keeps a leading two-letter word now, and a
        three-letter floor here would mint a second merchant beside the
        Plaid-named one for exactly those descriptors."""
        from oikonome.engine import merchant_identity as mi
        pid = mi._create(self.conn, "NW Bank", "plaid", entity="ENT-NORTHWIND")
        self.assertEqual(
            mi._merchant_for_name(self.conn, "NW Bank Brookhollow", "layer1", {}),
            pid)
        # a lone letter is still too little to match on
        self.assertNotEqual(
            mi._merchant_for_name(self.conn, "N Bank Brookhollow", "layer1", {}),
            pid)

    def test_descriptor_adopts_the_plaid_merchant(self):
        from oikonome.engine import merchant_identity as mi
        pid = mi._create(self.conn, "Zenith Insurance", "plaid",
                         entity="ENT-ZENITH")
        got = mi._merchant_for_name(
            self.conn, "Zenith Ins Brookhollow Usa", "layer1", {})
        self.assertEqual(got, pid)
        # an unrelated name that merely shares a first token stays its own
        other = mi._merchant_for_name(self.conn, "Zenith Notes Llc",
                                      "layer1", {})
        self.assertNotEqual(other, pid)
        # and a Plaid-named merchant is never adopted by a Plaid lookup —
        # entities match on their id, not by abbreviation
        self.assertNotEqual(
            mi._merchant_for_name(self.conn, "Zenith Ins", "plaid", {}),
            pid)


class ARenameCoversTheSpellingFamily(unittest.TestCase):
    """A payee whose descriptor embeds the amount is a new string every
    payday. A person's rename must cover the family, not only the strings
    that existed when they made it — or every later paycheck mints a
    merchant beside the one they named."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_new_paycheck_string_follows_the_persons_name(self):
        from oikonome.engine import merchant_dedup
        for i, amt in enumerate((4000.0, 4000.01, 5000.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=60 - i * 14), -amt,
                    f"NORTHWIND HEALTH NORTHWIND~ Future Amount: {amt} ~ Tran: DDIR",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
        merchant_identity.resolve(self.conn)
        shown = self.conn.execute(
            "SELECT DISTINCT m.name FROM transactions t JOIN merchants m ON m.id=t.merchant_id "
            "WHERE t.name LIKE 'NORTHWIND HEALTH%'").fetchall()
        self.assertEqual(len(shown), 1)
        merchant_dedup.rename(self.conn, shown[0]["name"], "Northwind HealthCare")
        # the next payday: a string nobody has ever seen
        add_txn(self.conn, TODAY - dt.timedelta(days=1), -6000.00,
                "NORTHWIND HEALTH NORTHWIND~ Future Amount: 6000.00 ~ Tran: DDIR",
                account="chk", primary="INCOME", detailed="INCOME_WAGES")
        merchant_identity.resolve(self.conn)
        names = {r["name"] for r in self.conn.execute(
            "SELECT m.name FROM transactions t JOIN merchants m ON m.id=t.merchant_id "
            "WHERE t.name LIKE 'NORTHWIND HEALTH%'").fetchall()}
        self.assertEqual(names, {"Northwind HealthCare"})

    def test_a_family_the_person_split_stays_split(self):
        a = add_txn(self.conn, TODAY - dt.timedelta(days=30), 40.0,
                    "RIVERTON MART~ Future Amount: 40 ~ Tran: POS", account="chk")
        b = add_txn(self.conn, TODAY - dt.timedelta(days=20), 60.0,
                    "RIVERTON MART~ Future Amount: 60 ~ Tran: POS", account="chk")
        merchant_identity.resolve(self.conn)
        # the person sent the two strings to two different names on purpose
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, as_of) "
            "VALUES (%s,%s,'manual',now()), (%s,%s,'manual',now()) "
            "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET canonical=EXCLUDED.canonical, method='manual'",
            ("RIVERTON MART~ Future Amount: 40 ~ Tran: POS", "Riverton Mart Fuel",
             "RIVERTON MART~ Future Amount: 60 ~ Tran: POS", "Riverton Mart"))
        merchant_identity.resolve(self.conn, only_unresolved=False)
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 55.0,
                "RIVERTON MART~ Future Amount: 55 ~ Tran: POS", account="chk")
        merchant_identity.resolve(self.conn)
        new = self.conn.execute(
            "SELECT m.name FROM transactions t JOIN merchants m ON m.id=t.merchant_id "
            "WHERE t.name LIKE '%Amount: 55%'").fetchone()["name"]
        self.assertNotIn(new, ("Riverton Mart Fuel",))
        self.assertEqual(new, "Riverton Mart")
        del a, b


if __name__ == "__main__":
    unittest.main()
