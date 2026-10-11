"""The monthly email draws a parent bucket the way the lens page
does: once a carve-out has its own row, the parent row shows what is LEFT of
both its spend and its budget. Taking the carve-out out of the spend but
leaving it in the budget would offer room that does not exist, and the
budgets on the card would add up to more than the plan."""

import datetime as dt
import re
import unittest

from oikonome.web import lens_email

from .util import TODAY, add_txn, make_db, write_config


def _row(html: str, label: str) -> tuple[str, str]:
    m = re.search(rf"<td[^>]*>{re.escape(label)}</td><td[^>]*>([^<]*)</td>"
                  rf"<td[^>]*>of ([^<]*)</td>", html)
    assert m, f"no {label!r} row in the email"
    return m.group(1), m.group(2)


class ParentRowAfterCarveOuts(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=800, other_monthly=1000,
                     custom_buckets=[{"name": "Restaurants", "parent": "food",
                                      "monthly": 300, "categories":
                                      ["RESTAURANTS"], "merchants": []}])

    def tearDown(self):
        self.conn.close()

    def test_monthly_parent_row_budget_leaves_the_carve_out_out(self):
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        add_txn(self.conn, last.replace(day=5), 500.0, "GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, last.replace(day=6), 300.0, "BISTRO",
                primary="FOOD_AND_DRINK", detailed="RESTAURANTS",
                override="RESTAURANTS")
        _, _, html = lens_email.build_monthly(self.conn, TODAY)
        self.assertEqual(_row(html, "Food"), ("$500", "$500"))
        self.assertEqual(_row(html, "Restaurants"), ("$300", "$300"))


if __name__ == "__main__":
    unittest.main()
