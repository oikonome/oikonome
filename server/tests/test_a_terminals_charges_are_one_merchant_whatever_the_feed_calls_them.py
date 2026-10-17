"""One card terminal, one merchant, whatever the feed calls each charge.

A feed with no entity id for a terminal names it from the line, and it
does not name it the same way twice: one terminal can come back as
"Mw Canteen", then as the two-letter stub "Mw", then as "Rvt Canteen" —
one of them outright wrong — while every hold prints one bank line and
every posted charge the same line behind a wallet prefix. Each new name
must not mint a merchant beside the one the household already has.

The sequence below delivers the charges in the order a sync does. The
ledger must end with ONE live merchant: a name the line does not contain
yields to the line's one earlier agreeing row (a settled hold counts),
and a posted charge that repeats the stray guess of the hold it settled
follows the hold.

The hold's set-aside name vouches only for the posted charge linked to
that hold. Another charge carrying the same name under an unrelated line
in the same sync is a different purchase, and it keeps its own merchant —
whichever of the two arrives first."""

import unittest

from oikonome.engine import merchant_identity

from .util import add_txn, make_db, write_config

LINE = "MW CANTEEN"
WALLET = "PAYLOOP MW CANTEEN"


class OneTerminalOneMerchantTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.n = 0

    def tearDown(self):
        self.conn.close()

    def _hold(self, date, amount, name, line=LINE):
        self.n += 1
        tid = add_txn(self.conn, date, amount, line, merchant=name,
                      txn_id=f"hold{self.n}", pending=1)
        merchant_identity.resolve(self.conn)
        return tid

    def _link(self, hold, posted):
        """The feed retires the hold and links the posted charge to it."""
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (hold,))
        self.conn.execute(
            "UPDATE transactions SET pending_transaction_id=%s WHERE id=%s",
            (hold, posted))

    def _post(self, hold, date, amount, name, line=WALLET):
        tid = add_txn(self.conn, date, amount, line, merchant=name,
                      txn_id=f"post{hold[4:]}")
        self._link(hold, tid)
        merchant_identity.resolve(self.conn)
        return tid

    def _mid(self, tid):
        return self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE id=%s",
            (tid,)).fetchone()["merchant_id"]

    def _name_of(self, tid):
        return self.conn.execute(
            "SELECT m.name FROM transactions t JOIN merchants m "
            "  ON m.id = t.merchant_id WHERE t.id=%s", (tid,)).fetchone()["name"]

    def _live(self):
        return sorted(r["name"] for r in self.conn.execute(
            "SELECT name FROM merchants WHERE merged_into IS NULL").fetchall())

    def _alias(self, raw):
        row = self.conn.execute(
            "SELECT merchant_id FROM merchant_canonical WHERE raw_merchant=%s",
            (raw,)).fetchone()
        return row["merchant_id"] if row else None

    def test_the_feeds_three_names_for_one_terminal_are_one_merchant(self):
        h1 = self._hold("2025-03-04", 4.35, "Mw Canteen")
        p1 = self._post(h1, "2025-03-05", 4.35, "Mw Canteen", line=LINE)
        first = self._mid(p1)
        self.assertEqual(self._mid(h1), first)

        h2 = self._hold("2025-03-18", 9.40, "Mw")
        self.assertEqual(self._mid(h2), first)
        p2 = self._post(h2, "2025-03-19", 9.40, "Mw")
        self.assertEqual(self._mid(p2), first)

        h3 = self._hold("2025-04-14", 3.85, "Rvt Canteen")
        self.assertEqual(self._mid(h3), first)
        p3 = self._post(h3, "2025-04-15", 3.85, "Rvt Canteen")
        self.assertEqual(self._mid(p3), first)

        self.assertEqual(self._live(), ["MW Canteen"])
        # the stray names claim no alias: a real business called that,
        # under its own line, still gets its own merchant
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchant_canonical "
            " WHERE raw_merchant IN ('Mw', 'Rvt Canteen')").fetchone()["n"], 0)
        # a whole-ledger re-resolve of the rows as they now stand reaches
        # the same answer: the moved rows carry their lines, and the lines
        # are aliased
        merchant_identity.resolve(self.conn)
        self.assertEqual({self._mid(t) for t in (h1, p1, h2, p2, h3, p3)}, {first})
        self.assertEqual(self._live(), ["MW Canteen"])

    def _two_charges_one_stray_name(self, order, kiosk_named=False):
        """A hold whose stray name its line set aside, then ONE sync
        carrying its posted charge AND an unrelated charge the feed gave
        the same stray name under a line of its own. `order` is which of
        the two the sync delivers first ("together" = one pass).
        `kiosk_named`: the unrelated line already has one earlier row the
        feed named in agreement with it, so its stray name can be
        overruled into that row's merchant."""
        if kiosk_named:
            add_txn(self.conn, "2025-04-20", 7.25, "RIVERTON TRAILHEAD KIOSK",
                    merchant="Trailhead Kiosk", txn_id="k0")
            merchant_identity.resolve(self.conn)
        h1 = self._hold("2025-05-01", 4.35, "Mw Canteen")
        self._post(h1, "2025-05-02", 4.35, "Mw Canteen", line=LINE)
        home = self._mid(h1)
        h2 = self._hold("2025-05-10", 9.40, "Ashbury Park")
        self.assertEqual(self._mid(h2), home)

        def posted():
            add_txn(self.conn, "2025-05-11", 9.40, WALLET,
                    merchant="Ashbury Park", txn_id="p2")
            self._link(h2, "p2")

        def other():
            add_txn(self.conn, "2025-05-11", 16.10, "RIVERTON TRAILHEAD KIOSK",
                    merchant="Ashbury Park", txn_id="u1")

        if order == "together":
            posted(); other()
            merchant_identity.resolve(self.conn)
        else:
            for step in ((posted, other) if order == "posted" else (other, posted)):
                step()
                merchant_identity.resolve(self.conn)
        return home

    def _assert_unrelated_line_untouched(self, home):
        self.assertEqual(self._mid("p2"), home)
        self.assertNotEqual(self._mid("u1"), home)
        self.assertEqual(self._name_of("u1"), "Ashbury Park")
        # the unrelated row keeps the name the feed gave it
        self.assertEqual(self.conn.execute(
            "SELECT merchant_name FROM transactions WHERE id='u1'"
        ).fetchone()["merchant_name"], "Ashbury Park")
        # only the posted charge's own line is aliased to the hold's merchant
        self.assertEqual(self._alias(WALLET), home)
        self.assertNotEqual(self._alias("RIVERTON TRAILHEAD KIOSK"), home)
        # and a later charge on the unrelated line, unnamed, is not pulled in
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES ('u2','card','2025-06-01',5.00,
                       'RIVERTON TRAILHEAD KIOSK',NULL,'GENERAL_MERCHANDISE',
                       0,0,'{}'::jsonb)""")
        merchant_identity.resolve(self.conn)
        self.assertNotEqual(self._mid("u2"), home)

    def test_a_holds_set_aside_name_moves_only_the_charge_it_settled(self):
        home = self._two_charges_one_stray_name("together")
        self._assert_unrelated_line_untouched(home)

    def test_the_answer_does_not_depend_on_which_charge_arrives_first(self):
        for order in ("posted", "other", "together"):
            with self.subTest(order=order):
                self.tearDown()
                self.setUp()
                home = self._two_charges_one_stray_name(order)
                self._assert_unrelated_line_untouched(home)

    def test_a_follower_in_the_same_sync_does_not_shield_a_stray_name(self):
        """The posted charge that follows its hold leaves the stray name's
        group before it settles, so its bank line must not be counted
        among the group's lines either. Counted, it made the unrelated
        charge look like a name the feed uses under two lines — a payee it
        knows — and the stray name was minted as a merchant, permanently,
        only when the two charges shared a sync. Separately, the unrelated
        charge yields to its line's earlier agreeing row; together must
        reach the same answer."""
        seen = {}
        for order in ("posted", "other", "together"):
            with self.subTest(order=order):
                self.tearDown()
                self.setUp()
                home = self._two_charges_one_stray_name(order, kiosk_named=True)
                kiosk = self._mid("k0")
                self.assertNotEqual(kiosk, home)
                self.assertEqual(self._mid("p2"), home)
                self.assertEqual(self._mid("u1"), kiosk)
                self.assertEqual(self._name_of("u1"), "Trailhead Kiosk")
                self.assertNotIn("Ashbury Park", self._live())
                seen[order] = (self._live(), self._name_of("p2"),
                               self._name_of("u1"))
        self.assertEqual(len(set(map(repr, seen.values()))), 1, seen)

    def test_a_charge_following_its_hold_files_where_its_own_line_already_answers(self):
        """The posted charge repeats its hold's set-aside name, but it
        arrives on a line the ledger already files under another merchant.
        The line's mapping wins: the charge keys on that line from now on,
        and filing it under the hold's merchant would re-point the line's
        alias there while the line's earlier rows stayed behind — one key,
        two merchants."""
        bakery_line = "KESTREL BAKERY 042"
        earlier = [add_txn(self.conn, d, 6.20, bakery_line, txn_id=f"k{i}")
                   for i, d in enumerate(("2025-04-01", "2025-04-15"))]
        merchant_identity.resolve(self.conn)
        bakery = self._alias(bakery_line)
        self.assertIsNotNone(bakery)
        self.assertEqual({self._mid(t) for t in earlier}, {bakery})

        h1 = self._hold("2025-05-01", 4.35, "Mw Canteen")
        self._post(h1, "2025-05-02", 4.35, "Mw Canteen", line=LINE)
        home = self._mid(h1)
        self.assertNotEqual(home, bakery)
        h2 = self._hold("2025-05-10", 9.40, "Ashbury Park")
        self.assertEqual(self._mid(h2), home)

        p2 = self._post(h2, "2025-05-11", 9.40, "Ashbury Park", line=bakery_line)
        self.assertEqual(self._mid(p2), bakery)
        self.assertEqual(self._alias(bakery_line), bakery)
        self.assertEqual({self._mid(t) for t in earlier}, {bakery})
        # and a whole-ledger pass over the rows as they now stand agrees
        merchant_identity.resolve(self.conn)
        self.assertEqual({self._mid(t) for t in [*earlier, p2]}, {bakery})
        self.assertEqual(self._alias(bakery_line), bakery)


if __name__ == "__main__":
    unittest.main()
