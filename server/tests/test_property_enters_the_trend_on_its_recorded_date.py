"""Net Worth's trend counts a property item from the month it was
recorded, never back-filled to the start of the ledger.

The trend held today's property value flat back through every month the
ledger reaches, so a house recorded this year read as owned for decades
and "All" opened on a total it never had. Each item
enters on its recorded date; earlier months show the financial accounts
alone. A linked loan leaves those months with the property it stands
beside (a mortgage with its house)."""

import datetime as dt
import pathlib
import unittest
from unittest import mock

from oikonome.db import migrate as migrations
from oikonome.engine import budget, reporting
from oikonome.sync import restore

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)
MONTHS = ["2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2026-11"]


class PropertyEntersOnItsDate(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, manual_assets=[
            {"name": "House", "kind": "real_estate", "value": 300000,
             "as_of": "2025-06-01"},
            {"name": "Car", "kind": "vehicle", "value": 12000}])
        for m in MONTHS[:-1]:
            self.conn.execute("INSERT INTO networth_recorded (month, total) "
                              "VALUES (%s, 4750)", (m,))

    def tearDown(self):
        self.conn.close()

    def _nw(self):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[[m, 4750.0] for m in MONTHS]):
            return reporting.compute_networth(self.conn, today=TODAY)

    def test_the_house_enters_on_its_recorded_month(self):
        full = dict(self._nw()["full_trend"])
        # before June 2025 the financial accounts alone (the undated car
        # enters on the month in progress)
        for m in ("2025-03", "2025-04", "2025-05"):
            self.assertEqual(full[m], 4750.0, m)
        for m in ("2025-06", "2025-07"):
            self.assertEqual(full[m], 4750.0 + 300000.0, m)
        self.assertEqual(full["2026-11"], 4750.0 + 312000.0)

    def test_a_mortgage_enters_with_its_house(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('mtg','it1','Home Loan','loan','mortgage',200000)")
        add_txn(self.conn, "2025-07-05", -800.0, "PRINCIPAL PAYMENT",
                account="mtg")
        full = dict(self._nw()["full_trend"])
        # before the house: the financial accounts alone, no debt either
        self.assertEqual(full["2025-05"], 4750.0)
        self.assertNotEqual(full["2025-06"], 4750.0)

    def test_a_revalued_house_keeps_the_month_it_was_acquired(self):
        # a value edit restamps as_of to the day of the edit; the item
        # still enters on the month it was acquired
        write_config(self.conn, manual_assets=[
            {"name": "House", "kind": "real_estate", "value": 315000,
             "as_of": "2026-11-10", "acquired_on": "2025-06-01"}])
        full = dict(self._nw()["full_trend"])
        self.assertEqual(full["2025-05"], 4750.0)
        for m in ("2025-06", "2025-07"):
            self.assertEqual(full[m], 4750.0 + 315000.0, m)


class AnItemStoredBeforeTheField(unittest.TestCase):
    """A row stored before acquired_on existed takes its as_of, both from
    the migration and from a restore of an older archive."""

    def test_the_migration_backfills_from_as_of(self):
        conn = make_db()
        try:
            write_config(conn, manual_assets=[
                {"name": "House", "kind": "real_estate", "value": 300000,
                 "as_of": "2025-06-01"},
                {"name": "Car", "kind": "vehicle", "value": 12000,
                 "as_of": "2026-01-15", "acquired_on": "2021-03-02"},
                {"name": "Boat", "kind": "vehicle", "value": 4000}])
            sql = (pathlib.Path(migrations.__file__).parent / "migrations"
                   / "228_a_hand_entered_asset_remembers_when_it_was_"
                     "acquired.sql").read_text()
            for _ in range(2):          # a second run changes nothing
                conn.execute(sql)
                got = {a["name"]: a.get("acquired_on")
                       for a in budget.load_config(conn)["manual_assets"]}
                self.assertEqual(got, {"House": "2025-06-01",
                                       "Car": "2021-03-02", "Boat": None})
        finally:
            conn.close()

    def test_a_restored_row_takes_its_as_of(self):
        cfg = {"manual_assets": [
            {"name": "House", "kind": "real_estate", "value": 300000,
             "as_of": "2025-06-01"},
            {"name": "Car", "kind": "vehicle", "value": 12000,
             "as_of": "2026-01-15", "acquired_on": "not a day"}]}
        restore.clean_manual_assets(cfg)
        self.assertEqual([a["acquired_on"] for a in cfg["manual_assets"]],
                         ["2025-06-01", "2026-01-15"])


class AMortgageHeldAtTodaysBalance(unittest.TestCase):
    """No month-end is recorded, so the property layer holds every month at
    today's figure (house less today's loan balance). The months before the
    house leave out exactly what that layer held for the loan."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, manual_assets=[
            {"name": "House", "kind": "real_estate", "value": 300000,
             "as_of": "2025-06-01", "acquired_on": "2025-06-01"}])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('mtg','it1','Home Loan','loan','mortgage',200000)")
        add_txn(self.conn, "2025-07-05", -12000.0, "PRINCIPAL PAYMENT",
                account="mtg")

    def tearDown(self):
        self.conn.close()

    def test_the_months_before_the_house_are_the_financial_accounts(self):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[[m, 4750.0] for m in MONTHS]):
            full = dict(reporting.compute_networth(
                self.conn, today=TODAY)["full_trend"])
        for m in ("2025-03", "2025-04", "2025-05"):
            self.assertEqual(full[m], 4750.0, m)
        for m in ("2025-06", "2025-07", "2026-11"):
            self.assertEqual(full[m], 4750.0 + 100000.0, m)


if __name__ == "__main__":
    unittest.main()
