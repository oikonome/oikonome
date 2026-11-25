#!/usr/bin/env python3
"""Refuse a schema that would break a client build already installed.

    python3 scripts/check-api-compat.py            # exit 1 on a break

The generated types (specs/api-types.md) make the NEXT client build fail to
compile when the server stops sending a field; a build already on a phone is
compiled and cannot notice. This check compares the working tree's
specs/openapi.json with the copy every supported release carried, and fails
when the new schema would break a client built against the old one:

* a door (path and method) that is gone;
* a reply field that is gone, or no longer always sent (required before,
  optional now) — anywhere in the reply, nested shapes included;
* a reply value the old client could not take: a type it was not compiled
  for (a number becoming a string, a value becoming nullable, a new
  member of a closed set);
* a body key or query parameter the server now requires that an old
  client did not have to send;
* a body or parameter value an old client may send that the new schema
  no longer accepts (a narrowed type or a shrunk closed set).

Adding an optional field, a new door or a new optional parameter passes,
and so does widening what a body accepts. A door the old schema left
undocumented (`{}`, or a body with no named keys) has nothing to compare.

Which releases are compared is set in specs/api-compat.json:
`oldest_supported` names the oldest release tag whose builds the server
still serves; every release tag from it on that carries
specs/openapi.json is a baseline. A tag older than the schema file has no
copy, so the first commit that added the file stands in for it (and the
output says so). `accepted` lists breaks that were decided — each entry is
the exact problem line this script prints (`problem`), or the sha256 of
that line (`problem_sha256`) where the line would spell a name the tree no
longer carries, and a `reason`; nothing else is waived.

Exit codes: 0 compatible, 1 a break, 2 no baseline could be read (not a
git checkout, or no commit has the schema file).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = "specs/openapi.json"
CONFIG = ROOT / "specs" / "api-compat.json"

_METHODS = ("get", "put", "post", "delete", "patch")


# ---------------------------------------------------------------- comparing

class _Spec:
    """One schema document, with its own $ref table."""

    def __init__(self, doc: dict):
        self.doc = doc
        self.schemas = doc.get("components", {}).get("schemas", {})

    def resolve(self, node):
        seen = 0
        while isinstance(node, dict) and "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            node = self.schemas.get(name, {})
            seen += 1
            if seen > 50:
                break
        return node if isinstance(node, dict) else {}


def _ref_name(node) -> str | None:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"].rsplit("/", 1)[-1]
    return None


def _alternatives(spec: _Spec, node) -> list[dict]:
    """The schema as a list of single-type alternatives (anyOf/oneOf and a
    list-valued `type` flattened). [] never occurs; `{}` means anything."""
    node = spec.resolve(node)
    for key in ("anyOf", "oneOf"):
        if key in node:
            out: list[dict] = []
            for sub in node[key]:
                out.extend(_alternatives(spec, sub))
            return out
    if "allOf" in node and len(node["allOf"]) == 1:
        return _alternatives(spec, node["allOf"][0])
    t = node.get("type")
    if isinstance(t, list):
        return [dict(node, type=x) for x in t]
    if "const" in node and "type" not in node:
        v = node["const"]
        guess = ("null" if v is None else "boolean" if isinstance(v, bool)
                 else "integer" if isinstance(v, int)
                 else "number" if isinstance(v, float)
                 else "string" if isinstance(v, str) else None)
        if guess:
            return [dict(node, type=guess)]
    return [node]


def _values(alt: dict):
    """The closed set of values an alternative allows, or None if open."""
    if "const" in alt:
        return {json.dumps(alt["const"], sort_keys=True)}
    if "enum" in alt:
        return {json.dumps(v, sort_keys=True) for v in alt["enum"]}
    return None


def _is_any(alt: dict) -> bool:
    return not any(k in alt for k in ("type", "const", "enum", "properties",
                                      "items", "prefixItems",
                                      "additionalProperties"))


def _undocumented_object(alt: dict) -> bool:
    """An object the schema says nothing about (FastAPI's `dict` body)."""
    return (alt.get("type") == "object" and not alt.get("properties")
            and alt.get("additionalProperties", True) is True)


class _Compare:
    """`reader` must accept everything `sender` may produce. For a reply the
    old schema is the reader (the installed client) and the new one the
    sender; for a body or a parameter it is the other way round."""

    def __init__(self, old: _Spec, new: _Spec, problems: list[str]):
        self.old, self.new = old, new
        self.problems = problems

    def run(self, where: str, reader, sender, *, reply: bool) -> None:
        if reply:
            rspec, sspec = self.old, self.new
        else:
            rspec, sspec = self.new, self.old
        self._accepts(where, rspec, reader, sspec, sender, reply, set())

    def _say(self, msg: str) -> None:
        if msg not in self.problems:
            self.problems.append(msg)

    def _accepts(self, where, rspec, rnode, sspec, snode, reply, seen):
        key = (_ref_name(rnode), _ref_name(snode), reply)
        if key[0] and key[1]:
            if key in seen:
                return
            seen = seen | {key}
        ralts = _alternatives(rspec, rnode)
        salts = _alternatives(sspec, snode)
        if any(_is_any(a) for a in ralts):
            return
        if not reply and any(_undocumented_object(a) for a in salts):
            # the old body was never documented: nothing to compare
            return
        for s in salts:
            if _is_any(s):
                if reply:
                    self._say(f"{where}: now sends a value the old client "
                              "was not compiled for (any value)")
                else:
                    self._say(f"{where}: no longer accepts every value an "
                              "old client may send")
                continue
            match = [r for r in ralts if self._kind_fits(r, s)]
            if not match:
                stype = s.get("type", "?")
                rtypes = "|".join(sorted({str(r.get("type", "?"))
                                          for r in ralts}))
                if reply:
                    self._say(f"{where}: now sends {stype} where the old "
                              f"client expects {rtypes}")
                else:
                    self._say(f"{where}: no longer accepts {stype} "
                              f"(now {rtypes})")
                continue
            sv = _values(s)
            open_match = [r for r in match if _values(r) is None]
            if not open_match:
                allowed = set().union(*(_values(r) for r in match))
                extra = sorted((sv or {"<any>"}) - allowed) if sv is not None \
                    else ["<any>"]
                if extra:
                    if reply:
                        self._say(f"{where}: now sends {', '.join(extra)} "
                                  "outside the set the old client knows")
                    else:
                        self._say(f"{where}: no longer accepts "
                                  f"{', '.join(extra)}")
                    continue
            t = s.get("type")
            if t not in ("object", "array"):
                continue
            # a union (a discriminated block, a reply with two forms): the
            # sender's form is fine if ANY reader form takes it; otherwise
            # the closest reader form names what broke
            best: list[str] | None = None
            for r in (open_match or match):
                trial = _Compare(self.old, self.new, [])
                step = trial._object if t == "object" else trial._array
                step(where, rspec, r, sspec, s, reply, seen)
                if best is None or len(trial.problems) < len(best):
                    best = trial.problems
                if not best:
                    break
            for msg in best or ():
                self._say(msg)

    @staticmethod
    def _kind_fits(r: dict, s: dict) -> bool:
        rt, st = r.get("type"), s.get("type")
        if rt is None and "enum" in r and st is not None:
            return True
        return rt == st or (rt == "number" and st == "integer")

    def _array(self, where, rspec, r, sspec, s, reply, seen):
        ritems, sitems = r.get("items"), s.get("items")
        rtuple, stuple = r.get("prefixItems"), s.get("prefixItems")
        if rtuple and stuple:
            if len(rtuple) != len(stuple):
                self._say(f"{where}: tuple length changed "
                          f"({len(stuple)} sent, {len(rtuple)} read)")
            for i, (ri, si) in enumerate(zip(rtuple, stuple)):
                self._accepts(f"{where}[{i}]", rspec, ri, sspec, si, reply,
                              seen)
            return
        if ritems is not None and sitems is not None:
            self._accepts(f"{where}[]", rspec, ritems, sspec, sitems, reply,
                          seen)

    def _object(self, where, rspec, r, sspec, s, reply, seen):
        rprops = r.get("properties") or {}
        sprops = s.get("properties") or {}
        rreq = set(r.get("required") or ())
        sreq = set(s.get("required") or ())
        if reply:
            if not rprops and isinstance(r.get("additionalProperties"), dict):
                # a keyed map: every value the sender may put in it
                sap = s.get("additionalProperties")
                if isinstance(sap, dict):
                    self._accepts(f"{where}{{}}", rspec,
                                  r["additionalProperties"], sspec, sap,
                                  reply, seen)
                return
            for name, rprop in rprops.items():
                path = f"{where}.{name}"
                if name not in sprops:
                    self._say(f"{path}: removed from the reply")
                    continue
                if name in rreq and name not in sreq:
                    self._say(f"{path}: no longer always sent")
                self._accepts(path, rspec, rprop, sspec, sprops[name], reply,
                              seen)
            return
        # a body: the new server (reader) against what an old client sends
        if _undocumented_object(s):
            return
        for name in sorted(rreq):
            if name not in sreq:
                self._say(f"{where}.{name}: newly required in the body")
        for name, sprop in sprops.items():
            if name in rprops:
                self._accepts(f"{where}.{name}", rspec, rprops[name], sspec,
                              sprop, reply, seen)
        rap, sap = r.get("additionalProperties"), s.get("additionalProperties")
        if isinstance(rap, dict) and isinstance(sap, dict):
            self._accepts(f"{where}{{}}", rspec, rap, sspec, sap, reply, seen)


def _json_schema(content: dict | None):
    """{media type: schema} of a request body or a 200 reply."""
    out = {}
    for media, body in (content or {}).items():
        if isinstance(body, dict) and "schema" in body:
            out[media] = body["schema"]
    return out


def compare(old_doc: dict, new_doc: dict) -> list[str]:
    """Every way `new_doc` would break a client built against `old_doc`."""
    old, new = _Spec(old_doc), _Spec(new_doc)
    problems: list[str] = []
    cmp = _Compare(old, new, problems)
    for path, ops in sorted(old_doc.get("paths", {}).items()):
        for method in _METHODS:
            if method not in ops:
                continue
            door = f"{method.upper()} {path}"
            new_op = new_doc.get("paths", {}).get(path, {}).get(method)
            if new_op is None:
                problems.append(f"{door}: the door is gone")
                continue
            old_op = ops[method]
            # the reply
            o_reply = _json_schema(
                old_op.get("responses", {}).get("200", {}).get("content"))
            n_reply = _json_schema(
                new_op.get("responses", {}).get("200", {}).get("content"))
            for media, o_schema in o_reply.items():
                if media in n_reply:
                    cmp.run(f"{door} reply", o_schema, n_reply[media],
                            reply=True)
                elif not _is_any(old.resolve(o_schema)):
                    problems.append(f"{door} reply: no longer {media}")
            # the body
            o_body = old_op.get("requestBody") or {}
            n_body = new_op.get("requestBody") or {}
            o_media = _json_schema(o_body.get("content"))
            n_media = _json_schema(n_body.get("content"))
            if n_body.get("required") and n_media and not o_media:
                problems.append(f"{door} body: newly required")
            for media, o_schema in o_media.items():
                if media in n_media:
                    cmp.run(f"{door} body", n_media[media], o_schema,
                            reply=False)
                elif n_media:
                    problems.append(f"{door} body: no longer takes {media}")
            # query, path and header parameters
            o_params = {(p.get("in"), p.get("name")): p
                        for p in old_op.get("parameters", ())}
            for p in new_op.get("parameters", ()):
                k = (p.get("in"), p.get("name"))
                where = f"{door} {k[0]} parameter {k[1]}"
                o_p = o_params.get(k)
                if o_p is None:
                    if p.get("required"):
                        problems.append(f"{where}: newly required")
                    continue
                if p.get("required") and not o_p.get("required"):
                    problems.append(f"{where}: newly required")
                cmp.run(where, p.get("schema", {}), o_p.get("schema", {}),
                        reply=False)
    return problems


# ---------------------------------------------------------------- baselines

def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args],
                           capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return r.stdout if r.returncode == 0 else None


def _version(tag: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v(\d+(?:\.\d+)*)", tag)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def baselines(config: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """([(label, schema text)], notes): every copy to compare against."""
    notes: list[str] = []
    oldest = config["oldest_supported"]
    floor = _version(oldest)
    if floor is None:
        raise ValueError(f"oldest_supported {oldest!r} is not a release "
                         "tag (vMAJOR.MINOR.PATCH)")
    tags = (_git("tag", "--list", "v*") or "").split()
    supported = sorted((t for t in tags
                        if (_version(t) or ()) >= floor),
                       key=lambda t: _version(t) or ())
    out: list[tuple[str, str]] = []
    blobs: set[str] = set()

    def add(label: str, rev: str) -> bool:
        blob = (_git("rev-parse", "--verify", "-q",
                     f"{rev}:{SCHEMA}") or "").strip()
        if not blob:
            return False
        if blob not in blobs:
            blobs.add(blob)
            text = _git("show", f"{rev}:{SCHEMA}")
            if text is not None:
                out.append((label, text))
        return True

    first = (_git("log", "--diff-filter=A", "--format=%H", "--reverse",
                  "--", SCHEMA) or "").split()
    # only a release made after the file existed can carry a copy
    carrying = set((_git("tag", "--contains", first[0]) or "").split()) \
        if first else set()
    has_oldest = oldest in carrying and add(oldest, oldest)
    if not has_oldest:
        if first:
            add(f"first commit with {SCHEMA} ({first[0][:8]})", first[0])
            notes.append(
                f"{oldest} predates {SCHEMA} (or is not in this checkout); "
                f"compared against the first commit that has it, "
                f"{first[0][:8]}, instead")
    for t in supported:
        if t != oldest and t in carrying:
            add(t, t)
    return out, notes


def check(config: dict | None = None,
          current: str | None = None) -> tuple[int, list[str], list[str]]:
    """(exit code, problems, notes)."""
    config = config or load_config()
    if current is None:
        current = (ROOT / SCHEMA).read_text(encoding="utf-8")
    new_doc = json.loads(current)
    bases, notes = baselines(config)
    if not bases:
        return 2, [], notes + [f"no baseline {SCHEMA} could be read from "
                               "git"]
    accepted = {a["problem"] for a in config.get("accepted", ())
                if a.get("problem")}
    hashed = {a["problem_sha256"].lower() for a in config.get("accepted", ())
              if a.get("problem_sha256")}
    problems: list[str] = []
    for label, text in bases:
        notes.append(f"compared against {label}")
        for p in compare(json.loads(text), new_doc):
            waived = (p in accepted
                      or hashlib.sha256(p.encode()).hexdigest() in hashed)
            if not waived and f"{p}  [since {label}]" not in problems:
                problems.append(f"{p}  [since {label}]")
    return (1 if problems else 0), problems, notes


def main(argv: list[str]) -> int:
    code, problems, notes = check()
    for n in notes:
        print(n)
    for p in problems:
        print(p)
    if code == 1:
        print(f"{len(problems)} break(s) for installed clients; keep "
              "serving the old shape, or record a decided break in "
              "specs/api-compat.json `accepted`")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
