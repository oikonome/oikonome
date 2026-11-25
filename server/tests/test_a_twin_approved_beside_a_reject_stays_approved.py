"""An offer approved while a twin of it is rejected stays approved
(specs/invariants-and-fuzzing.md, section 3).

The list shows one offer per series, so rejecting it answers the series:
its twins at other amounts are rejected with it. A stale tab or a mail
button can still approve a twin the page no longer lists, at the same
moment. The two must end as some serial order does: either the twin is
approved (and its bill made) and the rest rejected, or everything is
rejected and the approval refused. A reject that overwrote the twin's
answer after its approval committed would leave a bill whose offer reads
"rejected" — a recorded decision rewritten.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.engine import bills

from .concurrency import ScenarioCase, Scenario, Seam
from .util import TODAY

PAYEE = "Lantern Cleaners"
APPROVED = Seam("the twin's approval written",
                sql=r"UPDATE bill_proposals SET status='approved'")
CLAIMS = Seam("the bill-claim lock", rank=0)


def _setup(hh):
    for pid, amount in (("prop:twin-w", 15.0), ("prop:twin-t", 18.0)):
        bills._insert_proposal(
            hh.conn, pid=pid, kind="add", payee=PAYEE, amount=amount,
            frequency="MONTHLY", next_due=TODAY + dt.timedelta(days=5),
            evidence={"hits": 4, "cycle_days": 30})
    hh.conn.commit()
    return {}


def _act(pid, action):
    def run(hh, ctx):
        conn = hh.connect()
        try:
            return bills.apply_proposal(conn, pid, action)
        finally:
            conn.close()
    return run


def _digest(hh, ctx):
    c = hh.conn
    return (
        tuple((r["payee"], r["active"]) for r in c.execute(
            "SELECT payee, active FROM bills ORDER BY payee").fetchall()),
        tuple((r["id"], r["status"]) for r in c.execute(
            "SELECT id, status FROM bill_proposals ORDER BY id").fetchall()))


def _an_answer_is_never_rewritten(hh, ctx) -> list[str]:
    """A bill made from an offer keeps that offer's answer 'approved'."""
    c = hh.conn
    made = c.execute("SELECT 1 FROM bills WHERE active = 1").fetchone()
    st = c.execute("SELECT status FROM bill_proposals WHERE id = %s",
                   ("prop:twin-t",)).fetchone()["status"]
    return (["the approved twin's offer reads " + st]
            if made and st != "approved" else [])


class ARejectBesideATwinsApproval(ScenarioCase, unittest.TestCase):
    def test_a_reject_beside_a_twins_approval(self):
        self.check(Scenario(
            "a reject beside a twin's approval",
            setup=_setup,
            actors={"approve": _act("prop:twin-t", "approve"),
                    "reject": _act("prop:twin-w", "reject")},
            seams=(APPROVED, CLAIMS), digest=_digest,
            invariants=_an_answer_is_never_rewritten,
            refused=lambda n, r: isinstance(r, dict) and "error" in r))


if __name__ == "__main__":
    unittest.main()
