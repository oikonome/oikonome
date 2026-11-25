"""Retirement saving is projected in the tax bucket it is saved into.

What breaks if these fail: a household saving into a Roth has that money
taxed as ordinary income at withdrawal and forced out by RMDs, so its
plan reads worse than it is; two plans with the same total saving and a
different split share one cached answer; an older client's Recalculate
(it knows no Roth box) wipes the Roth figure and the numbers jump; the
parts the page lists do not add up to the saving it says it counts
(specs/retirement-figures.md, "Saving is split by tax treatment").
"""

import datetime as dt
import hashlib
import json
import unittest

from oikonome.engine import retirement as retire
from oikonome.sync import restore

from .test_retirement_saving_is_new_money_by_tax_bucket import _Household
from .util import add_txn, write_config

Q = "/api/retirement?age=50&spend=60000&end=90"


def _blank(**kw):
    b = {"cash": 0.0, "taxable": 0.0, "td": 0.0, "roth": 0.0}
    b.update(kw)
    return b


class TheEngineFillsTheRothBucket(unittest.TestCase):
    ARGS = dict(rr=0.04, ss_annual=0.0, ss_age=67, rental_annual=0.0,
                taxable_annual=0.0, resume_age=40, eff_cg=0.18,
                gain_frac=0.5)

    def test_roth_saving_without_tax_matches_tax_deferred_saving(self):
        # before any RMD and with no ordinary-income tax the two buckets
        # are the same money
        b = _blank(cash=20000.0)
        for spend in (30000.0, 60000.0):
            with self.subTest(spend=spend):
                td = retire.simulate(b, 40, 60, 74, spend, td_annual=9000.0,
                                     eff_ord=0.0, **self.ARGS)
                ro = retire.simulate(b, 40, 60, 74, spend, td_annual=0.0,
                                     roth_annual=9000.0, eff_ord=0.0,
                                     **self.ARGS)
                self.assertEqual(td, ro)

    def test_the_roth_balance_grows_with_roth_saving(self):
        path = retire._growth_path(_blank(), 40, 50, 0.04, 0.0, 0.0, 40,
                                   roth_annual=6000.0)
        self.assertGreater(path[-1][1], 60000.0)
        flat = retire._growth_path(_blank(), 40, 50, 0.04, 0.0, 0.0, 40)
        self.assertEqual(flat[-1][1], 0.0)


def _buckets(cash, taxable, td, roth):
    d = {"cash": cash, "taxable": taxable, "td": td, "roth": roth,
         "debt": 0.0, "debt_apr": None, "debt_residual": 0.0,
         "total": cash + taxable + td + roth}
    for k in ("cash", "taxable", "td", "roth"):
        d[f"cards_from_{k}"] = 0.0
    return tuple(sorted(d.items()))


class NoRothSavingMovesNoFigure(unittest.TestCase):
    """Plans with no Roth saving: the whole projection is the one the
    engine produced before Roth saving had a bucket of its own. The digests
    were taken from that engine on these exact keys; a deliberate change
    to the projection re-takes them."""

    # (buckets, age, end, spend, ss/mo, ss age, td/yr, taxable/yr,
    #  resume, stock share) -> digest of the whole cached projection
    PLANS = [
        ((_buckets(20000, 50000, 180000, 30000), 50, 90, 60000,
          2100.0, 67, 10200, 0, 50, 0.9), "e27feee11dd3491e"),
        ((_buckets(5000, 0, 40000, 0), 35, 95, 48000, 0.0, 67,
          6000, 2400, 35, 0.7), "73fd8005473e16c6"),
        ((_buckets(80000, 300000, 600000, 90000), 58, 92, 90000,
          2800.0, 70, 0, 0, 58, 0.6), "a5237d18d4c350ce"),
        ((_buckets(1000, 0, 0, 0), 44, 90, 52000, 1500.0, 62,
          14400, 0, 44, 0.9), "d321eb58037a6446"),
    ]

    def test_the_projection_is_byte_identical(self):
        for (b, age, end, spend, ss, ssage, td, tx, res, stock), want in \
                self.PLANS:
            with self.subTest(age=age, spend=spend):
                key = (b, age, end, spend, 0.07, 0.03, ss, ssage, 0, td, 0,
                       tx, res, 0.25, 0.18, 0.5, 72, stock, 0.0, 75,
                       dt.date(2026, 11, 1))
                got = hashlib.sha256(json.dumps(
                    retire._project_cached(key),
                    sort_keys=True).encode()).hexdigest()[:16]
                self.assertEqual(got, want)


class SavingPlan(unittest.TestCase):
    """The one decider of the split."""

    def test_the_ledger_split_sums_to_the_total_it_shows(self):
        found = {"monthly": 660.0, "td_monthly": 330.0,
                 "roth_monthly": 330.0, "months": 12.0}
        p = retire.saving_plan({}, {}, found)
        self.assertEqual(p["basis"], "ledger")
        self.assertEqual(p["td_mo"] + p["roth_mo"] + p["taxable_mo"],
                         int(round(660.0 / 50) * 50))
        self.assertEqual(sum(x["monthly"] for x in p["parts"]),
                         p["td_mo"] + p["roth_mo"] + p["taxable_mo"])
        self.assertGreater(p["roth_mo"], 0)

    def test_a_saved_answer_with_no_roth_figure_counts_no_roth(self):
        found = {"monthly": 800.0, "td_monthly": 0.0,
                 "roth_monthly": 800.0, "months": 12.0}
        p = retire.saving_plan({}, {"employer_mo": 300}, found)
        self.assertEqual((p["td_mo"], p["roth_mo"], p["taxable_mo"]),
                         (300, 0, 0))
        self.assertEqual(p["basis"], "saved")
        # the boxes' seeds split the saved total by the ledger, capped at it
        self.assertEqual((p["seed_td_mo"], p["seed_roth_mo"]), (0, 300))
        self.assertEqual(p["hint"],
                         "Your saved $300/mo includes about $300/mo that "
                         "your last 12 months show going into Roth "
                         "accounts — save it to count it as Roth.")
        # answered, even with $0: no hint
        p = retire.saving_plan({}, {"employer_mo": 300, "roth_mo": 0}, found)
        self.assertIsNone(p["hint"])

    def test_a_what_if_replaces_only_the_field_it_names(self):
        p = retire.saving_plan({"employer_mo": 650, "taxable_mo": 100},
                               {"employer_mo": 300, "roth_mo": 400}, None)
        self.assertEqual((p["td_mo"], p["roth_mo"], p["taxable_mo"]),
                         (650, 400, 100))
        self.assertEqual(p["basis"], "what_if")

    def test_the_parts_are_worded_by_the_server(self):
        p = retire.saving_plan({}, {"employer_mo": 1250, "roth_mo": 500,
                                    "taxable_mo": 0}, None)
        self.assertEqual([x["text"] for x in p["parts"]], [
            "$1,250/mo into tax-deferred accounts — taxed when you take it out",
            "$500/mo into Roth accounts — not taxed when you take it out"])


class TheLedgerSplitsByAccount(_Household):
    def setUp(self):
        super().setUp()
        add_txn(self.conn, dt.date.today() - dt.timedelta(days=500), 10.0,
                "Fee", account="ira", primary="TRANSFER_OUT",
                detailed="TRANSFER_OUT_ACCOUNT_TRANSFER")

    def deposits(self, acct, amount=600.0):
        for m in range(12):
            self.move(dt.date.today() - dt.timedelta(days=20 + 30 * m),
                      amount, "chk", acct, detailed="TRANSFER_IN_DEPOSIT")

    def reply(self, q=""):
        r = self.client.get(Q + q)
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.json()

    def test_a_roth_only_saver_is_projected_better_than_tax_deferred(self):
        self.deposits("ira")
        trad = self.reply()
        self.conn.execute("DELETE FROM transactions WHERE amount < 0 "
                          "AND account_id = 'ira'")
        self.deposits("rira")
        roth = self.reply()
        self.assertEqual(roth["inputs"]["td_mo"], 0)
        # what an older client has always been given: the saving that is
        # not taxable-account saving
        self.assertEqual(roth["inputs"]["employer_mo"], 600)
        self.assertEqual(roth["inputs"]["roth_mo"], 600)
        self.assertEqual(trad["inputs"]["roth_mo"], 0)
        self.assertEqual(roth["inputs"]["saving_mo"],
                         trad["inputs"]["saving_mo"])
        for a, b in zip(roth["rows"], trad["rows"]):
            self.assertGreaterEqual(a["max_spend"], b["max_spend"])
        self.assertGreater(roth["rows"][-1]["max_spend"],
                           trad["rows"][-1]["max_spend"])
        self.assertEqual([p["kind"] for p in roth["inputs"]["saving_parts"]],
                         ["roth"])

    def test_two_splits_of_one_total_are_two_answers(self):
        a = self.reply("&employer_mo=1000&roth_mo=0")
        b = self.reply("&employer_mo=0&roth_mo=1000")
        self.assertEqual(a["inputs"]["saving_mo"], b["inputs"]["saving_mo"])
        self.assertNotEqual([r["max_spend"] for r in a["rows"]],
                            [r["max_spend"] for r in b["rows"]])

    def test_an_older_clients_what_if_keeps_the_roth_figure(self):
        write_config(self.conn, birthdate="1980-03-01", ret_wizard_answers={
            "employer_mo": 500, "roth_mo": 400, "taxable_mo": 0,
            "answers_v2": ["employer_mo", "roth_mo", "taxable_mo"]})
        got = self.reply("&employer_mo=650&taxable_mo=100")["inputs"]
        self.assertEqual((got["employer_mo"], got["roth_mo"],
                          got["taxable_mo"]), (650, 400, 100))
        self.assertEqual(got["saving_mo"], 1150)
        self.assertEqual(sum(p["monthly"] for p in got["saving_parts"]),
                         got["saving_mo"])
        # and on the ledger's base
        write_config(self.conn, birthdate="1980-03-01",
                     ret_wizard_answers=None)
        self.deposits("rira", 300.0)
        # an older client's employer figure there is tax-deferred + the
        # ledger's Roth: its untouched Recalculate moves nothing, and a
        # smaller figure is that whole saving
        base = self.reply()
        same = self.reply("&employer_mo=300&taxable_mo=0")
        self.assertEqual(same["rows"], base["rows"])
        self.assertEqual((same["inputs"]["td_mo"], same["inputs"]["roth_mo"],
                          same["inputs"]["employer_mo"]), (0, 300, 300))
        got = self.reply("&employer_mo=200&taxable_mo=0")["inputs"]
        self.assertEqual((got["td_mo"], got["roth_mo"], got["saving_mo"]),
                         (0, 200, 200))

    def test_a_lone_taxable_what_if_keeps_the_ledger_figure(self):
        self.deposits("ira", 400.0)
        got = self.reply("&taxable_mo=100")["inputs"]
        self.assertEqual((got["td_mo"], got["taxable_mo"]), (400, 100))

    def test_a_saved_answer_without_roth_is_offered_the_ledgers(self):
        self.deposits("rira", 350.0)
        write_config(self.conn, birthdate="1980-03-01", ret_wizard_answers={
            "employer_mo": 500, "answers_v2": ["employer_mo"]})
        got = self.reply()["inputs"]
        # the projection keeps the saved total as tax-deferred; the boxes
        # are seeded with its split, summing to that total
        self.assertEqual((got["employer_mo"], got["td_mo"], got["roth_mo"],
                          got["saving_mo"]), (500, 150, 350, 500))
        self.assertEqual([(p["kind"], p["monthly"])
                          for p in got["saving_parts"]], [("td", 500)])
        self.assertIn("$350/mo", got["saving_hint"])


class AnOlderClientsWalkthroughMovesNoFigure(_Household):
    """A household whose saving comes from its ledger, part of it into a
    Roth, walked through by a client that knows no Roth box."""

    def setUp(self):
        super().setUp()
        add_txn(self.conn, dt.date.today() - dt.timedelta(days=500), 10.0,
                "Fee", account="ira", primary="TRANSFER_OUT",
                detailed="TRANSFER_OUT_ACCOUNT_TRANSFER")
        for m in range(12):
            d = dt.date.today() - dt.timedelta(days=20 + 30 * m)
            self.move(d, 450.0, "chk", "ira", detailed="TRANSFER_IN_DEPOSIT")
            self.move(d, 250.0, "chk", "rira", detailed="TRANSFER_IN_DEPOSIT")

    def inputs(self):
        return self.client.get(Q).json()["inputs"]

    def test_an_old_shape_save_of_the_prefilled_figure_keeps_the_total(self):
        before = self.inputs()
        self.assertEqual((before["employer_mo"], before["roth_mo"]),
                         (700, 250))
        # the old walkthrough confirms its prefilled employer box
        r = self.client.post("/api/settings", json={
            "ret_wizard_steps": {"saving": "done"}, "ret_answers_v": 2,
            "ret_wizard_answers": {"employer_mo": before["employer_mo"],
                                   "taxable_mo": before["taxable_mo"]}})
        self.assertEqual(r.status_code, 200, r.text[:300])
        after = self.inputs()
        self.assertEqual(after["saving_mo"], before["saving_mo"])

    def test_a_new_clients_save_stores_the_split(self):
        before = self.inputs()
        r = self.client.post("/api/settings", json={
            "ret_wizard_steps": {"saving": "done"}, "ret_answers_v": 2,
            "ret_wizard_answers": {"employer_mo": before["td_mo"],
                                   "taxable_mo": 0},
            "ret_wizard_roth_mo": before["roth_mo"]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        a = self.client.get("/api/settings").json()["ret_wizard_answers"]
        self.assertEqual((a["employer_mo"], a["roth_mo"]), (450, 250))
        after = self.inputs()
        self.assertEqual((after["td_mo"], after["roth_mo"], after["saving_mo"]),
                         (450, 250, 700))
        self.assertIsNone(after["saving_hint"])

    def test_a_total_saved_before_the_split_is_not_counted_twice(self):
        write_config(self.conn, birthdate="1980-03-01", ret_wizard_answers={
            "employer_mo": 700, "taxable_mo": 0,
            "answers_v2": ["employer_mo", "taxable_mo"]})
        got = self.inputs()
        # seeded as the saved total split by the ledger; projected as saved
        self.assertEqual((got["td_mo"], got["roth_mo"], got["saving_mo"],
                          got["employer_mo"]), (450, 250, 700, 700))
        self.assertEqual([(p["kind"], p["monthly"])
                          for p in got["saving_parts"]], [("td", 700)])
        # a current client saves the seeded boxes: the same total, the
        # Roth part now counted as Roth
        r = self.client.post("/api/settings", json={
            "ret_wizard_steps": {"saving": "done"}, "ret_answers_v": 2,
            "ret_wizard_answers": {"employer_mo": got["td_mo"],
                                   "taxable_mo": got["taxable_mo"]},
            "ret_wizard_roth_mo": got["roth_mo"]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        after = self.inputs()
        self.assertEqual((after["td_mo"], after["roth_mo"],
                          after["saving_mo"]), (450, 250, 700))
        self.assertEqual([(p["kind"], p["monthly"])
                          for p in after["saving_parts"]],
                         [("td", 450), ("roth", 250)])
        self.assertIsNone(after["saving_hint"])

    def test_no_seed_moves_with_a_saved_roth_answer(self):
        write_config(self.conn, birthdate="1980-03-01", ret_wizard_answers={
            "employer_mo": 700, "roth_mo": 0,
            "answers_v2": ["employer_mo", "roth_mo"]})
        got = self.inputs()
        self.assertEqual((got["td_mo"], got["roth_mo"], got["employer_mo"]),
                         (700, 0, 700))


class NoRothDepositsNoSeed(_Household):
    def test_a_saved_total_is_seeded_as_it_is(self):
        write_config(self.conn, birthdate="1980-03-01", ret_wizard_answers={
            "employer_mo": 700, "answers_v2": ["employer_mo"]})
        got = self.client.get(Q).json()["inputs"]
        self.assertEqual((got["td_mo"], got["roth_mo"], got["employer_mo"]),
                         (700, 0, 700))
        self.assertIsNone(got["saving_hint"])


class TheRothAnswerIsStored(_Household):
    def save(self, body):
        return self.client.post("/api/settings", json=body)

    def answers(self):
        return self.client.get("/api/settings").json()["ret_wizard_answers"]

    def test_it_rides_beside_the_answers_and_is_marked(self):
        r = self.save({"ret_wizard_answers": {"employer_mo": 300},
                       "ret_wizard_roth_mo": 450, "ret_answers_v": 2})
        self.assertEqual(r.status_code, 200, r.text[:300])
        a = self.answers()
        self.assertEqual((a["employer_mo"], a["roth_mo"]), (300, 450))
        self.assertIn("roth_mo", a["answers_v2"])
        got = self.client.get(Q).json()["inputs"]
        self.assertEqual((got["employer_mo"], got["roth_mo"]), (300, 450))
        # an older client's save of the other two leaves it alone
        self.save({"ret_wizard_answers": {"employer_mo": 350,
                                          "taxable_mo": 0},
                   "ret_answers_v": 2})
        self.assertEqual(self.answers()["roth_mo"], 450)
        # null forgets it
        self.save({"ret_wizard_roth_mo": None, "ret_answers_v": 2})
        self.assertNotIn("roth_mo", self.answers())

    def test_a_bad_figure_is_refused_by_its_label(self):
        r = self.save({"ret_wizard_roth_mo": -5, "ret_answers_v": 2})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Roth retirement $/mo", r.text)

    def test_a_restore_keeps_it(self):
        kept, dropped = restore._ret_answers(
            {"roth_mo": 450, "employer_mo": 300,
             "answers_v2": ["roth_mo", "employer_mo"]})
        self.assertEqual(kept["roth_mo"], 450)
        self.assertEqual(dropped, 0)


if __name__ == "__main__":
    unittest.main()
