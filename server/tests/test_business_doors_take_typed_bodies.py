"""The business doors read their JSON bodies through typed accessors.

The doors that keep a business's books — the entity itself, its members,
capital movements, fronted costs, Schedule C classes, the compliance
calendar, the mileage log, 1099 payees, the combined view and the account
and transaction assignments — used to read a plain dict. Each field's type
was then its own decision, and the ones nobody made let a wrong type through
as a coerced value: `"is_manager": "false"` stored a manager, `"amount": true`
recorded a capital movement of 1.00, `"on": "false"` combined the books.
They now read through `web.fields.Fields`, which answers a wrong type with
a 400 naming the field.

Pinned here:

* the matrix: every key each door reads, sent as an int, a list, an object,
  a 10 kB string and a NUL character inside an otherwise valid body, is
  answered below 500;
* wrongly typed flags and amounts are refused, never coerced;
* every body the released web and mobile clients send (copied from their
  call sites, older shapes included) is still accepted: a phone build in a
  store cannot be changed by a server deploy;
* none of these doors reads its body raw.
"""

import ast
import inspect
import os
import re
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, all_routes, seed_accounts, write_config

BAD_VALUES = (7, [1], {"a": 1}, "x" * 10240, "\u0000")

_KEY_PATTERN = (r'body\.(?:get|text|ident|flag|money|int_in|date|one_of'
                r'|list_of|present|sub|opaque|opaque_rows|json_type'
                r'|money_given|text_or_int|text_or_list|number)\(\s*"([A-Za-z_]+)"')

# the handlers that read their body through the typed reader, by route
DOORS = {
    "POST:/api/business/entities": "entities_create",
    "PATCH:/api/business/entities/{entity_id}": "entities_update",
    "POST:/api/business/entities/{entity_id}/status": "entities_status",
    "POST:/api/business/entities/{entity_id}/delete-forever":
        "entities_delete_forever",
    "POST:/api/business/entities/{entity_id}/members": "entities_add_member",
    "PATCH:/api/business/entities/{entity_id}/members/{member_id}":
        "entities_update_member",
    "POST:/api/business/entities/{entity_id}/equity": "equity_record",
    "POST:/api/business/entities/{entity_id}/reimburse": "equity_reimburse",
    "POST:/api/business/entities/{entity_id}/transactions/{txn_id:path}/class":
        "business_classify",
    "POST:/api/business/entities/{entity_id}/compliance": "compliance_add",
    "POST:/api/business/entities/{entity_id}/mileage": "se_mileage_add",
    "POST:/api/business/entities/{entity_id}/vendors": "se_vendor_mark",
    "POST:/api/business/combine": "business_combine",
    "POST:/api/accounts/{account_id}/entity": "account_assign_entity",
    "POST:/api/transactions/{txn_id:path}/entity": "txn_assign_entity",
}


def _handlers():
    from .util import api_function
    return {name: api_function(name) for name in DOORS.values()}


def _keys(fn) -> list[str]:
    return list(dict.fromkeys(re.findall(_KEY_PATTERN, inspect.getsource(fn))))


def _raw_reads(fn) -> list[str]:
    """`body.get(`, `body.items(`, `body[...]`, `... in body` and the
    dict-era `_body_text(body, …)` inside a handler."""
    out = []
    for node in ast.walk(ast.parse(inspect.getsource(fn))):
        if (isinstance(node, ast.Attribute) and node.attr in ("get", "items")
                and isinstance(node.value, ast.Name)
                and node.value.id == "body"):
            out.append(f"body.{node.attr} (line {node.lineno})")
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
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "_body_text"):
            out.append(f"_body_text (line {node.lineno})")
    return out


def _signup(client, prefix):
    client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    tid = client.get("/api/me").json()["tenant_id"]
    conn = tenancy.tenant_connect(tid)
    try:
        seed_accounts(conn)
        write_config(conn)
    finally:
        conn.close()
    return tid


def _txn(tid, amount=42.0, name="PAPER SUPPLY CO"):
    conn = tenancy.tenant_connect(tid)
    try:
        return add_txn(conn, TODAY, amount, name, account="card",
                       txn_id=f"typed-{uuid.uuid4().hex[:10]}")
    finally:
        conn.close()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.app = appmod.app

    def _entity(self, name=None):
        r = self.client.post("/api/business/entities", json={
            "name": name or f"Typed {uuid.uuid4().hex[:6]} LLC",
            "structure": "multi_member_llc"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def _member(self, eid, name="Avery Lindqvist"):
        r = self.client.post(f"/api/business/entities/{eid}/members",
                             json={"member_name": name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]


class BusinessDoorsRefuseWrongTypes(_Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = TestClient(cls.app, raise_server_exceptions=False)
        cls.tid = _signup(cls.client, "biztypes")

    def _cases(self):
        """(method, url, valid base body) per door."""
        eid = self._entity()
        mid = self._member(eid)
        day = TODAY.isoformat()
        e = f"/api/business/entities/{eid}"
        txn = _txn(self.tid)
        self.client.post(f"/api/transactions/{txn}/entity",
                         json={"entity_id": eid})
        return {
            "POST:/api/business/entities": (
                "post", "/api/business/entities",
                {"name": "Matrix Works LLC", "structure": "sole_prop"}),
            "PATCH:/api/business/entities/{entity_id}": ("patch", e, {}),
            "POST:/api/business/entities/{entity_id}/status": (
                "post", f"{e}/status", {"status": "active"}),
            "POST:/api/business/entities/{entity_id}/delete-forever": (
                "post", f"{e}/delete-forever", {"name": "not its name"}),
            "POST:/api/business/entities/{entity_id}/members": (
                "post", f"{e}/members", {"member_name": "Rowan Tesh"}),
            "PATCH:/api/business/entities/{entity_id}/members/{member_id}": (
                "patch", f"{e}/members/{mid}", {}),
            "POST:/api/business/entities/{entity_id}/equity": (
                "post", f"{e}/equity",
                {"kind": "contribution", "amount": 100, "date": day}),
            "POST:/api/business/entities/{entity_id}/reimburse": (
                "post", f"{e}/reimburse", {"txn_id": txn, "mode": "reimburse"}),
            "POST:/api/business/entities/{entity_id}/transactions/"
            "{txn_id:path}/class": (
                "post", f"{e}/transactions/{txn}/class",
                {"bucket": "operating"}),
            "POST:/api/business/entities/{entity_id}/compliance": (
                "post", f"{e}/compliance",
                {"title": "Annual report", "due_date": day}),
            "POST:/api/business/entities/{entity_id}/mileage": (
                "post", f"{e}/mileage", {"date": day, "miles": 12}),
            "POST:/api/business/entities/{entity_id}/vendors": (
                "post", f"{e}/vendors",
                {"merchant": "Matrix Plumbing", "reportable": True}),
            "POST:/api/business/combine": (
                "post", "/api/business/combine", {"on": False}),
            "POST:/api/accounts/{account_id}/entity": (
                "post", "/api/accounts/chk/entity", {"entity_id": None}),
            "POST:/api/transactions/{txn_id:path}/entity": (
                "post", f"/api/transactions/{txn}/entity",
                {"entity_id": eid}),
        }

    def test_every_typed_door_is_a_route_with_a_matrix_entry(self):
        from .util import is_api_endpoint
        routes = {}
        for r in all_routes(self.app):
            if not is_api_endpoint(r):
                continue
            for m in r.methods - {"HEAD", "OPTIONS"}:
                routes[f"{m}:{r.path}"] = r.endpoint.__name__
        for door, name in DOORS.items():
            self.assertEqual(routes.get(door), name, door)
        self.assertEqual(set(self._cases()), set(DOORS))

    def test_every_key_a_business_door_reads_never_answers_5xx(self):
        handlers = _handlers()
        cases = self._cases()
        failures = []
        for door, name in DOORS.items():
            method, url, base = cases[door]
            keys = _keys(handlers[name])
            self.assertTrue(keys, f"{name} reads no keys through Fields")
            for key in keys:
                for bad in BAD_VALUES:
                    body = dict(base)
                    body[key] = bad
                    r = getattr(self.client, method)(url, json=body)
                    if r.status_code >= 500:
                        failures.append(f"{door} {key}={str(bad)[:12]!r} "
                                        f"-> {r.status_code}")
        self.assertEqual(failures, [])

    def test_wrongly_typed_flags_and_amounts_are_refused_not_coerced(self):
        eid = self._entity()
        mid = self._member(eid)
        e = f"/api/business/entities/{eid}"
        day = TODAY.isoformat()
        cases = [
            ("post", f"{e}/members",
             {"member_name": "Sky Arden", "is_manager": "false"}),
            ("post", f"{e}/members",
             {"member_name": "Sky Arden", "is_household": 1}),
            ("patch", f"{e}/members/{mid}", {"is_manager": "true"}),
            ("post", f"{e}/members",
             {"member_name": "Sky Arden", "ownership_pct": True}),
            ("post", f"{e}/equity",
             {"kind": "contribution", "amount": True, "date": day}),
            ("post", f"{e}/mileage", {"date": day, "miles": True}),
            ("post", f"{e}/compliance",
             {"title": "Fee", "due_date": day, "fee": True}),
            ("post", f"{e}/vendors",
             {"merchant": "Matrix Plumbing", "reportable": "false"}),
            ("post", f"{e}/vendors",
             {"merchant": "Matrix Plumbing", "tin_last4": True}),
            ("post", "/api/business/combine", {"on": "false"}),
            ("patch", e, {"home_office_sqft": True}),
            ("patch", e, {"income_tax_rate": False}),
            ("post", "/api/accounts/chk/entity", {"entity_id": {}}),
        ]
        for method, url, body in cases:
            r = getattr(self.client, method)(url, json=body)
            self.assertEqual(r.status_code, 400, f"{url} {body}: {r.text}")
        # nothing was coerced into the books
        ent = self.client.get(e).json()
        self.assertEqual([m["member_name"] for m in ent["members"]],
                         ["Avery Lindqvist"])
        self.assertFalse(ent["members"][0]["is_manager"])
        self.assertEqual(self.client.get(f"{e}/equity").json()["movements"],
                         [])
        self.assertFalse(self.client.get("/api/business/summary")
                         .json()["combined"])
        self.assertIsNone(ent["home_office_sqft"])
        # the TIN refusal never echoes what was sent
        r = self.client.post(f"{e}/vendors", json={
            "merchant": "Matrix Plumbing", "tin_last4": ["1111"]})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("1111", r.text)

    def test_no_business_door_reads_its_body_raw(self):
        from oikonome.web.fields import Fields
        offenders = {}
        for name, fn in _handlers().items():
            reads = _raw_reads(fn)
            if reads:
                offenders[name] = reads
            param = inspect.signature(fn, eval_str=True).parameters["body"]
            if param.annotation is not Fields:
                offenders.setdefault(name, []).append(
                    f"body is {param.annotation!r}, not Fields")
        self.assertEqual(offenders, {})

    def test_the_entity_editor_reads_every_editable_key(self):
        from oikonome.engine import entities
        from oikonome.web import api
        self.assertLessEqual(entities._EDITABLE | {"ein"},
                             set(_keys(api.entities_update)))


class ShippedBusinessClientBodiesAreAccepted(_Base):
    """The bodies the released web and mobile clients send to the business
    doors, copied from webapp/src (pages/Business.tsx, BusinessWizard.tsx,
    TxnTable.tsx) and mobile/src (app/business.tsx, business-wizard.tsx,
    txn.tsx), older shapes included. Every one must still be a 200."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = TestClient(cls.app)
        cls.tid = _signup(cls.client, "bizshapes")

    def _ok(self, method, url, body):
        r = getattr(self.client, method)(url, json=body)
        self.assertEqual(r.status_code, 200, (url, body, r.text))
        return r.json()

    def test_entity_create_takes_every_shipped_shape(self):
        for body in (
                # web: blank optional fields are left out
                {"name": "Shape Studio LLC", "structure": "single_member_llc"},
                {"name": "Shape Works LLC", "structure": "multi_member_llc",
                 "state": "CA", "ein": "98-7654321",
                 "formation_date": "2024-03-01",
                 "business_start_date": "2024-04-15"},
                # mobile wizard and form: the same keys, trimmed
                {"name": "Shape Kiln", "structure": "sole_prop",
                 "state": "OR", "formation_date": "2023-01-09"},
                {"name": "Shape Loom Inc", "structure": "s_corp",
                 "ein": "987654321", "business_start_date": "2022-06-01"}):
            ent = self._ok("post", "/api/business/entities", body)
            self.assertEqual(ent["name"], body["name"])
            self.assertNotIn("ein", ent)

    def test_entity_update_takes_every_shipped_shape(self):
        eid = self._entity()
        e = f"/api/business/entities/{eid}"
        self._ok("post", "/api/accounts/chk/entity", {"entity_id": eid})
        name = f"Shape Edit {uuid.uuid4().hex[:6]} LLC"
        for body in (
                # web and mobile edit forms: blanks go as null
                {"name": name, "structure": "multi_member_llc",
                 "state": None, "formation_date": None,
                 "business_start_date": None},
                {"name": name, "structure": "multi_member_llc",
                 "state": "WA", "formation_date": "2024-02-01",
                 "business_start_date": "2024-02-15",
                 "ein": "12-3456789"},
                # the tax section: numbers from Number(text), or null
                {"income_tax_rate": 22.5, "home_office_sqft": 150},
                {"income_tax_rate": 24},
                {"home_office_sqft": 180.0},
                {"income_tax_rate": None, "home_office_sqft": None},
                # the tax reserve picker: an id, its empty option, or null
                {"tax_reserve_account_id": "chk"},
                {"tax_reserve_account_id": ""},
                {"tax_reserve_account_id": None}):
            self._ok("patch", e, body)
        ent = self.client.get(e).json()
        self.assertEqual(ent["ein_last4"], "6789")
        self.assertEqual(ent["formation_date"], "2024-02-01")
        self.assertIsNone(ent["tax_reserve_account_id"])
        # a fractional size typed into the box is cut to its whole part,
        # as the engine always did
        ent = self._ok("patch", e, {"home_office_sqft": 150.7})
        self.assertEqual(ent["home_office_sqft"], 150)
        ent = self._ok("patch", e, {"income_tax_rate": 22.5})
        self.assertEqual(ent["income_tax_rate"], 22.5)
        # an edit that leaves the EIN out keeps the one on file
        ent = self._ok("patch", e, {"name": name, "structure": "sole_prop"})
        self.assertEqual(ent["ein_last4"], "6789")

    def test_status_and_delete_forever_take_the_shipped_shapes(self):
        eid = self._entity()
        e = f"/api/business/entities/{eid}"
        self.assertEqual(self._ok("post", f"{e}/status",
                                  {"status": "archived"})["status"],
                         "archived")
        self.assertEqual(self._ok("post", f"{e}/status",
                                  {"status": "active"})["status"], "active")
        name = self.client.get(e).json()["name"]
        self.assertTrue(self._ok("post", f"{e}/delete-forever",
                                 {"name": name})["ok"])

    def test_members_take_every_shipped_shape(self):
        eid = self._entity()
        e = f"/api/business/entities/{eid}"
        # web: ownership_pct undefined when blank; mobile: left out
        a = self._ok("post", f"{e}/members", {
            "member_name": "Quinn Marlow", "ownership_pct": 60,
            "is_manager": True, "is_household": True})
        b = self._ok("post", f"{e}/members", {
            "member_name": "Jules Ferrand", "is_manager": False,
            "is_household": False})
        c = self._ok("post", f"{e}/members", {
            "member_name": "Ira Solberg", "ownership_pct": 12.5,
            "is_manager": False, "is_household": False})
        self.assertEqual(a["ownership_pct"], 60.0)
        self.assertIsNone(b["ownership_pct"])
        self.assertEqual(c["ownership_pct"], 12.5)
        # the editors send every field; null clears the share
        m = self._ok("patch", f"{e}/members/{b['id']}", {
            "member_name": "Jules Ferrand", "ownership_pct": 27.5,
            "is_manager": True, "is_household": False})
        self.assertEqual((m["ownership_pct"], m["is_manager"]), (27.5, True))
        m = self._ok("patch", f"{e}/members/{b['id']}", {
            "member_name": "Jules Ferrand", "ownership_pct": None,
            "is_manager": True, "is_household": False})
        self.assertIsNone(m["ownership_pct"])
        # an older editor sent no household flag
        m = self._ok("patch", f"{e}/members/{a['id']}", {
            "member_name": "Quinn Marlow", "ownership_pct": 60,
            "is_manager": True})
        self.assertTrue(m["is_household"])

    def test_equity_and_fronted_costs_take_every_shipped_shape(self):
        eid = self._entity()
        e = f"/api/business/entities/{eid}"
        day = TODAY.isoformat()
        for body in (
                # web: Number(text), form and note when typed
                {"kind": "contribution", "amount": 1250.5, "date": day,
                 "form": "check", "note": "opening capital"},
                {"kind": "draw", "amount": 300, "date": day},
                # mobile: parseMoneyDraft, form when typed
                {"kind": "distribution", "amount": 75.25, "date": day,
                 "form": "transfer"}):
            mv = self._ok("post", f"{e}/equity", body)
            self.assertEqual(mv["amount"], round(float(body["amount"]), 2))
        for mode in ("contribute", "reimburse"):
            self._ok("post", f"{e}/reimburse",
                     {"txn_id": _txn(self.tid), "mode": mode})

    def test_classify_takes_every_shipped_shape(self):
        eid = self._entity()
        e = f"/api/business/entities/{eid}"
        txn = _txn(self.tid)
        self._ok("post", f"/api/transactions/{txn}/entity",
                 {"entity_id": eid})
        url = f"{e}/transactions/{txn}/class"
        for body in ({"bucket": "operating"},
                     {"bucket": "operating", "sched_c_line": "Advertising"},
                     {"bucket": "startup_195", "sched_c_line": None},
                     {"bucket": "organizational"}):
            self._ok("post", url, body)

    def test_compliance_and_mileage_take_every_shipped_shape(self):
        eid = self._entity()
        e = f"/api/business/entities/{eid}"
        day = TODAY.isoformat()
        for body in (
                {"title": "Annual report", "due_date": day,
                 "recurrence": "yearly"},
                {"title": "Franchise tax", "due_date": day,
                 "recurrence": "once"},
                # an older web form sent no recurrence
                {"title": "Registered agent renewal", "due_date": day},
                # the client type allows a fee and a link
                {"title": "Filing fee", "due_date": day, "recurrence": "yearly",
                 "fee": 50, "url": "https://example.org/filing"}):
            self._ok("post", f"{e}/compliance", body)
        obl = self.client.get(f"{e}/compliance").json()["obligations"]
        self.assertEqual({o["recurrence"] for o in obl
                          if o["title"] == "Registered agent renewal"},
                         {"yearly"})
        for body in ({"date": day, "miles": 12.5, "purpose": "client visit"},
                     {"date": day, "miles": 30}):
            self._ok("post", f"{e}/mileage", body)

    def test_vendor_marks_take_every_shipped_shape(self):
        eid = self._entity()
        e = f"/api/business/entities/{eid}"
        merchant = "Shape Electric"
        self._ok("post", f"{e}/vendors",
                 {"merchant": merchant, "reportable": True,
                  "tin_last4": "4321"})

        def tin():
            from oikonome.engine import selfemploy
            conn = tenancy.tenant_connect(self.tid)
            try:
                return selfemploy.vendor_tin(conn, eid, merchant)
            finally:
                conn.close()

        # a toggle leaves the TIN out and keeps the one on file
        self._ok("post", f"{e}/vendors",
                 {"merchant": merchant, "reportable": False})
        self.assertEqual(tin(), "4321")
        # the TIN box sends a new one, or null / "" to clear it
        self._ok("post", f"{e}/vendors", {"merchant": merchant,
                                          "reportable": True,
                                          "tin_last4": "5678"})
        self.assertEqual(tin(), "5678")
        self._ok("post", f"{e}/vendors", {"merchant": merchant,
                                          "reportable": True,
                                          "tin_last4": None})
        self.assertIsNone(tin())
        self._ok("post", f"{e}/vendors", {"merchant": merchant,
                                          "reportable": True,
                                          "tin_last4": ""})
        self.assertIsNone(tin())
        # a TIN sent as a bare number keeps its digits, as it always did
        self._ok("post", f"{e}/vendors", {"merchant": merchant,
                                          "reportable": True,
                                          "tin_last4": 2468})
        self.assertEqual(tin(), "2468")

    def test_combine_and_assignments_take_every_shipped_shape(self):
        eid = self._entity()
        self.assertTrue(self._ok("post", "/api/business/combine",
                                 {"on": True})["combined"])
        self.assertFalse(self._ok("post", "/api/business/combine",
                                  {"on": False})["combined"])
        self.assertEqual(self._ok("post", "/api/accounts/card/entity",
                                  {"entity_id": eid})["entity_id"], eid)
        self.assertIsNone(self._ok("post", "/api/accounts/card/entity",
                                   {"entity_id": None})["entity_id"])
        txn = _txn(self.tid)
        self.assertEqual(self._ok("post", f"/api/transactions/{txn}/entity",
                                  {"entity_id": eid})["entity_id"], eid)
        self.assertIsNone(self._ok("post", f"/api/transactions/{txn}/entity",
                                   {"entity_id": None})["entity_id"])


if __name__ == "__main__":
    unittest.main()
