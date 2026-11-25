"""Every JSON reply the suite receives from a documented door is held to the
shape the door documents.

A documented shape (web/schemas, `@documented`) is never run over a live
request: the server sends what its handler builds. So the proof that the
shape is TRUE is here. Installed from tests/__init__.py, the hook wraps
`TestClient.request`; after each request it finds the route that answered
(the app's own first-match order), and when that route documents a response
model and the reply is a successful (2xx) JSON body, validates the body against the model
strictly, undeclared keys refused. A mismatch fails the test that made the
request, naming the door and the field.

The whole suite thereby checks the documented doors on every reply it
happens to receive. Set OIKONOME_API_CONFORMANCE_REPORT=<path> to have
each test process write how many replies it validated, per door, to
`<path>.<pid>` when it exits — a sharded run is several processes, and one
shared file would hold only the last to exit. Sum them with

    python -m tests.api_conformance <path>

which prints the total and writes it to `<path>`.
"""

from __future__ import annotations

import atexit
import json
import os

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from oikonome.web.schemas import response_of

_validated: dict[str, int] = {}
_tables: dict[int, list[APIRoute]] = {}
_installed = False


def _flat(routes):
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _flat(inner.routes)
        elif isinstance(r, APIRoute):
            yield r


def _table(app) -> list[APIRoute]:
    t = _tables.get(id(app))
    if t is None:
        t = _tables[id(app)] = list(_flat(getattr(app, "routes", ())))
    return t


def route_for(app, method: str, path: str) -> APIRoute | None:
    """The route that answered `method path`: the first whose pattern and
    method match, as Starlette picks it."""
    for r in _table(app):
        if method in r.methods and r.path_regex.match(path):
            return r
    return None


def check_reply(app, method: str, path: str, status: int,
                content_type: str, content: bytes) -> None:
    """Raise AssertionError when a documented door's successful JSON reply
    (any 2xx but 204: a background job's 202 is its reply too) does not
    match its documented model."""
    if not 200 <= status < 300 or status == 204 \
            or not path.startswith("/api/") \
            or not content_type.startswith("application/json"):
        return
    route = route_for(app, method, path)
    if route is None:
        return
    model = response_of(route.endpoint)
    if model is None:
        return
    try:
        model.model_validate_json(content, strict=True)
    except ValidationError as e:
        raise AssertionError(
            f"{method} {route.path}: the reply does not match its "
            f"documented shape {model.__name__}:\n{e}") from None
    key = f"{method} {route.path}"
    _validated[key] = _validated.get(key, 0) + 1


def validated() -> dict[str, int]:
    """Replies validated so far in this process, per door."""
    return dict(_validated)


def _report() -> None:
    path = os.environ.get("OIKONOME_API_CONFORMANCE_REPORT")
    if not path:
        return
    with open(f"{path}.{os.getpid()}", "w", encoding="utf-8") as f:
        json.dump({"validated": sum(_validated.values()),
                   "doors": dict(sorted(_validated.items()))}, f, indent=1)


def merge_reports(path: str) -> dict:
    """The per-process reports `<path>.<pid>` summed into one."""
    import glob
    doors: dict[str, int] = {}
    parts = sorted(p for p in glob.glob(glob.escape(path) + ".*")
                   if p.rsplit(".", 1)[1].isdigit())
    for part in parts:
        with open(part, encoding="utf-8") as f:
            for k, n in json.load(f)["doors"].items():
                doors[k] = doors.get(k, 0) + n
    return {"validated": sum(doors.values()), "processes": len(parts),
            "doors": dict(sorted(doors.items()))}


if __name__ == "__main__":
    import sys
    # the merging process validated nothing and must not add a report of
    # its own (tests/__init__ installed the hook on the way in)
    atexit.unregister(sys.modules["tests.api_conformance"]._report)
    merged = merge_reports(sys.argv[1])
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=1)
    print(f"{merged['validated']} replies validated in "
          f"{merged['processes']} processes")


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    original = TestClient.request

    def request(self, method, url, *args, **kwargs):
        resp = original(self, method, url, *args, **kwargs)
        check_reply(self.app, str(method).upper(), resp.request.url.path,
                    resp.status_code, resp.headers.get("content-type", ""),
                    resp.content)
        return resp

    request.__wrapped__ = original
    TestClient.request = request
    atexit.register(_report)
