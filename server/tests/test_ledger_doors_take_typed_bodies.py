"""The ledger, merchant, link, reimbursement and provider-key doors read
their JSON bodies through typed accessors, and still take every body the
released clients send.

A JSON body is whatever the client sent. Read as a plain dict, every field
was a separate type decision, and the ones nobody made surfaced as a 500
(`.strip()` on an int, a list bound into SQL) or as a wrongly typed value
reaching the engine. These doors now read through `web.fields.Fields`.

Three checks pin that:

* the matrix: every key each door reads, sent one at a time inside an
  otherwise valid body as an int, a list, an object, a 10 kB string and a
  NUL character, is answered below 500 — and so is each key inside a
  split's parts;
* the shipped shapes: the bodies the web and mobile clients really send,
  copied from their call sites (older shapes included — an installed
  phone build cannot be changed by a server deploy), are each answered
  with a 200;
* the source: none of these doors reads its body raw any more.
"""

import ast
import datetime as dt
import inspect
import re
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, all_routes, seed_accounts, write_config

BAD_VALUES = (7, [1], {"a": 1}, "x" * 10240, "\u0000")

PASSWORD = "correct-horse-battery"

# (method, registered route path) -> the keys the door reads and a valid
# body; the request path fills the path parameter with a real row
DOORS = {
    ("POST", "/api/transactions/{txn_id:path}/category"):
        (("category", "scope"), {"category": "FOOD_AND_DRINK",
                                 "scope": "one"}),
    ("PUT", "/api/transactions/{txn_id:path}/split"):
        (("parts",), {"parts": [{"category": "FOOD_AND_DRINK", "amount": 30},
                                {"category": "GENERAL_MERCHANDISE",
                                 "amount": 20}]}),
    ("POST", "/api/accounts/{account_id}/owner"):
        (("owner",), {"owner": "Robin"}),
    ("POST", "/api/transactions/bulk"):
        (("ids", "action", "category"),
         {"ids": ["SELF"], "action": "category",
          "category": "FOOD_AND_DRINK"}),
    ("POST", "/api/transactions/retire-pending"):
        (("id",), {"id": "no-such-hold"}),
    ("POST", "/api/merchants/merge"):
        (("pid", "action", "bulk", "into_id"),
         {"pid": "mm:none", "action": "reject"}),
    ("POST", "/api/merchants/rename"):
        (("display", "to"), {"display": "Nobody Here", "to": "Somebody"}),
    ("POST", "/api/merchants/undo"):
        (("id",), {"id": 999999}),
    ("POST", "/api/transactions/{txn_id:path}/note"):
        (("note",), {"note": "lunch"}),
    ("POST", "/api/transactions/{txn_id:path}/owner"):
        (("owner",), {"owner": "Robin"}),
    ("POST", "/api/accounts/mx/keys"):
        (("password", "totp_code", "recovery_code", "client_id", "api_key",
          "env"),
         {"client_id": "c1", "api_key": "k1", "env": "sandbox"}),
    ("POST", "/api/accounts/plaid/keys"):
        (("password", "totp_code", "recovery_code", "client_id", "secret",
          "env"),
         {"client_id": "c1", "secret": "s1", "env": "sandbox"}),
    ("POST", "/api/connections/export"):
        (("password", "totp_code", "recovery_code", "passphrase"),
         {"passphrase": "a long enough passphrase"}),
    ("POST", "/api/accounts/plaid/validate"):
        (("client_id", "secret", "env"),
         {"client_id": "c1", "secret": "s1", "env": "sandbox"}),
    ("POST", "/api/accounts/links"):
        (("account_ids", "auto_order"),
         {"account_ids": ["no-a", "no-b"], "auto_order": True}),
    ("POST", "/api/accounts/links/{group_id}/order"):
        (("account_ids",), {"account_ids": ["no-a", "no-b"]}),
    ("POST", "/api/accounts/links/dismiss"):
        (("key", "card"), {"key": "no-a|no-b"}),
    ("POST", "/api/reimburse/link"):
        (("txn_id", "other_ids", "partial"),
         {"txn_id": "SELF", "other_ids": ["no-such"], "partial": False}),
    ("POST", "/api/reimburse/unlink"):
        (("expense_id", "reimburse_id"),
         {"expense_id": "no-a", "reimburse_id": "no-b"}),
}

# the keys a handler reads, before or after the typed reader
_KEY_PATTERNS = (
    r'body\.(?:get|text|ident|flag|money|int_in|date|one_of|list_of|present'
    r'|sub|opaque|opaque_rows|json_type|money_given'
    r'|text_or_int|text_or_list|number)\(\s*"([A-Za-z_]+)"',
    r'body\[\s*"([A-Za-z_]+)"\s*\]',
    r'_(?:body_text|category_text|category_name|label_or_400|label_field)'
    r'\(\s*body\s*,\s*"([A-Za-z_]+)"',
)


def _harvest(src: str) -> set[str]:
    keys: set[str] = set()
    for pat in _KEY_PATTERNS:
        keys.update(re.findall(pat, src))
    return keys


def _raw_reads(fn) -> list[str]:
    """`body.get(`, `body[...]` and `... in body` inside a handler."""
    tree = ast.parse(inspect.getsource(fn).lstrip())
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "get"
                and isinstance(node.value, ast.Name)
                and node.value.id == "body"):
            out.append(f"body.get (line {node.lineno})")
        elif (isinstance(node, ast.Subscript)
              and isinstance(node.value, ast.Name)
              and node.value.id == "body"):
            out.append(f"body[...] (line {node.lineno})")
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if (isinstance(op, (ast.In, ast.NotIn))
                        and isinstance(right, ast.Name)
                        and right.id == "body"):
                    out.append(f"... in body (line {node.lineno})")
    return out


def _routes(app):
    from .util import is_api_endpoint
    found = {}
    for r in all_routes(app):
        if not is_api_endpoint(r):
            continue
        for m in r.methods:
            if (m, r.path) in DOORS:
                found[(m, r.path)] = r
    return found


def _no_network_plaid(*_a, **_k):
    resp = mock.Mock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = {"institutions": []}
    return resp


class _Tenant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app, raise_server_exceptions=False)
        r = cls.client.post("/api/signup", data={
            "email": f"ledgertypes-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PASSWORD})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.seed(conn)
        finally:
            conn.close()

    @classmethod
    def seed(cls, conn):
        pass

    def _send(self, method, path, body):
        from oikonome.web import security
        security._limiter._hits.clear()
        return self.client.request(method, path, json=body)

    def _quiet(self):
        """No live provider calls and no step-up sheet: what is under test
        is the body reader, not the providers or the elevation rule."""
        return (mock.patch("oikonome.web.app._require_elevation"),
                mock.patch("oikonome.sync.mx.validate", return_value=True),
                mock.patch("httpx.post", side_effect=_no_network_plaid),
                mock.patch("oikonome.web.app._notify_security_change"))


class LedgerDoorsRefuseWrongTypes(_Tenant):
    @classmethod
    def seed(cls, conn):
        today = dt.date.today()
        cls.txn = add_txn(conn, today, 50.0, "MATRIX MARKET",
                          merchant="Matrix Market")

    def _path(self, route_path):
        return (route_path.replace("{txn_id:path}", self.txn)
                .replace("{account_id}", "card")
                .replace("{group_id}", str(uuid.uuid4())))

    def _base(self, body):
        out = dict(body)
        for k, v in out.items():
            if v == "SELF":
                out[k] = self.txn
            elif isinstance(v, list):
                out[k] = [self.txn if x == "SELF" else x for x in v]
        return out

    def test_the_matrix_names_every_key_each_door_reads(self):
        found = _routes(self.app)
        self.assertEqual(set(found), set(DOORS), "a door went missing")
        for key, route in found.items():
            harvested = _harvest(inspect.getsource(route.endpoint))
            self.assertLessEqual(harvested, set(DOORS[key][0]),
                                 f"{key}: a key read but not in the matrix")

    def test_every_key_answers_a_wrong_type_below_500(self):
        failures = []
        patches = self._quiet()
        for p in patches:
            p.start()
        try:
            for (method, route_path), (keys, base) in DOORS.items():
                for key in keys:
                    for bad in BAD_VALUES:
                        body = self._base(base)
                        body[key] = bad
                        r = self._send(method, self._path(route_path), body)
                        if r.status_code >= 500:
                            failures.append(
                                f"{method} {route_path} {key}="
                                f"{str(bad)[:20]!r} -> {r.status_code}")
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(failures, [])

    def test_every_key_of_a_split_part_answers_a_wrong_type_below_500(self):
        failures = []
        path = f"/api/transactions/{self.txn}/split"
        for key in ("category", "amount"):
            for bad in BAD_VALUES:
                part = {"category": "FOOD_AND_DRINK", "amount": 30}
                part[key] = bad
                body = {"parts": [part, {"category": "GENERAL_MERCHANDISE",
                                         "amount": 20}]}
                r = self._send("PUT", path, body)
                if r.status_code >= 500:
                    failures.append(f"parts.{key}={str(bad)[:20]!r} -> "
                                    f"{r.status_code}")
            # and each part itself the wrong type
        for bad in BAD_VALUES:
            r = self._send("PUT", path, {"parts": [bad, bad]})
            if r.status_code >= 500:
                failures.append(f"parts[i]={str(bad)[:20]!r} -> "
                                f"{r.status_code}")
        self.assertEqual(failures, [])

    def test_a_wrongly_typed_field_is_a_400_naming_it(self):
        cases = [
            ("POST", f"/api/transactions/{self.txn}/note", {"note": 7},
             "note"),
            ("POST", "/api/transactions/bulk",
             {"ids": [self.txn], "action": ["category"]}, "action"),
            ("POST", "/api/transactions/bulk",
             {"ids": [7], "action": "biz_on"}, "ids"),
            ("POST", "/api/accounts/links/dismiss", {"card": "false"},
             "card"),
            ("POST", "/api/reimburse/link",
             {"txn_id": self.txn, "other_ids": ["x"], "partial": "false"},
             "partial"),
            ("POST", "/api/reimburse/unlink",
             {"expense_id": {"a": 1}, "reimburse_id": "x"}, "expense_id"),
            ("POST", "/api/accounts/links",
             {"account_ids": [["chk"], "card"]}, "account_ids"),
            # a part's amount is a JSON number, as the engine always held
            ("PUT", f"/api/transactions/{self.txn}/split",
             {"parts": [{"category": "FOOD_AND_DRINK", "amount": "30"},
                        {"category": "GENERAL_MERCHANDISE", "amount": 20}]},
             "part 1 needs an amount"),
        ]
        for method, path, body, field in cases:
            r = self._send(method, path, body)
            self.assertEqual(r.status_code, 400, (path, body, r.text))
            self.assertIn(field, r.json()["detail"], (path, body))

    def test_no_door_reads_its_body_raw(self):
        from oikonome.web.fields import Fields
        offenders = {}
        for key, route in _routes(self.app).items():
            reads = _raw_reads(route.endpoint)
            if reads:
                offenders[key] = reads
            body = inspect.signature(
                route.endpoint, eval_str=True).parameters.get("body")
            if body is None or body.annotation is not Fields:
                offenders.setdefault(key, []).append(
                    f"body is {getattr(body, 'annotation', None)!r}")
        self.assertEqual(offenders, {})


class ShippedClientBodiesAreAccepted(_Tenant):
    """The bodies the released web and mobile clients really send, copied
    from webapp/src/api/client.ts, mobile/src/lib/api.ts and the screens
    that build them. Every one must still be answered with a 200."""

    @classmethod
    def seed(cls, conn):
        today = dt.date.today()
        cls.spend = add_txn(conn, today, 50.0, "SHAPE MARKET",
                            merchant="Shape Market")
        cls.other = add_txn(conn, today, 12.0, "SHAPE CAFE",
                            merchant="Shape Cafe")
        cls.deposit = add_txn(conn, today, -50.0, "SHAPE PAYBACK",
                              account="chk", primary="TRANSFER_IN")
        cls.hold = add_txn(conn, today - dt.timedelta(days=60), 9.0,
                           "SHAPE ABANDONED HOLD", pending=1)
        add_txn(conn, today, 7.0, "QUILLWORTH PAPER", merchant="Quillworth")
        for aid, name in (("sav1", "Shape Savings"),
                          ("sav2", "Shape Savings Mirror"),
                          ("sav3", "Shape Savings Third"),
                          ("sav4", "Shape Savings Fourth")):
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES (%s,'it1',%s,'depository',"
                "'savings',100)", (aid, name))
        f = conn.execute("INSERT INTO merchants (name) VALUES "
                         "('Shape Kiosk') RETURNING id").fetchone()["id"]
        i = conn.execute("INSERT INTO merchants (name) VALUES "
                         "('Shape Kiosk Co') RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO merchant_merge_proposals (id, "
                     "from_merchant_id, into_merchant_id) VALUES "
                     "('mm:shape', %s, %s)", (f, i))

    def _ok(self, method, path, body):
        patches = self._quiet()
        for p in patches:
            p.start()
        try:
            r = self._send(method, path, body)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(r.status_code, 200, (path, body, r.text))
        return r

    def test_category_takes_the_shipped_shapes(self):
        p = f"/api/transactions/{self.spend}/category"
        self._ok("POST", p, {"category": "FOOD_AND_DRINK", "scope": "one"})
        self._ok("POST", p, {"category": "FOOD_AND_DRINK", "scope": "all"})
        self._ok("POST", p, {"category": "", "scope": "one"})

    def test_split_takes_the_shipped_shapes(self):
        p = f"/api/transactions/{self.spend}/split"
        # web: Number(text) cents-rounded; mobile: sign * cents / 100
        self._ok("PUT", p, {"parts": [
            {"category": "FOOD_AND_DRINK", "amount": 30},
            {"category": "GENERAL_MERCHANDISE", "amount": 20}]})
        self._ok("PUT", p, {"parts": [
            {"category": "FOOD_AND_DRINK", "amount": 30.25},
            {"category": "GENERAL_MERCHANDISE", "amount": 19.75}]})

    def test_owner_doors_take_the_shipped_shapes(self):
        for owner in ("Robin", ""):
            self._ok("POST", "/api/accounts/card/owner", {"owner": owner})
            self._ok("POST", f"/api/transactions/{self.spend}/owner",
                     {"owner": owner})

    def test_bulk_takes_the_shipped_shapes(self):
        # the category is undefined (dropped) for every other action
        self._ok("POST", "/api/transactions/bulk",
                 {"ids": [self.spend, self.other], "action": "category",
                  "category": "FOOD_AND_DRINK"})
        self._ok("POST", "/api/transactions/bulk",
                 {"ids": [self.spend], "action": "biz_on"})
        self._ok("POST", "/api/transactions/bulk",
                 {"ids": [self.spend], "action": "biz_off"})

    def test_retire_pending_takes_the_shipped_shape(self):
        self._ok("POST", "/api/transactions/retire-pending",
                 {"id": self.hold})

    def test_merchant_doors_take_the_shipped_shapes(self):
        self._ok("POST", "/api/merchants/merge",
                 {"pid": "mm:shape", "action": "reject"})
        self._ok("POST", "/api/merchants/rename",
                 {"display": "Quillworth", "to": "Quillworth Paper"})
        conn = tenancy.tenant_connect(self.tid)
        try:
            change = conn.execute(
                "SELECT id FROM merchant_renames ORDER BY id DESC "
                "LIMIT 1").fetchone()["id"]
        finally:
            conn.close()
        # the id is echoed from the catalog's `recent`, a JSON number
        self._ok("POST", "/api/merchants/undo", {"id": change})

    def test_note_takes_the_shipped_shapes(self):
        p = f"/api/transactions/{self.spend}/note"
        r = self._ok("POST", p, {"note": "  split with a friend  "})
        self.assertEqual(r.json()["note"], "split with a friend")
        self._ok("POST", p, {"note": ""})

    def test_provider_key_doors_take_the_shipped_shapes(self):
        body = {"client_id": "c-shape", "secret": "s-shape",
                "env": "sandbox"}
        self._ok("POST", "/api/accounts/plaid/validate", body)
        self._ok("POST", "/api/accounts/plaid/keys", body)
        self._ok("POST", "/api/accounts/mx/keys",
                 {"client_id": "c-shape", "api_key": "k-shape",
                  "env": "sandbox"})

    def test_key_doors_take_a_password_in_the_body(self):
        """A script, and the tests of the step-up rule, send the password
        beside the keys; the real elevation check must still pass it."""
        with mock.patch("oikonome.sync.mx.validate", return_value=True):
            r = self._send("POST", "/api/accounts/mx/keys", {
                "password": PASSWORD, "client_id": "c-pw",
                "api_key": "k-pw", "env": "sandbox"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_export_takes_the_shipped_shapes(self):
        # current clients: the passphrase alone (the sheet elevates first)
        self._ok("POST", "/api/connections/export",
                 {"passphrase": "a shape passphrase long enough"})
        # older builds sent the password, and the codes only when typed
        with mock.patch("oikonome.web.app._notify_security_change"):
            r = self._send("POST", "/api/connections/export", {
                "passphrase": "a shape passphrase long enough",
                "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)

    def test_link_doors_take_the_shipped_shapes(self):
        r = self._ok("POST", "/api/accounts/links",
                     {"account_ids": ["sav1", "sav2"], "auto_order": True})
        gid = r.json()["group_id"]
        self._ok("POST", f"/api/accounts/links/{gid}/order",
                 {"account_ids": ["sav2", "sav1"]})
        self._ok("POST", "/api/accounts/links/dismiss",
                 {"key": "sav3|sav4"})
        self._ok("POST", "/api/accounts/links/dismiss", {"card": True})
        self._ok("POST", "/api/accounts/links/dismiss", {"card": False})

    def test_a_blank_id_list_reads_as_no_ids_as_it_always_did(self):
        # `ids or []` read "" as no ids: a script clearing the list with a
        # blank is answered as an empty list is, never refused for its type
        # (no link is made here: the shipped-shapes test makes its own)
        gid = "no-such-group"
        for method, path, key, rest in (
                ("POST", "/api/transactions/bulk", "ids",
                 {"action": "biz_on"}),
                ("POST", "/api/reimburse/link", "other_ids",
                 {"txn_id": self.deposit}),
                ("POST", "/api/accounts/links", "account_ids", {}),
                ("POST", f"/api/accounts/links/{gid}/order", "account_ids",
                 {})):
            with self.subTest(path=path):
                blank = self._send(method, path, dict(rest, **{key: ""}))
                empty = self._send(method, path, dict(rest, **{key: []}))
                self.assertEqual((blank.status_code, blank.text),
                                 (empty.status_code, empty.text))
                self.assertLess(blank.status_code, 500, blank.text)

    def test_reimburse_doors_take_the_shipped_shapes(self):
        r = self._ok("POST", "/api/reimburse/link",
                     {"txn_id": self.deposit, "other_ids": [self.other],
                      "partial": False})
        self.assertEqual(r.json()["linked"], 1, r.text)
        self._ok("POST", "/api/reimburse/unlink",
                 {"expense_id": self.other, "reimburse_id": self.deposit})
        self._ok("POST", "/api/reimburse/link",
                 {"txn_id": self.deposit, "other_ids": [self.other],
                  "partial": True})


if __name__ == "__main__":
    unittest.main()
