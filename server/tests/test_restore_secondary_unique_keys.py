"""restore_zip promises that restoring into a populated tenant skips rows
already present instead of failing — and it runs as one all-or-nothing
transaction, so a single unhandled unique violation throws away the whole
restore.

Two tables carry a SECOND unique key beyond (tenant_id, id):
equity_movement has a partial unique index on (tenant_id, txn_id, kind)
where txn_id is set, and vendor_1099 has UNIQUE (tenant_id, entity_id,
merchant). A destination row that matches on the secondary key under a
DIFFERENT id sails past an id-targeted ON CONFLICT clause and aborts the
entire restore. The equivalent row must be skipped and everything else in
the ZIP must still land."""

import csv
import io
import unittest
import uuid
import zipfile

from oikonome.sync import restore

from .util import TODAY, _ensure_db, add_txn, make_db


def _zip(tables: dict[str, tuple[list[str], list[dict]]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl, (cols, rows) in tables.items():
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else r.get(c))
                            for c in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


class SecondaryUniqueKeyCollisionTests(unittest.TestCase):

    def test_restore_completes_when_destination_collides_on_secondary_keys(self):
        _ensure_db()
        conn = make_db()
        try:
            entity = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO business_entity (id, name, structure)
                   VALUES (%s, 'Acme LLC', 'sole_prop')""", (entity,))
            add_txn(conn, TODAY, 500.0, "OWNER CONTRIBUTION",
                    txn_id="uniq-tx-1")
            # destination already recorded the same movement and the same
            # vendor, under its OWN ids
            conn.execute(
                """INSERT INTO equity_movement (id, entity_id, kind, amount,
                       date, txn_id)
                   VALUES (%s, %s, 'contribution', 500.0, %s, 'uniq-tx-1')""",
                (str(uuid.uuid4()), entity, TODAY))
            conn.execute(
                """INSERT INTO vendor_1099 (id, entity_id, merchant,
                       reportable)
                   VALUES (%s, %s, 'ACME CONTRACTOR', TRUE)""",
                (str(uuid.uuid4()), entity))

            data = _zip({
                "business_entity": (["id", "name", "structure"], [
                    {"id": entity, "name": "Acme LLC",
                     "structure": "sole_prop"}]),
                # a fresh transaction that must land even though other rows
                # in the same ZIP collide — proof the restore wasn't aborted
                "transactions": (
                    ["id", "account_id", "date", "amount", "name"], [
                        {"id": "uniq-tx-2", "account_id": "chk",
                         "date": str(TODAY), "amount": 42.0,
                         "name": "LANDS ANYWAY"}]),
                "equity_movement": (
                    ["id", "entity_id", "kind", "amount", "date",
                     "member_id", "txn_id", "form", "note"], [
                        {"id": str(uuid.uuid4()), "entity_id": entity,
                         "kind": "contribution", "amount": 500.0,
                         "date": str(TODAY), "txn_id": "uniq-tx-1"}]),
                "vendor_1099": (
                    ["id", "entity_id", "merchant", "reportable",
                     "tin_last4", "note"], [
                        {"id": str(uuid.uuid4()), "entity_id": entity,
                         "merchant": "ACME CONTRACTOR",
                         "reportable": "True"}]),
            })

            counts = restore.restore_zip(conn, data)

            # the non-colliding row landed — the restore was not rolled back
            self.assertGreaterEqual(counts.get("transactions", 0), 1)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM transactions WHERE id='uniq-tx-2'").fetchone())
            # colliding rows were skipped, not duplicated and not fatal
            self.assertEqual(conn.execute(
                """SELECT COUNT(*) AS n FROM equity_movement
                    WHERE txn_id='uniq-tx-1' AND kind='contribution'"""
            ).fetchone()["n"], 1)
            self.assertEqual(conn.execute(
                """SELECT COUNT(*) AS n FROM vendor_1099
                    WHERE merchant='ACME CONTRACTOR'""").fetchone()["n"], 1)
            self.assertEqual(counts.get("equity_movement", 0), 0)
            self.assertEqual(counts.get("vendor_1099", 0), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
