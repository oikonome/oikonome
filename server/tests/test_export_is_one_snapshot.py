"""An export is one moment of the household, not one moment per table.

Syncs, collectors and other members keep writing while an export streams.
If every table is read at its own moment, a new account that commits after
accounts were read but before transactions are read leaves its charges in
the archive with no account to hang them on. The full-data ZIP is restored
in one transaction with foreign keys, so such an archive is refused whole,
and the person only learns it when they try to move. The portable archive
claims to be a complete copy, and would hold rows that point at nothing.

The invariant: every row an export writes refers only to rows the same
export also wrote, however many writes land while it runs.
"""

import io
import json
import unittest
import zipfile

from oikonome import tenant_export
from oikonome.db import tenancy
from oikonome.sync import export, restore

from .util import make_db


def _tid(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]


def _write_new_account_elsewhere(tid: str) -> None:
    """What a sync committing on another connection looks like."""
    other = tenancy.tenant_connect(tid)
    try:
        other.execute(
            "INSERT INTO accounts (id, item_id, name, type) "
            "VALUES ('mid-export', 'it1', 'Opened Mid Export', 'depository')")
        other.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed) VALUES ('mid-export-1', 'mid-export', "
            "'2026-07-01', 12.5, 'LANTERN BAKERY', 0, 0)")
    finally:
        other.close()


class _WritesBeforeTransactions:
    """The export's connection, with a concurrent write committed just as
    the export reaches the transactions table (after accounts is read)."""

    def __init__(self, conn, tid):
        self._conn, self._tid, self.fired = conn, tid, False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def cursor(self, *a, **kw):
        if kw.get("name") == "exp_transactions" and not self.fired:
            self.fired = True
            _write_new_account_elsewhere(self._tid)
        return self._conn.cursor(*a, **kw)


class FullExportSnapshotTests(unittest.TestCase):
    def test_an_account_opened_mid_export_does_not_break_the_restore(self):
        conn = make_db()
        tid = _tid(conn)
        conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed) VALUES ('before-1', 'chk', '2026-06-01', 4.0, "
            "'HARBOR LIGHTS CAFE', 0, 0)")
        proxy = _WritesBeforeTransactions(conn, tid)
        try:
            data = export.build_zip(proxy)
        finally:
            conn.close()
        self.assertTrue(proxy.fired, "the concurrent write never ran")
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            ids = {r["id"] for r in dest.execute(
                "SELECT id FROM transactions").fetchall()}
            self.assertIn("before-1", ids)
        finally:
            dest.close()


def _with_rows(data: bytes, extra: dict) -> bytes:
    """The archive with rows appended to some members, as an archive
    written one table at a time could hold them."""
    import csv
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for n in zin.namelist():
            body = zin.read(n).decode()
            if n in extra:
                header = next(csv.reader(io.StringIO(body)), None)
                s = io.StringIO()
                w = csv.DictWriter(s, fieldnames=header or list(extra[n][0]),
                                   extrasaction="ignore")
                if header is None:
                    w.writeheader()
                for row in extra[n]:
                    w.writerow(row)
                body += s.getvalue()
            zout.writestr(n, body)
    return out.getvalue()


class TableByTableArchiveTests(unittest.TestCase):
    """Archives already written by an exporter that read each table at its
    own moment exist, and may hold a row whose parent it missed. Restore
    keeps what it can rather than refusing the whole archive: a pointer to
    a business or connection that is not there is dropped (what deleting
    that parent does to it anyway), and a row that cannot exist without its
    missing parent is skipped."""

    def test_rows_pointing_at_missing_parents_do_not_sink_the_restore(self):
        src = make_db()
        try:
            src.execute(
                "INSERT INTO transactions (id, account_id, date, amount, "
                "name, pending, removed) VALUES ('kept-1', 'chk', "
                "'2026-06-01', 4.0, 'HARBOR LIGHTS CAFE', 0, 0)")
            data = export.build_zip(src)
        finally:
            src.close()
        gone = "00000000-0000-4000-8000-00000000dead"
        data = _with_rows(data, {
            "accounts.csv": [{"id": "orphan-acct", "item_id": "no-such-item",
                              "name": "Opened Late", "type": "depository",
                              "entity_id": gone}],
            "transactions.csv": [
                {"id": "orphan-1", "account_id": "no-such-acct",
                 "date": "2026-06-02", "amount": "3.0", "name": "X",
                 "pending": "0", "removed": "0"},
                {"id": "kept-2", "account_id": "chk", "date": "2026-06-03",
                 "amount": "5.0", "name": "LANTERN BAKERY", "pending": "0",
                 "removed": "0", "entity_id": gone}],
            "mileage_log.csv": [{"id": "00000000-0000-4000-8000-0000000000a1",
                                 "entity_id": gone, "date": "2026-06-01",
                                 "miles": "12"}],
        })
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            txns = {r["id"]: r for r in dest.execute(
                "SELECT id, entity_id FROM transactions").fetchall()}
            self.assertIn("kept-1", txns)
            self.assertIn("kept-2", txns)
            self.assertIsNone(txns["kept-2"]["entity_id"])
            self.assertNotIn("orphan-1", txns)
            acct = dest.execute(
                "SELECT item_id, entity_id FROM accounts "
                "WHERE id='orphan-acct'").fetchone()
            self.assertIsNotNone(acct)
            self.assertIsNone(acct["item_id"])
            self.assertIsNone(acct["entity_id"])
            self.assertEqual(dest.execute(
                "SELECT count(*) AS n FROM mileage_log").fetchone()["n"], 0)
        finally:
            dest.close()


class PortableArchiveSnapshotTests(unittest.TestCase):
    def test_every_archived_charge_has_its_account_in_the_archive(self):
        conn = make_db()
        tid = _tid(conn)
        conn.close()
        real = tenancy.tenant_connect
        proxies = []

        def connect(t, *a, **kw):
            p = _WritesBeforeTransactions(real(t, *a, **kw), tid)
            proxies.append(p)
            return p

        orig = tenant_export.tenancy.tenant_connect
        tenant_export.tenancy.tenant_connect = connect
        try:
            data = tenant_export.build_archive(tid, operator="op",
                                               consented=True)
        finally:
            tenant_export.tenancy.tenant_connect = orig
        self.assertTrue(any(p.fired for p in proxies),
                        "the concurrent write never ran")
        z = zipfile.ZipFile(io.BytesIO(data))
        accounts = {a["id"] for a in json.loads(z.read("data/accounts.json"))}
        for t in json.loads(z.read("data/transactions.json")):
            self.assertIn(t["account_id"], accounts,
                          f"{t['id']} points at an account not in the archive")


if __name__ == "__main__":
    unittest.main()
