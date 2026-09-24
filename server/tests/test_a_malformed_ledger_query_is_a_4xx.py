"""A ledger link the household never wrote must be answered, not crashed.

Every deep link into the ledger carries a period — a year with a month, a
year alone for a money-map label, a date window from a timeframe toggle —
and every one of those reaches `dt.date()`, a calendar lookup or a bigint
bind. A hand-edited URL, a stale bookmark or a client that sends the wrong
shape has to come back as a refusal naming the parameter, never as an
opaque server error: a 500 tells the reader nothing and hides a broken
client from whoever is reading the logs.
"""

import datetime as dt
import os
import unittest
import uuid
import zoneinfo
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

# a fixed household moment, so "this year" is the fixture's year whenever
# the suite runs
LOCAL_NOW = dt.datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0,
                        tzinfo=zoneinfo.ZoneInfo("UTC"))


class MalformedLedgerQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"badq-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=1000, other_monthly=1000)
            add_txn(conn, dt.date(TODAY.year, TODAY.month, 10), 40.0,
                    "SAFEWAY", primary="FOOD_AND_DRINK")
        finally:
            conn.close()

    def get(self, qs: str):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            return self.client.get(f"/api/transactions?{qs}")

    def test_an_impossible_period_is_refused_not_crashed(self):
        for qs in ("y=99999", "y=0&m=13", "y=-1", "m=13", "m=0&y=1899",
                   "y=2026&m=13", "bucket=food&y=99999", "bucket=food&m=13",
                   "counted=1&cat=Food&y=2026&m=13"):
            r = self.get(qs)
            self.assertEqual(r.status_code, 400, f"{qs} -> {r.text}")
            self.assertEqual(r.json()["detail"], "y/m out of range", qs)

    def test_a_period_that_is_not_a_number_is_refused(self):
        for qs in ("y=abc", "m=abc", "page=abc", "bucket=food&y=abc"):
            r = self.get(qs)
            self.assertEqual(r.status_code, 422, f"{qs} -> {r.text}")

    def test_a_date_window_that_is_not_a_date_is_refused(self):
        """The timeframe toggles set date_from/date_to. A year the ledger
        cannot store (0001, 9999) is as unusable as junk text, and both
        have to be caught before the search builds its bind."""
        for qs, field in (("date_from=nonsense", "date_from"),
                          ("date_to=2026-13-40", "date_to"),
                          ("date_from=0001-01-01", "date_from"),
                          ("date_from=2026-01-01&date_to=9999-12-31",
                           "date_to")):
            r = self.get(qs)
            self.assertEqual(r.status_code, 400, f"{qs} -> {r.text}")
            self.assertEqual(r.json()["detail"],
                             f"{field} must be YYYY-MM-DD", qs)

    def test_a_name_that_is_no_buckets_is_refused_in_both_scopes(self):
        """A year's bucket is `y` with no `m`; a month's is `y` and `m`.
        Either way a name the plan does not have — or a year the plan
        never covered — is a refusal, not an empty listing that would read
        as "you spent nothing"."""
        for qs in (f"bucket=nosuchbucket&y={TODAY.year}",
                   f"bucket=nosuchbucket&y={TODAY.year}&m={TODAY.month}",
                   "bucket=food&y=1900", "bucket=food&y=2100"):
            r = self.get(qs)
            self.assertEqual(r.status_code, 400, f"{qs} -> {r.text}")
            self.assertEqual(r.json()["detail"], "unknown bucket", qs)
        # the shape both clients actually send for a money-map label does
        # answer, so the refusals above are about the input, not the route
        r = self.get(f"bucket=food&y={TODAY.year}&page=1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["bucket_scope"], "year")

    def test_a_read_on_day_belongs_to_the_month_it_names(self):
        y, m = TODAY.year, TODAY.month
        for qs, detail in (
                (f"bucket=food&y={y}&m={m}&as_of=nonsense",
                 "as_of must be YYYY-MM-DD"),
                (f"bucket=food&y={y}&m={m}&as_of=1999-01-01",
                 "as_of must be a day of that month, not after today"),
                (f"bucket=food&y={y}&as_of={TODAY.isoformat()}",
                 "as_of belongs to a month's bucket")):
            r = self.get(qs)
            self.assertEqual(r.status_code, 400, f"{qs} -> {r.text}")
            self.assertEqual(r.json()["detail"], detail, qs)

    def test_an_unknown_payee_history_window_reads_as_all(self):
        """The timeframe vocabulary grows; an older client asking for a
        window this build has never heard of gets the whole history, not
        an error, because the lifetime aggregate beside it is lifetime
        anyway."""
        for qs in ("payee=SAFEWAY", "payee=SAFEWAY&window=all",
                   "payee=SAFEWAY&window=", "payee=SAFEWAY&window=nosuch",
                   "payee=SAFEWAY&window=cur", "payee=SAFEWAY&window=1m"):
            r = self.client.get(f"/api/bills/history?{qs}")
            self.assertEqual(r.status_code, 200, f"{qs} -> {r.text}")


if __name__ == "__main__":
    unittest.main()
