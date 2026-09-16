"""The demo generator: a seeded synthetic household must light up every
product surface — verdict, forecast (with card scenarios), recurring,
budgets, lenses, cash flow, net worth. Same seed, same household."""

import datetime as dt
import unittest
import uuid

from oikonome import demo
from oikonome.db import tenancy

from .util import _ensure_db

TODAY = dt.date.today()


def _seed_tenant(seed=42, years=6):
    """demo.seed() refuses non-fresh instances (the shared test DB has
    users), so drive the generator directly into a fresh tenant — the
    exact code path after seed()'s freshness check."""
    import random
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, f"demo-{uuid.uuid4().hex[:8]}")
    finally:
        admin.close()
    rng = random.Random(seed)
    # the SHARED sequence — never repeat it here: a hand-listed copy
    # silently omits whichever generator it forgets
    g = demo._generate(demo._Gen(rng, years, TODAY))
    conn = tenancy.tenant_connect(tid)
    from oikonome.engine import bills, budget
    demo._write(conn, g, rng, TODAY)
    demo._configure(conn, g, rng, TODAY, budget, bills,
                    login=("demo@example.com", "pw-demo-12345"))
    return tid, conn, g


class DemoGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        cls.tid, cls.conn, cls.g = _seed_tenant()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_volume_and_shape(self):
        c = self.conn
        n = c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
        # ~90–120 txns/month reads like an ordinary household
        self.assertGreater(n, 6 * 12 * 70)
        self.assertLess(n, 6 * 12 * 170)
        accts = {r["id"]: r for r in c.execute(
            "SELECT id, type, balance_current FROM accounts").fetchall()}
        for a in ("demo-chk", "demo-sav", "demo-visa", "demo-amex",
                  "demo-brok", "demo-401k", "demo-roth", "demo-cry"):
            self.assertIn(a, accts)
            self.assertIsNotNone(accts[a]["balance_current"])
        self.assertGreater(accts["demo-chk"]["balance_current"], 0)
        n_hold = c.execute("SELECT COUNT(*) n FROM holdings").fetchone()["n"]
        self.assertGreaterEqual(n_hold, 8)
        n_tax = c.execute(
            "SELECT COUNT(*) n FROM income_annual").fetchone()["n"]
        self.assertGreaterEqual(n_tax, 6)

    def test_bills_income_and_config(self):
        c = self.conn
        bills = c.execute("SELECT COUNT(*) n FROM bills WHERE active=1 "
                          "AND amount < 0").fetchone()["n"]
        self.assertGreaterEqual(bills, 9)
        income = c.execute("SELECT COUNT(*) n FROM bills WHERE active=1 "
                           "AND amount > 0").fetchone()["n"]
        self.assertEqual(income, 1)
        from oikonome.engine import budget
        cfg = budget.load_config(c)
        self.assertTrue(cfg["wizard_done"])
        self.assertEqual(cfg["checking_account_id"], "demo-chk")
        self.assertTrue(cfg["savings_goals"])
        self.assertGreater(cfg["food_monthly"], 0)

    def test_every_surface_computes(self):
        c = self.conn
        from oikonome.engine import budget, forecast, reporting
        st = budget.month_status(c, TODAY)
        self.assertIn(st["verdict"],
                      ("OVER BUDGET", "UNDER BUDGET", "ON BUDGET"))
        self.assertGreater(st["buckets"]["fixed"]["month_budget"], 0)
        fc = forecast.build(c, days=60)
        self.assertGreater(fc["checking"], 0)
        self.assertGreater(fc["card_debt"], 0)      # liabilities light up
        self.assertTrue(fc["card_events_stmt"])     # autopay scenario real
        nw = reporting.compute_networth(c)
        self.assertGreater(nw["current_total"], 0)
        cf = reporting.compute_cashflow(c)
        self.assertGreaterEqual(len(cf["savings_by_year"]), 6)
        self.assertTrue(cf["reported_income"])      # tax spine present
        self.assertTrue(cf["career_income"])
        from oikonome.web import lenses
        y = lenses.year_summary(c, TODAY.year, today=TODAY)
        self.assertTrue(any(cell["verdict"] for cell in y["cells"]))

    def test_fees_report_lights_up(self):
        from oikonome.engine import reporting
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM transactions "
            "WHERE category_primary='BANK_FEES'").fetchone()["n"]
        self.assertGreater(n, 5)
        fees = reporting.compute_fees(self.conn)
        self.assertGreater(fees["total"], 0)
        self.assertTrue(fees["by_platform"])
        self.assertTrue(fees["by_year"])
        # investment drag only — the seeded card membership and ATM
        # fees are spending, so their institutions must not appear here.
        # Both investment platforms must show, or the comparison the section
        # exists for degenerates into a one-row table.
        self.assertEqual(sorted(p[0] for p in fees["by_platform"]),
                         ["Evergreen Brokerage", "Meridian Retirement Services"])

    def test_alert_surfaces(self):
        c = self.conn
        hist = c.execute("SELECT COUNT(*) n FROM alerts_log").fetchone()["n"]
        self.assertGreaterEqual(hist, 8)     # a believable past
        # live conditions: a flagged reimbursement + detector proposals
        self.assertEqual(c.execute(
            "SELECT COUNT(*) n FROM reimburse_flags").fetchone()["n"], 1)
        from oikonome.engine import bills
        pending = bills.pending_proposals(c)
        self.assertGreaterEqual(len(pending), 1)   # PixelCloud is unbilled
        payees = {p["payee"].lower() for p in pending}
        self.assertTrue(any("pixelcloud" in p for p in payees), payees)

    def test_demo_mode_and_retire_inputs(self):
        from oikonome.engine import budget, retirement
        cfg = budget.load_config(self.conn)
        self.assertTrue(cfg["demo_mode"])
        self.assertGreater(int(cfg["ss_estimates"]["67"]), 0)
        # the page default derives from THIS household's ledger, not $250k
        ds = retirement.default_spend(self.conn)
        self.assertNotEqual(ds, 250_000)
        self.assertGreater(ds, 30_000)
        self.assertLess(ds, 220_000)

    def test_income_is_ordinary(self):
        """The demo household's expected income has to read like an
        ordinary one — around $5-9k take-home, not five figures a month."""
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        self.assertGreaterEqual(cfg["budgeted_income_monthly"], 4_300)
        self.assertLessEqual(cfg["budgeted_income_monthly"], 9_500)

    def test_networth_property_layer_and_loans(self):
        """House + vehicle as manual assets, mortgage + auto loan as live
        loan accounts — the Net Worth property layer must never be empty."""
        from oikonome.engine import budget, reporting
        cfg = budget.load_config(self.conn)
        kinds = {a["kind"] for a in cfg["manual_assets"]}
        self.assertEqual(kinds, {"property", "vehicle"})
        loans = {r["id"]: r["balance_current"] for r in self.conn.execute(
            "SELECT id, balance_current FROM accounts WHERE type='loan'")}
        self.assertEqual(set(loans), {"demo-mort", "demo-auto"})
        self.assertGreater(loans["demo-mort"], 50_000)
        self.assertGreater(loans["demo-auto"], 1_000)
        nw = reporting.compute_networth(self.conn)
        self.assertEqual(len(nw["property_items"]), 4)   # 2 manual + 2 loans
        # house value beats the two loan balances → positive property net
        self.assertGreater(nw["property_net"], 0)

    def test_networth_trend_is_alive(self):
        """The reconstructed trend must ride the ledger's share history —
        rising over ten years, not a flat back-cast."""
        from oikonome.engine import reporting
        trend = reporting.compute_networth(self.conn)["trend"]
        self.assertGreater(len(trend), 48)
        vals = [v for _, v in trend]
        early = sum(vals[:12]) / 12
        late = sum(vals[-12:]) / 12
        self.assertGreater(late, early * 1.5)
        # and it wobbles — a flat line has (nearly) identical steps
        steps = {round(b - a) for a, b in zip(vals, vals[1:])}
        self.assertGreater(len(steps), 10)

    def test_holdings_match_ledger_share_history(self):
        """Holdings today == the ledger's accumulated share deltas, per
        account+symbol — the invariant _reconstruct_trend walks back on."""
        import json
        deltas: dict[tuple, float] = {}
        for r in self.conn.execute(
                "SELECT account_id, raw FROM transactions "
                "WHERE raw::text LIKE '%shares%'"):
            raw = r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"])
            k = (r["account_id"], raw["symbol"])
            deltas[k] = deltas.get(k, 0.0) + raw["shares"]
        held = {(r["account_id"], r["symbol"]): r["quantity"]
                for r in self.conn.execute(
                    "SELECT account_id, symbol, quantity FROM holdings")}
        self.assertEqual(set(held), set(deltas))
        for k, qty in held.items():
            self.assertAlmostEqual(qty, deltas[k], places=1, msg=str(k))
        # investment balances agree with their holdings — and every
        # account is ALIVE (a $0 brokerage is a dead Net Worth surface)
        for aid in ("demo-brok", "demo-401k", "demo-roth", "demo-cry"):
            bal = self.conn.execute(
                "SELECT balance_current AS b FROM accounts WHERE id=%s",
                (aid,)).fetchone()["b"]
            self.assertGreater(bal, 1_000, aid)
            hsum = self.conn.execute(
                "SELECT COALESCE(SUM(value),0) AS v FROM holdings "
                "WHERE account_id=%s", (aid,)).fetchone()["v"]
            self.assertAlmostEqual(bal, hsum, delta=1.0, msg=aid)

    def test_business_entity_seeded(self):
        """The business surfaces need a business to show: an entity, a
        100% member, its own entity-owned account, and business costs on a
        personal card via the per-transaction override."""
        ent = self.conn.execute(
            "SELECT name, structure, state, ein_last4, formation_date, "
            "business_start_date FROM business_entity").fetchall()
        self.assertEqual(len(ent), 1)
        e = ent[0]
        self.assertTrue(e["name"])
        self.assertEqual(e["structure"], "single_member_llc")
        self.assertEqual(len(e["ein_last4"]), 4)
        # started operating on or after it was formed
        self.assertGreaterEqual(e["business_start_date"], e["formation_date"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM entity_membership").fetchone()["n"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM accounts WHERE id='demo-biz' "
            "AND entity_id IS NOT NULL").fetchone()["n"], 1)
        self.assertGreater(self.conn.execute(
            "SELECT COUNT(*) n FROM transactions WHERE entity_id IS NOT NULL "
            "AND account_id <> 'demo-biz'").fetchone()["n"], 0)

    def test_business_equity_matches_the_ledger(self):
        """The equity page sits next to the ledger, so it must agree with
        it to the cent — every draw and the opening contribution."""
        led_draws = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) v FROM transactions "
            "WHERE account_id='demo-biz' AND name='OWNER DRAW'"
        ).fetchone()["v"]
        rec_draws = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) v FROM equity_movement "
            "WHERE kind='draw'").fetchone()["v"]
        self.assertAlmostEqual(float(led_draws), float(rec_draws), places=2)
        led_con = self.conn.execute(
            "SELECT COALESCE(-SUM(amount),0) v FROM transactions "
            "WHERE account_id='demo-biz' AND name='OWNER CONTRIBUTION'"
        ).fetchone()["v"]
        rec_con = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) v FROM equity_movement "
            "WHERE kind='contribution'").fetchone()["v"]
        self.assertAlmostEqual(float(led_con), float(rec_con), places=2)
        # every draw out of the business lands in personal checking
        into_chk = self.conn.execute(
            "SELECT COALESCE(-SUM(amount),0) v FROM transactions "
            "WHERE account_id='demo-chk' AND name LIKE 'TRANSFER FROM %%'"
        ).fetchone()["v"]
        self.assertAlmostEqual(float(led_draws), float(into_chk), places=2)

    def test_business_reads_as_a_side_practice(self):
        """Sized against the household's salary: a side practice, not a
        second full-time job, and its float is a working balance."""
        bal = self.conn.execute(
            "SELECT balance_current v FROM accounts WHERE id='demo-biz'"
        ).fetchone()["v"]
        self.assertGreater(float(bal), 500)
        self.assertLess(float(bal), 60_000)
        rev = self.conn.execute(
            """SELECT COALESCE(-SUM(amount),0) v FROM transactions
               WHERE account_id='demo-biz' AND amount < 0
                 AND category_primary='INCOME'
                 AND date >= date_trunc('year', CURRENT_DATE)
                             - INTERVAL '1 year'
                 AND date <  date_trunc('year', CURRENT_DATE)"""
        ).fetchone()["v"]
        self.assertGreater(float(rev), 10_000)     # a real practice
        self.assertLess(float(rev), 80_000)        # not a second salary

    def test_notes_seeded(self):
        """Free-text notes, attached to real rows, none blank or holding an
        unformatted placeholder."""
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM transaction_notes").fetchone()["n"]
        self.assertGreaterEqual(n, 30)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM transaction_notes x WHERE NOT EXISTS "
            "(SELECT 1 FROM transactions t WHERE t.id = x.txn_id)"
        ).fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM transaction_notes "
            "WHERE BTRIM(note) = '' OR note LIKE '%%{n}%%'"
        ).fetchone()["n"], 0)

    def test_receipts_seeded(self):
        """Items page + Business expense report need parsed line items."""
        n = self.conn.execute("SELECT COUNT(*) n FROM receipts "
                              "WHERE status='parsed'").fetchone()["n"]
        # 3 curated (grocery / pharmacy / business) + a random scattering
        # across the last ~18 months, so the feature looks USED rather than
        # staged. Bounded on both sides: a runaway seeder would be as wrong
        # as an empty one.
        self.assertGreaterEqual(n, 10)
        self.assertLessEqual(n, 40)
        items = self.conn.execute(
            "SELECT COUNT(*) n FROM receipt_items").fetchone()["n"]
        self.assertGreaterEqual(items, 9)
        biz = self.conn.execute(
            "SELECT COUNT(*) n FROM receipt_items "
            "WHERE tag='business'").fetchone()["n"]
        self.assertGreaterEqual(biz, 1)
        # …and the transaction-level Business report has flagged rows
        flags = self.conn.execute(
            "SELECT COUNT(*) n FROM business_flags").fetchone()["n"]
        self.assertGreaterEqual(flags, 1)
        # no receipt without its transaction
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM receipts r WHERE NOT EXISTS "
            "(SELECT 1 FROM transactions t WHERE t.id = r.txn_id)"
        ).fetchone()["n"], 0)
        # line items sum to their receipt's transaction amount
        for r in self.conn.execute(
                """SELECT r.id, t.amount, SUM(ri.amount) AS s
                   FROM receipts r
                   JOIN transactions t ON t.id = r.txn_id
                   JOIN receipt_items ri ON ri.receipt_id = r.id
                   GROUP BY r.id, t.amount"""):
            self.assertAlmostEqual(r["amount"], r["s"], places=1)

    def test_bill_roster_is_full(self):
        """A household has more than a handful of recurring bills:
        utilities, loans, insurance, subscriptions."""
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM bills "
            "WHERE active=1 AND type='BILL'").fetchone()["n"]
        self.assertGreaterEqual(n, 13)

    def test_demo_login_stored_and_served_safely(self):
        """The seed stores the printed credentials in config so the login
        page can pre-fill them — but /api/demo-login only ever reveals
        them on a single-user demo instance."""
        from oikonome.engine import budget
        from oikonome.web.app import _demo_login_payload
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["demo_login"],
                         {"email": "demo@example.com",
                          "password": "pw-demo-12345"})
        # single-user + demo-locked + creds present → served
        self.assertEqual(_demo_login_payload(1, cfg),
                         {"demo": True, "email": "demo@example.com",
                          "password": "pw-demo-12345"})
        # every other shape stays dark
        self.assertEqual(_demo_login_payload(2, cfg), {"demo": False})
        self.assertEqual(_demo_login_payload(0, cfg), {"demo": False})
        self.assertEqual(_demo_login_payload(1, None), {"demo": False})
        no_lock = dict(cfg, demo_mode=False)   # planted key, not demo-locked
        self.assertEqual(_demo_login_payload(1, no_lock), {"demo": False})
        no_creds = {k: v for k, v in cfg.items() if k != "demo_login"}
        self.assertEqual(_demo_login_payload(1, no_creds), {"demo": False})

    def test_demo_login_endpoint_dark_on_busy_instance(self):
        """The shared test DB has many users — the pre-auth endpoint must
        refuse to serve anyone's credentials there."""
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        r = TestClient(app).get("/api/demo-login")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"demo": False})

    def test_demo_guard_blocks_doors(self):
        from oikonome.web import demoguard
        demoguard._cache.clear()
        self.assertTrue(demoguard.is_demo(self.tid))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            demoguard.deny({"tenant_id": self.tid})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_signs_and_conventions(self):
        c = self.conn
        # income deposits are negative on checking; card purchases positive
        pay = c.execute("SELECT amount FROM transactions WHERE "
                        "name LIKE '%PAYROLL%' LIMIT 1").fetchone()
        self.assertLess(pay["amount"], 0)
        # card payments excluded from spend (LOAN_PAYMENTS_CC on both sides)
        n = c.execute("SELECT COUNT(*) n FROM transactions WHERE "
                      "category_detailed='LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'"
                      ).fetchone()["n"]
        self.assertGreater(n, 50)

    def test_custom_buckets_light_up(self):
        """The carve-out buckets must claim REAL generated spend — a
        merchant token that doesn't match the generated names would render
        a permanently-empty bar (the failure mode this test pins)."""
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        specs = budget.custom_bucket_specs(cfg)
        by_name = {s["name"]: s for s in specs}
        self.assertGreaterEqual(len(specs), 4)
        self.assertEqual(by_name["Coffee"]["parent"], "food")
        self.assertEqual(by_name["Dining out"]["parent"], "food")
        for n in ("Gas", "Online shopping"):
            self.assertEqual(by_name[n]["parent"], "other")
        rows = self.conn.execute(
            "SELECT name, merchant_name FROM transactions "
            "WHERE amount > 0 AND removed = 0").fetchall()
        for s in specs:
            self.assertGreater(s["monthly"], 0, s["name"])
            matchers = [budget.merchant_matcher(m) for m in s["merchants"]]
            hits = sum(1 for r in rows if any(
                m(f"{r['merchant_name'] or ''} {r['name']}")
                for m in matchers))
            self.assertGreater(hits, 0, s["name"])
        # and the display decomposition carries them as children with the
        # derived budgets (the Today/Budget bars read from these)
        st = budget.month_status(self.conn, TODAY)
        kids = {c["name"] for p in ("food", "other")
                for c in st["buckets"][p]["children"]}
        self.assertTrue({"Coffee", "Dining out", "Gas",
                         "Online shopping"} <= kids, kids)

    def test_first_signin_lands_on_today(self):
        """The wizard is fully skipped: the onboarding payload —
        the ONLY thing the SPA router reads — must be in the landed state,
        so neither App.tsx's "Finish setup" pill (setupPending), Today's
        resume-setup card, nor Welcome's resume logic has a leg to stand
        on. Welcome.tsx additionally bounces to Today when every step is
        marked AND wizard_done — assert that exact condition holds."""
        from oikonome.web import api as web_api
        d = web_api.onboarding_api(user={"tenant_id": self.tid})
        self.assertTrue(d["wizard_done"])
        # App.tsx setupPending: !wizard_done && !(accounts && transactions
        # && bills && budgets_set) — both halves must clear
        self.assertGreater(d["accounts"], 0)
        self.assertGreater(d["transactions"], 0)
        self.assertGreater(d["bills"], 0)
        self.assertTrue(d["budgets_set"])
        # Welcome.tsx stepKeys(): hosted drops "email"; self-host is the
        # full list: connect → bills → budgets → (email) → finish, with no
        # sync/import steps. Every key resume scans must be marked or
        # /welcome re-enters mid-wizard instead of bouncing.
        for k in ("connect", "bills", "budgets", "email", "finish"):
            self.assertIn(d["wizard_steps"].get(k), ("done", "skipped"), k)

    def test_alert_strip_is_exactly_two(self):
        """A strip of six notifications reads noisy. Bills are seeded at the
        ledger's own recent median, so bills.run applies ZERO amount drifts
        on a fresh demo — the live strip is exactly the
        awaiting-reimbursement + pending-proposals pair."""
        from oikonome.web import report
        st = report.gather(self.conn, TODAY)
        kinds = [a["kind"] for a in st["alerts"]]
        self.assertEqual(sorted(kinds), ["proposals", "reimb"], st["alerts"])

    def test_food_budget_reads_1500(self):
        """The Food line (post-carve-outs) shows a round $1,500 — and the
        generated grocery spend lives near it, so the bar reads lived-in
        rather than permanently under."""
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        st = budget.month_status(self.conn, TODAY)
        food = st["buckets"]["food"]
        carve = sum(c["month_budget"] for c in food["children"])
        self.assertEqual(food["month_budget"] - carve, 1500)
        self.assertEqual(cfg["food_monthly"] - self.g.coffee_m
                         - self.g.dining_m, 1500)
        # non-carve-out food spend (groceries) averages near the line
        cutoff = TODAY - dt.timedelta(days=365)
        grocers = {x.upper() for x in demo.GROCERS}
        avg = sum(t["amount"] for t in self.g.txns
                  if t["name"] in grocers and t["date"] >= cutoff
                  and t["amount"] > 0) / 12
        self.assertGreater(avg, 1_100)
        self.assertLess(avg, 1_900)

    def test_plan_is_not_overcommitted(self):
        """The bigger food budget + vacation plan must fit inside the
        household's income — a negative plan surplus reads broken."""
        from oikonome.engine import budget
        st = budget.month_status(self.conn, TODAY)
        self.assertIsNotNone(st["plan_surplus"])
        self.assertGreaterEqual(st["plan_surplus"], 0, st["plan_surplus"])

    def test_savings_goals_shape(self):
        """A six-figure Savings target reads absurd. "Savings" stays
        name-keyed (money-map row) but open-ended; "Vacation" is the
        relatable $5k goal, mid-flight via its token-matched transfers."""
        from oikonome.engine import budget, savings
        cfg = budget.load_config(self.conn)
        prog = {p["name"]: p for p in savings.progress(self.conn, cfg, TODAY)}
        self.assertEqual(set(prog), {"Savings", "Vacation"})
        s = prog["Savings"]
        self.assertEqual(s["target"], 0)          # open-ended
        self.assertIsNone(s["pct"])
        self.assertGreater(s["saved"], 0)
        self.assertGreater(s["rate_90d"], 0)
        v = prog["Vacation"]
        self.assertEqual(v["target"], 5000)
        self.assertGreater(v["pct"], 40)          # in progress…
        self.assertLess(v["pct"], 90)             # …not nearly-done
        # $200/mo, token-matched. NOT a fixed 200: rate_90d is the matched
        # net over 90 days ÷ 3, and the generator contributes on the 4th of
        # each month, so the window spans three contributions only when today
        # is on or after the 4th. On the 1st-3rd it spans two and the rate is
        # 133.33, so the expectation is derived the same way the data is
        # seeded.
        today = dt.date.today()
        since = today - dt.timedelta(days=90)
        n, d = 0, dt.date(since.year, since.month, 4)
        while d <= today:
            if d >= since:
                n += 1
            d = (d.replace(day=28) + dt.timedelta(days=7)).replace(day=4)
        self.assertEqual(v["rate_90d"], round(200 * n / 3.0, 2))

    def test_rules_page_has_rows(self):
        """The Rules page shows a lived-in mix of provenances, every rule
        matching real generated merchants."""
        from oikonome.web import data
        rules = data.list_rules(self.conn)["rules"]
        self.assertGreaterEqual(len(rules), 6)
        self.assertEqual({r["source"] for r in rules},
                         {"user", "llm", "seed"})
        for r in rules:
            self.assertGreater(r["count"], 0, r["merchant"])

    def test_same_seed_same_household(self):
        tid2, conn2, g2 = _seed_tenant(seed=42, years=6)
        try:
            self.assertEqual(len(g2.txns), len(self.g.txns))
            self.assertEqual(g2.txns[0], self.g.txns[0])
            self.assertEqual(g2.txns[-1], self.g.txns[-1])
            self.assertEqual(g2.housing, self.g.housing)
        finally:
            conn2.close()

    def test_different_seed_different_household(self):
        import random
        g3 = demo._Gen(random.Random(7), 6, TODAY)
        self.assertNotEqual((g3.gross0, g3.housing),
                            (self.g.gross0, self.g.housing))


if __name__ == "__main__":
    unittest.main()
