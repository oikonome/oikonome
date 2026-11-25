"""The shapes the JSON API sends and accepts, written down for the clients.

Each model here describes what one door already sends (or reads); none of
them is ever run over a live request. A door is described with
`@documented(response=..., body=...)` placed UNDER its route decorator,
which only sets attributes on the handler. FastAPI never sees the models,
so nothing is filtered, coerced or refused because of them: a model that is
wrong fails the suite (tests/api_conformance.py validates every response
the suite receives), never a request.

`scripts/gen-api-types.py` reads these attributes into specs/openapi.json
and from there into the TypeScript both clients compile against. The design
is specs/api-types.md.

A model describes the wire as it is: `pending` stays an int if the server
sends 0/1. A field that can be absent has a default (of its own type when
it is never null: `reason: str = ""`); a field that is always present but
can be null is `X | None` without one.
"""

from __future__ import annotations

from typing import Callable, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

F = TypeVar("F", bound=Callable)


class Shape(BaseModel):
    """Base of every documented shape: an undeclared key is a mismatch,
    so the generated type is exact. A docstring under a field becomes its
    description and, in the clients' types, its comment."""
    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


class Ok(Shape):
    """The bare acknowledgement many doors answer with."""
    ok: Literal[True]


RESPONSE_ATTR = "__api_response__"
BODY_ATTR = "__api_body__"


def documented(*, response: type[Shape] | None = None,
               body: type[Shape] | None = None) -> Callable[[F], F]:
    """Record a door's shapes on its handler and return the handler itself,
    unchanged: no wrapper, so the route's signature, dependencies and
    behaviour are exactly what they were."""
    def mark(fn: F) -> F:
        if response is not None:
            setattr(fn, RESPONSE_ATTR, response)
        if body is not None:
            setattr(fn, BODY_ATTR, body)
        return fn
    return mark


def response_of(fn) -> type[Shape] | None:
    return getattr(fn, RESPONSE_ATTR, None)


def body_of(fn) -> type[Shape] | None:
    return getattr(fn, BODY_ATTR, None)
