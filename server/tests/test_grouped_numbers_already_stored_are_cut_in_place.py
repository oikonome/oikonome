"""Grouped card numbers stored before the wider cut are cut in place.

A file row's name, merchant and raw cells, its import's recorded filename and
the merchant aliases keyed on that name all left the instance holding "AUTOPAY
4111 1111 1111 1111" until the live rule (base.mask_account_name) learned the
grouped forms. A migration applies the same cut once. It must agree with the
live rule to the character, leave aggregator rows alone and keep a person's
rename joined to the rows it was made for. The row's id is not touched: the
keying pass that runs after the migrations takes it, and a re-import of the same
file still finds the row.
"""
import datetime as dt
import json
import pathlib
import random
import unittest

from oikonome.engine import merchant_identity
from oikonome.sync import base, card_number, csvimport

from .util import make_db, write_config

MIGRATION = (pathlib.Path(base.__file__).parent.parent / "db" / "migrations"
             / "203_grouped_card_numbers_stored_on_file_rows_are_cut.sql")
SPACES = "AUTOPAY 4111 1111 1111 1111"
DASHES = "AUTOPAY 4111-1111-1111-1111"
CUT = "AUTOPAY …1111"
CSV_MAP = {"date": "Date", "amount": "Amount", "name": "Description",
           "merchant": "Payee"}
CSV_TEXT = ("Date,Description,Payee,Memo,Amount\n"
            f"2026-03-10,{SPACES},{SPACES},card {SPACES},-50.00\n")


class StoredGroupedNumbersAreCut(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _migrate(self):
        self.conn.execute(MIGRATION.read_text())

    def _row(self, tid, name=SPACES, merchant=SPACES, raw=None, aside=None,
             outlet=None):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " merchant_name, merchant_name_set_aside, merchant_outlet,"
            " removed, raw)"
            " VALUES (%s,'chk','2026-03-10',50,%s,%s,%s,%s,0,%s::jsonb)",
            (tid, name, merchant, aside, outlet, json.dumps(raw or {})))

    def _get(self, tid):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE id = %s", (tid,)).fetchone()

    def _alias(self, raw, canonical, method):
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method)"
            " VALUES (%s,%s,%s)", (raw, canonical, method))

    def _aliases(self):
        return {r["raw_merchant"]: (r["canonical"], r["method"])
                for r in self.conn.execute(
                    "SELECT raw_merchant, canonical, method"
                    " FROM merchant_canonical").fetchall()}

    def test_the_text_columns_of_a_file_row_are_cut(self):
        self._row("csv:aa", aside=DASHES, outlet=f"{SPACES} Gas")
        self._migrate()
        r = self._get("csv:aa")
        self.assertEqual(
            (r["name"], r["merchant_name"], r["merchant_name_set_aside"],
             r["merchant_outlet"]), (CUT, CUT, CUT, f"{CUT} Gas"))

    def test_an_aggregator_row_is_the_feeds_own_word(self):
        self._row("plaid-1")
        self._migrate()
        self.assertEqual(self._get("plaid-1")["name"], SPACES)

    def test_a_date_range_and_a_phone_number_are_left_alone(self):
        text = "Plan 2023-2024 call 617-555-0100 on 12 31 2024"
        self._row("csv:bb", name=text, merchant=text)
        self._migrate()
        self.assertEqual(self._get("csv:bb")["name"], text)

    def test_raw_cells_agree_with_the_live_rule(self):
        raw = {"Details": f"PAID {SPACES}", "Memo/Notes": f"ref {DASHES}",
               "null": [f"x {SPACES}", 12, f"y {DASHES}"],
               "Whatever": SPACES, "account": "Visa 4111 1111 1111 1111",
               "line": f"03/10 {SPACES} 50.00",
               "Ref": "4111 1111 1111 1111",
               "nested": {"memo": SPACES}, "n": 1234,
               "_batches": ["b1"]}
        self._row("csv:cc", raw=raw)
        self._migrate()
        got = self._get("csv:cc")["raw"]
        want = base.mask_file_raw(raw, SPACES, SPACES)
        want["line"] = base.mask_account_name(raw["line"])
        self.assertEqual(got, want)
        self.assertEqual(got["Details"], f"PAID {CUT}")
        self.assertEqual(got["Ref"], "4111 1111 1111 1111")
        self.assertEqual(got["nested"], {"memo": SPACES})

    def test_a_raw_cell_holding_the_name_under_any_header_is_cut(self):
        """The name is cut in the same run; the cell is still found by it."""
        self._row("ofx:dd", raw={"Whatever": f" {SPACES.lower()} "})
        self._migrate()
        self.assertEqual(self._get("ofx:dd")["raw"]["Whatever"],
                         " autopay …1111 ")

    def test_an_aggregators_raw_is_left_alone(self):
        self._row("plaid-2", raw={"memo": SPACES})
        self._migrate()
        self.assertEqual(self._get("plaid-2")["raw"], {"memo": SPACES})

    def test_an_import_filename_is_cut(self):
        self.conn.execute(
            "INSERT INTO import_batches (id, source, account_id, filename)"
            " VALUES ('b1','csv','chk','statement 4111-1111-1111-1111.csv'),"
            "        ('b2','csv','chk','2023-2024.csv')")
        self._migrate()
        self.assertEqual(
            {r["id"]: r["filename"] for r in self.conn.execute(
                "SELECT id, filename FROM import_batches").fetchall()},
            {"b1": "statement …1111.csv", "b2": "2023-2024.csv"})

    def test_a_persons_rename_follows_the_rows_and_the_number_is_gone(self):
        self._row("csv:ee")
        self._alias(SPACES, "Card Payment", "manual")
        self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical,"
            " to_canonical) VALUES (%s,'Autopay','Card Payment')", (SPACES,))
        self._migrate()
        self.assertEqual(self._aliases(), {CUT: ("Card Payment", "manual")})
        self.assertEqual(
            [r["raw_merchant"] for r in self.conn.execute(
                "SELECT raw_merchant FROM merchant_renames").fetchall()], [CUT])

    def test_a_persons_rename_outranks_a_guess_on_the_cut_key(self):
        self._row("csv:ff")
        self._row("csv:gg", name=DASHES, merchant=DASHES)
        self._alias(SPACES, "Guess", "llm")
        self._alias(DASHES, "Mine", "manual")
        self._alias(CUT, "Older guess", "layer1")
        self._migrate()
        self.assertEqual(self._aliases(), {CUT: ("Mine", "manual")})

    def test_an_alias_a_feed_row_still_keys_on_stays_too(self):
        self._row("csv:hh")
        self._row("plaid-3")
        self._alias(SPACES, "Card Payment", "manual")
        self._migrate()
        self.assertEqual(self._aliases(),
                         {SPACES: ("Card Payment", "manual"),
                          CUT: ("Card Payment", "manual")})

    def test_an_alias_no_file_row_carries_is_left_where_it_is(self):
        self._row("plaid-4")
        self._alias(SPACES, "Card Payment", "manual")
        self._migrate()
        self.assertEqual(self._aliases(),
                         {SPACES: ("Card Payment", "manual")})

    def test_a_category_rule_on_the_whole_key_follows_it(self):
        self._row("csv:ii")
        self._row("csv:jj", name=DASHES, merchant=DASHES)
        for key, cat, source in ((SPACES, "SHOPPING", "user"),
                                 (DASHES, "OTHER", "llm"),
                                 (CUT, "FEES", "llm")):
            self.conn.execute(
                "INSERT INTO merchant_categories (merchant, category_primary,"
                " source) VALUES (%s,%s,%s)", (key, cat, source))
        self._migrate()
        self.assertEqual(
            [(r["merchant"], r["category_primary"], r["source"])
             for r in self.conn.execute(
                 "SELECT merchant, category_primary, source"
                 " FROM merchant_categories").fetchall()],
            [(CUT, "SHOPPING", "user")])

    def test_a_second_run_writes_nothing(self):
        self._row("csv:kk", raw={"Memo": SPACES, "line": SPACES})
        self._alias(SPACES, "Card Payment", "manual")
        self.conn.execute(
            "INSERT INTO import_batches (id, source, account_id, filename)"
            " VALUES ('b3','csv','chk','4111 1111 1111 1111.csv')")
        self._migrate()

        def stamps():
            return [self.conn.execute(
                f"SELECT xmin::text AS x, * FROM {t}").fetchall()
                for t in ("transactions", "merchant_canonical",
                          "import_batches")]
        before = stamps()
        self._migrate()
        self.assertEqual(stamps(), before)

    def test_a_row_with_no_number_is_not_written(self):
        self._row("csv:ll", name="COFFEE", merchant="COFFEE",
                  raw={"Ref": "4111 1111 1111 1111"})
        x = self.conn.execute(
            "SELECT xmin::text AS x FROM transactions WHERE id = 'csv:ll'"
        ).fetchone()["x"]
        self._migrate()
        self.assertEqual(self.conn.execute(
            "SELECT xmin::text AS x FROM transactions WHERE id = 'csv:ll'"
        ).fetchone()["x"], x)

    def test_the_sql_cut_and_the_python_cut_agree(self):
        """The migration's SQL is a frozen copy of an earlier, narrower cut
        (its exact agreement with the module is pinned case by case in
        test_card_number_sql_matches_python): it cuts nothing the module
        does not read as a number, and leaves no fewer digits than the
        module would — a cut the module would not make would scar the row
        with one a re-import undoes."""
        rnd = random.Random(203)
        parts = ["4111", "1111", "123456", "12345", "12", "2024",
                 "1234567890123", "x", "…1111", " ", "-", "  ", "AUTOPAY ",
                 " ", "-", " ", "-", ".", "4111 1111 1111 1111",
                 "3782-822463-10005"]
        texts = sorted({"".join(rnd.choice(parts)
                                for _ in range(rnd.randint(1, 12)))
                        for _ in range(1500)})
        for i, t in enumerate(texts):
            self._row(f"csv:z{i}", name=t, merchant=t)
        self._migrate()
        held = 0
        for i, t in enumerate(texts):
            got = self._get(f"csv:z{i}")["name"]
            held += got != t
            self.assertTrue(got == t or card_number.holds_number(t), (t, got))
            self.assertGreaterEqual(sum(c.isdigit() for c in got),
                                    sum(c.isdigit() for c in card_number.cut(t)),
                                    (t, got))
        self.assertGreater(held, 100)


class TheMigrationMeetsTheKeyingPass(unittest.TestCase):
    """The upgrade as it runs: migrations first, then the keying pass."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        # a row as an earlier release left it: under the plain id, whole
        csvimport.import_csv(self.conn, "chk", CSV_TEXT, CSV_MAP)
        self.plain = base.FileImportIds("csv", "chk").id_for(
            dt.date(2026, 3, 10), 50.0, SPACES)
        self.conn.execute(
            "UPDATE transactions SET id = %s, name = %s, merchant_name = %s,"
            " raw = %s::jsonb", (self.plain, SPACES, SPACES, json.dumps(
                {"Date": "2026-03-10", "Description": SPACES, "Payee": SPACES,
                 "Memo": f"card {SPACES}", "Amount": "-50.00"})))
        self.conn.execute("DELETE FROM merchant_canonical")
        self.conn.execute("UPDATE transactions SET merchant_id = NULL")
        merchant_identity.resolve(self.conn)
        self.conn.execute(
            "UPDATE merchant_canonical SET canonical = 'Card Payment',"
            " method = 'manual' WHERE raw_merchant = %s", (SPACES,))
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,'mine')",
            (self.plain,))

    def _upgrade(self):
        self.conn.execute(MIGRATION.read_text())
        return base.key_file_import_ids(self.conn)

    def test_the_cut_row_is_keyed_and_its_references_carry(self):
        self.assertEqual(self._upgrade(), 1)
        [r] = self.conn.execute(
            "SELECT id, name, merchant_name FROM transactions").fetchall()
        self.assertEqual(r["id"], base.keyed_file_id(
            base.txn_id_secret(self.conn), self.plain))
        self.assertEqual((r["name"], r["merchant_name"]), (CUT, CUT))
        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM transaction_notes").fetchone()["txn_id"],
            r["id"])

    def test_a_re_import_finds_it_and_the_rename_still_joins(self):
        mid = self.conn.execute(
            "SELECT merchant_id FROM transactions").fetchone()["merchant_id"]
        self._upgrade()
        [before] = self.conn.execute("SELECT id FROM transactions").fetchall()
        csvimport.import_csv(self.conn, "chk", CSV_TEXT, CSV_MAP)
        [after] = self.conn.execute(
            "SELECT id, name, merchant_id FROM transactions").fetchall()
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["name"], CUT)
        self.assertEqual(after["merchant_id"], mid)
        self.assertEqual(
            self.conn.execute(
                "SELECT canonical, method FROM merchant_canonical"
                " WHERE raw_merchant = %s", (CUT,)).fetchone(),
            {"canonical": "Card Payment", "method": "manual"})

    def test_nothing_is_left_holding_the_number(self):
        self._upgrade()
        for table in ("transactions", "merchant_canonical"):
            for r in self.conn.execute(f"SELECT * FROM {table}").fetchall():
                self.assertNotIn("4111 1111", json.dumps(r, default=str))


if __name__ == "__main__":
    unittest.main()
