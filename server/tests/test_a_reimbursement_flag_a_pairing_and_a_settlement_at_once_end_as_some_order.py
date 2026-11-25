"""A reimbursement flag, a pairing and the settlement of the same pending
hold, all at once, end as the three run in some order
(specs/invariants-and-fuzzing.md, section 3, fourth scenario).

The settlement carries what a person did to the hold onto its posted row
and retires the hold, in one commit under the pairing lock; the flag and
the link take that lock too and follow a hold that settled meanwhile to
its posted row. So whatever the timing: nothing ends on a retired row,
no link pays back more than its charge or its deposit holds, and the
household matches one serial order of the three.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import activity
from oikonome.sync import base
from oikonome.web import api, api_reimburse  # noqa: F401  (api loads the app)
from oikonome.web.fields import Fields

from .concurrency import ScenarioCase, Scenario, Seam, money_invariants
from .util import add_txn

DAY = dt.date.today() - dt.timedelta(days=2)

PAIRING = Seam("the pairing lock", rank=2)
SETTLE_ROWS = Seam("the settling rows' locks",
                   sql=r"WHERE id = ANY\(%s\)\s+ORDER BY id FOR UPDATE")
IDS = ("hold-1", "post-1", "dep-1")


def _setup(hh):
    add_txn(hh.conn, DAY, 80.0, "CITY CLINIC", account="chk", pending=1,
            primary="MEDICAL", txn_id="hold-1")
    add_txn(hh.conn, DAY, -50.0, "INSURANCE CO", account="chk",
            primary="INCOME", txn_id="dep-1")


def _flag(hh, ctx):
    return api_reimburse.reimburse_flag("hold-1", user=hh.user,
                                        body=Fields({}))


def _link(hh, ctx):
    return api_reimburse.reimburse_link(user=hh.user, body=Fields({
        "txn_id": "hold-1", "other_ids": ["dep-1"], "partial": True}))


def _settle(hh, ctx):
    conn = hh.connect()
    try:
        return base.upsert_transactions(conn, [base.Transaction(
            id="post-1", account_id="chk", date=DAY, amount=80.0,
            name="CITY CLINIC", category_primary="MEDICAL",
            raw={"pending_transaction_id": "hold-1"})])
    finally:
        conn.close()


def _digest(hh, ctx):
    c = hh.conn
    return (
        tuple((r["expense_id"], r["reimburse_id"], r["partial"],
               round(r["amount"] or 0, 2)) for r in c.execute(
            "SELECT expense_id, reimburse_id, partial, amount"
            "  FROM reimbursements ORDER BY 1, 2").fetchall()),
        tuple((r["txn_id"], r["partial"]) for r in c.execute(
            "SELECT txn_id, partial FROM reimburse_flags ORDER BY 1"
        ).fetchall()),
        tuple((r["id"], r["removed"], r["category_override"])
              for r in c.execute(
            "SELECT id, removed, category_override FROM transactions"
            " WHERE id = ANY(%s) ORDER BY id", (list(IDS),)).fetchall()))


class AFlagAPairingAndASettlement(ScenarioCase, unittest.TestCase):
    def test_a_flag_a_pairing_and_a_settlement_of_one_hold(self):
        self.check(Scenario(
            "a flag, a pairing and a settlement of one hold",
            setup=_setup,
            actors={"flag": _flag, "link": _link, "settle": _settle},
            seams=(PAIRING, SETTLE_ROWS),
            digest=_digest, invariants=lambda hh, ctx: money_invariants(hh.conn),
            doors=frozenset({"flag", "link"}),
            patches=lambda: mock.patch.object(activity, "record")))


if __name__ == "__main__":
    unittest.main()
