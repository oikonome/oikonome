"""A receipt snapped while a file import runs ends as the two run one after
the other (specs/invariants-and-fuzzing.md, section 3, fifth scenario).

Both take the household's file-storage lock and then its pairing lock: the
snap reserves its bytes and then finds its charge, the import takes the
ledger locks and may park its upload (bytes again) to ask the person
something. Run at once, they take turns: the receipt lands once on its
charge, the import's rows or its parked upload land once, the storage
count holds both, and neither is killed as a deadlock.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from oikonome.engine import receipts
from oikonome.web import api, pages  # noqa: F401  (api loads the app)

from .concurrency import ScenarioCase, Scenario, Seam
from .util import add_txn

STORAGE = Seam("the file-storage lock", rank=1)
PAIRING = Seam("the pairing lock", rank=2)
# no importer recognizes these columns, so the import parks for mapping
UNMAPPED = (b"Alpha,Beta,Gamma\n"
            b"first,second,third\n"
            b"fourth,fifth,sixth\n")
ROWS = (b"Date,Description,Amount\n"
        b"2026-03-03,HARBOR HARDWARE,-23.10\n"
        b"2026-03-04,CORNER BAKERY,-12.00\n")
PDF = b"%PDF-1.4\n% synthetic receipt\n"
QUOTA_ON = {"OIKONOME_HOUSEHOLD_STORAGE_MB": "2048"}


def _setup(hh):
    add_txn(hh.conn, "2026-03-02", 12.0, "CORNER BAKERY", account="chk",
            txn_id="r1")


def _snap(hh, ctx):
    conn = hh.connect()
    try:
        return receipts.add(conn, "r1", PDF, "application/pdf")
    finally:
        conn.close()


def _import(name, data):
    def run(hh, ctx):
        conn = hh.connect()
        try:
            return pages.dispatch_import(conn, "chk", "bank", name, data,
                                         role="owner")
        finally:
            conn.close()
    return run


def _digest(hh, ctx):
    c = hh.conn
    return (
        tuple((r["txn_id"], r["mime"], r["kind"]) for r in c.execute(
            "SELECT txn_id, mime, kind FROM receipts ORDER BY 1, 2"
        ).fetchall()),
        tuple((r["kind"], r["filename"]) for r in c.execute(
            "SELECT kind, filename FROM import_staging ORDER BY 1, 2"
        ).fetchall()),
        tuple((str(r["date"]), round(r["amount"], 2), r["name"], r["removed"])
              for r in c.execute(
            "SELECT date, amount, name, removed FROM transactions"
            " ORDER BY date, name, amount").fetchall()))


def _bytes_counted(hh, ctx) -> list[str]:
    """The receipt's bytes are the household's once: one receipt per
    snap, on the charge it was taken for."""
    n = hh.conn.execute("SELECT count(*) AS n FROM receipts").fetchone()["n"]
    return [] if n == 1 else [f"{n} receipts for one snap"]


class ASnapBesideAnImport(ScenarioCase, unittest.TestCase):
    def _check(self, name, data):
        self.check(Scenario(
            f"a receipt snap beside an import that {name}",
            setup=_setup,
            actors={"snap": _snap, "import": _import(f"{name}.csv", data)},
            seams=(STORAGE, PAIRING), digest=_digest,
            invariants=_bytes_counted,
            patches=lambda: mock.patch.dict(os.environ, QUOTA_ON)))

    def test_a_snap_beside_an_import_that_parks_its_upload(self):
        self._check("parks", UNMAPPED)

    def test_a_snap_beside_an_import_that_lands_rows(self):
        self._check("lands", ROWS)


if __name__ == "__main__":
    unittest.main()
