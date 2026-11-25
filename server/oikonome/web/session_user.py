"""The signed-in-user dependency, importable without the app.

Every route names one dependency for "who is asking": `current_user`. The
checks behind it (session, token scopes, lockouts, the write gates) live in
web/app.py beside the tables they read, and app.py imports every route
module. A route module that reached into app.py for the dependency while it
was being imported therefore could not be imported first: the app would
re-enter it half-built.

So the dependency OBJECT lives here, where nothing is imported but the
framework, and it looks the checks up when a request arrives. app.py
re-exports this same object as `app.current_user`, so there is one
callable: FastAPI resolves it once per request however a route came by it,
and an override keyed on it reaches every route.
"""

from __future__ import annotations

from fastapi import Request


def current_user(request: Request) -> dict:
    """The caller's session row, or the refusal their standing earns."""
    from . import app
    return app._resolve_current_user(request)
