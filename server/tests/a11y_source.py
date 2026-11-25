"""A small reader of JSX elements for the accessibility source tests.

It is not a parser: it finds an element's opening tag (attributes may hold
`{…}` expressions, arrows and strings), its children up to the matching
closing tag, and the words a person would see in those children. That is
enough to ask "is this control only a glyph?" and "does it carry a name?"
from the Python suite alone (specs/accessibility.md).
"""

import pathlib
import re

import oikonome

APP = pathlib.Path(oikonome.__file__).parent.parent.parent
WEB = APP / "webapp" / "src"
MOB = APP / "mobile" / "src"


def sources(tree: pathlib.Path):
    for p in sorted(tree.rglob("*.tsx")):
        if "node_modules" in p.parts:
            continue
        yield p


def _skip_string(src: str, i: int) -> int:
    q = src[i]
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if q == "`" and src.startswith("${", i):
            i = _skip_braces(src, i + 1)
            continue
        if c == q:
            return i + 1
        i += 1
    return i


def _skip_braces(src: str, i: int) -> int:
    """From the `{` at i, the index after its matching `}`."""
    depth = 0
    while i < len(src):
        c = src[i]
        if c in "\"'`":
            i = _skip_string(src, i)
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = len(src) if j < 0 else j + 2
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = len(src) if j < 0 else j
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _open_tag_end(src: str, i: int):
    """From just past `<Tag`, the index after the opening tag's `>` and
    whether it closed itself (`/>`)."""
    while i < len(src):
        c = src[i]
        if c == "{":
            i = _skip_braces(src, i)
            continue
        if c in "\"'":
            i = _skip_string(src, i)
            continue
        if src.startswith("/>", i):
            return i + 2, True
        if c == ">":
            return i + 1, False
        i += 1
    return i, True


class Element:
    def __init__(self, path, line, tag, attrs, children, start=0, end=0):
        self.path, self.line, self.tag = path, line, tag
        self.attrs, self.children = attrs, children
        # offsets in the source: the `<` of the tag, and past its close
        self.start, self.end = start, end

    def where(self):
        return f"{self.path.relative_to(APP)}:{self.line}"

    def has(self, *names):
        return any(re.search(r"(?<![\w-])" + re.escape(n) + r"\s*=", self.attrs)
                   for n in names)

    def attr(self, name):
        m = re.search(r"(?<![\w-])" + re.escape(name) + r"\s*=\s*", self.attrs)
        if not m:
            return None
        i = m.end()
        if i < len(self.attrs) and self.attrs[i] == "{":
            return self.attrs[i:_skip_braces(self.attrs, i)]
        if i < len(self.attrs) and self.attrs[i] in "\"'":
            return self.attrs[i:_skip_string(self.attrs, i)]
        return ""

    def text(self):
        return visible_text(self.children)


def elements(path: pathlib.Path, tags, src=None):
    """Every <Tag …>…</Tag> (or self-closed) element of the given tags."""
    src = path.read_text() if src is None else src
    pat = re.compile(r"<(" + "|".join(map(re.escape, tags)) + r")(?=[\s>/])")
    for m in pat.finditer(src):
        tag = m.group(1)
        end, closed = _open_tag_end(src, m.end())
        attrs = src[m.end():end]
        children, stop = "", end
        if not closed:
            # children up to the matching close, counting same-tag nesting
            depth, j = 1, end
            same = re.compile(r"<(/?)" + re.escape(tag) + r"(?=[\s>/])")
            while depth and j < len(src):
                n = same.search(src, j)
                if not n:
                    break
                if n.group(1):
                    depth -= 1
                    if depth == 0:
                        children = src[end:n.start()]
                        stop = src.find(">", n.end()) + 1
                    j = n.end()
                else:
                    e, c = _open_tag_end(src, n.end())
                    if not c:
                        depth += 1
                    j = e
        yield Element(path, src.count("\n", 0, m.start()) + 1, tag, attrs,
                      children, m.start(), stop)


_LIT = r"(?:\"[^\"]*\"|'[^']*'|`[^`$]*`)"


def visible_text(children: str) -> str:
    """The literal words in JSX children: inner tags dropped, `{"…"}`
    string expressions kept, any other expression turned into a word
    placeholder (it may be a name; the caller cannot know)."""
    out, i = [], 0
    while i < len(children):
        c = children[i]
        if c == "{":
            j = _skip_braces(children, i)
            inner = children[i + 1:j - 1].strip()
            if inner.startswith("/*") or not inner:
                pass
            elif re.fullmatch(_LIT, inner):
                out.append(inner[1:-1])
            elif (m := re.fullmatch(r"[^\"'`?:{}]+\?\s*(" + _LIT + r")\s*:\s*("
                                    + _LIT + r")", inner, re.S)):
                # `cond ? "▾" : "▸"`: either branch is what shows
                out.append(m.group(1)[1:-1] + m.group(2)[1:-1])
            else:
                out.append(" \x00expr ")
            i = j
            continue
        if c == "<":
            e, _ = _open_tag_end(children, i + 1)
            i = e
            continue
        out.append(c)
        i += 1
    return re.sub(r"&\w+;", " ", "".join(out))


def glyph_only(text: str) -> bool:
    """True when what shows is one or more symbols and nothing a person
    could read as a word (no letters, digits or expression)."""
    t = text.strip()
    if not t or "\x00expr" in t:
        return False
    return not re.search(r"[A-Za-z0-9]", t)


# `{err && `, `{msg ? (`, `{!!notice && (`, `{res.error && ` … just before
# an element: the element shows what happened, so it is an announced line
# (a field of a result, `{res.note ? …}`, is a detail inside that line)
_MESSAGE = (r"(?:err|error|msg|notice|note|warning|warn|res|result|saved|done"
            r"|[a-z]+(?:Err|Error|Msg|Note|Notice))")
MESSAGE_GUARD = re.compile(r"\{\s*(?:!!)?\(?\s*(" + _MESSAGE + r")"
                           r"(?:\.(?:text|message|error))?\s*\)?"
                           r"\s*(?:&&|\?)\s*\(?\s*$")


def guarded_by_message(src: str, e: Element):
    """The message variable an element is shown under, or None."""
    m = MESSAGE_GUARD.search(src[max(0, e.start - 200):e.start])
    return m.group(1) if m else None
