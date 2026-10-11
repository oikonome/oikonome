"""A SimpleFIN bank that was disconnected comes back when the owner
connects a new setup token, and its next pull reaches back over the time
it was away.

Disconnecting the bridge archives every bank under it, and a pull skips an
archived bank's accounts and transactions. If a new token brought back
only the bridge, the banks would stay archived for ever: every pull would
quietly drop their rows, and the only way out would be purging their whole
history. And the routine window would never reach the days the bank was
disconnected, so those would be missing from its history for good.
"""

import base64
import datetime as dt
import json
import unittest

import httpx

from oikonome.sync import base as sync_base
from oikonome.web import setup

from .util import make_db, write_config

ACCESS = "https://u:p@bridge.test/simplefin"
TOKEN = base64.b64encode(b"https://bridge.test/claim/invented").decode()
ORG_A = {"name": "Demo Bank"}
ORG_B = {"name": "Gone Bank"}


def _ts(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time.min,
                                   dt.timezone.utc).timestamp())


class _Bridge:
    """Serves the transactions on or after the requested start-date, as the
    real bridge does, and records every start-date asked for."""

    def __init__(self, orgs):
        self.orgs = orgs
        self.starts: list[dt.date | None] = []

    def transport(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, text=ACCESS)
            raw = request.url.params.get("start-date")
            start = (dt.datetime.fromtimestamp(int(raw), dt.timezone.utc)
                     .date() if raw else None)
            self.starts.append(start)
            accounts = []
            for i, org in enumerate(self.orgs):
                txns = []
                for age in (5, 150):
                    day = dt.date.today() - dt.timedelta(days=age)
                    if start is None or day >= start:
                        txns.append({"id": f"t{i}-{age}", "posted": _ts(day),
                                     "amount": "-5.00",
                                     "description": f"SHOP {age}"})
                accounts.append({"id": f"acct-{i}", "currency": "USD",
                                 "name": f"{org['name']} Checking",
                                 "balance": "100.00", "org": org,
                                 "transactions": txns})
            return httpx.Response(200, text=json.dumps(
                {"errors": [], "accounts": accounts}))
        return httpx.MockTransport(handler)


class BankReturnsWithANewToken(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _connect(self, bridge):
        return setup.connect_simplefin(self.conn, TOKEN,
                                       transport=bridge.transport())

    def _child(self, org):
        return f"{setup.SIMPLEFIN_ITEM}:" + "".join(
            c if c.isalnum() else "-" for c in org["name"].lower())

    def _status(self, item_id):
        return self.conn.execute(
            "SELECT status, resync_from FROM items WHERE id=%s",
            (item_id,)).fetchone()

    def _rows(self, account_id):
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id=%s",
            (account_id,)).fetchall()}

    def _disconnect_the_bridge(self, days_ago: int):
        sync_base.disconnect_item(self.conn, setup.SIMPLEFIN_ITEM)
        self.conn.execute(
            "UPDATE items SET archived_at = now() - make_interval(days => %s) "
            "WHERE id LIKE %s", (days_ago, setup.SIMPLEFIN_ITEM + "%"))
        # what the bank posted while it was away
        self.conn.execute("DELETE FROM transactions WHERE account_id='acct-0'")

    def test_a_new_token_brings_the_banks_back_with_their_gap(self):
        self._connect(_Bridge([ORG_A]))
        self._disconnect_the_bridge(days_ago=160)
        bridge = _Bridge([ORG_A])
        r = self._connect(bridge)
        self.assertEqual(r["accounts"], 1, r)
        row = self._status(self._child(ORG_A))
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["resync_from"], "the gap was filled")
        # the routine 90-day window would have stopped short of the 150-day
        # row; the pull reached back to a week before the disconnect
        self.assertIn("sfin:acct-0:t0-150", self._rows("acct-0"))
        self.assertIn("sfin:acct-0:t0-5", self._rows("acct-0"))
        self.assertLessEqual(min(s for s in bridge.starts if s),
                             dt.date.today() - dt.timedelta(days=160))

    def test_later_pulls_go_back_to_the_usual_window(self):
        self._connect(_Bridge([ORG_A]))
        self._disconnect_the_bridge(days_ago=160)
        self._connect(_Bridge([ORG_A]))
        from oikonome.sync import simplefin
        bridge = _Bridge([ORG_A])
        simplefin.sync(self.conn, setup.SIMPLEFIN_ITEM, ACCESS,
                       since=dt.date.today() - dt.timedelta(days=30),
                       transport=bridge.transport())
        self.assertEqual(bridge.starts,
                         [dt.date.today() - dt.timedelta(days=30)])

    def test_a_bank_the_bridge_no_longer_returns_stays_disconnected(self):
        self._connect(_Bridge([ORG_A, ORG_B]))
        self._disconnect_the_bridge(days_ago=20)
        bridge = _Bridge([ORG_A])          # Gone Bank was removed there
        self._connect(bridge)
        self.assertEqual(self._status(self._child(ORG_A))["status"], "ok")
        gone = self._status(self._child(ORG_B))
        self.assertEqual(gone["status"], "archived")
        self.assertIsNotNone(gone["resync_from"],
                             "its gap is still owed if it comes back")
        # and its owed gap does not deepen the routine pulls meanwhile
        self.assertEqual(bridge.starts,
                         [dt.date.today() - dt.timedelta(days=90)])

    def test_a_failed_first_pull_leaves_the_banks_disconnected(self):
        """The claim held and the bridge is saved, but the pull that would
        have shown which banks are still there never finished. None may be
        left live on the strength of the new token alone: one the bridge
        no longer returns would read as a connection that never syncs."""
        self._connect(_Bridge([ORG_A, ORG_B]))
        self._disconnect_the_bridge(days_ago=20)

        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, text=ACCESS)
            return httpx.Response(503, text="bridge having a bad minute")
        with self.assertRaises(setup.SimplefinFirstPullFailed):
            setup.connect_simplefin(self.conn, TOKEN,
                                    transport=httpx.MockTransport(handler))
        for org in (ORG_A, ORG_B):
            row = self._status(self._child(org))
            self.assertEqual(row["status"], "archived", org)
            self.assertIsNotNone(row["resync_from"],
                                 "its gap is still owed if it comes back")


    # Only a bank that went down with the bridge comes back with a new
    # token. One the owner cut by itself was cut for a reason of its own —
    # a duplicate of another feed, history deleted on purpose — and the
    # bridge still carrying it is no word that they want it back.

    def _accounts_and_rows(self, account_id):
        n = self.conn.execute(
            "SELECT count(*) AS n FROM accounts WHERE id=%s",
            (account_id,)).fetchone()["n"]
        return n, self._rows(account_id)

    def test_a_purged_bank_does_not_come_back_with_a_new_token(self):
        self._connect(_Bridge([ORG_A, ORG_B]))
        gone = self._child(ORG_B)
        sync_base.disconnect_item(self.conn, gone)
        sync_base.purge_item_accounts(self.conn, gone)
        self.assertEqual(self._accounts_and_rows("acct-1"), (0, set()))
        self._connect(_Bridge([ORG_A, ORG_B]))   # the bridge still has it
        self.assertEqual(self._accounts_and_rows("acct-1"), (0, set()),
                         "history deleted on purpose came back")
        self.assertEqual(self._status(gone)["status"], "archived")

    def test_a_bank_purged_after_the_bridge_went_down_stays_out(self):
        """Purge on an already-disconnected bank: it went down with the
        bridge, then the owner deleted its data on its own."""
        self._connect(_Bridge([ORG_A, ORG_B]))
        self._disconnect_the_bridge(days_ago=20)
        gone = self._child(ORG_B)
        sync_base.purge_item_accounts(self.conn, gone)
        self._connect(_Bridge([ORG_A, ORG_B]))
        self.assertEqual(self._accounts_and_rows("acct-1"), (0, set()))
        self.assertEqual(self._status(self._child(ORG_A))["status"], "ok")

    def test_a_bank_disconnected_on_its_own_stays_disconnected(self):
        self._connect(_Bridge([ORG_A, ORG_B]))
        dup = self._child(ORG_B)
        sync_base.disconnect_item(self.conn, dup)
        self.conn.execute("DELETE FROM transactions WHERE account_id='acct-1'")
        # the bridge going down later does not make it the bridge's
        self._disconnect_the_bridge(days_ago=20)
        self._connect(_Bridge([ORG_A, ORG_B]))
        self.assertEqual(self._status(dup)["status"], "archived")
        self.assertEqual(self._rows("acct-1"), set())
        self.assertEqual(self._status(self._child(ORG_A))["status"], "ok")

    def test_a_bank_a_new_link_took_over_stays_retired(self):
        self._connect(_Bridge([ORG_A]))
        self._disconnect_the_bridge(days_ago=20)
        self.conn.execute(
            "UPDATE items SET archived_reason='reconnected — a new link now "
            "serves it' WHERE id=%s", (self._child(ORG_A),))
        self._connect(_Bridge([ORG_A]))
        self.assertEqual(self._status(self._child(ORG_A))["status"],
                         "archived")

    def test_one_bank_comes_back_on_the_owners_word(self):
        """With the new token no longer bringing it back, a bank cut by
        itself returns through the reconnect door, owing its gap."""
        self._connect(_Bridge([ORG_A, ORG_B]))
        dup = self._child(ORG_B)
        sync_base.disconnect_item(self.conn, dup)
        self.conn.execute(
            "UPDATE items SET archived_at = now() - interval '160 days' "
            "WHERE id=%s", (dup,))
        self.conn.execute("DELETE FROM transactions WHERE account_id='acct-1'")
        bridge = _Bridge([ORG_A, ORG_B])
        r = setup.reconnect_bank(self.conn, dup, transport=bridge.transport())
        self.assertLessEqual(dt.date.fromisoformat(r["resync_from"]),
                             dt.date.today() - dt.timedelta(days=160))
        row = self._status(dup)
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["resync_from"], "the gap was filled at once")
        self.assertIn("sfin:acct-1:t1-150", self._rows("acct-1"))
        with self.assertRaises(ValueError):
            setup.reconnect_bank(self.conn, dup,     # not disconnected
                                 transport=bridge.transport())

    def test_a_purged_bank_is_not_reconnected(self):
        self._connect(_Bridge([ORG_A, ORG_B]))
        gone = self._child(ORG_B)
        sync_base.disconnect_item(self.conn, gone)
        sync_base.purge_item_accounts(self.conn, gone)
        with self.assertRaises(ValueError):
            setup.reconnect_bank(self.conn, gone)

    def test_a_reconnected_bank_the_bridge_no_longer_returns_stays_out(self):
        """Only a pull can tell whether the bridge still carries the bank.
        Left live without one, a bank removed at the bridge would read as
        a connection that never syncs, for ever."""
        self._connect(_Bridge([ORG_A, ORG_B]))
        gone = self._child(ORG_B)
        sync_base.disconnect_item(self.conn, gone)
        with self.assertRaisesRegex(ValueError, "no longer returns"):
            setup.reconnect_bank(self.conn, gone,
                                 transport=_Bridge([ORG_A]).transport())
        row = self.conn.execute(
            "SELECT status, archived_reason, resync_from FROM items "
            "WHERE id=%s", (gone,)).fetchone()
        self.assertEqual(row["status"], "archived")
        # as it was: cut by the owner, not by the bridge, so a new token
        # does not bring it back either
        self.assertIsNone(row["archived_reason"])
        self.assertIsNotNone(row["resync_from"],
                             "its gap is still owed if it comes back")

    def test_a_reconnect_whose_pull_fails_leaves_the_bank_out(self):
        self._connect(_Bridge([ORG_A, ORG_B]))
        gone = self._child(ORG_B)
        sync_base.disconnect_item(self.conn, gone)

        def handler(request):
            return httpx.Response(503, text="bad minute")
        with self.assertRaisesRegex(ValueError, "did not answer"):
            setup.reconnect_bank(self.conn, gone,
                                 transport=httpx.MockTransport(handler))
        self.assertEqual(self._status(gone)["status"], "archived")

    def test_a_bank_a_new_link_adopted_from_after_the_bridge_went_down(self):
        """The bank was already archived — gone with the bridge — when a
        new link adopted some of its accounts. Still marked as the
        bridge's, a new token or the reconnect door would bring it back,
        and its next pull would re-create every adopted account beside
        the one the new link keeps."""
        from oikonome.sync import adopt
        self._connect(_Bridge([ORG_A, ORG_B]))
        self._disconnect_the_bridge(days_ago=20)
        donor = self._child(ORG_A)
        adopt._retire_donors(self.conn, {donor})
        self._connect(_Bridge([ORG_A, ORG_B]))
        self.assertEqual(self._status(donor)["status"], "archived")
        self.assertEqual(self._status(self._child(ORG_B))["status"], "ok")
        with self.assertRaisesRegex(ValueError, "twice"):
            setup.reconnect_bank(self.conn, donor,
                                 transport=_Bridge([ORG_A]).transport())
        self.assertEqual(self._status(donor)["status"], "archived")

    def test_a_bank_a_new_link_took_over_is_not_reconnected(self):
        self._connect(_Bridge([ORG_A]))
        donor = self._child(ORG_A)
        sync_base.disconnect_item(self.conn, donor)
        self.conn.execute(
            "UPDATE items SET archived_reason='reconnected — a new link now "
            "serves it' WHERE id=%s", (donor,))
        with self.assertRaisesRegex(ValueError, "twice"):
            setup.reconnect_bank(self.conn, donor,
                                 transport=_Bridge([ORG_A]).transport())

    def test_a_failed_pull_keeps_the_banks_the_bridges_to_bring_back(self):
        self._connect(_Bridge([ORG_A]))
        self._disconnect_the_bridge(days_ago=20)

        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, text=ACCESS)
            return httpx.Response(503, text="bad minute")
        with self.assertRaises(setup.SimplefinFirstPullFailed):
            setup.connect_simplefin(self.conn, TOKEN,
                                    transport=httpx.MockTransport(handler))
        self._connect(_Bridge([ORG_A]))
        self.assertEqual(self._status(self._child(ORG_A))["status"], "ok")


if __name__ == "__main__":
    unittest.main()
