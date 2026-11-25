"""The Retirement projection starts from what the household told it.

The walkthrough saves each answer (`ret_wizard_answers`); the page's
projection must read them, or a person who answered "$80,000 a year,
$1,500 a month into the 401(k)" opens a plan built on the ledger's spend
and no saving at all. Where nothing was saved, the trailing year of the
ledger is the spend; where the ledger is too thin for that, there is no
assumed spend — the payload says one is needed and projects nothing.
Each source is named (`spend_source`, `saving_source`) so the page can
say which it used, and a query-string what-if still overrides for the
adjust panel.

Social Security at a claim age with no saved estimate is the straight
line between the nearest saved ages (an age outside them takes the
nearest), never $0; the claim age itself is saved with the answers.

Card debt in the projection is the Debt page's: an account the household
excluded is left out, and each card grows at the rate the Debt page walks
it at. The worst stress-scenario spend is decided on the server
(`stress_floor_spend`), never as a minimum a client takes.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, retirement

from .util import _ensure_db, add_txn, seed_accounts, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class _Tenant(unittest.TestCase):
    """One fresh household per test class: checking $5,000, card $250."""

    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"rin-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def conn(self):
        return tenancy.tenant_connect(self.tid)

    def set_cfg(self, **kv):
        conn = self.conn()
        try:
            cfg = budget.load_config(conn)
            for k, v in kv.items():
                if v is None:
                    cfg.pop(k, None)
                else:
                    cfg[k] = v
            budget.save_config(conn, cfg)
        finally:
            conn.close()

    def get(self, qs=""):
        # as the current clients ask: they can show a spend-less reply
        sep = "&" if "?" in qs else "?"
        r = self.client.get(f"/api/retirement{qs}{sep}ask_spend=1")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class SpendAndSavingSources(_Tenant):
    def setUp(self):
        self.set_cfg(ret_wizard_answers=None, birthdate=None)

    def test_saved_answers_drive_the_projection(self):
        r = self.client.post("/api/settings", json={"ret_wizard_answers": {
            "spend": 80_000, "employer_mo": 1_500, "taxable_mo": 200,
            "ret": 6, "infl": 2.5, "end": 90, "age": 50},
            "ret_answers_v": 2})
        self.assertEqual(r.status_code, 200, r.text)
        body = self.get()
        inp = body["inputs"]
        self.assertEqual(inp["spend"], 80_000)
        self.assertEqual(inp["spend_source"], "saved")
        self.assertEqual(inp["employer_mo"], 1_500)
        self.assertEqual(inp["taxable_mo"], 200)
        self.assertEqual(inp["saving_yr"], 1_700 * 12)
        self.assertEqual(inp["saving_source"], "saved")
        self.assertEqual((inp["ret"], inp["infl"], inp["end"], inp["age"]),
                         (6.0, 2.5, 90, 50))
        self.assertEqual(body["spend"], 80_000)
        self.assertTrue(body["rows"])

    def test_a_query_what_if_still_overrides_the_answers(self):
        self.client.post("/api/settings", json={"ret_wizard_answers": {
            "spend": 80_000, "employer_mo": 1_500}, "ret_answers_v": 2})
        inp = self.get("?spend=50000&employer_mo=0")["inputs"]
        self.assertEqual(inp["spend"], 50_000)
        self.assertEqual(inp["spend_source"], "what_if")
        self.assertEqual(inp["employer_mo"], 0)
        self.assertEqual(inp["saving_source"], "what_if")

    def test_no_saved_saving_is_no_new_saving_and_says_so(self):
        self.client.post("/api/settings",
                         json={"ret_wizard_answers": {"spend": 40_000},
                               "ret_answers_v": 2})
        body = self.get()
        inp = body["inputs"]
        self.assertEqual(inp["saving_source"], "none")
        self.assertEqual((inp["employer_mo"], inp["taxable_mo"],
                          inp["saving_yr"]), (0, 0, 0))
        self.assertTrue(body["rows"])            # still projected

    def test_a_thin_ledger_asks_for_a_spend_and_projects_nothing(self):
        # the fixture household has no ledger at all: no assumed spend
        body = self.get()
        inp = body["inputs"]
        self.assertIsNone(inp["spend"])
        self.assertEqual(inp["spend_source"], "needed")
        self.assertEqual(inp["saving_source"], "none")
        self.assertIsNone(body["spend"])
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["stress"], [])
        self.assertIsNone(body["hist"])
        self.assertIsNone(body["earliest"])
        self.assertIsNone(body["stress_floor_spend"])
        self.assertIsNone(body["stress_floor_vs_spend"])
        # the balances are still the household's own
        self.assertEqual(body["buckets"]["cash"], 4_750.0)

    def test_a_year_of_ledger_is_the_spend_when_none_was_saved(self):
        conn = self.conn()
        try:
            today = dt.date.today()
            # a ledger more than a year deep, so the year is read as a year
            add_txn(conn, today - dt.timedelta(days=400), 1.0,
                    "EXAMPLE GROCER", txn_id="led-old")
            for i in range(12):
                add_txn(conn, today - dt.timedelta(days=30 * i + 5), 3_000.0,
                        "EXAMPLE GROCER", txn_id=f"led{i}")
        finally:
            conn.close()
        try:
            inp = self.get()["inputs"]
            self.assertEqual(inp["spend_source"], "ledger")
            self.assertEqual(inp["spend"], 36_000)
        finally:
            conn = self.conn()
            try:
                conn.execute("DELETE FROM transactions WHERE id LIKE 'led%%'")
            finally:
                conn.close()


class SocialSecurityByClaimAge(_Tenant):
    def setUp(self):
        self.set_cfg(ret_wizard_answers=None)

    def ss(self, sched, ssage):
        self.set_cfg(ss_estimates=sched)
        return self.get(f"?age=60&end=75&spend=40000&ssage={ssage}")[
            "inputs"]["your_ss"]

    def test_a_claim_age_between_saved_ages_is_the_straight_line(self):
        self.assertEqual(self.ss({"62": 2000, "70": 3600}, 66), 2800)
        self.assertEqual(self.ss({"64": 2000, "68": 3200}, 67), 2900)

    def test_a_saved_age_is_read_as_saved(self):
        self.assertEqual(self.ss({"62": 2000, "70": 3600}, 70), 3600)

    def test_an_age_outside_the_saved_ones_is_the_nearest_adjusted(self):
        # SSA's early reduction / delayed credit from the nearest saved age
        self.assertEqual(self.ss({"65": 2500, "68": 3000}, 62),
                         round(2500 * 0.70 / (1 - 24 * 5 / 900), 2))
        self.assertEqual(self.ss({"65": 2500, "68": 3000}, 70),
                         round(3000 * 1.24 / 1.08, 2))

    def test_the_claim_age_is_saved_with_the_answers(self):
        self.set_cfg(ss_estimates={"62": 2000, "70": 3600})
        r = self.client.post("/api/settings",
                             json={"ret_wizard_ssage": 64,
                                   "ret_answers_v": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.client.get("/api/settings").json()
            ["ret_wizard_answers"]["ssage"], 64)
        inp = self.get("?age=60&end=75&spend=40000")["inputs"]
        self.assertEqual(inp["ssage"], 64)
        self.assertEqual(inp["your_ss"], 2400)
        for bad in (75, 66.5):
            with self.subTest(bad=bad):
                r = self.client.post("/api/settings", json={
                    "ret_wizard_ssage": bad, "ret_answers_v": 2})
                self.assertEqual(r.status_code, 400)
                self.assertIn("whole", r.json()["detail"])


class StressFloorIsServerSent(_Tenant):
    def test_stress_floor_is_the_worst_scenario_not_average_markets(self):
        body = self.get("?age=50&end=90&spend=40000")
        bad = [s["max_spend_at_ref"] for s in body["stress"]
               if s["label"] != retirement.SCENARIOS[0][0]]
        self.assertEqual(body["stress_floor_spend"], min(bad))
        self.assertEqual(body["stress_floor_vs_spend"],
                         min(bad) - body["spend"])


    def test_each_row_and_scenario_says_how_it_reads_against_the_target(self):
        """The stress table's red figures and the age rows' green ones
        are the server's comparison with the target spend; the clients
        colour by these flags and compare nothing themselves."""
        body = self.get("?age=50&end=90&spend=40000")
        spend = body["spend"]
        self.assertTrue(body["rows"] and body["stress"])
        for row in body["rows"]:
            self.assertEqual(row["meets_target"], row["max_spend"] >= spend)
        for s in body["stress"]:
            self.assertEqual(s["under_target"],
                             s["max_spend_at_ref"] < spend)


class DebtsComeFromTheDebtPage(_Tenant):
    """Cards and assets share the Net Worth scope (shadows and hidden
    accounts out, the Accounts page's budget exclusion not applied to
    balances); each card grows at the rate the Debt page walks it at."""

    def setUp(self):
        self.set_cfg(excluded_accounts=None, debt_plan=None)
        conn = self.conn()
        try:
            conn.execute("UPDATE accounts SET balance_current=250, "
                         "user_removed_at=NULL WHERE id='card'")
        finally:
            conn.close()

    def buckets(self):
        conn = self.conn()
        try:
            return retirement.current_buckets(conn)
        finally:
            conn.close()

    def test_an_excluded_card_counts_as_its_checking_does(self):
        self.set_cfg(excluded_accounts=["card", "chk"])
        b = self.buckets()
        self.assertEqual((b["debt"], b["cash"]), (250.0, 4_750.0))

    def test_a_hidden_card_is_left_out_like_a_hidden_asset(self):
        conn = self.conn()
        try:
            conn.execute("UPDATE accounts SET user_removed_at=now() "
                         "WHERE id='card'")
        finally:
            conn.close()
        b = self.buckets()
        self.assertEqual((b["debt"], b["cash"]), (0.0, 5_000.0))

    def test_a_card_credit_balance_still_nets(self):
        conn = self.conn()
        try:
            conn.execute("UPDATE accounts SET balance_current=-100 "
                         "WHERE id='card'")
        finally:
            conn.close()
        b = self.buckets()
        self.assertEqual((b["debt"], b["cash"]), (-100.0, 5_100.0))

    def test_the_card_grows_at_the_debt_pages_rate(self):
        from oikonome.engine import debt
        # no issuer rate on file: the Debt page's typical card rate
        self.assertEqual(self.buckets()["debt_apr"], debt.TYPICAL_APR["card"])
        r = self.client.post("/api/debt-plan", json={
            "method": "avalanche", "extra_monthly": 0,
            "debts": {"card": {"apr": 12.5}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.buckets()["debt_apr"], 12.5)
        # excluded from the Debt plan, the household's rate still applies
        self.set_cfg(excluded_accounts=["card"])
        self.assertEqual(self.buckets()["debt_apr"], 12.5)


class OnlyMarkedAnswersAreInputs(_Tenant):
    """Answers saved before the projection read them were the old wizard's
    prefilled boxes (an assumed spend, an untouched $0 saving). Only an
    answer a current client saved — it sends `ret_answers_v: 2` beside
    them, a field older servers ignore — is an input; the rest are neither
    inputs nor shown."""

    def setUp(self):
        self.set_cfg(ret_wizard_answers=None, birthdate=None)

    def test_a_legacy_blob_prefills_but_is_not_an_input(self):
        self.set_cfg(ret_wizard_answers={"spend": 250_000, "employer_mo": 0,
                                         "taxable_mo": 0, "age": 45})
        inp = self.get()["inputs"]
        self.assertEqual(inp["spend_source"], "needed")
        self.assertEqual(inp["saving_source"], "none")
        got = self.client.get("/api/settings").json()["ret_wizard_answers"]
        self.assertEqual(got["spend"], 250_000)

    def test_an_old_phone_save_never_becomes_an_answer(self):
        post = lambda a: self.client.post(  # noqa: E731
            "/api/settings", json={"ret_wizard_answers": a})
        self.assertEqual(post({"spend": 60_000}).status_code, 200)
        self.assertEqual(self.get()["inputs"]["spend_source"], "needed")
        r = self.client.post("/api/settings", json={
            "ret_wizard_answers": {"spend": 70_000}, "ret_answers_v": 2})
        self.assertEqual(r.status_code, 200)
        inp = self.get()["inputs"]
        self.assertEqual((inp["spend"], inp["spend_source"]),
                         (70_000, "saved"))
        # an old client overwriting it takes it back out of the inputs
        post({"spend": 60_000})
        self.assertEqual(self.get()["inputs"]["spend_source"], "needed")

    def test_an_old_phone_resending_a_confirmed_answer_keeps_it(self):
        self.client.post("/api/settings", json={
            "ret_wizard_answers": {"spend": 70_000}, "ret_answers_v": 2})
        # an older build pre-fills the confirmed figure and re-sends it
        self.client.post("/api/settings",
                         json={"ret_wizard_answers": {"spend": 70_000}})
        inp = self.get()["inputs"]
        self.assertEqual((inp["spend"], inp["spend_source"]),
                         (70_000, "saved"))

    def test_a_marked_save_of_one_step_does_not_promote_the_rest(self):
        self.set_cfg(ret_wizard_answers={"spend": 250_000})
        self.client.post("/api/settings", json={
            "ret_wizard_answers": {"age": 50}, "ret_answers_v": 2})
        inp = self.get()["inputs"]
        self.assertEqual(inp["age"], 50)
        self.assertEqual(inp["spend_source"], "needed")

    def test_an_unknown_format_is_refused(self):
        r = self.client.post("/api/settings", json={
            "ret_wizard_answers": {"spend": 1000}, "ret_answers_v": 3})
        self.assertEqual(r.status_code, 400)
        # inside the answers the marker is an unknown answer, as it is to
        # every older server — so no current client puts it there
        r = self.client.post("/api/settings", json={
            "ret_wizard_answers": {"spend": 1000, "answers_v": 2}})
        self.assertEqual(r.status_code, 400)

    def test_a_restore_keeps_the_marker(self):
        from oikonome.sync.restore import clean_config_shapes
        cfg = {"ret_wizard_answers": {"spend": 70_000, "age": 50,
                                      "answers_v2": ["spend", "bogus", 3]}}
        clean_config_shapes(cfg)
        self.assertEqual(cfg["ret_wizard_answers"],
                         {"spend": 70_000, "age": 50,
                          "answers_v2": ["spend"]})


class OlderClientsQueries(_Tenant):
    def setUp(self):
        self.set_cfg(ret_wizard_answers=None, birthdate=None)

    def test_the_old_phones_apply_on_a_needed_spend(self):
        # the store build turns spend:null into "null", then numv() → 0
        for spend in ("0", "null", "undefined"):
            with self.subTest(spend=spend):
                body = self.get(
                    f"?age=45&spend={spend}&ret=7.0&infl=3.0&end=95&ssage=67"
                    "&stockpct=90&employer_mo=0&taxable_mo=0&resume=45")
                self.assertEqual(body["inputs"]["spend_source"], "needed")

    def test_an_old_apply_reproduces_what_the_server_sent(self):
        self.client.post("/api/settings", json={"ret_wizard_answers": {
            "spend": 80_000, "employer_mo": 1_500}, "ret_answers_v": 2})
        first = self.get()
        i = first["inputs"]
        qs = "&".join(f"{k}={i[k]}" for k in (
            "age", "spend", "ret", "infl", "end", "ssage", "stockpct",
            "employer_mo", "taxable_mo", "resume"))
        again = self.get(f"?{qs}")
        for k in ("spend", "employer_mo", "taxable_mo", "saving_yr"):
            self.assertEqual(again["inputs"][k], i[k], k)
        self.assertEqual(again["rows"], first["rows"])


if __name__ == "__main__":
    unittest.main()
