"""The Investment fees table says what each platform's count counts: fee
rows are charges, an estimated advisory fee is counted in years
(specs/shown-figures.md). One bare number per platform read "5" for five
years of estimate beside "40" for forty charges.
"""
import pathlib
import unittest
from unittest import mock

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

_ROOT = pathlib.Path(__file__).resolve().parents[2]


class FeeCountsSayTheirUnit(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('brk','plaid','Contoso Brokerage','t')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('inv','brk','Invented Taxable',"
            "'investment','brokerage',9000)")

    def tearDown(self):
        self.conn.close()

    def test_fee_rows_are_charges(self):
        add_txn(self.conn, "2026-03-31", 25.0, "ADVISORY FEE", account="inv")
        r = reporting.compute_fees(self.conn)
        self.assertEqual(r["platform_counts"], ["1 charge"])
        add_txn(self.conn, "2026-06-30", 25.0, "ADVISORY FEE", account="inv")
        r = reporting.compute_fees(self.conn)
        self.assertEqual([p[2] for p in r["by_platform"]], [2])
        self.assertEqual(r["platform_counts"], ["2 charges"])

    def test_an_estimate_is_counted_in_years(self):
        add_txn(self.conn, "2026-03-31", 25.0, "ADVISORY FEE", account="inv")
        est = {"Fabrikam advisory (estimated 0.4%)":
               {"2024": 30.0, "2025": 40.0, "2026": 50.0}}
        with mock.patch.object(reporting, "_advisory_fee_estimate",
                               return_value=est):
            r = reporting.compute_fees(self.conn)
        by_name = dict(zip((p[0] for p in r["by_platform"]),
                           r["platform_counts"]))
        self.assertEqual(by_name, {
            "Fabrikam advisory (estimated 0.4%)": "3 years, estimated",
            "Contoso Brokerage": "1 charge"})

    def test_both_clients_print_the_words(self):
        web = (_ROOT / "webapp/src/pages/NetWorth.tsx").read_text()
        mob = (_ROOT / "mobile/src/app/networth.tsx").read_text()
        self.assertTrue("fe.platform_counts[i]" in web)
        self.assertTrue("platform_counts?.[i]" in mob)
        self.assertFalse("v={`${m$(amt)} · ${n}`}" in mob)


if __name__ == "__main__":
    unittest.main()
