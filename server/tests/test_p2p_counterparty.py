"""P2P counterparties become their own merchants.

Aggregators normalize Venmo rows to name "Venmo" with no merchant, and
Zelle imports store the whole statement line ("Zelle payment from A to B")
as the name with a truncated copy in merchant_name. The real counterparty
survives in the statement text; ingest must recover it and store it as
merchant_name — so each person groups, searches, and totals like any
other payee — and the stored-history migrations must agree with the
ingest parsers. Ledger search must also reach the raw statement line.
"""

import datetime as dt
import unittest
from pathlib import Path

from oikonome.sync.base import (Transaction, upsert_transactions,
                                venmo_counterparty, zelle_counterparty)

from .util import add_txn, jsonb, make_db

_MIGRATIONS = Path(__file__).resolve().parents[1] / "oikonome/db/migrations"
VENMO_MIGRATION = _MIGRATIONS / "081_venmo_counterparty_merchant.sql"
ZELLE_MIGRATION = _MIGRATIONS / "082_zelle_counterparty_merchant.sql"
RAIL_MIGRATION = _MIGRATIONS / "083_venmo_merchant_keeps_rail.sql"


class VenmoCounterpartyParser(unittest.TestCase):
    def test_all_caps_name_with_address_tail(self):
        self.assertEqual(
            venmo_counterparty(
                "VENMO *CASEY EXAMPLE 1234 EXAMPLE PKWY 5555550100, NY, US"),
            "Venmo — Casey Example")

    def test_mixed_case_name_with_city_tail(self):
        self.assertEqual(
            venmo_counterparty("VENMO *Taylor Sample NEW YORK, NY, US"),
            "Venmo — Taylor Sample")

    def test_bare_descriptors_name_nobody(self):
        self.assertIsNone(venmo_counterparty("VENMO PAYMENT"))
        self.assertIsNone(venmo_counterparty("VENMO PURCHASE"))
        self.assertIsNone(venmo_counterparty("VENMO CASHOUT"))
        self.assertIsNone(venmo_counterparty(None))
        self.assertIsNone(venmo_counterparty("SAFEWAY STORE 123"))


class VenmoMerchantPromotion(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _sync_venmo(self, txn_id, orig, *, merchant=None):
        upsert_transactions(self.conn, [Transaction(
            id=txn_id, account_id="chk", date=dt.date(2025, 7, 10),
            amount=60.0, name="Venmo", merchant_name=merchant,
            raw={"original_description": orig})])
        return self.conn.execute(
            "SELECT merchant_name FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["merchant_name"]

    def test_ingest_promotes_counterparty_to_merchant(self):
        self.assertEqual(
            self._sync_venmo(
                "v1",
                "VENMO *TAYLOR SAMPLE 1234 EXAMPLE PKWY 5555550100, NY, US"),
            "Venmo — Taylor Sample")

    def test_aggregator_supplied_merchant_wins(self):
        self.assertEqual(
            self._sync_venmo("v2", "VENMO *TAYLOR SAMPLE 7700, NY, US",
                             merchant="Venmo"),
            "Venmo")

    def test_bare_descriptor_stays_merchantless(self):
        self.assertIsNone(self._sync_venmo("v3", "VENMO PAYMENT"))

    def test_ledger_payee_is_the_person(self):
        from oikonome.web import data
        self._sync_venmo(
            "v4", "VENMO *CASEY EXAMPLE 1234 EXAMPLE PKWY 5555550100, NY, US")
        rows = {r["id"]: r for r in data.transactions(self.conn, 2025, 7)}
        self.assertEqual(rows["v4"]["payee"], "Venmo — Casey Example")

    def test_search_reaches_the_statement_line(self):
        from oikonome.web import data
        t = add_txn(self.conn, "2025-07-11", 45.0, "Venmo")
        self.conn.execute(
            "UPDATE transactions SET merchant_name=NULL, raw=%s WHERE id=%s",
            (jsonb({"original_description": "VENMO CASHOUT"}), t))
        add_txn(self.conn, "2025-07-12", 12.0, "SAFEWAY STORE")
        rows, total, _, _, _, _hits = data.search_transactions(self.conn, "cashout")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], t)
        month = data.transactions(self.conn, 2025, 7, search="cashout")
        self.assertEqual([r["id"] for r in month], [t])

    def test_backfill_migration_agrees_with_ingest_parser(self):
        cases = [
            "VENMO *TAYLOR SAMPLE 1234 EXAMPLE PKWY 5555550100, NY, US",
            "VENMO *Taylor Sample NEW YORK, NY, US",
            "VENMO *CASEY EXAMPLE 1234 EXAMPLE PKWY 5555550100, NY, US",
            "VENMO PAYMENT",
            "VENMO CASHOUT",
        ]
        ids = []
        for i, orig in enumerate(cases):
            t = add_txn(self.conn, "2025-07-10", 10.0 + i, "Venmo")
            self.conn.execute(
                "UPDATE transactions SET merchant_name=NULL, raw=%s WHERE id=%s",
                (jsonb({"original_description": orig}), t))
            ids.append(t)
        self.conn.execute(VENMO_MIGRATION.read_text())
        self.conn.execute(RAIL_MIGRATION.read_text())
        for orig, t in zip(cases, ids):
            got = self.conn.execute(
                "SELECT merchant_name FROM transactions WHERE id=%s",
                (t,)).fetchone()["merchant_name"]
            self.assertEqual(got, venmo_counterparty(orig), orig)


_OUT = ("ACH DEBIT — Zelle payment from Sam Owner (Acme Checking "
        "XXXXXX1234) to CASEY EXAMPLE")
_IN = ("ACH CREDIT — Zelle payment from TAYLOR SAMPLE to Sam Owner "
       "(Acme Savings XXXXXX5678)")


class ZelleCounterpartyParser(unittest.TestCase):
    def test_money_out_names_the_to_side(self):
        self.assertEqual(zelle_counterparty(_OUT, money_in=False),
                         "Zelle — Casey Example")

    def test_money_in_names_the_from_side(self):
        self.assertEqual(zelle_counterparty(_IN, money_in=True),
                         "Zelle — Taylor Sample")

    def test_ach_receiver_tag_stripped(self):
        self.assertEqual(
            zelle_counterparty(
                "ACH Withdrawal — Zelle payment from SAM OWNER (Acme Checking "
                "1234) to CASEY EXAMPLE RECEIVER", money_in=False),
            "Zelle — Casey Example")

    def test_llc_suffix_survives_title_casing(self):
        self.assertEqual(
            zelle_counterparty(
                "Zelle payment from ACME HOLDINGS, LLC to Sam Owner "
                "(Acme Checking XXXXXX1234)", money_in=True),
            "Zelle — Acme Holdings, LLC")

    def test_counterparty_name_containing_to_survives_the_split(self):
        """A counterparty can be named "Farm to Table LLC" — splitting at
        the first " to " would turn that payment into merchant "Zelle — Farm".
        The own side's "(Acme Checking …)" parenthetical anchors the true
        boundary when present; without one, the split keeps the side being
        returned whole."""
        # money out: the OWN (from) name carries " to "; a first-split
        # would hand back "Table LLC (…" as the recipient
        self.assertEqual(
            zelle_counterparty(
                "Zelle payment from Farm to Table LLC (Acme Checking "
                "XXXXXX1234) to Jane Doe", money_in=False),
            "Zelle — Jane Doe")
        # money out: the recipient's own name carrying " to " stays whole
        self.assertEqual(
            zelle_counterparty(
                "Zelle payment from Sam Owner (Acme Checking "
                "XXXXXX1234) to Farm to Table LLC", money_in=False),
            "Zelle — Farm to Table LLC")
        # money in: the counterparty (from side) itself contains " to "
        self.assertEqual(
            zelle_counterparty(
                "Zelle payment from Farm to Table LLC to Sam Owner "
                "(Acme Checking XXXXXX1234)", money_in=True),
            "Zelle — Farm to Table LLC")

    def test_truncated_line_names_nobody(self):
        self.assertIsNone(zelle_counterparty(
            "ACH DEBIT — Zelle payment from Sam Owner (Acme",
            money_in=False))
        self.assertIsNone(zelle_counterparty(
            "Transfer Return — Zelle Debit Return", money_in=True))
        self.assertIsNone(zelle_counterparty(None, money_in=False))


class ZelleMerchantPromotion(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _sync_zelle(self, txn_id, name, amount, *, merchant=None):
        upsert_transactions(self.conn, [Transaction(
            id=txn_id, account_id="chk", date=dt.date(2025, 7, 10),
            amount=amount, name=name, merchant_name=merchant)])
        return self.conn.execute(
            "SELECT merchant_name FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["merchant_name"]

    def test_ingest_promotes_from_the_name_field(self):
        self.assertEqual(self._sync_zelle("z1", _OUT, 60.0),
                         "Zelle — Casey Example")

    def test_truncated_descriptor_merchant_is_replaced(self):
        # imports truncate the statement line into merchant_name — that
        # truncation is noise, not an aggregator-resolved merchant
        self.assertEqual(
            self._sync_zelle("z2", _OUT, 60.0,
                             merchant="Zelle payment from Sam Owner (Acme"),
            "Zelle — Casey Example")

    def test_real_merchant_wins(self):
        self.assertEqual(
            self._sync_zelle("z3", _OUT, 60.0, merchant="Some Store"),
            "Some Store")

    def test_backfill_migration_agrees_with_ingest_parser(self):
        cases = [(_OUT, 60.0), (_IN, -45.0),
                 ("ACH DEBIT — Zelle payment from Sam Owner (Acme", 10.0),
                 ("Transfer Return — Zelle Debit Return", -10.0)]
        ids = []
        for i, (name, amount) in enumerate(cases):
            t = add_txn(self.conn, "2025-07-10", amount, name)
            self.conn.execute(
                "UPDATE transactions SET merchant_name=%s WHERE id=%s",
                (f"{name[:40]}", t))
            ids.append(t)
        self.conn.execute(ZELLE_MIGRATION.read_text())
        for (name, amount), t in zip(cases, ids):
            got = self.conn.execute(
                "SELECT merchant_name FROM transactions WHERE id=%s",
                (t,)).fetchone()["merchant_name"]
            expect = (zelle_counterparty(name, amount < 0)
                      or name[:40])   # unparseable rows keep their merchant
            self.assertEqual(got, expect, name)


if __name__ == "__main__":
    unittest.main()
