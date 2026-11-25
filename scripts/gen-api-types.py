#!/usr/bin/env python3
"""Generate the clients' API types from the server.

    python3 scripts/gen-api-types.py          # write specs/openapi.json + both .ts files
    python3 scripts/gen-api-types.py --check  # exit 1 if anything is stale

Run from anywhere; it imports the app in-process (no server, no database,
no network). Two steps:

1. the app's OpenAPI, cut to the client-facing /api doors, with the shapes
   documented in oikonome/web/schemas merged in and the handler prose
   stripped, is written to specs/openapi.json (sorted keys);
2. the pinned openapi-typescript in tools/api-types (install it with
   `cd tools/api-types && npm ci --ignore-scripts`) turns that file into
   TypeScript, written byte-identical to both clients.

Exit codes: 0 current / written, 1 stale, 2 the tool is not installed or
node is missing (the .ts half could not be checked). The design is
specs/api-types.md; server/tests/test_api_types_are_generated_and_current.py
runs --check.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "specs" / "openapi.json"
TS_TARGETS = (ROOT / "webapp" / "src" / "api" / "api.generated.ts",
              ROOT / "mobile" / "src" / "lib" / "api.generated.ts")
TOOL_DIR = ROOT / "tools" / "api-types"
TOOL_CLI = TOOL_DIR / "node_modules" / "openapi-typescript" / "bin" / "cli.js"

# /api doors no client calls: the dev-only test doors and the provider
# webhooks. Listed by the module the handler lives in; the test pins this
# set, so dropping a new module from the clients' types is a decision.
EXCLUDED_MODULES = frozenset({
    "oikonome.web.testing",
    "oikonome.web.plaid_webhook",
    "oikonome.web.postmark_webhook",
})

HEADER = ("// GENERATED from specs/openapi.json by scripts/gen-api-types.py —\n"
          "// do not edit by hand. The server's documented shapes\n"
          "// (server/oikonome/web/schemas) are the source; web and mobile\n"
          "// carry byte-identical copies (specs/api-types.md).\n\n")

_OPERATION_PROSE = ("summary", "description")


class AddonRoutesPresent(RuntimeError):
    """An installed add-on registered routers: its doors must never enter
    the public schema file."""


# this tree's server, ahead of any installed copy of the package
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))


def _plain_instance() -> None:
    """Import the app as an instance with no add-on installed, whatever the
    environment has: the schema must not depend on whose machine ran this,
    and an add-on's doors must never reach the public file. Only possible
    before the app is first imported; build_schema refuses afterwards if an
    add-on's routers made it in."""
    if "oikonome.web.app" in sys.modules:
        return
    from oikonome import ext
    ext.entry_points = lambda **_kw: ()
    ext._group.cache_clear()


def _app():
    _plain_instance()
    from oikonome.web.app import app
    return app


def _flat_routes(routes):
    """Every APIRoute in registration order, included routers unwrapped
    (FastAPI 0.16x keeps an included router as one entry)."""
    from fastapi.routing import APIRoute
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _flat_routes(inner.routes)
        elif isinstance(r, APIRoute):
            yield r


def client_routes(app=None):
    """The routes the clients' types cover, in registration order."""
    app = app or _app()
    return [r for r in _flat_routes(app.routes)
            if r.path.startswith("/api/")
            and r.endpoint.__module__ not in EXCLUDED_MODULES]


def _refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            out.add(ref.rsplit("/", 1)[1])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def _documented_schemas(routes) -> dict:
    """JSON Schema for every documented model, keyed by model name."""
    from oikonome.web.schemas import body_of, response_of
    from pydantic.json_schema import models_json_schema
    models = {}
    for r in routes:
        for m in (response_of(r.endpoint), body_of(r.endpoint)):
            if m is None:
                continue
            seen = models.get(m.__name__)
            if seen is not None and seen is not m:
                raise ValueError(
                    f"two documented models are both named {m.__name__!r} "
                    f"({seen.__module__} and {m.__module__}); the generated "
                    "TypeScript has one namespace, so rename one")
            models[m.__name__] = m
    if not models:
        return {}
    _, top = models_json_schema(
        [(m, "validation") for m in models.values()],
        ref_template="#/components/schemas/{model}")
    return top.get("$defs", {})


def build_schema(app=None) -> dict:
    """The filtered, documented OpenAPI document (a dict)."""
    app = app or _app()
    from oikonome import ext
    from oikonome.web.schemas import body_of, response_of
    if ext.routers():
        raise AddonRoutesPresent(
            "an add-on has registered routers; generate the clients' types "
            "on an instance without add-ons so its doors stay out of "
            "specs/openapi.json")
    full = app.openapi()
    keep = client_routes(app)
    # OpenAPI writes `{txn_id:path}` as `{txn_id}`
    kept_ops = {(re.sub(r"\{(\w+):\w+\}", r"{\1}", r.path), m.lower()): r
                for r in keep for m in r.methods}
    paths = {}
    for path, ops in full["paths"].items():
        for method, op in ops.items():
            route = kept_ops.get((path, method))
            if route is None:
                continue
            op = {k: v for k, v in op.items() if k not in _OPERATION_PROSE}
            resp = response_of(route.endpoint)
            if resp is not None:
                op["responses"]["200"]["content"] = {
                    "application/json": {"schema": {
                        "$ref": f"#/components/schemas/{resp.__name__}"}}}
            body = body_of(route.endpoint)
            if body is not None:
                # FastAPI knows whether the door insists on a body (a
                # `Body(default=None)` or an or-empty reader does not)
                required = op.get("requestBody", {}).get("required", False)
                op["requestBody"] = {"required": required, "content": {
                    "application/json": {"schema": {
                        "$ref": f"#/components/schemas/{body.__name__}"}}}}
            paths.setdefault(path, {})[method] = op
    components = dict(full.get("components", {}).get("schemas", {}))
    for name, sch in _documented_schemas(keep).items():
        if name in components:
            raise ValueError(f"documented model {name!r} collides with a "
                             "schema FastAPI generated; rename the model")
        components[name] = sch
    # only what the kept doors reach (the admin console's bodies go)
    reach: set[str] = set()
    _refs(paths, reach)
    todo = list(reach)
    while todo:
        found: set[str] = set()
        _refs(components.get(todo.pop(), {}), found)
        for n in found - reach:
            reach.add(n)
            todo.append(n)
    return {
        "openapi": full["openapi"],
        "info": {"title": "Oikonome client API", "version": "generated"},
        "paths": paths,
        "components": {"schemas": {n: components[n] for n in sorted(reach)}},
    }


def schema_text(app=None) -> str:
    return json.dumps(build_schema(app), indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def _node() -> str | None:
    return shutil.which("node")


def ts_text(schema_path: pathlib.Path) -> str:
    """The TypeScript the pinned generator writes for `schema_path`."""
    node = _node()
    if node is None:
        raise FileNotFoundError("node is not installed")
    if not TOOL_CLI.exists():
        raise FileNotFoundError(
            f"{TOOL_CLI.relative_to(ROOT)} is missing — install the pinned "
            "generator: cd tools/api-types && npm ci --ignore-scripts")
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "api.generated.ts"
        # --default-non-nullable=false: a field with a default is one the
        # server may leave out, so it stays optional in TypeScript (the
        # generator's own default would make it required)
        r = subprocess.run([node, str(TOOL_CLI), str(schema_path), "-o",
                            str(out), "--default-non-nullable=false"], capture_output=True, text=True,
                           check=False,
                           cwd=str(TOOL_DIR))
        if r.returncode != 0:
            raise RuntimeError("openapi-typescript failed:\n" + r.stdout
                               + r.stderr)
        return HEADER + out.read_text(encoding="utf-8")


def check() -> tuple[int, list[str]]:
    """(exit code, what is stale or could not be checked)."""
    problems: list[str] = []
    want = schema_text()
    have = SCHEMA_FILE.read_text(encoding="utf-8") if SCHEMA_FILE.exists() \
        else None
    if have != want:
        problems.append(f"{SCHEMA_FILE.relative_to(ROOT)} is stale")
    copies = [p.read_bytes() if p.exists() else None for p in TS_TARGETS]
    if copies[0] != copies[1]:
        problems.append("the web and mobile copies differ")
    with tempfile.TemporaryDirectory() as tmp:
        sp = pathlib.Path(tmp) / "openapi.json"
        sp.write_text(want, encoding="utf-8")
        try:
            ts = ts_text(sp)
        except FileNotFoundError as e:
            problems.append(f"the .ts files could not be checked: {e}")
            return 2, problems
    for p, got in zip(TS_TARGETS, copies):
        if got != ts.encode("utf-8"):
            problems.append(f"{p.relative_to(ROOT)} is stale")
    return (1 if problems else 0), problems


def write() -> None:
    SCHEMA_FILE.write_text(schema_text(), encoding="utf-8")
    ts = ts_text(SCHEMA_FILE)
    for p in TS_TARGETS:
        p.write_text(ts, encoding="utf-8")


def main(argv: list[str]) -> int:
    os.environ.setdefault("OIKONOME_DEV", "1")
    if "--check" in argv:
        code, problems = check()
        for p in problems:
            print(p)
        if code:
            print("regenerate with: python3 scripts/gen-api-types.py")
        return code
    write()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
