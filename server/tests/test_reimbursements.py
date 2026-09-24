"""Explicit reimbursement pairing (web/data): link/unlink lifecycle,
auto-orientation, transfer semantics, shared-deposit refcounting, awaiting
flags, candidate scoping, multi-select."""

import datetime as dt
import unittest

from oikonome.web import data as d

from .util import TODAY, add_txn, make_db, write_config


class ReimbursementTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.exp = add_txn(self.conn, TODAY, 200.0, "OFFICE SUPPLY CO")
        self.dep = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                           account="chk", primary="INCOME")

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def _set_amount(self, tid, amount):
        self.conn.execute("UPDATE transactions SET amount=%s WHERE id=%s",
                          (amount, tid))

    def test_link_sets_transfer_semantics_and_auto_orients(self):
        # pass the DEPOSIT first — orientation must come from signs
        r = d.link_reimbursement(self.conn, self.dep, self.exp)
        self.assertEqual(r, {"expense": self.exp, "reimburse": self.dep})
        self.assertEqual(self._cat(self.exp), "TRANSFER_OUT")
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")
        self.assertEqual(len(d.reimbursement_pairs(self.conn, self.exp)), 1)

    def test_same_direction_rejected(self):
        exp2 = add_txn(self.conn, TODAY, 50.0, "OTHER CHARGE")
        r = d.link_reimbursement(self.conn, self.exp, exp2)
        self.assertIn("error", r)
        self.assertIsNone(self._cat(self.exp))

    def test_unlink_restores_categories(self):
        d.link_reimbursement(self.conn, self.exp, self.dep)
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertIsNone(self._cat(self.exp))
        self.assertIsNone(self._cat(self.dep))
        self.assertEqual(d.reimbursement_pairs(self.conn, self.exp), [])

    def test_recent_pairs_lists_both_sides_newest_first(self):
        """The Matched list both clients render as the undo surface: every
        pair with each side's payee, date and amount, newest first — a
        wrong match leaves the pending list, so this list is the only
        place it can be seen and unlinked."""
        d.link_reimbursement(self.conn, self.exp, self.dep)
        exp2 = add_txn(self.conn, TODAY, 80.0, "SECOND CHARGE")
        dep2 = add_txn(self.conn, TODAY, -80.0, "SECOND PAYBACK",
                       account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, exp2, dep2)
        rows = d.recent_reimbursement_pairs(self.conn)
        self.assertEqual(len(rows), 2)
        by_exp = {r["expense_id"]: r for r in rows}
        self.assertIn(self.exp, by_exp)
        r = by_exp[self.exp]
        self.assertEqual(r["reimburse_id"], self.dep)
        self.assertEqual(r["expense_amount"], 200.0)
        self.assertEqual(r["deposit_amount"], -200.0)
        self.assertTrue(r["expense_payee"])
        self.assertTrue(r["deposit_payee"])
        # unlink empties it and both sides fall back — the undo really undoes
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertEqual(len(d.recent_reimbursement_pairs(self.conn)), 1)
        self.assertIsNone(self._cat(self.exp))

    def test_shared_deposit_keeps_override_until_last_unlink(self):
        """One check covering two expenses: unlinking one keeps the deposit
        classified until the last pair goes."""
        exp2 = add_txn(self.conn, TODAY, 100.0, "OFFICE SUPPLY CO 2")
        self._set_amount(self.dep, -300.0)     # the check pays both
        self.assertEqual(d.link_reimbursements(self.conn, self.dep,
                                               [self.exp, exp2]),
                         {"linked": 2, "errors": []})
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertIsNone(self._cat(self.exp))          # freed
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")   # still paired
        d.unlink_reimbursement(self.conn, exp2, self.dep)
        self.assertIsNone(self._cat(self.dep))

    def test_flag_lifecycle(self):
        """Flag = a to-remember list entry, never a category change."""
        d.flag_reimbursement(self.conn, self.exp)
        d.flag_reimbursement(self.conn, self.exp)   # idempotent
        pend = d.pending_reimbursements(self.conn)
        self.assertEqual([p["id"] for p in pend], [self.exp])
        self.assertIsNone(self._cat(self.exp))      # category untouched
        d.unflag_reimbursement(self.conn, self.exp)
        self.assertEqual(d.pending_reimbursements(self.conn), [])

    def test_flag_cleared_by_matching(self):
        d.flag_reimbursement(self.conn, self.exp)
        d.link_reimbursement(self.conn, self.exp, self.dep)
        self.assertEqual(d.pending_reimbursements(self.conn), [])
        self.assertEqual(self._cat(self.exp), "TRANSFER_OUT")

    def test_flag_missing_txn_noop(self):
        d.flag_reimbursement(self.conn, "no-such-txn")
        self.assertEqual(d.pending_reimbursements(self.conn), [])

    def test_pending_list_oldest_first(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=90), 75.0, "OLD CHARGE")
        d.flag_reimbursement(self.conn, self.exp)
        d.flag_reimbursement(self.conn, old)
        self.assertEqual([p["id"] for p in d.pending_reimbursements(self.conn)],
                         [old, self.exp])

    def test_candidates_opposite_side_sorted_by_amount(self):
        add_txn(self.conn, TODAY, -500.0, "PAYCHECK", account="chk")
        anchor, cands = d.reimbursement_candidates(self.conn, self.exp)
        self.assertEqual(anchor["id"], self.exp)
        self.assertTrue(all(c["amount"] < 0 for c in cands))
        self.assertEqual(cands[0]["id"], self.dep)      # $200 beats $500
        # text filter narrows
        _, cands = d.reimbursement_candidates(self.conn, self.exp, q="paycheck")
        self.assertEqual([c["payee"] for c in cands], ["PAYCHECK"])

    def test_deposit_candidates_from_checking_or_the_charged_card(self):
        """Anchor = a charge → deposit candidates come from checking
        accounts and from the charge's OWN account: a merchant refund is
        credited back to the card that was charged, so a checking-only
        list could never pair a $1 charge with its $1 refund. Credits on
        some other card stay out, and so does the charged card's own
        payment (a settlement, not a refund)."""
        add_txn(self.conn, TODAY, -200.0, "CARD REFUND", account="card")
        add_txn(self.conn, TODAY, -200.0, "CARD PAYMENT", account="card",
                primary="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('card2','it1','Other Card','credit','credit card',0)")
        add_txn(self.conn, TODAY, -200.0, "OTHER CARD CREDIT", account="card2")
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        self.assertEqual(sorted(c["payee"] for c in cands),
                         ["CARD REFUND", "EMPLOYER REIMB"])
        # anchor = the deposit → charge candidates stay account-wide
        _, cands = d.reimbursement_candidates(self.conn, self.dep)
        self.assertIn("OFFICE SUPPLY CO", [c["payee"] for c in cands])

    def test_deposit_candidates_survive_a_dangling_primary_pin(self):
        """The primary-checking pin widens the deposit side, never narrows
        it. A pin pointing at an account id that no longer exists (a
        re-linked institution mints new ids) must not empty the list down
        to card refunds, and a deposit into the household's OTHER
        checking or savings account is still a reimbursement."""
        write_config(self.conn, checking_account_id="gone-with-the-relink")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('chk2','it1','Second Checking','depository','checking',0),"
            "        ('sav','it1','Savings','depository','savings',0)")
        add_txn(self.conn, TODAY, -200.0, "OTHER CHECKING REIMB", account="chk2",
                primary="INCOME")
        add_txn(self.conn, TODAY, -200.0, "SAVINGS REIMB", account="sav",
                primary="INCOME")
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        self.assertEqual(sorted(c["payee"] for c in cands),
                         ["EMPLOYER REIMB", "OTHER CHECKING REIMB",
                          "SAVINGS REIMB"])

    def test_one_deposit_repaying_several_charges_ranks_by_date(self):
        """One insurance check covers a clinic visit, a pharmacy fill and a
        lab bill: against each charge the check is far off in amount but
        days away in time. An exact amount still leads; after that the
        nearest in time comes before the nearest in amount, so the check is
        on top of every charge's list instead of buried under half a year
        of closer-sized credits."""
        charges = [add_txn(self.conn, TODAY - dt.timedelta(days=3), amt, name)
                   for amt, name in ((60.0, "CLINIC VISIT"),
                                     (210.0, "PHARMACY"), (135.0, "LAB"))]
        check = add_txn(self.conn, TODAY - dt.timedelta(days=2), -405.0,
                        "INSURANCE CLAIM PAYMENT", account="chk",
                        primary="INCOME")
        # closer in amount to every charge, but months away
        for i, amt in enumerate((55.0, 215.0, 130.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=100 + i), -amt,
                    f"OLD CREDIT {i}", account="chk", primary="INCOME")
        for c in charges:
            _, cands = d.reimbursement_candidates(self.conn, c)
            self.assertEqual(cands[0]["id"], check,
                             [x["payee"] for x in cands])
        # an exact match beats the date — the $200 pair from setUp
        exact = add_txn(self.conn, TODAY - dt.timedelta(days=1), -50.0,
                        "NEARER DEPOSIT", account="chk", primary="INCOME")
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        self.assertEqual(cands[0]["id"], self.dep)
        self.assertNotEqual(cands[0]["id"], exact)

    def test_equal_candidates_list_in_a_stable_order(self):
        """Two deposits equal on every ranking key list by id, so the top
        of the list never changes between two opens of the same charge."""
        twins = sorted(add_txn(self.conn, TODAY - dt.timedelta(days=4), -90.0,
                               "TWIN CREDIT", account="chk", primary="INCOME",
                               txn_id=tid) for tid in ("zz-twin-b", "zz-twin-a"))
        exp = add_txn(self.conn, TODAY - dt.timedelta(days=4), 30.0, "SHOP")
        for _ in range(3):
            _, cands = d.reimbursement_candidates(self.conn, exp)
            ids = [c["id"] for c in cands if c["id"] in twins]
            self.assertEqual(ids, twins)

    def test_posted_deposit_ranks_before_a_pending_one(self):
        """A pending deposit is replaced when the bank posts it, so a posted
        row is listed first even when the pending one is nearer in time."""
        pend = add_txn(self.conn, TODAY, -75.0, "PENDING CREDIT",
                       account="chk", primary="INCOME", pending=1)
        posted = add_txn(self.conn, TODAY - dt.timedelta(days=20), -75.0,
                         "POSTED CREDIT", account="chk", primary="INCOME")
        exp = add_txn(self.conn, TODAY, 40.0, "SHOP")
        _, cands = d.reimbursement_candidates(self.conn, exp)
        ids = [c["id"] for c in cands]
        self.assertLess(ids.index(posted), ids.index(pend))

    def test_pair_on_a_pending_deposit_follows_it_when_it_posts(self):
        """Linked while pending, the pair moves to the posted row the bank
        replaces it with, and the posted deposit carries the transfer
        category — nothing is left pointing at a removed row."""
        from oikonome.sync import base
        pend = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                       account="chk", primary="INCOME", pending=1,
                       txn_id="pend-reimb")
        r = d.link_reimbursement(self.conn, self.exp, pend)
        self.assertNotIn("error", r)
        base.upsert_transactions(self.conn, [base.Transaction(
            id="posted-reimb", account_id="chk", date=TODAY, amount=-200.0,
            name="EMPLOYER REIMB", category_primary="INCOME",
            raw={"pending_transaction_id": "pend-reimb"})])
        base.mark_removed(self.conn, ["pend-reimb"])
        pairs = d.reimbursement_pairs(self.conn, self.exp)
        self.assertEqual([p["other_id"] for p in pairs], ["posted-reimb"])
        self.assertEqual(self._cat("posted-reimb"), "TRANSFER_IN")
        self.assertEqual(self._cat(self.exp), "TRANSFER_OUT")

    def test_hidden_account_deposits_are_not_offered(self):
        """A hidden account counts toward nothing, pairs included: its
        deposits are not candidates and cannot be linked."""
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,user_removed_at) VALUES "
            "('hid','it1','Hidden Checking','depository','checking',0,now())")
        hidden = add_txn(self.conn, TODAY, -200.0, "HIDDEN REIMB",
                         account="hid", primary="INCOME")
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        self.assertNotIn(hidden, [c["id"] for c in cands])
        r = d.link_reimbursement(self.conn, self.exp, hidden)
        self.assertIn("hidden", r.get("error", ""))
        self.assertIsNone(self._cat(self.exp))

    def test_linked_duplicate_account_offers_each_deposit_once(self):
        """One checking account reached through two connections is two
        account rows carrying one history: only the primary copy's
        deposits are listed, and the duplicate copy cannot be linked."""
        from oikonome.engine import links
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('chk-copy','it1','Test Checking','depository','checking',0)")
        copy = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                       account="chk-copy", primary="INCOME")
        links.create(self.conn, ["chk", "chk-copy"])
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        ids = [c["id"] for c in cands]
        self.assertEqual(ids.count(self.dep), 1)
        self.assertNotIn(copy, ids)
        self.assertIn("error", d.link_reimbursement(self.conn, self.exp, copy))

    def _business_account(self):
        ent = str(self.conn.execute(
            "INSERT INTO business_entity (name, structure) VALUES "
            "('Test Studio LLC','single_member_llc') RETURNING id"
        ).fetchone()["id"])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,entity_id) VALUES "
            "('biz','it1','Studio Checking','depository','checking',0,%s)",
            (ent,))
        return ent

    def test_personal_and_business_money_never_pair(self):
        """A household charge is never offered a business account's
        deposit, a business charge never a household deposit, and linking
        across the line is refused whatever the client sends."""
        self._business_account()
        biz_dep = add_txn(self.conn, TODAY, -200.0, "CLIENT PAYMENT",
                          account="biz", primary="INCOME")
        biz_exp = add_txn(self.conn, TODAY, 200.0, "STUDIO SUPPLIES",
                          account="biz")
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        ids = [c["id"] for c in cands]
        self.assertIn(self.dep, ids)
        self.assertNotIn(biz_dep, ids)
        _, cands = d.reimbursement_candidates(self.conn, biz_exp)
        ids = [c["id"] for c in cands]
        self.assertIn(biz_dep, ids)
        self.assertNotIn(self.dep, ids)
        # the charge side of a household deposit lists no business charge
        _, cands = d.reimbursement_candidates(self.conn, self.dep)
        self.assertNotIn(biz_exp, [c["id"] for c in cands])
        for exp, dep in ((self.exp, biz_dep), (biz_exp, self.dep)):
            for partial in (False, True):
                r = d.link_reimbursement(self.conn, exp, dep, partial=partial)
                self.assertIn("personal and business", r.get("error", ""))
        self.assertEqual(d.recent_reimbursement_pairs(self.conn), [])
        # the same business on both sides pairs normally
        self.assertNotIn("error",
                         d.link_reimbursement(self.conn, biz_exp, biz_dep))

    def test_a_charge_marked_business_pairs_only_with_that_business(self):
        """A business cost on a personal card carries the entity on the
        row itself; it follows the business side, not the account's."""
        ent = self._business_account()
        biz_dep = add_txn(self.conn, TODAY, -200.0, "CLIENT PAYMENT",
                          account="biz", primary="INCOME")
        self.conn.execute("UPDATE transactions SET entity_id=%s WHERE id=%s",
                          (ent, self.exp))
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        ids = [c["id"] for c in cands]
        self.assertIn(biz_dep, ids)
        self.assertNotIn(self.dep, ids)
        self.assertIn("error", d.link_reimbursement(self.conn, self.exp,
                                                    self.dep))

    def _income_this_month(self):
        from oikonome.engine import reporting
        ym = TODAY.strftime("%Y-%m")
        return sum(float(r["amt"] or 0)
                   for r in reporting._income_by(self.conn, "month")
                   if r["p"] == ym)

    def test_partial_link_keeps_the_rest_of_a_deposit_as_income(self):
        """A paycheck that carried one expense back with it: $3,000 in,
        $200 of it repaying a charge. Partial-linked, the charge nets to
        zero and the other $2,800 is still income — not the whole deposit
        dropped from income because a category is all-or-nothing."""
        pay = add_txn(self.conn, TODAY, -3000.0, "EMPLOYER DIRECT DEP",
                      account="chk", primary="INCOME")
        # setUp's own $200 reimbursement is income too until it is linked
        d.link_reimbursement(self.conn, self.exp, self.dep)
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        base_income = self._income_this_month()
        r = d.link_reimbursement(self.conn, charge, pay, partial=True)
        self.assertEqual(r.get("received"), 200.0)
        self.assertNotEqual(self._cat(pay), "TRANSFER_IN")
        self.assertAlmostEqual(self._income_this_month(), base_income - 200.0,
                               places=2)
        self.assertAlmostEqual(base_income, 3000.0, places=2)

    def test_deposit_turns_transfer_once_partial_links_use_all_of_it(self):
        """An insurance check split across several charges is income only
        in the part no charge claimed: once its partial links account for
        all of it, it is a transfer; unlinking one gives the part back."""
        check = add_txn(self.conn, TODAY, -405.0, "INSURANCE CLAIM PAYMENT",
                        account="chk", primary="INCOME")
        a = add_txn(self.conn, TODAY, 270.0, "CLINIC VISIT")
        b = add_txn(self.conn, TODAY, 135.0, "LAB")
        d.link_reimbursement(self.conn, a, check, partial=True)
        self.assertIsNone(self._cat(check))
        d.link_reimbursement(self.conn, b, check, partial=True)
        self.assertEqual(self._cat(check), "TRANSFER_IN")
        d.unlink_reimbursement(self.conn, b, check)
        self.assertIsNone(self._cat(check))
        self.assertEqual(len(d.reimbursement_pairs(self.conn, check)), 1)

    def test_full_link_on_a_much_bigger_deposit_is_made_partial(self):
        """A full link drops the whole deposit from income, so a $3,000
        deposit fully linked to a $200 charge is linked as partial
        instead: the charge nets to zero, the other $2,800 stays income,
        and the result says what was done."""
        pay = add_txn(self.conn, TODAY, -3000.0, "EMPLOYER DIRECT DEP",
                      account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, self.dep, self.exp)
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        base_income = self._income_this_month()
        r = d.link_reimbursement(self.conn, charge, pay)
        self.assertTrue(r.get("partial"))
        self.assertEqual(r.get("received"), 200.0)
        self.assertIn("partial", r.get("note", ""))
        self.assertIsNone(self._cat(pay))
        self.assertIsNone(self._cat(charge))
        self.assertAlmostEqual(self._income_this_month(), base_income - 200.0,
                               places=2)

    def test_charge_anchored_link_turns_partial_and_says_so(self):
        """Both clients anchor on the charge. A full link from a charge's
        page to a much bigger deposit is not an error: it is linked as
        partial, counted in `partial`, explained in `notes`, and the
        deposit's unclaimed part is still income."""
        pay = add_txn(self.conn, TODAY, -3000.0, "EMPLOYER DIRECT DEP",
                      account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, self.dep, self.exp)
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        r = d.link_reimbursements(self.conn, charge, [pay])
        self.assertEqual(r["linked"], 1)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r.get("partial"), 1)
        self.assertTrue(r.get("notes"))
        self.assertAlmostEqual(self._income_this_month(), 2800.0, places=2)

    def test_one_check_linked_charge_by_charge_nets_every_charge(self):
        """One insurance check covering three charges, each linked in
        full from its own page: the first two become partial links (the
        check is bigger than either), the last one uses the check up, and
        then the whole check is a transfer — no income, no spend left."""
        from oikonome.engine import reporting
        d.link_reimbursement(self.conn, self.dep, self.exp)
        check = add_txn(self.conn, TODAY, -405.0, "INSURANCE CLAIM PAYMENT",
                        account="chk", primary="INCOME")
        exps = [add_txn(self.conn, TODAY, amt, name) for amt, name in
                ((60.0, "CLINIC VISIT"), (210.0, "PHARMACY"), (135.0, "LAB"))]
        results = [d.link_reimbursements(self.conn, e, [check]) for e in exps]
        self.assertEqual([r["linked"] for r in results], [1, 1, 1])
        self.assertEqual([r["errors"] for r in results], [[], [], []])
        self.assertTrue(results[0].get("notes"))
        self.assertTrue(results[1].get("notes"))
        self.assertEqual(self._cat(check), "TRANSFER_IN")
        self.assertAlmostEqual(self._income_this_month(), 0.0, places=2)
        spend = self.conn.execute(
            f"SELECT SUM({reporting.NET_AMOUNT}) s FROM transactions t "
            f"WHERE t.id = ANY(%s) AND COALESCE(t.category_override, '') "
            f"<> 'TRANSFER_OUT'", (exps,)).fetchone()["s"]
        self.assertAlmostEqual(float(spend or 0), 0.0, places=2)

    def test_full_link_after_partials_used_the_check_up_records_the_rest(self):
        """A $250 check and two $150 visits, each linked in full: the
        first is made partial for $150, and the second can only draw the
        $100 left — a full pair there would have the check repay $300."""
        check = add_txn(self.conn, TODAY, -250.0, "INSURANCE CLAIM PAYMENT",
                        account="chk", primary="INCOME")
        v1 = add_txn(self.conn, TODAY, 150.0, "CLINIC VISIT")
        v2 = add_txn(self.conn, TODAY, 150.0, "CLINIC VISIT 2")
        r1 = d.link_reimbursement(self.conn, v1, check)
        self.assertEqual((r1.get("partial"), r1.get("received")),
                         (True, 150.0))
        r2 = d.link_reimbursement(self.conn, v2, check)
        self.assertEqual((r2.get("partial"), r2.get("received")),
                         (True, 100.0))
        self.assertTrue(r2.get("note"))
        self.assertIsNone(self._cat(v2))                 # still spend, net 50
        self.assertEqual(self._cat(check), "TRANSFER_IN")
        got = self.conn.execute(
            "SELECT SUM(amount) s FROM reimbursements WHERE reimburse_id=%s",
            (check,)).fetchone()["s"]
        self.assertAlmostEqual(float(got), 250.0, places=2)

    def test_full_link_of_a_charge_partials_already_repaid_is_refused(self):
        """A charge its partial links already paid back in full has
        nothing left for another deposit; a full link to one would drop
        that whole deposit out of income."""
        d1 = add_txn(self.conn, TODAY, -150.0, "INSURANCE CO",
                     account="chk", primary="INCOME")
        other = add_txn(self.conn, TODAY, -80.0, "EMPLOYER REIMB 2",
                        account="chk", primary="INCOME")
        charge = add_txn(self.conn, TODAY, 80.0, "PHARMACY")
        d.link_reimbursement(self.conn, charge, d1, partial=True)
        r = d.link_reimbursement(self.conn, charge, other)
        self.assertIn("fully reimbursed", r.get("error", ""))
        self.assertIsNone(self._cat(other))
        self.assertIsNone(self._cat(charge))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM reimbursements WHERE reimburse_id=%s",
            (other,)).fetchone()["n"], 0)

    def test_repeated_full_link_after_a_downgrade_changes_nothing(self):
        """The same full-link request twice (a double-tap, a retry, the id
        twice in one request) is one link: the repeat stamps nothing, so
        unlinking leaves the charge with no category pin at all."""
        pay = add_txn(self.conn, TODAY, -3000.0, "EMPLOYER DIRECT DEP",
                      account="chk", primary="INCOME")
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        r1 = d.link_reimbursements(self.conn, charge, [pay, pay])
        self.assertEqual((r1["linked"], r1.get("partial")), (1, 1))
        r2 = d.link_reimbursements(self.conn, charge, [pay])
        self.assertEqual(r2["linked"], 0)
        self.assertEqual(r2["errors"], [])
        self.assertTrue(d.link_reimbursement(self.conn, charge, pay)
                        .get("existing"))
        self.assertIsNone(self._cat(charge))
        self.assertIsNone(self._cat(pay))
        d.unlink_reimbursement(self.conn, charge, pay)
        self.assertIsNone(self._cat(charge))
        self.assertIsNone(self._cat(pay))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM manual_categories WHERE transaction_id=%s",
            (charge,)).fetchone())

    def test_partial_after_a_full_link_draws_only_what_is_left(self):
        """A $1,000 deposit fully paired to a $980 charge has $20 left to
        give. A later partial link on it records $20, not $500 — or the
        deposit would repay $1,480."""
        dep = add_txn(self.conn, TODAY, -1000.0, "EXPENSE REPORT",
                      account="chk", primary="INCOME")
        big = add_txn(self.conn, TODAY, 980.0, "AIRFARE")
        other = add_txn(self.conn, TODAY, 500.0, "HOTEL")
        self.assertNotIn("partial", d.link_reimbursement(self.conn, big, dep))
        r = d.link_reimbursement(self.conn, other, dep, partial=True)
        self.assertEqual(r.get("received"), 20.0)
        r = d.link_reimbursement(self.conn, add_txn(
            self.conn, TODAY, 50.0, "TAXI"), dep, partial=True)
        self.assertIn("fully applied", r.get("error", ""))

    def test_unlinking_the_full_pair_while_partials_remain(self):
        """The full pair goes, the partial stays: the deposit is income
        again in the part the partial link does not claim, and the fully
        paired charge is spend again."""
        dep = add_txn(self.conn, TODAY, -1000.0, "EXPENSE REPORT",
                      account="chk", primary="INCOME")
        big = add_txn(self.conn, TODAY, 980.0, "AIRFARE")
        other = add_txn(self.conn, TODAY, 500.0, "HOTEL")
        d.link_reimbursement(self.conn, big, dep)
        d.link_reimbursement(self.conn, other, dep, partial=True)
        d.link_reimbursement(self.conn, self.dep, self.exp)
        self.assertAlmostEqual(self._income_this_month(), 0.0, places=2)
        d.unlink_reimbursement(self.conn, big, dep)
        self.assertIsNone(self._cat(big))
        self.assertIsNone(self._cat(dep))
        self.assertEqual(len(d.reimbursement_pairs(self.conn, dep)), 1)
        self.assertAlmostEqual(self._income_this_month(), 980.0, places=2)

    def test_old_style_transfer_stamp_on_a_partly_claimed_deposit(self):
        """A deposit partial-linked before partial links stopped stamping
        it carries TRANSFER_IN though its links claim only part of it
        (and an archive restores it that way). It still counts as income
        in the part no charge claimed."""
        pay = add_txn(self.conn, TODAY, -3000.0, "EMPLOYER DIRECT DEP",
                      account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, self.dep, self.exp)
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        d.link_reimbursement(self.conn, charge, pay, partial=True)
        d.set_category(self.conn, pay, "TRANSFER_IN")   # the old stamp
        self.assertAlmostEqual(self._income_this_month(), 2800.0, places=2)

    def test_old_style_stamp_does_not_revive_an_aggregator_transfer(self):
        """A deposit the aggregator itself called a transfer was never
        income, partial links or not."""
        xfer = add_txn(self.conn, TODAY, -500.0, "FROM SAVINGS",
                       account="chk", primary="TRANSFER_IN")
        d.link_reimbursement(self.conn, self.dep, self.exp)
        charge = add_txn(self.conn, TODAY, 100.0, "CONFERENCE FEE")
        d.link_reimbursement(self.conn, charge, xfer, partial=True)
        d.set_category(self.conn, xfer, "TRANSFER_IN")
        self.assertAlmostEqual(self._income_this_month(), 0.0, places=2)

    def test_income_net_reaches_the_month_flows(self):
        """The month's money in (the cash-flow page's figure) counts a
        partial-linked deposit only in its unclaimed part."""
        from oikonome.engine import reporting
        pay = add_txn(self.conn, TODAY, -3000.0, "EMPLOYER DIRECT DEP",
                      account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, self.dep, self.exp)
        before = reporting.month_flows(self.conn, TODAY.year, TODAY.month)["in"]
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        d.link_reimbursement(self.conn, charge, pay, partial=True)
        after = reporting.month_flows(self.conn, TODAY.year, TODAY.month)["in"]
        self.assertAlmostEqual(before, 3000.0, places=2)
        self.assertAlmostEqual(after, 2800.0, places=2)

    def test_unlink_keeps_a_transfer_set_by_hand_before_linking(self):
        """Only what the link wrote is undone: a deposit the person had
        already filed as a transfer stays one when its pair is removed."""
        d.set_category(self.conn, self.dep, "TRANSFER_IN")
        d.link_reimbursement(self.conn, self.exp, self.dep)
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")
        self.assertIsNone(self._cat(self.exp))
        # the same through a partial pair
        charge = add_txn(self.conn, TODAY, 50.0, "CONFERENCE FEE")
        d.link_reimbursement(self.conn, charge, self.dep, partial=True)
        d.unlink_reimbursement(self.conn, charge, self.dep)
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")

    def test_unlink_in_link_order_still_frees_a_shared_deposit(self):
        """Two full pairs on one deposit, unlinked oldest first: the stamp
        the links wrote goes with the last of them."""
        exp2 = add_txn(self.conn, TODAY, 100.0, "OFFICE SUPPLY CO 2")
        self._set_amount(self.dep, -300.0)     # the check pays both
        self.assertEqual(d.link_reimbursements(self.conn, self.dep,
                                               [self.exp, exp2]),
                         {"linked": 2, "errors": []})
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")
        d.unlink_reimbursement(self.conn, exp2, self.dep)
        self.assertIsNone(self._cat(self.dep))
        self.assertIsNone(self._cat(exp2))

    def test_charges_in_ignored_accounts_are_not_awaiting(self):
        """A flagged charge in a hidden account or on the duplicate copy of
        a linked account cannot be linked, so it is neither listed as
        awaiting nor alerted on."""
        from oikonome.engine import alerts, links
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,user_removed_at) VALUES "
            "('hid','it1','Hidden Card','credit','credit card',0,now())")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('card-copy','it1','Test Card','credit','credit card',0)")
        links.create(self.conn, ["card", "card-copy"])
        hidden = add_txn(self.conn, TODAY, 40.0, "HIDDEN CHARGE", account="hid")
        copy = add_txn(self.conn, TODAY, 70.0, "COPY CHARGE",
                       account="card-copy")
        for t in (hidden, copy):
            d.flag_reimbursement(self.conn, t)
        self.assertEqual(d.pending_reimbursements(self.conn), [])
        self.assertIsNone(alerts.reimb_pending(self.conn))
        d.flag_reimbursement(self.conn, self.exp)
        self.assertEqual([p["id"] for p in d.pending_reimbursements(self.conn)],
                         [self.exp])
        self.assertEqual(alerts.reimb_pending(self.conn)["total"], 200.0)

    def test_full_link_weighs_a_check_against_every_charge_it_covers(self):
        """One check anchoring several charges in one request is compared
        with all of them together, not refused against the first alone."""
        check = add_txn(self.conn, TODAY, -405.0, "INSURANCE CLAIM PAYMENT",
                        account="chk", primary="INCOME")
        exps = [add_txn(self.conn, TODAY, amt, name) for amt, name in
                ((60.0, "CLINIC VISIT"), (210.0, "PHARMACY"), (135.0, "LAB"))]
        r = d.link_reimbursements(self.conn, check, exps)
        self.assertEqual(r, {"linked": 3, "errors": []})
        self.assertEqual(self._cat(check), "TRANSFER_IN")

    def _partials(self, charge):
        return sorted(float(r["amount"]) for r in self.conn.execute(
            "SELECT amount FROM reimbursements WHERE expense_id=%s "
            "AND partial=1", (charge,)).fetchall())

    def test_slightly_short_deposit_is_partial_not_full(self):
        """A $9,501 check against a $10,000 bill repays $9,501. A full pair
        would take the whole bill out of spend and lose the $499 nobody
        paid back; only rounding may be waved through on the short side."""
        check = add_txn(self.conn, TODAY, -9501.0, "INSURANCE CLAIM PAYMENT",
                        account="chk", primary="INCOME")
        bill = add_txn(self.conn, TODAY, 10000.0, "HOSPITAL")
        r = d.link_reimbursement(self.conn, bill, check)
        self.assertEqual((r.get("partial"), r.get("received")),
                         (True, 9501.0))
        self.assertIsNone(self._cat(bill))
        self.assertEqual(d._charge_room(self.conn, {
            "id": bill, "amount": 10000.0})[0], 499.0)

    def test_full_link_draws_no_more_than_a_used_check_has_left(self):
        """A $250 check already gave $150 to one visit; a $105 visit linked
        in full can only draw the $100 left, and $5 stays spend."""
        check = add_txn(self.conn, TODAY, -250.0, "INSURANCE CLAIM PAYMENT",
                        account="chk", primary="INCOME")
        v1 = add_txn(self.conn, TODAY, 150.0, "CLINIC VISIT")
        v2 = add_txn(self.conn, TODAY, 105.0, "CLINIC VISIT 2")
        d.link_reimbursement(self.conn, v1, check)
        r = d.link_reimbursement(self.conn, v2, check)
        self.assertEqual((r.get("partial"), r.get("received")),
                         (True, 100.0))
        self.assertIsNone(self._cat(v2))

    def test_old_full_pair_on_a_small_deposit_leaves_the_rest_linkable(self):
        """A full pair from before short deposits were made partial: a $150
        check paired in full to a $500 bill repaid $150, not $500, so a
        $350 second check is accepted for the $350 still owed."""
        small = add_txn(self.conn, TODAY, -150.0, "INSURANCE CO",
                        account="chk", primary="INCOME")
        bill = add_txn(self.conn, TODAY, 500.0, "VET")
        self.conn.execute("INSERT INTO reimbursements (expense_id, "
                          "reimburse_id) VALUES (%s,%s)", (bill, small))
        d.set_category(self.conn, bill, "TRANSFER_OUT")
        d.set_category(self.conn, small, "TRANSFER_IN")
        second = add_txn(self.conn, TODAY, -350.0, "INSURANCE CO 2",
                         account="chk", primary="INCOME")
        r = d.link_reimbursement(self.conn, bill, second)
        self.assertEqual((r.get("partial"), r.get("received")),
                         (True, 350.0))
        self.assertEqual(self._cat(second), "TRANSFER_IN")
        self.assertEqual(d._charge_room(self.conn, {
            "id": bill, "amount": 500.0})[0], 0.0)

    def test_two_checks_repay_one_charge_in_either_order(self):
        """$490 and $10 checks repaying one $500 bill, ticked in either
        order, end as two partial links totalling $500 — neither order may
        pass the first as a full pair and then refuse the second."""
        for order in ((490.0, 10.0), (10.0, 490.0)):
            bill = add_txn(self.conn, TODAY, 500.0, f"SURGERY {order[0]}")
            checks = [add_txn(self.conn, TODAY, -amt, f"INSURER {amt}",
                              account="chk", primary="INCOME")
                      for amt in order]
            r = d.link_reimbursements(self.conn, bill, checks)
            self.assertEqual((r["linked"], r["errors"], r.get("partial")),
                             (2, [], 2), order)
            self.assertEqual(self._partials(bill), sorted(order))
            self.assertIsNone(self._cat(bill))

    def test_check_anchor_ignores_a_charge_already_repaid_elsewhere(self):
        """A $1,000 check anchored on a $900 visit another check already
        paired in full, plus a new $100 visit: the $900 visit has nothing
        left to claim, so the check repays only $100 — a partial link —
        and $900 of it stays income."""
        d.link_reimbursement(self.conn, self.exp, self.dep)
        other = add_txn(self.conn, TODAY, -900.0, "INSURER A",
                        account="chk", primary="INCOME")
        visit = add_txn(self.conn, TODAY, 900.0, "CLINIC VISIT")
        d.link_reimbursement(self.conn, visit, other)
        check = add_txn(self.conn, TODAY, -1000.0, "INSURER B",
                        account="chk", primary="INCOME")
        small = add_txn(self.conn, TODAY, 100.0, "LAB")
        r = d.link_reimbursements(self.conn, check, [visit, small])
        self.assertEqual((r["linked"], r.get("partial")), (1, 1))
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(self._partials(small), [100.0])
        self.assertIsNone(self._cat(check))
        self.assertAlmostEqual(self._income_this_month(), 900.0, places=2)

    def test_link_refuses_rows_flowing_the_same_way(self):
        dep2 = add_txn(self.conn, TODAY, -200.0, "OTHER DEPOSIT", account="chk")
        self.assertIn("same direction",
                      d.link_reimbursement(self.conn, self.dep, dep2)["error"])

    def test_charge_candidates_exclude_settlements_and_investments(self):
        """Charge-side candidates: card settlements and investment-account
        internals never appear; an already-TRANSFER_OUT charge STAYS
        selectable."""
        add_txn(self.conn, TODAY, 500.0, "AMEX EPAYMENT", account="chk",
                primary="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('inv','it1','Test Brokerage','investment','brokerage',1000)")
        add_txn(self.conn, TODAY, 300.0, "BROKERAGE INTERNAL", account="inv",
                primary="TRANSFER_OUT")
        d.set_category(self.conn, self.exp, "TRANSFER_OUT")   # old override
        _, cands = d.reimbursement_candidates(self.conn, self.dep)
        payees = [c["payee"] for c in cands]
        self.assertIn("OFFICE SUPPLY CO", payees)
        self.assertNotIn("AMEX EPAYMENT", payees)
        self.assertNotIn("BROKERAGE INTERNAL", payees)

    def test_many_charges_one_check(self):
        """6 charges → 1 reimbursement check in one multi-select shot."""
        exps = [self.exp] + [add_txn(self.conn, TODAY, 30.0 + i, f"OFFICE SUPPLY {i}")
                             for i in range(5)]
        self._set_amount(self.dep, -360.0)     # the check pays all six
        r = d.link_reimbursements(self.conn, self.dep, exps)
        self.assertEqual(r, {"linked": 6, "errors": []})
        for e in exps:
            self.assertEqual(self._cat(e), "TRANSFER_OUT")
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")
        self.assertEqual(len(d.reimbursement_pairs(self.conn, self.dep)), 6)
        d.unlink_reimbursement(self.conn, exps[0], self.dep)
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")   # 5 remain

    def test_multi_link_reports_bad_rows(self):
        dep2 = add_txn(self.conn, TODAY, -50.0, "OTHER DEPOSIT", account="chk")
        r = d.link_reimbursements(self.conn, self.dep, [self.exp, dep2])
        self.assertEqual(r["linked"], 1)         # the charge linked
        self.assertEqual(len(r["errors"]), 1)    # same-direction row reported


class UnlinkSerializesWithLinkTests(unittest.TestCase):
    """Unlink takes the same per-tenant lock as link, so its DELETE, its
    "is anything still paired" read and its category fallback cannot
    interleave with a link and wipe the TRANSFER_IN that link just wrote."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.exp = add_txn(self.conn, TODAY, 200.0, "OFFICE SUPPLY CO")
        self.dep = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                           account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, self.exp, self.dep)

    def test_unlink_waits_for_a_held_link_lock(self):
        import threading
        from oikonome.db import tenancy
        holder = tenancy.tenant_connect(self.tid)
        self.addCleanup(holder.close)
        done = threading.Event()

        def work():
            conn = tenancy.tenant_connect(self.tid)
            try:
                d.unlink_reimbursement(conn, self.exp, self.dep)
            finally:
                conn.close()
                done.set()

        with holder.transaction():
            # the lock link_reimbursement takes, spelled out so this test
            # holds exactly what a concurrent link would
            holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext(COALESCE("
                "current_setting('app.tenant_id', true), '')), "
                "hashtext('reimbursement-link'))")
            t = threading.Thread(target=work, daemon=True)
            t.start()
            self.assertFalse(done.wait(1.0),
                             "unlink ran while a link held the lock")
        self.assertTrue(done.wait(10.0), "unlink never finished")
        t.join(timeout=5)
        self.assertEqual(d.reimbursement_pairs(self.conn, self.exp), [])


class TestCategoryOverride(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_set_and_clear_round_trip(self):
        t = add_txn(self.conn, TODAY, 42.0, "SHOP")
        d.set_category(self.conn, t, "ENTERTAINMENT")
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertEqual(row["category_override"], "ENTERTAINMENT")
        d.clear_category(self.conn, t)
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertIsNone(row["category_override"])

    def test_search_amount_and_total(self):
        add_txn(self.conn, TODAY, 45.0, "TARGET")
        add_txn(self.conn, TODAY, 145.0, "BIGBOX")
        rows, total, s, _, _, _hits = d.search_transactions(self.conn, "45")
        self.assertEqual(total, 2)               # 45.00 and 145.00 both match
        self.assertEqual(s, 190.0)
        rows, total, s, _, _, _hits = d.search_transactions(self.conn, "target")
        self.assertEqual([r["payee"] for r in rows], ["TARGET"])


if __name__ == "__main__":
    unittest.main()


class ASecondFullLinkOnARepaidChargeIsRefused(unittest.TestCase):
    """A full pair repays the whole charge; a second deposit fully linked
    to it would leave income for money nobody is owed."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def test_second_full_link_is_refused(self):
        exp = add_txn(self.conn, TODAY, 120.0, "CITY CLINIC", primary="MEDICAL")
        dep_a = add_txn(self.conn, TODAY, -120.0, "INSURANCE CO", primary="INCOME")
        dep_b = add_txn(self.conn, TODAY, -120.0, "INSURANCE CO", primary="INCOME")
        first = d.link_reimbursements(self.conn, exp, [dep_a])
        self.assertEqual(first["linked"], 1, first)
        second = d.link_reimbursements(self.conn, exp, [dep_b])
        self.assertEqual(second["linked"], 0, second)
        self.assertTrue(second["errors"], second)
        cat = self.conn.execute("SELECT category_override FROM transactions WHERE id=%s",
                                (dep_b,)).fetchone()["category_override"]
        self.assertIsNone(cat)


class LinkMathBoundaries(unittest.TestCase):
    """The dollar allowance is a boundary in cents, not in floats, and a
    full pair made inside it leaves the charge nothing to give twice."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _link(self, exp, dep):
        return d.link_reimbursements(self.conn, exp, [dep])

    def _pairs(self, exp):
        return self.conn.execute(
            "SELECT partial, amount FROM reimbursements WHERE expense_id=%s ORDER BY partial",
            (exp,)).fetchall()

    def test_exactly_a_dollar_short_is_a_full_pair(self):
        exp = add_txn(self.conn, TODAY, 2.47, "PARKING METER", primary="TRANSPORTATION")
        dep = add_txn(self.conn, TODAY, -1.47, "FRIEND PAYBACK", primary="INCOME")
        out = self._link(exp, dep)
        self.assertEqual(out["linked"], 1, out)
        self.assertEqual([p["partial"] for p in self._pairs(exp)], [0])

    def test_a_dollar_and_a_cent_short_is_partial(self):
        exp = add_txn(self.conn, TODAY, 2.48, "PARKING METER", primary="TRANSPORTATION")
        dep = add_txn(self.conn, TODAY, -1.47, "FRIEND PAYBACK", primary="INCOME")
        out = self._link(exp, dep)
        self.assertEqual(out["linked"], 1, out)
        self.assertEqual([p["partial"] for p in self._pairs(exp)], [1])

    def test_a_full_pair_inside_the_allowance_leaves_no_room(self):
        exp = add_txn(self.conn, TODAY, 1000.0, "HOSPITAL", primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -999.5, "INSURANCE CO", primary="INCOME")
        self.assertEqual(self._link(exp, dep)["linked"], 1)
        room, _ = d._charge_room(self.conn, {"id": exp, "amount": 1000.0})
        self.assertEqual(room, 0.0)
        other = add_txn(self.conn, TODAY, -50.0, "INSURANCE CO", primary="INCOME")
        out = self._link(exp, other)
        self.assertEqual(out["linked"], 0, out)

    def test_a_charge_already_partly_paid_by_this_check_is_not_weighed_twice(self):
        a = add_txn(self.conn, TODAY, 600.0, "CLINIC A", primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 400.0, "CLINIC B", primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -1000.0, "INSURANCE CO", primary="INCOME")
        # $300 of the check already went to A (another $300 came from a
        # check that was since unlinked), so A still needs $300
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial, amount) "
            "VALUES (%s,%s,1,300)", (a, dep))
        out = d.link_reimbursements(self.conn, dep, [a, b])
        self.assertEqual(out["linked"], 1, out)
        cat = self.conn.execute("SELECT category_override FROM transactions WHERE id=%s",
                                (dep,)).fetchone()["category_override"]
        self.assertIsNone(cat, "the check still has $300 nobody claimed")
        pairs_b = self._pairs(b)
        self.assertEqual(len(pairs_b), 1)
        self.assertEqual(pairs_b[0]["partial"], 1)
