"""Hostile request bodies and text for every documented door
(specs/invariants-and-fuzzing.md, body fuzzing).

The doors are the `/api` routes whose handler documents a body
(`@documented(body=...)`). For each one this builds a plausible body from
its model — the field names hinted to values the fixture household holds,
so a door reads past its first check — and then sends it with one hostile
change at a time:

* VALUE classes, each once per door, at a field picked by a hash of the
  door and the class (the same field every run); in long mode
  (`OIKONOME_PROPERTY=long`) at up to `LONG_FIELDS` fields drawn from the
  run's printed seed (`props.run_seed`), which each failure names;
* WHOLE-BODY classes: a top-level list, an empty object, an extra key,
  bytes that are not UTF-8;
* TEXT classes, at EVERY text field of every door: one 200,000-letter
  token, thousands of short words, and regex / LIKE metacharacters — the
  shapes that reach a pattern the database compiles.

`text_routes()` and `run_text()` send the same text shapes, sized for a URL
(URL_TEXT_CLASSES), to the path and query text of every GET door.

The property is F1: the answer is never a 5xx. The one 503 allowed is a
door saying what it needs (ALLOWED_503); "server busy" is not allowed —
nothing else runs beside the fuzz, so a busy answer means the request
itself was too costly.
"""

from __future__ import annotations

import json
import os
import random
import re
import uuid
import zlib

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web.schemas import body_of

from . import props
from .api_conformance import _flat
from .util import _ensure_db, add_bill, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
PAYEE = "Corner Grocer"
CATEGORY = "FOOD_AND_DRINK"
DAY = "2026-07-01"
NO_SUCH = "00000000-0000-0000-0000-000000000001"

# Replies that name what a door needs, not a fault.
ALLOWED_503 = ("the assistant needs a local LLM configured",)

# Doors that can end the session or the household they run in. They run
# last, and the session is checked (and renewed) after each request.
RISKY = re.compile(r"logout|password|delete|erase|leave|revoke|session"
                   r"|passkey|factor|totp|recovery|reset|email|elevate"
                   r"|step-up|step_up|unlock|transfer-owner|owner")

DEEP = "__DEEP__"
BIGINT = "__BIGINT__"
_RAW = {
    # past Python's own nesting and integer-text limits: written as text
    DEEP: '{"a":' * 5000 + "1" + "}" * 5000,
    BIGINT: "1" + "0" * 4400,
}

VALUE_CLASSES = (
    ("int", 7), ("nan", float("nan")), ("inf", float("-inf")),
    ("int-past-float", 10 ** 400), ("int-past-parse", BIGINT),
    ("bool", True), ("null", None), ("object", {"a": {"b": [1]}}),
    ("list", [1, "x", None]), ("deep", DEEP), ("nul", "a\u0000b"),
    ("surrogate", "\ud800"), ("bidi", "‮́\U0001f4a5"),
    ("formula", "=1+1"), ("long", "x" * 200_000),
)

TEXT_CLASSES = (
    ("one-long-token", "abcd" * 50_000),
    ("many-words", " ".join(f"word{chr(97 + i % 26)}abc" for i in range(6000))),
    ("pattern-meta", "(.*){999}[%_\\'\"|^$+?*]" * 50),
)

# The same shapes, sized for a URL: the server reads a request line of
# about 16 kB at most, so text past that never reaches a door.
URL_TEXT_CLASSES = (
    ("one-long-token", "abcd" * 3000),
    ("many-words", " ".join(f"word{chr(97 + i % 26)}" for i in range(1800))),
    ("pattern-meta", "(.*){999}[%_\\'\"|^$+?*]" * 20),
    ("nul", "a\u0000b"),
)

WHOLE_BODY_CLASSES = ("top-level-list", "empty-object", "extra-key",
                      "not-utf8")


def doors(app) -> list[APIRoute]:
    """Every /api route documenting a body, in path order."""
    out = [r for r in _flat(app.routes) if isinstance(r, APIRoute)
           and r.path.startswith("/api/")
           and r.endpoint.__module__.startswith("oikonome.")
           and body_of(r.endpoint) is not None]
    return sorted(out, key=lambda r: (bool(RISKY.search(r.path)), r.path,
                                      sorted(r.methods)))


def json_body_routes(app) -> list[APIRoute]:
    """Every /api route of the product whose handler takes a JSON body
    (a form or an upload is not one)."""
    from fastapi import params
    out = []
    for r in _flat(app.routes):
        if not isinstance(r, APIRoute) or not r.path.startswith("/api/") \
                or not r.endpoint.__module__.startswith("oikonome."):
            continue
        bf = r.body_field
        if bf is None or isinstance(bf.field_info, (params.Form, params.File)):
            continue
        out.append(r)
    return out


def _all_dependants(dep):
    for d in dep.dependencies:
        yield d
        yield from _all_dependants(d)


def method_of(r: APIRoute) -> str:
    return sorted(r.methods - {"HEAD"})[0]


def pick(door: str, cls: str, n: int) -> int:
    return zlib.crc32(f"{door} {cls}".encode()) % n


# fields each value class reaches per door in long mode
LONG_FIELDS = 3


def fields_for(door: str, cls: str, targets: list[str]) -> list[str]:
    """The fields a value class is sent at: one, by hash, by default; in
    long mode up to LONG_FIELDS of them, drawn from the run's seed."""
    if not props.LONG:
        return [targets[pick(door, cls, len(targets))]]
    rng = random.Random(f"{props.run_seed()} {door} {cls}")
    return rng.sample(targets, min(LONG_FIELDS, len(targets)))


def signup(app, prefix: str = "fuzz") -> tuple[TestClient, str, str]:
    """A new household's elevated owner: (client, email, tenant id)."""
    from oikonome.web import security
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    security._limiter._hits.clear()
    client = TestClient(app, raise_server_exceptions=False)
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    client.post("/api/auth/elevate", json={"password": PW})
    return client, email, client.get("/api/me").json()["tenant_id"]


def join(app, owner: TestClient, role: str) -> tuple[TestClient, str]:
    """A person the owner's session invites with `role` joins, and opens
    an elevated session of their own: (client, email)."""
    from oikonome.web import security
    security._limiter._hits.clear()
    inv = owner.post("/api/invites", json={"label": role, "role": role,
                                           "password": PW})
    assert inv.status_code == 200, inv.text
    token = inv.json()["url"].rsplit("token=", 1)[1]
    client = TestClient(app, raise_server_exceptions=False)
    email = f"fuzz-{role}-{uuid.uuid4().hex[:8]}@example.dev"
    r = client.post("/api/invite/claim", json={"token": token, "email": email,
                                               "password": PW})
    assert r.status_code == 200, r.text
    assert client.get("/api/me").json()["role"] == role
    client.post("/api/auth/elevate", json={"password": PW})
    return client, email


class Household:
    """A signed-up household whose rows the hints name, and an elevated
    session in it: its owner, or (role "member" / "viewer") a person the
    owner invited with that role."""

    def __init__(self, app, role: str = "owner"):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        self.app = app
        self.role = role
        self.account = "chk"
        # ids a pass sends in place of this household's own, by path
        # parameter and by body field name (the other household's, in the
        # cross-household pass)
        self.path_ids: dict = {}
        self.body_ids: dict = {}
        self.renew()

    def renew(self):
        self.client, self.email, self.tid = signup(self.app)
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            self.txn = add_txn(conn, DAY, 42.5, PAYEE.upper(),
                               primary=CATEGORY, txn_id=f"fz{uuid.uuid4().hex[:8]}")
            add_txn(conn, DAY, -2500, "PAYROLL ACME", primary="INCOME",
                    account="chk")
            add_bill(conn, "Rent Example", 1200.0)
        finally:
            conn.close()
        if self.role != "owner":
            self.owner = self.client
            self.client, self.email = join(self.app, self.owner, self.role)

    def alive(self) -> bool:
        return self.client.get("/api/me").status_code == 200

    # ---- plausible values, by field name ----------------------------------
    def hint(self, name: str, schema: dict):
        n = name.lower()
        t = _type_of(schema)
        if n in self.body_ids and t in ("string", None):
            return self.body_ids[n]
        if "enum" in schema:
            return schema["enum"][0]
        if "const" in schema:
            return schema["const"]
        if t == "boolean":
            return False
        if t in ("integer", "number"):
            return 1
        if t == "array":
            item = schema.get("items") or {}
            if n in ("ids", "txn_ids", "transaction_ids"):
                return [self.body_ids.get("txn_id", self.txn)]
            return [self.hint(n.rstrip("s"), item)] if item.get("type") in (
                "string", "integer", "number") else []
        if t == "object":
            return {}
        if "password" in n:
            return PW
        if "email" in n:
            return self.email
        if n in ("payee", "merchant", "display", "name", "old", "from"):
            return PAYEE
        if "category" in n or n in ("new", "to"):
            return CATEGORY
        if "date" in n or n in ("since", "until", "start", "end"):
            return DAY
        if "amount" in n:
            return "12.50"
        if n in ("id", "txn_id", "transaction_id"):
            return self.txn
        if "account" in n:
            return self.account
        return "x"

    def path(self, r: APIRoute) -> str:
        def fill(m):
            n = m.group(1).split(":")[0].lower()
            if n in self.path_ids:
                return self.path_ids[n]
            if "txn" in n or "transaction" in n:
                return self.txn
            if "account" in n:
                return self.account
            if n in ("payee", "name", "display", "merchant"):
                return PAYEE
            return NO_SUCH
        return re.sub(r"\{([^}]+)\}", fill, r.path)


def _type_of(schema: dict) -> str | None:
    if "type" in schema:
        return schema["type"]
    for alt in schema.get("anyOf") or ():
        if alt.get("type") != "null":
            return _type_of(alt)
    return None


def base_body(hh: Household, model) -> tuple[dict, list[str], list[str]]:
    """(body, every field, the text fields) for a door's model."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    body, fields_, text = {}, [], []
    for name, prop in (schema.get("properties") or {}).items():
        if "$ref" in prop:
            prop = defs.get(prop["$ref"].rsplit("/", 1)[1], {})
        alts = [a for a in prop.get("anyOf") or () if a.get("type") != "null"]
        if alts and "$ref" in alts[0]:
            prop = defs.get(alts[0]["$ref"].rsplit("/", 1)[1], {})
        fields_.append(name)
        if _type_of(prop) == "string":
            text.append(name)
        body[name] = (hh.hint(name, prop) if _type_of(prop) != "object"
                      or "properties" not in prop else {})
    return body, fields_, text


def encode(body) -> bytes:
    raw = json.dumps(body, allow_nan=True)
    for key, text in _RAW.items():
        raw = raw.replace(f'"{key}"', text)
    return raw.encode("utf-8", "surrogatepass")


def cases(hh: Household, r: APIRoute, *, text_classes: bool = True):
    """(label, raw bytes, content type) for every hostile body of a door;
    `text_classes=False` leaves out the large text bodies at every text
    field (the value and whole-body classes still reach each door)."""
    body, fields_, text = base_body(hh, body_of(r.endpoint))
    if not text_classes:
        text = []
    door = f"{method_of(r)} {r.path}"
    targets = fields_ or ["unexpected"]
    for cls, value in VALUE_CLASSES:
        for f in fields_for(door, cls, targets):
            yield f"{f}={cls}", encode({**body, f: value}), \
                "application/json"
    for f in text:
        for cls, value in TEXT_CLASSES:
            yield f"{f}={cls}", encode({**body, f: value}), "application/json"
    yield "top-level-list", encode([body]), "application/json"
    yield "empty-object", b"{}", "application/json"
    yield "extra-key", encode({**body, "unexpected_key": {"x": 1}}), \
        "application/json"
    yield "not-utf8", b'{"x": "\xff\xfe"}', "application/json"


def text_routes(app) -> list[tuple[APIRoute, list[str], list[str]]]:
    """(route, its text path params, its text query params) for every GET
    /api door that takes any text."""
    out = []
    for r in _flat(app.routes):
        if not isinstance(r, APIRoute) or not r.path.startswith("/api/") \
                or "GET" not in r.methods \
                or not r.endpoint.__module__.startswith("oikonome."):
            continue
        deps = [r.dependant, *_all_dependants(r.dependant)]
        path = sorted({p.name for d in deps for p in d.path_params
                       if _is_text(p)})
        query = sorted({p.alias for d in deps for p in d.query_params
                        if _is_text(p)})
        if path or query:
            out.append((r, path, query))
    return sorted(out, key=lambda x: x[0].path)


def _is_text(p) -> bool:
    ann = str(p.field_info.annotation)
    return "str" in ann and "list" not in ann


def server_error(resp) -> bool:
    """F1: a 5xx, except a door's stated need."""
    if resp.status_code < 500:
        return False
    if resp.status_code == 503:
        try:
            return resp.json().get("detail") not in ALLOWED_503
        except ValueError:
            return True
    return True


def run(hh: Household, routes, checks=None, *,
        text_classes: bool = True) -> list[str]:
    """Every hostile body at every door of `routes`; the failures. With
    `checks` (tests/fuzz_checks.py), each reply is also held to its
    properties: no write on a refusal, nothing of the other household,
    a clean pool."""
    from oikonome.web import security
    failures = []
    for r in routes:
        m = method_of(r)
        for label, raw, ctype in cases(hh, r, text_classes=text_classes):
            security._limiter._hits.clear()
            resp = hh.client.request(m, hh.path(r), content=raw,
                                     headers={"content-type": ctype})
            where = f"{m} {r.path} [{label}]" + (
                f" ({props.replay()})" if props.LONG else "")
            if server_error(resp):
                failures.append(f"{where} -> "
                                f"{resp.status_code} {resp.text[:80]}")
            if checks is not None:
                failures += checks.after(where, resp)
            if RISKY.search(r.path) and not hh.alive():
                hh.renew()
                if checks is not None:
                    checks.settle()
    return failures


def text_cases(hh: Household, r: APIRoute, path: list[str], query: list[str]):
    """(label, url, query params) for every URL text class at every text
    path and query parameter of a GET door."""
    from urllib.parse import quote
    for name in path + query:
        for cls, value in URL_TEXT_CLASSES:
            if name in path:
                url = re.sub(r"\{%s(:[^}]*)?\}" % re.escape(name),
                             lambda _m: quote(value, safe=""), r.path)
                url = hh.path(type("R", (), {"path": url})())
                yield f"{name}={cls}", url, None
            else:
                yield f"{name}={cls}", hh.path(r), {name: value}


def run_text(hh: Household, routes, checks=None) -> list[str]:
    """Every URL text class at every GET door of `routes`; the failures."""
    from oikonome.web import security
    failures = []
    for r, path, query in routes:
        for label, url, params in text_cases(hh, r, path, query):
            security._limiter._hits.clear()
            resp = hh.client.get(url, params=params)
            where = f"GET {r.path} [{label}]"
            if server_error(resp):
                failures.append(f"{where} -> "
                                f"{resp.status_code} {resp.text[:80]}")
            if checks is not None:
                failures += checks.after(where, resp)
    return failures


def product_routes(app) -> list[APIRoute]:
    """Every route of the product, risky ones last, in path order."""
    out = [r for r in _flat(app.routes) if isinstance(r, APIRoute)
           and r.endpoint.__module__.startswith("oikonome.")]
    return sorted(out, key=lambda r: (bool(RISKY.search(r.path)), r.path,
                                      sorted(r.methods)))


def write_doors(app) -> list[APIRoute]:
    """Every route that changes state on a signed-in session's say: a
    POST/PUT/PATCH/DELETE whose handler reads the session."""
    import inspect
    from .util import MUTATING_METHODS
    out = []
    for r in product_routes(app):
        if not set(r.methods) & MUTATING_METHODS:
            continue
        names = {getattr(d.call, "__name__", "")
                 for d in _all_dependants(r.dependant) if d.call}
        if "current_user" in names or "current_user(" in inspect.getsource(
                r.endpoint):
            out.append(r)
    return out


def base_request(hh: Household, r: APIRoute) -> tuple[str, str, bytes]:
    """(method, url, body) a door takes with plausible values."""
    model = body_of(r.endpoint)
    body = base_body(hh, model)[0] if model is not None else {}
    return method_of(r), hh.path(r), encode(body)


def run_plain(hh: Household, routes, checks, *, expect=None) -> list[str]:
    """Each route once, with a plausible body: no 5xx, the checks, and
    `expect(route, reply)` (a failure text or None) when given."""
    from oikonome.web import security
    failures = []
    for r in routes:
        security._limiter._hits.clear()
        m, url, raw = base_request(hh, r)
        resp = hh.client.request(m, url, content=raw,
                                 headers={"content-type": "application/json"})
        where = f"{m} {url}"
        if server_error(resp):
            failures.append(f"{where} -> {resp.status_code} {resp.text[:80]}")
        failures += checks.after(where, resp)
        if expect is not None:
            why = expect(r, resp)
            if why:
                failures.append(f"{where} -> {resp.status_code} {why}")
        if not hh.alive():
            hh.renew()
            checks.settle()
    return failures


def group(app, modules: tuple[str, ...] | None, rest_of=()) -> list[APIRoute]:
    """The body doors whose handler lives in one of `modules` — or, with
    modules None, in none of `rest_of`."""
    out = []
    for r in doors(app):
        m = r.endpoint.__module__.rsplit(".", 1)[-1]
        if (m in modules) if modules is not None else (m not in rest_of):
            out.append(r)
    return out


# The doors split three ways by handler module, so each module of the fuzz
# stays inside the suite's per-module budget; the third takes every module
# the first two do not name, so a new API module is fuzzed without anyone
# remembering to add it.
GROUP_SESSION_AND_HOUSEHOLD = ("app",)
GROUP_LEDGER_BILLS_BUSINESS_ACCOUNTS = ("api_ledger", "api_bills",
                                       "api_business", "api_accounts")
NAMED_GROUPS = (GROUP_SESSION_AND_HOUSEHOLD
                + GROUP_LEDGER_BILLS_BUSINESS_ACCOUNTS)
