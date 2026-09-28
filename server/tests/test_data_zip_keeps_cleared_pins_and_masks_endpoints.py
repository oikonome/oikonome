"""The "Your data" ZIP carries what restore needs to rebuild a link's
memory exactly, and nothing that authenticates.

A link that replaced a person's explicit "no category" pin remembers the
empty pin; one that replaced nothing remembers NULL. A CSV cell writes
both as empty, so without a companion flag the cleared pin comes back as
nothing, and unlinking on the restored copy deletes the pin instead of
putting it back — the bill and store passes then recategorise a row the
person deliberately cleared.

And a closed month's budget snapshot is a frozen copy of the settings.
One frozen before the scrub existed still holds the endpoint URL as it was
typed, password included; the archive masks it exactly as it masks the
live settings row."""

import csv
import io
import json
import unittest
import zipfile

from oikonome.sync import export, restore
from oikonome.web import data

from .util import TODAY, _ensure_db, add_txn, make_db


def _member(z: bytes, name: str) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        return list(csv.DictReader(io.StringIO(
            zf.read(name).decode("utf-8"))))


class DataZipKeepsClearedPinsAndMasksEndpointsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _pin(conn, tid):
        r = conn.execute(
            "SELECT category FROM manual_categories WHERE transaction_id=%s",
            (tid,)).fetchone()
        return None if r is None else r["category"]

    def test_a_cleared_pin_a_link_replaced_survives_export_and_restore(self):
        exp = add_txn(self.conn, TODAY, 200.0, "OFFICE SUPPLY CO")
        dep = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                      account="chk", primary="INCOME")
        data.set_category(self.conn, exp, "")      # the person's "no category"
        self.assertEqual(self._pin(self.conn, exp), "")
        data.link_reimbursement(self.conn, exp, dep)
        z = export.build_zip(self.conn)
        flags = {r["transaction_id"]: r["link_prior_set"]
                 for r in _member(z, "manual_categories.csv")}
        self.assertEqual(flags, {exp: "True", dep: "False"})
        dest = make_db()
        try:
            restore.restore_zip(dest, z)
            data.unlink_reimbursement(dest, exp, dep)
            self.assertEqual(self._pin(dest, exp), "")   # given back, as ''
            self.assertIsNone(self._pin(dest, dep))      # nothing to give back
        finally:
            dest.close()

    def test_a_snapshot_endpoint_password_is_masked_in_the_zip(self):
        cfg = {"llm_url": "https://user:hunter2@llm.example.test/v1",
               "nested": {"url": "https://user:hunter2@hook.example.test/x"},
               "income": 5000}
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config, bills) "
            "VALUES (2026, 6, %s::jsonb, '[]'::jsonb)", (json.dumps(cfg),))
        z = export.build_zip(self.conn)
        rows = _member(z, "budget_snapshots.csv")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("hunter2", rows[0]["config"])
        out = json.loads(rows[0]["config"])
        self.assertTrue(out["llm_url"].startswith("https://user:"))
        self.assertTrue(out["llm_url"].endswith("@llm.example.test/v1"))
        self.assertEqual(out["income"], 5000)


if __name__ == "__main__":
    unittest.main()
