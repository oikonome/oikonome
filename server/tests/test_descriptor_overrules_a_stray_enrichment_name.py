"""A bank descriptor with a habit outranks a one-off enrichment name.

An aggregator's merchant name with no entity id behind it is a guess, and
the same terminal can be guessed differently on different days: a line the
ledger has filed under one grocer fifty times comes back once named after
a park. Taken at its word that row mints a second merchant, and the
household is asked to merge something it has seen fifty times. The
descriptor is the better witness — but only where it has a HABIT: rows it
has carried where the aggregator named the payee and the line says that
name too. Two sources agreeing, over and over. Rows nobody named count for
nothing, so a bank's constant line can never pull a new payee into an old
one."""

import unittest

from oikonome.engine import (merchant_dedup, merchant_identity, merchant_merge,
                             merchant_sql)

from .util import add_txn, as_date, jsonb, make_db, write_config

# the terminal glued the city onto a cut name, as card terminals do
LINE = "TapPay ASHBURY'S HARVSPRINGFIELD OR"
USUAL = "Ashbury's Harv"
STRAY = "Ashbury Park"


class StrayEnrichmentNameTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    def _habit(self, line=LINE, name=USUAL, n=merchant_identity.ESTABLISHED_ROWS):
        ids = [add_txn(self.conn, f"2026-08-{d + 1:02d}", 20.0 + d, line,
                       merchant=name, txn_id=f"h{line[:3]}{d}") for d in range(n)]
        merchant_identity.resolve(self.conn)
        return ids

    def _bare(self, line, txn_id, day="2026-08-01"):
        """A row the aggregator never named at all."""
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES (%s,'card',%s,7.25,%s,NULL,'GENERAL_MERCHANDISE',
                       0,0,%s)""",
            (txn_id, as_date(day), line, jsonb({})))
        return txn_id

    def _merchant_of(self, txn_id):
        return self.conn.execute(
            "SELECT m.id, m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id=%s",
            (txn_id,)).fetchone()

    def _names(self):
        return {r["name"] for r in self.conn.execute(
            "SELECT name FROM merchants WHERE merged_into IS NULL").fetchall()}

    def _alias_for(self, raw):
        row = self.conn.execute(
            "SELECT canonical, method, merchant_id FROM merchant_canonical "
            " WHERE raw_merchant = %s", (raw,)).fetchone()
        return dict(row) if row else None

    def _display_of(self, txn_id):
        """What the ledger shows for the row — the one definition of it."""
        return self.conn.execute(
            f"SELECT {merchant_sql.DISPLAY_MERCHANT} AS d FROM transactions t "
            f" {merchant_sql.MC_JOIN} WHERE t.id = %s", (txn_id,)
        ).fetchone()["d"]

    def test_the_stray_row_joins_the_merchant_its_line_has_always_meant(self):
        usual = self._habit()
        stray = add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY,
                        txn_id="stray1")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(stray)["id"],
                         self._merchant_of(usual[0])["id"])
        self.assertNotIn(STRAY, self._names())
        # nothing was minted, so there is nothing to offer a merge for
        merchant_merge.run(self.conn)
        self.assertEqual(merchant_merge.pending(self.conn), [])

    def test_the_stray_name_itself_is_left_unmapped(self):
        """The answer belongs to the descriptor. A real business of that
        name, arriving under its own line, must get its own merchant."""
        self._habit()
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY, txn_id="stray2")
        merchant_identity.resolve(self.conn)
        self.assertIsNone(self.conn.execute(
            "SELECT merchant_id FROM merchant_canonical WHERE raw_merchant=%s "
            "AND merchant_id IS NOT NULL", (STRAY,)).fetchone())
        real = add_txn(self.conn, "2026-09-20", 4.00, "ASHBURY PARK KIOSK 12",
                       merchant=STRAY, txn_id="real1")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(real)["name"], STRAY)

    def test_a_second_stray_row_follows_the_first(self):
        usual = self._habit()
        for k, day in enumerate(("2026-04-14", "2026-04-21")):
            add_txn(self.conn, day, 7.40, LINE, merchant=STRAY, txn_id=f"again{k}")
            merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("again1")["id"],
                         self._merchant_of(usual[0])["id"])

    def test_a_group_is_never_its_own_witness_about_its_line(self):
        """The descriptor answers with what OTHER rows on the line were
        given. A group allowed to cite its own last answer confirms it for
        good — and a re-resolve exists precisely so a better string clean
        can move those rows to the name it now makes."""
        line = "NORTHWIND INS 5512"
        self._bare(line, "own1", "2026-08-01")
        # an older, worse cleaning of that line, and rows resolved under it
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "                                as_of) "
            "VALUES (%s,'Ins','layer1',now())", (line,))
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("own1")["name"], "Ins")
        better = merchant_dedup.canonical_merchant(line)
        self.assertNotEqual(better, "Ins")
        self.conn.execute(
            "UPDATE merchant_canonical SET canonical = %s "
            " WHERE raw_merchant = %s AND method = 'layer1'", (better, line))

        merchant_identity.resolve(self.conn, only_unresolved=False)
        self.assertEqual(self._merchant_of("own1")["name"], better)
        self.assertEqual(self._alias_for(line)["merchant_id"],
                         self._merchant_of("own1")["id"])

    def test_a_layer_one_alias_for_the_name_is_nobody_answering(self):
        """The nightly string clean mints an alias for every key it sees,
        with no merchant on it. Such a row is the machine having noticed a
        string, not anyone having answered for it, so the line may still
        overrule the name."""
        usual = self._habit()
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY, txn_id="re1")
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, as_of) "
            "VALUES (%s,%s,'layer1',now()) ON CONFLICT DO NOTHING", (STRAY, STRAY))
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("re1")["id"],
                         self._merchant_of(usual[0])["id"])

    def test_it_survives_the_nightly_string_clean(self):
        """The nightly pass recomputes every machine-written canonical
        from the raw string and re-resolves the rows whose answer moved.
        A bank line cleans to a name of its own — nothing like the payee's
        — so after that pass the line's alias no longer mirrors the
        merchant these rows joined. Read as an answer, it sends the whole
        group to a merchant named after the bank's descriptor, standing
        beside the payee it belongs to with the line's alias pointing at
        it. The moved charge and the line's unnamed rows must both stay
        where they are, and nothing may be minted."""
        usual = self._habit()
        home = self._merchant_of(usual[0])["id"]
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY, txn_id="re1")
        self._bare(LINE, "refund1", "2026-09-19")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("re1")["id"], home)
        self.assertEqual(self._merchant_of("refund1")["id"], home)
        before = self._names()

        merchant_dedup.apply(self.conn)
        # and the pass that follows it, which revisits resolved rows
        merchant_identity.resolve(self.conn, only_unresolved=False)
        self.assertEqual(self._names(), before)
        self.assertEqual(self._merchant_of("re1")["id"], home)
        self.assertEqual(self._merchant_of("refund1")["id"], home)
        self.assertEqual(self._alias_for(LINE)["merchant_id"], home)
        # the mirror text is the string clean's business and it rewrites it
        # to the cleaned line; what the household reads is the merchant
        # ROW's name, which the alias's merchant_id still reaches
        self.assertEqual(self._display_of("refund1"),
                         self._merchant_of(usual[0])["name"])

    def test_a_whole_ledger_resolved_at_once_gives_the_same_answer(self):
        """A restore resolves every row in one pass; the verdict must not
        depend on the stray key sorting before the usual one."""
        for d in range(merchant_identity.ESTABLISHED_ROWS):
            add_txn(self.conn, f"2026-08-{d + 1:02d}", 20.0, LINE,
                    merchant=USUAL, txn_id=f"all{d}")
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY, txn_id="allstray")
        self.assertLess(STRAY, USUAL)
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("allstray")["id"],
                         self._merchant_of("all0")["id"])

    def test_a_name_the_line_does_contain_is_the_aggregators_to_give(self):
        usual = self._habit()
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant="Tappay Ashbury",
                txn_id="named1")
        merchant_identity.resolve(self.conn)
        self.assertNotEqual(self._merchant_of("named1")["id"],
                            self._merchant_of(usual[0])["id"])

    def test_a_bank_constant_never_pulls_a_new_payee_into_an_old_one(self):
        """"POS DEBIT PURCHASE" names nobody: the payee it has carried so
        far has no claim on the next one."""
        self._habit(line="POS DEBIT PURCHASE", name="Juniper Hardware")
        add_txn(self.conn, "2026-04-14", 31.00, "POS DEBIT PURCHASE",
                merchant="Marlow Bakery", txn_id="const1")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("const1")["name"], "Marlow Bakery")

    def test_a_name_that_extends_the_line_is_detail_not_a_misreading(self):
        """A deposit line is a bank's constant; the aggregator naming the
        payer after it is the only payee evidence there is."""
        self._habit(line="eCheck Deposit", name="Echeck Deposit")
        add_txn(self.conn, "2026-04-14", 480.00, "eCheck Deposit",
                merchant="eCheck Deposit - Marlow Supply", txn_id="ext1")
        merchant_identity.resolve(self.conn)
        self.assertNotEqual(self._merchant_of("ext1")["name"], "Echeck Deposit")

    def test_a_few_rows_are_a_coincidence_not_a_habit(self):
        self._habit(n=merchant_identity.ESTABLISHED_ROWS - 1)
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY, txn_id="few1")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("few1")["name"], STRAY)

    def test_a_line_already_split_between_two_merchants_decides_nothing(self):
        self._habit()
        # a name the line contains is taken at its word: a second merchant
        add_txn(self.conn, "2026-09-01", 5.0, LINE, merchant="Tappay Ashbury",
                txn_id="split1")
        merchant_identity.resolve(self.conn)
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY, txn_id="split2")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("split2")["name"], STRAY)

    def test_a_payment_app_fronting_many_payees_is_not_a_payee(self):
        ids = self._habit(line="PAYLINK INST XFER", name="Paylink")
        self.conn.execute(
            "UPDATE merchants SET kind='payment_app' WHERE id=%s",
            (self._merchant_of(ids[0])["id"],))
        add_txn(self.conn, "2026-04-14", 12.00, "PAYLINK INST XFER",
                merchant="Marlow Bakery", txn_id="app1")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("app1")["name"], "Marlow Bakery")

    def test_a_line_that_has_only_ever_named_itself_has_no_habit(self):
        """Rows the aggregator never named were filed under a merchant the
        string clean made out of the LINE. Asking whether the line names
        that merchant asks nothing: it names itself, always. So a payee the
        aggregator did name is the only payee evidence there is, and it
        stands."""
        line = "JUNIPER MARKET SPRINGFIELD OR"
        for d in range(merchant_identity.ESTABLISHED_ROWS):
            self._bare(line, f"nameless{d}", f"2026-08-{d + 1:02d}")
        merchant_identity.resolve(self.conn)
        add_txn(self.conn, "2026-04-14", 12.00, line, merchant=STRAY,
                txn_id="afterbare")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("afterbare")["name"], STRAY)

    def test_a_constant_with_unnamed_rows_keeps_its_payees_apart(self):
        """The shape a bank constant actually has: a pile of rows nobody
        named, filed under a merchant named after the constant, and then
        two real payees. Three payees, three merchants — whichever order
        the ledger is read in."""
        line = "POS DEBIT PURCHASE"

        def build():
            for d in range(merchant_identity.ESTABLISHED_ROWS):
                self._bare(line, f"c{d}", f"2026-08-{d + 1:02d}")
            for d in range(merchant_identity.ESTABLISHED_ROWS):
                add_txn(self.conn, f"2026-08-{d + 10:02d}", 30.0 + d, line,
                        merchant="Juniper Hardware", txn_id=f"ch{d}")
            add_txn(self.conn, "2026-04-14", 31.00, line,
                    merchant="Marlow Bakery", txn_id="cm")

        build()
        merchant_identity.resolve(self.conn)
        at_once = {self._merchant_of(t)["name"] for t in ("c0", "ch0", "cm")}
        self.assertEqual(len(at_once), 3, at_once)
        self.assertIn("Juniper Hardware", at_once)
        self.assertIn("Marlow Bakery", at_once)

        self.tearDown()
        self.setUp()
        build()
        for t in ("c0", "c1", "c2", "c3", "c4", "ch0", "ch1", "ch2", "ch3",
                  "ch4", "cm"):
            merchant_identity.resolve(self.conn, txn_ids=[t])
        row_by_row = {self._merchant_of(t)["name"] for t in ("c0", "ch0", "cm")}
        self.assertEqual(row_by_row, at_once)

    def test_a_persons_rename_of_the_stray_name_still_wins(self):
        self._habit()
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, as_of) "
            "VALUES (%s,'Ashbury Park Cafe','manual',now())", (STRAY,))
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY, txn_id="man1")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("man1")["name"], "Ashbury Park Cafe")


if __name__ == "__main__":
    unittest.main()
