"""A new bank link asks Plaid for the history the household chose.

With `history_months` set, the link token requests that many months at 31
days each (never more than Plaid's two-year ceiling); with nothing chosen
it asks for the full two years, as it always has. An update-mode session
for an existing link carries no backfill request at all, so the choice
cannot reach it.

MX and SimpleFIN links honour the same choice: their deep first pull starts
at the window instead of two years (MX) or the bridge's first-pull depth
(SimpleFIN). The routine refresh of a link that already has its history is
never narrowed by it.
"""

import datetime as dt
import os
import unittest

from oikonome.engine import budget
from oikonome.sync import base, mx, plaid, simplefin

from .test_plaid_product_gating import _capture_transport
from .util import make_db, write_config


class LinkHistoryWindowTests(unittest.TestCase):
    def setUp(self):
        self._hosted = os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()
        if self._hosted is not None:
            os.environ["OIKONOME_HOSTED"] = self._hosted

    @staticmethod
    def _link_created(conn, **kw) -> dict:
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=_capture_transport({}))
        return client.create_hosted_link("tenant-1", client_name="Oikonome",
                                         conn=conn, **kw)

    def test_the_created_session_carries_the_window_it_asked_for(self):
        self._choose(6)
        created = self._link_created(self.conn)
        self.assertEqual(created["days_requested"], 186)
        self.assertEqual(created["link_token"], "lt-1")
        self.assertNotIn("days_requested",
                         self._link_created(self.conn, access_token="a-x"))

    def _link_body(self, **kw) -> dict:
        captured: dict = {}
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=_capture_transport(captured))
        client.create_hosted_link("tenant-1", client_name="Oikonome",
                                  conn=self.conn, **kw)
        return captured["/link/token/create"]

    def _choose(self, months):
        with budget.config_txn(self.conn) as cfg:
            if months is None:
                cfg.pop("history_months", None)
            else:
                cfg["history_months"] = months

    def test_no_choice_asks_for_the_full_two_years(self):
        body = self._link_body()
        self.assertEqual(body["transactions"], {"days_requested": 730})

    def test_each_choice_asks_for_its_months(self):
        for months, days in ((1, 31), (3, 93), (6, 186), (12, 372)):
            self._choose(months)
            body = self._link_body()
            self.assertEqual(body["transactions"],
                             {"days_requested": days}, months)

    def test_a_value_outside_the_choices_keeps_the_full_window(self):
        # a restored or hand-edited config must widen, never narrow
        self._choose(48)
        self.assertEqual(self._link_body()["transactions"],
                         {"days_requested": 730})

    def test_an_update_session_asks_for_no_backfill(self):
        self._choose(3)
        body = self._link_body(access_token="access-x")
        self.assertNotIn("transactions", body)

    def test_without_a_tenant_connection_the_default_stands(self):
        self.assertEqual(plaid.days_requested(None), plaid.DAYS_REQUESTED)


class LinkRecordsItsWindowTests(unittest.TestCase):
    """The Item keeps the window its link asked for, because the import
    bar measures each Item against its own window: the household's choice
    can change afterwards, the Item's backfill cannot."""

    def setUp(self):
        import uuid
        self._hosted = os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.item_id = f"pl-win-{uuid.uuid4().hex[:10]}"

    def tearDown(self):
        self.conn.close()
        if self._hosted is not None:
            os.environ["OIKONOME_HOSTED"] = self._hosted

    def _link(self, **kw):
        from .test_plaid_best_practices import _transport
        plaid.link_item(self.conn, "public-x",
                        transport=_transport(self.item_id), **kw)
        return self.conn.execute(
            "SELECT raw->'days_requested' AS d FROM items WHERE id=%s",
            (self.item_id,)).fetchone()["d"]

    def test_a_new_link_records_the_window_it_asked_for(self):
        _choose(self.conn, 3)
        self.assertEqual(self._link(), 93)

    def test_no_choice_records_the_full_two_years(self):
        self.assertEqual(self._link(), plaid.DAYS_REQUESTED)

    def test_the_window_the_token_asked_for_wins_over_a_later_choice(self):
        # the token was minted asking for two years; the household chose
        # three months while Link was open. Plaid backfills what the token
        # asked, so that is the Item's window.
        self.assertEqual(
            LinkHistoryWindowTests._link_created(self.conn)["days_requested"],
            730)
        _choose(self.conn, 3)
        self.assertEqual(self._link(window_asked=730), 730)

    def test_a_relink_keeps_the_window_the_item_was_made_with(self):
        # update mode asks for no backfill, so a choice made since the
        # first link says nothing about this Item
        self.assertEqual(self._link(), 730)
        _choose(self.conn, 1)
        self.assertEqual(self._link(), 730)


def _choose(conn, months):
    with budget.config_txn(conn) as cfg:
        cfg["history_months"] = months


class MxFirstPullWindowTests(unittest.TestCase):
    def setUp(self):
        from .test_mx import _save_creds
        self.conn = make_db()
        write_config(self.conn)
        _save_creds(self.conn)

    def tearDown(self):
        self.conn.close()

    def _since(self):
        from .test_mx import PAYLOAD, _capturing_transport
        seen: list = []
        mx.sync(self.conn, transport=_capturing_transport(PAYLOAD, seen))
        self.assertEqual(len(seen), 1)
        return dt.date.fromisoformat(seen[0])

    def test_the_first_pull_starts_at_the_chosen_window(self):
        _choose(self.conn, 3)
        self.assertEqual(self._since(), base.history_cutoff(self.conn))

    def test_no_choice_keeps_the_two_year_first_pull(self):
        self.assertLess(self._since(),
                        dt.date.today() - dt.timedelta(days=700))

    def test_the_routine_refresh_is_not_narrowed(self):
        _choose(self.conn, 1)
        self._since()                       # the first pull lands its rows
        self.assertEqual(self._since(),
                         dt.date.today() - dt.timedelta(days=30))


class ReconnectGapStaysInsideTheWindowTests(unittest.TestCase):
    """A reconnected bank is owed the days it was away, but MX and the
    SimpleFIN bridge each take one start date for every bank: a gap reaching
    past the history window would land every other bank's pre-window rows
    with it. The gap is filled as far as the window, and no further."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _owe(self, item_like, days) -> str:
        """Owe the bank `days` of history; returns the date owed, read back
        from the row so the expectation shares the database's clock (the
        test host's local date is one day behind CURRENT_DATE every
        evening west of UTC)."""
        return self.conn.execute(
            "UPDATE items SET resync_from = CURRENT_DATE - %s::int"
            " WHERE id LIKE %s RETURNING resync_from::text",
            (days, item_like)).fetchone()["resync_from"]

    def test_an_mx_reconnect_does_not_reach_past_the_window(self):
        from .test_mx import PAYLOAD, _capturing_transport, _save_creds
        _save_creds(self.conn)
        mx.sync(self.conn, transport=_capturing_transport(PAYLOAD, []))
        _choose(self.conn, 3)
        self._owe("mx-%", 240)             # away about eight months
        seen: list = []
        mx.sync(self.conn, transport=_capturing_transport(PAYLOAD, seen))
        self.assertEqual([dt.date.fromisoformat(x) for x in seen],
                         [base.history_cutoff(self.conn)])

    def test_an_mx_gap_inside_the_window_is_still_filled(self):
        from .test_mx import PAYLOAD, _capturing_transport, _save_creds
        _save_creds(self.conn)
        mx.sync(self.conn, transport=_capturing_transport(PAYLOAD, []))
        _choose(self.conn, 6)
        owed = self._owe("mx-%", 60)
        seen: list = []
        mx.sync(self.conn, transport=_capturing_transport(PAYLOAD, seen))
        self.assertEqual(seen, [owed])

    def test_a_simplefin_reconnect_does_not_reach_past_the_window(self):
        from .test_simplefin_first_pull_depth import _Bridge
        bridge = _Bridge()
        simplefin.sync(self.conn, "sfin-demo",
                       "https://u:p@bridge.test/simplefin",
                       since=None, transport=bridge.transport())
        _choose(self.conn, 3)
        self.conn.execute("UPDATE items SET aggregator='simplefin-org'"
                          " WHERE id LIKE 'sfin-demo:%%'")
        self._owe("sfin-demo:%", 240)
        bridge.requests.clear()
        simplefin.sync(self.conn, "sfin-demo",
                       "https://u:p@bridge.test/simplefin",
                       since=dt.date.today() - dt.timedelta(days=30),
                       transport=bridge.transport())
        self.assertEqual(bridge.requests, [
            dt.date.today() - dt.timedelta(days=30),
            base.history_cutoff(self.conn)])


class SimplefinFirstPullWindowTests(unittest.TestCase):
    def setUp(self):
        from .test_simplefin_first_pull_depth import _Bridge
        self.conn = make_db()
        write_config(self.conn)
        self.bridge = _Bridge()

    def tearDown(self):
        self.conn.close()

    def _sync(self, days):
        return simplefin.sync(
            self.conn, "sfin-demo", "https://u:p@bridge.test/simplefin",
            since=(None if days is None
                   else dt.date.today() - dt.timedelta(days=days)),
            transport=self.bridge.transport())

    def test_the_claim_pull_starts_at_the_chosen_window(self):
        _choose(self.conn, 1)
        self._sync(simplefin.FIRST_PULL_DAYS)
        self.assertEqual(self.bridge.requests,
                         [base.history_cutoff(self.conn)])

    def test_an_unbounded_pull_is_bounded_by_the_window(self):
        _choose(self.conn, 1)
        self._sync(None)
        self.assertEqual(self.bridge.requests,
                         [base.history_cutoff(self.conn)])

    def test_a_bank_added_later_reaches_no_further_than_the_window(self):
        # a one-month window is no deeper than the routine pull that found
        # the bank, so there is nothing more to fetch
        from .test_simplefin_first_pull_depth import ORG_B
        _choose(self.conn, 1)
        self._sync(simplefin.FIRST_PULL_DAYS)
        self.bridge.orgs.append(ORG_B)
        self.bridge.requests.clear()
        self._sync(30)
        self.assertEqual(self.bridge.requests,
                         [dt.date.today() - dt.timedelta(days=30)])

    def test_a_window_wider_than_the_first_pull_does_not_deepen_it(self):
        from .test_simplefin_first_pull_depth import ORG_B
        _choose(self.conn, 6)
        self._sync(simplefin.FIRST_PULL_DAYS)
        self.bridge.orgs.append(ORG_B)
        self.bridge.requests.clear()
        self._sync(30)
        self.assertEqual(self.bridge.requests, [
            dt.date.today() - dt.timedelta(days=30),
            dt.date.today() - dt.timedelta(days=simplefin.FIRST_PULL_DAYS)])

    def test_the_routine_refresh_is_not_narrowed(self):
        _choose(self.conn, 1)
        self._sync(simplefin.FIRST_PULL_DAYS)
        self.bridge.requests.clear()
        self._sync(30)
        self.assertEqual(self.bridge.requests,
                         [dt.date.today() - dt.timedelta(days=30)])

    def test_no_choice_keeps_the_first_pull_depth(self):
        self._sync(simplefin.FIRST_PULL_DAYS)
        self.assertEqual(self.bridge.requests, [
            dt.date.today() - dt.timedelta(days=simplefin.FIRST_PULL_DAYS)])


if __name__ == "__main__":
    unittest.main()
