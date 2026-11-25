"""The rule, account, assistant, plan, continuity, alert, receipt and token
doors read their JSON bodies through typed accessors.

A JSON body is whatever the client sends. Read as a plain dict, each field's
type check was a separate decision at each door, and a missed one surfaced as
a 500 (a NUL byte reaching a TEXT column, a list reaching `.strip()`) or as a
wrongly typed value reaching SQL. Three checks pin the typed reading:

* the matrix: every key each door reads, sent one at a time inside an
  otherwise valid body as an int, a list, an object, a 10 kB string and a NUL
  character, is answered below 500;
* the shapes: the bodies the released web and mobile clients really send,
  copied from their call sites (older shapes included — an installed phone
  build cannot be changed by a server deploy), are still accepted;
* the source: none of these doors reads its body raw.
"""

import datetime as dt
import inspect
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .test_bill_doors_refuse_wrong_types import BAD_VALUES, _harvest, _raw_reads
from .util import _ensure_db, add_txn, all_routes, seed_accounts, write_config

PW = "correct-horse-battery"
PNG = ("r.png", b"\x89PNG...", "image/png")
_NO_ENV_LLM = {"OIKONOME_LLM_URL": "", "OIKONOME_LLM_MODEL": ""}
TODAY = dt.date.today()

# the handlers that read a typed body (the orchestrator's shared allowlist
# names the same set)
CONVERTED = (
    "rules_set_api", "categories_rename_api", "rules_disable_api",
    "rules_delete_api", "assistant_ask_api", "accounts_rename_api",
    "accounts_classify_api", "accounts_exclude_api", "accounts_add_api",
    "accounts_balance_api", "accounts_simplefin_api", "debt_plan_save_api",
    "continuity_put", "continuity_email", "alerts_dismiss_api",
    "alerts_restore_api", "receipt_match_by_hand", "receipt_details",
    "receipt_tag", "tokens_mint_api", "tokens_revoke_api",
)


def _canned_answer(conn, question, context=None):
    return {"answer": "About forty dollars.", "blocks": [
        {"kind": "text", "text": "About forty dollars."}], "tools_used": []}


class _Household:
    """One signed-up household with the rows these doors act on."""

    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, _NO_ENV_LLM)
        cls._env.start()
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(appmod.app, raise_server_exceptions=False)
        cls.email = f"typed-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.client.post("/api/signup", data={"email": cls.email,
                                                 "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.txn = add_txn(conn, TODAY, 27.35, "COPPER KETTLE CAFE",
                              merchant="Copper Kettle", account="chk",
                              override="COFFEE")
            add_txn(conn, TODAY, 12.0, "FERN STREET BOOKS",
                    merchant="Fern Street Books", override="Matrix Old Cat")
            conn.execute(
                "INSERT INTO alerts_log (kind, severity, message, first_seen,"
                " last_seen) VALUES ('low_balance', 'warn', %s, %s, %s)",
                ("Checking is under $100", TODAY, TODAY))
        finally:
            conn.close()
        cls._clear()
        r = cls.client.post("/api/accounts/add", json={
            "name": "Coin Jar", "kind": "depository/checking", "balance": "5"})
        assert r.status_code == 200, r.text
        cls.manual = r.json()["account_id"]
        r = cls.client.post("/api/receipts", files={"file": PNG})
        assert r.status_code == 200, r.text
        cls.rid = r.json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            conn.execute("INSERT INTO receipt_items (receipt_id, line, "
                         "description) VALUES (%s::uuid, 1, 'Pour-over')",
                         (cls.rid,))
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    @staticmethod
    def _clear():
        from oikonome.web import security
        security._limiter._hits.clear()

    def _send(self, method, path, body):
        self._clear()
        from oikonome.engine import assistant
        from oikonome.web import setup as _setup
        with mock.patch.object(assistant, "ask", _canned_answer), \
                mock.patch.object(assistant, "available", return_value=True), \
                mock.patch.object(_setup, "connect_simplefin",
                                  side_effect=ValueError("claim refused")), \
                mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "smtp.test",
                                             "OIKONOME_HOSTED": ""}), \
                mock.patch("oikonome.web.report.send"):
            return self.client.request(method, "/api" + path, json=body)


def _doors(cls):
    """(method, path, valid body, nested specs) per door. Built per class
    because the receipt and manual-account ids are the fixture's."""
    rid = cls.rid
    return [
        ("POST", "/rules/set", {"merchant": "Copper Kettle",
                                "category": "COFFEE"}, []),
        ("POST", "/categories/rename", {"old": "No Such Cat",
                                        "new": "Matrix New Cat"}, []),
        ("POST", "/rules/disable", {"merchant": "Copper Kettle",
                                    "disabled": False}, []),
        ("POST", "/rules/delete", {"merchant": "No Such Rule"}, []),
        ("POST", "/assistant", {"question": "what went on coffee"}, []),
        ("POST", "/assistant/report", {
            "question": "what went on coffee",
            "blocks": [{"kind": "text", "text": "About forty dollars."}]},
         [("turns", lambda inner: [inner],
           {"question": "q", "blocks": [{"kind": "text", "text": "a"}]},
           ("question", "blocks"))]),
        ("POST", "/accounts/rename", {"account_id": "chk",
                                      "name": "Test Checking"}, []),
        ("POST", "/accounts/classify", {"account_id": "card",
                                        "kind": "credit/credit card"}, []),
        ("POST", "/accounts/exclude", {"account_id": "card",
                                       "excluded": False}, []),
        ("POST", "/accounts/add", {"name": "Matrix Cash",
                                   "kind": "depository/checking",
                                   "balance": "10"}, []),
        ("POST", "/accounts/balance", {"account_id": cls.manual,
                                       "balance": "12.50"}, []),
        ("POST", "/accounts/simplefin", {"token": "not-a-claim"}, []),
        ("POST", "/debt-plan", {"method": "avalanche", "extra_monthly": 0,
                                "debts": {}},
         [("debts", lambda inner: {"card": inner},
           {"apr": "19.9", "min_payment": 25, "skip": False},
           ("apr", "min_payment", "skip"))]),
        ("PUT", "/continuity", {"note": "The papers are in the hall desk.",
                                "prepared_for": "Robin"}, []),
        ("POST", "/continuity/email", {"to": cls.email}, []),
        ("POST", "/alerts/dismiss", {"kind": "low_balance",
                                     "message": "Checking is under $100"}, []),
        ("POST", "/alerts/restore", {"kind": "low_balance",
                                     "message": "Checking is under $100"}, []),
        ("POST", f"/receipts/{rid}/match", {"txn_id": "no-such-txn"}, []),
        ("POST", f"/receipts/{rid}/details", {"total": "27.35",
                                              "date": TODAY.isoformat(),
                                              "merchant": "Copper Kettle"},
         []),
        ("POST", f"/receipts/{rid}/items/1/tag", {"tag": "business"}, []),
        ("POST", "/tokens", {"name": "matrix", "scope": "push"}, []),
        ("POST", "/tokens/revoke", {"id": str(uuid.uuid4())}, []),
    ]


def _handlers(app):
    from .util import is_api_endpoint
    return {r.endpoint.__name__: r for r in all_routes(app)
            if is_api_endpoint(r)
            and r.endpoint.__name__ in CONVERTED + ("assistant_report_api",)}


class WrongTypesNeverAnswer5xx(_Household, unittest.TestCase):
    def test_the_matrix_names_every_key_each_door_reads(self):
        routes = _handlers(self.app)
        by_path = {}
        for r in routes.values():
            verb = sorted(set(r.methods) - {"HEAD", "OPTIONS"})[0]
            by_path[(verb, r.path)] = r
        missing = []
        for method, path, base, nested in _doors(self):
            tmpl = path.replace(self.rid, "{rid}").replace("/1/tag",
                                                           "/{line}/tag")
            route = by_path[(method, "/api" + tmpl)]
            src = inspect.getsource(route.endpoint)
            covered = set(base) | {n[0] for n in nested}
            for key in _harvest(src):
                if key not in covered and key not in _EXTRA_KEYS.get(path, ()):
                    missing.append(f"{method} {path} {key}")
        self.assertEqual(missing, [])

    def test_every_key_answers_a_wrong_type_below_500(self):
        failures = []
        for method, path, base, _nested in _doors(self):
            keys = list(base) + list(_EXTRA_KEYS.get(path, ()))
            for key in keys:
                for bad in BAD_VALUES:
                    body = dict(base)
                    body[key] = bad
                    r = self._send(method, path, body)
                    if r.status_code >= 500:
                        failures.append(f"{method} {path} {key}="
                                        f"{str(bad)[:20]!r} -> "
                                        f"{r.status_code} {r.text[:80]}")
        self.assertEqual(failures, [])

    def test_every_nested_key_answers_a_wrong_type_below_500(self):
        failures = []
        for method, path, base, nested in _doors(self):
            for outer, wrap, valid, inner_keys in nested:
                for key in inner_keys:
                    for bad in BAD_VALUES:
                        inner = dict(valid)
                        inner[key] = bad
                        body = dict(base)
                        body[outer] = wrap(inner)
                        r = self._send(method, path, body)
                        if r.status_code >= 500:
                            failures.append(
                                f"{method} {path} {outer}.{key}="
                                f"{str(bad)[:20]!r} -> {r.status_code}")
        self.assertEqual(failures, [])


# keys a door reads that its valid base body leaves out
_EXTRA_KEYS = {
    "/categories/rename": ("from", "to"),
    "/assistant": ("thread_id",),
    "/assistant/report": ("turns", "partial"),
    "/accounts/classify": ("primary_checking",),
    "/continuity/email": ("password", "totp_code", "recovery_code"),
    "/tokens": ("password", "totp_code", "recovery_code"),
    "/tokens/revoke": ("password", "totp_code", "recovery_code"),
}


class ShippedClientBodiesAreAccepted(_Household, unittest.TestCase):
    """Each body below is copied from a call site in webapp/src or
    mobile/src (the api wrappers and the screens that build the body),
    older shapes included. A typed door stricter than any of them would
    break the feature on a phone nobody can update from here."""

    def _ok(self, method, path, body, codes=(200,)):
        r = self._send(method, path, body)
        self.assertIn(r.status_code, codes, (method, path, body, r.text))
        return r

    def _elevate(self):
        self._clear()
        r = self.client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_rules_set_disable_delete_take_the_shipped_shapes(self):
        # web and mobile ruleSet / ruleDisable / ruleDelete
        self._ok("POST", "/rules/set", {"merchant": "Copper Kettle",
                                        "category": "COFFEE"})
        self._ok("POST", "/rules/disable", {"merchant": "Copper Kettle",
                                            "disabled": True})
        self._ok("POST", "/rules/disable", {"merchant": "Copper Kettle",
                                            "disabled": False})
        self._ok("POST", "/rules/delete", {"merchant": "No Such Rule"})

    def test_category_rename_takes_the_shipped_shape(self):
        # web and mobile categoryRename send old/new
        self._ok("POST", "/categories/rename", {"old": "Matrix Old Cat",
                                                "new": "Matrix Renamed"})
        # the door's documented aliases
        self._ok("POST", "/categories/rename", {"from": "Matrix Renamed",
                                                "to": "Matrix Old Cat"})

    def test_assistant_ask_takes_the_shipped_shapes(self):
        r = self._ok("POST", "/assistant", {"question": "what went on coffee"})
        thread = r.json()["thread_id"]
        self.assertTrue(thread)
        # a follow-up echoes the thread id the reply carried
        self._ok("POST", "/assistant", {"question": "and last month",
                                        "thread_id": thread})

    def test_assistant_report_takes_the_shipped_shapes(self):
        blocks = [{"kind": "text", "text": "About forty dollars."}]
        # older clients: one answer
        self._ok("POST", "/assistant/report",
                 {"question": "what went on coffee", "blocks": blocks})
        # current clients: the turns plus the first turn as the old body
        turns = [{"question": "what went on coffee", "blocks": blocks},
                 {"question": "and last month", "blocks": blocks}]
        for partial in (False, True):
            self._ok("POST", "/assistant/report", {
                "turns": turns, "partial": partial,
                "question": turns[0]["question"], "blocks": blocks})

    def test_account_doors_take_the_shipped_shapes(self):
        # rename: a typed name, or "" to revert to the bank's name
        self._ok("POST", "/accounts/rename", {"account_id": "chk",
                                              "name": "Everyday"})
        self._ok("POST", "/accounts/rename", {"account_id": "chk",
                                              "name": ""})
        # classify: primary_checking left out, or a boolean
        self._ok("POST", "/accounts/classify", {
            "account_id": "card", "kind": "credit/credit card"})
        self._ok("POST", "/accounts/classify", {
            "account_id": "chk", "kind": "depository/checking",
            "primary_checking": True})
        self._ok("POST", "/accounts/classify", {
            "account_id": "chk", "kind": "depository/checking",
            "primary_checking": False})
        # exclude: a boolean
        self._ok("POST", "/accounts/exclude", {"account_id": "card",
                                               "excluded": True})
        self._ok("POST", "/accounts/exclude", {"account_id": "card",
                                               "excluded": False})
        # add: balance "" by default, a typed string, and the business
        # wizards' bare subtype with no balance key
        self._ok("POST", "/accounts/add", {"name": "Shape Cash",
                                           "kind": "depository/savings",
                                           "balance": ""})
        self._ok("POST", "/accounts/add", {"name": "Shape Jar",
                                           "kind": "depository/checking",
                                           "balance": "1,234.56"})
        self._ok("POST", "/accounts/add", {"name": "Shape Biz",
                                           "kind": "checking",
                                           "balance": ""})
        # balance: the text the person typed
        self._ok("POST", "/accounts/balance", {"account_id": self.manual,
                                               "balance": "42.10"})
        self._ok("POST", "/accounts/balance", {"account_id": self.manual,
                                               "balance": "-3"})

    def test_simplefin_takes_the_shipped_shape(self):
        # the claim token is a string; the stubbed claim refuses it, which
        # is the door's own 400, not a typing refusal
        r = self._send("POST", "/accounts/simplefin", {"token": "aGVsbG8="})
        self.assertIn("connection failed", r.text)

    def test_debt_plan_takes_the_shipped_shapes(self):
        # extra_monthly a number or the typed text, overrides with "" sent
        # as null, numbers, the typed text and the skip switch
        self._ok("POST", "/debt-plan", {
            "method": "avalanche", "extra_monthly": 150,
            "debts": {"card": {"apr": "19.99", "min_payment": None,
                               "skip": False}}})
        self._ok("POST", "/debt-plan", {
            "method": "snowball", "extra_monthly": "75.50",
            "debts": {"card": {"apr": None, "min_payment": 35,
                               "skip": True},
                      "gone-loan": {"apr": 6.5}}})
        self._ok("POST", "/debt-plan", {"method": "avalanche",
                                        "extra_monthly": "", "debts": {}})

    def test_continuity_takes_the_shipped_shapes(self):
        self._ok("PUT", "/continuity", {
            "note": "The will is in the hall desk.\nPasswords: the safe.",
            "prepared_for": "Robin"})
        self._ok("PUT", "/continuity", {"note": "", "prepared_for": ""})
        self._elevate()
        self._ok("POST", "/continuity/email", {"to": self.email})

    def test_alert_doors_take_the_shipped_shapes(self):
        body = {"kind": "low_balance", "message": "Checking is under $100"}
        r = self._ok("POST", "/alerts/dismiss", body)
        self.assertEqual(r.json()["dismissed"], 1)
        r = self._ok("POST", "/alerts/restore", body)
        self.assertEqual(r.json()["restored"], 1)

    def test_receipt_doors_take_the_shipped_shapes(self):
        rid = self.rid
        # the tag the person typed, and "" to clear it
        self._ok("POST", f"/receipts/{rid}/items/1/tag", {"tag": "business"})
        self._ok("POST", f"/receipts/{rid}/items/1/tag", {"tag": ""})
        # details: total a number rounded to the cent, merchant only when
        # typed
        self._ok("POST", f"/receipts/{rid}/details", {
            "total": 27.35, "date": TODAY.isoformat(),
            "merchant": "Copper Kettle"})
        self._ok("POST", f"/receipts/{rid}/details", {
            "total": 27, "date": TODAY.isoformat()})
        # match: the txn id echoed from the waiting list (the receipt may
        # already be paired by the details save, a 404 then)
        self._ok("POST", f"/receipts/{rid}/match", {"txn_id": self.txn},
                 codes=(200, 404))

    def test_token_doors_take_the_shipped_shapes(self):
        self._elevate()
        # current clients: name and scope, the step-up done beforehand
        r = self._ok("POST", "/tokens", {"name": "collector", "scope": "push"})
        self._ok("POST", "/tokens/revoke", {"id": r.json()["id"]})
        r = self._ok("POST", "/tokens", {"name": "reader", "scope": "read"})
        self._ok("POST", "/tokens/revoke", {"id": r.json()["id"]})
        # older clients carried the proof in the body
        r = self._ok("POST", "/tokens", {"name": "old build", "password": PW,
                                         "recovery_code": "", "totp_code": ""})
        self._ok("POST", "/tokens/revoke", {"id": r.json()["id"],
                                            "password": PW,
                                            "recovery_code": "",
                                            "totp_code": ""})
        # a script minting with no name and no scope
        r = self._ok("POST", "/tokens", {"password": PW})
        self.assertEqual(r.json()["scope"], "push")


class NullBlankAndAbsentKeepTheirMeaning(_Household, unittest.TestCase):
    """The typed readers read null and "" alike; where a door always told
    them apart, or coerced a script's scalar, it still does."""

    def test_a_null_extra_payment_is_refused_and_a_blank_one_is_zero(self):
        r = self._send("POST", "/debt-plan", {"extra_monthly": None})
        self.assertEqual(r.status_code, 400, r.text)
        r = self._send("POST", "/debt-plan", {"extra_monthly": ""})
        self.assertEqual(r.status_code, 200, r.text)
        r = self._send("POST", "/debt-plan", {"method": None})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_null_manual_balance_is_refused_and_a_blank_one_is_zero(self):
        r = self._send("POST", "/accounts/balance",
                       {"account_id": self.manual, "balance": None})
        self.assertEqual(r.status_code, 400, r.text)
        r = self._send("POST", "/accounts/balance",
                       {"account_id": self.manual, "balance": ""})
        self.assertEqual(r.status_code, 200, r.text)

    def _store(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return receipts_merchant(conn, self.rid)
        finally:
            conn.close()

    def test_a_receipt_store_left_out_or_null_is_kept_and_blank_clears(self):
        path = f"/receipts/{self.rid}/details"
        day = TODAY.isoformat()
        self.assertEqual(self._send("POST", path, {
            "total": 9.5, "date": day, "merchant": "Fern Street Books"}
        ).status_code, 200)
        for kept in ({"total": 9.5, "date": day},
                     {"total": 9.5, "date": day, "merchant": None}):
            self.assertEqual(self._send("POST", path, kept).status_code, 200)
            self.assertEqual(self._store(), "Fern Street Books")
        self.assertEqual(self._send("POST", path, {
            "total": 9.5, "date": day, "merchant": ""}).status_code, 200)
        self.assertIsNone(self._store())

    def test_a_script_sending_a_number_still_reaches_the_token_doors(self):
        # str() of the scalar, as always: a numeric id is simply not found
        r = self._send("POST", "/tokens/revoke", {"id": 7, "password": PW})
        self.assertEqual(r.status_code, 404, r.text)
        r = self._send("POST", "/tokens", {"name": 2026, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "2026")

    def test_a_rule_disable_with_no_state_re_enables(self):
        self._send("POST", "/rules/set", {"merchant": "Copper Kettle",
                                          "category": "COFFEE"})
        self.assertEqual(self._send("POST", "/rules/disable", {
            "merchant": "Copper Kettle", "disabled": True}).status_code, 200)
        self.assertEqual(self._send("POST", "/rules/disable", {
            "merchant": "Copper Kettle"}).status_code, 200)
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT disabled FROM merchant_categories WHERE merchant = %s",
                ("Copper Kettle",)).fetchone()
        finally:
            conn.close()
        if row is not None:
            self.assertFalse(row["disabled"])


def receipts_merchant(conn, rid):
    row = conn.execute("SELECT parsed FROM receipts WHERE id = %s::uuid",
                       (rid,)).fetchone()
    parsed = (row or {}).get("parsed") or {}
    return parsed.get("merchant")


class TheseDoorsReadTheirBodiesTyped(unittest.TestCase):
    def test_no_converted_door_reads_its_body_raw(self):
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        from oikonome.web.fields import Fields
        routes = _handlers(appmod.app)
        offenders = {}
        for name in CONVERTED:
            route = routes.get(name)
            if route is None:
                offenders[name] = ["no such handler"]
                continue
            reads = _raw_reads(route.endpoint)
            src = inspect.getsource(route.endpoint)
            if "_body_text(body" in src or "_label_or_400(body" in src \
                    or "_category_text(body" in src:
                reads.append("a dict-body helper")
            if reads:
                offenders[name] = reads
            body = inspect.signature(
                route.endpoint, eval_str=True).parameters.get("body")
            if body is None or body.annotation is not Fields:
                offenders.setdefault(name, []).append(
                    f"body is {getattr(body, 'annotation', None)!r}")
        self.assertEqual(offenders, {})


if __name__ == "__main__":
    unittest.main()
