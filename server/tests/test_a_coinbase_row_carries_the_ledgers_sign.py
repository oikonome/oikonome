"""A Coinbase row reads in the ledger's sign: positive is money out.

Coinbase signs a transaction by the WALLET: + is value arriving in it. The
ledger signs by the household: + is money leaving. Stored verbatim, a
withdrawal leaving Coinbase lists as money arriving, a deposit as money
leaving, and a sale's legs read as the same sum arriving twice and leaving
once. Every door now takes the amount from the row's own record (its
native_amount, negated) and the transfer label from that sign: the push
door, the native pull, migration 229 over rows already stored, and a
restore of an archive written before it. A person's override is theirs.

Every amount and id is invented.
"""

import datetime as dt
import io
import json
import pathlib
import unittest
import zipfile

from oikonome.sync import coinbase_push, restore

from .util import make_db, write_config

MIGRATION = (pathlib.Path(coinbase_push.__file__).parent.parent / "db" /
             "migrations" / "229_a_coinbase_row_carries_the_ledgers_sign.sql")

TODAY = dt.date.today().isoformat()


def _record(kind: str, native: str) -> dict:
    return {"id": f"rec-{kind}", "type": kind, "status": "completed",
            "native_amount": {"amount": native, "currency": "USD"}}


def _payload(rows: list[dict]) -> dict:
    return {
        "items": [{"id": "coinbase-test", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                      "name": "Coinbase (test)", "type": "investment",
                      "subtype": "crypto", "balance_current": 10.0}],
        "transactions": rows, "full_replace": True}


class ACoinbaseRowCarriesTheLedgersSign(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _rows(self) -> dict:
        return {r["id"]: r for r in self.conn.execute(
            "SELECT id, amount, category_primary, category_override "
            "FROM transactions WHERE id LIKE 'coinbase:%'").fetchall()}

    def test_a_pushed_withdrawal_is_money_out_whatever_sign_it_was_sent(self):
        # the collector sends the wallet's sign; the record decides
        coinbase_push.import_payload(self.conn, _payload([
            {"id": "coinbase:w", "account_id": "coinbase-test",
             "date": TODAY, "amount": -517.3, "name": "cash out",
             "category_primary": "TRANSFER_IN",
             "raw": _record("fiat_withdrawal", "-517.30")},
            {"id": "coinbase:d", "account_id": "coinbase-test",
             "date": TODAY, "amount": 64.0, "name": "cash in",
             "category_primary": "TRANSFER_OUT",
             "raw": _record("fiat_deposit", "64.00")}]))
        rows = self._rows()
        self.assertEqual(rows["coinbase:w"]["amount"], 517.3)
        self.assertEqual(rows["coinbase:w"]["category_primary"], "TRANSFER_OUT")
        self.assertEqual(rows["coinbase:d"]["amount"], -64.0)
        self.assertEqual(rows["coinbase:d"]["category_primary"], "TRANSFER_IN")

    def test_a_sales_two_legs_net_to_nothing_and_the_withdrawal_leaves(self):
        coinbase_push.import_payload(self.conn, _payload([
            {"id": f"coinbase:{k}", "account_id": "coinbase-test",
             "date": TODAY, "amount": float(n), "name": k,
             "raw": _record(k, n)}
            for k, n in (("sell-coin", "-300.00"), ("sell-cash", "300.00"),
                         ("fiat_withdrawal", "-300.00"))]))
        amounts = [r["amount"] for r in self._rows().values()]
        # one sum left the account, once
        self.assertEqual(round(sum(amounts), 2), 300.0)

    def test_a_row_with_no_record_keeps_the_amount_it_was_sent(self):
        coinbase_push.import_payload(self.conn, _payload([
            {"id": "coinbase:bare", "account_id": "coinbase-test",
             "date": TODAY, "amount": 19.0, "name": "no record"}]))
        self.assertEqual(self._rows()["coinbase:bare"]["amount"], 19.0)

    def _stored_wallet_signed(self, override=None):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, raw) "
            "VALUES ('coinbase-old','coinbase','Coinbase','{}'::jsonb)")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype) VALUES "
            "('coinbase-old','coinbase-old','Coinbase','investment','crypto')")
        for tid, native, label in (("coinbase:o1", "-1500.00", "TRANSFER_IN"),
                                   ("coinbase:o2", "40.25", "TRANSFER_OUT")):
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, name,"
                " category_primary, category_override, removed, raw) VALUES "
                "(%s,'coinbase-old',%s,%s,'a row',%s,%s,0,%s::jsonb)",
                (tid, TODAY, float(native), label, override,
                 json.dumps(_record("fiat_withdrawal", native))))

    def test_the_migration_turns_stored_rows_to_the_ledgers_sign_once(self):
        self._stored_wallet_signed()
        first = self.conn.execute(MIGRATION.read_text()).rowcount
        rows = self._rows()
        self.assertEqual(first, 2)
        self.assertEqual(rows["coinbase:o1"]["amount"], 1500.0)
        self.assertEqual(rows["coinbase:o1"]["category_primary"], "TRANSFER_OUT")
        self.assertEqual(rows["coinbase:o2"]["amount"], -40.25)
        self.assertEqual(rows["coinbase:o2"]["category_primary"], "TRANSFER_IN")
        self.assertEqual(self.conn.execute(MIGRATION.read_text()).rowcount, 0)

    def test_an_amount_past_the_cap_is_left_and_the_migration_completes(self):
        self._stored_wallet_signed()
        for tid, native in (("coinbase:huge", "9" * 400),
                            ("coinbase:wide", "1" * 20)):
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, name,"
                " category_primary, removed, raw) VALUES "
                "(%s,'coinbase-old',%s,7.5,'a row','TRANSFER_IN',0,%s::jsonb)",
                (tid, TODAY, json.dumps(_record("send", native))))
            self.assertIsNone(coinbase_push.ledger_amount(
                _record("send", native)))
        self.assertEqual(self.conn.execute(MIGRATION.read_text()).rowcount, 2)
        rows = self._rows()
        for tid in ("coinbase:huge", "coinbase:wide"):
            self.assertEqual(rows[tid]["amount"], 7.5, tid)
            self.assertEqual(rows[tid]["category_primary"], "TRANSFER_IN")

    def test_the_migration_leaves_a_persons_override_alone(self):
        self._stored_wallet_signed(override="Crypto savings")
        self.conn.execute(MIGRATION.read_text())
        for r in self._rows().values():
            self.assertEqual(r["category_override"], "Crypto savings")

    def test_an_archive_with_the_wallets_sign_restores_in_the_ledgers(self):
        header = ["id", "account_id", "date", "amount", "name",
                  "merchant_name", "category_primary", "category_detailed",
                  "category_override", "pending", "removed", "raw"]
        rec = json.dumps(_record("fiat_withdrawal", "-700.00"))
        line = ",".join(['"coinbase:a1"', '"chk"', f'"{TODAY}"', '"-700"',
                         '"a cash out"', '"Coinbase"',
                         '"TRANSFER_IN"', '', '', '0', '0',
                         '"' + rec.replace('"', '""') + '"'])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("transactions.csv", ",".join(header) + "\n" + line + "\n")
        restore.restore_zip(self.conn, buf.getvalue())
        row = self._rows()["coinbase:a1"]
        self.assertEqual(row["amount"], 700.0)
        self.assertEqual(row["category_primary"], "TRANSFER_OUT")


if __name__ == "__main__":
    unittest.main()
