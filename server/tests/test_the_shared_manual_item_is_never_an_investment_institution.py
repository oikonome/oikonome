"""Hand-made and file-imported accounts all share the item 'manual'
(institution "Manual"), which is no login and no firm. A hand-made
investment account beside an imported checking account must not make
"Manual" an investment institution for budget.cash_events: that would drop
every inflow to the imported checking (it would read as the firm's own
cash account) and count any inflow whose text says "manual" as money
pulled from investments."""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, add_bill, add_txn, make_db, write_config


class SharedManualItemTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('manual','manual','Manual',NULL) "
            "ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('m-ira','manual','Contoso IRA','investment','ira',40000),"
            "('m-chk','manual','Fabrikam Checking','depository','checking',"
            "2500) ON CONFLICT (tenant_id, id) DO NOTHING")
        add_bill(self.conn, "Acme Payroll", 2600, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=3))

    def tearDown(self):
        self.conn.close()

    def month(self):
        return budget.cash_events(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))

    def test_a_bonus_into_an_imported_checking_is_still_seen(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=4), -6000,
                "ACME PAYROLL BONUS", account="m-chk")
        self.assertEqual(self.month()["bonus"],
                         [((TODAY - dt.timedelta(days=4)).isoformat(),
                           6000.0)])

    def test_an_inflow_saying_manual_is_not_investment_funding(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -1200,
                "MANUAL DEPOSIT", account="chk")
        ev = self.month()
        self.assertEqual(ev["funding_mtd"], [])
        self.assertEqual(ev["large_mtd"], [])


if __name__ == "__main__":
    unittest.main()
