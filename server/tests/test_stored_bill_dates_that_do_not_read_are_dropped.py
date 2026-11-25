"""A bill's stored raw dates that do not read as dates are dropped once,
and its due date is taken back from the due_on column.

Restore validates a bill's raw dueOn, lastDueOn and recurrence endOn and
dueOn now (restore.clean_bill_raw), and the readers guard each one
(bills.stored_date). Rows an install wrote before either still hold "an
impossible day", a blank, a number; every reader that forgets the guard
takes the Bills screen down on them. Migration 216 drops each such key and
refills raw dueOn from the column, as a restore does. Every bill here is
invented.
"""
import datetime as dt
import json
import unittest

from oikonome.db import migrate

from .util import TODAY, add_bill, make_db

STEP = "216_bill_dates_that_do_not_read_as_dates_are_dropped.sql"


class UnreadableBillDates(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _bill(self, payee, **raw_over):
        add_bill(self.conn, payee, 21.50,
                 next_due=TODAY + dt.timedelta(days=9))
        for path, value in raw_over.items():
            self.conn.execute(
                "UPDATE bills SET raw = jsonb_set(raw, %s, %s::jsonb, true)"
                " WHERE payee = %s",
                (path.split("."), json.dumps(value), payee))

    def _raw(self, payee):
        return self.conn.execute(
            "SELECT raw, raw::text AS text, due_on FROM bills"
            " WHERE payee = %s", (payee,)).fetchone()

    def _step(self):
        migrate.PYTHON_STEPS[STEP](self.conn)

    def test_unreadable_dates_are_dropped_and_due_is_refilled(self):
        self._bill("Northwind Water", dueOn="2026-02-30")
        self._bill("Contoso Gym", **{"recurrence": {"frequency": "MONTHLY"},
                                     "recurrence.endOn": "2026-13-01",
                                     "recurrence.dueOn": 42})
        self._bill("Tailspin Power", lastDueOn="")
        self._bill("Fourth Coffee", dueOn=42)
        self._step()
        water = self._raw("Northwind Water")
        self.assertEqual(water["raw"]["dueOn"], water["due_on"].isoformat())
        gym = self._raw("Contoso Gym")["raw"]
        self.assertNotIn("endOn", gym["recurrence"])
        self.assertNotIn("dueOn", gym["recurrence"])
        self.assertEqual(gym["recurrence"]["frequency"], "MONTHLY")
        self.assertNotIn("lastDueOn", self._raw("Tailspin Power")["raw"])
        coffee = self._raw("Fourth Coffee")
        self.assertEqual(coffee["raw"]["dueOn"], coffee["due_on"].isoformat())

    def test_a_valid_row_is_left_byte_for_byte_and_a_rerun_writes_nothing(self):
        self._bill("Litware Phone", lastDueOn="2026-06-15",
                   **{"recurrence": {"frequency": "MONTHLY",
                                     "endOn": "2027-06-15"}})
        # a null is how save_bill says "no floor yet": it stays
        self._bill("Adatum Rent", lastDueOn=None)
        before = {p: self._raw(p)["text"]
                  for p in ("Litware Phone", "Adatum Rent")}
        self.assertEqual(migrate.drop_unreadable_bill_dates(self.conn), 0)
        self.assertEqual({p: self._raw(p)["text"] for p in before}, before)
        self._bill("Northwind Water", dueOn="2026-02-30")
        self.assertEqual(migrate.drop_unreadable_bill_dates(self.conn), 1)
        self.assertEqual(migrate.drop_unreadable_bill_dates(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
