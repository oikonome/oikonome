"""Read a documented shape as both clients receive it: the server's model
field, and the generated TypeScript both clients compile against.

Source-shape tests that once grepped a hand-written client type for a
field now hold the property where it lives: the model documents the field
(so the conformance hook proves the server sends it), the generated type
carries it (so a client cannot drop it without the type checker noticing),
and the client aliases the generated type under the name its screens use.
"""

from __future__ import annotations

import pathlib
import re

import oikonome

APP = pathlib.Path(oikonome.__file__).parent.parent.parent
GENERATED = {
    "web": APP / "webapp" / "src" / "api" / "api.generated.ts",
    "mobile": APP / "mobile" / "src" / "lib" / "api.generated.ts",
}
CLIENT = {
    "web": APP / "webapp" / "src" / "api" / "client.ts",
    "mobile": APP / "mobile" / "src" / "lib" / "api.ts",
}


def schema_block(which: str, name: str) -> str:
    """The generated TypeScript of components["schemas"][name]."""
    src = GENERATED[which].read_text(encoding="utf-8")
    start = src.index(f"\n        {name}: {{")
    end = src.index("\n        };", start)
    return src[start:end]


def ts_field(which: str, name: str, field: str) -> str:
    """The generated TypeScript type of one field of a shape, as written
    (`number | null`, `"owner" | "member"`), with `?` kept when optional."""
    m = re.search(rf"\n\s+{re.escape(field)}(\??): ([^;]+);",
                  schema_block(which, name))
    if m is None:
        raise AssertionError(f"{name}.{field} is not in the {which} "
                             "generated types")
    return m.group(1) + m.group(2)


def aliases(which: str, alias: str, schema: str) -> bool:
    """Whether the client exports `alias` as (a view of) the generated
    `schema` shape."""
    src = CLIENT[which].read_text(encoding="utf-8")
    m = re.search(rf"export type {re.escape(alias)} =([^;]+);", src)
    return bool(m) and f'S["{schema}"]' in m.group(1)
