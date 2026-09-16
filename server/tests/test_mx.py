"""MX connector: key validation, tenant user creation, item-per-member
sync with sign mapping, worker integration, keys API."""

import datetime as dt
import unittest
import uuid
from unittest import mock

import httpx

from oikonome.db import crypto, tenancy
from oikonome.engine import budget
from oikonome.sync import mx

from .util import _ensure_db, make_db, write_config


def _transport(routes: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        body = routes.get(key)
        if body is None:
            return httpx.Response(404, json={"missing": key})
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


PAYLOAD = {
    "GET /users": {"users": [], "pagination": {"total_pages": 1}},
    "POST /users": {"user": {"guid": "USR-1"}},
    "POST /users/USR-1/widget_urls":
        {"widget_url": {"url": "https://int-widgets.moneydesktop.com/x"}},
    "GET /users/USR-1/members": {"members": [
        {"guid": "MBR-1", "name": "Demo Credit Union"}],
        "pagination": {"total_pages": 1}},
    "GET /users/USR-1/accounts": {"accounts": [
        {"guid": "ACT-1", "member_guid": "MBR-1", "name": "Checking",
         "type": "CHECKING", "balance": 1000.0, "currency_code": "USD",
         "account_number": "XXXX1234"},
        {"guid": "ACT-2", "member_guid": "MBR-1", "name": "Card",
         "type": "CREDIT_CARD", "balance": 250.0, "currency_code": "USD"}],
        "pagination": {"total_pages": 1}},
    "GET /users/USR-1/transactions": {"transactions": [
        {"guid": "TRN-1", "account_guid": "ACT-1", "amount": 12.5,
         "type": "DEBIT", "transacted_at": "2026-07-10T12:00:00Z",
         "description": "COFFEE", "status": "POSTED",
         "top_level_category": "Food & Dining"},
        {"guid": "TRN-2", "account_guid": "ACT-1", "amount": 500.0,
         "type": "CREDIT", "transacted_at": "2026-07-11T12:00:00Z",
         "description": "PAYCHECK", "status": "POSTED"}],
        "pagination": {"total_pages": 1}},
}


def _save_creds(conn, env="sandbox"):
    cfg = budget.load_config(conn)
    cfg["mx_client_id"] = "cid"
    cfg["mx_api_key"] = crypto.encrypt(conn, "key")
    cfg["mx_env"] = env
    budget.save_config(conn, cfg)


class MxTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_validate(self):
        self.assertTrue(mx.validate("c", "k", "sandbox",
                                    transport=_transport(PAYLOAD)))
        bad = httpx.MockTransport(lambda r: httpx.Response(401))
        self.assertFalse(mx.validate("c", "k", "sandbox", transport=bad))

    def test_sync_items_accounts_signs(self):
        _save_creds(self.conn)
        r = mx.sync(self.conn, transport=_transport(PAYLOAD))
        self.assertEqual((r["members"], r["accounts"], r["transactions"]),
                         (1, 2, 2))
        # user guid persisted for next time
        self.assertEqual(budget.load_config(self.conn)["mx_user_guid"],
                         "USR-1")
        item = self.conn.execute(
            "SELECT institution_name, aggregator, status FROM items "
            "WHERE id='mx-MBR-1'").fetchone()
        self.assertEqual((item["institution_name"], item["aggregator"],
                          item["status"]), ("Demo Credit Union", "mx", "ok"))
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, amount, category_primary FROM transactions "
            "WHERE id LIKE 'mx:%'").fetchall()}
        self.assertEqual(rows["mx:TRN-1"]["amount"], 12.5)      # DEBIT = out
        self.assertEqual(rows["mx:TRN-2"]["amount"], -500.0)    # CREDIT = in
        self.assertEqual(rows["mx:TRN-1"]["category_primary"],
                         "FOOD_&_DINING")
        mask = self.conn.execute(
            "SELECT mask, type FROM accounts WHERE id='mx-ACT-1'").fetchone()
        self.assertEqual((mask["mask"], mask["type"]), ("1234", "depository"))
        # idempotent resync
        mx.sync(self.conn, transport=_transport(PAYLOAD))
        n = self.conn.execute("SELECT COUNT(*) AS n FROM transactions "
                              "WHERE id LIKE 'mx:%'").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_sync_error_marks_items(self):
        _save_creds(self.conn)
        mx.sync(self.conn, transport=_transport(PAYLOAD))  # create item
        boom = httpx.MockTransport(lambda r: httpx.Response(500, json={}))
        with self.assertRaises(Exception):
            mx.sync(self.conn, transport=boom)
        st = self.conn.execute("SELECT status FROM items WHERE id='mx-MBR-1'"
                               ).fetchone()["status"]
        self.assertTrue(st.startswith("error:"))

    def test_widget_url(self):
        _save_creds(self.conn)
        url = mx.connect_widget_url(self.conn,
                                    transport=_transport(PAYLOAD))
        self.assertTrue(url.startswith("https://"))


class MxKeysApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"mx-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_keys_validated_then_saved_encrypted(self):
        with mock.patch("oikonome.sync.mx.validate", return_value=False):
            r = self.client.post("/api/accounts/mx/keys", json={
                "client_id": "c", "api_key": "k", "env": "sandbox"})
        self.assertEqual(r.status_code, 400)
        with mock.patch("oikonome.sync.mx.validate", return_value=True):
            r = self.client.post("/api/accounts/mx/keys", json={
                "client_id": "c", "api_key": "k", "env": "sandbox"})
        self.assertEqual(r.status_code, 200)
        conn = tenancy.tenant_connect(self.tid)
        try:
            c = mx.creds(conn)
        finally:
            conn.close()
        self.assertEqual((c["client_id"], c["api_key"], c["env"]),
                         ("c", "k", "sandbox"))


if __name__ == "__main__":
    unittest.main()


class EnsureUserConcurrencyTests(unittest.TestCase):
    """Two doors reaching ensure_user must not leave an orphan at MX.

    The guid read at the top and the write-back at the bottom straddle an
    HTTP round trip, and three unserialised doors reach this function. Both
    could see no guid, both create an MX user, and the second write would
    overwrite the first — leaving a user at MX holding whatever the widget
    linked to it, with nothing on our side pointing at it.
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_the_loser_keeps_the_winner_guid_and_releases_its_own(self):
        from oikonome.engine import budget
        from oikonome.sync import mx

        write_config(self.conn, mx_client_id="cid", mx_api_key="key",
                     mx_env="int")
        created, deleted = [], []

        def handler(request):
            if request.method == "DELETE":
                deleted.append(request.url.path)
                return httpx.Response(204)
            created.append(1)
            # while OUR create is in flight, another door finishes and
            # writes its own guid
            with budget.config_txn(self.conn) as cfg:
                cfg["mx_user_guid"] = "USR-winner"
            return httpx.Response(
                200, json={"user": {"guid": "USR-loser"}})

        got = mx.ensure_user(self.conn,
                             transport=httpx.MockTransport(handler))
        self.assertEqual(got, "USR-winner", "the stored guid wins")
        self.assertEqual(created, [1])
        self.assertTrue(any("USR-loser" in d for d in deleted),
                        "the duplicate must be released at MX")
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["mx_user_guid"], "USR-winner")

    def test_the_ordinary_path_stores_and_returns_the_new_guid(self):
        from oikonome.engine import budget
        from oikonome.sync import mx

        write_config(self.conn, mx_client_id="cid", mx_api_key="key",
                     mx_env="int")
        with budget.config_txn(self.conn) as cfg:
            cfg.pop("mx_user_guid", None)

        def handler(request):
            return httpx.Response(200, json={"user": {"guid": "USR-solo"}})

        got = mx.ensure_user(self.conn,
                             transport=httpx.MockTransport(handler))
        self.assertEqual(got, "USR-solo")
        self.assertEqual(budget.load_config(self.conn)["mx_user_guid"],
                         "USR-solo")


def _capturing_transport(routes: dict, from_dates: list):
    """_transport plus a record of every /transactions from_date asked."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):
            from_dates.append(request.url.params.get("from_date"))
        key = f"{request.method} {request.url.path}"
        body = routes.get(key)
        if body is None:
            return httpx.Response(404, json={"missing": key})
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


# PAYLOAD plus a SECOND institution (member) added later — MX serves ONE
# /users/{guid}/transactions feed for every member, so the shared
# from_date is what decides whether the new member gets its history.
PAYLOAD_TWO_MEMBERS = dict(PAYLOAD)
PAYLOAD_TWO_MEMBERS["GET /users/USR-1/members"] = {"members": [
    {"guid": "MBR-1", "name": "Demo Credit Union"},
    {"guid": "MBR-2", "name": "Second Bank"}],
    "pagination": {"total_pages": 1}}
PAYLOAD_TWO_MEMBERS["GET /users/USR-1/accounts"] = {"accounts": (
    PAYLOAD["GET /users/USR-1/accounts"]["accounts"] + [
        {"guid": "ACT-3", "member_guid": "MBR-2", "name": "Checking 2",
         "type": "CHECKING", "balance": 10.0, "currency_code": "USD"}]),
    "pagination": {"total_pages": 1}}
PAYLOAD_TWO_MEMBERS["GET /users/USR-1/transactions"] = {"transactions": (
    PAYLOAD["GET /users/USR-1/transactions"]["transactions"] + [
        {"guid": "TRN-3", "account_guid": "ACT-3", "amount": 5.0,
         "type": "DEBIT", "transacted_at": "2026-07-12T12:00:00Z",
         "description": "OLD HISTORY", "status": "POSTED"}]),
    "pagination": {"total_pages": 1}}


class SecondInstitutionBackfillTests(unittest.TestCase):
    """The deep-history window must be decided per MEMBER: a tenant whose
    first MX institution already synced still owes a second, later-added
    institution its full 2-year backfill — keying the check on ANY mx: row
    existing would pin the shared from_date to 30 days forever, so the new
    institution would never get history older than a month."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _save_creds(self.conn)

    def tearDown(self):
        self.conn.close()

    def _since(self, payload):
        seen: list = []
        mx.sync(self.conn, transport=_capturing_transport(payload, seen))
        self.assertEqual(len(seen), 1)
        return dt.date.fromisoformat(seen[0])

    def test_a_second_member_still_gets_the_deep_backfill(self):
        today = dt.date.today()
        deep, refresh = (today - dt.timedelta(days=700),
                         today - dt.timedelta(days=40))
        # first pull: deep
        self.assertLess(self._since(PAYLOAD), deep)
        # steady state: 30-day refresh
        self.assertGreater(self._since(PAYLOAD), refresh)
        # a NEW institution appears (no rows of its own yet): deep again,
        # even though the tenant already has mx: rows from MBR-1
        self.assertLess(self._since(PAYLOAD_TWO_MEMBERS), deep)
        # its history landed — back to the refresh window
        self.assertGreater(self._since(PAYLOAD_TWO_MEMBERS), refresh)

    def test_an_explicit_since_is_untouched(self):
        seen: list = []
        mx.sync(self.conn, since=dt.date(2026, 1, 1),
                transport=_capturing_transport(PAYLOAD, seen))
        self.assertEqual(seen, ["2026-01-01"])


class AggregatorPayloadCapTests(unittest.TestCase):
    """An MX pull funnels through base.upsert_transactions, where the
    generous sync-batch ceiling refuses an implausibly large provider
    payload instead of upserting it row by row inside the request."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _save_creds(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_an_oversized_provider_payload_is_refused(self):
        from oikonome.sync import rowcap
        with mock.patch.object(rowcap, "MAX_SYNC_ROWS", 1):
            with self.assertRaises(ValueError) as e:
                mx.sync(self.conn, transport=_transport(PAYLOAD))
        self.assertIn("implausibly large", str(e.exception))
