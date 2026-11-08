"""A Plaid object stored raw keeps nothing that identifies an account or a
person: no loan or account number, no payment reference number (on a
student loan it is the loan number again), no home address, no owner name.

raw leaves the instance verbatim through /export and the portability
archive, and nothing the app computes reads these fields. One list
(plaid.PLAID_RAW_IDENTIFIERS) is applied when a feed object is written,
when an export writes the row out, and once by a migration to the rows
stored before the rule.
"""

import csv
import io
import pathlib
import re
import unittest
import zipfile

from oikonome.engine.compat import jsonb
from oikonome.sync import base as sync_base
from oikonome.sync import export, plaid

from .test_plaid_products import _transport
from .util import TODAY, add_txn, make_db, write_config

MIGRATION = (pathlib.Path(plaid.__file__).parent.parent / "db" /
             "migrations" /
             "214_plaid_raw_keeps_no_account_or_person_identifiers.sql")

# invented values, nobody's
LOAN_NO = "LN0000555577"
STREET = "12 Example Lane"
OWNER = "Pat Example"

STUDENT = {"account_id": "stu-1", "account_number": LOAN_NO,
           "payment_reference_number": LOAN_NO,
           "next_payment_due_date": "2026-12-01",
           "minimum_payment_amount": 120.0}
MORTGAGE = {"account_id": "mort-1", "account_number": LOAN_NO,
            "property_address": {"street": STREET, "city": "Exampleton",
                                 "postal_code": "00000"},
            "next_payment_due_date": "2026-12-01"}


def _plant(conn):
    """Rows in the shape the Plaid pulls wrote before the rule."""
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,balance_current) "
        "VALUES ('stu-1','it-p','Student loan','loan',9000), "
        "('inv-1','it-p','Brokerage','investment',3000)")
    conn.execute(
        "INSERT INTO liabilities (account_id, as_of, raw) VALUES "
        "('stu-1', now(), %s), ('card', now(), %s)",
        (jsonb(STUDENT | {"account_number": None}),
         jsonb({"account_id": "card", "last_statement_balance": 50.0})))
    conn.execute("UPDATE accounts SET raw=%s WHERE id='chk'", (jsonb({
        "account_id": "chk", "mask": "0000",
        "owners": [{"names": [OWNER]}]}),))
    conn.execute(
        "INSERT INTO holdings (account_id, symbol, name, quantity, price, "
        "value, as_of, raw) VALUES ('inv-1','VTI','Fund',1,10,10,now(),%s)",
        (jsonb({"account_id": "inv-1", "account_owner": OWNER,
                "quantity": 1}),))
    add_txn(conn, TODAY, 10.0, "FEED ROW", txn_id="plaid-1")
    conn.execute("UPDATE transactions SET raw=%s WHERE id='plaid-1'",
                 (jsonb({"transaction_id": "plaid-1",
                         "account_owner": OWNER, "check_number": "1001",
                         "name": "FEED ROW"}),))


class PlaidRawIdentifierTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")

    def test_loans_land_without_their_number_or_the_home_address(self):
        n = plaid.sync_liabilities(
            self.conn, "it-p", transport=_transport(liabilities={
                "liabilities": {"student": [STUDENT],
                                "mortgage": [MORTGAGE]}}))
        self.assertEqual(n, 2)
        raws = {r["account_id"]: r["raw"] for r in self.conn.execute(
            "SELECT account_id, raw FROM liabilities")}
        self.assertEqual(raws["stu-1"], {
            "account_id": "stu-1", "next_payment_due_date": "2026-12-01",
            "minimum_payment_amount": 120.0})
        self.assertEqual(raws["mort-1"], {
            "account_id": "mort-1", "next_payment_due_date": "2026-12-01"})

    def test_feed_rows_land_without_owner_names(self):
        t = plaid._norm_txn({"transaction_id": "x", "account_id": "chk",
                             "date": TODAY.isoformat(), "amount": 5.0,
                             "name": "SHOP", "account_owner": OWNER,
                             "check_number": "1001"})
        self.assertNotIn("account_owner", t.raw)
        # a check number is a ledger fact the app reads, not an identity
        self.assertEqual(t.raw["check_number"], "1001")
        (a,) = plaid._norm_accounts({"accounts": [{
            "account_id": "chk", "name": "Checking", "mask": "0000",
            "owners": [{"names": [OWNER]}], "balances": {"current": 1}}]})
        self.assertNotIn("owners", a.raw)
        self.assertEqual(a.raw["mask"], "0000")

    def _stored(self):
        return " ".join(str(r["raw"]) for q in (
            "SELECT raw FROM liabilities", "SELECT raw FROM accounts",
            "SELECT raw FROM holdings", "SELECT raw FROM transactions")
            for r in self.conn.execute(q))

    def test_stored_rows_are_cut_once_and_stay_cut(self):
        _plant(self.conn)
        for _ in range(2):
            self.conn.execute(MIGRATION.read_text())
            text = self._stored()
            for v in (LOAN_NO, OWNER, "payment_reference_number",
                      "account_owner", "owners"):
                self.assertNotIn(v, text)
        # everything else is kept
        raw = self.conn.execute("SELECT raw FROM transactions "
                                "WHERE id='plaid-1'").fetchone()["raw"]
        self.assertEqual(raw, {"transaction_id": "plaid-1",
                               "check_number": "1001", "name": "FEED ROW"})
        self.assertEqual(self.conn.execute(
            "SELECT raw FROM liabilities WHERE account_id='stu-1'"
        ).fetchone()["raw"], {"account_id": "stu-1",
                              "next_payment_due_date": "2026-12-01",
                              "minimum_payment_amount": 120.0})

    def test_the_export_carries_none_of_them(self):
        _plant(self.conn)
        with zipfile.ZipFile(io.BytesIO(export.build_zip(self.conn))) as z:
            for name in ("liabilities.csv", "accounts.csv", "holdings.csv",
                         "transactions.csv"):
                body = z.read(name).decode()
                for v in (LOAN_NO, OWNER, "payment_reference_number"):
                    self.assertNotIn(v, body, name)
            rows = list(csv.DictReader(io.StringIO(
                z.read("transactions.csv").decode())))
        self.assertIn('"check_number": "1001"',
                      next(r for r in rows if r["id"] == "plaid-1")["raw"])

    def test_the_migration_cuts_the_one_list(self):
        arr = re.findall(r"ARRAY\[(.*?)\]", MIGRATION.read_text(), re.S)
        self.assertTrue(arr)
        for a in arr:
            self.assertEqual(set(re.findall(r"'([^']*)'", a)),
                             set(plaid.PLAID_RAW_IDENTIFIERS))


if __name__ == "__main__":
    unittest.main()
