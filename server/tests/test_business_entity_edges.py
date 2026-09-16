"""Business-entity edge coverage: the business route gate, forecast/savings
hard-separation, per-transaction entity assignment, and self-employed edge
cases (net loss, bad input, 1099 canonical grouping, ON CONFLICT
re-mark)."""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import books, entities, equity, forecast, selfemploy

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   write_config, TODAY)

MONTH_START = TODAY.replace(day=1)
MONTH_END = (MONTH_START + dt.timedelta(days=40)).replace(day=1)


class _EntBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"be-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        write_config(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="sole_prop")

    def tearDown(self):
        self.conn.close()
        self.admin.close()


class GateIntegrationTests(_EntBase):
    """_require_business is the gate every /business route invokes."""

    def test_self_host_ungated(self):
        # no gate installed → _require_business never blocks
        from oikonome.web.api import _require_business
        _require_business(self.conn)


class ForecastSeparationTests(_EntBase):
    def test_business_checking_not_auto_picked(self):
        # a business checking with the LARGEST balance must NOT become the
        # personal cash-runway baseline
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('bizchk','it1','Biz Checking','depository','checking',9999,9999)")
        entities.assign_account(self.conn, "bizchk", self.ent["id"])
        conn = tenancy.tenant_connect(self.tid)   # recompute session vars
        try:
            bal = forecast._checking_balance(conn, {})
        finally:
            conn.close()
        self.assertEqual(bal, 5000)   # the personal checking, not the 9999 biz


class BooksPerTxnTests(_EntBase):
    def test_per_transaction_assignment_shows_in_pnl(self):
        # a business cost on a PERSONAL card, assigned per-transaction
        t = add_txn(self.conn, TODAY, 120.0, "OFFICE DEPOT", account="card")
        entities.assign_transaction(self.conn, t, self.ent["id"])
        p = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(p["operating_expenses"], 120.0)
        # and in the 1099 vendor totals
        v = {x["merchant"]: x for x in
             selfemploy.vendor_totals(self.conn, self.ent["id"], TODAY.year)}
        self.assertIn("OFFICE DEPOT", v)


class EquityDirectionTests(_EntBase):
    def test_contribute_rejects_revenue_txn(self):
        rev = add_txn(self.conn, TODAY, -500.0, "CLIENT PAYMENT",
                      account="card", primary="INCOME")
        with self.assertRaises(ValueError):
            equity.contribute_expense(self.conn, self.ent["id"], rev)
        with self.assertRaises(ValueError):
            equity.reimburse_expense(self.conn, self.ent["id"], rev)


class SavingsSeparationTests(_EntBase):
    def test_business_transfer_excluded_from_goal_net(self):
        from oikonome.engine import savings
        # a savings goal on the personal checking; a business-assigned transfer
        # into it must not count toward the goal
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('sav','it1','Savings','depository','savings',0)")
        g = {"account_id": "sav", "tokens": []}
        biz_in = add_txn(self.conn, TODAY, -300.0, "TRANSFER", account="sav",
                         primary="TRANSFER_IN")
        entities.assign_transaction(self.conn, biz_in, self.ent["id"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            net = savings._matched_net(conn, g, MONTH_START, MONTH_END)
        finally:
            conn.close()
        self.assertEqual(net, 0.0)   # business transfer excluded


class SelfEmployEdgeTests(_EntBase):
    def _biz_account(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('biz','it1','Biz','depository','checking',1000)")
        entities.assign_account(self.conn, "biz", self.ent["id"])

    def test_add_trip_rejects_nonpositive_miles(self):
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                selfemploy.add_trip(self.conn, self.ent["id"], date=TODAY,
                                    miles=bad)

    def test_estimated_tax_net_loss_clamps_to_zero(self):
        self._biz_account()
        # expenses exceed revenue → net loss → everything clamps to 0
        add_txn(self.conn, TODAY, -100.0, "SMALL SALE", account="biz",
                primary="INCOME", txn_id="rev")
        add_txn(self.conn, TODAY, 5000.0, "BIG EXPENSE", account="biz",
                txn_id="exp")
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], TODAY.year)
        self.assertEqual(est["taxable_profit"], 0.0)
        self.assertEqual(est["se_tax"], 0.0)
        self.assertEqual(est["total_estimated"], 0.0)

    def test_mark_vendor_reconflict_unflags(self):
        self._biz_account()
        add_txn(self.conn, TODAY, 800.0, "JANE", account="biz")
        selfemploy.mark_vendor(self.conn, self.ent["id"], "JANE")
        v = {x["merchant"]: x for x in
             selfemploy.vendor_totals(self.conn, self.ent["id"], TODAY.year)}
        self.assertTrue(v["JANE"]["needs_1099"])
        # re-mark (ON CONFLICT) to un-flag
        selfemploy.mark_vendor(self.conn, self.ent["id"], "JANE",
                               reportable=False)
        v = {x["merchant"]: x for x in
             selfemploy.vendor_totals(self.conn, self.ent["id"], TODAY.year)}
        self.assertFalse(v["JANE"]["needs_1099"])

    def test_1099_groups_by_canonical_merchant(self):
        self._biz_account()
        # two descriptors for ONE vendor, each under $600, together over it
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES ('SQ *JANE PLUMBING','Jane Plumbing','test'),"
            "('JANE PLUMBING LLC','Jane Plumbing','test')")
        add_txn(self.conn, TODAY, 400.0, "SQ *JANE PLUMBING", account="biz",
                merchant="SQ *JANE PLUMBING", txn_id="j1")
        add_txn(self.conn, TODAY, 400.0, "JANE PLUMBING LLC", account="biz",
                merchant="JANE PLUMBING LLC", txn_id="j2")
        v = {x["merchant"]: x for x in
             selfemploy.vendor_totals(self.conn, self.ent["id"], TODAY.year)}
        # collapsed to one canonical vendor at $800 (over the threshold)
        self.assertIn("Jane Plumbing", v)
        self.assertEqual(v["Jane Plumbing"]["paid"], 800.0)


if __name__ == "__main__":
    unittest.main()
