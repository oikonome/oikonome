"""Goals that share one destination account split its rows once.

Each row on the account belongs to exactly one goal: the first goal, in the
household's own order, whose match words all appear in it; a row no goal's
words claim goes to the goal with no match words. So the goals' saved
figures on one account add up to what arrived there, a transfer whose text
carries two goals' words counts once, and the goal with no words is never
charged for a sibling's transfer twice. Every reader (the goal list, the
sweep line, the forecast's plan evidence, the pace) reads that partition.
"""

import datetime as dt
import random
import unittest

from oikonome.engine import savings

from .util import TODAY, add_txn, make_db


def _account(conn, acc):
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES (%s,'it1',%s,'depository','savings',0)"
        " ON CONFLICT (tenant_id, id) DO NOTHING", (acc, f"Savings {acc}"))


class OverlappingMatchWordsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        _account(self.conn, "sav")
        self.trip = {"name": "Trip", "account_id": "sav",
                     "tokens": ["getaway"], "monthly_plan": 100.0}
        self.rainy = {"name": "Rainy day", "account_id": "sav",
                      "tokens": ["reserve"], "monthly_plan": 100.0}
        self.home = {"name": "Home", "account_id": "sav",
                     "monthly_plan": 100.0}
        self.cfg = {"savings_goals": [self.trip, self.rainy, self.home]}
        d = TODAY - dt.timedelta(days=3)
        add_txn(self.conn, d, -400.0, "Online Transfer GETAWAY RESERVE",
                account="sav", primary="TRANSFER_IN")
        add_txn(self.conn, d, -250.0, "Transfer HOME", account="sav",
                primary="TRANSFER_IN")

    def tearDown(self):
        self.conn.close()

    def test_a_transfer_with_two_goals_words_counts_once(self):
        nets = [savings.matched_net(self.conn, self.cfg, g)
                for g in (self.trip, self.rainy, self.home)]
        self.assertEqual(nets, [400.0, 0.0, 250.0])
        self.assertEqual(round(sum(nets), 2), 650.0)

    def test_the_first_goal_in_the_households_order_takes_the_row(self):
        cfg = {"savings_goals": [self.rainy, self.trip, self.home]}
        self.assertEqual(savings.matched_net(self.conn, cfg, self.rainy),
                         400.0)
        self.assertEqual(savings.matched_net(self.conn, cfg, self.trip), 0.0)

    def test_the_goal_list_and_sweep_line_read_the_partition(self):
        by = {p["name"]: p for p in savings.progress(self.conn, self.cfg,
                                                     TODAY)}
        self.assertEqual(by["Trip"]["saved"], 400.0)
        self.assertEqual(by["Rainy day"]["saved"], 0.0)
        self.assertEqual(by["Home"]["saved"], 250.0)
        month = TODAY.replace(day=1)
        self.assertEqual(savings.posted_for_plan(
            self.conn, self.cfg, self.rainy, since=month), 0.0)
        self.assertEqual(savings.month_saved(
            self.conn, self.cfg, self.home, month, TODAY), 250.0)


class PartitionPropertyTests(unittest.TestCase):
    """Seeded goal sets over a seeded ledger: on every account the goals'
    figures add up to what arrived, no row counts twice, and the answer
    does not depend on the order the goals are read in."""

    WORDS = ["alpha", "bravo", "cobalt", "delta", "ember"]

    def test_goals_on_an_account_add_up_to_what_arrived(self):
        conn = make_db()
        try:
            rng = random.Random(7)
            accs = ["sa", "sb"]
            for a in accs:
                _account(conn, a)
            arrived = {a: 0.0 for a in accs}
            rows = {a: [] for a in accs}
            for i in range(40):
                a = rng.choice(accs)
                amt = -round(rng.randint(1, 900) + rng.randint(0, 99) / 100, 2)
                if rng.random() < 0.2:
                    amt = -amt                      # money back out
                text = " ".join(rng.sample(self.WORDS, rng.randint(0, 3)))
                add_txn(conn, TODAY - dt.timedelta(days=rng.randint(0, 80)),
                        amt, f"Transfer {i} {text}", account=a,
                        primary=rng.choice(["TRANSFER_IN", "TRANSFER_OUT"]))
                arrived[a] = round(arrived[a] - amt, 2)
                rows[a].append(text)
            for trial in range(12):
                gs = []
                for n in range(rng.randint(2, 5)):
                    toks = rng.sample(self.WORDS, rng.randint(0, 2))
                    gs.append({"name": f"G{trial}-{n}",
                               "account_id": rng.choice(accs),
                               "tokens": toks, "monthly_plan": 50.0})
                cfg = {"savings_goals": gs}
                nets = {g["name"]: savings.matched_net(conn, cfg, g)
                        for g in gs}
                again = {g["name"]: savings.matched_net(conn, cfg, g)
                         for g in reversed(gs)}
                self.assertEqual(nets, again, "read order moved a figure")
                for a in accs:
                    on = [g for g in gs if g["account_id"] == a]
                    if not on:
                        continue
                    total = round(sum(nets[g["name"]] for g in on), 2)
                    if any(not g["tokens"] for g in on):
                        self.assertEqual(total, arrived[a], (trial, a, on))
                    else:
                        self.assertLessEqual(
                            round(sum(abs(nets[g["name"]]) for g in on), 2),
                            round(sum(abs(float(r["amount"])) for r in
                                      conn.execute(
                                          "SELECT amount FROM transactions"
                                          " WHERE account_id=%s", (a,))), 2),
                            (trial, a, on))
                    # no row twice: each row's claimant set has at most one
                    claimed = {}
                    for g in on:
                        for rid in savings.goal_row_ids(conn, cfg, g):
                            self.assertNotIn(rid, claimed, (trial, g, rid))
                            claimed[rid] = g["name"]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
