"""Multi-source accounts: suggestion heuristic, link lifecycle,
health-aware effective primary, shadow suppression in spend + net worth,
edge-triggered failover alert, SimpleFIN per-org item split + type
guess."""

import datetime as dt
import unittest

from oikonome.engine import budget, links

from .util import TODAY, add_txn, make_db, write_config


def _mk_source(conn, item_id, aggregator, acct_id, mask="1234",
               typ="credit", bal=100.0, name=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING",
        (item_id, aggregator, item_id))
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                 balance_current, updated_at)
           VALUES (%s,%s,%s,%s,'credit card',%s,%s,now())""",
        (acct_id, item_id, name or acct_id, typ, mask, bal))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _mk_source(self.conn, "pl-1", "plaid", "pl-cc", bal=120.0,
                   name="Acme Card")
        _mk_source(self.conn, "sf-1", "simplefin-org", "sf-cc", bal=118.0,
                   name="Acme Card (bridge)")

    def tearDown(self):
        self.conn.close()

    def test_suggestion_same_mask_type_across_items(self):
        s = links.suggestions(self.conn)
        self.assertEqual(len(s), 1)
        pair = {s[0]["a"]["id"], s[0]["b"]["id"]}
        self.assertEqual(pair, {"pl-cc", "sf-cc"})
        # dismissed pairs stop suggesting
        self.assertEqual(
            links.suggestions(self.conn, dismissed=[s[0]["key"]]), [])

    def test_link_shadows_lower_rank_everywhere(self):
        links.create(self.conn, ["pl-cc", "sf-cc"])
        self.assertEqual(links.shadow_ids(self.conn), ["sf-cc"])
        # suggestions stop once linked
        self.assertEqual(links.suggestions(self.conn), [])
        # spend math ignores the shadow's transactions
        add_txn(self.conn, TODAY, 50.0, "SHADOW SPEND", account="sf-cc")
        add_txn(self.conn, TODAY, 20.0, "REAL SPEND", account="pl-cc")
        rows = budget._spend_rows(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))
        payees = {r["payee"] for r in rows}
        self.assertIn("REAL SPEND", payees)
        self.assertNotIn("SHADOW SPEND", payees)
        # net worth counts the linked account once (primary's balance)
        from oikonome.engine.reporting import _live_accounts
        bals = {r["id"]: r["bal"] for r in _live_accounts(self.conn)}
        self.assertIn("pl-cc", bals)
        self.assertNotIn("sf-cc", bals)

    def test_failover_promotes_next_healthy_and_alerts_once(self):
        links.create(self.conn, ["pl-cc", "sf-cc"])
        # primary's source breaks → backup becomes effective primary
        self.conn.execute(
            "UPDATE items SET status='error:ITEM_LOGIN_REQUIRED' "
            "WHERE id='pl-1'")
        self.assertEqual(links.shadow_ids(self.conn), ["pl-cc"])
        due = links.failover_pending(self.conn)
        self.assertEqual([(d["down"], d["using"]) for d in due],
                         [("pl-cc", "sf-cc")])
        # pending doesn't stamp — the worker marks after a successful send;
        # once marked, the next sweep is silent (edge-triggered)
        links.mark_failover_alerted(self.conn, [d["group_id"] for d in due])
        self.assertEqual(links.failover_pending(self.conn), [])
        # recovery: favorite takes back over, edge re-arms
        self.conn.execute("UPDATE items SET status='ok' WHERE id='pl-1'")
        self.assertEqual(links.shadow_ids(self.conn), ["sf-cc"])
        self.assertEqual(links.failover_pending(self.conn), [])
        self.conn.execute(
            "UPDATE items SET status='error:down' WHERE id='pl-1'")
        self.assertEqual(len(links.failover_pending(self.conn)), 1)

    def test_stale_live_source_is_unhealthy(self):
        links.create(self.conn, ["pl-cc", "sf-cc"])
        self.conn.execute(
            "UPDATE accounts SET updated_at = now() - interval '3 days' "
            "WHERE id='pl-cc'")
        self.assertEqual(links.shadow_ids(self.conn), ["pl-cc"])

    def test_stale_collector_source_fails_over_to_plaid_balance(self):
        # A retirement plan fed by the nightly scrape (a plan-CSV item) and
        # by a balance-only Plaid link: the scrape is the richer primary,
        # but once it stops pushing the Plaid balance must take over
        # instead of the group freezing on a stale figure.
        _mk_source(self.conn, "plan", "plan_csv", "plan-401a",
                   mask=None, typ="investment", bal=58000.0)
        _mk_source(self.conn, "pl-2", "plaid", "pl-401a", mask="2468",
                   typ="investment", bal=58500.0)
        links.create(self.conn, ["plan-401a", "pl-401a"])
        self.assertEqual(links.shadow_ids(self.conn), ["pl-401a"])
        self.conn.execute(
            "UPDATE accounts SET updated_at = now() - interval '3 days' "
            "WHERE id='plan-401a'")
        self.assertEqual(links.shadow_ids(self.conn), ["plan-401a"])
        # the SQL twin every money aggregate reads must agree
        links.set_shadow_scope(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT current_setting('app.shadow_ids', true) AS s"
        ).fetchone()["s"], "plan-401a")

    def test_reorder_and_unlink(self):
        gid = links.create(self.conn, ["pl-cc", "sf-cc"])
        links.reorder(self.conn, gid, ["sf-cc", "pl-cc"])
        self.assertEqual(links.shadow_ids(self.conn), ["pl-cc"])
        with self.assertRaises(ValueError):
            links.reorder(self.conn, gid, ["sf-cc"])
        self.assertEqual(links.unlink(self.conn, gid), 2)
        self.assertEqual(links.shadow_ids(self.conn), [])

    def test_default_order_prefers_the_source_that_delivered_data(self):
        # balance-only Plaid link vs the collector that has the ledger:
        # the ledger goes first even though Plaid outranks it as an
        # aggregator; a linked pair with data on both sides keeps the
        # aggregator order
        _mk_source(self.conn, "plan", "plan_csv", "plan-401a",
                   mask="2468", typ="investment", bal=58000.0)
        _mk_source(self.conn, "pl-2", "plaid", "pl-401a", mask="2468",
                   typ="investment", bal=58500.0)
        self.conn.execute(
            """INSERT INTO holdings (account_id, symbol, quantity, price, value)
               VALUES ('plan-401a', 'FXAIX', 10, 100, 1000)""")
        self.assertEqual(
            links.default_order(self.conn, ["pl-401a", "plan-401a"]),
            ["plan-401a", "pl-401a"])

    def test_default_order_by_richness(self):
        self.assertEqual(
            links.default_order(self.conn, ["sf-cc", "pl-cc"]),
            ["pl-cc", "sf-cc"])

    def test_create_validation(self):
        with self.assertRaises(ValueError):
            links.create(self.conn, ["pl-cc"])
        with self.assertRaises(ValueError):
            links.create(self.conn, ["pl-cc", "nope"])
        links.create(self.conn, ["pl-cc", "sf-cc"])
        with self.assertRaises(ValueError):
            links.create(self.conn, ["pl-cc", "sf-cc"])


class SimplefinOrgSplitTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_orgs_become_items_and_types_guessed(self):
        import httpx

        from oikonome.sync import simplefin
        payload = {"accounts": [
            {"id": "L1", "name": "NORTHWIND HEALTH 401A RET. PLAN",
             "balance": "60000", "currency": "USD",
             "org": {"name": "Northwind Plan Services"}, "transactions": []},
            {"id": "D1", "name": "Sample Acme Card", "balance": "-43",
             "currency": "USD", "org": {"name": "Acme Card Services"},
             "transactions": []},
        ]}
        import json as _json
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=_json.dumps(payload)))
        simplefin.sync(self.conn, "sfin-x", "https://u:p@bridge.test/sf",
                       transport=transport)
        rows = {r["id"]: r for r in self.conn.execute(
            """SELECT a.id, a.type, i.id AS item, i.institution_name AS inst,
                      i.aggregator
               FROM accounts a JOIN items i ON i.id = a.item_id""")}
        self.assertEqual(rows["L1"]["inst"], "Northwind Plan Services")
        self.assertEqual(rows["D1"]["inst"], "Acme Card Services")
        self.assertNotEqual(rows["L1"]["item"], rows["D1"]["item"])
        self.assertEqual(rows["L1"]["aggregator"], "simplefin-org")
        self.assertEqual(rows["L1"]["type"], "investment")   # not checking!
        self.assertEqual(rows["D1"]["type"], "credit")
        # the bridge item keeps the token, holds no accounts
        bridge = self.conn.execute(
            "SELECT institution_name FROM items WHERE id='sfin-x'").fetchone()
        self.assertEqual(bridge["institution_name"], "SimpleFIN bridge")


if __name__ == "__main__":
    unittest.main()


class ShadowExclusionEverywhereTests(unittest.TestCase):
    """Linked shadow accounts must be excluded from ALL money aggregates
    (Spending, retirement, forecast), not just the verdict."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_spending_report_no_double_count(self):
        from oikonome.engine import reporting
        _mk_source(self.conn, "pl", "plaid", "pl-chk", typ="depository",
                   bal=1000.0, name="Checking A")
        _mk_source(self.conn, "sf", "simplefin-org", "sf-chk",
                   typ="depository", bal=1000.0, name="Checking A2")
        self.conn.execute("UPDATE accounts SET subtype='checking' "
                          "WHERE id IN ('pl-chk','sf-chk')")
        # SAME grocery charge visible through both sources
        add_txn(self.conn, TODAY, 200.0, "GROCERY", account="pl-chk",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 200.0, "GROCERY", account="sf-chk",
                primary="FOOD_AND_DRINK")
        links.create(self.conn, ["pl-chk", "sf-chk"])
        # SPEND_WHERE-driven spend total counts it once
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(t.amount),0) s FROM transactions t "
            f"WHERE {reporting.SPEND_WHERE}").fetchone()
        self.assertEqual(row["s"], 200.0)

    def test_retirement_and_forecast_no_double_count(self):
        _mk_source(self.conn, "pl", "plaid", "pl-card", typ="credit",
                   bal=500.0, name="Card")
        _mk_source(self.conn, "mx", "mx", "mx-card", typ="credit",
                   bal=500.0, name="Card2")
        links.create(self.conn, ["pl-card", "mx-card"])
        d = self.conn.execute(
            "SELECT COALESCE(SUM(GREATEST(a.balance_current,0)),0) d "
            "FROM accounts a WHERE a.type='credit' AND "
            "(NULLIF(current_setting('app.shadow_ids', true),'') IS NULL "
            " OR NOT (a.id = ANY(string_to_array("
            "current_setting('app.shadow_ids', true),','))))").fetchone()["d"]
        # 500 real linked card + 250 fixture card; the 500 shadow excluded
        # (would be 1250 double-counted)
        self.assertEqual(d, 750.0)

    def test_forecast_card_events_not_double_scheduled(self):
        """A linked multi-source card must schedule ONE payment event,
        not one per source (the debt sum was already de-duped)."""
        from oikonome.engine import forecast
        _mk_source(self.conn, "pl", "plaid", "pl-card", typ="credit",
                   bal=500.0, name="Card")
        _mk_source(self.conn, "mx", "mx", "mx-card", typ="credit",
                   bal=500.0, name="Card2")
        links.create(self.conn, ["pl-card", "mx-card"])
        fc = forecast.build(self.conn, today=TODAY)
        # the fixture card ($250) + one linked card ($500) = 2 payment events,
        # never the 3 an un-filtered event loop produces. Exactly one event
        # names the linked "Card".
        self.assertEqual(len(fc["card_events"]), 2, fc["card_events"])
        # the linked card's $500 balance appears exactly once
        linked = [e for e in fc["card_events"] if e[1] == -500.0]
        self.assertEqual(len(linked), 1, fc["card_events"])

    def test_income_counted_once_for_linked_checking(self):
        """Identical paycheck on primary + shadow checking counts once."""
        from oikonome.engine import reporting
        _mk_source(self.conn, "pl", "plaid", "pl-chk", typ="depository",
                   bal=1000.0, name="Checking A")
        _mk_source(self.conn, "sf", "simplefin-org", "sf-chk",
                   typ="depository", bal=1000.0, name="Checking A2")
        self.conn.execute("UPDATE accounts SET subtype='checking' "
                          "WHERE id IN ('pl-chk','sf-chk')")
        add_txn(self.conn, TODAY, -3000.0, "PAYROLL", account="pl-chk",
                primary="INCOME")
        add_txn(self.conn, TODAY, -3000.0, "PAYROLL", account="sf-chk",
                primary="INCOME")
        links.create(self.conn, ["pl-chk", "sf-chk"])
        total = sum(r["amt"] for r in reporting._income_by(self.conn, "month"))
        self.assertEqual(total, 3000.0)
