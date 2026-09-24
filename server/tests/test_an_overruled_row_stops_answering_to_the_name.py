"""A row the bank line outvoted keys on that line, not on the name.

The resolver may decide that the merchant name an aggregator put on one
charge is a misreading of a line it has read the same way many times, and
file the charge under the line's merchant. The row must then stop
answering to the name it was moved off — in every reader, not only in the
ones that remember to ask. So the name MOVES: it goes to
merchant_name_set_aside, merchant_name is left NULL, and the row is
thereafter exactly a row the aggregator never named.

The invariant is that identity key → merchant is a FUNCTION. A rename
collects a merchant's keys and maps them; reconcile moves rows by key; the
split repair and a whole-ledger re-resolve both reason about keys. A row
filed under one merchant while it still answered to a key pointing at
another would drag a real business of that name into the merchant it was
moved to, and be dragged back out by a rename of that business — neither
of which anyone asked for.
"""

import unittest
import uuid

from oikonome.engine import merchant_dedup, merchant_identity

from .util import add_txn, as_date, jsonb, make_db, write_config

# the terminal glued the city onto a cut name, as card terminals do
LINE = "TapPay JUNIPER'S MKTSPRINGFIELD OR"
USUAL = "Juniper's Mkt"
# the merchant these rows settle under: whatever the string clean makes of
# the payee's name, asked rather than spelled out, because how it reads a
# possessive is that module's business and not this behaviour's
USUAL_MERCHANT = merchant_dedup.canonical_merchant(USUAL)
STRAY = "Harbor Lights"
# the real business of that name, under a line of its own
OWN_LINE = "HARBOR LIGHTS CAFE 88"


class OverruledRowKeysOnItsLineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    # ---- fixtures ---------------------------------------------------------

    def _habit(self, conn=None, line=LINE, name=USUAL,
               n=merchant_identity.ESTABLISHED_ROWS):
        """The line and the aggregator agreeing about one payee, n times."""
        conn = conn or self.conn
        return [add_txn(conn, f"2026-08-{d + 1:02d}", 20.0 + d, line,
                        merchant=name, txn_id=f"h{d}") for d in range(n)]

    def _stray(self, txn_id, conn=None, day="2026-09-18"):
        return add_txn(conn or self.conn, day, 9.25, LINE, merchant=STRAY,
                       txn_id=txn_id)

    def _real_business(self, txn_id, conn=None):
        return add_txn(conn or self.conn, "2026-09-20", 4.00, OWN_LINE,
                       merchant=STRAY, txn_id=txn_id)

    def _bare(self, txn_id, conn=None, day="2026-09-19", line=LINE):
        """A row the aggregator never named — a refund, a late arrival."""
        conn = conn or self.conn
        conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES (%s,'card',%s,%s,%s,NULL,'GENERAL_MERCHANDISE',0,0,%s)""",
            (txn_id, as_date(day), 6.50, line, jsonb({})))
        return txn_id

    # ---- readers ----------------------------------------------------------

    def _merchant_of(self, txn_id, conn=None):
        return (conn or self.conn).execute(
            "SELECT m.id, m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id=%s",
            (txn_id,)).fetchone()

    def _aliases_for(self, raw):
        return [dict(r) for r in self.conn.execute(
            "SELECT canonical, method, merchant_id FROM merchant_canonical "
            "WHERE raw_merchant = %s", (raw,)).fetchall()]

    def _cells(self, txn_id):
        """What the row now says its payee is, and what it used to say —
        read the way any other module reads it, by hand."""
        return dict(self.conn.execute(
            "SELECT COALESCE(merchant_name, name) AS payee, merchant_name, "
            "       merchant_name_set_aside AS aside "
            "  FROM transactions WHERE id=%s", (txn_id,)).fetchone())

    def _set_aside(self, txn_id):
        return self._cells(txn_id)["aside"]

    def _settled(self):
        return {r["id"]: r["merchant_id"] for r in self.conn.execute(
            "SELECT id, merchant_id FROM transactions").fetchall()}

    # ---- the four things a moved row must not do --------------------------

    def test_the_moved_row_carries_the_line_as_its_key(self):
        usual = self._habit()
        stray = self._stray("moved")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(stray)["id"],
                         self._merchant_of(usual[0])["id"])
        self.assertEqual(self._set_aside(stray), STRAY)
        # the raw strings the merchant answers to: its own name's key and
        # the bank line — never the name the row was moved off
        raws = merchant_dedup.raw_strings_for(
            self.conn, self._merchant_of(stray)["name"])
        self.assertIn(LINE, raws)
        self.assertNotIn(STRAY, raws)

    def test_a_reader_spelling_the_payee_by_hand_reads_the_line(self):
        """The class property the move exists for. Two dozen queries pick
        the payee with their own COALESCE(merchant_name, name) — the
        budget, the bills, the anomaly sweep, the ledger reads — and a
        flag beside the name would leave every one of them answering to
        the name the ledger had judged a misreading. With the name gone
        from the column there is nothing left to remember."""
        self._habit()
        stray = self._stray("hand")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._cells(stray),
                         {"payee": LINE, "merchant_name": None,
                          "aside": STRAY})

    def test_a_rename_of_that_merchant_never_claims_the_stray_name(self):
        """A rename maps every raw string the merchant displays under. The
        stray name is not one of them, so no alias for it is written — an
        alias would hand the next business of that name to this merchant."""
        self._habit()
        stray = self._stray("ren")
        merchant_identity.resolve(self.conn)
        merchant_dedup.rename(self.conn, USUAL_MERCHANT, "Juniper Market")
        self.assertEqual(self._aliases_for(STRAY), [])
        self.assertEqual(self._merchant_of(stray)["name"], "Juniper Market")

    def test_the_line_is_aliased_to_the_merchant_the_rows_joined(self):
        """The moved rows key on the bank line, so the alias table has to
        answer for THAT key — anything else leaves a key nobody mapped
        while rows carry it. It is the same row an unnamed charge on the
        line writes for itself, so a later unnamed row and a whole-ledger
        re-resolve must read it the same way."""
        self._habit()
        stray = self._stray("alias")
        merchant_identity.resolve(self.conn)
        home = self._merchant_of(stray)["id"]
        self.assertEqual([a["merchant_id"] for a in self._aliases_for(LINE)],
                         [home])
        refund = self._bare("refundalias")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(refund)["id"], home)
        merchant_identity.resolve(self.conn, only_unresolved=False)
        self.assertEqual([a["merchant_id"] for a in self._aliases_for(LINE)],
                         [home])
        self.assertEqual(self._merchant_of(stray)["id"], home)
        self.assertEqual(self._merchant_of(refund)["id"], home)
        self.assertEqual(self._set_aside(stray), STRAY)

    def test_a_machine_alias_for_the_line_naming_nothing_is_pointed_at_it(self):
        """The string clean mints an alias for a key before anything has
        resolved it, and a merchant it once named can be pruned or
        restored without its row. Either way the line's key maps to no
        merchant while the moved rows carry it — so the move points it at
        the merchant they joined, instead of leaving the key answerless
        until some later unnamed charge adopts."""
        for shape in ("no merchant", "a merchant that is gone"):
            with self.subTest(shape=shape):
                self.tearDown()
                self.setUp()
                self._habit()
                merchant_identity.resolve(self.conn)
                gone = ("gone" if shape == "a merchant that is gone"
                        else None) and str(uuid.uuid4())
                self.conn.execute(
                    "INSERT INTO merchant_canonical (raw_merchant, canonical, "
                    "                                method, as_of, merchant_id) "
                    "VALUES (%s,'Tappay Junipers Mkt','layer1',now(),%s)",
                    (LINE, gone))
                stray = self._stray("pointed")
                merchant_identity.resolve(self.conn)
                home = self._merchant_of(stray)["id"]
                self.assertEqual(self._set_aside(stray), STRAY)
                self.assertEqual(
                    [a["merchant_id"] for a in self._aliases_for(LINE)], [home])
                self.assertEqual(self._merchant_of("h0")["id"], home)

    def test_a_persons_alias_for_the_line_is_never_repointed(self):
        """A rename whose merchant row is missing is still the person's
        answer about that line. The rows take the merchant the line's
        habit names, and their answer is left exactly as they made it —
        the same protection every other write in the resolver gives it."""
        self._habit()
        merchant_identity.resolve(self.conn)
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "                                as_of, merchant_id) "
            "VALUES (%s,'Juniper Market Downtown','manual',now(),NULL)",
            (LINE,))
        stray = self._stray("theirsgone")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(stray)["id"],
                         self._merchant_of("h0")["id"])
        self.assertEqual(
            [(a["canonical"], a["method"], a["merchant_id"])
             for a in self._aliases_for(LINE)],
            [("Juniper Market Downtown", "manual", None)])

    def test_the_line_s_own_mapping_is_where_the_moved_rows_go(self):
        """Somebody who has answered for the LINE — renamed it, or split
        it off the merchant it was folded into — has said where charges on
        it belong, and the moved charge carries that line from now on.
        Filing it under the habit's merchant instead would leave one key
        pointing at two merchants, and the next rename would move it
        anyway. Their mapping is left exactly as they made it."""
        self._habit()
        merchant_identity.resolve(self.conn)
        theirs = self.conn.execute(
            "INSERT INTO merchants (name, name_source, kind) "
            "VALUES ('Juniper Market Downtown','manual','merchant') "
            "RETURNING id").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "                                as_of, merchant_id) "
            "VALUES (%s,'Juniper Market Downtown','manual',now(),%s)",
            (LINE, theirs))
        stray = self._stray("theirs")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(stray)["id"], theirs)
        self.assertEqual(self._set_aside(stray), STRAY)
        self.assertEqual(
            [(a["method"], a["merchant_id"]) for a in self._aliases_for(LINE)],
            [("manual", theirs)])

    def test_a_persons_answer_for_that_name_governs_the_rows_carrying_it(self):
        """A rename made AFTER a charge was moved is an answer about the
        name, and the moved charge no longer carries it. So the name maps
        to their merchant alone — one key, one merchant — the moved charge
        stays where its line put it, and a charge that arrives named that
        way later follows their answer, even on the line with the habit."""
        self._habit()
        stray = self._stray("keep")
        merchant_identity.resolve(self.conn)
        home = self._merchant_of(stray)["id"]
        real = self._real_business("realkeep")
        merchant_identity.resolve(self.conn)
        merchant_dedup.rename(self.conn, STRAY, "Harbor Lights Cafe")
        theirs = self._merchant_of(real)["id"]
        self.assertEqual(self._merchant_of(stray)["id"], home)
        self.assertEqual([a["merchant_id"] for a in self._aliases_for(STRAY)],
                         [theirs])
        later = self._stray("later", day="2026-09-28")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(later)["id"], theirs)
        self.assertIsNone(self._set_aside(later))

    def test_the_real_business_of_that_name_gets_its_own_merchant(self):
        """Before the rename and after it: the name was never mapped, so
        nothing is waiting to swallow it."""
        for after_rename in (False, True):
            with self.subTest(after_rename=after_rename):
                # a fresh ledger per case; the one before it goes back to
                # the pool first, or the pool is a connection short for good
                self.tearDown()
                self.setUp()
                self._habit()
                stray = self._stray("own")
                merchant_identity.resolve(self.conn)
                if after_rename:
                    merchant_dedup.rename(self.conn, USUAL_MERCHANT,
                                          "Juniper Market")
                real = self._real_business("realown")
                merchant_identity.resolve(self.conn)
                self.assertEqual(self._merchant_of(real)["name"], STRAY)
                self.assertNotEqual(self._merchant_of(real)["id"],
                                    self._merchant_of(stray)["id"])

    def test_renaming_that_business_leaves_the_moved_row_where_it_is(self):
        """The rename moves rows by KEY. The moved row's key is the bank
        line, so a rename of the business it was never part of cannot
        reach it."""
        self._habit()
        stray = self._stray("stay")
        merchant_identity.resolve(self.conn)
        home = self._merchant_of(stray)["id"]
        self._real_business("realstay")
        merchant_identity.resolve(self.conn)
        merchant_dedup.rename(self.conn, STRAY, "Harbor Lights Cafe")
        self.assertEqual(self._merchant_of(stray)["id"], home)
        self.assertEqual(self._merchant_of("realstay")["name"],
                         "Harbor Lights Cafe")

    def test_a_whole_ledger_re_resolve_changes_nothing(self):
        """The decision is stored, not re-derived: a backfill that revisits
        every resolved row must reach the same answer, twice over."""
        self._habit()
        stray = self._stray("stable")
        merchant_identity.resolve(self.conn)
        real = self._real_business("realstable")
        merchant_identity.resolve(self.conn)
        home, own = self._merchant_of(stray)["id"], self._merchant_of(real)["id"]
        self.assertNotEqual(home, own)
        before = self._settled()
        for _ in range(2):
            merchant_identity.resolve(self.conn, only_unresolved=False)
            self.assertEqual(self._settled(), before)
            self.assertEqual(self._merchant_of(stray)["id"], home)
            self.assertEqual(self._merchant_of(real)["id"], own)
            self.assertEqual(self._set_aside(stray), STRAY)

    # ---- the line goes on meaning what it meant ---------------------------

    def test_a_later_unnamed_row_on_that_line_joins_the_same_merchant(self):
        """The moved row's name is set aside, so it is not a second opinion
        about the line. A refund arriving with no name at all still adopts
        the payee the line means."""
        self._habit()
        stray = self._stray("adopt")
        merchant_identity.resolve(self.conn)
        refund = self._bare("refund")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(refund)["id"],
                         self._merchant_of(stray)["id"])

    def test_an_unnamed_row_in_the_same_pass_joins_it_too(self):
        self._habit()
        stray = self._stray("adopt2")
        refund = self._bare("refund2")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(refund)["id"],
                         self._merchant_of(stray)["id"])

    def test_one_pass_over_the_ledger_answers_as_row_by_row_did(self):
        """A restore resolves everything at once; a sync resolves a row at
        a time. Both must file every row under the same payee."""
        seen = ("h0", "stray", "bare")
        self._habit()
        for tid, add in (("stray", self._stray), ("bare", self._bare)):
            add(tid)
            merchant_identity.resolve(self.conn)
        row_by_row = {tid: self._merchant_of(tid)["name"] for tid in seen}
        self.tearDown()

        self.setUp()
        self._habit()
        self._stray("stray")
        self._bare("bare")
        merchant_identity.resolve(self.conn)
        at_once = {tid: self._merchant_of(tid)["name"] for tid in seen}
        self.assertEqual(at_once, row_by_row)
        self.assertEqual(len(set(at_once.values())), 1)

    def test_the_same_name_under_a_second_line_is_not_a_one_off(self):
        """A name the aggregator has used under ANOTHER descriptor too is a
        payee it knows, not a slip of this line — so a pass that sees both
        rows at once leaves the name alone. The doubt is about a name that
        appears nowhere but under this one line.

        A row already moved is not re-examined, so the order the two rows
        arrive in decides which answer this ledger ends up with; nothing
        here is retroactive."""
        self._habit()
        self._stray("both")
        self._real_business("realboth")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("both")["name"], STRAY)
        self.assertIsNone(self._set_aside("both"))

    def test_a_row_at_a_time_reads_the_second_line_too(self):
        """The two-lines test is a question about the LEDGER, and the
        answer cannot depend on how the rows were delivered. A sync that
        resolves each row as it arrives sees one of them at a time; asked
        of that batch alone, each row's name looks like a one-off of its
        own line, and the one whose line has the habit would be moved —
        for a ledger a single pass files under one new merchant. The other
        row never brings it back: a resolved row is not revisited."""
        second_line = "SQ *BEACON POINT 77"

        def build():
            self._habit()
            # the habit is settled history: earlier syncs resolved it
            merchant_identity.resolve(self.conn)
            self._stray("p")
            add_txn(self.conn, "2026-09-19", 4.00, second_line, merchant=STRAY,
                    txn_id="q")

        build()
        merchant_identity.resolve(self.conn)
        at_once = {t: self._merchant_of(t)["name"] for t in ("h0", "p", "q")}
        self.assertEqual(at_once["p"], STRAY)
        self.assertEqual(at_once["q"], STRAY)

        for order in (("p", "q"), ("q", "p")):
            with self.subTest(order=order):
                self.tearDown()
                self.setUp()
                build()
                for tid in order:
                    merchant_identity.resolve(self.conn, txn_ids=[tid])
                merchant_identity.resolve(self.conn)
                self.assertEqual(
                    {t: self._merchant_of(t)["name"] for t in ("h0", "p", "q")},
                    at_once)
                self.assertIsNone(self._set_aside("p"))

    def test_a_name_that_spreads_later_leaves_the_judgement_alone(self):
        """The other way round: the second line turns up after the charge
        was already moved. Nothing here is retroactive — the moved row
        carries its bank line now, so the newcomer is judged on its own
        evidence, and a whole-ledger re-resolve reads the same two keys
        and reaches the same two answers instead of flipping one."""
        self._habit()
        stray = self._stray("first")
        merchant_identity.resolve(self.conn)
        home = self._merchant_of(stray)["id"]
        late = add_txn(self.conn, "2026-09-25", 4.00, "SQ *BEACON POINT 77",
                       merchant=STRAY, txn_id="late")
        merchant_identity.resolve(self.conn)
        own = self._merchant_of(late)["id"]
        self.assertNotEqual(own, home)
        self.assertEqual(self._merchant_of(late)["name"], STRAY)
        for _ in range(2):
            merchant_identity.resolve(self.conn, only_unresolved=False)
            self.assertEqual(self._merchant_of(stray)["id"], home)
            self.assertEqual(self._merchant_of(late)["id"], own)
            self.assertEqual(self._set_aside(stray), STRAY)


if __name__ == "__main__":
    unittest.main()
