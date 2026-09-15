"""Hard separation — business-entity money must NOT appear in
personal spend, the verdict, or personal net worth. Leak-proof both ways:
assigned money disappears from personal aggregates; clearing the assignment
brings it back."""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import budget, entities, reporting

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   write_config, TODAY)

MONTH_START = TODAY.replace(day=1)
MONTH_END = (MONTH_START + dt.timedelta(days=40)).replace(day=1)


class HardSeparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"sep-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # a business checking account (balance) under the same test item
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current,balance_available) VALUES "
                "('bizchk','it1','Biz Checking','depository','checking',3000,3000)")
            add_txn(conn, TODAY, 100.0, "PERSONAL LUNCH", account="card")
            add_txn(conn, TODAY, 200.0, "BIZ SUPPLIES", account="card",
                    txn_id="bizt1")                    # per-txn override
            add_txn(conn, TODAY, 50.0, "BIZ RENT", account="bizchk")  # account-assigned
            self.ent = entities.create_entity(
                conn, name="Acme LLC", structure="sole_prop")
            entities.assign_transaction(conn, "bizt1", self.ent["id"])
            entities.assign_account(conn, "bizchk", self.ent["id"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.admin.close()

    def _conn(self):
        # fresh connection so app.entity_account_ids is recomputed
        return tenancy.tenant_connect(self.tid)

    def test_spend_rows_exclude_business(self):
        conn = self._conn()
        try:
            rows = budget._spend_rows(conn, MONTH_START, MONTH_END)
        finally:
            conn.close()
        payees = {r["payee"] for r in rows}
        self.assertIn("PERSONAL LUNCH", payees)
        self.assertNotIn("BIZ SUPPLIES", payees)   # per-transaction override
        self.assertNotIn("BIZ RENT", payees)       # whole-account assignment
        self.assertEqual(round(sum(r["amount"] for r in rows), 2), 100.0)

    def test_verdict_excludes_business(self):
        conn = self._conn()
        try:
            st = budget.month_status(conn, TODAY)
        finally:
            conn.close()
        # only the $100 personal lunch is variable spend; the $250 of business
        # must not show up in the verdict
        self.assertEqual(round(st["variable_actual"], 2), 100.0)

    def test_reports_exclude_business(self):
        # the SPEND_WHERE reporting constant (spending/cashflow/etc.) excludes
        # business the same way _spend_rows does
        conn = self._conn()
        try:
            row = conn.execute(
                f"SELECT COALESCE(SUM({reporting.NET_AMOUNT}),0) AS s "
                f"FROM transactions t WHERE {reporting.SPEND_WHERE}").fetchone()
        finally:
            conn.close()
        self.assertEqual(round(float(row["s"]), 2), 100.0)

    def test_networth_excludes_business_account(self):
        conn = self._conn()
        try:
            names = {a["name"] for a in reporting._live_accounts(conn)}
        finally:
            conn.close()
        self.assertIn("Test Checking", names)
        self.assertNotIn("Biz Checking", names)   # business account out of NW

    def test_clearing_assignment_restores_to_personal(self):
        conn = self._conn()
        try:
            entities.assign_transaction(conn, "bizt1", None)
            entities.assign_account(conn, "bizchk", None)
            conn.commit()
        finally:
            conn.close()
        conn = self._conn()
        try:
            rows = budget._spend_rows(conn, MONTH_START, MONTH_END)
            names = {a["name"] for a in reporting._live_accounts(conn)}
        finally:
            conn.close()
        payees = {r["payee"] for r in rows}
        self.assertIn("BIZ SUPPLIES", payees)      # back in personal spend
        self.assertIn("BIZ RENT", payees)
        self.assertIn("Biz Checking", names)       # back in net worth

    def test_combined_toggle_shows_business(self):
        conn = self._conn()
        try:
            entities.set_combined(conn, True)
            conn.commit()
        finally:
            conn.close()
        conn = self._conn()   # combine var set at connect from config
        try:
            rows = budget._spend_rows(conn, MONTH_START, MONTH_END)
            names = {a["name"] for a in reporting._live_accounts(conn)}
        finally:
            conn.close()
        payees = {r["payee"] for r in rows}
        # combined: business is back in the personal aggregates
        self.assertEqual(payees, {"PERSONAL LUNCH", "BIZ SUPPLIES", "BIZ RENT"})
        self.assertIn("Biz Checking", names)

    def test_import_flagged_reconciles(self):
        conn = self._conn()
        try:
            # a legacy business-flagged (but unassigned) transaction
            add_txn(conn, TODAY, 33.0, "OLD FLAGGED", account="card",
                    txn_id="flag1")
            conn.execute("INSERT INTO business_flags (txn_id) VALUES ('flag1')")
            self.assertEqual(entities.count_flagged(conn), 1)
            moved = entities.import_flagged_transactions(conn, self.ent["id"])
            self.assertEqual(moved, 1)
            self.assertEqual(entities.count_flagged(conn), 0)  # now assigned
            conn.commit()
        finally:
            conn.close()
        conn = self._conn()
        try:
            payees = {r["payee"]
                      for r in budget._spend_rows(conn, MONTH_START, MONTH_END)}
        finally:
            conn.close()
        self.assertNotIn("OLD FLAGGED", payees)   # reconciled → out of personal

    def test_no_entity_is_a_noop(self):
        """A tenant with no entities sees every transaction — the filter is
        free until an entity exists."""
        other = str(tenancy.create_tenant(
            self.admin, f"sep-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(other)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_txn(conn, TODAY, 77.0, "PLAIN SPEND", account="card")
            conn.commit()
        finally:
            conn.close()
        conn = tenancy.tenant_connect(other)
        try:
            rows = budget._spend_rows(conn, MONTH_START, MONTH_END)
        finally:
            conn.close()
        self.assertEqual({r["payee"] for r in rows}, {"PLAIN SPEND"})


if __name__ == "__main__":
    unittest.main()


class CashFlowSeparationTests(unittest.TestCase):
    """Cash flow must not count business INCOME as personal while personal
    spend correctly excludes business costs.

    SPEND_WHERE carries budget.PERSONAL_ONLY_SQL; `_income_by` carried only
    the shadow-account guard. So an entity owner saw the business's revenue
    land in personal income with none of the business's costs against it —
    the one direction that flatters the number.

    The card-settlement helpers inside _cash_graph have the same shape: they
    measure cash leaving a depository account to pay a card, and are summed
    against an mtd_spend_cash that is already personal-only.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"cf-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current,balance_available) VALUES "
                "('bizchk','it1','Biz Checking','depository','checking',"
                "9000,9000)")
            # personal paycheck (negative = money IN, house convention)
            add_txn(conn, TODAY, -4000.0, "ACME PAYROLL", account="chk")
            # business revenue into the business account
            add_txn(conn, TODAY, -7000.0, "CLIENT INVOICE", account="bizchk")
            self.ent = entities.create_entity(
                conn, name="Acme LLC", structure="sole_prop")
            entities.assign_account(conn, "bizchk", self.ent["id"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.admin.close()

    def _income_year(self, combine=False):
        conn = tenancy.tenant_connect(self.tid)
        try:
            if combine:
                cfg = budget.load_config(conn)
                cfg["combine_entities"] = True
                budget.save_config(conn, cfg)
                conn.commit()
                conn.close()
                conn = tenancy.tenant_connect(self.tid)
            rows = reporting._income_by(conn, "year")
        finally:
            conn.close()
        return {r["p"]: r["amt"] for r in rows}

    def test_income_excludes_business_revenue(self):
        got = self._income_year()
        year = TODAY.strftime("%Y")
        self.assertEqual(
            got.get(year), 4000.0,
            "business revenue leaked into personal income — spend already "
            "excludes business costs, so this inflates net cash flow by the "
            "entity's whole top line")

    def test_combined_toggle_still_shows_business_revenue(self):
        """The filter must be the SAME switch personal spend obeys: with
        combine_entities on, both sides come back."""
        got = self._income_year(combine=True)
        self.assertEqual(got.get(TODAY.strftime("%Y")), 11000.0)


class BusinessWizardWiringTests(unittest.TestCase):
    """The guided business setup follows the features map, and self-host
    is not gated.

    The gate is written `features.business !== false`, not `=== true`, and
    that distinction is load-bearing: a gated hosted tenant gets
    `business: false`, while SELF-HOST leaves the features map empty
    (undefined), and `=== true` would have hidden the wizard from every
    self-hosted install — the users who are never gated on anything.
    """

    def _spa(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name)

    def test_wizard_component_exists(self):
        self.assertTrue(self._spa("components/BusinessWizard.tsx").exists())

    def test_settings_entry_is_tier_gated_the_safe_way(self):
        p = self._spa("pages/Settings.tsx")
        if not p.exists():
            self.skipTest("SPA source not present")
        text = p.read_text(encoding="utf-8")
        self.assertIn("/business/setup", text,
                      "Guided setup has no business entry")
        # one vocabulary for all three, so the card reads as one feature
        for label in ("Onboarding wizard", "Business wizard",
                      "Retirement wizard"):
            self.assertIn(label, text, f"Guided setup is missing {label!r}")
        # The Business TAB is hidden until a business exists, so this button
        # is the only discoverable route to creating one. It must be
        # unconditional: any gate here can strand a tenant with no path in.
        card = text[text.index("function GuidedSetupCard"):]
        card = card[:card.index("\n}")]
        self.assertNotIn("features?.business", card,
                         "the Business wizard button must not be gated — it "
                         "is the only way to make the hidden tab appear")

    def test_account_step_warns_before_moving_money(self):
        """Assignment moves every transaction on the account out of the
        personal side. The step must say so — it is the one place in the
        wizard where clicking fast does real damage."""
        text = self._spa("components/BusinessWizard.tsx").read_text(
            encoding="utf-8")
        self.assertIn("past and future", text)
        self.assertIn("Skip for now", text,
                      "the account step must be skippable")


class LedgerListsBusinessRowsLabelledTests(unittest.TestCase):
    """The personal Transactions page LISTS a business entity's rows, each
    labelled with the entity's name, and never counts them as household
    spend. It used to hide them outright — which made a business account's
    own history unreachable from the Accounts page (0 results) — and the
    aggregates already carry PERSONAL_ONLY_SQL, so hiding bought nothing.
    `scope=personal` restores the household-only list; `scope=business`
    is its complement.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"lv-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES ('bizchk','it1','Biz Checking',"
                "'depository','checking',5000)")
            add_txn(conn, TODAY, 100.0, "PERSONAL LUNCH", account="card")
            add_txn(conn, TODAY, 250.0, "BIZ HOSTING", account="bizchk")
            ent = entities.create_entity(conn, name="Acme LLC",
                                         structure="sole_prop")
            entities.assign_account(conn, "bizchk", ent["id"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.admin.close()

    def _search(self, **kw):
        from oikonome.web import data
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows, total, _, _, spend, _hits = data.search_transactions(conn, "", **kw)
        finally:
            conn.close()
        return rows, total, spend

    def _month(self, **kw):
        from oikonome.web import data
        conn = tenancy.tenant_connect(self.tid)
        try:
            return data.transactions(conn, TODAY.year, TODAY.month, **kw)
        finally:
            conn.close()

    def test_search_lists_business_rows_with_entity_name(self):
        rows, total, spend = self._search()
        by = {(r["payee"] or "").upper(): r for r in rows}
        self.assertIn("PERSONAL LUNCH", by)
        self.assertIn("BIZ HOSTING", by,
                      "a business account's rows must be findable on the ledger")
        self.assertEqual(by["BIZ HOSTING"]["entity"], "Acme LLC")
        self.assertIsNone(by["PERSONAL LUNCH"]["entity"])
        self.assertEqual(total, 2)

    def test_search_total_is_household_money(self):
        """The headline total beside the count is household money like every
        other figure; the business rows are counted, not summed — unless
        the business scope was asked for."""
        from oikonome.web import data
        conn = tenancy.tenant_connect(self.tid)
        try:
            _, total, amount_sum, _, spend, _hits = data.search_transactions(conn, "")
            _, btotal, bsum, _, _, _hits = data.search_transactions(conn, "",
                                                             scope="business")
        finally:
            conn.close()
        self.assertEqual(total, 2)
        self.assertAlmostEqual(amount_sum, 100.0)
        self.assertEqual(spend["biz_count"], 1)
        self.assertEqual(btotal, 1)
        self.assertAlmostEqual(bsum, 250.0)

    def test_business_rows_never_count_as_household_spend(self):
        rows, _, spend = self._search()
        by = {(r["payee"] or "").upper(): r for r in rows}
        self.assertTrue(by["PERSONAL LUNCH"]["counts_as_spend"])
        self.assertFalse(by["BIZ HOSTING"]["counts_as_spend"])
        self.assertEqual(spend["count"], 1)
        self.assertAlmostEqual(spend["sum"], 100.0)
        month = {(r["payee"] or "").upper(): r for r in self._month()}
        self.assertFalse(month["BIZ HOSTING"]["counts_as_spend"])

    def test_month_browse_lists_business_rows_with_entity_name(self):
        by = {(r["payee"] or "").upper(): r for r in self._month()}
        self.assertIn("BIZ HOSTING", by)
        self.assertEqual(by["BIZ HOSTING"]["entity"], "Acme LLC")
        self.assertIsNone(by["PERSONAL LUNCH"]["entity"])

    def test_personal_scope_hides_business_rows(self):
        rows, total, _ = self._search(scope="personal")
        payees = {(r["payee"] or "").upper() for r in rows}
        self.assertEqual(payees, {"PERSONAL LUNCH"})
        self.assertEqual(total, 1)
        payees = {(r["payee"] or "").upper() for r in self._month(scope="personal")}
        self.assertEqual(payees, {"PERSONAL LUNCH"})

    def test_business_scope_lists_only_business_rows(self):
        rows, total, _ = self._search(scope="business")
        self.assertEqual({(r["payee"] or "").upper() for r in rows},
                         {"BIZ HOSTING"})
        payees = {(r["payee"] or "").upper() for r in self._month(scope="business")}
        self.assertEqual(payees, {"BIZ HOSTING"})

    def test_asking_for_the_business_account_lists_its_rows(self):
        """The Accounts page links a business account to the ledger filtered
        to that account — that link showed 0 results."""
        rows, total, _ = self._search(account_id="bizchk")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["entity"], "Acme LLC")

    def test_combined_toggle_makes_business_rows_household(self):
        """The combined view is the one place business money IS household
        money — then the rows count as spend and personal scope keeps them."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            cfg["combine_entities"] = True
            budget.save_config(conn, cfg)
            conn.commit()
        finally:
            conn.close()
        rows, _, spend = self._search(scope="personal")
        by = {(r["payee"] or "").upper(): r for r in rows}
        self.assertIn("BIZ HOSTING", by)
        self.assertTrue(by["BIZ HOSTING"]["counts_as_spend"])
        self.assertEqual(by["BIZ HOSTING"]["entity"], "Acme LLC",
                         "the label is the row's ownership, not the toggle")
        self.assertEqual(spend["count"], 2)
        rows, _, _ = self._search(scope="business")
        self.assertEqual({(r["payee"] or "").upper() for r in rows},
                         {"BIZ HOSTING"})


class WizardResumeTests(unittest.TestCase):
    """The wizard creates the entity at the end of step 2, so a run
    abandoned on step 3 leaves a real business with no accounts assigned.
    Re-opening the wizard must finish that one rather than create a
    SECOND business."""

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_wizard_accepts_a_resume_entity(self):
        w = self._src("components/BusinessWizard.tsx")
        self.assertIn("resume?:", w)
        self.assertIn("useState(resume ? 2 : 0)", w,
                      "a resumed run must start on the accounts step")
        self.assertIn("resume?.id ?? null", w,
                      "a resumed run must reuse the existing entity id")

    def test_business_page_resumes_instead_of_creating_a_second(self):
        b = self._src("pages/Business.tsx")
        self.assertIn("resume={entity", b,
                      "the page must hand the wizard the existing entity")
        self.assertIn("Continue guided setup", b,
                      "there must be a visible way back into the wizard")


class BusinessSetupRouteTests(unittest.TestCase):
    """The guided flow is its own route with PERSISTED marks now.

    A card inside the Business page holding completion in React state
    forgets everything on a refresh: no resume, and a prompt that never
    ends. Same shape as the Welcome wizard.
    """

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_route_exists_and_is_registered(self):
        self.assertTrue((__import__("pathlib").Path(__file__).resolve()
                         .parents[2] / "webapp" / "src" / "pages"
                         / "BusinessSetup.tsx").exists())
        self.assertIn('path="/business/setup"', self._src("App.tsx"))

    def test_marks_are_persisted_not_local_state(self):
        w = self._src("components/BusinessWizard.tsx")
        self.assertIn("biz_wizard_steps", w,
                      "step completion must persist in tenant config")
        self.assertIn("firstOpen", w,
                      "the flow must resume at the first unfinished step")

    def test_entry_points_use_the_route(self):
        b = self._src("pages/Business.tsx")
        s = self._src("pages/Settings.tsx")
        self.assertIn('to="/business/setup"', b)
        self.assertIn('to="/business/setup"', s)
        self.assertNotIn('/business?wizard=1', s,
                         "Settings must link the route, not the old flag")

    def test_retirement_setup_is_a_route_too(self):
        """All three wizards end up the same shape: own route, persisted
        marks, resumable — not a component holding progress in memory."""
        import pathlib as _p
        self.assertTrue((_p.Path(__file__).resolve().parents[2] / "webapp"
                         / "src" / "pages" / "RetirementSetup.tsx").exists())
        self.assertIn('path="/retirement/setup"', self._src("App.tsx"))
        s = self._src("pages/Settings.tsx")
        self.assertIn('to="/retirement/setup"', s)
        self.assertNotIn("/retirement?wizard=1", s,
                         "Settings must link the route, not the old flag")

    def test_setup_route_is_never_gated(self):
        """/business/setup is the ONLY way to create a business, and the
        Business tab is hidden until one exists (has_business). Gating the way
        in on having already been in would make the tab unreachable — so this
        route carries no entitlement check at all."""
        p = self._src("pages/BusinessSetup.tsx")
        self.assertNotIn("features?.business", p,
                         "the setup route must not be entitlement-gated")
        self.assertNotIn("Books", p,
                         "the setup route must not name a plan")


class TransientOfflineTests(unittest.TestCase):
    """Coming back to a backgrounded mobile tab must not blank the app.

    Leaving the browser and returning fires a focus-refetch while the radio
    is still asleep; that one request fails. An error branch that replaces
    the whole UI then shows "Can't reach the server — is it running?" and
    needs a manual reload, even though good data was already cached.
    """

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_error_card_only_when_there_is_no_data(self):
        app = self._src("App.tsx")
        i = app.index("Can't reach the server")
        window = app[max(0, i - 400):i]
        self.assertIn("if (!me.data)", window,
                      "the offline card must not replace a rendered app on a "
                      "transient refetch failure")

    def test_queries_retry_and_refetch_on_reconnect(self):
        m = self._src("main.tsx")
        self.assertIn("refetchOnReconnect: true", m)
        self.assertIn("retry: 3", m,
                      "one retry is not enough for a phone waking up")

    def test_auth_states_still_hard_stop(self):
        """Suspension, an add-on's lockouts and auth are real states, not
        connectivity — they must still take over the screen."""
        app = self._src("App.tsx")
        for marker in ("SuspendedPage", "ext.lockouts", "AuthError"):
            self.assertIn(marker, app)


class BusinessAccountsListTests(unittest.TestCase):
    """The entity's Accounts list shows THIS business's accounts.

    An assignment PICKER doing duty as a status DISPLAY renders every
    account in the tenant, so a page about one business lists a household's
    worth of unrelated accounts. The picker still exists; it is a
    deliberate action.
    """

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_default_view_lists_only_assigned_accounts(self):
        b = self._src("pages/Business.tsx")
        self.assertIn("{!picking ? (", b,
                      "the default view must not be the picker")
        self.assertIn("assigned.map(a =>", b,
                      "the default list must iterate ASSIGNED accounts")

    def test_assignment_is_a_deliberate_action(self):
        b = self._src("pages/Business.tsx")
        self.assertIn("Assign an account…", b)
        self.assertIn("setPicking(true)", b)

    def test_unassign_still_reachable_and_warns(self):
        """Removing an account returns real money to personal, so it keeps
        its confirmation."""
        b = self._src("pages/Business.tsx")
        self.assertIn("transactions return to personal", b)


class BusinessTabsTests(unittest.TestCase):
    """Business has its own sub-menu.

    Everything on one long page is unnavigable — the section components
    already exist; what is missing is any way to see one at a time.
    """

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_five_tabs_in_the_agreed_order(self):
        b = self._src("pages/Business.tsx")
        for key in ("overview", "transactions", "books", "tax", "log"):
            self.assertIn(f'key: "{key}"', b, f"missing the {key} tab")
        self.assertLess(b.index('key: "overview"'), b.index('key: "books"'),
                        "Overview must lead the sub-menu")

    def test_tab_lives_in_the_url(self):
        """A tab must be linkable and survive a reload — the same lesson the
        wizards taught when their position lived in React state."""
        b = self._src("pages/Business.tsx")
        self.assertIn("useSearchParams", b)
        self.assertIn('sp.set("tab"', b)

    def test_sections_are_grouped_by_task_not_by_render_order(self):
        b = self._src("pages/Business.tsx")
        books = b.index('tab === "books"')
        tax = b.index('tab === "tax"')
        self.assertIn("BooksSection", b[books:tax])
        self.assertIn("BalanceSheetSection", b[books:tax])
        self.assertIn("EquitySection", b[books:tax])
        # bound by the NEXT tab marker, not a fixed window — cards added to
        # the Tax tab (set-aside, year-end package) push these past any
        # arbitrary character count, which is exactly what happened
        tail = b[tax:b.index('tab === "log"')]
        for c in ("TaxSection", "VendorSection", "ComplianceSection"):
            self.assertIn(c, tail, f"{c} belongs on the Tax tab")


class OverviewDashboardTests(unittest.TestCase):
    """The four numbers the Overview tab exists to answer.

    A real dashboard, not a redirect and not an empty-when-clean inbox:
    "how is the business doing" is one of the jobs this section is opened
    for.
    """

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_all_four_tiles_present(self):
        b = self._src("pages/Business.tsx")
        for label in ("Profit this year", "Cash in the business",
                      "Next deadline", "Needs attention"):
            self.assertIn(label, b, f"dashboard is missing {label!r}")

    def test_dashboard_leads_the_overview_tab(self):
        """A dashboard below the config it summarizes is not a dashboard."""
        b = self._src("pages/Business.tsx")
        self.assertLess(b.index("<OverviewSection entity={entity} />"),
                        b.index("<h4 style={{ marginBottom: \".3rem\" }}>Accounts</h4>"),
                        "the dashboard must render above Accounts")

    def test_needs_attention_uses_the_suggestions_endpoint(self):
        """The count is real work outstanding, not a guess."""
        b = self._src("pages/Business.tsx")
        c = self._src("api/client.ts")
        self.assertIn("bizSuggestions", b)
        self.assertIn("/suggestions", c)

    def test_month_profit_respects_the_sign_convention(self):
        """positive = money out, house-wide. Getting this backwards would
        show a profitable month as a loss."""
        b = self._src("pages/Business.tsx")
        i = b.index("monthProfit")
        self.assertIn("s - x.amount", b[i:i + 400])


class BusinessSettingsTabTests(unittest.TestCase):
    """Entity setup lives behind a de-emphasized Settings tab.

    Entity details, account assignment and members on the main page
    permanently leave the workspace looking half-configured. Setup is
    something you finish, not a place you work — the same reasoning that
    stops the wizard prompt nagging once setup is complete.
    """

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_settings_is_not_in_the_primary_tab_row(self):
        b = self._src("pages/Business.tsx")
        self.assertNotIn('key: "settings"', b,
                         "Settings must stay OUT of BIZ_TABS — it is "
                         "de-emphasized, not a peer of the work tabs")
        self.assertIn('onTab("settings")', b, "…but must still be reachable")

    def test_config_sections_are_behind_settings(self):
        b = self._src("pages/Business.tsx")
        # both config sections must sit between the settings marker and the
        # first work tab — a fixed-size window is not enough, the Accounts
        # block alone is longer than 4k
        start = b.index('tab === "settings" && <>')
        end = b.index('tab === "books" && <>')
        block = b[start:end]
        self.assertIn("Accounts</h4>", block)
        self.assertIn("Members</h4>", block)

    def test_entity_edit_is_config_too(self):
        """Edit/Archive/Delete are setup, not daily work.

        Asserted on the shape of the region rather than one exact source
        line — the lifecycle controls moved into a flex row under the same
        guard during the redesign, which changed nothing about
        where they render or who may press them."""
        b = self._src("pages/Business.tsx")
        # the lifecycle block, from its guard to the next settings-only block
        start = b.index('{tab === "settings" && (')
        end = b.index('tab === "settings" && editing && !ro')
        block = b[start:end]
        self.assertIn("Edit details", block)
        self.assertIn("Archive", block)
        # editing an entity is an owner write, inside the settings tab
        self.assertIn("!ro && <button", block)
        self.assertIn('tab === "settings" && editing && !ro', b)


class ReviewQueueTests(unittest.TestCase):
    """The Schedule C review queue — the suggestions endpoint's first real
    consumer, and a dedicated queue rather than an inline prompt."""

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_queue_exists_on_the_transactions_tab(self):
        b = self._src("pages/Business.tsx")
        self.assertIn("function ReviewQueue", b)
        self.assertIn('tab === "transactions" && <ReviewQueue', b)

    def test_source_is_surfaced_so_trust_is_visible(self):
        """A learned suggestion and a weak category guess must not look the
        same — one is the user's own prior decision."""
        b = self._src("pages/Business.tsx")
        i = b.index("function ReviewQueue")
        block = b[i:i + 4000]
        self.assertIn('s.source === "merchant"', block)
        self.assertIn('s.source === "category"', block)

    def test_nothing_commits_without_a_chosen_line(self):
        # the button also gates on the viewer role now — the invariant
        # under test (no line chosen ⇒ not committable) is unchanged
        b = self._src("pages/Business.tsx")
        i = b.index("function ReviewQueue")
        self.assertRegex(b[i:i + 4000],
                         r"disabled=\{[^}]*\|\| !chosen \|\| ro\}",
                         "a row with no line selected must not be committable")


class TaxSetAsideTests(unittest.TestCase):
    """Owed vs what's actually in the business.

    "Put aside a quarter of profit" is the rule of thumb people arrive
    with. The app already computes the REAL figure from profit, SE tax and
    deductions, so a flat percentage would be less accurate than what it
    knows — the effective percentage is shown instead, which answers the
    same question without inventing a rule.
    """

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_card_is_on_the_tax_tab(self):
        b = self._src("pages/Business.tsx")
        self.assertIn("function TaxSetAside", b)
        i = b.index('tab === "tax" && <>')
        self.assertIn("<TaxSetAside", b[i:i + 400])

    def test_rate_is_derived_not_hardcoded(self):
        """A flat 25% would be a downgrade from the real computation."""
        b = self._src("pages/Business.tsx")
        i = b.index("function TaxSetAside")
        block = b[i:i + 2500]
        self.assertIn("owed / profit", block)
        self.assertNotIn("0.25", block)

    def test_the_card_says_which_question_it_is_answering(self):
        """"Saved" means one of two different things, and the card must say
        which. With a nominated reserve account it is that balance — "is it
        ring-fenced". Without one it is all business cash — "is the money
        there somewhere", which is the weaker claim and must not be allowed
        to read as the stronger one."""
        b = self._src("pages/Business.tsx")
        i = b.index("function TaxSetAside")
        block = b[i:i + 4000]
        self.assertIn("is it ring-fenced", block)
        self.assertIn("is the money\n              there", block)
        self.assertIn("const ringFenced", block)

    def test_a_dangling_nomination_falls_back_and_says_so(self):
        """An account purged or reassigned out of the business must not
        make the card read $0 saved — a scarier number than the truth."""
        b = self._src("pages/Business.tsx")
        i = b.index("function TaxSetAside")
        block = b[i:i + 4000]
        self.assertIn("const stale =", block)
        self.assertIn("no longer", block)

    def test_only_the_businesss_own_accounts_can_be_nominated(self):
        """Nominating a personal account — or another entity's — would
        misreport the set-aside for both, so the server refuses it."""
        import inspect

        from oikonome.engine import entities
        src = inspect.getsource(entities.update_entity)
        self.assertIn("AND entity_id = %s", src)
        self.assertIn("must be an account assigned to this", src)
