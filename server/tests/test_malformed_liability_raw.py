"""A malformed liabilities row must not take down the household's forecast.

The aggregator writes a card's liabilities raw with a numeric
last_statement_balance and an ISO next_payment_due_date; a restored ZIP
carries whatever the archive held. The forecast casts the balance in SQL
and parses the date in Python, so one "N/A" or "soon" raises out of both —
on every Today load, the Accounts page and the nightly email — until the
row is edited in the database. Two guards, tested separately:
the restore cleans the fields it cannot use, and the readers tolerate a
bad value that is already in the table.
"""

import csv
import datetime as dt
import io
import json
import unittest
import zipfile

from oikonome.engine import forecast
from oikonome.engine.compat import jsonb
from oikonome.sync import restore
from oikonome.web import data

from .util import TODAY, make_db, write_config


def _zip(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["account_id", "as_of", "raw"])
        w.writeheader()
        for r in rows:
            w.writerow({"account_id": "", "as_of": "", **r})
        z.writestr("liabilities.csv", s.getvalue())
    return buf.getvalue()


class CleanLiabilityRawTests(unittest.TestCase):
    def test_non_object_raw_reads_as_empty(self):
        for bad in ([1, 2], "x", 7, None):
            self.assertEqual(restore.clean_liability_raw(bad), {})

    def test_unusable_balance_and_date_are_dropped_not_the_row(self):
        raw = restore.clean_liability_raw({
            "last_statement_balance": "N/A",
            "minimum_payment_amount": [25],
            "next_payment_due_date": "soon",
            "last_payment_date": "2026-13-45",
            "last_statement_issue_date": "0999-01-01",
            "aprs": [{"apr_percentage": 24.99}]})
        for key in ("last_statement_balance", "minimum_payment_amount",
                    "next_payment_due_date", "last_payment_date",
                    "last_statement_issue_date"):
            self.assertNotIn(key, raw)
        # fields the forecast does not read pass through untouched
        self.assertEqual(raw["aprs"], [{"apr_percentage": 24.99}])

    def test_usable_values_are_kept_in_the_pull_shape(self):
        raw = restore.clean_liability_raw({
            "last_statement_balance": "300.50",
            "minimum_payment_amount": 25,
            "next_payment_due_date": "2026-08-05T00:00:00",
            "last_payment_date": "2026-07-05"})
        self.assertEqual(raw["last_statement_balance"], 300.5)
        self.assertEqual(raw["minimum_payment_amount"], 25)
        self.assertEqual(raw["next_payment_due_date"], "2026-08-05")
        self.assertEqual(raw["last_payment_date"], "2026-07-05")

    def test_non_finite_balances_are_dropped(self):
        for bad in ("nan", "inf", float("inf"), True):
            self.assertNotIn(
                "last_statement_balance",
                restore.clean_liability_raw({"last_statement_balance": bad}),
                bad)


class RestoreThenForecastTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)

    def tearDown(self):
        self.conn.close()

    def test_a_poisoned_liabilities_row_does_not_break_the_forecast(self):
        restore.restore_zip(self.conn, _zip([
            {"account_id": "card",
             "raw": json.dumps({"last_statement_balance": "N/A",
                                "next_payment_due_date": "soon"})}]))
        row = self.conn.execute(
            "SELECT raw FROM liabilities WHERE account_id='card'").fetchone()
        self.assertEqual(row["raw"], {})
        f = forecast.build(self.conn, today=TODAY, days=30)
        # no usable due date or statement: the full balance, tomorrow
        self.assertEqual(f["card_events"],
                         [((TODAY + dt.timedelta(days=1)).isoformat(),
                           -250.0, "Test Card payment")])
        card = next(r for r in data.accounts_detail(self.conn)
                    if r["id"] == "card")
        self.assertIsNone(card["card_statement"])
        self.assertIsNone(card["card_due"])


class TolerantReadTests(unittest.TestCase):
    """The same rows, already in the table (an older restore, a hand edit):
    the readers must not depend on the restore having cleaned them."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)

    def tearDown(self):
        self.conn.close()

    def _liability(self, raw: dict) -> None:
        self.conn.execute(
            """INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)
               ON CONFLICT (tenant_id, account_id)
               DO UPDATE SET raw = EXCLUDED.raw""", (jsonb(raw),))

    def test_forecast_survives_a_non_numeric_statement_balance(self):
        self._liability({"last_statement_balance": "N/A",
                         "next_payment_due_date":
                             (TODAY + dt.timedelta(days=10)).isoformat()})
        f = forecast.build(self.conn, today=TODAY, days=30)
        due = (TODAY + dt.timedelta(days=10)).isoformat()
        self.assertEqual(f["card_events"], [(due, -250.0, "Test Card payment")])
        # an unusable statement balance means the full balance, as when
        # the issuer never gave one
        self.assertEqual([(d, a) for d, a, _ in f["card_events_stmt"]],
                         [(due, -250.0)])

    def test_forecast_survives_an_unparsable_due_date(self):
        for bad in ("soon", "2026-13-45", "0999-01-01", 12345):
            self._liability({"last_statement_balance": 100.0,
                             "next_payment_due_date": bad})
            f = forecast.build(self.conn, today=TODAY, days=30)
            tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
            self.assertEqual(f["card_events"],
                             [(tomorrow, -250.0, "Test Card payment")], bad)
            # the assumed date is not a calendar fact: no chart marker
            self.assertEqual(f["card_autopay"], [], bad)

    def test_accounts_detail_survives_a_non_numeric_statement_balance(self):
        self._liability({"last_statement_balance": "N/A",
                         "next_payment_due_date": "2026-08-05"})
        card = next(r for r in data.accounts_detail(self.conn)
                    if r["id"] == "card")
        self.assertIsNone(card["card_statement"])
        self.assertEqual(card["card_due"], "2026-08-05")

    def test_a_good_statement_balance_still_reads_as_a_number(self):
        self._liability({"last_statement_balance": 120.25,
                         "next_payment_due_date": "2026-08-05"})
        card = next(r for r in data.accounts_detail(self.conn)
                    if r["id"] == "card")
        self.assertEqual(card["card_statement"], 120.25)
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual([(d, a) for d, a, _ in f["card_events_stmt"]],
                         [("2026-08-05", -120.25),
                          ("2026-09-05", -129.75)])


if __name__ == "__main__":
    unittest.main()
