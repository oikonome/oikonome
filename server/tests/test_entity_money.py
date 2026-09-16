"""The three money surfaces that must carry the entity predicate.

Hard separation says business-entity accounts stay out of PERSONAL money
math unless combine_entities is on. forecast.build, reporting._live_accounts
and budget.PERSONAL_ONLY_SQL all carry the predicate; without it:

* report.gather's runway `cards` query sums a business card into the
  personal card_debt, so "After paying cards" goes red by the business
  balance while the forecast chart on the SAME page uses the correct
  (personal-only) figure.
* retirement.current_buckets counts an LLC's checking/card as the
  household's nest egg, moving 'earliest feasible retirement' years early.
* upcoming_bill_occurrences' live_debt probe sees a business card's
  balance and silently drops a hand-tracked PERSONAL card-payment bill
  from every cash surface, with nothing replacing it (the forecast's card
  events exclude entity cards).
"""

import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import budget, entities, retirement
from oikonome.web import report

from .util import (TODAY, _admin_dsn, _ensure_db, TEST_DB, add_bill,
                   add_txn, seed_accounts, write_config)


class _EntityFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"emoney-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)          # chk $5,000 / personal card $250
            write_config(conn)
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('bizcard','it1','Biz Amex','credit','credit card',8000)")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current,balance_available) VALUES "
                "('bizchk','it1','Biz Checking','depository','checking',"
                "80000,80000)")
            self.ent = entities.create_entity(
                conn, name="Acme LLC", structure="sole_prop")
            entities.assign_account(conn, "bizcard", self.ent["id"])
            entities.assign_account(conn, "bizchk", self.ent["id"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.admin.close()

    def _conn(self):
        # fresh connection so app.entity_account_ids is recomputed
        return tenancy.tenant_connect(self.tid)

    def _combine(self, on: bool):
        conn = self._conn()
        try:
            entities.set_combined(conn, on)
            conn.commit()
        finally:
            conn.close()


class EntityMoneyTests(_EntityFixture):
    # ---- runway card_debt / headroom ---------------------------------
    def test_runway_card_debt_excludes_business_cards(self):
        conn = self._conn()
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual(st["runway"]["card_debt"], 250.0,
                         "business Amex leaked into personal card_debt")

    def test_runway_card_debt_includes_business_when_combined(self):
        self._combine(True)
        conn = self._conn()
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual(st["runway"]["card_debt"], 8250.0)

    # ---- retirement buckets ---------------------------------------------
    def test_retirement_buckets_exclude_business_accounts(self):
        conn = self._conn()
        try:
            b = retirement.current_buckets(conn)
        finally:
            conn.close()
        # personal: chk 5000 cash, card 250 debt netted out of cash;
        # b["debt"] keeps the raw (personal-only) figure
        self.assertEqual(b["cash"], 4750.0,
                         "LLC balances leaked into the retirement sim")
        self.assertEqual(b["debt"], 250.0,
                         "business Amex leaked into retirement debt")

    def test_retirement_buckets_include_business_when_combined(self):
        self._combine(True)
        conn = self._conn()
        try:
            b = retirement.current_buckets(conn)
        finally:
            conn.close()
        self.assertEqual(b["cash"], 5000 + 80000 - 250 - 8000)

    # ---- card-payment bill survives a business card ---------------------
    def test_business_card_does_not_drop_a_personal_card_pay_bill(self):
        conn = self._conn()
        try:
            # no PERSONAL live card debt — the hand-tracked bill is the only
            # signal the (unlinked) personal card exists
            conn.execute(
                "UPDATE accounts SET balance_current = 0 WHERE id = 'card'")
            add_bill(conn, "CHASE CREDIT CRD AUTOPAY", 600.0,
                     next_due=TODAY + dt.timedelta(days=5),
                     last_seen=TODAY - dt.timedelta(days=25))
            conn.commit()
            cfg = budget.load_config(conn)
            occ = budget.upcoming_bill_occurrences(
                conn, cfg, TODAY, TODAY + dt.timedelta(days=30))
        finally:
            conn.close()
        payees = {o["payee"] for o in occ}
        self.assertIn("CHASE CREDIT CRD AUTOPAY", payees,
                      "the business Amex's balance silently deleted the "
                      "personal card-payment bill from the cash surfaces")

    # ---- a per-row entity override is an OVERRIDE, not a union -----------
    def test_row_override_moves_the_expense_not_copies_it(self):
        """A $2,000 payment on Acme's account reassigned to Beta must
        appear in Beta's ledger only. Matched by the account clause AND the
        entity clause it lands in BOTH — $4,000 of expense for one
        payment."""
        from oikonome.engine import books
        conn = self._conn()
        try:
            beta = entities.create_entity(
                conn, name="Beta LLC", structure="sole_prop")
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed)
                   VALUES ('ovr-t','bizchk','2026-03-05',2000.0,
                           'CONTRACTOR PAYMENT',0,0)""")
            # per-row override: this one belongs to Beta
            entities.assign_transaction(conn, "ovr-t", beta["id"])
            conn.commit()
            acme_rows = books.entity_transactions(conn, self.ent["id"])
            beta_rows = books.entity_transactions(conn, beta["id"])
        finally:
            conn.close()
        acme_ids = {t["id"] for t in acme_rows}
        beta_ids = {t["id"] for t in beta_rows}
        self.assertNotIn("ovr-t", acme_ids,
                         "the overridden row still counts for the ACCOUNT's "
                         "entity — one payment, two P&Ls")
        self.assertIn("ovr-t", beta_ids)

    def test_personal_card_debt_still_drops_the_card_pay_bill(self):
        """The rule itself is unchanged: live PERSONAL card debt means
        the card machinery owns the payment, so the bill is skipped."""
        conn = self._conn()
        try:
            add_bill(conn, "CHASE CREDIT CRD AUTOPAY", 600.0,
                     next_due=TODAY + dt.timedelta(days=5),
                     last_seen=TODAY - dt.timedelta(days=25))
            conn.commit()          # personal card still carries $250
            cfg = budget.load_config(conn)
            occ = budget.upcoming_bill_occurrences(
                conn, cfg, TODAY, TODAY + dt.timedelta(days=30))
        finally:
            conn.close()
        self.assertNotIn("CHASE CREDIT CRD AUTOPAY",
                         {o["payee"] for o in occ})


if __name__ == "__main__":
    unittest.main()


class InflowIsPersonalOnlyTests(_EntityFixture):
    """_inflow_rows is the sibling of every outflow query and was the one
    without the personal-only predicate. Money landing in a BUSINESS
    depository account read as household income: it seeds income detection
    and matches paycheck occurrences on the Bills page, so a client paying an
    invoice could be recorded as somebody's salary."""

    def _inflows(self):
        from oikonome.engine import budget
        conn = self._conn()
        try:
            return [dict(r) for r in budget._inflow_rows(
                conn, TODAY - dt.timedelta(days=30),
                TODAY + dt.timedelta(days=1))]
        finally:
            conn.close()

    def setUp(self):
        super().setUp()
        conn = self._conn()
        try:
            add_txn(conn, TODAY, -5000.0, "PERSONAL PAYCHECK",
                    account="chk")
            add_txn(conn, TODAY, -9000.0, "ACME CLIENT WIRE",
                    account="bizchk")
            conn.commit()
        finally:
            conn.close()

    def test_business_deposits_are_not_personal_income(self):
        payees = {r["payee"] for r in self._inflows()}
        self.assertIn("PERSONAL PAYCHECK", payees)
        self.assertNotIn("ACME CLIENT WIRE", payees,
                         "a deposit into a business account is not household "
                         "income")

    def test_combined_view_shows_both(self):
        self._combine(True)
        payees = {r["payee"] for r in self._inflows()}
        self.assertIn("PERSONAL PAYCHECK", payees)
        self.assertIn("ACME CLIENT WIRE", payees,
                      "the combined toggle must still show both")
