"""An envelope's paying account is read from the rows the month's own
matcher claims for it (specs/today-figures.md, "Card spending leaves
checking when the household pays the card"): a pool that matches by
CATEGORY — payments to many different payees, each a person or a
bill-pay from checking — is paid from checking, beside a pool matched by
its merchant and paid by card. Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast

from .util import TODAY, add_bill, add_txn, make_db, write_config


class ACategoryPool(unittest.TestCase):
    def test_is_paid_from_checking_beside_a_card_paid_merchant_pool(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            add_bill(conn, "Lessons", 400.0, bill_type="envelope",
                     frequency=None, interval=1, category="GENERAL_SERVICES",
                     match_category=True)
            add_bill(conn, "Garden Center", 150.0, bill_type="envelope",
                     frequency=None, interval=1, merchant="GARDEN CENTER")
            d = TODAY - dt.timedelta(days=150)
            k = 0
            while d < TODAY:
                if d.day in (3, 17):
                    k += 1
                    # a different person or biller each time, from checking
                    add_txn(conn, d, 180.0, f"TRANSFER TO TUTOR {k}",
                            account="chk", primary="GENERAL_SERVICES")
                if d.day == 9:
                    add_txn(conn, d, 70.0, "GARDEN CENTER", account="card",
                            primary="GENERAL_MERCHANDISE")
                d += dt.timedelta(days=1)
            cfg = budget.load_config(conn)
            envs = budget._envelope_bills(conn)
            got = forecast.bill_paying_accounts(conn, cfg, TODAY, envs,
                                                raw=True)
        finally:
            conn.close()
        self.assertEqual(got, {"Lessons": forecast.CHECKING,
                               "Garden Center": "card"})


if __name__ == "__main__":
    unittest.main()
