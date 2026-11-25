"""The JSON API's route table is pinned, so moving handlers between modules
cannot change what the server answers.

`web/api.py` is being split into one module per area (specs/api-routers.md).
A move is meant to change nothing a client can see: the same paths and
methods, the same parameters with the same types and defaults, the same
dependencies (rate limits, auth, body readers) in the same order, the same
response settings, and the same ORDER of routes — Starlette takes the first
pattern that matches, so two patterns that can match one path answer
differently if they swap places.

The fixture is that table, written from the tree before any move. Every
stage of the split must leave the same routes with the same entries, and the
same order for every two routes one request could match (computed, not
listed by hand). The order of routes that can never meet decides nothing,
and an area spread through api.py registers at one import point, so that
order is not pinned. A deliberate change to
the API (a new route, a renamed parameter) rewrites it on purpose:

    python -m tests.test_api_route_table_is_unchanged_by_a_module_move --write

and the diff of the fixture is then the reviewable statement of what changed.

Which module a handler lives in is deliberately NOT part of the table: the
split exists to change exactly that. A handler must still live in one of the
API modules (`api.py`, `api_common.py`, `api_<area>.py`), which is recorded
as the normalised module name `oikonome.web.api`.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import unittest

FIXTURE = (pathlib.Path(__file__).resolve().parent / "fixtures"
           / "api_route_table.json")

_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
_API_MODULE = re.compile(r"^oikonome\.web\.api(?:_[a-z0-9_]+)?$")


def _norm_module(mod: str | None) -> str | None:
    """Every API module reads as the one it was split from."""
    if mod and _API_MODULE.match(mod):
        return "oikonome.web.api"
    return mod


def _text(v) -> str:
    """repr without memory addresses (they differ on every import)."""
    return _ADDR.sub("", repr(v))


def _call(fn) -> dict:
    """A dependency or endpoint callable: where it is, what it is called,
    and — for a factory-made closure such as `limit("retirement", 30, 60)`
    — the plain values it closed over, so a changed rate limit shows."""
    out = {"module": _norm_module(getattr(fn, "__module__", None)),
           "qualname": getattr(fn, "__qualname__", type(fn).__name__)}
    cells = getattr(fn, "__closure__", None) or ()
    held = []
    for c in cells:
        try:
            v = c.cell_contents
        except ValueError:          # an empty cell
            held.append("<empty>")
            continue
        if isinstance(v, (str, int, float, bool, type(None))):
            held.append(v)
        elif isinstance(v, (tuple, frozenset)) and all(
                isinstance(x, (str, int, float, bool)) for x in v):
            held.append(sorted(map(repr, v)) if isinstance(v, frozenset)
                        else list(v))
        elif callable(v):
            held.append(getattr(v, "__qualname__", type(v).__name__))
        else:
            held.append(type(v).__name__)
    if held:
        out["closure"] = held
    return out


def _param(p) -> dict:
    fi = p.field_info
    return {"name": p.name, "alias": p.alias,
            "annotation": _text(fi.annotation),
            "metadata": [_text(m) for m in fi.metadata],
            "default": _text(fi.default),
            "required": fi.is_required(),
            "kind": type(fi).__name__}


def _dependant(d) -> dict:
    return {
        "call": _call(d.call) if d.call is not None else None,
        "name": d.name,
        "use_cache": d.use_cache,
        "scope": _text(getattr(d, "scope", None)),
        "path": [_param(p) for p in d.path_params],
        "query": [_param(p) for p in d.query_params],
        "header": [_param(p) for p in d.header_params],
        "cookie": [_param(p) for p in d.cookie_params],
        "body": [_param(p) for p in d.body_params],
        "request": d.request_param_name,
        "response": d.response_param_name,
        "background": d.background_tasks_param_name,
        "dependencies": [_dependant(s) for s in d.dependencies],
    }


def _route(r) -> dict:
    rc = r.response_class
    return {
        "path": r.path,
        "methods": sorted(r.methods),
        "name": r.name,
        "unique_id": r.unique_id,
        "endpoint": _call(r.endpoint),
        "is_async": __import__("inspect").iscoroutinefunction(r.endpoint),
        "route_dependencies": [
            {"call": _call(x.dependency), "use_cache": x.use_cache}
            for x in r.dependencies],
        "dependant": _dependant(r.dependant),
        "response_model": _text(r.response_model),
        "response_class": getattr(getattr(rc, "value", rc), "__name__",
                                  _text(rc)),
        "status_code": r.status_code,
        "include_in_schema": r.include_in_schema,
        "tags": [str(t) for t in r.tags],
        "summary": r.summary,
        "deprecated": r.deprecated,
        "responses": _text(r.responses),
        "operation_id": r.operation_id,
    }


# ---- which patterns can answer the same request ----------------------------
# Starlette tries routes in order and the first match wins, so order only
# decides anything between two patterns some one path matches. A pattern is
# read as a small automaton: a literal character, `{x}` (one or more
# characters other than "/"), `{x:path}` (any characters, "/" included).
# Two patterns overlap when the product automaton reaches both accepting
# states.

def _pattern(path: str) -> list:
    els = []
    for m in re.finditer(r"\{[^}]+\}|.", path, re.S):
        tok = m.group(0)
        if tok.startswith("{"):
            els += ([("any*",)] if tok.endswith(":path}")
                    else [("ns",), ("ns*",)])
        else:
            els.append(("c", tok))
    return els


def _closure(els, states):
    st = set(states)
    stack = list(states)
    while stack:
        i = stack.pop()
        if i < len(els) and els[i][0] in ("ns*", "any*") and i + 1 not in st:
            st.add(i + 1)
            stack.append(i + 1)
    return frozenset(st)


def _step(els, states, ch):
    out = set()
    for i in states:
        if i >= len(els):
            continue
        kind = els[i][0]
        if kind == "c" and els[i][1] == ch:
            out.add(i + 1)
        elif kind == "ns" and ch != "/":
            out.add(i + 1)
        elif kind in ("ns*", "any*") and (ch != "/" or kind == "any*"):
            out.add(i)
    return _closure(els, out)


def patterns_overlap(p: str, q: str) -> bool:
    """Does some request path match both route patterns?"""
    a, b = _pattern(p), _pattern(q)
    # every literal character, "/", and one character no literal uses
    alphabet = (set(p) | set(q) | {"/", "\x00"}) - set("{}")
    start = (_closure(a, {0}), _closure(b, {0}))
    seen, stack = {start}, [start]
    while stack:
        x, y = stack.pop()
        if len(a) in x and len(b) in y:
            return True
        for ch in alphabet:
            nx, ny = _step(a, x, ch), _step(b, y, ch)
            if nx and ny and (nx, ny) not in seen:
                seen.add((nx, ny))
                stack.append((nx, ny))
    return False


def overlapping_pairs(routes: list) -> list:
    """[methods, earlier path, later path] for every two API routes one
    request could reach, in route-table order."""
    out = []
    for i, r in enumerate(routes):
        for s in routes[i + 1:]:
            both = sorted(set(r["methods"]) & set(s["methods"]))
            if both and r["path"] != s["path"] \
                    and patterns_overlap(r["path"], s["path"]):
                out.append([both, r["path"], s["path"]])
    return out


def build_table() -> dict:
    from oikonome.web import api
    from oikonome.web.app import app
    included = []
    for entry in app.routes:
        inner = getattr(entry, "original_router", None)
        if inner is None:
            continue
        mods = sorted({_norm_module(getattr(x, "endpoint", None)
                                    and x.endpoint.__module__)
                       for x in inner.routes if getattr(x, "endpoint", None)}
                      - {None})
        if not all(m.startswith("oikonome.") for m in mods):
            # an installed add-on's router: not part of this tree, and
            # absent on a plain instance
            continue
        included.append({"prefix": inner.prefix, "modules": mods,
                         "is_api_router": inner is api.router})
    routes = [_route(r) for r in api.router.routes]
    return {
        "api_router_prefix": api.router.prefix,
        "api_router_dependencies": [_call(x.dependency)
                                    for x in api.router.dependencies],
        # every API route, in the order Starlette tries them
        "api_routes": routes,
        # the pairs whose ORDER decides an answer: the one ordering fact a
        # split must keep even if it ever reorders routes that cannot meet
        "overlapping_pairs": overlapping_pairs(routes),
        # which routers the app mounts, in mount order
        "included_routers": included,
        "middleware": [m.cls.__name__ for m in app.user_middleware],
    }


def _dump(table: dict) -> str:
    return json.dumps(table, indent=1, sort_keys=True) + "\n"


class ApiRouteTableTests(unittest.TestCase):

    def test_the_route_table_matches_the_pinned_fixture(self):
        """Same routes, each entry unchanged, and the same order for every
        two routes one request could reach.

        The order of routes that can never meet decides nothing, and an
        area whose routes were spread through api.py registers them all at
        the one point its module is imported, so that order is not pinned.
        The overlapping pairs are computed afresh from the live table and
        from the pinned one, never taken on trust from the fixture."""
        got = build_table()
        want = json.loads(FIXTURE.read_text())
        key = lambda r: (r["path"], r["methods"])          # noqa: E731
        gr = {tuple(map(str, key(r))): r for r in got["api_routes"]}
        wr = {tuple(map(str, key(r))): r for r in want["api_routes"]}
        self.assertEqual(len(gr), len(got["api_routes"]),
                         "two routes share a path and methods")
        self.assertEqual(sorted(set(wr) - set(gr)), [], "routes went missing")
        self.assertEqual(sorted(set(gr) - set(wr)), [], "routes appeared")
        for k, w in wr.items():
            g = gr[k]
            if g != w:
                self.fail(f"{w['methods']} {w['path']} ({w['name']}) differs\n"
                          + "\n".join(f"  {f}: {w.get(f)!r} -> {g.get(f)!r}"
                                      for f in sorted(set(g) | set(w))
                                      if g.get(f) != w.get(f)))
        self.assertEqual(overlapping_pairs(want["api_routes"]),
                         want["overlapping_pairs"],
                         "the fixture's overlapping pairs are stale")
        self.assertEqual(got["overlapping_pairs"], want["overlapping_pairs"],
                         "two routes one request could reach changed order")
        rest = ("api_router_prefix", "api_router_dependencies",
                "included_routers", "middleware")
        self.assertEqual({k: got[k] for k in rest},
                         {k: want[k] for k in rest})

    def test_a_swap_of_overlapping_routes_is_caught(self):
        """The order check bites: putting the later route of an
        overlapping pair first changes the computed pairs."""
        want = json.loads(FIXTURE.read_text())
        self.assertTrue(want["overlapping_pairs"])
        _, earlier, later = want["overlapping_pairs"][0]
        routes = list(want["api_routes"])
        i = next(n for n, r in enumerate(routes) if r["path"] == earlier)
        j = next(n for n, r in enumerate(routes) if r["path"] == later)
        routes[i], routes[j] = routes[j], routes[i]
        self.assertNotEqual(overlapping_pairs(routes),
                            want["overlapping_pairs"])

    def test_the_overlap_reading_tells_patterns_apart(self):
        """A reader that never found an overlap would pin nothing."""
        self.assertTrue(patterns_overlap("/a/{x}", "/a/b"))
        self.assertTrue(patterns_overlap("/t/{x:path}/c", "/t/a/b/c"))
        self.assertFalse(patterns_overlap("/t/{x:path}/c", "/t/bulk"))
        self.assertFalse(patterns_overlap("/a/{x}/owner", "/a/mx/keys"))
        self.assertFalse(patterns_overlap("/a/{x}", "/a/b/c"))

    def test_the_table_sees_every_api_route(self):
        """A table that walked the wrong router would pin nothing."""
        table = build_table()
        self.assertGreater(len(table["api_routes"]), 150)
        paths = {r["path"] for r in table["api_routes"]}
        self.assertIn("/api/retirement", paths)
        self.assertIn("/api/transactions", paths)
        self.assertEqual(sum(r["is_api_router"]
                             for r in table["included_routers"]), 1)


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.write_text(_dump(build_table()))
        print(f"wrote {FIXTURE}")
    else:
        unittest.main()
