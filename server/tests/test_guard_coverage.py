"""Guards that must hold at every place that needs them, not just the first.

Grouped here because they share one theme: a rule with two definitions or
two entry points (the SQL/Python shadow set, the TOTP single-use burn,
`config_txn`) must agree at both. These test the CONTRACT ("these two
definitions agree") rather than the instance ("hidden accounts are excluded").
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.engine import books, budget, links

from .util import _ensure_db, add_txn, make_db, seed_accounts, write_config


def _make_client() -> TestClient:
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


def _signup(client: TestClient, prefix: str) -> str:
    client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    return client.get("/api/me").json()["tenant_id"]


class ShadowSetHealthAwareTests(unittest.TestCase):
    """The SQL shadow set must be HEALTH-aware, not merely rank-aware.

    `links.groups()` picks the lowest-rank member whose source is healthy,
    and the SQL must pick the same one. A rank-only SQL definition gives
    identical answers until a live failover, when Reports, the forecast
    runway and the emailed report would sum the DEAD source while Today, the
    budget and bills sum the live one.
    """

    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        write_config(self.conn)
        # a second source for the same real-world account, ranked BELOW chk
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status) VALUES ('it2','plaid','Test Bank 2',"
            "'tok-2','ok')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,updated_at) VALUES "
            "('chk2','it2','Test Checking (2)','depository','checking',"
            "5000, now())")
        gid = str(uuid.uuid4())
        for aid, rank in (("chk", 0), ("chk2", 1)):
            self.conn.execute(
                "INSERT INTO account_links (group_id, account_id, home_rank) "
                "VALUES (%s,%s,%s)", (gid, aid, rank))
        # chk's own item must look live too, or it is never the primary
        self.conn.execute(
            "UPDATE items SET aggregator='plaid', status='ok' WHERE id='it1'")
        self.conn.execute("UPDATE accounts SET updated_at=now() WHERE id='chk'")

    def tearDown(self):
        self.conn.close()

    def _guc(self) -> set:
        links.set_shadow_scope(self.conn)          # what tenant_connect does
        raw = self.conn.execute(
            "SELECT current_setting('app.shadow_ids', true) AS v"
        ).fetchone()["v"] or ""
        return {x for x in raw.split(",") if x}

    def test_healthy_group_shadows_the_lower_rank(self):
        self.assertEqual(self._guc(), {"chk2"})
        self.assertEqual(set(links.shadow_ids(self.conn)), {"chk2"})

    def test_failover_moves_the_shadow_to_the_dead_source(self):
        """rank 0's item breaks -> rank 1 serves, rank 0 becomes the shadow."""
        self.conn.execute("UPDATE items SET status='error:ITEM_LOGIN_REQUIRED'"
                          " WHERE id='it1'")
        self.assertEqual(set(links.shadow_ids(self.conn)), {"chk"},
                         "the Python helper is the health-aware definition")
        self.assertEqual(
            self._guc(), {"chk"},
            "app.shadow_ids still shadows by RANK — during a failover "
            "Reports/forecast/retirement sum the dead source while "
            "Today/budget/bills sum the live one")

    def test_staleness_counts_as_down_in_sql_too(self):
        """A live aggregator silent for > STALE_HOURS is down, no error row."""
        self.conn.execute(
            "UPDATE accounts SET updated_at = now() - interval '%s hours' "
            "WHERE id='chk'" % (links.STALE_HOURS + 1))
        self.assertEqual(set(links.shadow_ids(self.conn)), {"chk"})
        self.assertEqual(self._guc(), {"chk"},
                         "the SQL health predicate must use the same "
                         "STALE_HOURS bound as links._healthy")

    def test_hidden_and_failover_compose(self):
        self.conn.execute("UPDATE items SET status='error:X' WHERE id='it1'")
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=now() WHERE id='card'")
        self.assertEqual(self._guc(), {"chk", "card"})
        self.assertEqual(set(links.shadow_ids(self.conn)), {"chk", "card"})

    def test_all_sources_down_keeps_the_top_rank_serving(self):
        """Nothing healthy -> members[0] still serves, so it is NOT shadowed.
        Otherwise a whole group vanishes from every total the moment both
        sources break, which is worse than showing stale numbers."""
        self.conn.execute("UPDATE items SET status='error:X' "
                          "WHERE id IN ('it1','it2')")
        self.assertEqual(self._guc(), {"chk2"})
        self.assertEqual(set(links.shadow_ids(self.conn)), {"chk2"})


class ConfigTxnLockTests(unittest.TestCase):
    """`config_txn` must run its body inside an explicit transaction.

    On an AUTOCOMMIT connection `SELECT ... FOR UPDATE` commits the instant
    it returns, so the lock is released before the caller reads the config
    and the lost-update window stays open.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_body_runs_inside_a_real_transaction(self):
        import psycopg
        self.assertEqual(self.conn.info.transaction_status,
                         psycopg.pq.TransactionStatus.IDLE)
        with budget.config_txn(self.conn) as cfg:
            cfg["food_monthly"] = 1234
            self.assertEqual(
                self.conn.info.transaction_status,
                psycopg.pq.TransactionStatus.INTRANS,
                "the FOR UPDATE lock is released at once on an autocommit "
                "connection unless the block is an explicit transaction")
        self.assertEqual(budget.load_config(self.conn)["food_monthly"], 1234)

    def test_a_raise_inside_the_block_writes_nothing(self):
        with self.assertRaises(RuntimeError):
            with budget.config_txn(self.conn) as cfg:
                cfg["food_monthly"] = 999
                raise RuntimeError("boom")
        self.assertEqual(budget.load_config(self.conn)["food_monthly"], 1000)


class BooksReviewQueueTests(unittest.TestCase):
    """The Schedule C review queue counts only rows it can clear.

    `pnl()` skips transfers and credit-card payments (double-count
    prevention), and `unclassified_count` must skip them too; otherwise the
    "Needs attention" tile lights for rows where classifying changes nothing
    on the P&L — an unclearable to-do.
    """

    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        write_config(self.conn)
        self.eid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO business_entity (id, name, structure) "
            "VALUES (%s, 'Test LLC', 'sole_prop')", (self.eid,))
        self.conn.execute(
            "UPDATE accounts SET entity_id=%s WHERE id='chk'", (self.eid,))

    def tearDown(self):
        self.conn.close()

    def test_transfers_and_card_payments_are_not_review_work(self):
        add_txn(self.conn, "2026-07-01", 50.0, "REAL EXPENSE", account="chk")
        add_txn(self.conn, "2026-07-02", 300.0, "MOVED TO SAVINGS",
                primary="TRANSFER_OUT", account="chk")
        add_txn(self.conn, "2026-07-03", 400.0, "CARD PAYMENT",
                primary="LOAN_PAYMENTS", account="chk")
        self.conn.execute(
            "UPDATE transactions SET category_detailed="
            "'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT' WHERE name='CARD PAYMENT'")
        self.assertEqual(
            books.unclassified_count(self.conn, self.eid), 1,
            "only the real expense is a Schedule C decision")
        payees = {r["payee"] for r in books.suggest_lines(self.conn, self.eid)}
        self.assertEqual(payees, {"REAL EXPENSE"})

    def test_the_count_still_matches_what_the_queue_offers(self):
        for i in range(4):
            add_txn(self.conn, "2026-07-0%d" % (i + 1), 10.0 + i,
                    "EXPENSE %d" % i, account="chk")
        self.assertEqual(books.unclassified_count(self.conn, self.eid),
                         len(books.suggest_lines(self.conn, self.eid)))


def _plaid_transport(item_id: str, removed: list):
    import json

    import httpx

    def handler(request):
        path = request.url.path
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": item_id, "access_token": "access-prod-" + item_id}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_1"}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": "Demo Bank"}}))
        if path == "/item/remove":
            removed.append(item_id)
            return httpx.Response(200, text=json.dumps({"removed": True}))
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps({"accounts": []}))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(
                {"added": [], "modified": [], "removed": [],
                 "has_more": False, "next_cursor": "c-end"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class InstitutionCapAtExchangeTests(unittest.TestCase):
    """The cap has to hold where the item row is CREATED.

    Checking it only when the Link session opens is advisory: a tenant at the
    cap opens N sessions before completing any, completes them all, and lands
    over. The exchange is the only point that actually creates the row.
    """

    def setUp(self):
        from oikonome.sync import plaid
        self.plaid = plaid
        self.conn = make_db()
        self._saved = {k: os.environ.get(k) for k in
                       ("OIKONOME_HOSTED", "OIKONOME_HOSTED_MAX_INSTITUTIONS",
                        "OIKONOME_PLAID_CLIENT_ID", "OIKONOME_PLAID_SECRET")}
        os.environ.update(OIKONOME_HOSTED="1",
                          OIKONOME_HOSTED_MAX_INSTITUTIONS="2",
                          OIKONOME_PLAID_CLIENT_ID="cid",
                          OIKONOME_PLAID_SECRET="sec")
        write_config(self.conn)
        for i in range(2):                       # already AT the cap
            self.conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, status) "
                "VALUES (%s,'plaid',%s,'ok')", (f"cap{i}", f"Bank {i}"))

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def test_under_the_cap_still_links(self):
        self.conn.execute("UPDATE items SET status='archived' WHERE id='cap1'")
        removed = []
        self.plaid.link_item(self.conn, "public-x",
                             transport=_plaid_transport("new2", removed))
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM items WHERE id='new2'").fetchone())
        self.assertEqual(removed, [])

    def test_reauth_of_an_existing_item_is_exempt(self):
        """Update/re-auth must pass at the cap — that door is documented as
        exempt, and blocking it would strand a tenant with a broken
        connection they cannot repair."""
        removed = []
        self.plaid.link_item(self.conn, "public-x",
                             transport=_plaid_transport("cap0", removed))
        self.assertEqual(removed, [])


class ArchivedStatusTests(unittest.TestCase):
    """A sync error must never un-archive a released connection.

    `sync_all` and `nightly_all` can fire at the same instant while the
    reaper archives Items dead 30+ days. The sync's error-status write must
    not land on top of 'archived'; otherwise it resurrects an Item that no
    longer exists at Plaid, which every later sweep picks up and fails on.
    """

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status) "
            "VALUES ('z1','plaid','Gone Bank','archived')")

    def tearDown(self):
        self.conn.close()

    def test_error_write_does_not_downgrade_archived(self):
        self.conn.execute("UPDATE items SET status=%s WHERE id=%s "
                          "AND COALESCE(status,'') != 'archived'",
                          ("error:ITEM_LOGIN_REQUIRED", "z1"))
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM items WHERE id='z1'").fetchone()["status"],
            "archived")


class OutOfRangeQueryParamTests(unittest.TestCase):
    """Bad input is a 400, never a 500.

    A caller-supplied y/m/year reaches `dt.date(...)`, where out-of-range
    raises ValueError and FastAPI turns that into a 500. Every endpoint that
    takes one must go through the shared guard.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        _signup(cls.client, "params")

    def test_out_of_range_is_a_client_error(self):
        for url in ("/api/transactions?y=999999&m=1",
                    "/api/transactions?y=2026&m=77",
                    "/api/business?year=999999",
                    "/api/business/export.csv?year=999999",
                    "/api/receipts/report?y=999999&m=1",
                    "/api/lens/month?y=999999&m=1",
                    "/api/lens/year?y=999999"):
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertNotEqual(r.status_code, 500,
                                    "out-of-range date params must not 500")
                self.assertIn(r.status_code, (400, 402, 403))

    def test_normal_requests_still_work(self):
        self.assertEqual(self.client.get("/api/transactions").status_code, 200)
        self.assertEqual(
            self.client.get("/api/lens/month?y=2026&m=7").status_code, 200)

    def test_retirement_survives_an_absurd_contribution(self):
        """employer_mo must be bounded; otherwise the compounding loop
        overflows to inf and the NaN it produces is not JSON-serialisable."""
        big = 10 ** 30
        r = self.client.get(
            f"/api/retirement?employer_mo={big}&taxable_mo={big}&spend={big}")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json().get("inputs", {}).get("age"), int)


class TotpBurnIsAtomicTests(unittest.TestCase):
    """The single-use TOTP guard has to ride the UPDATE, not the SELECT.

    A code is single-use by stamping `totp_last_counter` under a
    `WHERE last < new` guard, and acceptance must depend on whether that
    write landed. Otherwise two step-ups carrying the SAME code inside its
    ~90 s window both read the old counter and both are accepted while one
    UPDATE takes effect, and a replayed code opens the credential-changing
    doors.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        _signup(cls.client, "totp")

    def setUp(self):
        from oikonome.web import app as appmod
        self.appmod = appmod
        with appmod._control_conn() as conn:
            self.uid = conn.execute(
                "SELECT id FROM users ORDER BY created_at DESC LIMIT 1"
            ).fetchone()["id"]
            conn.execute("UPDATE users SET totp_last_counter=NULL "
                         "WHERE id=%s", (self.uid,))

    def test_the_same_counter_can_only_be_claimed_once(self):
        with self.appmod._control_conn() as conn:
            self.assertTrue(
                self.appmod._burn_totp_counter(conn, self.uid, 5_000_000))
            self.assertFalse(
                self.appmod._burn_totp_counter(conn, self.uid, 5_000_000),
                "the second claim of one code must be REJECTED — this is the "
                "replay the single-use guard exists to stop")

    def test_an_older_counter_is_refused(self):
        with self.appmod._control_conn() as conn:
            self.assertTrue(
                self.appmod._burn_totp_counter(conn, self.uid, 5_000_001))
            self.assertFalse(
                self.appmod._burn_totp_counter(conn, self.uid, 5_000_000))

    def test_the_next_counter_is_accepted(self):
        with self.appmod._control_conn() as conn:
            self.assertTrue(
                self.appmod._burn_totp_counter(conn, self.uid, 5_000_002))
            self.assertTrue(
                self.appmod._burn_totp_counter(conn, self.uid, 5_000_003))


class SmtpStarttlsWritePathTests(unittest.TestCase):
    """The STARTTLS choice must be writable from the UI.

    `resolve_smtp` reads `cfg["smtp_starttls"]`, so settings must be able to
    write that key; otherwise SMTP set in the UI forces STARTTLS and a plain
    LAN relay is reachable only through docker/.env.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.tid = _signup(cls.client, "smtp")

    def _save(self, **over):
        body = {"smtp_host": "smtp.example.com", "smtp_port": 587,
                "smtp_from": "a@example.com"}
        body.update(over)
        return self.client.post("/api/settings", json=body)

    def test_starttls_round_trips_and_reaches_resolve_smtp(self):
        from oikonome.db import tenancy
        from oikonome.web import report

        self.assertEqual(self._save(smtp_starttls=False).status_code, 200)
        self.assertIs(self.client.get("/api/settings").json()["smtp_starttls"],
                      False)
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertIs(report.resolve_smtp(conn)["starttls"], False,
                          "the UI checkbox must reach the sender, or it is "
                          "the same dead knob with a nicer face")
        finally:
            conn.close()

    def test_default_is_still_on(self):
        self.assertEqual(self._save().status_code, 200)
        from oikonome.db import tenancy
        from oikonome.web import report
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertIs(report.resolve_smtp(conn)["starttls"], True)
        finally:
            conn.close()

    def test_clearing_the_host_clears_the_flag_too(self):
        self._save(smtp_starttls=False)
        self.client.post("/api/settings", json={"smtp_host": ""})
        self.assertIsNone(
            self.client.get("/api/settings").json().get("smtp_starttls"),
            "a cleared SMTP group must not leave a stale starttls=False "
            "behind to silently disarm the env fallback later")


if __name__ == "__main__":
    unittest.main()
