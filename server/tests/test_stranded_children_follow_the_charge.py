"""What a person wrote on a transaction follows the charge when the row is
retired under one id and lives on under another.

A removal delta after an Item re-link, a vanished hold on a stateless
connector, a full-replace push: each retires a row (`removed = 1`) that the
settlement carry never sees, and every read hides removed rows. The note,
category, receipt, pairing or business tag on it must land on the one live
row that is the same charge — or stay put and be counted when there is no
such row, or two.
"""
import datetime as dt
import unittest

from oikonome.sync import base, reanchor

from .util import TODAY, add_txn, make_db, write_config


def _note(conn, tid, text):
    conn.execute("INSERT INTO transaction_notes (txn_id, note) VALUES (%s, %s)",
                 (tid, text))


def _cat(conn, tid, cat):
    conn.execute("INSERT INTO manual_categories (transaction_id, category) "
                 "VALUES (%s, %s)", (tid, cat))


def _receipt(conn, tid):
    conn.execute("INSERT INTO receipts (txn_id, image, mime) VALUES (%s, %s, %s)",
                 (tid, b"\x89PNG", "image/png"))


def _set_aside(conn, tid, name):
    """The shape the resolver leaves a row in when the aggregator's name for
    it is not what the row's bank line means: the name moves aside and
    merchant_name is left NULL, so the row reads as one nobody named."""
    conn.execute("UPDATE transactions SET merchant_name = NULL, "
                 "merchant_name_set_aside = %s WHERE id = %s", (name, tid))


def _one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


class StrandedChildrenTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_removal_delta_hands_the_row_s_children_to_the_re_numbered_charge(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 42.0, "HARDWARE STORE",
                      override="Home Improvement", txn_id="plaid_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 42.0, "HARDWARE STORE",
                      txn_id="plaid_new")
        _note(self.conn, old, "for the fence")
        _cat(self.conn, old, "Home Improvement")
        _receipt(self.conn, old)
        self.conn.execute("INSERT INTO business_flags (txn_id) VALUES (%s)", (old,))
        self.conn.execute("INSERT INTO business_txn_class (txn_id, bucket) "
                          "VALUES (%s, 'supplies')", (old,))
        # the aggregator retires the old id: the same charge is `new`
        self.assertEqual(base.mark_removed(self.conn, [old]), 1)
        self.assertEqual(_one(self.conn, "SELECT note FROM transaction_notes "
                                         "WHERE txn_id=%s", new)["note"], "for the fence")
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM transaction_notes WHERE txn_id=%s", old))
        self.assertEqual(_one(self.conn, "SELECT category FROM manual_categories "
                                         "WHERE transaction_id=%s", new)["category"],
                         "Home Improvement")
        self.assertEqual(_one(self.conn, "SELECT count(*) AS n FROM receipts "
                                         "WHERE txn_id=%s", new)["n"], 1)
        self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM business_flags WHERE txn_id=%s", new))
        self.assertEqual(_one(self.conn, "SELECT bucket FROM business_txn_class "
                                         "WHERE txn_id=%s", new)["bucket"], "supplies")
        # the row's own override travelled too
        self.assertEqual(_one(self.conn, "SELECT category_override FROM transactions "
                                         "WHERE id=%s", new)["category_override"],
                         "Home Improvement")
        # nothing is left stranded
        self.assertEqual(reanchor.stranded(self.conn), [])

    def test_two_look_alikes_are_ambiguous_and_nothing_moves(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 5.0, "COFFEE", txn_id="c_old")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 5.0, "COFFEE", txn_id="c_a")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 5.0, "COFFEE", txn_id="c_b")
        _note(self.conn, old, "which one?")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["ambiguous"], out["no_twin"]), (0, 1, 0))
        self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM transaction_notes WHERE txn_id=%s", old))

    def test_the_same_name_breaks_a_tie_between_look_alikes(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 12.0, "GROCER", txn_id="g_old")
        same = add_txn(self.conn, TODAY - dt.timedelta(days=3), 12.0, "GROCER", txn_id="g_same")
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 12.0, "PHARMACY", txn_id="g_other")
        _cat(self.conn, old, "Groceries")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(out["moved"], 1)
        self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM manual_categories "
                                             "WHERE transaction_id=%s", same))

    def test_the_live_row_keeps_its_own_answers_and_notes_are_appended(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=4), 30.0, "GAS", txn_id="gas_old",
                      override="Fuel")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=3), 30.0, "GAS", txn_id="gas_new",
                      override="Travel")
        _note(self.conn, old, "trip to the coast")
        _note(self.conn, new, "posted")
        _cat(self.conn, old, "Fuel")
        _cat(self.conn, new, "Travel")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        # the live row refused what it already had an answer for, so the
        # retired row is still carrying something and is counted as such
        self.assertEqual((out["moved"], out["left"]), (0, 1))
        # the live row's own category and override win; the retired row's
        # DIFFERENT answer stays where the person wrote it rather than
        # being deleted for arriving second
        self.assertEqual(_one(self.conn, "SELECT category FROM manual_categories "
                                         "WHERE transaction_id=%s", new)["category"], "Travel")
        self.assertEqual(_one(self.conn, "SELECT category FROM manual_categories "
                                         "WHERE transaction_id=%s", old)["category"], "Fuel")
        self.assertEqual(_one(self.conn, "SELECT category_override FROM transactions "
                                         "WHERE id=%s", new)["category_override"], "Travel")
        # a person's words are never dropped: appended, not overwritten
        self.assertEqual(_one(self.conn, "SELECT note FROM transaction_notes "
                                         "WHERE txn_id=%s", new)["note"],
                         "posted\ntrip to the coast")

    def test_no_twin_means_the_row_stays_and_is_counted(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=40), 99.0, "ONE OFF", txn_id="oo")
        _note(self.conn, old, "kept")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["no_twin"]), (0, 1))
        self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM transaction_notes WHERE txn_id=%s", old))

    def test_a_lone_charge_of_the_same_amount_is_not_the_same_charge(self):
        """One candidate is not evidence on its own. Two unrelated
        purchases of the same amount on one card inside a week are
        ordinary, and carrying a receipt onto the wrong one is silent."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 25.0,
                      "BOOKSHOP", txn_id="b_old")
        other = add_txn(self.conn, TODAY - dt.timedelta(days=2), 25.0,
                        "PIZZA PLACE", txn_id="p_live")
        _note(self.conn, old, "gift for a friend")
        _receipt(self.conn, old)
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["no_twin"]), (0, 1))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM transaction_notes "
                                          "WHERE txn_id=%s", other))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM receipts WHERE txn_id=%s", other))

    def test_two_sellers_behind_one_processor_are_not_the_same_charge(self):
        """A payment processor's prefix is not the payee. Two unrelated
        sellers paid through one processor share the first word of their
        lines and nothing else, so a receipt must not cross between them."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 25.0,
                      "PAYPAL *BOOKSHOP", txn_id="pp_old")
        other = add_txn(self.conn, TODAY - dt.timedelta(days=2), 25.0,
                        "PAYPAL *PIZZA PLACE", txn_id="pp_live")
        _note(self.conn, old, "gift for a friend")
        _receipt(self.conn, old)
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["no_twin"]), (0, 1))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM transaction_notes "
                                          "WHERE txn_id=%s", other))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM receipts WHERE txn_id=%s", other))

    def test_one_brand_selling_two_things_is_not_the_same_charge(self):
        """A shared first word is not evidence: one brand bills unrelated
        purchases under it."""
        self.assertFalse(reanchor._same_payee("PIZZA PLACE DELIVERY",
                                              "PIZZA PLACE CATERING HIRE"))
        self.assertTrue(reanchor._same_payee("SQ *PIZZA PLACE", "PIZZA PLACE #12"))

    def test_a_name_in_any_script_follows_its_charge(self):
        """The same line is the same payee whatever alphabet it is written
        in; a name with no ASCII letters must still be able to match, or a
        household's note stays on a hidden row for ever."""
        for n, name in enumerate(("\u30d1\u30f3\u5c4b", "\u041f\u0435\u043a\u0430\u0440\u043d\u044f 12")):
            old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 12.0 + n,
                          name, txn_id=f"u_old_{n}")
            live = add_txn(self.conn, TODAY - dt.timedelta(days=3), 12.0 + n,
                           name, txn_id=f"u_live_{n}")
            _note(self.conn, old, "breakfast")
            self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
            out = reanchor.reanchor_stranded(self.conn)
            self.assertEqual(out["moved"], 1, name)
            self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM transaction_notes "
                                                 "WHERE txn_id=%s", live))

    def test_a_charge_exactly_a_week_away_is_a_weekly_charge(self):
        """Same card, same amount, same payee, seven days apart: that is
        what a weekly subscription looks like, not a re-numbered charge."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=14), 9.0,
                      "WEEKLY BOX", txn_id="w_old")
        later = add_txn(self.conn, TODAY - dt.timedelta(days=7), 9.0,
                        "WEEKLY BOX", txn_id="w_live")
        _note(self.conn, old, "the one that was short")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["no_twin"]), (0, 1))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM transaction_notes "
                                          "WHERE txn_id=%s", later))

    def test_a_hold_a_person_retired_by_hand_keeps_its_own_work(self):
        """Retiring a stuck hold says "this is not a charge", and it is
        meant to be undone by flipping one column back. Emptying the row
        would make that undo return a blank one — and when such a hold
        does post, the aggregator names it as the predecessor, so the
        settlement carry moves the work with the link in hand."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=5), 60.0,
                      "TICKETS", txn_id="tk_old")
        live = add_txn(self.conn, TODAY - dt.timedelta(days=4), 60.0,
                       "TICKETS", txn_id="tk_live")
        _note(self.conn, old, "refund promised")
        self.conn.execute("UPDATE transactions SET removed=1, retired_at=now() "
                          "WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(out["moved"], 0)
        self.assertEqual(_one(self.conn, "SELECT note FROM transaction_notes "
                                         "WHERE txn_id=%s", old)["note"],
                         "refund promised")
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM transaction_notes "
                                          "WHERE txn_id=%s", live))

    def test_the_second_retired_row_of_a_pair_keeps_its_own_answer(self):
        """Two retired rows can look like the same live charge. The first
        hands its category over; the second finds the place taken — and its
        category must stay on it, not be deleted for arriving late."""
        first = add_txn(self.conn, TODAY - dt.timedelta(days=3), 31.0,
                        "GARDEN CENTRE", txn_id="gc_1")
        second = add_txn(self.conn, TODAY - dt.timedelta(days=3), 31.0,
                         "GARDEN CENTRE", txn_id="gc_2")
        live = add_txn(self.conn, TODAY - dt.timedelta(days=2), 31.0,
                       "GARDEN CENTRE", txn_id="gc_live")
        _cat(self.conn, first, "Home Improvement")
        _cat(self.conn, second, "Groceries")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id = ANY(%s)",
                          ([first, second],))
        reanchor.reanchor_stranded(self.conn)
        rows = self.conn.execute(
            "SELECT transaction_id, category FROM manual_categories").fetchall()
        # one moved onto the live row, the other kept its own — neither
        # person's answer was thrown away
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["category"] for r in rows},
                         {"Home Improvement", "Groceries"})
        self.assertEqual(len([r for r in rows
                              if r["transaction_id"] == live]), 1)

    def test_a_carry_onto_a_pair_a_pull_has_restated_moves_nothing(self):
        """The sweep chose this pair before the write. A full-replace pull
        landing in between un-retires the source row — carrying then empties
        a row the person is looking at — or retires the twin, which would
        move the work onto a row every read hides."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 77.0,
                      "TOOL HIRE", txn_id="th_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 77.0,
                      "TOOL HIRE", txn_id="th_new")
        _note(self.conn, old, "deposit refunded")
        # the source row is live again
        self.assertIsNone(reanchor.carry(self.conn, old, new))
        # and the other way: the twin is the one that went
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (new,))
        self.assertIsNone(reanchor.carry(self.conn, old, new))
        self.assertEqual(_one(self.conn, "SELECT note FROM transaction_notes "
                                         "WHERE txn_id=%s", old)["note"],
                         "deposit refunded")
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM transaction_notes "
                                          "WHERE txn_id=%s", new))

    def test_a_reimbursement_pairing_follows_either_side(self):
        exp = add_txn(self.conn, TODAY - dt.timedelta(days=5), 80.0, "DINNER", txn_id="d_old")
        exp_new = add_txn(self.conn, TODAY - dt.timedelta(days=4), 80.0, "DINNER", txn_id="d_new")
        dep = add_txn(self.conn, TODAY - dt.timedelta(days=1), -80.0, "VENMO", txn_id="v1")
        self.conn.execute("INSERT INTO reimbursements (expense_id, reimburse_id) VALUES (%s, %s)",
                          (exp, dep))
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (exp,))
        reanchor.reanchor_stranded(self.conn)
        self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM reimbursements "
                                             "WHERE expense_id=%s AND reimburse_id=%s", exp_new, dep))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM reimbursements WHERE expense_id=%s", exp))

    def test_a_name_the_resolver_set_aside_still_pairs_hold_with_posted(self):
        """A hold and the charge that settles it can be spelled differently
        enough by the bank that neither line's words contain the other's;
        the aggregator's one name for both is then the only string they
        share. Where the resolver has MOVED that name off the row's key, a
        pairing that reads merchant_name alone sees two bank lines and
        nothing else, and the person's note and receipt stay on a row every
        read hides."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 64.0,
                      "CORNER DELI", txn_id="ah_hold")
        live = add_txn(self.conn, TODAY - dt.timedelta(days=2), 64.0,
                       "ASHBURY HARV MKT SPRINGFIELD",
                       merchant="Ashbury Harvest Market", txn_id="ah_posted")
        _note(self.conn, old, "half of it is for the office")
        _receipt(self.conn, old)
        _set_aside(self.conn, old, "Ashbury Harvest Market")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["no_twin"]), (1, 0))
        self.assertEqual(_one(self.conn, "SELECT note FROM transaction_notes "
                                         "WHERE txn_id=%s", live)["note"],
                         "half of it is for the office")
        self.assertEqual(_one(self.conn, "SELECT count(*) AS n FROM receipts "
                                         "WHERE txn_id=%s", live)["n"], 1)

    def test_a_set_aside_name_breaks_a_tie_between_look_alikes(self):
        """Two same-amount candidates, neither wearing the retired row's
        line: the one the aggregator called what the retired row was called
        is the charge, and the other is a different branch's purchase."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 58.0,
                      "CORNER DELI", txn_id="tie_hold")
        right = add_txn(self.conn, TODAY - dt.timedelta(days=2), 58.0,
                        "ASHBURY HARV MKT SPRINGFIELD",
                        merchant="Ashbury Harvest Market", txn_id="tie_right")
        wrong = add_txn(self.conn, TODAY - dt.timedelta(days=2), 58.0,
                        "ASHBURY HARV MKT RIVERTON",
                        merchant="Ashbury Harvest Riverton", txn_id="tie_wrong")
        _cat(self.conn, old, "Groceries")
        _set_aside(self.conn, old, "Ashbury Harvest Market")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual((out["moved"], out["ambiguous"]), (1, 0))
        self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM manual_categories "
                                             "WHERE transaction_id=%s", right))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM manual_categories "
                                          "WHERE transaction_id=%s", wrong))

    def test_the_retired_rows_own_line_outranks_a_set_aside_name(self):
        """A set-aside name is real evidence but never the best: a
        candidate wearing the retired row's own bank line is what the
        statement shows, and is not passed over for one wearing a name the
        resolver moved off a key."""
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 51.0,
                      "CORNER DELI", txn_id="rank_hold")
        line = add_txn(self.conn, TODAY - dt.timedelta(days=2), 51.0,
                       "CORNER DELI", merchant="Corner Deli",
                       txn_id="rank_line")
        named = add_txn(self.conn, TODAY - dt.timedelta(days=2), 51.0,
                        "ASHBURY HARV MKT SPRINGFIELD",
                        merchant="Ashbury Harvest Market", txn_id="rank_named")
        _cat(self.conn, old, "Groceries")
        _set_aside(self.conn, old, "Ashbury Harvest Market")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(out["moved"], 1)
        self.assertIsNotNone(_one(self.conn, "SELECT 1 FROM manual_categories "
                                             "WHERE transaction_id=%s", line))
        self.assertIsNone(_one(self.conn, "SELECT 1 FROM manual_categories "
                                          "WHERE transaction_id=%s", named))


if __name__ == "__main__":
    unittest.main()
