"""A bill approved while detection runs ends as the two run one after the
other, and detection runs once at a time (specs/invariants-and-fuzzing.md,
section 3, sixth scenario; L5).

Approving an offer creates the bill and stamps the charges it claims;
the detection pass reads every bill, withdraws offers a bill covers and
offers new ones. Run at once they must leave one bill for the series and
no fresh offer to add it again, as either serial order does. Two
detection passes asked for at once run one at a time: the second caller
of the door skips while the first runs.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.engine import bills

from .concurrency import ScenarioCase, Scenario, Seam
from .util import TODAY, add_txn

PAYEE = "HARBOR LIGHTS CLUB DUES"
CLAIMS = Seam("the bill-claim lock", rank=0)
CEILING = Seam("the bill-ceiling lock", rank=4)
DETECT = Seam("the detection lock",
              sql=r"pg_try_advisory_lock\(hashtext|pg_advisory_lock\(hashtext")


def _setup(hh):
    for i in range(6):
        m = TODAY.month - 1 - i
        day = dt.date(TODAY.year + m // 12, m % 12 + 1, 8)
        add_txn(hh.conn, day, 42.0, PAYEE, primary="GENERAL_SERVICES",
                account="chk", txn_id=f"dues-{i}")
    bills.run(hh.conn, TODAY)
    pid = hh.conn.execute(
        "SELECT id FROM bill_proposals WHERE kind = 'add' AND payee = %s"
        " AND status = 'pending'", (PAYEE,)).fetchone()["id"]
    return {"pid": pid}


def _approve(hh, ctx):
    conn = hh.connect()
    try:
        return bills.apply_proposal(conn, ctx["pid"], "approve")
    finally:
        conn.close()


def _detect(wait):
    def run(hh, ctx):
        conn = hh.connect()
        try:
            return bills.run_single_flight(conn, TODAY, wait=wait)
        finally:
            conn.close()
    return run


def _digest(hh, ctx):
    c = hh.conn
    return (
        tuple((r["payee"], round(r["amount"] or 0, 2), r["active"],
               r["frequency"]) for r in c.execute(
            "SELECT payee, amount, active, frequency FROM bills"
            " ORDER BY payee").fetchall()),
        tuple((r["id"], r["kind"], r["status"]) for r in c.execute(
            "SELECT id, kind, status FROM bill_proposals ORDER BY id"
        ).fetchall()),
        tuple((r["id"], r["category_override"]) for r in c.execute(
            "SELECT id, category_override FROM transactions ORDER BY id"
        ).fetchall()))


def _one_bill_per_series(hh, ctx) -> list[str]:
    """The plan counts the series once: one active bill for it, and no
    pending offer to add what a bill already covers."""
    c = hh.conn
    n = c.execute("SELECT count(*) AS n FROM bills WHERE active = 1"
                  ).fetchone()["n"]
    out = [] if n <= 1 else [f"{n} active bills for one series"]
    if n and c.execute("SELECT 1 FROM bill_proposals WHERE kind = 'add'"
                       " AND status = 'pending'").fetchone():
        out.append("an offer to add a series a bill already covers")
    return out


class ABillApprovalBesideDetection(ScenarioCase, unittest.TestCase):
    def test_an_approval_beside_the_nightly_detection(self):
        self.check(Scenario(
            "an approval beside the nightly detection",
            setup=_setup,
            actors={"approve": _approve, "detect": _detect(True)},
            seams=(CLAIMS, CEILING, DETECT), digest=_digest,
            invariants=_one_bill_per_series,
            refused=lambda n, r: isinstance(r, dict) and "error" in r))

    def test_two_detections_asked_for_at_once_run_one_at_a_time(self):
        def charges_only(hh):
            ctx = _setup(hh)
            # nothing offered yet: the pass that runs has an offer to make
            hh.conn.execute("DELETE FROM bill_proposals")
            return ctx

        self.check(Scenario(
            "two detections at once",
            setup=charges_only,
            actors={"A": _detect(False), "B": _detect(False)},
            seams=(DETECT,), digest=_digest, single_flight=True,
            refused=lambda n, r: r is None))


if __name__ == "__main__":
    unittest.main()
