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
        self.assertEqual(merchant_dedup.canonical_merchant("GglPay TARGET"),
                         "Target")
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
        # short piece stripped the brand itself (U-Haul → "Haul",
        # T-Mobile → "Mobile", H-E-B Gas → "Gas", E-ZPass → "Zpass")
        cm = merchant_dedup.canonical_merchant
        self.assertEqual(cm("U-Haul"), "U-Haul")
        self.assertEqual(cm("U-haul"), "U-Haul")
        self.assertEqual(cm("T-MOBILE"), "T-Mobile")
        self.assertEqual(cm("H-e-b Gas #118"), "H-E-B Gas")
        self.assertEqual(cm("E-ZPass"), "E-Zpass")
        self.assertEqual(cm("Jo-Ann Stores"), "Jo-Ann Stores")
        # a lone dash is still punctuation, not a word
        self.assertEqual(cm("FROZY D-LITE - 14TH ST"), "Frozy D-Lite")
        # digits inside a hyphenated brand are the brand; bare numbers
        # (phone, store number, date) are still noise
        self.assertEqual(cm("7-Eleven"), "7-Eleven")
        self.assertEqual(cm("7-ELEVEN #123"), "7-Eleven")
        self.assertEqual(cm("1-800-Flowers"), "1-800-Flowers")
        self.assertEqual(cm("A-1 Auto"), "A-1")   # "auto" is a stop token
        self.assertEqual(cm("A-1 LOCKSMITH"), "A-1 Locksmith")
        self.assertEqual(cm("A-1 TOWING"), "A-1 Towing")
        self.assertEqual(cm("CHICK-FIL-A #123"), "Chick-Fil-A")
        # a hyphen between two LONG words is a bank's punctuation, not a brand
        self.assertEqual(cm("WITHDRAWAL-CASH NORTHWIND CREDIT CD-ONLINE"),
                         "Withdrawal Cash Northwind Credit")
        self.assertEqual(cm("Giant-eagle #0031"), "Giant Eagle")
        self.assertEqual(cm("CREDIT-INTEREST"), "Credit Interest")
        self.assertEqual(cm("PARKING 217-555-01234 LOT"), "Parking")
        self.assertEqual(cm("SHELL 12-31 STORE 1010"), "Shell Store")

    def test_two_letter_brand_word_is_not_noise(self):
        """A blanket three-letter floor deleted the brand and left the
        generic half: 'US Bank' reached the ledger as 'Bank', 'JC Penney' as
        'Penney', 'Google Fi' as 'Google'. A whole two-letter WORD counts as
        name evidence; where it sits decides whether it is a name."""
        cm = merchant_dedup.canonical_merchant
        # leading — an initialism, and the point of the name
        self.assertEqual(cm("US BANK"), "US Bank")
        self.assertEqual(cm("us bank"), "US Bank")
        self.assertEqual(cm("JC PENNEY #3030"), "JC Penney")
        self.assertEqual(cm("GE APPLIANCES"), "GE Appliances")
        self.assertEqual(cm("NV DMV RENEWAL MARAIS"), "NV Dmv Renewal Marais")
        # a leading word that is a word keeps its lowercase-word shape
        self.assertEqual(cm("AT HOME 2020"), "At Home")
        self.assertEqual(cm("LA QUINTA INN"), "La Quinta Inn")
        # trailing, on a two-word name, the second word IS the product
        self.assertEqual(cm("GOOGLE FI"), "Google Fi")
        # trailing, anywhere else, two letters are what the bank truncated
        # or appended — a state, a street suffix, a company abbreviation
        self.assertEqual(cm("SANDWICH HUT MILLBROOK NV"), "Sandwich Hut Millbrook")
        self.assertEqual(cm("VALLEY ORGANIC FO"), "Valley Organic")
        self.assertEqual(cm("Walnutdale Brewing Co"), "Walnutdale Brewing")
        # a processor/bank code is not a name in any position
        self.assertEqual(cm("SQ COFFEE HOUSE"), "Coffee House")
        self.assertEqual(cm("CK STUDENT HOUSING"), "Student Housing")
        self.assertEqual(cm("ACME STORE XX"), "Acme Store")
        # ...and two letters standing alone are not a name either: a
        # descriptor that shrank to two letters has lost the payee
        self.assertEqual(cm("CC PL BILLPAY PAYMENT"), "CC PL BILLPAY PAYMENT")

    def test_two_letter_fragment_of_a_blob_is_still_noise(self):
        """The length floor exists for FRAGMENTS — the letter runs a digit
        split leaves behind. Those are not words at any length."""
        cm = merchant_dedup.canonical_merchant
        self.assertEqual(cm("FROZY D-LITE - 14TH ST"), "Frozy D-Lite")
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
        'Absolute Shine Detailing' and 'Absolute Shine Detailing Hawthorn
        Marais Olive' and stops grouping with itself."""
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "ABSOLUTE SHINE DETAILING 1200 N. HAWTHORN AVE. "
                "MARAIS D OLIVE, NV, US"),
            "Absolute Shine Detailing")
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "PAI ATM 220 WEST BIRCHWOOD DRIVE MARAIS D'OLIVE, NV, US"),
            "Pai Atm")
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "Riverside Central Credi 4400 CENTRAL WAY LARKSPUR, NV, US"),
            "Riverside Central Credi")

    def test_address_strip_needs_the_postal_tail(self):
        """The number-then-word cut applies ONLY to labels carrying a
        ', ST, US' tail. Unconditional, it would eat real names."""
        for name in ("Route 66 Diner", "Highway 55 Burgers",
                     "Store 24 Coffee"):
            got = merchant_dedup.canonical_merchant(name)
            # the words survive; only the digits go (pre-existing scrub)
            self.assertIn(name.split()[0].title(), got, name)
            self.assertIn(name.split()[-1].title(), got, name)

    def test_address_strip_never_empties_a_name(self):
        """A label that is nothing but an address keeps something."""
        self.assertTrue(merchant_dedup.canonical_merchant(
            "220 WEST BIRCHWOOD DRIVE, NV, US").strip())

    def test_city_strip_is_explicit_and_off_by_default(self):
        """Banks truncate the store's city into the merchant name at
        different lengths, so one restaurant becomes 'Burgo Walnutda' AND
        'Burgo Walnutdal'. The city list is CONFIGURED, never inferred:
        ranking trailing words by how many categories they span calls
        'chicago' a place and 'walnutda' a category word, because in a
        college town nearly all spending is food."""
        cities = ("walnutdale", "walnutdal", "walnutda", "marais olive",
                  "olivenv", "marais")
        # default: no list configured -> no change at all
        self.assertEqual(merchant_dedup.canonical_merchant("Burgo Walnutda"),
                         "Burgo Walnutda")
        # configured: both truncations land on the same merchant
        self.assertEqual(
            merchant_dedup.canonical_merchant("Burgo Walnutda", cities),
            "Burgo")
        self.assertEqual(
            merchant_dedup.canonical_merchant("Burgo Walnutdal", cities),
            "Burgo")
        # repeats until stable: 'Marais Olivenv' is two strips
        self.assertEqual(
            merchant_dedup.canonical_merchant(
                "Olive Grove Detailing Marais Olivenv", cities),
            "Olive Grove Detailing")

    def test_city_strip_never_leaves_a_generic_word_alone(self):
        """The guard that makes this safe: collapsing to a category word
        would fuse every such business in town into one row."""
        cities = ("walnutdale", "walnutdal", "walnutda")
        # 'Bank' alone would swallow every bank — left as-is
        self.assertEqual(
            merchant_dedup.canonical_merchant("Bank Of Walnutdale", cities),
            "Bank Walnutdale")
        # a distinctive short brand IS allowed to stand alone
        self.assertEqual(
            merchant_dedup.canonical_merchant("Kfc Walnutda", cities), "Kfc")
        # a leading city is part of the name, never stripped
        self.assertEqual(
            merchant_dedup.canonical_merchant("Walnutdale Brewing Co", cities),
            "Walnutdale Brewing")

    def test_never_reduces_to_empty(self):
        # all-noise names fall back to the raw string, never ''
        self.assertEqual(merchant_dedup.canonical_merchant("PP*"), "PP*")
        self.assertEqual(merchant_dedup.canonical_merchant(""), "")

    def test_stop_tokens_dropped_but_not_fused(self):
        self.assertEqual(
            merchant_dedup.canonical_merchant("Cedar Ridge Student ACH"),
            "Cedar Ridge Student")


class LabelLengthTests(unittest.TestCase):
    """The scrub must stay fast on a label a file import can hand it — a
    long run of one character is exactly what its regexes backtrack on."""

    def test_a_pathological_label_is_cut_before_the_scrub(self):
        import time
        from oikonome.engine.merchant_dedup import canonical_merchant
        for filler in (" ", "0", "~", "-"):
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
        the ledger reads the old name forever ('US BANK' shown as 'Bank')."""
        from oikonome.engine import merchant_identity
        add_txn(self.conn, "2025-07-01", 25, "US BANK", merchant="US BANK")
        # the pre-fix state: the old cleaning's canonical, and rows resolved
        # to a merchant carrying that name
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES ('US BANK', 'Bank', 'layer1')")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._display(), "Bank")

        merchant_dedup.apply(self.conn)

        self.assertEqual(self._canon("US BANK"), ("US Bank", "layer1"))
        self.assertEqual(self._display(), "US Bank")
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
        cfg["merchant_strip_cities"] = ["Walnutdale", " Marais Olive "]
        budget.save_config(self.conn, cfg)
        got = merchant_dedup.cities_for(self.conn)
        self.assertIn("Walnutdale", got)
        self.assertIn("Marais Olive", got)          # trimmed
        # longest first, so multi-word cities strip before their fragments
        self.assertEqual(list(got), sorted(got, key=len, reverse=True))

    def test_harvest_is_scoped_to_the_merchant_that_reported_it(self):
        """PER MERCHANT, never a global list. Real towns collide with
        ordinary business words — 'Center' is a town in Texas, and a global
        strip would turn 'Firestone Auto Center' into 'Firestone Auto'
        wherever that town appears. Scoped, it only ever touches a business
        the feed places there."""
        from .util import add_txn
        t = add_txn(self.conn, "2025-07-03", 30, "TACO HUT CENTER",
                    account="card")
        self.conn.execute(
            "UPDATE transactions SET raw = %s WHERE id = %s",
            ('{"location": {"city": "Center"}}', t))
        add_txn(self.conn, "2025-07-04", 40, "FIRESTONE AUTO CENTER",
                account="card")                    # no location at all
        merchant_dedup.apply(self.conn)
        got = {r["raw_merchant"]: r["canonical"] for r in self.conn.execute(
            "SELECT raw_merchant, canonical FROM merchant_canonical").fetchall()}
        self.assertEqual(got["TACO HUT CENTER"], "Taco Hut")
        # 'Auto' is dropped by the pre-existing stop-word filter; what
        # matters here is that 'Center' SURVIVES on a merchant the feed
        # never placed in Center, TX
        self.assertIn("Center", got["FIRESTONE AUTO CENTER"])

    def test_harvest_uses_the_aggregator_reported_city(self):
        """The honest automatic source: the feed says where the store is,
        so nothing has to be guessed or configured."""
        from oikonome.engine import budget
        from .util import add_txn
        t = add_txn(self.conn, "2025-07-01", 12, "BURGO WALNUTDA",
                    account="card")
        self.conn.execute(
            "UPDATE transactions SET raw = %s WHERE id = %s",
            ('{"location": {"city": "Walnutdale", "region": "NV"}}', t))
        self.assertIn("Walnutdale", merchant_dedup.harvest_cities(self.conn))
        merchant_dedup.apply(self.conn)
        # harvested list is persisted, and the truncation now strips
        cfg = budget.load_config(self.conn)
        self.assertIn("Walnutdale", cfg.get("merchant_cities_seen") or [])
        row = self.conn.execute(
            "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s",
            ("BURGO WALNUTDA",)).fetchone()
        self.assertEqual(row["canonical"], "Burgo")

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
        add_txn(self.conn, "2025-07-01", 10, "GglPay TARGET",
                merchant="GglPay TARGET")
        add_txn(self.conn, "2025-07-02", 12, "THE ORCHARD APTS ONLINE PMT~",
                merchant="THE ORCHARD APTS ONLINE PMT~")
        touched = merchant_dedup.apply(self.conn)
        self.assertEqual(touched, 2)
        self.assertEqual(self._canon("GglPay TARGET"), ("Target", "layer1"))
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
        t = add_txn(self.conn, "2025-07-01", 10, "GglPay TARGET",
                    merchant="GglPay TARGET")
        merchant_dedup.apply(self.conn)
        row = self.conn.execute(
            f"SELECT {merchant_dedup.DISPLAY_MERCHANT} d FROM transactions t "
            f"{merchant_dedup.MC_JOIN} WHERE t.id=%s", (t,)).fetchone()
        self.assertEqual(row["d"], "Target")

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
        # llm_categorize keys category RULES on the canonical string (moves
        # to merchant_id in the next phase) and merchant_dedup's docstring
        # quotes the chain; both are named exceptions, not a pattern gap.
        pat = re.compile(r"COALESCE\(\s*mc\.canonical\s*,\s*t\.merchant_outlet", re.I)
        offenders = []
        for f in root.rglob("*.py"):
            if f.name in ("merchant_sql.py", "llm_categorize.py",
                          "merchant_dedup.py"):
                continue
            if pat.search(f.read_text(encoding="utf-8")):
                offenders.append(str(f.relative_to(root)))
        self.assertEqual(offenders, [],
                         f"{offenders} spell the merchant join themselves — "
                         "import merchant_sql.MC_JOIN / DISPLAY_MERCHANT")


if __name__ == "__main__":
    unittest.main()
