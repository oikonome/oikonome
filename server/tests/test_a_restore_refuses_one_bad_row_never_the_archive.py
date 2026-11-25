"""A restore judges each archived row on its own: a row no door of the app
would write is refused and counted, and every other row lands.

Four ways a single row used to cost the household the whole archive, or
slip in where the live doors refuse it:

- a compliance obligation carrying a link that is not http(s), a
  recurrence outside the set, or a fee past its column — the Business
  page's door refuses all three, and the restore wrote them straight in
  (an oversized fee overflowed the column and rolled the restore back);
- a value past a bounded column (a tax rate, an ownership share, a
  mileage figure, a business movement of exactly the largest sum) —
  finite, so the plain number check passed it, and the column overflow
  then rolled back every other row;
- a liability, holding or crypto balance for an account the archive did
  not bring — landed with nothing to hang on;
- an unreadable date: a required one refuses its row, an optional one
  keeps the row without the date.
"""
import csv
import io
import unittest
import zipfile

from oikonome.sync import restore

from .util import make_db, write_config


def _member(rows: list[dict]) -> str:
    fields: list[str] = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=fields)
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


ENT = "0b5e0000-0000-4000-8000-000000000001"
ENT2 = "0b5e0000-0000-4000-8000-000000000002"


def _uid(n: int) -> str:
    return f"0b5e0000-0000-4000-8000-{n:012d}"


def _entity(eid=ENT, **over):
    return {"id": eid, "name": "Quill Studio", "structure": "llc",
            "state": "", "formation_date": "", "status": "active",
            "income_tax_rate": "22", **over}


class _Case(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _restore(self, **members):
        counts, out = restore.restore_outcomes(self.conn, _zip(**members))
        return counts, out

    def _ids(self, table, col="id"):
        return sorted(str(r[col]) for r in self.conn.execute(
            f"SELECT {col} FROM {table}"))


class ObligationsPassTheDoorsChecks(_Case):

    def test_a_bad_obligation_is_refused_and_the_good_one_lands(self):
        def ob(n, **over):
            return {"id": _uid(n), "entity_id": ENT, "title": "Annual filing",
                    "due_date": "2026-06-30", "recurrence": "yearly",
                    "fee": "10", "url": "https://example.test/file",
                    "note": "", **over}
        _, out = self._restore(
            business_entity=[_entity()],
            compliance_obligation=[
                ob(101),
                ob(102, url="javascript:alert(1)"),
                ob(103, recurrence="weekly"),
                ob(104, fee="not a fee"),
                ob(105, fee="1000000000"),
                ob(106, due_date="2026-02-31")])
        # the link is optional: one without http(s) is left out and its
        # obligation lands
        self.assertEqual(self._ids("compliance_obligation"),
                         [_uid(101), _uid(102)])
        self.assertIsNone(self.conn.execute(
            "SELECT url FROM compliance_obligation WHERE id=%s",
            (_uid(102),)).fetchone()["url"])
        self.assertEqual(out.refused.get(("compliance_obligation",
                                          "malformed")), 3)
        self.assertEqual(out.refused.get(("compliance_obligation",
                                          "out_of_range")), 1)


class BoundedColumnsRefuseTheirRow(_Case):

    def _restore_bounded(self):
        return self._restore(
            business_entity=[_entity(),
                             _entity(ENT2, income_tax_rate="1000")],
            entity_membership=[
                {"id": _uid(201), "entity_id": ENT, "member_name": "A",
                 "ownership_pct": "60", "is_manager": "1"},
                {"id": _uid(202), "entity_id": ENT2, "member_name": "B",
                 "ownership_pct": "1000", "is_manager": "0"}],
            mileage_log=[
                {"id": _uid(301), "entity_id": ENT2, "date": "2026-03-01",
                 "miles": "12.5", "purpose": "client"},
                {"id": _uid(302), "entity_id": ENT, "date": "2026-03-02",
                 "miles": "1e10", "purpose": "client"}],
            equity_movement=[
                {"id": _uid(401), "entity_id": ENT2, "kind": "contribution",
                 "amount": "500", "date": "2026-01-05"},
                {"id": _uid(402), "entity_id": ENT, "kind": "contribution",
                 "amount": "1000000000000", "date": "2026-01-06"}],
            transactions=[{"id": "bn-1", "account_id": "chk",
                           "date": "2026-01-07", "amount": "1e13",
                           "name": "Corner Cafe"}])

    def test_a_required_value_past_its_column_refuses_that_row_only(self):
        _, out = self._restore_bounded()
        self.assertEqual(self._ids("mileage_log"), [_uid(301)])
        self.assertEqual(self._ids("equity_movement"), [_uid(401)])
        for table in ("mileage_log", "equity_movement", "transactions"):
            self.assertEqual(out.refused.get((table, "out_of_range")), 1,
                             table)

    def test_an_optional_value_past_its_column_is_left_empty(self):
        """The business and the member land without the figure, so what
        hangs on them lands too."""
        _, out = self._restore_bounded()
        self.assertEqual(self._ids("business_entity"), [ENT, ENT2])
        self.assertIsNone(self.conn.execute(
            "SELECT income_tax_rate FROM business_entity WHERE id=%s",
            (ENT2,)).fetchone()["income_tax_rate"])
        self.assertEqual(self._ids("entity_membership"),
                         [_uid(201), _uid(202)])
        self.assertIsNone(self.conn.execute(
            "SELECT ownership_pct FROM entity_membership WHERE id=%s",
            (_uid(202),)).fetchone()["ownership_pct"])
        self.assertIsNone(out.refused.get(("business_entity",
                                           "out_of_range")))
        self.assertIsNone(out.refused.get(("entity_membership",
                                           "out_of_range")))

    def test_the_summary_names_each_columns_own_bound(self):
        counts, _ = self._restore_bounded()
        notes = " | ".join(counts.get("_notes") or [])
        for said in ("miles over 999,999,999.9",
                     "amount over 999,999,999,999.99",
                     "over 1,000,000,000,000",
                     "a tax rate the business page refuses",
                     "an ownership share outside 0-100"):
            self.assertIn(said, notes)


    def test_a_value_the_column_rounds_past_its_bound_is_refused(self):
        """999,999,999,999.995 rounds down in Python's float rounding;
        the column reads it at 15 digits and rounds it up, past its
        bound, which rolled back the whole restore."""
        _, out = self._restore(
            business_entity=[_entity()],
            equity_movement=[
                {"id": _uid(403), "entity_id": ENT, "kind": "contribution",
                 "amount": "999999999999.995", "date": "2026-01-06"},
                {"id": _uid(404), "entity_id": ENT, "kind": "contribution",
                 "amount": "999999999999.99", "date": "2026-01-07"}])
        self.assertEqual(self._ids("equity_movement"), [_uid(404)])
        self.assertEqual(out.refused.get(("equity_movement",
                                          "out_of_range")), 1)


class AFeeIsJudgedAsTheColumnRoundsIt(_Case):

    def test_a_fee_that_rounds_past_the_column_is_refused(self):
        from oikonome.engine import compliance
        with self.assertRaises(compliance.FeeTooLarge):
            compliance.check_obligation(title="Filing", recurrence="yearly",
                                        fee="99999999.995", url=None)
        self.assertEqual(compliance.check_obligation(
            title="Filing", recurrence="yearly", fee="99999999.99",
            url=None)[2], 99999999.99)
        _, out = self._restore(
            business_entity=[_entity()],
            compliance_obligation=[
                {"id": _uid(801), "entity_id": ENT, "title": "Filing",
                 "due_date": "2026-06-30", "fee": "99999999.995"},
                {"id": _uid(802), "entity_id": ENT, "title": "Filing",
                 "due_date": "2026-06-30", "fee": "99999999.99"}])
        self.assertEqual(self._ids("compliance_obligation"), [_uid(802)])
        self.assertEqual(out.refused.get(("compliance_obligation",
                                          "out_of_range")), 1)


class BalancesNeedTheirAccount(_Case):

    def test_balances_for_an_account_not_here_are_orphaned(self):
        _, out = self._restore(
            liabilities=[{"account_id": "card", "as_of": "", "raw": "{}"},
                         {"account_id": "ghost", "as_of": "", "raw": "{}"}],
            holdings=[{"account_id": "chk", "symbol": "IDX", "quantity": "1",
                       "price": "10", "value": "10"},
                      {"account_id": "ghost", "symbol": "IDX",
                       "quantity": "1", "price": "10", "value": "10"}],
            crypto_holdings=[{"account_id": "chk", "currency": "BTC",
                              "quantity": "1", "native_usd": "10"},
                             {"account_id": "ghost", "currency": "BTC",
                              "quantity": "1", "native_usd": "10"}])
        for table in ("liabilities", "holdings", "crypto_holdings"):
            self.assertEqual(self._ids(table, "account_id"),
                             ["card" if table == "liabilities" else "chk"],
                             table)
            self.assertEqual(out.refused.get((table, "orphaned")), 1, table)


class AnUnreadableDateCostsOnlyItsRow(_Case):

    BAD = "2026-13-45"

    def test_a_required_date_refuses_its_row_and_the_rest_lands(self):
        bad = self.BAD

        def txn(i, date, **over):
            return {"id": f"rd-{i}", "account_id": "chk", "date": date,
                    "amount": "12", "name": "Corner Cafe", **over}
        _, out = self._restore(
            business_entity=[_entity()],
            transactions=[txn(1, "2026-04-01"), txn(2, bad)],
            amazon_orders=[
                {"dedup_key": "az-1", "date": "2026-04-01", "amount": "5"},
                {"dedup_key": "az-2", "date": bad, "amount": "5"}],
            costco_receipts=[
                {"dedup_key": "cr-1", "date": "2026-04-01", "amount": "5"},
                {"dedup_key": "cr-2", "date": bad, "amount": "5"}],
            equity_movement=[
                {"id": _uid(501), "entity_id": ENT, "kind": "draw",
                 "amount": "5", "date": "2026-04-01"},
                {"id": _uid(502), "entity_id": ENT, "kind": "draw",
                 "amount": "5", "date": bad}],
            mileage_log=[
                {"id": _uid(601), "entity_id": ENT, "date": "2026-04-01",
                 "miles": "3"},
                {"id": _uid(602), "entity_id": ENT, "date": bad,
                 "miles": "3"}],
            compliance_obligation=[
                {"id": _uid(701), "entity_id": ENT, "title": "Filing",
                 "due_date": "2026-06-30"},
                {"id": _uid(702), "entity_id": ENT, "title": "Filing",
                 "due_date": bad}],
            networth_snapshot=[{"date": "2026-04-30", "total": "100"},
                               {"date": bad, "total": "100"}])
        self.assertEqual(self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'rd-%'").fetchall(),
            [{"id": "rd-1"}])
        self.assertEqual(self._ids("amazon_orders", "dedup_key"), ["az-1"])
        self.assertEqual(self._ids("costco_receipts", "dedup_key"), ["cr-1"])
        self.assertEqual(self._ids("equity_movement"), [_uid(501)])
        self.assertEqual(self._ids("mileage_log"), [_uid(601)])
        self.assertEqual(self._ids("compliance_obligation"), [_uid(701)])
        self.assertEqual(self._ids("networth_snapshot", "date"),
                         ["2026-04-30"])
        for table in ("transactions", "amazon_orders", "costco_receipts",
                      "equity_movement", "mileage_log",
                      "compliance_obligation", "networth_snapshot"):
            self.assertEqual(out.refused.get((table, "malformed")), 1, table)

    def test_an_optional_date_is_dropped_and_its_row_kept(self):
        self._restore(
            business_entity=[_entity(formation_date=self.BAD,
                                     business_start_date=self.BAD,
                                     archived_at=self.BAD)],
            transactions=[{"id": "od-1", "account_id": "chk",
                           "date": "2026-04-01", "amount": "12",
                           "name": "Corner Cafe",
                           "authorized_date": self.BAD,
                           "retired_at": self.BAD}])
        ent = self.conn.execute(
            "SELECT formation_date, business_start_date FROM business_entity"
            " WHERE id = %s", (ENT,)).fetchone()
        self.assertEqual(dict(ent), {"formation_date": None,
                                     "business_start_date": None})
        row = self.conn.execute(
            "SELECT authorized_date, retired_at FROM transactions"
            " WHERE id='od-1'").fetchone()
        self.assertEqual(dict(row), {"authorized_date": None,
                                     "retired_at": None})


if __name__ == "__main__":
    unittest.main()
