"""A card number never stands in a merchant's name unless a person typed it.

When the string clean finds no name in a descriptor ("ACH 4111 1111 1111 1234
ACME", where the junk cut takes all that follows the number) it keeps the
descriptor, and that kept the whole number as the merchant's name, on the Plaid
row and the file row alike, whatever a file row's own name had been cut to. The
clean now reads such a descriptor as its cut spelling, so a feed row and a file
row of one payee land on one merchant, and a migration applies the same cut to
the names already stored. What a person typed is theirs and is never rewritten.
"""
import pathlib
import random
import re
import unittest

from oikonome.engine import merchant_dedup, merchant_identity
from oikonome.sync import base, card_number

from .util import make_db, write_config


def _converges(text, stored):
    """What a frozen migration stored for `text` it cut only where the
    module reads a number, and it shows no fewer digits than the module's
    cut does: the module is never the narrower of the two."""
    def digits(v):
        return sum(c.isdigit() for c in v)
    return ((stored == text or card_number.holds_number(text))
            and digits(stored) >= digits(card_number.cut(text)))

MIGRATION = (pathlib.Path(base.__file__).parent.parent / "db" / "migrations"
             / "204_card_numbers_in_merchant_names_are_cut.sql")
DASHED = "4111-1111-1111-1234 VISA"
ACH = "ACH 4111 1111 1111 1234 ACME"
AMEX = "ONLINE PMT 3782 822463 10005"
RUN = "ONLINE PMT 4111111111111234"
WHOLE = "PMT 4111 1111 1111 1234"
CUT = "PMT …1234"
OTHER_CARD = "PMT 5500 0000 0000 1234"
NUMBER = re.compile(r"\d{4}[ -]\d{4}|\d{4}[ -]\d{6}|\d{8}")


def fuzzed(seed):
    rnd = random.Random(seed)
    parts = ["4111", "1111", "123456", "12345", "12", "2024",
             "1234567890123", "x", "…1111", " ", "-", "  ", "ACH ", "PMT ",
             " ", "-", " ", "-", ".", "4111 1111 1111 1111", "ACME", "VISA",
             "3782-822463-10005", "#", "~", "Venmo — "]
    return sorted({"".join(rnd.choice(parts)
                           for _ in range(rnd.randint(1, 12)))
                   for _ in range(1500)})


class TheCleanNeverMintsANumber(unittest.TestCase):
    def test_the_shapes_with_no_other_name_lose_the_number(self):
        for text, last in ((DASHED, None), (ACH, None), (AMEX, "0005"),
                           (RUN, "1234")):
            got = merchant_dedup.canonical_merchant(text)
            self.assertFalse(NUMBER.search(got), got)
            if last:
                self.assertIn("…" + last, got)

    def test_a_name_beside_the_number_is_the_merchant(self):
        self.assertEqual(merchant_dedup.canonical_merchant(ACH), "Acme")

    def test_a_feed_row_and_a_file_row_of_one_payee_read_alike(self):
        for text in (DASHED, ACH, AMEX, RUN):
            self.assertEqual(
                merchant_dedup.canonical_merchant(text),
                merchant_dedup.canonical_merchant(
                    base.mask_account_name(text)))

    def test_no_string_comes_out_holding_a_number(self):
        for text in fuzzed(204):
            for cities in ((), ("Springfield",)):
                got = merchant_dedup.canonical_merchant(text, cities)
                self.assertFalse(card_number.holds_number(got), (text, got))

    def test_a_short_number_and_a_descriptor_without_one_stay(self):
        for text in ("7-Eleven", "Route 66 Diner", "88 GAS 0123"):
            self.assertEqual(merchant_dedup.canonical_merchant(text),
                             merchant_dedup.canonical_merchant(
                                 base.mask_account_name(text)))
        self.assertEqual(merchant_dedup.canonical_merchant("2024"), "2024")
        self.assertEqual(merchant_dedup.canonical_merchant("1234 5678"),
                         "1234 5678")


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _row(self, tid, name, merchant=None, outlet=None, raw="{}"):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " merchant_name, merchant_outlet, removed, raw)"
            " VALUES (%s,'chk','2026-03-10',50,%s,%s,%s,0,%s::jsonb)",
            (tid, name, merchant, outlet, raw))

    def _names(self):
        return sorted(r["name"] for r in self.conn.execute(
            "SELECT name FROM merchants").fetchall())

    def _canonicals(self):
        return {r["raw_merchant"]: r["canonical"] for r in self.conn.execute(
            "SELECT raw_merchant, canonical FROM merchant_canonical"
        ).fetchall()}


class NewResolutionsMintNoNumber(_Ledger):
    def test_a_feed_row_with_no_other_name_resolves_cut(self):
        self._row("plaid-1", AMEX)
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._names(), ["ONLINE PMT …0005"])
        self.assertEqual(self._canonicals(), {AMEX: "ONLINE PMT …0005"})

    def test_a_name_the_clean_leaves_as_written_is_cut_when_minted(self):
        """An app-made counterparty name and an outlet skip the clean."""
        self._row("plaid-2", "x", merchant="Venmo — 4111 1111 1111 1234")
        self._row("plaid-3", "y", outlet="Pump 5500 0000 0000 1234")
        merchant_identity.resolve(self.conn)
        for name in self._names():
            self.assertFalse(NUMBER.search(name), name)
        self.assertIn("Venmo — …1234", self._names())
        self.assertIn("Pump …1234", self._names())

    def test_two_spellings_of_one_cut_name_are_one_merchant(self):
        self._row("plaid-4", "x", merchant="Venmo — 4111 1111 1111 1234")
        self._row("plaid-5", "y", merchant="Venmo — 4111-1111-1111-1234")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._names(), ["Venmo — …1234"])

    def test_an_aggregators_name_for_an_entity_is_cut_and_finds_its_twin(self):
        ident = {"entity_id": "ent-1", "name": f"Acme {WHOLE}", "kind": "merchant"}
        self.conn.execute("INSERT INTO merchants (name) VALUES (%s)",
                          (f"Acme {CUT}",))
        mid = merchant_identity._upsert_plaid_merchant(self.conn, ident, {})
        row = self.conn.execute(
            "SELECT name, plaid_entity_id FROM merchants WHERE id = %s",
            (mid,)).fetchone()
        self.assertEqual(row, {"name": f"Acme {CUT}",
                               "plaid_entity_id": "ent-1"})
        self.assertEqual(len(self._names()), 1)

    def test_a_reviewed_merge_is_written_cut(self):
        self._row("plaid-6", "PAYEE ONE")
        merchant_dedup.load_llm_map(self.conn, {"PAYEE ONE": WHOLE})
        self.assertEqual(self._canonicals()["PAYEE ONE"], CUT)
        self.assertEqual(self._names(), [CUT])

    def test_what_a_person_typed_stands(self):
        self._row("plaid-7", "PAYEE TWO")
        merchant_dedup.apply(self.conn)
        merchant_dedup.rename(self.conn, "Payee Two", WHOLE)
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._names(), [WHOLE])
        self.assertEqual(self._canonicals()["PAYEE TWO"], WHOLE)


class StoredNamesAreCut(_Ledger):
    def _migrate(self):
        self.conn.execute(MIGRATION.read_text())

    def _merchant(self, name, source="layer1", entity=None, merged_into=None):
        return self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id,"
            " merged_into) VALUES (%s,%s,%s,%s) RETURNING id",
            (name, source, entity, merged_into)).fetchone()["id"]

    def _alias(self, raw, canonical, method, mid=None):
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method,"
            " merchant_id) VALUES (%s,%s,%s,%s)", (raw, canonical, method, mid))

    def _rule(self, key, cat, source="llm"):
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary,"
            " source) VALUES (%s,%s,%s)", (key, cat, source))

    def _rules(self):
        return {r["merchant"]: (r["category_primary"], r["source"])
                for r in self.conn.execute(
                    "SELECT merchant, category_primary, source"
                    " FROM merchant_categories").fetchall()}

    def test_a_machines_name_and_its_alias_are_cut(self):
        mid = self._merchant(WHOLE)
        self._row("plaid-1", WHOLE)
        self.conn.execute("UPDATE transactions SET merchant_id = %s", (mid,))
        self._alias(WHOLE, WHOLE, "layer1", mid)
        self._migrate()
        self.assertEqual(self._names(), [CUT])
        # the raw key is the feed row's own word, as migration 203 left it
        self.assertEqual(self._canonicals(), {WHOLE: CUT})
        self.assertEqual(self.conn.execute(
            "SELECT merchant_id FROM transactions").fetchone()["merchant_id"],
            mid)

    def test_every_machine_source_and_a_merged_away_row_are_cut(self):
        live = self._merchant("Live")
        for name, source, into in (("LLM " + WHOLE, "llm", None),
                                   ("Feed " + WHOLE, "plaid", None),
                                   ("Gone " + WHOLE, "layer1", live)):
            self._merchant(name, source, merged_into=into)
        self._alias("PAYEE", "LLM " + WHOLE, "llm")
        self._migrate()
        self.assertEqual(self._names(), ["Feed " + CUT, "Gone " + CUT,
                                         "LLM " + CUT, "Live"])
        self.assertEqual(self._canonicals(), {"PAYEE": "LLM " + CUT})

    def test_a_persons_name_is_kept(self):
        typed = self._merchant(WHOLE, "manual")
        by_alias = self._merchant(OTHER_CARD)
        self._alias("PAYEE ONE", WHOLE, "manual", typed)
        self._alias("PAYEE TWO", OTHER_CARD, "manual", by_alias)
        self._migrate()
        self.assertEqual(self._names(), [WHOLE, OTHER_CARD])
        self.assertEqual(self._canonicals(), {"PAYEE ONE": WHOLE,
                                              "PAYEE TWO": OTHER_CARD})

    def test_a_date_range_and_a_phone_number_are_left_alone(self):
        text = "Plan 2023-2024 call 617-555-0100 on 12 31 2024"
        self._merchant(text)
        self._alias(text, text, "layer1")
        self._migrate()
        self.assertEqual(self._names(), [text])
        self.assertEqual(self._canonicals(), {text: text})

    def test_a_rule_under_the_old_name_follows_it(self):
        self._merchant(WHOLE)
        self._rule(WHOLE, "SHOPPING", "user")
        self._migrate()
        self.assertEqual(self._rules(), {CUT: ("SHOPPING", "user")})

    def test_a_persons_rule_outranks_the_machines_on_the_cut_name(self):
        self._merchant(WHOLE)
        self._merchant(OTHER_CARD)
        self._rule(WHOLE, "OTHER")
        self._rule(OTHER_CARD, "SHOPPING", "user")
        self._rule(CUT, "FEES")
        self._migrate()
        self.assertEqual(self._rules(), {CUT: ("SHOPPING", "user")})

    def test_a_rule_keyed_on_a_raw_spelling_stays_with_its_alias(self):
        mid = self._merchant(WHOLE)
        self._alias(WHOLE, WHOLE, "layer1", mid)
        self._rule(WHOLE, "SHOPPING", "user")
        self._migrate()
        self.assertEqual(self._rules(), {WHOLE: ("SHOPPING", "user")})

    def test_a_rule_under_a_persons_name_stays(self):
        self._merchant(WHOLE, "manual")
        self._rule(WHOLE, "SHOPPING", "user")
        self._migrate()
        self.assertEqual(self._rules(), {WHOLE: ("SHOPPING", "user")})

    def test_the_rename_journal_keeps_a_person_and_still_undoes(self):
        mid = self._merchant("Card Payment", "manual")
        self._row("plaid-2", WHOLE)
        self._alias(WHOLE, "Card Payment", "manual", mid)
        rid = self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical,"
            " to_canonical) VALUES (%s,%s,'Card Payment') RETURNING id",
            (WHOLE, WHOLE)).fetchone()["id"]
        self._migrate()
        row = self.conn.execute(
            "SELECT from_canonical, to_canonical FROM merchant_renames"
        ).fetchone()
        self.assertEqual(row, {"from_canonical": CUT,
                               "to_canonical": "Card Payment"})
        with self.conn.transaction():
            self.assertEqual(merchant_dedup.undo(self.conn, rid)["restored"],
                             CUT)
        self.assertEqual(self._canonicals()[WHOLE], CUT)

    def test_a_name_a_person_typed_earlier_stays_in_the_journal(self):
        self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical,"
            " to_canonical) VALUES ('P','Before',%s), ('P',%s,'After')",
            (WHOLE, WHOLE))
        self._migrate()
        self.assertEqual(
            [(r["from_canonical"], r["to_canonical"]) for r in
             self.conn.execute(
                 "SELECT from_canonical, to_canonical FROM merchant_renames"
                 " ORDER BY id").fetchall()],
            [("Before", WHOLE), (WHOLE, "After")])

    def test_a_journal_row_the_machine_wrote_can_still_be_undone(self):
        """A re-clean's split journals the machine's name as the live one;
        undo compares it with the alias, so the two are cut together."""
        self._alias("P", WHOLE, "layer1")
        self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical,"
            " to_canonical) VALUES ('P','Before',%s)", (WHOLE,))
        self._migrate()
        row = self.conn.execute(
            "SELECT to_canonical FROM merchant_renames").fetchone()
        self.assertEqual(row["to_canonical"], self._canonicals()["P"])
        self.assertEqual(row["to_canonical"], CUT)

    def _entity(self):
        return self.conn.execute(
            "INSERT INTO business_entity (name, structure) VALUES ('Biz','sole_prop')"
            " RETURNING id").fetchone()["id"]

    def _mark(self, entity, name, mid=None):
        self.conn.execute(
            "INSERT INTO vendor_1099 (entity_id, merchant, merchant_id)"
            " VALUES (%s,%s,%s)", (entity, name, mid))

    def _marks(self):
        return sorted(r["merchant"] for r in self.conn.execute(
            "SELECT merchant FROM vendor_1099").fetchall())

    def test_a_vendor_mark_follows_the_merchants_name(self):
        ent = self._entity()
        self._mark(ent, WHOLE, self._merchant(WHOLE))
        self._mark(ent, "Name " + WHOLE)
        self._merchant("Name " + WHOLE)
        self._mark(ent, "Own " + WHOLE)     # a name no merchant carries: typed
        self._migrate()
        self.assertEqual(self._marks(), ["Name " + CUT, "Own " + WHOLE, CUT])

    def test_two_marks_cutting_to_one_name_do_not_break_the_upgrade(self):
        ent = self._entity()
        a, b = self._merchant(WHOLE), self._merchant(OTHER_CARD)
        self._mark(ent, WHOLE, a)
        self._mark(ent, OTHER_CARD, b)
        self._migrate()
        self.assertIn(CUT, self._marks())
        merchant_identity.fold_twins(self.conn)
        self.assertEqual(self._marks(), [CUT])

    def test_two_merchants_cutting_to_one_name_fold_with_all_they_held(self):
        keep = self._merchant(WHOLE)
        gone = self._merchant(OTHER_CARD)
        self.conn.execute(
            "UPDATE merchants SET logo_url = 'https://logo.example/a.png'"
            " WHERE id = %s", (gone,))
        self._row("plaid-3", WHOLE)
        self._row("plaid-4", OTHER_CARD)
        self.conn.execute(
            "UPDATE transactions SET merchant_id = %s WHERE id = 'plaid-3'",
            (keep,))
        self.conn.execute(
            "UPDATE transactions SET merchant_id = %s WHERE id = 'plaid-4'",
            (gone,))
        self._alias(WHOLE, WHOLE, "layer1", keep)
        self._alias(OTHER_CARD, OTHER_CARD, "layer1", gone)
        self._migrate()
        self.assertEqual(merchant_identity.fold_twins(self.conn)["folded"], 1)
        live = self.conn.execute(
            "SELECT id, name, logo_url FROM merchants"
            " WHERE merged_into IS NULL").fetchall()
        self.assertEqual(live, [{"id": keep, "name": CUT,
                                 "logo_url": "https://logo.example/a.png"}])
        self.assertEqual(
            {r["merchant_id"] for r in self.conn.execute(
                "SELECT merchant_id FROM transactions").fetchall()}, {keep})
        self.assertEqual(
            {r["merchant_id"] for r in self.conn.execute(
                "SELECT merchant_id FROM merchant_canonical").fetchall()},
            {keep})

    def test_a_cut_name_meeting_a_persons_merchant_folds_into_theirs(self):
        typed = self._merchant(CUT, "manual")
        self._merchant(WHOLE)
        self._migrate()
        merchant_identity.fold_twins(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT id FROM merchants WHERE merged_into IS NULL").fetchall(),
            [{"id": typed}])

    def test_two_entities_sharing_a_cut_name_stay_two_businesses(self):
        self._merchant(WHOLE, "plaid", entity="ent-a")
        self._merchant(OTHER_CARD, "plaid", entity="ent-b")
        self._migrate()
        self.assertEqual(merchant_identity.fold_twins(self.conn)["folded"], 0)
        self.assertEqual(self._names(), [CUT, CUT])

    def test_the_nightly_clean_finishes_what_the_migration_began(self):
        """Stored spelling first, then the clean reads it as any new row."""
        mid = self._merchant(ACH)
        self._row("plaid-5", ACH)
        self.conn.execute("UPDATE transactions SET merchant_id = %s", (mid,))
        self._alias(ACH, ACH, "layer1", mid)
        self._migrate()
        self.assertEqual(self._names(), ["ACH …1234 ACME"])
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._canonicals()[ACH], "Acme")
        self.assertEqual(self.conn.execute(
            "SELECT m.name FROM transactions t JOIN merchants m"
            " ON m.id = t.merchant_id").fetchone()["name"], "Acme")
        for name in self._names():
            self.assertFalse(NUMBER.search(name), name)

    def test_a_second_run_writes_nothing(self):
        typed = self._merchant(OTHER_CARD, "manual")
        mid = self._merchant(WHOLE)
        self._alias(WHOLE, WHOLE, "layer1", mid)
        self._alias(OTHER_CARD, OTHER_CARD, "manual", typed)
        self._rule("Rule " + WHOLE, "OTHER")
        self._merchant("Rule " + WHOLE)
        self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical,"
            " to_canonical) VALUES ('P',%s,'After')", (WHOLE,))
        ent = self._entity()
        self._mark(ent, WHOLE, mid)
        self._migrate()

        def stamps():
            return [self.conn.execute(
                f"SELECT xmin::text AS x, * FROM {t}").fetchall()
                for t in ("merchants", "merchant_canonical",
                          "merchant_categories", "merchant_renames",
                          "vendor_1099")]
        before = stamps()
        self._migrate()
        self.assertEqual(stamps(), before)

    def test_the_sql_cut_and_the_python_cut_agree(self):
        """The migration's SQL is a frozen copy of an earlier, narrower cut
        (its exact agreement with the module is pinned case by case in
        test_card_number_sql_matches_python): it cuts nothing the module
        does not read as a number, and leaves no fewer digits than the
        module would."""
        texts = fuzzed(2040)
        ids = {t: self._merchant(t) for t in texts}
        for t in texts[:400]:
            self._alias(t, t, "llm")
        self._migrate()
        held = 0
        for t, mid in ids.items():
            got = self.conn.execute(
                "SELECT name FROM merchants WHERE id = %s", (mid,)
            ).fetchone()["name"]
            self.assertTrue(_converges(t, got), (t, got))
            held += got != t
        self.assertGreater(held, 100)
        aliases = self._canonicals()
        for t in texts[:400]:
            self.assertTrue(_converges(t, aliases[t]), (t, aliases[t]))


if __name__ == "__main__":
    unittest.main()
