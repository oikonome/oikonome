#!/usr/bin/env python3
"""Generate the Txn TypeScript type for web AND mobile from
specs/ledger-row.schema.json — one shape, three surfaces.

    python3 scripts/gen-ledger-row-types.py          # write both files
    python3 scripts/gen-ledger-row-types.py --check  # exit 1 if stale

The server test tests/test_ledger_row_contract.py runs --check, so a
schema edit without a regenerate fails the suite, and the API is held to
the same key set."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "specs" / "ledger-row.schema.json"
TARGETS = [ROOT / "webapp" / "src" / "api" / "ledger-row.generated.ts",
           ROOT / "mobile" / "src" / "lib" / "ledger-row.generated.ts"]

_TS = {"string": "string", "number": "number", "integer": "number",
       "boolean": "boolean"}


def ts_type(prop: dict) -> str:
    # An unknown JSON-schema type falling through to TypeScript `null`
    # would mean adding an array or object field to the schema silently
    # generates a field no client could ever hold. Refuse instead: a schema
    # this script cannot express is a bug in the script, not the schema.
    t = prop.get("type")
    types = t if isinstance(t, list) else [t]
    out = []
    for x in types:
        if x == "null":
            out.append("null")
        elif x in _TS:
            out.append(_TS[x])
        elif x == "array":
            # a list of one shape, rendered inline (`{ a: string }[]`)
            out.append(ts_type(prop["items"]) + "[]")
        elif x == "object":
            props = prop.get("properties") or {}
            req = set(prop.get("required") or [])
            fields = "; ".join(
                f"{k}{'' if k in req else '?'}: {ts_type(v)}"
                for k, v in props.items())
            out.append("{ " + fields + " }")
        else:
            raise ValueError(
                f"ledger-row schema type {x!r} has no TypeScript mapping — "
                "teach gen-ledger-row-types.py how to render it")
    return " | ".join(out)


def render(schema: dict) -> str:
    req = set(schema.get("required") or [])
    lines = ["// GENERATED from specs/ledger-row.schema.json by",
             "// scripts/gen-ledger-row-types.py — do not edit by hand. Change the",
             "// schema, run the script, and web + mobile + the server move together",
             "// (tests/test_ledger_row_contract.py holds all three to it).",
             "",
             "/** One transaction as every ledger surface receives it. */",
             "export interface Txn {"]
    for name, prop in schema["properties"].items():
        opt = "" if name in req else "?"
        desc = prop.get("description")
        if desc:
            lines.append(f"  /** {desc} */")
        lines.append(f"  {name}{opt}: {ts_type(prop)};")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    out = render(schema)
    check = "--check" in sys.argv
    stale = []
    for t in TARGETS:
        cur = t.read_text(encoding="utf-8") if t.exists() else None
        if cur != out:
            if check:
                stale.append(str(t.relative_to(ROOT)))
            else:
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text(out, encoding="utf-8")
                print(f"wrote {t.relative_to(ROOT)}")
    if stale:
        print("stale generated types (run scripts/gen-ledger-row-types.py):",
              *stale, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
