"""The Today-family doors (Today, the widget glance, the integrations
summary and /metrics, the Bills page) read the same small facts many
times in one request: which copy of a linked account counts, a savings
goal's net, the closed months of an all-time cash-flow picture. Each is
read once per checkout and read again after a write to what it came
from — never served stale, never re-read for nothing. And the bill
matcher's shortcuts must pick exactly the charges the plain rule picks."""

import copy
import datetime as dt
import random
import unittest

from oikonome.db import tenancy
from oikonome.engine import budget, links, reporting, savings

from .util import add_bill, add_txn, make_db, write_config


class _Counting:
    """Counts the statements a pooled checkout runs whose text holds
    `needle`, while the block is open."""

    def __init__(self, needle: str):
        self.needle, self.n = needle, 0

    def __enter__(self):
        self._orig = tenancy._PooledConn.execute
        outer = self

        def execute(conn, *a, **kw):
            if outer.needle in str(a[0] if a else kw.get("query", "")):
                outer.n += 1
            return outer._orig(conn, *a, **kw)
        tenancy._PooledConn.execute = execute
        return self

    def __exit__(self, *exc):
        tenancy._PooledConn.execute = self._orig


class LinkGroupsReadOnceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_link_groups_are_read_once_until_a_link_is_written(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('sav','it1','Savings','depository','savings',100)")
        with _Counting("FROM account_links") as c:
            for _ in range(5):
                self.assertEqual(links.effective_account(self.conn, "sav"), "sav")
            self.assertEqual(c.n, 1)
            # a write to the link table is a different answer: read again
            self.conn.execute(
                "INSERT INTO account_links (group_id, account_id, home_rank) "
                "VALUES (gen_random_uuid(), 'sav', 0)")
            links.effective_account(self.conn, "sav")
            self.assertEqual(c.n, 2)


class GoalNetReadOnceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_goals_net_is_one_query_per_checkout_and_follows_a_new_transfer(self):
        g = {"name": "Trip", "account_id": "chk", "tokens": []}
        add_txn(self.conn, "2026-07-01", -200.0, "XFER IN", primary="TRANSFER_IN",
                account="chk")
        with _Counting("SUM(-t.amount)") as c:
            self.assertEqual(savings._matched_net(self.conn, g), 200.0)
            self.assertEqual(savings._matched_net(self.conn, g), 200.0)
            self.assertEqual(c.n, 1)
            add_txn(self.conn, "2026-07-02", -50.0, "XFER IN 2",
                    primary="TRANSFER_IN", account="chk")
            self.assertEqual(savings._matched_net(self.conn, g), 250.0)
            self.assertEqual(c.n, 2)

    def test_a_goal_accounts_owner_is_read_again_after_the_account_moves(self):
        g = {"name": "Trip", "account_id": "chk", "tokens": []}
        self.assertEqual(savings._owner_scope(self.conn, g)[1], [])
        ent = self.conn.execute(
            "INSERT INTO business_entity (name, structure) "
            "VALUES ('Side LLC', 'single_member_llc') RETURNING id"
        ).fetchone()["id"]
        self.conn.execute("UPDATE accounts SET entity_id=%s WHERE id='chk'",
                          (ent,))
        self.assertEqual(savings._owner_scope(self.conn, g)[1], [ent, ent])


class FlowAllTimeClosedRunsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        reporting._FIXED_MONTH_CACHE.clear()
        self.addCleanup(reporting._FIXED_MONTH_CACHE.clear)

    def tearDown(self):
        self.conn.close()

    def test_closed_runs_are_scanned_once_and_a_ledger_edit_rescans(self):
        today = dt.date(2026, 9, 1)
        add_txn(self.conn, "2026-06-05", -3000.0, "EMPLOYER", primary="INCOME",
                account="chk", detailed="INCOME_WAGES")
        add_txn(self.conn, "2026-06-12", 400.0, "GROCER",
                primary="FOOD_AND_DRINK", account="chk")
        first = reporting.flow_breakdown(self.conn, today, "all")
        with _Counting("NOT LIKE '%%acorns%%'") as c:
            again = reporting.flow_breakdown(self.conn, today, "all")
            self.assertEqual(c.n, 0)
        self.assertEqual(again, first)
        # a charge filed into a closed month moves the picture at once
        add_txn(self.conn, "2026-07-20", 100.0, "HARDWARE",
                primary="GENERAL_MERCHANDISE", account="chk")
        moved = reporting.flow_breakdown(self.conn, today, "all")
        self.assertEqual(moved["out"]["total"], first["out"]["total"] + 100.0)
        self.assertEqual(moved["in"], first["in"])


def _reference_match(rows, occurrences):
    """match_occurrences without its shortcuts: every row against every
    bill's amount test, every row tokenised, every occurrence scanned."""
    cands = budget.bill_candidates(rows)
    candidates = []
    distinct = {}
    for occ in occurrences:
        distinct.setdefault(id(occ["bill"]), occ["bill"])
    for ridx, (r, _) in enumerate(cands):
        d = budget.as_date(r["date"])
        near = [(bid, b) for bid, b in distinct.items()
                if budget.amount_fits(budget._bands_of(b), r["amount"], d)]
        if not near:
            continue
        text = f"{r['payee'] or ''} {r['name'] or ''}".lower()
        toks = budget._tokens(text)
        fits = {bid: budget._bill_matches(b, toks, text, budget.row_merchant_id(r))
                for bid, b in near}
        for oidx, occ in enumerate(occurrences):
            if not fits.get(id(occ["bill"])):
                continue
            gap = abs((d - occ["due"]).days)
            if gap > occ["window"]:
                continue
            candidates.append((gap, budget.amount_gap(budget._bands_of(occ["bill"]),
                                                      r["amount"]), ridx, oidx))
    candidates.sort()
    taken, used, matched = set(), set(), set()
    for _, _, ridx, oidx in candidates:
        if ridx in taken or oidx in used:
            continue
        taken.add(ridx)
        used.add(oidx)
        occurrences[oidx]["txn"] = cands[ridx][0]
        matched.update(cands[ridx][1])
    return matched, occurrences


_WORDS = ["power", "water", "gym", "stream", "phone", "grocer", "fuel"]


def _random_bill(rng, i):
    """One bill of any shape the matcher is handed. Each family reaches a
    different branch the shortcuts skip around: an identity (`ids`, empty
    included) is decided inline by merchant id; phrase and `matcher` bills
    and plain word bills go through the text test; a bill without bands
    falls back to its amount; overlapping prices ($50 beside $52, a $0
    bill) make one charge fit several bills so the tie-break order is
    exercised."""
    amt = round(rng.choice([rng.uniform(5, 300), rng.uniform(5, 300),
                            50.0, 52.0, 9.99, 0.0]), 2)
    w = rng.choice(_WORDS)
    bill = {"payee": f"{w} {i}", "amount": amt, "companions": []}
    if rng.random() < 0.7:
        bands = [(amt, round(amt * rng.choice([0.0, 0.05, 0.2]), 2), None)]
        if rng.random() < 0.35:     # an older price, good until a date
            bands.append((round(amt * rng.choice([0.8, 1.3]), 2),
                          rng.choice([0.0, 1.0, 5.0]),
                          dt.date(2026, rng.randint(1, 6), 1)))
        bill["bands"] = bands
    kind = rng.random()
    if kind < 0.35:
        bill["ids"] = (frozenset({f"m{rng.randint(0, 4)}",
                                  f"m{rng.randint(0, 4)}"})
                       if rng.random() < 0.9 else frozenset())
        bill["kind"] = "ids"
    elif kind < 0.55:
        bill.update(ids=None, merchant_phrase=f" {w} ", kind="phrase")
    elif kind < 0.65:
        bill.update(ids=None, kind="matcher",
                    matcher=(lambda w: lambda text, toks: w in text)(w))
    else:
        bill.update(ids=None, merchant_tokens={w}, key=w, kind="words")
    return bill


def _random_rows(rng, bills):
    """Charges near some bill's price (and some well off it), with the
    shapes a ledger carries: refunds, missing amounts, store-receipt parts
    sharing one charge's id, merchant ids on rows a text bill must still
    judge by words, and an integer id the matcher must read as text."""
    rows = []
    for j in range(rng.randint(0, 150)):
        b = rng.choice(bills)
        ref = rng.choice(budget._bands_of(b))[0]
        amt = round(ref + rng.choice([0, 0, 0.01, 0.5, 3, 20, 40, 100])
                    * rng.choice([1, -1]), 2)
        if rng.random() < 0.15:
            amt = -amt
        src = rng.choice([None, None, None, "user", "amazon"])
        if rng.random() < 0.05:
            # a receipt part sums into its charge, so it carries 0 instead
            amt = 0.0 if src == "amazon" else None
        tid = (f"r{j}" if src != "amazon" or rng.random() < 0.5
               else f"r{max(0, j - 1)}")
        rows.append({
            "txn_id": tid,
            "date": dt.date(2026, rng.randint(1, 7), rng.randint(1, 28)),
            "amount": amt,
            "payee": " ".join(rng.sample(_WORDS, rng.randint(0, 2))).upper()
                     or None,
            "name": rng.choice(["ref", None, "GYM POWER"]),
            "merchant_id": rng.choice([None, "m0", "m1", "m2", "m3", "m4", 3]),
            "split_source": src, "split_line": None})
    return rows


class BillMatcherShortcutsTests(unittest.TestCase):
    def test_the_shortcuts_pick_exactly_what_the_plain_rule_picks(self):
        rng = random.Random(7)
        paid_kinds = set()
        for trial in range(1000):
            bills = [_random_bill(rng, i) for i in range(rng.randint(1, 10))]
            if trial % 2:
                # the widened bands bill_paid_rows matches budget rows with
                bills = [dict(b, bands=budget._drift_bands(b)) for b in bills]
            occs = [{"bill": b, "due": dt.date(2026, m, rng.randint(1, 28)),
                     "window": rng.choice([0, 3, 10, 35]), "txn": None,
                     "companions": []}
                    for b in bills for m in range(1, 8)]
            rows = _random_rows(rng, bills)
            got_m, got_o = budget.match_occurrences(rows, copy.deepcopy(occs))
            want_m, want_o = _reference_match(rows, copy.deepcopy(occs))
            self.assertEqual(got_m, want_m, trial)
            self.assertEqual([o["txn"] and o["txn"]["txn_id"] for o in got_o],
                             [o["txn"] and o["txn"]["txn_id"] for o in want_o],
                             trial)
            paid_kinds.update(o["bill"]["kind"] for o in want_o if o["txn"])
        # a generator that stopped producing a family would pass vacuously
        self.assertEqual(paid_kinds, {"ids", "phrase", "matcher", "words"})


class AnnualEnvelopeByMerchantTests(unittest.TestCase):
    """The year-to-date pass behind an annual pool skips the rows no annual
    envelope could claim before reading their words; what it counts must
    stay exactly that envelope's own earlier-month charges."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _merchant(self, name):
        return str(self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES (%s, 'manual') "
            "RETURNING id", (name,)).fetchone()["id"])

    def _charge(self, day, amount, name, mid):
        tid = add_txn(self.conn, day, amount, name, account="card")
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (mid, tid))

    def test_the_pool_counts_its_merchants_earlier_months_and_nothing_else(self):
        host = self._merchant("Namehost")
        shop = self._merchant("Bookshop")
        add_bill(self.conn, "Namehost", 120.0, bill_type="envelope",
                 frequency=None, interval=12, merchants=["Namehost"])
        add_bill(self.conn, "Bookshop", 50.0, bill_type="envelope",
                 frequency=None, interval=1, merchants=["Bookshop"])
        self._charge("2026-02-03", 60.0, "NAMEHOST RENEW", host)
        self._charge("2026-03-03", 30.0, "NAMEHOST DNS", host)
        # another merchant's charge with the pool's words in it: the pool
        # has an identity, so the words decide nothing
        self._charge("2026-03-04", 75.0, "NAMEHOST GIFT VIA BOOKSHOP", shop)
        add_txn(self.conn, "2026-04-04", 40.0, "NAMEHOST", account="card")
        # this month is not the prior pool
        self._charge("2026-07-02", 12.0, "NAMEHOST", host)
        envs = budget._envelope_bills(self.conn)
        self.assertEqual({e["payee"]: e["period_months"] for e in envs},
                         {"Namehost": 12, "Bookshop": 1})
        self.assertTrue(all(e["ids"] for e in envs))
        used = budget._envelope_prior_used(self.conn, envs, dt.date(2026, 7, 15))
        self.assertEqual(used, {"Namehost": 90.0})


if __name__ == "__main__":
    unittest.main()
