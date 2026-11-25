"""A deposit the feed reports again and a pair repair, at once, end as the
two run in some order (specs/counted-figures.md, "A withdrawn deposit
repays nothing").

A returning deposit's links take only the room its charge has left: what
repaid the charge while it was gone keeps its claim. The repair cannot
tell which link came back, so it cuts the newest. So the row coming back
and the trim of its links land in one commit under the pairing lock; a
repair parked between them would otherwise cut the household's newer
link, the one that repaid the charge meanwhile.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.sync import base
from oikonome.web import data as d

from .concurrency import ScenarioCase, Scenario, Seam, money_invariants
from .util import add_txn

DAY = dt.date.today() - dt.timedelta(days=2)

PAIRING = Seam("the pairing lock", rank=2)
WRITTEN = Seam("the returning row written",
               func=(base, "_write_rows"), when="after")
IDS = ("chg-1", "dep-a", "dep-b")


def _setup(hh):
    add_txn(hh.conn, DAY, 60.0, "GARDEN CENTER", txn_id="chg-1")
    add_txn(hh.conn, DAY, -50.0, "CLAIM PAYMENT", account="chk",
            primary="INCOME", txn_id="dep-a")
    d.link_reimbursement(hh.conn, "chg-1", "dep-a", partial=True)
    base.mark_removed(hh.conn, ["dep-a"])
    # repaid by hand while the first deposit was gone: the newer link
    add_txn(hh.conn, DAY, -60.0, "REFUND", account="chk",
            primary="INCOME", txn_id="dep-b")
    d.link_reimbursement(hh.conn, "chg-1", "dep-b", partial=True)


def _report_again(hh, ctx):
    conn = hh.connect()
    try:
        return base.upsert_transactions(conn, [base.Transaction(
            id="dep-a", account_id="chk", date=DAY, amount=-50.0,
            name="CLAIM PAYMENT", category_primary="INCOME")])
    finally:
        conn.close()


def _repair(hh, ctx):
    conn = hh.connect()
    try:
        return d.repair_pairs(conn)
    finally:
        conn.close()


def _digest(hh, ctx):
    c = hh.conn
    return (
        tuple((r["expense_id"], r["reimburse_id"], r["partial"],
               round(r["amount"] or 0, 2)) for r in c.execute(
            "SELECT expense_id, reimburse_id, partial, amount"
            "  FROM reimbursements ORDER BY 1, 2").fetchall()),
        tuple((r["id"], r["removed"], r["category_override"])
              for r in c.execute(
            "SELECT id, removed, category_override FROM transactions"
            " WHERE id = ANY(%s) ORDER BY id", (list(IDS),)).fetchall()))


class AReturningDepositAndAPairRepair(ScenarioCase, unittest.TestCase):
    def test_a_repair_beside_a_returning_deposit_keeps_the_newer_link(self):
        self.check(Scenario(
            "a deposit reported again and a pair repair",
            setup=_setup,
            actors={"report": _report_again, "repair": _repair},
            seams=(PAIRING, WRITTEN),
            digest=_digest,
            invariants=lambda hh, ctx: money_invariants(hh.conn)))


if __name__ == "__main__":
    unittest.main()
