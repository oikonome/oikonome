"""Reimbursements net within both sides of every pair, for generated
households (specs/invariants-and-fuzzing.md, M13; households from
tests/money_households.py, seeds from tests/props.py).

A household gets a few deposits that pay charges back, then a seeded
sequence of what a person does on the Reimburse page: flag a charge as
awaiting (partial or not, with an expected amount that may be past the
charge), link a charge to a deposit (partial or full), link one deposit
to several charges at once, unlink, unflag; the bank feed withdraws a
deposit — one a full pair is on, too — and reports a withdrawn one again
under its id. After every step:

* no charge has more linked back to it than it cost — its net, read
  BEFORE the floor the money math applies, is between 0 and its amount;
* no deposit has more claimed from it than it brought in;
* every link carries a positive amount, and a flag's expected amount is
  never past its charge (such a flag is refused);
* what has come back against a live charge, full pairs included, is
  never past its cost, and a full pair never sits on a deposit the feed
  withdrew: its charge is spend again until something repays it.

At the end, the money a link takes out of spend is the money it takes
out of income — what was repaid is counted neither twice nor never —
within the dollar a full pair on a slightly short deposit is allowed;
and once every link and flag is undone, Cash Flow's figures are the ones
the household started with.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.engine import budget, reporting
from oikonome.sync import base
from oikonome.web import data

from . import props
from .money_households import SPEND_CATS, Household
from .util import TODAY, add_txn


def _cents(x) -> int:
    return int(round(float(x) * 100))


def in_and_out(conn) -> tuple[int, int]:
    """Cash Flow's In and Out over every year, in cents."""
    cf = reporting.compute_cashflow(conn, TODAY)
    # income month by month: a year's In is printed only while all of its
    # income is known, and withdrawing a month's only bank row unmakes that
    inc = sum(_cents(v) for _m, v in cf["income_by_month"])
    out = sum(_cents(y[2]) for y in cf["savings_by_year"])
    return inc, out


class ReimbursementsNetWithinBothSides(unittest.TestCase):

    def _check_caps(self, conn, where):
        over = conn.execute(
            """SELECT t.id, t.amount, SUM(r.amount) AS s
                 FROM transactions t JOIN reimbursements r
                   ON r.expense_id = t.id AND r.partial = 1
                 JOIN transactions d ON d.id = r.reimburse_id AND d.removed = 0
                GROUP BY t.id, t.amount
               HAVING SUM(r.amount) > t.amount + 0.005""").fetchall()
        self.assertEqual(over, [], f"a charge repaid past its cost {where}")
        over = conn.execute(
            """SELECT t.id, t.amount, SUM(r.amount) AS s
                 FROM transactions t JOIN reimbursements r
                   ON r.reimburse_id = t.id AND r.partial = 1
                 JOIN transactions e ON e.id = r.expense_id AND e.removed = 0
                GROUP BY t.id, t.amount
               HAVING SUM(r.amount) > -t.amount + 0.005""").fetchall()
        self.assertEqual(over, [], f"a deposit claimed past its size {where}")
        bad = conn.execute(
            "SELECT * FROM reimbursements WHERE partial = 1 "
            "AND NOT (amount > 0)").fetchall()
        self.assertEqual(bad, [], f"a link of no money {where}")
        over = conn.execute(
            f"""SELECT t.id FROM transactions t
                 WHERE t.removed = 0 AND t.amount > 0
                   AND {budget.REIMB_RECEIVED} > t.amount + 0.005""").fetchall()
        self.assertEqual(over, [], f"a charge repaid past its cost {where}")
        bad = conn.execute(
            """SELECT r.expense_id FROM reimbursements r
                 JOIN transactions t ON t.id = r.reimburse_id
                WHERE r.partial = 0 AND t.removed <> 0""").fetchall()
        self.assertEqual(bad, [], f"a full pair on a withdrawn deposit {where}")
        bad = conn.execute(
            """SELECT f.txn_id FROM reimburse_flags f
                 JOIN transactions t ON t.id = f.txn_id
                WHERE f.expected > t.amount + 0.005""").fetchall()
        self.assertEqual(bad, [], f"a flag expecting past its charge {where}")

    def test_links_never_net_past_either_side(self):
        self.made = {"partial": 0, "full": 0, "flag": 0,
                     "full_withdrawn": 0, "again": 0}
        for seed in props.seeds(8):
            with self.subTest(seed=seed):
                hh = Household(seed)
                try:
                    self._walk(hh)
                finally:
                    hh.close()
        # the walk reached both kinds of link and a flag
        self.assertTrue(all(self.made.values()), self.made)

    def _walk(self, hh):
        rng, c = hh.rng, hh.conn
        charges = [r["id"] for r in c.execute(
            "SELECT id FROM transactions WHERE amount > 0 "
            "AND category_primary = ANY(%s) ORDER BY id",
            (list(SPEND_CATS),)).fetchall()]
        rng.shuffle(charges)
        charges = charges[:6]
        if not charges:
            return
        deposits, made = [], {}
        for i in range(rng.randint(2, 4)):
            hh.n += 1
            day = hh.months[-1] + dt.timedelta(days=rng.randint(0, 13))
            amt = -round(rng.uniform(1, 300), 2)
            deposits.append(add_txn(
                c, day, amt, "CLAIM PAYMENT",
                primary="INCOME", account="chk", txn_id=f"dep{i}"))
            made[deposits[-1]] = (day, amt)
        start = in_and_out(c)
        for step in range(24):
            op = rng.choice(("flag", "link", "link", "many", "unlink",
                             "unflag", "withdraw", "withdraw", "again"))
            ch, dep = rng.choice(charges), rng.choice(deposits)
            amount = float(c.execute("SELECT amount FROM transactions "
                                     "WHERE id = %s", (ch,)).fetchone()["amount"])
            if op == "flag":
                expected = rng.choice((None, round(rng.uniform(0, amount * 1.5), 2)))
                data.flag_reimbursement(c, ch, partial=rng.random() < 0.5,
                                        expected=expected)
            elif op == "link":
                data.link_reimbursement(c, ch, dep, partial=rng.random() < 0.6)
            elif op == "many":
                data.link_reimbursements(
                    c, dep, rng.sample(charges, rng.randint(1, len(charges))),
                    partial=rng.random() < 0.5)
            elif op == "unlink":
                pair = c.execute("SELECT expense_id, reimburse_id FROM "
                                 "reimbursements ORDER BY 1, 2").fetchall()
                if pair:
                    p = rng.choice(pair)
                    data.unlink_reimbursement(c, p["expense_id"],
                                              p["reimburse_id"])
            elif op == "withdraw":
                # the bank feed takes a deposit back: its links then claim
                # nothing, and a full pair's charge is spend again
                if c.execute("SELECT 1 FROM reimbursements WHERE "
                             "reimburse_id = %s AND partial = 0",
                             (dep,)).fetchone():
                    self.made["full_withdrawn"] += 1
                base.mark_removed(c, [dep])
            elif op == "again":
                # the feed reports a withdrawn deposit again under its id
                if c.execute("SELECT 1 FROM transactions WHERE id = %s "
                             "AND removed = 1", (dep,)).fetchone():
                    self.made["again"] += 1
                    day, amt = made[dep]
                    base.upsert_transactions(c, [base.Transaction(
                        id=dep, account_id="chk", date=day, amount=amt,
                        name="CLAIM PAYMENT", category_primary="INCOME")])
            else:
                data.unflag_reimbursement(c, ch)
            self._check_caps(c, f"after step {step} ({op})")
            r = c.execute("""SELECT
                  (SELECT COUNT(*) FROM reimbursements WHERE partial = 1) p,
                  (SELECT COUNT(*) FROM reimbursements WHERE partial = 0) f,
                  (SELECT COUNT(*) FROM reimburse_flags) g""").fetchone()
            self.made["partial"] += r["p"]
            self.made["full"] += r["f"]
            self.made["flag"] += r["g"]
        now = in_and_out(c)
        gone = sum(_cents(-r["amount"]) for r in c.execute(
            "SELECT amount FROM transactions WHERE id = ANY(%s) "
            "AND removed = 1", (deposits,)).fetchall())
        fulls = c.execute("SELECT COUNT(*) AS n FROM reimbursements "
                          "WHERE partial = 0").fetchone()["n"]
        # a deposit the feed took back is no income at all, and its links
        # claim nothing
        d_in, d_out = start[0] - now[0] - gone, start[1] - now[1]
        self.assertGreaterEqual(d_out, 0)
        self.assertLessEqual(abs(d_in - d_out), 100 * fulls + 1,
                             (d_in, d_out, fulls))
        for p in c.execute("SELECT expense_id, reimburse_id FROM "
                           "reimbursements").fetchall():
            data.unlink_reimbursement(c, p["expense_id"], p["reimburse_id"])
        for ch in charges:
            data.unflag_reimbursement(c, ch)
        c.execute("DELETE FROM transactions WHERE id = ANY(%s) AND removed = 1",
                  (deposits,))
        self.assertEqual(in_and_out(c), (start[0] - gone, start[1]))


if __name__ == "__main__":
    unittest.main()
