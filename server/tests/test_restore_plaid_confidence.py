"""The aggregator's category confidence is a TEXT tier ('VERY_HIGH',
'HIGH', 'MEDIUM', 'LOW', …) — see the transactions schema and the sync
writer, which both treat it as a string. The restore runs inside one
all-or-nothing transaction, so a restore that coerces the column to float
doesn't just drop the value: the first real Plaid row aborts the ENTIRE
restore. Any genuine Plaid dataset would fail to migrate."""

import csv
import io
import json
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import restore

from .util import TODAY, _ensure_db, add_txn, make_db


def _export_zip(conn, tables) -> bytes:
    """SELECT * → CSV zip, the same shape /export writes (mirrors the
    round-trip fixture in test_restore.py)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl in tables:
            rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()
            s = io.StringIO()
            if rows:
                cols = [c for c in rows[0].keys()
                        if c not in ("tenant_id", "access_token")]
                w = csv.DictWriter(s, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({k: (json.dumps(r[k])
                                    if isinstance(r[k], (dict, list))
                                    else r[k]) for k in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


class PlaidConfidenceRoundTripTests(unittest.TestCase):

    def test_confidence_string_survives_export_restore(self):
        _ensure_db()
        a = make_db()
        add_txn(a, TODAY, 12.5, "SAFEWAY", primary="FOOD_AND_DRINK",
                txn_id="conf-1")
        a.execute("""UPDATE transactions
                        SET category_plaid='FOOD_AND_DRINK',
                            category_plaid_detailed='FOOD_AND_DRINK_GROCERIES',
                            category_plaid_confidence='VERY_HIGH'
                      WHERE id='conf-1'""")
        data = _export_zip(a, ["items", "accounts", "transactions"])
        a.close()

        admin = tenancy.admin_connect()
        tid_b = tenancy.create_tenant(admin, "restore-conf-b")
        admin.close()
        b = tenancy.tenant_connect(tid_b)
        try:
            counts = restore.restore_zip(b, data)
            self.assertGreaterEqual(counts.get("transactions", 0), 1)
            row = b.execute(
                """SELECT category_plaid, category_plaid_confidence
                     FROM transactions WHERE id='conf-1'""").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["category_plaid_confidence"], "VERY_HIGH")
            self.assertEqual(row["category_plaid"], "FOOD_AND_DRINK")
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
