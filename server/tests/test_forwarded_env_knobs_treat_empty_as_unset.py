"""No env knob may hang behaviour on a state a container cannot deliver.

compose declares every key it forwards, so a variable the operator left
commented out in their `.env` reaches the app as an EMPTY STRING — never as
an absent key. Code written as::

    raw = os.environ.get("OIKONOME_SOMETHING")
    if raw is None:
        ...the documented default...

therefore has a branch that is dead in every containerized install, which is
the standard deployment path. The setting looks obeyed, nothing raises, and
the app quietly does the other thing. Two knobs shipped exactly that way, and
each one read correctly on its own page — so the rule is checked mechanically
over the whole package rather than restated at each new call site (`envnum`
holds the same rule for numbers and flags).

Exempt: a key compose marks REQUIRED (``${NAME:?message}``). compose refuses
to start the stack when such a key is unset OR empty, so the code never sees
either state and `is None` there means "running outside a container", which
is a real distinction.

Also exempt: a comparison that is already paired with a truthiness test of
the same value (``v is not None and v.strip()``), which normalizes empty to
unset itself.
"""

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "server" / "oikonome"
COMPOSE = REPO / "docker" / "compose.yaml"


def _optional_forwarded() -> set[str]:
    """Keys compose materialises even when nobody set them."""
    out = set()
    for name, expr in re.findall(r"(OIKONOME_[A-Z0-9_]+):\s*(\$\{[^}]*\})",
                                 COMPOSE.read_text()):
        if ":?" not in expr:          # not marked required
            out.add(name)
    return out


def _env_name(node) -> str | None:
    """The literal name in `os.environ.get("NAME")` — no default argument.

    A call that passes a default is a different question: `get(name, "")`
    already treats missing and empty alike.
    """
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "get"):
        return None
    if not (isinstance(f.value, ast.Attribute) and f.value.attr == "environ"):
        return None
    if len(node.args) != 1 or node.keywords:
        return None
    arg = node.args[0]
    return arg.value if isinstance(arg, ast.Constant) \
        and isinstance(arg.value, str) else None


_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _scopes(tree):
    """Every function body, plus the module body, as its own scope."""
    yield tree
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n


def _in_scope(scope):
    """Nodes belonging to `scope` itself — a nested def is its own scope, so
    its body is not walked here. Without that, one function's local name is
    matched against another function's `os.environ.get`, and the check
    reports a line that never reads the environment at all."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _NESTED):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _guarded(scope, compare, var: str | None) -> bool:
    """True when the None check sits beside a truthiness test of the same
    value, which already collapses empty into unset."""
    if var is None:
        return False
    for node in _in_scope(scope):
        if not isinstance(node, ast.BoolOp) or compare not in node.values:
            continue
        for other in node.values:
            if other is compare:
                continue
            for sub in ast.walk(other):
                if isinstance(sub, ast.Name) and sub.id == var:
                    return True
    return False


def unset_branches() -> list[str]:
    """Every `is None` / `is not None` branch on a knob compose forwards
    without marking it required, as "path:line name"."""
    optional = _optional_forwarded()
    found: dict[tuple[str, int], str] = {}
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = path.relative_to(REPO)
        for scope in _scopes(tree):
            reads: dict[str, str] = {}
            for node in _in_scope(scope):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.targets[0], ast.Name):
                    name = _env_name(node.value)
                    if name:
                        reads[node.targets[0].id] = name
            for node in _in_scope(scope):
                if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                    continue
                if not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
                    continue
                rhs = node.comparators[0]
                if not (isinstance(rhs, ast.Constant) and rhs.value is None):
                    continue
                var = node.left.id if isinstance(node.left, ast.Name) else None
                name = reads.get(var) if var else _env_name(node.left)
                if not name or name not in optional:
                    continue
                if _guarded(scope, node, var):
                    continue
                found[(str(rel), node.lineno)] = name
    return [f"{p}:{ln} {n}" for (p, ln), n in sorted(found.items())]


class EmptyIsTheOnlyUnsetAContainerHasTests(unittest.TestCase):
    def test_no_forwarded_knob_branches_on_being_absent(self):
        offenders = unset_branches()
        self.assertEqual(
            offenders, [],
            "these read an env knob compose forwards and then branch on it "
            "being ABSENT — a state a container never produces, so the "
            "branch is dead wherever the app actually runs. Treat empty as "
            "unset and give the other meaning a word of its own: "
            + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
