"""Money correctness: shadow-aware income detection and investment
funding, and the partial-reimbursement floor.

Income — income DETECTION must ignore linked shadow accounts, not just the
reporting side. Blind to the link, two checking sources feeding ONE real
account turn a $3,000 paycheck into a same-day $6,000 event (`_events` sums
same-day amounts), which drives income proposals the user can approve as
real ("biweekly $6k" that is $3k×2), income bill-health, and the forecast's
paycheck median.

Reimbursements — a partial reimbursement must never net NEGATIVE. Without a
floor on the net expression and a cap on linking, over-linking understates
spend: a false UNDER BUDGET.

Investment funding — `_investment_funding` must exclude shadows too, or a
dual-source account's transfers to an investment provider count twice.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget, links, reporting
from oikonome.web import data

from .util import TODAY, add_txn, make_db, write_config


def _source(conn, item_id, aggregator, acct_id, *, typ="depository",
            subtype="checking", mask="9911", bal=1000.0, inst=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING",
        (item_id, aggregator, inst or item_id))
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                 balance_current, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,now())
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (acct_id, item_id, acct_id, typ, subtype, mask, bal))


class IncomeDetectionShadowTests(unittest.TestCase):
    """The same paycheck arriving from two linked sources is ONE paycheck."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _source(self.conn, "pl-1", "plaid", "pl-chk")
        _source(self.conn, "sf-1", "simplefin-org", "sf-chk")
        # the SAME biweekly $3,000 payroll, seen by both sources
        for i in range(6):
            d = TODAY - dt.timedelta(days=14 * i)
            add_txn(self.conn, d, -3000.0, "ACME PAYROLL DIRECT DEP",
                    primary="INCOME", account="pl-chk",
                    txn_id=f"pl-pay-{i}")
            add_txn(self.conn, d, -3000.0, "ACME PAYROLL DIRECT DEP",
                    primary="INCOME", account="sf-chk",
                    txn_id=f"sf-pay-{i}")

    def tearDown(self):
        self.conn.close()

    def _acme_events(self):
        rows = [r for r in bills._income_rows(self.conn, TODAY)
                if "acme" in r["text"]]
        return bills._events(rows)

    def test_unlinked_sources_are_two_real_accounts(self):
        # guard: with NO link these are two genuinely different accounts and
        # the sum is correct — the fix must key on the link, not on payee
        self.assertEqual(len(links.shadow_ids(self.conn)), 0)
        self.assertTrue(any(e["amount"] == 6000.0 for e in self._acme_events()))

    def test_linked_sources_yield_one_paycheck_not_two(self):
        links.create(self.conn, ["pl-chk", "sf-chk"])
        self.assertEqual(links.shadow_ids(self.conn), ["sf-chk"])
        amounts = {e["amount"] for e in self._acme_events()}
        self.assertEqual(amounts, {3000.0},
                         "a dual-source paycheck must not double")

    def _income_proposals(self):
        return self.conn.execute(
            "SELECT payee, amount FROM bill_proposals "
            "WHERE kind='add' AND (evidence->>'income')::bool IS TRUE"
        ).fetchall()

    def test_income_proposal_is_not_doubled(self):
        # the user can APPROVE a proposal — a doubled one becomes a tracked
        # "biweekly $6k" income stream that is really $3k from two sources
        links.create(self.conn, ["pl-chk", "sf-chk"])
        bills.run(self.conn, TODAY)
        acme = [p for p in self._income_proposals()
                if "acme" in (p["payee"] or "").lower()]
        self.assertTrue(acme, "the paycheck series should still be detected")
        self.assertAlmostEqual(acme[0]["amount"], 3000.0, delta=1.0)

    def test_unlinked_proposal_still_sees_both_accounts(self):
        # guard: without a link these are two real accounts, and the doubled
        # figure is the CORRECT answer — the fix keys on the link, not the payee
        bills.run(self.conn, TODAY)
        acme = [p for p in self._income_proposals()
                if "acme" in (p["payee"] or "").lower()]
        self.assertTrue(acme)
        self.assertAlmostEqual(acme[0]["amount"], 6000.0, delta=1.0)


class PartialReimbursementFloorTests(unittest.TestCase):
    """Reimbursements can never make a charge worth negative spend."""

    def setUp(self):
        self.conn = make_db()          # seeds the 'card' fixture account
        write_config(self.conn)
        add_txn(self.conn, TODAY, 400.0, "CITY HOSPITAL", account="card",
                txn_id="exp-1", primary="MEDICAL")

    def tearDown(self):
        self.conn.close()

    def _spend(self):
        rows = budget._spend_rows(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))
        return sum(r["amount"] for r in rows if r["txn_id"] == "exp-1")

    def test_partial_reimbursement_nets_off_the_charge(self):
        add_txn(self.conn, TODAY, -150.0, "INSURANCE CO", account="card",
                txn_id="rb-1", primary="INCOME")
        data.link_reimbursement(self.conn, "exp-1", "rb-1", partial=True)
        self.assertAlmostEqual(self._spend(), 250.0, places=2)

    def test_over_linking_is_clamped_then_refused(self):
        # Remaining-balance semantics: a deposit larger than the charge's remaining
        # need is CLAMPED to the need (the recorded amount, not the raw
        # deposit, is what nets) — the floor invariant still holds: cumulative
        # received never exceeds the charge, spend never goes negative.
        # Once the charge is fully reimbursed, further deposits are refused.
        add_txn(self.conn, TODAY, -150.0, "INSURANCE CO", account="card",
                txn_id="rb-1", primary="INCOME")
        add_txn(self.conn, TODAY, -900.0, "INSURANCE CO", account="card",
                txn_id="rb-2", primary="INCOME")
        add_txn(self.conn, TODAY, -50.0, "INSURANCE CO", account="card",
                txn_id="rb-3", primary="INCOME")
        data.link_reimbursement(self.conn, "exp-1", "rb-1", partial=True)
        out = data.link_reimbursement(self.conn, "exp-1", "rb-2", partial=True)
        self.assertEqual(out.get("received"), 250.0)   # clamped, not 900
        self.assertAlmostEqual(self._spend(), 0.0, places=2)
        out = data.link_reimbursement(self.conn, "exp-1", "rb-3", partial=True)
        self.assertIn("error", out)
        self.assertIn("fully reimbursed", out["error"])
        self.assertAlmostEqual(self._spend(), 0.0, places=2)

    def test_net_is_floored_for_rows_linked_before_the_cap(self):
        # over-linked rows written straight to the table: the floor must
        # still hold, because a negative net SUBTRACTS from real spend
        add_txn(self.conn, TODAY, -900.0, "INSURANCE CO", account="card",
                txn_id="rb-3", primary="INCOME")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial, "
            "amount) VALUES ('exp-1','rb-3',1,900)")
        self.assertEqual(self._spend(), 0.0,
                         "an over-reimbursed charge is worth $0 spend, "
                         "never negative")

    def test_floor_holds_in_reporting_too(self):
        add_txn(self.conn, TODAY, -900.0, "INSURANCE CO", account="card",
                txn_id="rb-4", primary="INCOME")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial, "
            "amount) VALUES ('exp-1','rb-4',1,900)")
        # reporting re-exports the canonical expression — same floor
        self.assertIn("GREATEST", reporting.NET_AMOUNT)
        self.assertEqual(reporting.NET_AMOUNT, budget.NET_AMOUNT)


class InvestmentFundingShadowTests(unittest.TestCase):
    """Dual-source checking must not double-count money into investments."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _source(self.conn, "pl-1", "plaid", "pl-chk", inst="Northwind Bank")
        _source(self.conn, "sf-1", "simplefin-org", "sf-chk",
                inst="Northwind Bank")
        for a in ("pl-chk", "sf-chk"):
            add_txn(self.conn, TODAY, 5000.0, "VANGUARD TRANSFER",
                    account=a, txn_id=f"{a}-inv", primary="TRANSFER_OUT")

    def tearDown(self):
        self.conn.close()

    def _funded(self):
        return dict((y, v) for y, v in reporting._investment_funding(self.conn))

    def test_linked_sources_count_the_transfer_once(self):
        links.create(self.conn, ["pl-chk", "sf-chk"])
        self.assertAlmostEqual(self._funded().get(str(TODAY.year), 0.0),
                               5000.0, places=2)


if __name__ == "__main__":
    unittest.main()
