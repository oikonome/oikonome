"""The breakdown lists add up in the whole dollars the pages print
(specs/counted-figures.md, "Lists add up in the dollars they show").

* Over generated households (tests/money_households.py): Net Worth's
  asset-class and institution rows, as shown, add up to the Financial
  accounts figure in whole dollars, and the drawn rows' shares to 100; on
  Accounts the sections' counted parts add up to the headline, the
  institutions to their section, the accounts to their institution.
* No shown row is more than a dollar off its own rounding.
* The fixed case: a household whose rows, rounded one by one, add up to a
  dollar more than the total printed above them.
* Neither client computes a share or rounds these rows itself: the web
  donut legend and the phone's lists print the server's fields.
"""

from __future__ import annotations

import math
import pathlib
import random
import re
import unittest

import oikonome
from oikonome.engine import links, reporting

from .money_households import Household
from .util import make_db, write_config

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"

SEEDS = tuple(range(1, 41))


def _pyround(x: float) -> int:
    """Whole dollars, halves to even: what the clients' money() prints."""
    return reporting.whole_dollars(x)


def _second_institution(hh: Household) -> None:
    """Some households keep their savings (and a second card) at another
    bank, so the institution list has several rows of both signs."""
    if hh.rng.random() < 0.6:
        hh.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('it2','test','Other Bank','tok-2')")
        hh.conn.execute("UPDATE accounts SET item_id = 'it2' "
                        "WHERE id IN ('sav', 'card2')")


class TheShownRowsAddUp(unittest.TestCase):

    def _households(self):
        for seed in SEEDS:
            hh = Household(seed, ledger=False)
            try:
                _second_institution(hh)
                yield seed, hh
            finally:
                hh.close()

    def test_net_worth_rows_and_shares(self):
        for seed, hh in self._households():
            with self.subTest(seed=seed):
                nw = reporting.compute_networth(hh.conn, trend=False)
                net = _pyround(nw["current_total"])
                for key in ("by_asset_class", "by_institution"):
                    shown = nw["rings"][key]["shown"]
                    rows = shown["rows"]
                    self.assertEqual([r["label"] for r in rows],
                                     [k for k, _ in nw[key]])
                    self.assertEqual(shown["net"], net)
                    self.assertEqual(sum(r["dollars"] for r in rows), net,
                                     (key, rows))
                    self.assertEqual(shown["owned"] + shown["owed"], net)
                    for r, (_, v) in zip(rows, nw[key]):
                        self.assertLessEqual(abs(r["dollars"] - v), 1, r)
                        self.assertEqual(r["owed"], v < 0)
                    shares = [r["share"] for r in rows
                              if r["share"] is not None]
                    if shares:
                        self.assertEqual(sum(shares), 100, rows)
                    self.assertEqual(
                        shown["owned"],
                        sum(r["dollars"] for r in rows
                            if r["share"] is not None))

    def test_accounts_sections_institutions_and_accounts(self):
        for seed, hh in self._households():
            with self.subTest(seed=seed):
                at = reporting.account_totals(hh.conn)
                cats = [c for pane in at["sections"].values()
                        for c in pane.values()]
                self.assertEqual(
                    sum(c["shown"]["counted_total"] for c in cats),
                    _pyround(at["financial_total"]), cats)
                acc = {r["id"]: r for r in hh.conn.execute(
                    "SELECT id, item_id, balance_current FROM accounts")}
                for c in cats:
                    sh = c["shown"]
                    self.assertEqual(sh["total"], _pyround(c["total"]))
                    self.assertEqual(sum(sh["by_item"].values()), sh["total"])
                    self.assertEqual(set(sh["by_item"]), set(c["by_item"]))
                    for item, d in sh["by_item"].items():
                        self.assertLessEqual(abs(d - c["by_item"][item]), 1)
                        self.assertEqual(
                            sum(v for a, v in sh["by_account"].items()
                                if acc[a]["item_id"] == item), d,
                            (item, sh))
                    for a, d in sh["by_account"].items():
                        self.assertLessEqual(
                            abs(d - float(acc[a]["balance_current"])), 1)


class ABackupReadsAsItsServingAccount(unittest.TestCase):
    """Checking and savings at one bank show $1,201 and $800 (adding up to
    the bank's $2,001), so the checking row is a dollar above its own
    rounding. A second login feeds a copy of the checking account, linked
    to it as its backup."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute("UPDATE accounts SET balance_current = 1200.45 "
                          "WHERE id = 'chk'")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',800.40)")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('it2','plan_csv','Test Bank Export','x')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('chk2','it2','Checking copy',"
            "'depository','checking',1200.45)")
        links.create(self.conn, ["chk", "chk2"])

    def tearDown(self):
        self.conn.close()

    def _totals(self):
        t = reporting.account_totals(self.conn)
        return t, t["sections"]["personal"]["Bank"]["shown"]

    def test_the_same_balance_prints_the_same_dollars(self):
        self.assertEqual(links.shadow_ids(self.conn), ["chk2"])
        t, shown = self._totals()
        self.assertEqual(shown["by_account"], {"chk": 1201, "sav": 800})
        # on its own the backup's balance rounds to $1,200
        self.assertEqual(_pyround(1200.45), 1200)
        self.assertEqual(t["backup_shown"], {"chk2": 1201})

    def test_a_backup_a_cent_apart_keeps_its_own_balance(self):
        self.conn.execute("UPDATE accounts SET balance_current = 1200.46 "
                          "WHERE id = 'chk2'")
        t, shown = self._totals()
        self.assertEqual(shown["by_account"]["chk"], 1201)
        self.assertEqual(t["backup_shown"], {})

    def test_a_backup_is_in_no_sum(self):
        t, shown = self._totals()
        self.assertNotIn("chk2", shown["by_account"])
        self.assertEqual(sum(shown["by_account"].values()), shown["total"])

    def test_the_door_sends_the_map(self):
        from oikonome.web.schemas.accounts import AccountTotals
        t, _ = self._totals()
        self.assertEqual(
            AccountTotals.model_validate(t).model_dump()["backup_shown"],
            {"chk2": 1201})


class TheFixedCase(unittest.TestCase):
    """Seed 7 holds investments, cash and card debt whose rows, each
    rounded on its own, add up to a dollar more than the total shown."""

    def test_seed_seven_before_and_after(self):
        hh = Household(7, ledger=False)
        try:
            nw = reporting.compute_networth(hh.conn, trend=False)
        finally:
            hh.close()
        rows = nw["by_asset_class"]
        self.assertEqual([k for k, _ in rows],
                         ["Taxable investments", "Cash", "Card debt"])
        # before: each row rounded on the device misses the total shown
        self.assertEqual(sum(_pyround(v) for _, v in rows), 11270)
        self.assertEqual(_pyround(nw["current_total"]), 11269)
        # after: the two largest remainders go up, the card debt (the
        # smallest) goes down, a dollar off its own rounding
        (_, inv), (_, cash), (_, card) = rows
        shown = nw["rings"]["by_asset_class"]["shown"]
        self.assertEqual([(r["label"], r["dollars"], r["share"], r["owed"])
                          for r in shown["rows"]],
                         [("Taxable investments", math.ceil(inv), 66, False),
                          ("Cash", math.ceil(cash), 34, False),
                          ("Card debt", math.floor(card), None, True)])
        self.assertEqual(math.floor(card), _pyround(card) - 1)
        self.assertEqual(sum(r["dollars"] for r in shown["rows"]), 11269)
        self.assertEqual(shown["net"], 11269)
        self.assertEqual(shown["owned"], math.ceil(inv) + math.ceil(cash))
        self.assertEqual(shown["owed"], math.floor(card))


class TheRoundingFunctions(unittest.TestCase):

    def test_generated_lists(self):
        for seed in range(500):
            rng = random.Random(seed)
            n = rng.randint(1, 9)
            cents = [rng.randint(-500_000, 9_000_000) for _ in range(n)]
            if rng.random() < 0.2:
                cents[0] = 0
            values = [c / 100 for c in cents]
            total = sum(cents) / 100
            with self.subTest(seed=seed):
                d = reporting.shown_dollars(values, total)
                self.assertEqual(sum(d), _pyround(total))
                for x, v in zip(d, values):
                    self.assertLessEqual(abs(x - v), 1)
                sh = reporting.shown_shares(values)
                self.assertTrue(all(p is None
                                    for p, c in zip(sh, cents) if c <= 0))
                whole = sum(c for c in cents if c > 0)
                drawn = [(p, c) for p, c in zip(sh, cents) if c > 0]
                if drawn:
                    self.assertEqual(sum(p for p, _ in drawn), 100)
                    for p, c in drawn:
                        self.assertLess(abs(p - 100 * c / whole), 1)

    def test_halves_go_to_even_as_the_clients_round(self):
        self.assertEqual([reporting.whole_dollars(v) for v in
                          (0.5, 1.5, 2.5, -0.5, -1.5, 0.49, -0.51)],
                         [0, 2, 2, 0, -2, 0, -1])

    def test_an_empty_list(self):
        self.assertEqual(reporting.shown_dollars([], 0), [])
        self.assertEqual(reporting.shown_shares([]), [])


class TheClientsComputeNoShare(unittest.TestCase):

    @staticmethod
    def _src(p: pathlib.Path) -> str:
        return p.read_text()

    def test_web_donut_legend_prints_the_servers_rows(self):
        src = self._src(_WEB / "components" / "Donut.tsx")
        self.assertIn("figures.shown", src)
        self.assertIn("row.share", src)
        self.assertIn("row.dollars", src)
        self.assertNotIn("pyround", src)
        # the ring's geometry divides by what it draws; the legend prints
        legend = src[src.index('<div className="legend">'):]
        self.assertNotIn("toFixed(", legend)
        self.assertNotIn("Math.round", legend)
        self.assertIsNone(re.search(r"100\s*\*|/\s*total", legend))

    def test_phone_lists_print_the_servers_rows(self):
        src = self._src(_MOB / "app" / "networth.tsx")
        self.assertNotIn("withPct", src)
        self.assertIn("ring?.shown", src)
        self.assertIn("r.share", src)
        body = src[src.index("function Breakdown("):]
        body = body[:body.index("\n}\n")]
        self.assertNotIn("Math.round", body)
        self.assertIsNone(re.search(r"100\s*\*", body))
        self.assertNotIn("reduce(", body)

    def test_accounts_lists_read_the_shown_dollars(self):
        web = self._src(_WEB / "pages" / "Accounts.tsx")
        # no figure from the server is no figure printed, never a $0
        self.assertIn("total: shown?.by_item[itemId],", web)
        # a linked backup's dollars are the server's too, beside the
        # counted accounts'
        self.assertIn(
            "shown: { ...totals?.backup_shown, ...shown?.by_account }", web)
        self.assertIn("?.shown.total;", web)
        self.assertNotIn("?.shown.total ?? 0", web)
        self.assertIn("shown={g.shown?.[a.id]}", web)
        mob = self._src(_MOB / "app" / "(tabs)" / "accounts.tsx")
        self.assertIn("sec?.shown?.total", mob)
        self.assertIn("sec.shown?.by_account", mob)
        self.assertIn("Object.entries(totals?.backup_shown ?? {})", mob)
        self.assertIn("shown={shownBalance.get(a.id)}", mob)


if __name__ == "__main__":
    unittest.main()
