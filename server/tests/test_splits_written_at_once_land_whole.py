"""Two splits written at once end as one of them, whole; a split written
while its hold settles ends on the posted row (specs/invariants-and-fuzzing.md,
section 3, first scenario).

A split is several rows that only mean anything together. Two people
splitting one charge at the same moment must leave one person's parts,
never a mix that adds up to neither, and never a deadlock. A hold the
bank settles while someone splits it carries the parts to the posted row,
or the split follows the hold there: in either order the parts end on the
row every page reads, summing to its amount.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.sync import base
from oikonome.web import api
from oikonome.web.fields import Fields

from .concurrency import ScenarioCase, Scenario, Seam, money_invariants
from .util import add_txn

DAY = dt.date.today() - dt.timedelta(days=2)

PAIRING = Seam("the pairing lock", rank=2)
CHARGE_ROW = Seam("the charge's row lock",
                  sql=r"FROM transactions t\s+WHERE t\.id = %s AND t\.removed = 0 FOR UPDATE")
SETTLE_ROWS = Seam("the settling rows' locks",
                   sql=r"WHERE id = ANY\(%s\)\s+ORDER BY id FOR UPDATE")


def _split(txn_id, parts):
    def write(hh, ctx):
        return api.set_split_api(txn_id, user=hh.user, body=Fields({
            "parts": [{"category": c, "amount": a} for c, a in parts]}))
    return write


def _digest(ids):
    def digest(hh, ctx):
        c = hh.conn
        return (
            tuple((r["txn_id"], r["line"], r["category"], float(r["amount"]),
                   r["source"]) for r in c.execute(
                "SELECT txn_id, line, category, amount, source"
                "  FROM transaction_splits ORDER BY txn_id, line").fetchall()),
            tuple((r["id"], r["removed"]) for r in c.execute(
                "SELECT id, removed FROM transactions WHERE id = ANY(%s)"
                " ORDER BY id", (list(ids),)).fetchall()))
    return digest


def _parts_add_up(hh, ctx) -> list[str]:
    return money_invariants(hh.conn)


class SplitsWrittenAtOnce(ScenarioCase, unittest.TestCase):
    def test_two_splits_of_one_charge_end_as_one_of_them(self):
        def setup(hh):
            add_txn(hh.conn, DAY, 60.0, "CORNER MARKET", account="chk",
                    txn_id="r1")

        self.check(Scenario(
            "two splits of one charge",
            setup=setup,
            actors={"A": _split("r1", [("FOOD_AND_DRINK", 40.0),
                                       ("ENTERTAINMENT", 20.0)]),
                    "B": _split("r1", [("FOOD_AND_DRINK", 10.0),
                                       ("TRAVEL", 50.0)])},
            seams=(PAIRING, CHARGE_ROW),
            digest=_digest(["r1"]),
            invariants=_parts_add_up,
            doors=frozenset({"A", "B"})))

    def test_a_split_beside_its_holds_settlement_ends_on_the_posted_row(self):
        def setup(hh):
            add_txn(hh.conn, DAY, 38.0, "CORNER BISTRO", account="chk",
                    pending=1, txn_id="hold-1")

        def settle(hh, ctx):
            conn = hh.connect()
            try:
                return base.upsert_transactions(conn, [base.Transaction(
                    id="post-1", account_id="chk", date=DAY, amount=38.0,
                    name="CORNER BISTRO",
                    raw={"pending_transaction_id": "hold-1"})])
            finally:
                conn.close()

        def ends_on_the_posted_row(hh, ctx):
            out = _parts_add_up(hh, ctx)
            if not hh.conn.execute("SELECT 1 FROM transaction_splits"
                                   " WHERE txn_id = 'post-1'").fetchone():
                out.append("the split did not reach the posted row")
            return out

        self.check(Scenario(
            "a split beside its hold's settlement",
            setup=setup,
            actors={"split": _split("hold-1", [("FOOD_AND_DRINK", 30.0),
                                               ("ENTERTAINMENT", 8.0)]),
                    "settle": settle},
            seams=(PAIRING, SETTLE_ROWS),
            digest=_digest(["hold-1", "post-1"]),
            invariants=ends_on_the_posted_row,
            doors=frozenset({"split"})))


if __name__ == "__main__":
    unittest.main()
