"""The import, account and notification doors read their JSON bodies through
`web.fields.Fields`, refuse a wrongly typed field with a 4xx, and still take
every body their real callers send.

A body is whatever the caller sent. Read as a plain dict, each door decided
per field whether `{"endpoint": 7}` or `{"reason": "\\u0000"}` was
acceptable, and a field nobody checked reached SQL as a list, or a TEXT
column with a NUL byte in it — an opaque 500 for ordinary bad input. Three
checks pin the typed reading:

* the matrix: each key a door reads is sent, one at a time inside an
  otherwise valid body, as an int, a list, an object, a 10 kB string and a
  NUL; the answer must be below 500.
* the shapes: the bodies the released web and mobile clients build, and the
  body a household's own plan-activity script pushes (docs/community-scripts.md),
  older shapes included, are answered without a 400. An installed phone
  build and a script on somebody's server are older than this code, and a server
  deploy cannot change what they send.
* the source: none of these doors reads its body raw.

Every name, number and amount here is invented.
"""

import inspect
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .test_bill_doors_refuse_wrong_types import BAD_VALUES, _raw_reads
from .push_keys import browser_keys
from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"
SMS_ENV = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
           "OIKONOME_TWILIO_AUTH_TOKEN": "t",
           "OIKONOME_TWILIO_FROM": "+12175550000"}
PLAN_CSV = ("Trade Date,Investments,Ticker,Transaction,Transaction Amount,"
            "Share Price,Total shares\n"
            '07/01/2025,Example Index Fund,EXIDX,Contribution,"250.00",'
            "50.00,5.000\n")
PEM = ("-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEIExampleExampleExample\n"
       "-----END EC PRIVATE KEY-----\n")
ENDPOINT = "https://fcm.googleapis.com/fcm/send/matrix-" + "0" * 16
MASK = "4417"

# the handlers converted to the typed reader, by name
CONVERTED = (
    "import_mapped_api", "import_ynab_accounts_api", "import_rollback_api",
    "coinbase_link_api", "enrich_run_api", "plan_activity_import_api",
    "account_set_hidden", "accounts_remove_api", "notify_phone_api",
    "notify_phone_verify_api", "push_subscribe_api", "push_unsubscribe_api",
    "notify_test_api", "support_access_grant_api")

# door -> (a valid body, every key the door or its helpers read)
_STEP_UP = ("password", "totp_code", "recovery_code")
_DOORS = {
    "/import/mapped": (
        {"token": "no-such-token", "date_col": "Posted",
         "name_col": "Payee Text", "amount_col": "Value"},
        ("token", "date_col", "name_col", "amount_col", "debit_col",
         "credit_col", "merchant_col", "category_col")),
    "/import/ynab-accounts": (
        {"token": "no-such-token", "accounts": {"Checking": "skip"},
         "remove_earlier": {}},
        ("token", "accounts", "remove_earlier")),
    "/import/rollback": (
        {"batch_id": "no-such-batch"}, ("batch_id", "force_kept")),
    "/accounts/coinbase/link": (
        {"label": "Matrix Coins", "key_name": "organizations/o/apiKeys/k",
         "private_key": PEM},
        ("label", "key_name", "private_key")),
    "/import/enrich": ({"limit": 5}, ("limit",)),
    "/import/plan-activity": (
        {"plan": "401K", "csv": PLAN_CSV, "kind": "scrape",
         "provider": "examplefin", "institution": "Example Financial",
         "balance": 1250.5, "mask": MASK},
        ("plan", "csv", "kind", "provider", "institution", "balance",
         "mask")),
    "/accounts/chk/hidden": ({"hidden": False}, ("hidden",)),
    "/accounts/card/remove": ({"mode": "hide"}, ("mode", "force_kept")),
    "/notify/phone": (
        {"number": "+12175551234", "consent": ["summary"], "password": PW},
        ("number", "consent") + _STEP_UP),
    "/notify/phone/verify": ({"code": "000000"}, ("code",)),
    "/notify/push/subscribe": (
        {"endpoint": ENDPOINT, "expirationTime": None,
         "keys": browser_keys(),
         "user_agent": "Matrix Browser", "password": PW},
        ("endpoint", "keys", "user_agent") + _STEP_UP),
    "/notify/push/unsubscribe": ({"endpoint": ENDPOINT}, ("endpoint",)),
    "/notify/test": ({"channel": "push"}, ("channel",)),
    "/support-access": (
        {"hours": 24, "reason": "matrix", "password": PW},
        ("hours", "reason") + _STEP_UP),
}
_NESTED = {"/notify/push/subscribe": ("keys", ("p256dh", "auth"))}


def _household(prefix):
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    client = TestClient(appmod.app, raise_server_exceptions=False)
    r = client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": PW})
    assert r.status_code == 200, r.text
    tid = client.get("/api/me").json()["tenant_id"]
    conn = tenancy.tenant_connect(tid)
    try:
        seed_accounts(conn)
        write_config(conn)
    finally:
        conn.close()
    return client, tid


def _clear_limits():
    from oikonome.web import security
    security._limiter._hits.clear()


class ImportAndNotifyDoorsRefuseWrongTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _household("r3types")

    def _post(self, path, body):
        _clear_limits()
        with mock.patch.dict(os.environ, SMS_ENV), \
                mock.patch("oikonome.notify.sms.send", return_value=True):
            return self.client.post("/api" + path, json=body)

    def test_every_key_these_doors_read_refuses_a_wrong_type(self):
        failures = []
        for path, (base, keys) in _DOORS.items():
            for key in keys:
                for bad in BAD_VALUES:
                    r = self._post(path, dict(base, **{key: bad}))
                    if r.status_code >= 500:
                        failures.append(f"{path} {key}={str(bad)[:20]!r} "
                                        f"-> {r.status_code}")
        self.assertEqual(failures, [])

    def test_every_nested_key_refuses_a_wrong_type(self):
        failures = []
        for path, (outer, inner_keys) in _NESTED.items():
            base = _DOORS[path][0]
            for key in inner_keys:
                for bad in BAD_VALUES:
                    body = dict(base, **{outer: dict(base[outer],
                                                      **{key: bad})})
                    r = self._post(path, body)
                    if r.status_code >= 500:
                        failures.append(f"{path} {outer}.{key}="
                                        f"{str(bad)[:20]!r} -> "
                                        f"{r.status_code}")
        self.assertEqual(failures, [])

    def test_a_string_is_never_read_as_true(self):
        # the old reading took any truthy value: "false" hid the account
        r = self._post("/accounts/chk/hidden", {"hidden": "false"})
        self.assertEqual(r.status_code, 400, r.text)
        # a destructive option is on only for the literal true: anything
        # else leaves it off (the door then asks first), never forces
        from oikonome.web import api
        from oikonome.web.fields import Fields
        for v in ("true", 1, [True], {"a": True}, False, None):
            self.assertFalse(api._literal_true(Fields({"force_kept": v}),
                                               "force_kept"), v)
        self.assertTrue(api._literal_true(Fields({"force_kept": True}),
                                          "force_kept"))


class TheseDoorsReadTheirBodiesTyped(unittest.TestCase):
    def test_no_converted_door_reads_its_body_raw(self):
        from oikonome.web.fields import Fields
        from .util import api_function
        offenders = {}
        for name in CONVERTED:
            fn = api_function(name)
            reads = _raw_reads(fn)
            if reads:
                offenders[name] = reads
            body = inspect.signature(fn, eval_str=True).parameters["body"]
            if body.annotation is not Fields:
                offenders.setdefault(name, []).append(
                    f"body is {body.annotation!r}, not Fields")
        self.assertEqual(offenders, {})


class ShippedCallerBodiesAreAccepted(unittest.TestCase):
    """The bodies the released callers send, copied from their call sites:
    webapp/src/api/client.ts, mobile/src/lib/api.ts and the screens that
    build each body, and the documented POST /api/import/plan-activity
    contract (docs/community-scripts.md) a household's own script follows."""

    def setUp(self):
        self.client, self.tid = _household("r3shapes")
        # both clients raise the step-up sheet, which elevates the session,
        # before they retry a door that asks for it
        r = self.client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        _clear_limits()

    def _post(self, path, body):
        _clear_limits()
        return self.client.post("/api" + path, json=body)

    def _ok(self, path, body):
        r = self._post(path, body)
        self.assertEqual(r.status_code, 200, (path, body, r.text))
        return r.json()

    def test_a_mapped_import_takes_the_clients_full_column_map(self):
        from .test_a_mapped_import_stays_on_the_account_holding_its_rows \
            import unmapped_for
        for tag in ("WEBTAG", "MOBTAG"):
            r = self.client.post(
                "/api/import",
                data={"account_id": "chk", "amount_sign": "bank"},
                files={"file": ("export.csv", unmapped_for(tag),
                                "text/csv")})
            self.assertEqual(r.status_code, 200, r.text)
            token = r.json()["mapping_needed"]["token"]
            # both clients start every column at "" and send all seven
            cols = {"date_col": "Posted", "amount_col": "Value",
                    "name_col": "Payee Text", "merchant_col": "",
                    "category_col": "", "debit_col": "", "credit_col": ""}
            out = self._ok("/import/mapped", dict(cols, token=token))
            self.assertEqual(out["result"].get("imported"), 3, out)

    def test_a_ynab_register_takes_every_destination_shape(self):
        from .test_ynab_register_rows_land_on_the_account_they_came_from \
            import REGISTER
        r = self.client.post(
            "/api/import",
            data={"account_id": "", "amount_sign": "bank",
                  "new_account_name": "Shared Budget", "ynab_choice": "1"},
            files={"file": ("register.csv", REGISTER.encode(), "text/csv")})
        need = r.json()["mapping_needed"]
        out = self._ok("/import/ynab-accounts", {
            "token": need["token"],
            "accounts": {"Checking": {"id": "chk", "typed": "Checking"},
                         "Visa": "skip"},
            "remove_earlier": {"Checking": False}})
        self.assertNotIn("error", out["result"])

    def test_an_undo_takes_both_shapes(self):
        self._ok("/import/rollback", {"batch_id": "no-such-batch"})
        self._ok("/import/rollback", {"batch_id": "no-such-batch",
                                      "force_kept": True})

    def test_a_coinbase_link_takes_a_pasted_key(self):
        out = self._ok("/accounts/coinbase/link", {
            "label": "Main", "key_name": "organizations/o/apiKeys/k",
            "private_key": PEM})
        self.assertTrue(out["ok"])

    def test_an_enrich_run_reads_its_limit_as_it_always_has(self):
        from oikonome.sync import plaid as _plaid
        seen = []
        with mock.patch.object(_plaid, "credentials", return_value=None), \
                mock.patch.object(_plaid, "enrich_rows",
                                  side_effect=lambda conn, limit:
                                  seen.append(limit) or {"sent": 0}):
            # web and mobile send a number; an empty body, 0 and blank are
            # the default batch; digits and fractions read as before
            for body in ({"limit": 80}, {"limit": 15}, {}, {"limit": 0},
                         {"limit": ""}, {"limit": "50"}, {"limit": 12.7},
                         {"limit": 9000}):
                self._ok("/import/enrich", body)
            _clear_limits()
            r = self.client.post("/api/import/enrich")
            self.assertEqual(r.status_code, 200, r.text)
        d = _plaid.ENRICH_BATCH
        self.assertEqual(seen, [80, 15, d, d, d, 50, 12, 500, d])

    def test_the_plan_collector_push_takes_every_shape_it_sends(self):
        base = {"plan": "401K", "csv": PLAN_CSV, "kind": "scrape",
                "provider": "examplefin", "institution": "Example Financial"}
        # with a scraped balance and the plan's zero-padded mask
        self._ok("/import/plan-activity",
                 dict(base, balance=1250.5, mask=MASK))
        # no balance scraped, no mask known
        self._ok("/import/plan-activity", base)
        # an older push: no provider, no kind, a whole-number balance
        self._ok("/import/plan-activity",
                 {"plan": "403B", "csv": PLAN_CSV, "balance": 900})
        # the balance as text and the mask as a number, which the published
        # contract has always taken from a household's own script
        self._ok("/import/plan-activity",
                 dict(base, balance="1250.50", mask=int(MASK)))

    def test_hide_and_remove_take_the_clients_shapes(self):
        out = self._ok("/accounts/chk/hidden", {"hidden": True})
        self.assertTrue(out["hidden"])
        out = self._ok("/accounts/chk/hidden", {"hidden": False})
        self.assertFalse(out["hidden"])
        self._ok("/accounts/card/remove", {"mode": "hide"})
        self._ok("/accounts/card/hidden", {"hidden": False})
        self._ok("/accounts/card/remove", {"mode": "hide",
                                           "force_kept": True})

    def test_the_phone_doors_take_the_clients_shapes(self):
        sent = []
        with mock.patch.dict(os.environ, SMS_ENV), \
                mock.patch("oikonome.notify.sms.send",
                           side_effect=lambda to, text:
                           sent.append(text) or True):
            # both clients: a list of programs
            self._ok("/notify/phone", {"number": "+12175551234",
                                       "consent": ["summary"]})
            # an older client: a bare true, and the step-up password inline
            self._ok("/notify/phone", {"number": "+1 217 555 1235",
                                       "consent": True, "password": PW})
            code = sent[-1].rsplit(" ", 1)[-1]
            # the code as typed into the field, pasted with a space
            self._ok("/notify/phone/verify",
                     {"code": f"{code[:3]} {code[3:]}"})
            self._ok("/notify/test", {"channel": "sms"})
            # clearing sends the empty number
            self._ok("/notify/phone", {"number": "", "consent": []})

    def test_web_push_takes_the_browsers_subscription(self):
        sub = {"endpoint": ENDPOINT[:-8] + uuid.uuid4().hex[:8],
               "expirationTime": None,
               "keys": browser_keys()}
        # pushSubscribe and pushRebind send toJSON() plus the user agent
        self._ok("/notify/push/subscribe",
                 dict(sub, user_agent="Mozilla/5.0 (X11; Linux x86_64)"))
        self._ok("/notify/push/subscribe", dict(sub, user_agent=""))
        with mock.patch("oikonome.notify.push.send_tenant", return_value=1):
            self._ok("/notify/test", {"channel": "push"})
        self._ok("/notify/push/unsubscribe", {"endpoint": sub["endpoint"]})

    def test_a_support_grant_takes_the_clients_shapes(self):
        # web select and mobile chips send a number; the reason trimmed
        self._ok("/support-access", {"hours": 24, "reason": ""})
        self._ok("/support-access", {"hours": 1, "reason": "export help"})
        # an older web build sent the password with it
        self._ok("/support-access", {"hours": 72, "reason": "",
                                     "password": PW})
        # absent hours is the default window
        out = self._ok("/support-access", {"reason": ""})
        self.assertEqual(out["hours"], 24)


if __name__ == "__main__":
    unittest.main()
