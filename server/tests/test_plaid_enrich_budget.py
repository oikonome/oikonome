"""Plaid Enrich bills per row SENT, so the monthly cap and the batch
selection are money guards, not cosmetics.

The invariants pinned here:

* the spend is recorded BEFORE Plaid is asked, so two callers racing the
  same cap cannot both see the whole of it as free;
* only one enrich run per tenant is in flight at a time, so the same
  candidate rows are never sent (and billed) twice;
* a batch Plaid describes nothing in still consumes the cap — the bill is
  for the send, not for the answer;
* a transport failure part-way through reports what was sent instead of
  raising, and the rows that never left do not stay charged;
* a category Plaid invents outside the known primaries is not written onto
  the row as its working category;
* rows on a Plaid-linked connection are not candidates — Plaid already
  described them on the way in, so paying to describe them again is waste.
"""

import datetime as dt
import json
import unittest

import httpx

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import plaid

from .util import add_txn, make_db, write_config

TODAY = dt.date.today()


def _reply(enrichments_by_index=None, empty=False):
    """A Plaid Enrich transport. `empty` returns an answer that describes
    nothing at all — Plaid's response for descriptions it cannot resolve,
    which is still a billed send."""
    def handler(request):
        if request.url.path != "/transactions/enrich":
            return httpx.Response(404, text="{}")
        body = json.loads(request.content)
        if empty:
            return httpx.Response(200, text=json.dumps(
                {"enriched_transactions": []}))
        out = [{"id": t["id"],
                "enrichments": (enrichments_by_index or {}).get(i, {})}
               for i, t in enumerate(body["transactions"])]
        return httpx.Response(200, text=json.dumps(
            {"enriched_transactions": out}))
    return httpx.MockTransport(handler)


class EnrichSpendIsReservedBeforeThePlaidCall(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        for i in range(4):
            add_txn(self.conn, TODAY - dt.timedelta(days=i + 1), 10.0 + i,
                    f"MYSTERY {i}", account="chk")

    def tearDown(self):
        self.conn.close()

    def test_a_run_in_flight_has_already_spent_its_room_in_the_cap(self):
        """The window a concurrent caller reads in. If the usage write waits
        for Plaid's answer, two callers each see the whole cap as free and
        the operator is billed twice the cap."""
        seen: dict = {}

        def handler(request):
            other = tenancy.tenant_connect(self.tid)
            try:
                seen["remaining"] = plaid.enrich_usage(other)["remaining"]
            finally:
                other.close()
            body = json.loads(request.content)
            return httpx.Response(200, text=json.dumps(
                {"enriched_transactions": [{"id": t["id"], "enrichments": {}}
                                           for t in body["transactions"]]}))

        with budget.config_txn(self.conn) as cfg:
            cfg["plaid_enrich_cap"] = 10
        plaid.enrich_rows(self.conn, limit=4,
                          transport=httpx.MockTransport(handler))
        self.assertEqual(seen["remaining"], 6,
                         "the four rows in flight must already be charged")

    def test_a_second_run_for_the_same_tenant_refuses_instead_of_resending(self):
        """Two clicks on Enrich must not send the same candidate rows twice:
        the second run finds the first still holding the tenant's lock."""
        second: dict = {}

        def handler(request):
            if "stats" not in second:
                other = tenancy.tenant_connect(self.tid)
                try:
                    second["stats"] = plaid.enrich_rows(
                        other, limit=4, transport=_reply())
                finally:
                    other.close()
            body = json.loads(request.content)
            return httpx.Response(200, text=json.dumps(
                {"enriched_transactions": [{"id": t["id"], "enrichments": {}}
                                           for t in body["transactions"]]}))

        plaid.enrich_rows(self.conn, limit=4,
                          transport=httpx.MockTransport(handler))
        self.assertEqual(second["stats"]["sent"], 0)
        self.assertEqual(second["stats"]["error"], "already running")

    def test_a_batch_plaid_describes_nothing_in_still_consumes_the_cap(self):
        """Plaid bills the send, not the answer. Counting only the rows that
        came back described lets an unenrichable ledger be re-sent forever
        at full price."""
        st = plaid.enrich_rows(self.conn, limit=4, transport=_reply(empty=True))
        self.assertEqual(st["sent"], 4)
        self.assertEqual(st["enriched"], 0)
        self.assertEqual(plaid.enrich_usage(self.conn)["used"], 4)


class EnrichSurvivesATransportFailure(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        # two account types = two chunks, so one can fail after the other
        # has already been sent and billed
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 10.0, "CHK ROW",
                account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 20.0, "CARD ROW",
                account="card")

    def tearDown(self):
        self.conn.close()

    def test_a_network_error_is_reported_and_only_the_sent_rows_are_charged(self):
        """The contract is 'reported, not raised' — a connection reset is no
        different from a Plaid error, and the chunk that did go out is
        billed whatever happens to the next one."""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                body = json.loads(request.content)
                return httpx.Response(200, text=json.dumps(
                    {"enriched_transactions": [{"id": t["id"],
                                                "enrichments": {}}
                                               for t in body["transactions"]]}))
            raise httpx.ConnectError("connection reset")

        st = plaid.enrich_rows(self.conn, limit=10,
                               transport=httpx.MockTransport(handler))
        self.assertIsNotNone(st["error"])
        self.assertEqual(st["sent"], 1)
        self.assertEqual(plaid.enrich_usage(self.conn)["used"], 1)

    def test_a_failure_after_the_send_still_refunds_the_unsent_rows(self):
        """The reservation is committed before Plaid is asked; if the
        bookkeeping after a partial send raises, the rows that never went
        out must still be given back or the month's room is lost."""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                body = json.loads(request.content)
                return httpx.Response(200, text=json.dumps(
                    {"enriched_transactions": [{"id": t["id"],
                                                "enrichments": {}}
                                               for t in body["transactions"]]}))
            raise httpx.ConnectError("connection reset")

        from unittest import mock
        from oikonome.engine import merchant_identity
        with mock.patch.object(merchant_identity, "resolve",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                plaid.enrich_rows(self.conn, limit=10,
                                  transport=httpx.MockTransport(handler))
        # two rows reserved, one sent → one charged, one refunded
        self.assertEqual(plaid.enrich_usage(self.conn)["used"], 1)

    def test_a_network_error_on_recurring_streams_yields_nothing(self):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token) "
            "VALUES ('plaid-net','plaid','Bank','tok') "
            "ON CONFLICT (tenant_id, id) DO NOTHING")

        def handler(request):
            raise httpx.ReadTimeout("timed out")

        self.assertEqual(
            plaid.recurring_streams(self.conn, "plaid-net",
                                    transport=httpx.MockTransport(handler)),
            [])


class EnrichWritesOnlyCategoriesWeKnow(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.t = add_txn(self.conn, TODAY - dt.timedelta(days=1), 33.0,
                         "MYSTERY SHOP", account="chk",
                         primary="GENERAL_MERCHANDISE")
        self.conn.execute(
            "UPDATE transactions SET category_source='import' WHERE id=%s",
            (self.t,))

    def tearDown(self):
        self.conn.close()

    def test_a_primary_outside_the_known_set_is_not_adopted_as_the_category(self):
        """Every other layer picks from the sixteen Plaid primaries. A label
        Enrich returns that is not one of them would fragment the budget
        buckets and never appear in the picker, so it is left on the mirror
        column and not adopted."""
        st = plaid.enrich_rows(self.conn, limit=5, transport=_reply(
            {0: {"personal_finance_category": {"primary": "SPACE_TRAVEL",
                                               "detailed": "SPACE_TRAVEL_ORBIT",
                                               "confidence_level": "VERY_HIGH"}}}))
        self.assertEqual(st["sent"], 1)
        r = self.conn.execute(
            "SELECT category_primary, category_source FROM transactions "
            "WHERE id=%s", (self.t,)).fetchone()
        self.assertEqual(r["category_primary"], "GENERAL_MERCHANDISE")
        self.assertEqual(r["category_source"], "import")

    def test_a_known_primary_is_still_adopted(self):
        plaid.enrich_rows(self.conn, limit=5, transport=_reply(
            {0: {"personal_finance_category": {"primary": "FOOD_AND_DRINK",
                                               "detailed": "FOOD_AND_DRINK_FAST_FOOD",
                                               "confidence_level": "VERY_HIGH"}}}))
        r = self.conn.execute(
            "SELECT category_primary, category_source FROM transactions "
            "WHERE id=%s", (self.t,)).fetchone()
        self.assertEqual((r["category_primary"], r["category_source"]),
                         ("FOOD_AND_DRINK", "plaid"))


class PlaidLinkedRowsAreNotCandidates(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    def test_a_row_on_a_plaid_connection_is_never_sent_for_enrichment(self):
        """Plaid describes its own transactions on the way in. A row from a
        Plaid connection that arrived without a category is not going to be
        resolved by paying Plaid to look at the same descriptor again."""
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token) "
            "VALUES ('it-plaid','plaid','Plaid Bank','tok') "
            "ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('pchk','it-plaid','Plaid Checking','depository',"
            "'checking',100) ON CONFLICT (tenant_id, id) DO NOTHING")
        linked = add_txn(self.conn, TODAY - dt.timedelta(days=1), 15.0,
                         "PLAID ROW", account="pchk")
        imported = add_txn(self.conn, TODAY - dt.timedelta(days=2), 25.0,
                           "IMPORTED ROW", account="chk")
        ids = [r["id"] for r in plaid.enrich_candidates(self.conn)]
        self.assertEqual(ids, [imported])
        self.assertNotIn(linked, ids)

    def test_the_settings_card_count_matches_what_would_be_sent(self):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token) "
            "VALUES ('it-plaid','plaid','Plaid Bank','tok') "
            "ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('pchk','it-plaid','Plaid Checking','depository',"
            "'checking',100) ON CONFLICT (tenant_id, id) DO NOTHING")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 15.0, "PLAID ROW",
                account="pchk")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 25.0, "IMPORTED ROW",
                account="chk")
        self.assertEqual(plaid.enrich_candidate_count(self.conn),
                         len(plaid.enrich_candidates(self.conn)))


if __name__ == "__main__":
    unittest.main()
