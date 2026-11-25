"""The Business page's figures are decided on the server: this month's
profit under the year's (books.period_profit, by the P&L's own rules),
the cash in the business (books.entity_cash, a hidden account left out)
and the "Set aside for tax" card (selfemploy.tax_set_aside), whose
"covered by" is the printed cash less the printed still to pay
(specs/shown-figures.md, "Business")."""

import datetime as dt
import unittest
import uuid

from oikonome.engine import books, entities, selfemploy

from .util import make_db


class BusinessFiguresTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.e = entities.create_entity(
            self.conn, name=f"Biz {uuid.uuid4().hex[:6]}",
            structure="sole_prop", business_start_date="2031-01-01")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES ('item-biz','manual','Test Bank') ON CONFLICT DO NOTHING")

    def tearDown(self):
        self.conn.close()

    def _account(self, bal, typ="depository", **cols):
        aid = f"acct-{uuid.uuid4().hex[:8]}"
        keys = ["id", "item_id", "name", "type", "balance_current",
                "entity_id", *cols]
        vals = [aid, "item-biz", "Operating", typ, bal, self.e["id"],
                *cols.values()]
        self.conn.execute(
            f"INSERT INTO accounts ({', '.join(keys)}) VALUES "
            f"({', '.join(['%s'] * len(keys))})", vals)
        return aid

    def _txn(self, acct, day, amount, cat):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "category_primary) VALUES (%s,%s,%s,%s,%s,%s)",
            (f"biz-{uuid.uuid4().hex[:8]}", acct, day, amount, "ROW", cat))

    def test_the_month_is_the_years_rules_on_the_months_rows(self):
        a = self._account(1000.0)
        self._txn(a, dt.date(2031, 5, 3), -900.0, "INCOME")
        self._txn(a, dt.date(2031, 5, 9), 150.0, "GENERAL_SERVICES")
        self._txn(a, dt.date(2031, 5, 12), 300.0, "TRANSFER_OUT")
        self._txn(a, dt.date(2031, 4, 9), 80.0, "GENERAL_SERVICES")
        self.assertEqual(books.period_profit(self.conn, self.e["id"],
                                             "2031-05"),
                         {"period": "2031-05", "net_operating": 750.0})
        whole = books.pnl(self.conn, self.e["id"], 2031)["net_operating"]
        april = books.period_profit(self.conn, self.e["id"],
                                    "2031-04")["net_operating"]
        self.assertEqual(round(april + 750.0, 2), whole)

    def test_cash_leaves_a_hidden_account_out(self):
        self._account(1200.0)
        self._account(300.0, user_removed_at=dt.datetime(2031, 5, 1))
        self._account(-50.0, typ="credit")
        self.assertEqual(books.entity_cash(self.conn, self.e["id"]),
                         {"total": 1200.0, "accounts": 2})

    def _est(self, owed, paid, profit, share=None):
        return {"total_estimated": owed, "covered_tax_paid": paid,
                "covered_profit": profit,
                "covers": "household" if share else "owner",
                "household": {"ownership_pct": share} if share else None}

    def test_the_set_aside_adds_up_as_printed(self):
        # business cash $1,000.60 prints $1,001; $400 still to pay; covered
        # by $601, the printed difference
        self._account(1000.60)
        sa = selfemploy.tax_set_aside(self.conn, self.e["id"],
                                      self._est(900.0, 500.0, 4000.0))
        self.assertEqual(sa["still_due"], 400.0)
        self.assertEqual(sa["held"], 1000.6)
        self.assertEqual(sa["covered"], 601)
        self.assertEqual(sa["rate_of_profit"], 23)  # 22.5% reads 23%
        self.assertIsNone(sa["account"])
        self.assertFalse(sa["stale"])

    def test_paid_past_the_estimate_leaves_nothing_due_and_a_short_reads_negative(self):
        self._account(100.0)
        sa = selfemploy.tax_set_aside(self.conn, self.e["id"],
                                      self._est(500.0, 650.0, 0.0))
        self.assertEqual(sa["still_due"], 0.0)
        self.assertIsNone(sa["rate_of_profit"])
        sa = selfemploy.tax_set_aside(self.conn, self.e["id"],
                                      self._est(500.0, 0.0, 2000.0))
        self.assertEqual(sa["covered"], -400)

    def test_the_nominated_reserve_and_the_households_share(self):
        self._account(9000.0)
        reserve = self._account(2000.0)
        entities.update_entity(self.conn, self.e["id"],
                               tax_reserve_account_id=reserve)
        sa = selfemploy.tax_set_aside(self.conn, self.e["id"],
                                      self._est(800.0, 0.0, 3000.0, 50))
        self.assertEqual(sa["held"], 1000.0)
        self.assertEqual(sa["account"]["id"], reserve)
        # the reserve hidden since: all business cash, and it says so
        self.conn.execute("UPDATE accounts SET user_removed_at = now() "
                          "WHERE id = %s", (reserve,))
        sa = selfemploy.tax_set_aside(self.conn, self.e["id"],
                                      self._est(800.0, 0.0, 3000.0))
        self.assertTrue(sa["stale"])
        self.assertEqual(sa["held"], 9000.0)


if __name__ == "__main__":
    unittest.main()
