"""Restore holds every money cell to a real sum, and keeps every tax
document an archive carries.

A value like 1.7e308 is finite, so it passes the plain number check, but two
of them overflow every total they meet: net worth, the bills' planned
totals, the holdings value. The live doors refuse them, so only an archive
can bring one in, and then every page that adds them up fails. Such a row
is left out and counted, as a transaction amount is; an account balance is
left empty instead, because the account itself has to land.

Tax documents are numbered 1, 2, 3 within each household, so an archive's
ids collide with the documents a populated household already has. Keyed on
the id, the archive's different W-2s were dropped and reported as already
present.
"""
import csv
import io
import json
import unittest
import zipfile

from oikonome.sync import restore

from .util import make_db, write_config


def _member(rows: list[dict]) -> str:
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=list(rows[0]))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return s.getvalue()


def _zip(**members) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in members.items():
            z.writestr(f"{name}.csv", _member(rows))
    return buf.getvalue()


HUGE = "1.7e308"


class EveryMoneyCellIsBounded(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _restore(self):
        return restore.restore_zip(self.conn, _zip(
            items=[{"id": "rb-item", "aggregator": "manual",
                    "institution_name": "Harbor Bank", "status": "ok"}],
            accounts=[{"id": "rb-acct", "item_id": "rb-item",
                       "name": "Everyday", "type": "depository",
                       "balance_current": HUGE, "balance_available": "10",
                       "balance_limit": ""}],
            bills=[{"id": "rb-bill", "type": "expense", "payee": "Water",
                    "amount": HUGE, "frequency": "monthly",
                    "monthly_amount": HUGE, "active": "1"},
                   {"id": "rb-bill-2", "type": "expense", "payee": "Power",
                    "amount": "80", "frequency": "monthly",
                    "monthly_amount": "80", "active": "1"}],
            holdings=[{"account_id": "rb-acct", "symbol": "IDX",
                       "quantity": "1", "price": HUGE, "value": HUGE}],
            networth_snapshot=[{"date": "2026-01-31", "total": HUGE,
                                "by_class": ""}]))

    def test_an_impossible_balance_lands_empty_with_its_account(self):
        self._restore()
        row = self.conn.execute(
            "SELECT balance_current, balance_available FROM accounts "
            "WHERE id='rb-acct'").fetchone()
        self.assertIsNone(row["balance_current"])
        self.assertEqual(row["balance_available"], 10)

    def test_rows_with_impossible_amounts_are_left_out_and_counted(self):
        counts = self._restore()
        self.assertEqual(counts.get("out_of_range"), 3)
        self.assertEqual([r["id"] for r in self.conn.execute(
            "SELECT id FROM bills WHERE id LIKE 'rb-%' ORDER BY id")],
            ["rb-bill-2"])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM holdings WHERE account_id='rb-acct'").fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM networth_snapshot WHERE date='2026-01-31'"
        ).fetchone())


def _w2(doc_id, payer, wages):
    return {"id": str(doc_id), "year": "2025", "form": "W-2",
            "owner": "primary", "payer": payer, "ein_last4": "",
            "primary_amount": str(wages),
            "amounts": json.dumps({"box1": wages}), "notes": ""}


class EveryTaxDocumentLands(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        for doc_id, payer, wages in ((1, "Lantern Works", 51000),
                                     (2, "Tidewater Partners", 12000)):
            self.conn.execute(
                "INSERT INTO income_documents (id, year, form, owner, payer, "
                "primary_amount, amounts) VALUES (%s, 2025, 'W-2', "
                "'primary', %s, %s, %s)",
                (doc_id, payer, wages, json.dumps({"box1": wages})))

    def _docs(self):
        return sorted((r["payer"], r["primary_amount"]) for r in
                      self.conn.execute(
                          "SELECT payer, primary_amount FROM income_documents"))

    def test_an_archives_documents_land_beside_the_households_own(self):
        restore.restore_zip(self.conn, _zip(income_documents=[
            _w2(1, "Orchard Co", 40000), _w2(2, "Brightwater Inc", 9000),
            _w2(3, "Juniper LLC", 3000)]))
        self.assertEqual(self._docs(), sorted([
            ("Lantern Works", 51000), ("Tidewater Partners", 12000),
            ("Orchard Co", 40000), ("Brightwater Inc", 9000),
            ("Juniper LLC", 3000)]))

    def test_a_document_already_held_is_not_added_twice(self):
        archive = _zip(income_documents=[_w2(7, "Lantern Works", 51000),
                                         _w2(1, "Orchard Co", 40000)])
        restore.restore_zip(self.conn, archive)
        restore.restore_zip(self.conn, archive)
        self.assertEqual(self._docs(), sorted([
            ("Lantern Works", 51000), ("Tidewater Partners", 12000),
            ("Orchard Co", 40000)]))


if __name__ == "__main__":
    unittest.main()
