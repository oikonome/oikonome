"""A restore beside a person's edit: the edit waits its turn or answers
busy within the bounded wait, and afterwards the household is one the two
make one after the other (specs/invariants-and-fuzzing.md, section 3,
ninth scenario, and R8).

A restore holds the household's storage, pairing and identity locks for
its whole transaction. An edit behind it waits at most
reanchor.REQUEST_LOCK_WAIT_MS and then answers 409, writing nothing; an
edit that gets in first is merged with the archive as either serial order
merges it. Money invariants hold on what both touched.
"""

from __future__ import annotations

import threading
import unittest

from oikonome.db import tenancy
from oikonome.sync import export, restore_job
from oikonome.web import api
from oikonome.web.fields import Fields

from .concurrency import (Household, ScenarioCase, Scenario, Seam,
                          fixed_schedules, money_invariants, outlasting)
from .util import add_txn, join_product_threads

STORAGE = Seam("the restore's storage lock", rank=1)
PAIRING = Seam("the pairing lock", rank=2)
SEAMS = (STORAGE, PAIRING)


def _setup(hh):
    add_txn(hh.conn, "2026-03-02", 12.0, "CORNER BAKERY", account="chk",
            txn_id="r1")


def _restore(data):
    def run(hh, ctx):
        # the categorize pass after a restore runs on a thread of its own,
        # after the commit; the restore is counted as done once that has
        # finished too, so the digest reads what it wrote
        before = set(threading.enumerate())
        conn = hh.connect()
        try:
            return restore_job.restore_now(conn, data, tenant_id=hh.tid)
        finally:
            conn.close()
            join_product_threads(before)
    return run


def _note(hh, ctx):
    return api.set_txn_note("r1", user=hh.user,
                            body=Fields({"note": "for the school bake sale"}))


def _split(hh, ctx):
    return api.set_split_api("r1", user=hh.user, body=Fields({"parts": [
        {"category": "FOOD_AND_DRINK", "amount": 7.0},
        {"category": "ENTERTAINMENT", "amount": 5.0}]}))


def _digest(hh, ctx):
    c = hh.conn
    return (
        tuple((r["id"], round(r["amount"], 2), r["name"], r["removed"],
               r["category_override"]) for r in c.execute(
            "SELECT id, amount, name, removed, category_override"
            "  FROM transactions ORDER BY id").fetchall()),
        tuple((r["txn_id"], r["note"]) for r in c.execute(
            "SELECT txn_id, note FROM transaction_notes ORDER BY 1"
        ).fetchall()),
        tuple((r["txn_id"], r["line"], r["category"], float(r["amount"]))
              for r in c.execute(
            "SELECT txn_id, line, category, amount FROM transaction_splits"
            " ORDER BY 1, 2").fetchall()))


class ARestoreBesideAnEdit(ScenarioCase, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # the archive: another household's ledger, with a note and a split
        # of its own, built once
        src = Household.new()
        try:
            add_txn(src.conn, "2026-03-05", 64.0, "HARBOR HARDWARE",
                    account="chk", txn_id="a1")
            add_txn(src.conn, "2026-03-06", 30.0, "LANTERN BOOKS",
                    account="card", txn_id="a2")
            api.set_txn_note("a1", user=src.user,
                             body=Fields({"note": "the fence posts"}))
            conn = tenancy.tenant_connect(src.tid)
            try:
                cls.data = export.build_zip(conn)
            finally:
                conn.close()
        finally:
            src.close()

    def _scenario(self, name, edit):
        return Scenario(
            f"a restore beside {name}", setup=_setup,
            actors={"restore": _restore(self.data), "edit": edit},
            seams=SEAMS, digest=_digest,
            invariants=lambda hh, ctx: money_invariants(hh.conn),
            doors=frozenset({"edit"}))

    def test_a_restore_beside_a_note(self):
        sc = self._scenario("a note", _note)
        self.check(sc, fixed_schedules(sc.actors, SEAMS)
                   + [outlasting("restore", PAIRING, "edit")])

    def test_a_restore_beside_a_split(self):
        self.check(self._scenario("a split", _split))


if __name__ == "__main__":
    unittest.main()
