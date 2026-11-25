"""Every bill door answers a wrongly typed body field with a 4xx, never a 5xx.

A JSON body is whatever the client sends: `{"payee": 7}`, `{"cap": [1]}`,
`{"category": "\\u0000"}`. Read as a plain dict, each door had to type-check
each field itself, and a field nobody checked reached `.strip()` on an int,
a `%s` bind as a list, or a TEXT column carrying a NUL byte — an opaque 500
and a traceback for what is ordinary bad input. The bill doors now read their
bodies through `web.fields.Fields`, whose typed accessors refuse a wrong type
with a 400 naming the field.

Two checks pin that:

* the matrix: for every POST door under /bills/, the keys its handler reads
  are harvested from its source and each is sent, one at a time inside an
  otherwise valid body, as an int, a list, an object, a 10 kB string and a
  NUL character; the answer must be below 500. Nested objects (an offer's
  `adjust`, a bill's `companions`, the undo snapshot) get the same matrix
  for their own keys.
* the shape: no bill door reads its body raw — it takes no body as a plain
  dict, however that dict is then read (`body.get(`, `body[`, `"key" in
  body`, a renamed copy, the dict handed to a helper) — and the doors
  elsewhere in api.py that still do are an allowlist that may only shrink.
"""

import ast
import inspect
import re
import unittest
import uuid

from fastapi import Body, Depends
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web.api import _json_fields_or_empty
from oikonome.web.fields import Fields, json_fields

from .util import _ensure_db, add_bill, all_routes, seed_accounts, write_config

BAD_VALUES = (7, [1], {"a": 1}, "x" * 10240, "\u0000")

# the keys a handler reads from its body, before or after the typed reader
_KEY_PATTERNS = (
    r'body\.(?:get|text|ident|flag|money|int_in|date|one_of|list_of|present'
    r'|sub|opaque|opaque_rows|json_type|money_given'
    r'|text_or_int|text_or_list|number)\(\s*"([A-Za-z_]+)"',
    r'body\[\s*"([A-Za-z_]+)"\s*\]',
    r'"([A-Za-z_]+)"\s+(?:not\s+)?in\s+body\b',
    r'_(?:body_text|category_text|category_name|alert_key|label_or_400)\(\s*body\s*,'
    r'\s*"([A-Za-z_]+)"',
)

# a valid body per door, so the field under test is the one that is wrong
_BASE = {
    "/bills/save": {"payee": "Matrix Gym", "amount": "25",
                    "cadence": "MONTHLY:1"},
    "/bills/proposal": {"pid": "no-such-offer", "action": "approve",
                        "adjust": {}},
    "/bills/delete": {"payee": "Not A Bill", "mode": "archive"},
    "/bills/restore": {"payee": "Not A Bill"},
    "/bills/toggle": {"payee": "Matrix Toggle", "disabled": True},
    "/bills/hint": {"payee": "Matrix Gym", "action": "dismiss",
                    "target": "fit"},
    "/bills/merchant-category/preview": {"payee": "Matrix Gym",
                                         "category": "Fitness"},
    "/bills/merchant-category": {"payee": "Matrix Gym",
                                 "category": "Fitness"},
    "/bills/merchant-category/undo": {"undo": {"ref": "no-such-ref"}},
    "/bills/proposals/approve-all": {"pids": []},
}

# nested objects: the key that holds them, how one is placed in the body, a
# valid element, and the keys read inside it
_NESTED = {
    "/bills/proposal": [
        ("adjust", lambda inner: inner,
         {}, ("name", "amount", "cadence", "by_month_day", "due_day"))],
    "/bills/save": [
        ("companions", lambda inner: [inner],
         {"tokens": "late fee", "amount": "5", "window_days": 3},
         ("tokens", "amount", "window_days"))],
    "/bills/merchant-category/undo": [
        ("undo", lambda inner: inner,
         {"ref": "no-such-ref"}, ("ref", "merchant", "rows", "rules"))],
}

# Doors in api.py that still read a raw dict body: none. The list stays
# so the check below has one place to say so; nothing is ever added.
UNCONVERTED: frozenset = frozenset()


def _bill_doors(app):
    from .util import is_api_endpoint
    return [r for r in all_routes(app)
            if "POST" in r.methods and r.path.startswith("/api/bills/")
            and is_api_endpoint(r)]


def _harvest(src: str) -> list[str]:
    keys: list[str] = []
    for pat in _KEY_PATTERNS:
        keys.extend(re.findall(pat, src))
    return list(dict.fromkeys(keys))


_DICT_READS = frozenset({"get", "items", "keys", "values", "pop",
                         "setdefault", "copy"})


def _takes_raw_body(param) -> bool:
    """Whether a handler parameter hands the handler the JSON body as a
    plain dict: a `Body(...)` not typed `Fields`, or a dependency that
    itself takes such a body and does not answer a `Fields`."""
    from fastapi import params

    from oikonome.web.fields import Fields
    d = param.default
    if isinstance(d, params.Form):
        # a form field or an uploaded file, not a JSON body
        return False
    if isinstance(d, params.Body):
        return param.annotation is not Fields
    if isinstance(d, params.Depends) and d.dependency is not None:
        try:
            sig = inspect.signature(d.dependency, eval_str=True)
        except (TypeError, ValueError, NameError):
            return False
        if sig.return_annotation is Fields:
            return False
        return any(_takes_raw_body(p) for p in sig.parameters.values())
    return False


def _raw_reads(fn) -> list[str]:
    """How a handler reads a JSON body raw, one line per way.

    A door reads raw when it declares a body that reaches it as a plain
    dict: whatever it then does with it — `body.get(`, `body[...]`, `... in
    body`, `(body or {}).items()`, a renamed copy, or the dict handed to a
    helper such as `_body_text(body, …)` — no type was checked on the way
    in. The signature decides that; the source walk below names the lines,
    following the dict through any local it is assigned to. Reaching past
    the typed reader into `Fields._d` counts too."""
    try:
        sig = inspect.signature(fn, eval_str=True)
    except (TypeError, ValueError, NameError):
        return []
    raw = {n for n, p in sig.parameters.items() if _takes_raw_body(p)}
    out = [f"{n} is a raw dict body" for n in sorted(raw)]
    try:
        tree = ast.parse(inspect.getsource(fn))
    except (OSError, TypeError, SyntaxError):
        return out

    def _is_raw(node) -> bool:
        if isinstance(node, ast.Name):
            return node.id in raw
        if isinstance(node, ast.BoolOp):
            return any(_is_raw(v) for v in node.values)
        if isinstance(node, ast.IfExp):
            return _is_raw(node.body) or _is_raw(node.orelse)
        return False

    # a local assigned the dict (`b = body or {}`) is the dict
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_raw(node.value):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id not in raw:
                        raw.add(t.id)
                        grew = True
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute) and node.attr == "_d":
            out.append(f"Fields._d (line {line})")
        elif (isinstance(node, ast.Attribute) and node.attr in _DICT_READS
              and _is_raw(node.value)):
            out.append(f"body.{node.attr} (line {line})")
        elif isinstance(node, ast.Subscript) and _is_raw(node.value):
            out.append(f"body[...] (line {line})")
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and _is_raw(right):
                    out.append(f"... in body (line {line})")
        elif isinstance(node, ast.Call):
            args = list(node.args) + [k.value for k in node.keywords]
            if any(_is_raw(a) for a in args):
                out.append(f"body handed to a helper (line {line})")
    return out


class BillDoorsRefuseWrongTypesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(appmod.app, raise_server_exceptions=False)
        cls.client.post("/api/signup", data={
            "email": f"billtypes-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_bill(conn, "Matrix Gym", 25.0, next_due="2026-03-05")
            add_bill(conn, "Matrix Toggle", 40.0, next_due="2026-03-09")
        finally:
            conn.close()

    def _post(self, path, body):
        return self.client.post("/api" + path, json=body)

    def test_the_matrix_covers_every_bill_door_with_a_body(self):
        with_body = {r.path[len("/api"):] for r in _bill_doors(self.app)
                     if _harvest(inspect.getsource(r.endpoint))}
        self.assertEqual(with_body, set(_BASE),
                         "a bill door with a body needs a matrix entry")

    def test_every_key_a_bill_door_reads_refuses_a_wrong_type(self):
        failures = []
        for route in _bill_doors(self.app):
            path = route.path[len("/api"):]
            if path not in _BASE:
                continue
            for key in _harvest(inspect.getsource(route.endpoint)):
                for bad in BAD_VALUES:
                    body = dict(_BASE[path])
                    body[key] = bad
                    r = self._post(path, body)
                    if r.status_code >= 500:
                        failures.append(
                            f"{path} {key}={str(bad)[:20]!r} -> "
                            f"{r.status_code}")
        self.assertEqual(failures, [])

    def test_every_nested_key_refuses_a_wrong_type(self):
        failures = []
        for path, specs in _NESTED.items():
            for outer, wrap, valid, inner_keys in specs:
                for key in inner_keys:
                    for bad in BAD_VALUES:
                        inner = dict(valid)
                        inner[key] = bad
                        body = dict(_BASE[path])
                        body[outer] = wrap(inner)
                        r = self._post(path, body)
                        if r.status_code >= 500:
                            failures.append(
                                f"{path} {outer}.{key}={str(bad)[:20]!r} "
                                f"-> {r.status_code}")
        self.assertEqual(failures, [])

    def test_no_bill_door_reads_its_body_raw(self):
        from oikonome.web.fields import Fields
        offenders = {}
        for route in _bill_doors(self.app):
            reads = _raw_reads(route.endpoint)
            if reads:
                offenders[route.path] = reads
            body = inspect.signature(
                route.endpoint, eval_str=True).parameters.get("body")
            if body is not None and body.annotation is not Fields:
                offenders.setdefault(route.path, []).append(
                    f"body is {body.annotation!r}, not Fields")
        self.assertEqual(offenders, {})

    def test_the_raw_body_allowlist_only_shrinks(self):
        from .util import is_api_endpoint
        raw = set()
        for route in all_routes(self.app):
            if not is_api_endpoint(route):
                continue
            if _raw_reads(route.endpoint):
                verb = sorted(set(route.methods) - {"HEAD", "OPTIONS"})[0]
                raw.add(f"{verb}:{route.path}")
        self.assertEqual(
            sorted(raw - UNCONVERTED), [],
            "a door takes its JSON body as a plain dict (a Body() not typed "
            "Fields, or a dependency handing one over): declare it as "
            "`body: Fields = Depends(json_fields)` (or "
            "`Depends(json_fields_or_empty)` for an optional body) and read "
            "every field through a typed accessor in web/fields.py")
        self.assertEqual(sorted(UNCONVERTED - raw), [],
                         "a door on the allowlist was converted: drop it "
                         "from UNCONVERTED")


# Handlers of each shape the raw-read check must see, and of the shapes it
# must leave alone. Never routed: only their source and signature are read.
def _pick(d, key):
    return d.get(key)


def _raw_dep(body: dict = Body(...)) -> dict:
    return body


def _shape_renamed(b: dict = Body(...)):
    got = b or {}
    return [k for k, _ in got.items()]


def _shape_handed_to_a_helper(body: dict = Body(...)):
    return _pick(body, "payee")


def _shape_optional_body(body: dict | None = Body(default=None)):
    return (body or {}).items()


def _shape_through_a_dependency(payload: dict = Depends(_raw_dep)):
    return _pick(payload, "payee")


def _shape_past_the_typed_reader(body: Fields = Depends(json_fields)):
    return body._d


def _shape_typed(body: Fields = Depends(json_fields)):
    return body.text("payee")


def _shape_typed_optional(body: Fields = Depends(_json_fields_or_empty)):
    return body.text("payee")


def _shape_a_local_named_body():
    body = {"payee": "x"}
    return body.get("payee")


class TheRawReadCheckSeesEveryRawRead(unittest.TestCase):
    """The allowlist above is only as good as the check that fills it. A
    handler that renamed its dict, read it through `(body or {})`, or
    handed it whole to a helper used to pass for converted."""

    def test_every_raw_shape_is_seen(self):
        for fn in (_shape_renamed, _shape_handed_to_a_helper,
                   _shape_optional_body, _shape_through_a_dependency,
                   _shape_past_the_typed_reader):
            with self.subTest(fn.__name__):
                self.assertNotEqual(_raw_reads(fn), [])

    def test_a_typed_door_and_a_mere_local_are_not_raw(self):
        for fn in (_shape_typed, _shape_typed_optional,
                   _shape_a_local_named_body):
            with self.subTest(fn.__name__):
                self.assertEqual(_raw_reads(fn), [])


if __name__ == "__main__":
    unittest.main()


class ShippedClientBodiesAreAccepted(unittest.TestCase):
    """The bodies the released web and mobile clients really send, copied
    from their call sites (webapp/src/api/client.ts and mobile/src/lib/api.ts
    and the screens that build each body), older shapes included: an
    installed mobile build is older than the code here and a server deploy
    cannot change what it sends. A typed door stricter than those bodies
    would break bill saving on a phone nobody can update from here, so
    every one of them must still be answered with a 200."""

    @classmethod
    def setUpClass(cls):
        import datetime as dt
        import os

        from oikonome.engine import bills as rd
        from oikonome.sync import base

        from .util import TODAY
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"billshapes-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # three monthly charges the finder offers as bills
            feed = []
            for name, merchant, amt in (
                    ("LUMEN STREAM 800-555", "Lumen Stream", 11.49),
                    ("PARCEL BOX CO", "Parcel Box", 23.75),
                    ("QUILL NOTES APP", "Quill Notes", 6.25)):
                feed += [base.Transaction(
                    id=f"shape-{merchant}-{i}", account_id="card",
                    date=dt.date(TODAY.year, TODAY.month - 5 + i, 8),
                    amount=amt, name=name, merchant_name=merchant)
                    for i in range(5)]
            base.upsert_transactions(conn, feed)
            rd.run(conn, TODAY)
            cls.pids = {r["payee"]: r["id"] for r in conn.execute(
                "SELECT id, payee FROM bill_proposals WHERE status='pending' "
                "AND kind='add'").fetchall()}
            add_bill(conn, "Shape Gym", 40.0, next_due="2026-08-05",
                     source="manual")
            add_bill(conn, "Shape Pool", 120.0, bill_type="envelope",
                     frequency=None, category="HOBBIES", match_category=True)
            add_bill(conn, "Shape Payroll", 2100.0, income=True,
                     frequency="WEEKLY", interval=2, next_due="2026-07-24")
            add_bill(conn, "Shape Old", 15.0, next_due="2026-08-01")
            rd.archive_bill(conn, "Shape Old")
            for payee in ("Shape Toggle", "Shape Purge", "Shape Hint"):
                add_bill(conn, payee, 9.0, next_due="2026-08-02")
        finally:
            conn.close()

    def _ok(self, path, body, query=""):
        r = self.client.post("/api" + path + query, json=body)
        self.assertEqual(r.status_code, 200, (path, body, r.text))
        return r.json()

    def _detail(self, payee):
        return self.client.get("/api/bills/bill",
                               params={"payee": payee}).json()

    def test_bill_save_takes_every_shipped_shape(self):
        gym = self._detail("Shape Gym")
        pool = self._detail("Shape Pool")
        shapes = [
            # web, the triage queue's Accept (amount a number from the fit)
            {"payee": "Shape Gym", "orig_payee": "Shape Gym", "amount": 41.5},
            # mobile Accept; an older build also sent the row's direction
            {"payee": "Shape Gym", "amount": 41.5, "orig_payee": "Shape Gym"},
            {"payee": "Shape Gym", "amount": 41.5, "orig_payee": "Shape Gym",
             "income": False},
            # web add form: amount from a text input, next_due "" when blank
            {"payee": "Shape Web Add", "amount": "42.00",
             "cadence": "MONTHLY:1", "next_due": "", "income": False,
             "merchant": "Shape Web Add", "show_today": False},
            {"payee": "Shape Web Pool", "amount": "60", "cadence": "ENVELOPE:1",
             "next_due": "", "income": False, "merchant": "Shape Web Pool",
             "show_today": True, "category": "HOBBIES",
             "match_category": True},
            # the add over an archived name, with the explicit restore
            {"payee": "Shape Old", "amount": "15", "cadence": "MONTHLY:1",
             "next_due": "", "income": False, "merchant": "Shape Old",
             "show_today": False, "restore": True},
            # web edit row: cap from a text input ("" clears, digits set),
            # fees with amount Number(text) and window_days Number(text) —
            # which is 3.5 when "3.5" was typed — merchants echoed back
            {"payee": "Shape Gym", "amount": "40.00", "income": gym["income"],
             "cadence": gym["cadence"], "orig_payee": "Shape Gym", "cap": "",
             "show_today": True,
             "companions": [{"tokens": "late fee", "amount": 5,
                             "window_days": 3}],
             "txn_category": "", "merchants": gym["merchants"]},
            {"payee": "Shape Gym", "amount": "40.00", "income": False,
             "cadence": "MONTHLY:1", "next_due": "2026-08-07",
             "orig_payee": "Shape Gym", "cap": "2", "show_today": False,
             "companions": [{"tokens": "late fee", "amount": 4.5,
                             "window_days": 3.5}]},
            # web edit of an envelope: category "" sends null
            {"payee": "Shape Pool", "amount": "120.00",
             "income": pool["income"], "cadence": "ENVELOPE:1",
             "orig_payee": "Shape Pool", "show_today": False,
             "match_category": False, "category": None},
            # web bill-history add: income from the history's inflow flag
            {"payee": "Shape Hist Add", "merchant": "Shape Hist Add",
             "amount": "9.99", "cadence": "MONTHLY:1", "next_due": "",
             "income": True},
            # web wizard add: amount a parsed number
            {"payee": "Shape Wiz Add", "amount": 15, "cadence": "WEEKLY:2",
             "next_due": "2026-08-01", "income": False},
            # mobile bill editor: cap null or the text, category and its
            # flag echoed from the bill on an occurrence save, fee amount
            # parsed, window_days Number(text)
            {"payee": "Shape Gym", "orig_payee": "Shape Gym",
             "amount": "40.00", "cadence": "MONTHLY:1", "income": False,
             "cap": None, "category": gym["category"],
             "match_category": gym["match_category"], "show_today": True,
             "companions": [{"tokens": "late fee", "amount": 5,
                             "window_days": 3}]},
            {"payee": "Shape Gym", "orig_payee": "Shape Gym",
             "amount": "40.00", "cadence": "MONTHLY:1", "income": False,
             "cap": "3", "category": None, "match_category": False,
             "show_today": False},
            # an older mobile editor: cap on every save, show_today even on
            # income, category always echoed
            {"payee": "Shape Payroll", "orig_payee": "Shape Payroll",
             "amount": "2100.00", "cadence": "WEEKLY:2", "income": True,
             "cap": None, "category": None, "show_today": False},
            {"payee": "Shape Pool", "orig_payee": "Shape Pool",
             "amount": "120.00", "cadence": "ENVELOPE:1", "income": False,
             "cap": "", "match_category": True, "category": "HOBBIES",
             "show_today": False},
            # mobile add (bills tab): next_due undefined is left out
            {"payee": "Shape Mob Add", "amount": "12", "cadence": "MONTHLY:1",
             "income": False, "show_today": False},
            {"payee": "Shape Mob Pool", "amount": "30", "cadence": "ENVELOPE:1",
             "next_due": "2026-08-01", "income": False, "show_today": False,
             "category": "HOBBIES", "match_category": True},
            # mobile bill-history add and wizard add
            {"payee": "Shape Mob Hist", "amount": "8.25",
             "cadence": "MONTHLY:1", "income": False},
            {"payee": "Shape Mob Wiz", "amount": "7.50", "cadence": "MONTHLY:1",
             "income": True, "next_due": "2026-08-09"},
        ]
        for body in shapes:
            self._ok("/bills/save", body)

    def test_bill_proposal_takes_every_shipped_shape(self):
        lumen, parcel, quill = (self.pids[k] for k in
                                ("Lumen Stream", "Parcel Box", "Quill Notes"))
        # web and mobile adjust: amount a rounded number, due_day a number
        self._ok("/bills/proposal", {
            "pid": lumen, "action": "approve",
            "adjust": {"name": "Lumen", "amount": 12.5,
                       "cadence": "MONTHLY:1", "due_day": 15}})
        # no adjust (both clients leave the key out when nothing changed)
        self._ok("/bills/proposal", {"pid": parcel, "action": "approve"})
        self._ok("/bills/proposal", {"pid": quill, "action": "reject"})

    def test_a_blank_adjust_field_is_one_left_alone(self):
        # an offer's adjust has always read a blank field as one the
        # person did not change: "" is not a due day, but it is not a
        # refusal either
        from oikonome.web import api
        from oikonome.web.fields import Fields
        self.assertEqual(api._proposal_adjust(Fields(
            {"name": "", "amount": "", "cadence": "", "due_day": ""})), {})
        self.assertEqual(api._proposal_adjust(Fields(
            {"due_day": None, "amount": None})), {})

    def test_bill_delete_takes_every_shipped_shape(self):
        self._ok("/bills/delete", {"payee": "Shape Purge", "mode": "archive"})
        self._ok("/bills/delete", {"payee": "Shape Purge", "mode": "purge"})

    def test_bill_restore_takes_the_shipped_shape(self):
        self._ok("/bills/delete", {"payee": "Shape Toggle",
                                   "mode": "archive"})
        self._ok("/bills/restore", {"payee": "Shape Toggle"})

    def test_bill_toggle_takes_every_shipped_shape(self):
        self._ok("/bills/toggle", {"payee": "Shape Toggle", "disabled": True})
        self._ok("/bills/toggle", {"payee": "Shape Toggle", "disabled": False})
        # an older build sent no target state: a bare flip
        self._ok("/bills/toggle", {"payee": "Shape Toggle"})

    def test_bill_hint_takes_every_shipped_shape(self):
        self._ok("/bills/hint", {"payee": "Shape Hint", "action": "dismiss",
                                 "target": "fit"})
        self._ok("/bills/hint", {"payee": "Shape Hint", "action": "restore",
                                 "target": "health"})
        # an older build sent no target
        self._ok("/bills/hint", {"payee": "Shape Hint", "action": "dismiss"})

    def test_merchant_category_preview_takes_every_shipped_shape(self):
        self._ok("/bills/merchant-category/preview",
                 {"payee": "Lumen Stream", "category": "ENTERTAINMENT"})
        self._ok("/bills/merchant-category/preview",
                 {"payee": "Lumen Stream", "category": "ENTERTAINMENT"},
                 query="?canon_only=1")

    def test_merchant_category_write_and_undo_take_the_shipped_shapes(self):
        out = self._ok("/bills/merchant-category",
                       {"payee": "Parcel Box", "category": "SHOPPING"})
        # the undo is the write's own reply, echoed back whole
        self._ok("/bills/merchant-category/undo", {"undo": out["undo"]})


class UndoLogTargetIsBounded(unittest.TestCase):
    """An older client's inline undo snapshot names its merchant, and the
    undo door writes that name into the household activity log, which is
    kept for years and rides the export. The name came from the client, so
    it is held to the length a merchant name may have at every other door,
    not stored at whatever size was posted."""

    def test_a_long_merchant_in_an_inline_snapshot_is_cut_in_the_log(self):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import api
        client = TestClient(appmod.app)
        client.post("/api/signup", data={
            "email": f"undolog-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = client.get("/api/me").json()["tenant_id"]
        long_name = "Contoso Lumber " + "x" * 5000
        r = client.post("/api/bills/merchant-category/undo", json={
            "undo": {"canons": [{"canon": "Contoso Lumber"}],
                     "affected": [], "merchant": long_name}})
        self.assertEqual(r.status_code, 200, r.text)
        conn = tenancy.tenant_connect(tid)
        try:
            row = conn.execute(
                "SELECT target FROM activity_log WHERE kind='rule' "
                "AND action='undone' ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertLessEqual(len(row["target"]), api.MERCHANT_NAME_MAX)
        self.assertTrue(long_name.startswith(row["target"]))
