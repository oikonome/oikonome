"""The side doors — collector pushes, product syncs, linked sources —
keep the ledger's shape.

- Hiding the healthy favorite of a linked group fails over to the live
  copy instead of shadowing every member (the whole real account used to
  vanish from the money math).
- Two Amazon orders that share an amount but carry no order number are
  two orders; a split shipment charged twice is two rows; a row whose
  amount is junk is skipped, not the whole push.
- A push whose every row was rejected does not read as a fresh
  collector; a Coinbase push stamps one at all.
- A Plaid holdings replace is atomic: a bad row further down the
  response leaves yesterday's positions standing.
"""

import unittest
from unittest import mock

from oikonome.engine import links
from oikonome.sync import amazon_orders, coinbase_push, plaid

from .util import make_db, write_config


def _mk_source(conn, item_id, aggregator, acct_id, bal=100.0):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING",
        (item_id, aggregator, item_id))
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                 balance_current, updated_at)
           VALUES (%s,%s,%s,'credit','credit card','1234',%s,now())""",
        (acct_id, item_id, acct_id, bal))


class HiddenPrimaryTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _mk_source(self.conn, "pl-1", "plaid", "pl-disc")
        _mk_source(self.conn, "sf-1", "simplefin-org", "sf-disc")
        links.create(self.conn, ["pl-disc", "sf-disc"])

    def tearDown(self):
        self.conn.close()

    def test_hiding_the_favorite_fails_over_instead_of_blacking_out(self):
        self.assertEqual(links.shadow_ids(self.conn), ["sf-disc"])
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=now() WHERE id='pl-disc'")
        # the hidden favorite is the only shadow; the live backup serves
        self.assertEqual(links.shadow_ids(self.conn), ["pl-disc"])
        # and the SQL mirror agrees
        links.set_shadow_scope(self.conn)
        ambient = self.conn.execute(
            "SELECT current_setting('app.shadow_ids', true) AS s"
        ).fetchone()["s"]
        self.assertEqual(set((ambient or "").split(",")), {"pl-disc"})

    def test_every_member_hidden_leaves_no_primary(self):
        self.conn.execute("UPDATE accounts SET user_removed_at=now() "
                          "WHERE id IN ('pl-disc', 'sf-disc')")
        g = links.groups(self.conn)[0]
        self.assertFalse(any(m["primary"] for m in g["members"]))
        self.assertEqual(set(links.shadow_ids(self.conn)),
                         {"pl-disc", "sf-disc"})

    def test_unhide_refreshes_the_ambient_shadow_set(self):
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=now() WHERE id='pl-disc'")
        links.set_shadow_scope(self.conn)
        self.conn.execute(
            "UPDATE accounts SET user_removed_at=NULL WHERE id='pl-disc'")
        links.set_shadow_scope(self.conn)
        ambient = self.conn.execute(
            "SELECT current_setting('app.shadow_ids', true) AS s"
        ).fetchone()["s"]
        self.assertEqual(set((ambient or "").split(",")), {"sf-disc"})


class AmazonDriftTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return self.conn.execute(
            "SELECT dedup_key, date FROM amazon_orders ORDER BY date"
        ).fetchall()

    def test_two_same_amount_orders_without_a_number_are_two_orders(self):
        base = {"account": "main", "amount": 9.99, "payee": "Amazon"}
        amazon_orders.import_orders(self.conn, [
            {**base, "dedup_key": "k1", "date": "2026-07-01"}])
        amazon_orders.import_orders(self.conn, [
            {**base, "dedup_key": "k2", "date": "2026-07-04"}])
        self.assertEqual([r["dedup_key"] for r in self._rows()], ["k1", "k2"])

    def test_a_split_shipment_charged_twice_stays_two_rows(self):
        base = {"account": "main", "amount": 24.99, "payee": "Amazon",
                "order_number": "111-222"}
        amazon_orders.import_orders(self.conn, [
            {**base, "dedup_key": "s1", "date": "2026-07-01"},
            {**base, "dedup_key": "s2", "date": "2026-07-03"}])
        # a third push re-dating one of them must not pick "whichever"
        amazon_orders.import_orders(self.conn, [
            {**base, "dedup_key": "s3", "date": "2026-07-05"}])
        self.assertEqual(len(self._rows()), 3)

    def test_a_real_redate_still_refreshes_the_row(self):
        base = {"account": "main", "amount": 24.99, "payee": "Amazon",
                "order_number": "111-333"}
        amazon_orders.import_orders(self.conn, [
            {**base, "dedup_key": "d1", "date": "2026-07-01"}])
        out = amazon_orders.import_orders(self.conn, [
            {**base, "dedup_key": "d2", "date": "2026-07-03"}])
        self.assertEqual(out["date_drift_refreshed"], 1)
        self.assertEqual([r["dedup_key"] for r in self._rows()], ["d2"])

    def test_a_junk_amount_skips_the_row_not_the_push(self):
        out = amazon_orders.import_orders(self.conn, [
            {"account": "main", "dedup_key": "j1", "date": "2026-07-01",
             "amount": "lots", "payee": "Amazon"},
            {"account": "main", "dedup_key": "j3", "date": "2026-07-01",
             "amount": True, "payee": "Amazon"},        # not $1.00
            {"account": "main", "dedup_key": "j2", "date": "2026-07-01",
             "amount": 5.0, "payee": "Amazon"}])
        self.assertEqual(out["new"], 1)

    def test_an_all_rejected_push_is_not_a_fresh_heartbeat(self):
        amazon_orders.import_orders(self.conn, [
            {"account": "main", "dedup_key": "z1", "date": "not-a-date",
             "amount": 5.0}])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM script_heartbeats WHERE source='amazon-orders'"
        ).fetchone())
        amazon_orders.import_orders(self.conn, [
            {"account": "main", "dedup_key": "z2", "date": "2026-07-01",
             "amount": 5.0}])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM script_heartbeats WHERE source='amazon-orders'"
        ).fetchone())


class SpotPriceTests(unittest.TestCase):
    def test_non_finite_and_negative_prices_are_missing_but_zero_is_a_price(self):
        from oikonome.sync import coinbase
        c = coinbase.CoinbaseClient.__new__(coinbase.CoinbaseClient)
        answers = iter(["NaN", "Infinity", "-3", "0", "12.5"])
        c.get = lambda path: {"data": {"amount": next(answers)}}
        self.assertIsNone(c.spot("A", {}))
        self.assertIsNone(c.spot("B", {}))
        self.assertIsNone(c.spot("C", {}))
        self.assertEqual(c.spot("D", {}), 0.0)
        self.assertEqual(c.spot("E", {}), 12.5)


class CoinbaseHeartbeatTests(unittest.TestCase):
    def test_a_balance_only_push_stamps_the_heartbeat(self):
        conn = make_db()
        try:
            write_config(conn)
            coinbase_push.import_payload(conn, {
                "items": [{"id": f"{coinbase_push.ITEM_PREFIX}main",
                           "institution_name": "Coinbase"}],
                "accounts": [{"id": f"{coinbase_push.ITEM_PREFIX}main:btc",
                              "item_id": f"{coinbase_push.ITEM_PREFIX}main",
                              "name": "BTC Wallet", "type": "investment",
                              "subtype": "crypto", "balance_current": 12.5}],
                "transactions": []})
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM script_heartbeats WHERE source='coinbase'"
            ).fetchone())
        finally:
            conn.close()


class HoldingsReplaceTests(unittest.TestCase):
    def test_a_sold_position_on_an_oddly_typed_account_disappears(self):
        """Replace covers every account the response writes holdings for,
        not only the ones it types as investment. A holding whose account
        Plaid typed as something else was inserted and then never deleted,
        so a position sold from it stayed in net worth forever; and our
        own investment account that sold everything is cleared too."""
        conn = make_db()
        try:
            write_config(conn)
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token) VALUES ('inv-2','plaid','Broker','tok') "
                "ON CONFLICT (tenant_id, id) DO NOTHING")
            for aid, typ in (("inv-b", "investment"), ("odd-b", "other")):
                conn.execute(
                    """INSERT INTO accounts (id, item_id, name, type,
                                             subtype, balance_current,
                                             updated_at)
                       VALUES (%s,'inv-2','Acct',%s,'brokerage',1000,
                               now())""", (aid, typ))

            def client_with(holdings):
                class _Client:
                    def get_investment_holdings(self, token):
                        return {"accounts": [{"account_id": "inv-b",
                                              "type": "investment"},
                                             {"account_id": "odd-b",
                                              "type": "other"}],
                                "securities": [], "holdings": holdings}
                return _Client()

            def h(aid, sid):
                return {"account_id": aid, "security_id": sid,
                        "quantity": 1, "institution_price": 1,
                        "institution_value": 1}
            with mock.patch.object(plaid, "get_access_token",
                                   return_value="tok"):
                plaid.sync_holdings(conn, "inv-2", client=client_with(
                    [h("inv-b", "s1"), h("odd-b", "s2")]))
                # everything sold: the next response carries no holdings
                plaid.sync_holdings(conn, "inv-2", client=client_with([]))
            left = conn.execute(
                "SELECT account_id FROM holdings "
                "WHERE account_id IN ('inv-b','odd-b')").fetchall()
            self.assertEqual(left, [])
        finally:
            conn.close()

    def test_a_bad_row_leaves_yesterdays_positions_standing(self):
        conn = make_db()
        try:
            write_config(conn)
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token) VALUES ('inv-1','plaid','Broker','tok') "
                "ON CONFLICT (tenant_id, id) DO NOTHING")
            conn.execute(
                """INSERT INTO accounts (id, item_id, name, type, subtype,
                                         balance_current, updated_at)
                   VALUES ('inv-a','inv-1','Brokerage','investment',
                           'brokerage',1000,now())""")
            conn.execute(
                """INSERT INTO holdings (account_id, symbol, name, quantity,
                                         price, value, as_of, raw)
                   VALUES ('inv-a','VTI','Total Market',10,100,1000,now(),
                           '{}'::jsonb)""")

            class _Client:
                def get_investment_holdings(self, token):
                    return {"accounts": [{"account_id": "inv-a",
                                          "type": "investment"}],
                            "securities": [],
                            # the second row's quantity is not a number:
                            # the insert raises after the delete ran
                            "holdings": [
                                {"account_id": "inv-a", "security_id": "s1",
                                 "quantity": 5, "institution_price": 1,
                                 "institution_value": 5},
                                {"account_id": "inv-a", "security_id": "s2",
                                 "quantity": {"bad": 1},
                                 "institution_price": 1,
                                 "institution_value": 5}]}
            with mock.patch.object(plaid, "get_access_token",
                                   return_value="tok"):
                with self.assertRaises(Exception):
                    plaid.sync_holdings(conn, "inv-1", client=_Client())
            rows = conn.execute(
                "SELECT symbol FROM holdings WHERE account_id='inv-a'"
            ).fetchall()
            self.assertEqual([r["symbol"] for r in rows], ["VTI"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
