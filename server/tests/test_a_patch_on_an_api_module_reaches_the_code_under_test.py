"""A test that patches a name on an API module patches it where the code it
exercises looks the name up.

`mock.patch("oikonome.web.api._conn")` replaces `_conn` in api.py's globals
and nowhere else. A handler that lives in another module (the API is split
into web/api_<area>.py, specs/api-routers.md) reads its own module's
`_conn`, so the patch reaches nothing: the race, the clock or the spy the
test meant to install never happens, and the test passes without testing
anything — a race test whose handlers moved would go on passing with no
race injected.

So for every patch a test puts on an API module (by dotted name, or with
`mock.patch.object` on a name bound to the module), this checks two things:

* the patched module's own code reads the name (a re-export alone is not a
  read: nothing in that module would see the patch);
* the test exercises code in that module — one of the API routes it
  requests, or one of the module's functions it calls directly, is defined
  there, or late-imports the name from it. The routes and calls are looked
  for in the function holding the patch, then its class, then its file, so
  a patch installed in setUp or in a helper is matched against the tests
  that use it.

A patch whose name is not a literal (a loop over knob names) is resolved
when the loop runs over a literal tuple, and otherwise reported.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import re
import unittest
import warnings

from .util import all_routes, api_module_names

TESTS = pathlib.Path(__file__).resolve().parent


def _parse(path: pathlib.Path):
    # another test file's own escape-sequence warnings are not ours to show
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(path.read_text())


def _module_aliases(tree) -> dict:
    """local name -> dotted API module, for every way a test imports one."""
    mods = api_module_names()
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "oikonome.web":
            for a in node.names:
                if f"oikonome.web.{a.name}" in mods:
                    out[a.asname or a.name] = f"oikonome.web.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in mods and a.asname:
                    out[a.asname] = a.name
    return out


def _literal_loop_values(func, name) -> list | None:
    """The literal values `name` takes in `for name, ... in (literal tuple)`."""
    for node in ast.walk(func):
        if not isinstance(node, ast.For):
            continue
        tgt, it = node.target, node.iter
        if not isinstance(it, (ast.Tuple, ast.List)):
            continue
        idx = None
        if isinstance(tgt, ast.Name) and tgt.id == name:
            idx = -1
        elif isinstance(tgt, ast.Tuple):
            for k, e in enumerate(tgt.elts):
                if isinstance(e, ast.Name) and e.id == name:
                    idx = k
        if idx is None:
            continue
        vals = []
        for e in it.elts:
            v = e if idx == -1 else (e.elts[idx] if isinstance(e, ast.Tuple) else None)
            if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
                return None
            vals.append(v.value)
        return vals
    return None


def patches_in(tree):
    """Yield (module, name, scopes, lineno) for every patch on an API
    module; `scopes` is [function, class, file] holding the patch,
    innermost first. `name` is None when it cannot be read statically."""
    mods = api_module_names()
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def scopes(node):
        out, cur = [], node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                out.append(cur)
        return out + [tree]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if fname == "patch" and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            mod, _, name = node.args[0].value.rpartition(".")
            if mod in mods:
                yield mod, name, scopes(node), node.lineno
        elif fname == "object" and isinstance(f, ast.Attribute) \
                and getattr(f.value, "attr", getattr(f.value, "id", "")) == "patch" \
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Name):
            sc = scopes(node)
            # the binding the patch sees: the innermost scope that imports
            # the name wins over one elsewhere in the file
            mod = next((m for m in (_module_aliases(x).get(node.args[0].id)
                                    for x in sc) if m), None)
            if mod is None:
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield mod, arg.value, sc, node.lineno
            elif isinstance(arg, ast.Name):
                vals = _literal_loop_values(sc[0], arg.id)
                for v in vals or [None]:
                    yield mod, v, sc, node.lineno
            else:
                yield mod, None, sc, node.lineno


def _strings(node):
    for x in ast.walk(node):
        if isinstance(x, ast.Constant) and isinstance(x.value, str):
            yield x.value
        elif isinstance(x, ast.JoinedStr):
            yield "".join(v.value if isinstance(v, ast.Constant) else "x"
                          for v in x.values)


def _reads(module_name: str) -> set:
    """Names the module's own code reads (Load), re-export imports aside."""
    mod = importlib.import_module(module_name)
    tree = _parse(pathlib.Path(mod.__file__))
    return {x.id for x in ast.walk(tree)
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}


class PatchesReachTheirCodeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from oikonome.web.app import app
        cls.routes = [r for r in all_routes(app) if r.path.startswith("/api")]

    def _exercised(self, scope, aliases, module_name) -> list:
        """Handlers and functions of `module_name` this scope exercises:
        routes it requests, direct calls through an API module alias, and
        handlers that late-import from the module."""
        hits = []
        for s in _strings(scope):
            if not s.startswith("/api/"):
                continue
            path = s.split("?")[0]
            for r in self.routes:
                if r.path_regex.match(path):
                    src_mod = r.endpoint.__module__
                    if src_mod == module_name:
                        hits.append(f"route {r.path}")
                    else:
                        try:
                            body = inspect.getsource(r.endpoint)
                        except (OSError, TypeError):
                            body = ""
                        short = module_name.rsplit(".", 1)[1]
                        if re.search(rf"from \.{short} import", body):
                            hits.append(f"route {r.path} (late import)")
        for x in ast.walk(scope):
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) \
                    and isinstance(x.func.value, ast.Name) \
                    and x.func.value.id in aliases:
                mod = importlib.import_module(aliases[x.func.value.id])
                fn = getattr(mod, x.func.attr, None)
                if getattr(fn, "__module__", None) == module_name:
                    hits.append(f"call {x.func.attr}")
        return hits

    def check(self, files) -> list:
        problems = []
        for path in files:
            tree = _parse(path)
            aliases = _module_aliases(tree)
            for mod, name, scopes, line in patches_in(tree):
                where = f"{path.name}:{line}"
                if name is None:
                    problems.append(f"{where}: patch on {mod} with a name "
                                    "this check cannot read")
                    continue
                if name not in _reads(mod):
                    problems.append(f"{where}: {mod}.{name} is patched but "
                                    f"{mod} never reads it")
                    continue
                if not any(self._exercised(sc, aliases, mod) for sc in scopes):
                    problems.append(f"{where}: {mod}.{name} is patched but "
                                    "nothing the test exercises lives in "
                                    f"{mod}")
        return problems

    def test_every_patch_on_an_api_module_reaches_the_code_under_test(self):
        files = sorted(TESTS.glob("test_*.py"))
        self.assertEqual(self.check(files), [])

    def test_the_check_finds_the_patches_it_reads(self):
        """A reader that found no patches would pass vacuously."""
        n = sum(1 for p in sorted(TESTS.glob("test_*.py"))
                for _ in patches_in(_parse(p)))
        self.assertGreater(n, 10)


if __name__ == "__main__":
    unittest.main()
