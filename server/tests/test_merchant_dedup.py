"""Merchant canonicalization: layer-1 string cleaning, llm/manual rows
preserved across re-runs, and the SQL-fragment drift lock against
reporting.py's inlined copies."""

import unittest

from oikonome.engine import merchant_dedup, reporting

from .util import add_txn, make_db


class CanonicalMerchantTests(unittest.TestCase):
    """Pure-function layer 1 — the documented cleaning rules."""

    def test_mint_volatile_clause_stripped(self):
        # the '~…~ Tran:' clause embeds the dollar amount — one payee
        # exploded into dozens of strings without this
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "THE ORCHARD APTS ONLINE payment~ Future Amount: 1875 ~ Tran:"),
            "Orchard Apts")
        self.assertEqual(
            merchant_dedup.canonical_merchant("THE ORCHARD APTS ONLINE PMT~"),
            "Orchard Apts")

    def test_processor_wrappers_stripped(self):
        self.assertEqual(merchant_dedup.canonical_merchant("GglPay BRIGHTMART"),
                         "Brightmart")
        self.assertEqual(merchant_dedup.canonical_merchant("SQ *COFFEE HOUSE"),
                         "Coffee House")
        self.assertEqual(merchant_dedup.canonical_merchant("TST* BURGER BARN"),
                         "Burger Barn")

    def test_trailing_junk_stripped(self):
        self.assertEqual(
            merchant_dedup.canonical_merchant("ACME STORE XXXXX1234"),
            "Acme Store")
        self.assertEqual(
            merchant_dedup.canonical_merchant("PARKING 217-555-01234 LOT"),
            "Parking")

    def test_hyphenated_brand_keeps_its_short_part(self):
        # the hyphen is part of the word: cutting on it and dropping the
        # short piece would strip the brand itself (Q-Pack → "Pack",
        # K-Wireless → "Wireless", A-B-C Gas → "Gas", Q-ZTrip → "Ztrip")
        cm = merchant_dedup.canonical_merchant
        self.assertEqual(cm("Q-Pack"), "Q-Pack")
        self.assertEqual(cm("Q-pack"), "Q-Pack")
        self.assertEqual(cm("K-WIRELESS"), "K-Wireless")
        self.assertEqual(cm("A-b-c Gas #0555"), "A-B-C Gas")
        self.assertEqual(cm("Q-ZTrip"), "Q-Ztrip")
        self.assertEqual(cm("Jo-Ann Stores"), "Jo-Ann Stores")
        # a lone dash is still punctuation, not a word
        self.assertEqual(cm("ZIPPY Q-TREAT - 14TH ST"), "Zippy Q-Treat")
        # digits inside a hyphenated brand are the brand; bare numbers
        # (phone, store number, date) are still noise
        self.assertEqual(cm("9-Lantern"), "9-Lantern")
        self.assertEqual(cm("9-LANTERN #123"), "9-Lantern")
        self.assertEqual(cm("1-800-Blooms"), "1-800-Blooms")
        self.assertEqual(cm("A-1 Auto"), "A-1")   # "auto" is a stop token
        self.assertEqual(cm("A-1 LOCKSMITH"), "A-1 Locksmith")
        self.assertEqual(cm("A-1 TOWING"), "A-1 Towing")
        self.assertEqual(cm("QUICK-PIE-A #123"), "Quick-Pie-A")
        # a hyphen between two LONG words is a bank's punctuation, not a brand
        self.assertEqual(cm("WITHDRAWAL-CASH NORTHWIND CREDIT CD-ONLINE"),
                         "Withdrawal Cash Northwind Credit")
        self.assertEqual(cm("Giant-eagle #0031"), "Giant Eagle")
        self.assertEqual(cm("CREDIT-INTEREST"), "Credit Interest")
        self.assertEqual(cm("PARKING 217-555-01234 LOT"), "Parking")
        self.assertEqual(cm("PETROX 12-31 STORE 1010"), "Petrox Store")

    def test_two_letter_brand_word_is_not_noise(self):
        """A whole two-letter word must survive: a blanket three-letter
        floor would delete the brand and leave the generic half ('NW Bank'
        as 'Bank', 'JT Outfitters' as 'Outfitters', 'Nova Fi' as 'Nova'). A whole two-letter WORD counts as
        name evidence; where it sits decides whether it is a name."""
        cm = merchant_dedup.canonical_merchant
        # leading — an initialism, and the point of the name
        self.assertEqual(cm("NW BANK"), "NW Bank")
        self.assertEqual(cm("nw bank"), "NW Bank")
        self.assertEqual(cm("JT OUTFITTERS #3030"), "JT Outfitters")
        self.assertEqual(cm("QX APPLIANCES"), "QX Appliances")
        self.assertEqual(cm("KS DMV RENEWAL ELMHAVEN"), "KS Dmv Renewal Elmhaven")
        # a leading word that is a word keeps its lowercase-word shape
        self.assertEqual(cm("AT HOME 2020"), "At Home")
        self.assertEqual(cm("LA PALMA INN"), "La Palma Inn")
        # trailing, on a two-word name, the second word IS the product
        self.assertEqual(cm("NOVA FI"), "Nova Fi")
        # trailing, anywhere else, two letters are what the bank truncated
        # or appended — a state, a street suffix, a company abbreviation
        self.assertEqual(cm("SANDWICH HUT ELMHAVEN IL"), "Sandwich Hut Elmhaven")
        self.assertEqual(cm("VALLEY ORGANIC FO"), "Valley Organic")
        self.assertEqual(cm("Elmhaven Brewing Co"), "Elmhaven Brewing")
        # a processor/bank code is not a name in any position
        self.assertEqual(cm("SQ COFFEE HOUSE"), "Coffee House")
        self.assertEqual(cm("CK LAKEVIEW APARTMENTS"), "Lakeview Apartments")
        self.assertEqual(cm("ACME STORE XX"), "Acme Store")
        # ...and two letters standing alone are not a name either: a
        # descriptor that shrank to two letters has lost the payee
        self.assertEqual(cm("CC PL BILLPAY PAYMENT"), "CC PL BILLPAY PAYMENT")

    def test_two_letter_fragment_of_a_blob_is_still_noise(self):
        """The length floor exists for FRAGMENTS — the letter runs a digit
        split leaves behind. Those are not words at any length."""
        cm = merchant_dedup.canonical_merchant
        self.assertEqual(cm("ZIPPY Q-TREAT - 14TH ST"), "Zippy Q-Treat")
        self.assertEqual(cm("AMAZON.COM*1A2B3C AMZN.COM/BILL WA"),
                         "Amazon Amzn")
        # a lone letter is not name evidence either, unless a hyphen binds
        # it — banks prefix bare letters to descriptors
        self.assertEqual(cm("N BROADWAY MARKET"), "Broadway Market")
        self.assertEqual(cm("A-1 LOCKSMITH"), "A-1 Locksmith")

    def test_word_boundary_keeps_transportation(self):
        # 'Tran:' marker cut, 'Transportation' survives (word boundary)
        self.assertEqual(
            merchant_dedup.canonical_merchant("LKS Transportation"),
            "Lks Transportation")

    def test_postal_address_tail_stripped(self):
        """An aggregator that sends NO merchant name leaves the raw bank
        descriptor — street address and all — so one payee splits into
        'Sparkle Car Wash' and 'Sparkle Car Wash Maple
        Springfield' and stops grouping with itself."""
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "SPARKLE CAR WASH 350 S. MAPLE AVE. "
                "SPRINGFIELD, IL, US"),
            "Sparkle Car Wash")
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "KESTREL ATM 100 EAST WILLOW DRIVE COVE D'ARBRE, IL, US"),
            "Kestrel Atm")
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "Acme Community Credi 100 MAIN ST SPRINGFIELD, IL, US"),
            "Acme Community Credi")

    def test_address_strip_needs_the_postal_tail(self):
        """The number-then-word cut applies ONLY to labels carrying a
        ', ST, US' tail. Unconditional, it would eat real names."""
        for name in ("Route 66 Diner", "Highway 55 Burgers",
                     "Store 24 Coffee"):
            got = merchant_dedup.canonical_merchant(name)
            # the words survive; only the digits go (the digit scrub)
            self.assertIn(name.split()[0].title(), got, name)
            self.assertIn(name.split()[-1].title(), got, name)

    def test_address_strip_never_empties_a_name(self):
        """A label that is nothing but an address keeps something."""
        self.assertTrue(merchant_dedup.canonical_merchant(
            "100 EAST WILLOW DRIVE, IL, US").strip())

    def test_city_strip_is_explicit_and_off_by_default(self):
        """Banks truncate the store's city into the merchant name at
        different lengths, so one restaurant becomes 'Tacoria Elmhav' AND
        'Tacoria Elmhave'. The city list is CONFIGURED, never inferred:
        ranking trailing words by how many categories they span can call a
        truncated town a category word wherever most of its spending is
        one kind."""
        cities = ("elmhaven", "elmhave", "elmhav", "stone creek",
                  "creekil", "stone")
        # default: no list configured -> no change at all
        self.assertEqual(merchant_dedup.canonical_merchant("Tacoria Elmhav"),
                         "Tacoria Elmhav")
        # configured: both truncations land on the same merchant
        self.assertEqual(
            merchant_dedup.canonical_merchant("Tacoria Elmhav", cities),
            "Tacoria")
        self.assertEqual(
            merchant_dedup.canonical_merchant("Tacoria Elmhave", cities),
            "Tacoria")
        # repeats until stable: 'Stone Creekil' is two strips
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "Birch Grove Car Wash Stone Creekil", cities),
            "Birch Grove Car Wash")

    def test_city_strip_never_leaves_a_generic_word_alone(self):
        """The guard that makes this safe: collapsing to a category word
        would fuse every such business in town into one row."""
        cities = ("elmhaven", "elmhave", "elmhav")
        # 'Bank' alone would swallow every bank — left as-is
        self.assertEqual(
            merchant_dedup.canonical_merchant("Bank Of Elmhaven", cities),
            "Bank Elmhaven")
        # a distinctive short brand IS allowed to stand alone
        self.assertEqual(
            merchant_dedup.canonical_merchant("Kfc Elmhav", cities), "Kfc")
        # a leading city is part of the name, never stripped
        self.assertEqual(
            merchant_dedup.canonical_merchant("Elmhaven Brewing Co", cities),
            "Elmhaven Brewing")

    def test_a_configured_city_spelled_with_an_apostrophe_still_strips(self):
        """City variants are matched against the already-cleaned label, so
        they take the same apostrophe rule the label took."""
        cities = tuple(sorted(merchant_dedup._city_variants("Anse d'Or"),
                              key=len, reverse=True))
        self.assertEqual(
            merchant_dedup.canonical_merchant("Tacoria Anse d'Or", cities),
            "Tacoria")

    def test_an_apostrophe_joins_its_letters_instead_of_breaking_them(self):
        """A feed resolves a payee as "Juniper's Market" while the card line
        for the same purchase prints "JUNIPERS MARKET". Both have to clean
        to one string, or one business is two merchants."""
        canon = merchant_dedup.canonical_merchant
        self.assertEqual(canon("Juniper's Market"), "Junipers Market")
        self.assertEqual(canon("JUNIPERS MARKET"), "Junipers Market")
        # every character a feed writes it with
        for glyph in ("'", "\u2019", "\u02bc"):
            self.assertEqual(canon(f"Juniper{glyph}s Market"),
                             "Junipers Market", repr(glyph))
        # a trailing possessive
        self.assertEqual(canon("Junipers' Market"), "Junipers Market")
        # an elided prefix agrees with the line that prints it closed up
        self.assertEqual(canon("L'Anfora"), canon("LANFORA"))
        # two letters an apostrophe joins are a whole word, not two dropped
        # pieces
        self.assertEqual(canon("Q'Z BURGER BARN"), "QZ Burger Barn")
        # two different payees still differ
        self.assertNotEqual(canon("Juniper's Pizza"), canon("Marion Pizza"))
        # a LEADING one is a quote mark, and its word break is real
        self.assertEqual(canon("Casa 'Verde Grill"), "Casa Verde Grill")

    def test_never_reduces_to_empty(self):
        # all-noise names fall back to the raw string, never ''
        self.assertEqual(merchant_dedup.canonical_merchant("PP*"), "PP*")
        self.assertEqual(merchant_dedup.canonical_merchant(""), "")

    def test_stop_tokens_dropped_but_not_fused(self):
        self.assertEqual(
            merchant_dedup.canonical_merchant("Pinecrest Tutoring ACH"),
            "Pinecrest Tutoring")


class LabelLengthTests(unittest.TestCase):
    """The scrub must stay fast on a label a file import can hand it — a
    long run of one character is exactly what its regexes backtrack on."""

    def test_a_pathological_label_is_cut_before_the_scrub(self):
        import time
        from oikonome.engine.merchant_dedup import canonical_merchant
        for filler in (" ", "0", "~", "-", "'"):
            label = "Acme" + filler * 200_000 + "Store"
            t0 = time.monotonic()
            canonical_merchant(label)
            self.assertLess(time.monotonic() - t0, 1.0, repr(filler))

    def test_an_ordinary_label_is_untouched_by_the_cut(self):
        from oikonome.engine.merchant_dedup import canonical_merchant
        self.assertEqual(canonical_merchant("SQ *ACME COFFEE ROASTERS"),
                         canonical_merchant("SQ *ACME COFFEE ROASTERS"[:256]))


class ApplyTests(unittest.TestCase):
    def test_improved_cleaning_reaches_rows_already_in_the_ledger(self):
        """A layer1 canonical that CHANGES has to move its rows to the
        merchant it now names. The merchant ROW is what every surface
        displays, and the ordinary resolve pass only visits rows that have
        none — so without this a rule fix is invisible on existing data and
        the ledger reads the old name forever ('NW BANK' shown as 'Bank')."""
        from oikonome.engine import merchant_identity
        add_txn(self.conn, "2025-07-01", 25, "NW BANK", merchant="NW BANK")
        # a stale state: an older cleaning's canonical, and rows resolved
        # to a merchant carrying that name
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES ('NW BANK', 'Bank', 'layer1')")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._display(), "Bank")

        merchant_dedup.apply(self.conn)

        self.assertEqual(self._canon("NW BANK"), ("NW Bank", "layer1"))
        self.assertEqual(self._display(), "NW Bank")
        # and the emptied merchant does not linger in name matching
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM merchants WHERE name='Bank' "
            "AND merged_into IS NULL").fetchone())

    def _display(self):
        row = self.conn.execute(
            f"SELECT {merchant_dedup.DISPLAY_MERCHANT} AS d "
            f"  FROM transactions t {merchant_dedup.MC_JOIN} "
            f" WHERE t.removed = 0 LIMIT 1").fetchone()
        return row["d"]

    def test_cities_for_reads_config(self):
        from oikonome.engine import budget
        self.assertEqual(merchant_dedup.cities_for(self.conn), ())
        cfg = budget.load_config(self.conn)
        cfg["merchant_strip_cities"] = ["Elmhaven", " Stone Creek "]
        budget.save_config(self.conn, cfg)
        got = merchant_dedup.cities_for(self.conn)
        self.assertIn("Elmhaven", got)
        self.assertIn("Stone Creek", got)          # trimmed
        # longest first, so multi-word cities strip before their fragments
        self.assertEqual(list(got), sorted(got, key=len, reverse=True))

    def test_harvest_is_scoped_to_the_merchant_that_reported_it(self):
        """PER MERCHANT, never a global list. Real towns collide with
        ordinary business words — 'Center' is a town in Texas, and a global
        strip would turn 'Brightline Auto Center' into 'Brightline Auto'
        wherever that town appears. Scoped, it only ever touches a business
        the feed places there."""
        from .util import add_txn
        t = add_txn(self.conn, "2025-07-03", 30, "TACO HUT CENTER",
                    account="card")
        self.conn.execute(
            "UPDATE transactions SET raw = %s WHERE id = %s",
            ('{"location": {"city": "Center"}}', t))
        add_txn(self.conn, "2025-07-04", 40, "BRIGHTLINE AUTO CENTER",
                account="card")                    # no location at all
        merchant_dedup.apply(self.conn)
        got = {r["raw_merchant"]: r["canonical"] for r in self.conn.execute(
            "SELECT raw_merchant, canonical FROM merchant_canonical").fetchall()}
        self.assertEqual(got["TACO HUT CENTER"], "Taco Hut")
        # 'Auto' is dropped by the pre-existing stop-word filter; what
        # matters here is that 'Center' SURVIVES on a merchant the feed
        # never placed in Center, TX
        self.assertIn("Center", got["BRIGHTLINE AUTO CENTER"])

    def test_harvest_uses_the_aggregator_reported_city(self):
        """The honest automatic source: the feed says where the store is,
        so nothing has to be guessed or configured."""
        from oikonome.engine import budget
        from .util import add_txn
        t = add_txn(self.conn, "2025-07-01", 12, "TACORIA ELMHAV",
                    account="card")
        self.conn.execute(
            "UPDATE transactions SET raw = %s WHERE id = %s",
            ('{"location": {"city": "Elmhaven", "region": "IL"}}', t))
        self.assertIn("Elmhaven", merchant_dedup.harvest_cities(self.conn))
        merchant_dedup.apply(self.conn)
        # harvested list is persisted, and the truncation now strips
        cfg = budget.load_config(self.conn)
        self.assertIn("Elmhaven", cfg.get("merchant_cities_seen") or [])
        row = self.conn.execute(
            "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s",
            ("TACORIA ELMHAV",)).fetchone()
        self.assertEqual(row["canonical"], "Tacoria")

    def test_harvest_ignores_rows_without_a_city(self):
        from .util import add_txn
        add_txn(self.conn, "2025-07-02", 9, "NO LOCATION HERE", account="card")
        self.assertEqual(merchant_dedup.harvest_cities(self.conn), [])

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _canon(self, raw):
        r = self.conn.execute(
            "SELECT canonical, method FROM merchant_canonical "
            "WHERE raw_merchant=%s", (raw,)).fetchone()
        return (r["canonical"], r["method"]) if r else None

    def test_layer1_rows_written_and_idempotent(self):
        add_txn(self.conn, "2025-07-01", 10, "GglPay BRIGHTMART",
                merchant="GglPay BRIGHTMART")
        add_txn(self.conn, "2025-07-02", 12, "THE ORCHARD APTS ONLINE PMT~",
                merchant="THE ORCHARD APTS ONLINE PMT~")
        touched = merchant_dedup.apply(self.conn)
        self.assertEqual(touched, 2)
        self.assertEqual(self._canon("GglPay BRIGHTMART"), ("Brightmart", "layer1"))
        self.assertEqual(self._canon("THE ORCHARD APTS ONLINE PMT~"),
                         ("Orchard Apts", "layer1"))
        # steady-state re-run: nothing recomputed
        self.assertEqual(merchant_dedup.apply(self.conn), 0)

    def test_llm_rows_win_and_survive_apply(self):
        merchant_dedup.load_llm_map(self.conn, {"RAW WEIRD STRING 42": "Nice Shop"})
        add_txn(self.conn, "2025-07-01", 10, "RAW WEIRD STRING 42",
                merchant="RAW WEIRD STRING 42")
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._canon("RAW WEIRD STRING 42"),
                         ("Nice Shop", "llm"))       # NOT recomputed to layer1

    def test_manual_rows_survive_apply(self):
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES ('HAND RENAMED', 'My Name', 'manual')")
        add_txn(self.conn, "2025-07-01", 10, "HAND RENAMED",
                merchant="HAND RENAMED")
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._canon("HAND RENAMED"), ("My Name", "manual"))

    def test_display_fragments_resolve_canonical(self):
        t = add_txn(self.conn, "2025-07-01", 10, "GglPay BRIGHTMART",
                    merchant="GglPay BRIGHTMART")
        merchant_dedup.apply(self.conn)
        row = self.conn.execute(
            f"SELECT {merchant_dedup.DISPLAY_MERCHANT} d FROM transactions t "
            f"{merchant_dedup.MC_JOIN} WHERE t.id=%s", (t,)).fetchone()
        self.assertEqual(row["d"], "Brightmart")

    def test_no_drift_against_reporting_inlines(self):
        """reporting imports the fragments now; the assertion stays as the
        cheapest possible drift alarm."""
        self.assertEqual(merchant_dedup.MC_JOIN, reporting.MC_JOIN)
        self.assertEqual(merchant_dedup.DISPLAY_MERCHANT,
                         reporting.DISPLAY_MERCHANT)

    def test_no_module_spells_the_merchant_join_itself(self):
        """ONE definition of "the merchant of this row" (engine/merchant_sql).
        A dozen modules each spelling the lookup for themselves is how the
        same transaction comes to resolve to different names on different
        screens. Anything that needs the merchant imports the fragment;
        nothing re-types it."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1] / "oikonome"
        # the DISPLAY chain — the thing that must read the same everywhere.
        pat = re.compile(r"COALESCE\(\s*mc\.canonical\s*,\s*t\.merchant_outlet", re.I)
        offenders = []
        for f in root.rglob("*.py"):
            if f.name == "merchant_sql.py":
                continue
            if pat.search(f.read_text(encoding="utf-8")):
                offenders.append(str(f.relative_to(root)))
        self.assertEqual(offenders, [],
                         f"{offenders} spell the merchant join themselves — "
                         "import merchant_sql.MC_JOIN / DISPLAY_MERCHANT")

    def test_no_module_spells_the_identity_key_itself(self):
        """The IDENTITY KEY — what a row is grouped, aliased and re-resolved
        by — has one spelling, merchant_sql.raw_key.

        A module that types the outlet-first chain out by hand keeps the
        OLD key: it would go on filing a row under the aggregator name the
        resolver set aside, while every other surface files it under the
        bank line. Raw key → merchant would stop being a function, and a
        rename would move rows a household never asked about."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1] / "oikonome"
        pat = re.compile(
            r"COALESCE\(\s*(?:\w+\.)?merchant_outlet\s*,\s*(?:\w+\.)?merchant_name",
            re.I)
        offenders = []
        for f in root.rglob("*.py"):
            if f.name == "merchant_sql.py":
                continue
            if pat.search(f.read_text(encoding="utf-8")):
                offenders.append(str(f.relative_to(root)))
        self.assertEqual(offenders, [],
                         f"{offenders} spell the identity key themselves — "
                         "use merchant_sql.raw_key / RAW_KEY")


if __name__ == "__main__":
    unittest.main()
