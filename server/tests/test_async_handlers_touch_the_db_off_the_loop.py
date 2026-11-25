"""Every `async def` route handler in the web package does its database
work, its step-up proof and its advisory locks in the threadpool, never on
the event loop.

An async handler runs ON the loop: a pooled checkout, a query, an
advisory lock wait or an argon2 proof there stalls every request the
worker is serving — two operators creating the same address at once hold
the whole worker while one waits on the other's lock. The handler may read
the request body and files (that is why it is async); everything that
touches the database goes into a function it hands to `run_in_threadpool`
(or `asyncio.to_thread`). The push relay's app is scanned beside the web
package, and so are the background worker's job entry points: arq runs
each registered `async def` on the worker's one loop, so a query there
stalls every other job in flight. Those are registered by name, not by a
decorator, so every top-level `async def` in a job module is read as one.

The scan is static and follows the call graph across the whole
`oikonome` package: a call the handler makes on the loop is resolved
through the module's imports (`from .x import f`, `from ..pkg import mod`
then `mod.f(...)`, the same module's own helpers, imports made inside the
function) to the function it names, and that function's on-loop calls are
followed the same way, up to `MAX_DEPTH` hops. A call through the policy
gate (`ext.gate.<method>(…)`, `from ..ext import gate` then
`gate.<method>(…)`) is a call on whichever Gate the instance runs: it is
followed into `oikonome.ext.Gate.<method>` and, when the index is handed
an add-on's root, into the override of every Gate subclass that add-on
defines, and it is on-loop work when ANY of them is. Any other call that
cannot be resolved (a method, a third-party library, a name bound at run
time) is not followed. Anything handed to the threadpool is not followed
either."""
import ast
import pathlib
import textwrap
import unittest

PKG = pathlib.Path(__file__).resolve().parents[1] / "oikonome"
WEB = PKG / "web"
# the push relay is its own FastAPI app; its handlers are held to the
# same rule
RELAY = PKG / "relay" / "app.py"
MAX_DEPTH = 4


def _registered_jobs():
    """{(module, function name)} of every coroutine the worker registers:
    its `functions` and its `cron_jobs`, the installed add-ons' included
    (they are appended to both lists). Read from the real settings, so a
    job added there is scanned without anyone editing this file."""
    from oikonome.jobs import worker
    out = set()
    for f in (list(worker.WorkerSettings.functions)
              + list(worker.WorkerSettings.cron_jobs)):
        c = getattr(f, "coroutine", f)
        c = getattr(c, "__wrapped__", c)
        out.add((c.__module__, c.__name__))
    return out


def _job_modules():
    """The modules of this package that hold a registered job; every
    top-level async def in them is read as an entry point (or an enqueue
    helper a handler awaits). An add-on's jobs live in its own package and
    are held to the rule by that package's copy of this guard."""
    paths = set()
    for mod, _ in _registered_jobs():
        parts = mod.split(".")
        if parts[0] != "oikonome":
            continue
        base = PKG.joinpath(*parts[1:])
        for p in (base.with_suffix(".py"), base / "__init__.py"):
            if p.is_file():
                paths.add(p)
                break
        else:
            raise AssertionError(f"registered job module {mod} has no file "
                                 "under the package")
    return sorted(paths)


JOBS = _job_modules()


def _scanned():
    return sorted(WEB.glob("*.py")) + [RELAY]


def _all_scanned():
    """(path, every_async) for every module the guard reads."""
    return [(p, False) for p in _scanned()] + [(p, True) for p in JOBS]


# bare-name calls and attribute calls that touch the database or prove
FORBIDDEN_NAMES = {"_conn", "tenant_connect", "admin_connect",
                   "control_connect", "_require_elevation"}
FORBIDDEN_ATTRS = {"execute", "tenant_connect", "admin_connect",
                   "control_connect", "_require_elevation", "_conn"}
# hand-off calls whose callable runs on another thread: the threadpool
# helpers (callable first), `loop.run_in_executor(executor, callable)`
# (callable second) and `anyio.to_thread.run_sync(callable)`
OFF_LOOP = {"run_in_threadpool", "to_thread", "run_in_executor", "run_sync"}
# the keyword a hand-off may name its callable by
OFF_LOOP_KW = {"func"}

# blocking network clients the loop must not call: a sync HTTP request, a
# mail session, a URL fetch, a sync Redis client (each one waits on a
# socket with the whole worker held). Keyed by the library module; the
# value is the attribute names that block (None = every call into it).
# `httpx.AsyncClient` and `redis.asyncio` are the loop-safe forms and are
# not listed.
SYNC_NET = {
    "httpx": {"get", "post", "put", "patch", "delete", "head", "options",
              "request", "stream", "Client"},
    "requests": None,
    "smtplib": None,
    "urllib.request": None,
    "redis": {"from_url", "Redis", "StrictRedis"},
}

# handler -> the reason it is allowed to look like on-loop work. Empty is
# the goal; an entry needs a one-line reason the scan cannot be made to see.
ALLOWED = {}


def _name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _dotted(func):
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return ".".join(reversed(parts))
    return ""


def _handed_args(call):
    """The argument nodes of a hand-off call that name the callable it
    runs on another thread (a `partial(...)` unwrapped to its target)."""
    if call.args and _name(call.func) == "run_in_executor":
        cands = call.args[1:2]
    else:
        cands = call.args[:1]
    cands = list(cands) + [k.value for k in call.keywords
                           if k.arg in OFF_LOOP_KW]
    out = []
    for a in cands:
        if (isinstance(a, ast.Call) and _name(a.func) == "partial"
                and a.args):
            a = a.args[0]
        out.append(a)
    return out


def _handed_off(fn):
    """(names of nested functions that run only in the threadpool, ids of
    lambda nodes handed to it).

    The exemption is per reference, not per name: a nested function counts
    as off the loop only when EVERY use of its name in `fn` is a hand-off
    argument. Called directly on the loop as well, or passed anywhere else,
    its body is read as on-loop work."""
    names, lambdas, handed_refs = set(), set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _name(n.func) in OFF_LOOP:
            for a in _handed_args(n):
                if isinstance(a, ast.Name):
                    names.add(a.id)
                    handed_refs.add(id(a))
                elif isinstance(a, ast.Lambda):
                    lambdas.add(id(a))
    for n in ast.walk(fn):
        if (isinstance(n, ast.Name) and n.id in names
                and id(n) not in handed_refs):
            names.discard(n.id)
    return names, lambdas


def _on_loop_nodes(fn):
    """Every node of `fn` that runs on the caller's thread: the bodies of
    nested functions and lambdas handed to the threadpool are skipped."""
    names, lambdas = _handed_off(fn)

    def walk(node):
        for c in ast.iter_child_nodes(node):
            if id(c) in lambdas:
                continue
            if (isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and c.name in names):
                continue
            yield c
            yield from walk(c)
    return list(walk(fn))


def _handed_off_calls(fn):
    """The callables `fn` hands to the threadpool, as call-shaped nodes
    (the callable argument of each hand-off call)."""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _name(n.func) in OFF_LOOP:
            for a in _handed_args(n):
                if isinstance(a, (ast.Name, ast.Attribute)):
                    out.append(a)
    return out


def _net_aliases(nodes):
    """{local name: library dotted name} for the SYNC_NET libraries the
    import statements in `nodes` bind (`import redis as _r` -> {"_r":
    "redis"}; `from urllib.request import urlopen` -> {"urlopen":
    "urllib.request.urlopen"})."""
    out = {}
    for n in nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    out[top] = top
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            for a in n.names:
                out[a.asname or a.name] = f"{n.module}.{a.name}"
    return {k: v for k, v in out.items()
            if any(v == lib or v.startswith(lib + ".")
                   or lib.startswith(v + ".") for lib in SYNC_NET)}


def _module_net_aliases(tree):
    return _net_aliases([n for n in tree.body
                         if isinstance(n, (ast.Import, ast.ImportFrom))])


def _sync_net(full):
    """True when the dotted library name `full` is a blocking call."""
    for lib, attrs in SYNC_NET.items():
        if not full.startswith(lib + "."):
            continue
        rest = full[len(lib) + 1:].split(".")
        if attrs is None or rest[0] in attrs:
            return True
    return False


def _direct_hits(fn, module_aliases=None):
    hits = []
    aliases = dict(module_aliases or {})
    aliases.update(_net_aliases([n for n in ast.walk(fn)
                                 if isinstance(n, (ast.Import,
                                                   ast.ImportFrom))]))
    for c in _on_loop_nodes(fn):
        if isinstance(c, ast.Call):
            nm, dotted = _name(c.func), _dotted(c.func)
            head, _, tail = dotted.partition(".")
            full = (f"{aliases[head]}.{tail}" if tail else aliases[head]) \
                if head in aliases else ""
            if ((isinstance(c.func, ast.Name) and nm in FORBIDDEN_NAMES)
                    or (isinstance(c.func, ast.Attribute)
                        and nm in FORBIDDEN_ATTRS)
                    or dotted.endswith("_activity.record")
                    or (full and _sync_net(full))):
                hits.append(f"{dotted or nm}() at line {c.lineno}")
        elif (isinstance(c, ast.Constant) and isinstance(c.value, str)
              and "pg_advisory" in c.value):
            hits.append(f"pg_advisory at line {c.lineno}")
    return hits


# the policy gate: `ext.gate` is a proxy over the installed Gate, so a call
# through it is a call on the base class's method or an add-on's override
GATE_MODULE, GATE_ATTR, GATE_CLASS = "oikonome.ext", "gate", "Gate"


class Index:
    """Lazily parsed modules of the `oikonome` package (and of any other
    package handed in by `roots` — {top-level name: its directory}, or a
    list of package directories, each named by its own directory name —
    which an add-on's guard uses), plus any synthetic modules handed in by
    name (the self-test). A Gate subclass defined under an extra root is
    one the instance may run, so a gate call reaches its overrides."""

    def __init__(self, sources=None, roots=None):
        self._sources = dict(sources or {})
        if isinstance(roots, (list, tuple)):
            roots = {pathlib.Path(r).name: pathlib.Path(r) for r in roots}
        self._roots = {"oikonome": PKG, **(roots or {})}
        self._mods = {}
        self._gates = None

    def _path(self, mod):
        parts = mod.split(".")
        if parts[0] not in self._roots:
            return None
        base = self._roots[parts[0]].joinpath(*parts[1:])
        for p in (base.with_suffix(".py"), base / "__init__.py"):
            if p.is_file():
                return p
        return None

    def is_module(self, mod):
        return mod in self._sources or self._path(mod) is not None

    def is_package(self, mod):
        if mod in self._sources:
            return False
        p = self._path(mod)
        return p is not None and p.name == "__init__.py"

    def module(self, mod):
        """(tree, {top-level function name: node}, module-level imports)
        or None."""
        if mod in self._mods:
            return self._mods[mod]
        src = self._sources.get(mod)
        if src is None:
            p = self._path(mod)
            src = p.read_text() if p else None
        got = None
        if src is not None:
            tree = ast.parse(src)
            funcs = {n.name: n for n in tree.body
                     if isinstance(n, (ast.FunctionDef,
                                       ast.AsyncFunctionDef))}
            # a top-level class's methods, as "Class.method" (never a
            # name a call spells, so only the gate's resolution finds them)
            for c in tree.body:
                if isinstance(c, ast.ClassDef):
                    for n in c.body:
                        if isinstance(n, (ast.FunctionDef,
                                          ast.AsyncFunctionDef)):
                            funcs[f"{c.name}.{n.name}"] = n
            imports = self._imports(mod, [n for n in tree.body
                                          if isinstance(n, (ast.Import,
                                                            ast.ImportFrom))])
            got = (tree, funcs, imports)
        self._mods[mod] = got
        return got

    def _imports(self, mod, nodes):
        """{local alias: ("mod", dotted) | ("func", module, name)}."""
        out = {}
        pkg = mod if self.is_package(mod) else mod.rpartition(".")[0]
        for n in nodes:
            if isinstance(n, ast.Import):
                for a in n.names:
                    top = a.name.split(".")[0]
                    if top not in self._roots:
                        continue
                    if a.asname:
                        out[a.asname] = ("mod", a.name)
                    else:
                        out[top] = ("mod", top)
                continue
            if n.level:
                base = pkg
                for _ in range(n.level - 1):
                    base = base.rpartition(".")[0]
                src = f"{base}.{n.module}" if n.module else base
            else:
                src = n.module or ""
            if src.split(".")[0] not in self._roots:
                continue
            for a in n.names:
                local = a.asname or a.name
                sub = f"{src}.{a.name}"
                if self.is_module(sub):
                    out[local] = ("mod", sub)
                else:
                    out[local] = ("func", src, a.name)
        return out

    def scope_imports(self, mod, fn):
        """Module-level imports overlaid with those made inside `fn`."""
        m = self.module(mod)
        out = dict(m[2]) if m else {}
        out.update(self._imports(mod, [n for n in ast.walk(fn)
                                       if isinstance(n, (ast.Import,
                                                         ast.ImportFrom))]))
        return out

    def resolve(self, mod, fn, func, imports=None):
        """(module, function name) the call target `func` names, or None."""
        imports = imports if imports is not None else \
            self.scope_imports(mod, fn)
        m = self.module(mod)
        if isinstance(func, ast.Name):
            if func.id in imports:
                b = imports[func.id]
                return self._func(b[1], b[2]) if b[0] == "func" else None
            if m and func.id in m[1]:
                return (mod, func.id)
            return None
        dotted = _dotted(func)
        if not dotted:
            return None
        head, *rest = dotted.split(".")
        b = imports.get(head)
        if not b:
            return None
        if b[0] == "mod":
            target = b[1]
        else:
            target = f"{b[1]}.{b[2]}"
            if not self.is_module(target):
                return None
        *path, name = rest
        for p in path:
            target = f"{target}.{p}"
        if not self.is_module(target):
            return None
        return self._func(target, name)

    def resolve_all(self, mod, fn, func, imports=None):
        """Every (module, function) the call target `func` may run: the
        gate's candidates for a gate call, else `resolve`'s one answer."""
        imports = imports if imports is not None else \
            self.scope_imports(mod, fn)
        method = self._gate_method(func, imports)
        if method is not None:
            return self.gate_methods(method)
        t = self.resolve(mod, fn, func, imports)
        return [t] if t else []

    def _gate_method(self, func, imports):
        """The method name when `func` is `<ext>.gate.<method>` or
        `<gate>.<method>` with the names bound to oikonome.ext and its
        `gate`; None for anything else."""
        parts = _dotted(func).split(".")
        if len(parts) < 2:
            return None
        b = imports.get(parts[0])
        if b == ("func", GATE_MODULE, GATE_ATTR) and len(parts) == 2:
            return parts[1]
        if b and b[0] == "mod":
            target, rest = b[1], parts[1:]
            while len(rest) > 2 and self.is_module(f"{target}.{rest[0]}"):
                target, rest = f"{target}.{rest[0]}", rest[1:]
            if target == GATE_MODULE and len(rest) == 2 \
                    and rest[0] == GATE_ATTR:
                return rest[1]
        return None

    def gate_classes(self):
        """[(module, class name)] of every Gate the instance may run: the
        product's base class, and each subclass (direct or not) defined
        anywhere under an extra root."""
        if self._gates is None:
            known = [(GATE_MODULE, GATE_CLASS)]
            classes = []
            for top, root in self._roots.items():
                if top == "oikonome":
                    continue
                for p in sorted(root.rglob("*.py")):
                    rel = p.relative_to(root).with_suffix("")
                    parts = [top, *rel.parts]
                    if parts[-1] == "__init__":
                        parts = parts[:-1]
                    mod = ".".join(parts)
                    m = self.module(mod)
                    if not m:
                        continue
                    for c in m[0].body:
                        if isinstance(c, ast.ClassDef):
                            classes.append((mod, c, m[2]))
            grew = True
            while grew:
                grew = False
                for mod, c, imports in classes:
                    if (mod, c.name) in known:
                        continue
                    if any(self._class_ref(mod, b, imports) in known
                           for b in c.bases):
                        known.append((mod, c.name))
                        grew = True
            self._gates = known
        return self._gates

    def _class_ref(self, mod, node, imports):
        """(module, class name) a base-class expression names, or None."""
        if isinstance(node, ast.Name):
            b = imports.get(node.id)
            if b and b[0] == "func":
                return (b[1], b[2])
            m = self.module(mod)
            if m and any(isinstance(c, ast.ClassDef) and c.name == node.id
                         for c in m[0].body):
                return (mod, node.id)
            return None
        parts = _dotted(node).split(".")
        b = imports.get(parts[0]) if len(parts) >= 2 else None
        if b and b[0] == "mod":
            return (".".join([b[1], *parts[1:-1]]), parts[-1])
        return None

    def gate_methods(self, method):
        """(module, "Class.method") of `method` on every Gate the instance
        may run, each class's own definition or the one it inherits."""
        out = []
        for mod, cls in self.gate_classes():
            got = self._method(mod, cls, method)
            if got and got not in out:
                out.append(got)
        return out

    def _method(self, mod, cls, method, hops=0):
        m = self.module(mod)
        if not m or hops > 8:
            return None
        if f"{cls}.{method}" in m[1]:
            return (mod, f"{cls}.{method}")
        for c in m[0].body:
            if isinstance(c, ast.ClassDef) and c.name == cls:
                for b in c.bases:
                    ref = self._class_ref(mod, b, m[2])
                    if ref:
                        got = self._method(ref[0], ref[1], method, hops + 1)
                        if got:
                            return got
        return None

    def _func(self, mod, name, hops=0):
        m = self.module(mod)
        if not m:
            return None
        if name in m[1]:
            return (mod, name)
        # a package that re-exports the name from a submodule
        b = m[2].get(name)
        if b and b[0] == "func" and hops < 3:
            return self._func(b[1], b[2], hops + 1)
        return None


class Scan:
    def __init__(self, index):
        self.ix = index
        self._callees = {}
        self._hits = {}

    def _node(self, key):
        m = self.ix.module(key[0])
        return m[1][key[1]] if m else None

    def hits(self, key):
        if key not in self._hits:
            m = self.ix.module(key[0])
            self._hits[key] = _direct_hits(
                self._node(key), _module_net_aliases(m[0]) if m else None)
        return self._hits[key]

    def callees(self, mod, fn):
        """The resolved functions `fn` calls on the loop."""
        key = (mod, id(fn))
        if key not in self._callees:
            imports = self.ix.scope_imports(mod, fn)
            out = []
            for c in _on_loop_nodes(fn):
                if (isinstance(c, ast.Call)
                        and _name(c.func) not in OFF_LOOP):
                    for t in self.ix.resolve_all(mod, fn, c.func, imports):
                        if t not in out:
                            out.append(t)
            self._callees[key] = out
        return self._callees[key]

    def handed_off(self, mod, fn):
        """The resolved functions `fn` hands to the threadpool."""
        imports = self.ix.scope_imports(mod, fn)
        out = set()
        for a in _handed_off_calls(fn):
            out.update(self.ix.resolve_all(mod, fn, a, imports))
        return out

    def reach(self, mod, fn, depth=MAX_DEPTH):
        """{(module, function): call path} of everything `fn` reaches on
        the loop within `depth` hops; breadth-first, so each function is
        reported by its shortest path and a cycle ends."""
        seen = {}
        frontier = [(t, (t,)) for t in self.callees(mod, fn)]
        for _ in range(depth):
            nxt = []
            for t, path in frontier:
                if t in seen:
                    continue
                seen[t] = path
                node = self._node(t)
                if node is not None:
                    nxt += [(c, path + (c,)) for c in self.callees(t[0], node)]
            frontier = nxt
        return seen

    def handler_reasons(self, mod, fn):
        m = self.ix.module(mod)
        reasons = list(_direct_hits(
            fn, _module_net_aliases(m[0]) if m else None))
        for t, path in sorted(self.reach(mod, fn).items()):
            h = self.hits(t)
            if h:
                via = " -> ".join(f"{m.rpartition('.')[2]}.{n}"
                                  for m, n in path)
                reasons.append(f"{via}: {h[0]}")
        return reasons


def _modname(path, base=PKG.parent):
    rel = path.relative_to(base).with_suffix("")
    return ".".join(rel.parts)


def scan_module(source, modname="oikonome.web._synthetic", index=None,
                every_async=False):
    """{handler name: [reasons]} for every decorated async def in
    `source` (every top-level async def with `every_async`, for job
    modules whose entry points are registered by name) that touches the
    database on the loop."""
    ix = index or Index({modname: source})
    if modname not in ix._sources:
        ix._sources[modname] = source
    sc = Scan(ix)
    out = {}
    for n in ast.parse(source).body:
        if not (isinstance(n, ast.AsyncFunctionDef)
                and (n.decorator_list or every_async)):
            continue
        reasons = sc.handler_reasons(modname, n)
        if reasons:
            out[n.name] = reasons
    return out


def _handlers(source):
    return {n.name for n in ast.parse(source).body
            if isinstance(n, ast.AsyncFunctionDef) and n.decorator_list}


def _handler_node(path, name):
    for n in ast.parse(path.read_text()).body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{path.name}:{name} not found")


class AsyncHandlersTouchTheDbOffTheLoop(unittest.TestCase):
    def test_no_async_route_handler_touches_the_db_on_the_loop(self):
        ix = Index()
        found = {}
        for path, every_async in _all_scanned():
            mod = _modname(path)
            for name, reasons in scan_module(
                    path.read_text(), mod, ix, every_async).items():
                if f"{path.name}:{name}" not in ALLOWED:
                    found[f"{path.name}:{name}"] = reasons
        self.assertEqual(found, {}, "async handlers touching the database "
                         "on the event loop — hand that work to "
                         "run_in_threadpool")

    def test_the_scan_reaches_the_known_async_doors(self):
        # a scan that silently finds no handlers would pass vacuously
        seen = set()
        for path in _scanned():
            seen |= {f"{path.name}:{h}" for h in _handlers(path.read_text())}
        from .util import api_file_of
        api_doors = tuple(f"{api_file_of(n)}:{n}" for n in (
            "receipt_upload", "receipt_snap", "import_api"))
        for door in (*api_doors, "pages.py:import_submit",
                     "adminconsole.py:create_instance",
                     "adminconsole.py:invite", "app.py:unsubscribe_act",
                     "app.py:act_do", "postmark_webhook.py:email_webhook"):
            self.assertIn(door, seen)
        self.assertTrue(RELAY.is_file())
        # the relay's one async def is its lifespan, which the scan reads
        self.assertIn("_lifespan", _handlers(RELAY.read_text()))
        jobs = set()
        for path in JOBS:
            jobs |= {f"{path.name}:{n.name}"
                     for n in ast.parse(path.read_text()).body
                     if isinstance(n, ast.AsyncFunctionDef)}
        for door in ("worker.py:sync_all", "worker.py:nightly_all",
                     "worker.py:sync_item", "worker.py:plaid_link_finish",
                     "bulk_import_job.py:bulk_import_run",
                     "bulk_import_job.py:bulk_import_sweep"):
            self.assertIn(door, jobs)
        for door in ALLOWED:
            self.assertIn(door, seen | jobs, "a stale allow-list entry")

    def test_the_call_graph_resolves_the_mail_doors_across_modules(self):
        # the mail doors open their connection in another module; a scan
        # that resolved nothing across modules would pass them vacuously.
        # Each handler hands the work to the threadpool, so it is reached
        # as a handed-off callable, and the target itself must read as
        # touching the database — on the loop, it would be flagged.
        sc = Scan(Index())
        app = WEB / "app.py"
        cases = [
            (app, "unsubscribe_act", ("oikonome.notify.unsubscribe",
                                      "apply")),
            (app, "act_do", ("oikonome.notify.mailact", "apply")),
        ]
        for path, handler, target in cases:
            fn = _handler_node(path, handler)
            self.assertIn(target, sc.handed_off(_modname(path), fn), handler)
            self.assertTrue(sc.hits(target) or any(
                sc.hits(t) for t in sc.reach(target[0], sc._node(target))),
                target)
        # the webhook hands its own _handle over; the failure record that
        # opens a connection is two hops further, in notify/delivery
        pm = WEB / "postmark_webhook.py"
        fn = _handler_node(pm, "email_webhook")
        handle = ("oikonome.web.postmark_webhook", "_handle")
        self.assertIn(handle, sc.handed_off(_modname(pm), fn))
        reached = sc.reach(handle[0], sc._node(handle))
        target = ("oikonome.notify.delivery", "record_failure")
        self.assertIn(target, reached)
        self.assertTrue(sc.hits(target) or any(
            sc.hits(t) for t in sc.reach(target[0], sc._node(target))))

    def test_the_scan_catches_each_shape_of_on_loop_work(self):
        src = textwrap.dedent('''
            def _helper():
                return _inner()
            def _inner():
                return tenancy.admin_connect()
            def _off():
                return _conn(None)
            @router.post("/a")
            async def direct(request):
                conn = _conn(request)
            @router.post("/b")
            async def via_helper(request):
                return _helper()
            @router.post("/c")
            async def lock(request, admin):
                admin.execute("SELECT pg_advisory_xact_lock(1)")
            @router.post("/d")
            async def proof(request, user):
                _require_elevation(request, user)
            @router.post("/e")
            async def log(request, conn):
                _activity.record(conn, "x")
            @router.post("/ok")
            async def fine(request):
                body = await request.body()
                def _work():
                    return _conn(request).execute("SELECT 1")
                await run_in_threadpool(_work)
                await run_in_threadpool(lambda: _require_elevation(1, 2))
                return await run_in_threadpool(_off)
        ''')
        self.assertEqual(set(scan_module(src)),
                         {"direct", "via_helper", "lock", "proof", "log"})

    def test_the_scan_follows_calls_into_other_modules(self):
        ix = Index({
            "oikonome.notify._syn_a": textwrap.dedent('''
                from ..db import tenancy
                from . import _syn_b
                def apply(t):
                    return _syn_b.write(t)
                def pure(t):
                    return t
                def shallow(t):
                    return _syn_b._db(t)
            '''),
            "oikonome.notify._syn_b": textwrap.dedent('''
                from . import _syn_a
                def write(t):
                    _syn_a.apply(t)      # a cycle ends, it does not hang
                    return _deep1(t)
                def _deep1(t):
                    return _deep2(t)
                def _deep2(t):
                    return _deep3(t)
                def _deep3(t):
                    return _db(t)
                def _db(t):
                    return _conn(t)
            '''),
        })
        src = textwrap.dedent('''
            from ..notify import _syn_a
            from ..notify._syn_a import apply as _apply, pure
            from ..notify._syn_b import _deep3
            @router.post("/a")
            async def via_module(request):
                return _syn_a.apply(1)
            @router.post("/b")
            async def via_name(request):
                from ..notify import _syn_b
                return _syn_b._deep3(1)
            @router.post("/a2")
            async def via_module_two_hops(request):
                return _syn_a.shallow(1)
            @router.post("/a3")
            async def via_from_import(request):
                return _deep3(1)
            @router.post("/c")
            async def too_deep(request):
                return _apply(1)
            @router.post("/ok")
            async def handed(request):
                pure(1)
                return await run_in_threadpool(_syn_a.apply, 1)
        ''')
        got = scan_module(src, "oikonome.web._synthetic", ix)
        # apply -> write -> _deep1 -> _deep2 -> _deep3 -> _db is six hops:
        # past MAX_DEPTH, so neither route through apply is flagged
        self.assertEqual(set(got), {"via_name", "via_module_two_hops",
                                    "via_from_import"})


    def test_a_hand_off_exempts_only_the_handed_call_not_the_name(self):
        # the nested function's body is off the loop only where it is
        # handed over; a direct call of the same function on the loop runs
        # that body on the loop and is flagged
        src = textwrap.dedent('''
            @router.post("/a")
            async def also_called_directly(request):
                def _work():
                    return _conn(request)
                await run_in_threadpool(_work)
                return _work()
            @router.post("/b")
            async def via_partial_then_direct(request):
                def _work(x):
                    return _conn(x)
                await run_in_threadpool(partial(_work, request))
                _work(request)
            @router.post("/c")
            async def referenced_elsewhere(request):
                def _work():
                    return _conn(request)
                await run_in_threadpool(_work)
                callbacks.append(_work)
            @router.post("/ok")
            async def only_handed(request):
                def _work():
                    return _conn(request)
                await run_in_threadpool(_work)
                return await asyncio.to_thread(partial(_work))
        ''')
        self.assertEqual(set(scan_module(src)),
                         {"also_called_directly", "via_partial_then_direct",
                          "referenced_elsewhere"})

    def test_the_scan_follows_awaited_undecorated_async_helpers(self):
        ix = Index({"oikonome.notify._syn_c": textwrap.dedent('''
            async def remote(t):
                return _conn(t)
        ''')})
        src = textwrap.dedent('''
            from ..notify._syn_c import remote
            async def _local(t):
                return tenant_connect(t)
            async def _off(t):
                return await run_in_threadpool(_conn, t)
            @router.post("/a")
            async def awaits_local(request):
                return await _local(request)
            @router.post("/b")
            async def awaits_remote(request):
                return await remote(request)
            @router.post("/ok")
            async def awaits_clean(request):
                return await _off(request)
        ''')
        self.assertEqual(set(scan_module(src, "oikonome.web._synthetic", ix)),
                         {"awaits_local", "awaits_remote"})

    def test_job_entry_points_are_scanned_without_a_decorator(self):
        # arq runs a job's async def on the worker's one loop, registered
        # by name rather than by a decorator
        src = textwrap.dedent('''
            async def job(ctx):
                return _conn(None)
            async def clean(ctx):
                return await asyncio.to_thread(_conn, None)
        ''')
        self.assertEqual(set(scan_module(src)), set())
        self.assertEqual(set(scan_module(src, every_async=True)), {"job"})

    def test_registered_jobs_are_all_in_scanned_modules(self):
        # the job modules are derived from what the worker registers; a
        # job whose coroutine is not a top-level async def of a scanned
        # module (a re-export, a wrapper) would escape the scan
        scanned = {_modname(p): p for p in JOBS}
        for mod, name in sorted(_registered_jobs()):
            if not mod.startswith("oikonome."):
                continue
            self.assertIn(mod, scanned, f"job {name}: module {mod} is "
                          "registered with the worker but not scanned")
            tops = {n.name for n in ast.parse(scanned[mod].read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef)}
            self.assertIn(name, tops, f"{mod}.{name} is registered but is "
                          "not a top-level async def the scan reads")
        for door in ("worker.py", "bulk_import_job.py"):
            self.assertIn(door, {p.name for p in JOBS})

    def test_sync_network_calls_on_the_loop_are_flagged(self):
        ix = Index({"oikonome.notify._syn_net": textwrap.dedent('''
            import redis as _r
            def client():
                return _r.from_url("redis://x")
        ''')})
        src = textwrap.dedent('''
            import httpx
            import requests
            import smtplib
            import urllib.request
            from urllib.request import urlopen as _open
            from ..notify._syn_net import client
            @router.post("/a")
            async def http_get(request):
                return httpx.post("https://example.invalid", json={})
            @router.post("/b")
            async def via_requests(request):
                return requests.get("https://example.invalid")
            @router.post("/c")
            async def mail(request):
                with smtplib.SMTP("localhost") as s:
                    pass
            @router.post("/d")
            async def fetch(request):
                return urllib.request.urlopen("https://example.invalid")
            @router.post("/d2")
            async def fetch_from_import(request):
                return _open("https://example.invalid")
            @router.post("/e")
            async def redis_helper(request):
                return client().zcard("q")
            @router.post("/e2")
            async def redis_inline(request):
                import redis
                return redis.Redis.from_url("redis://x").get("k")
            @router.post("/f")
            async def sync_client(request):
                with httpx.Client() as c:
                    return c.get("https://example.invalid")
            @router.post("/ok")
            async def loop_safe(request):
                import redis.asyncio as aioredis
                async with httpx.AsyncClient() as c:
                    await c.get("https://example.invalid")
                await aioredis.from_url("redis://x").get("k")
                await run_in_threadpool(httpx.post, "https://example.invalid")
                return await run_in_threadpool(client)
        ''')
        self.assertEqual(
            set(scan_module(src, "oikonome.web._synthetic", ix)),
            {"http_get", "via_requests", "mail", "fetch",
             "fetch_from_import", "redis_helper", "redis_inline",
             "sync_client"})

    def test_every_hand_off_form_is_recognised(self):
        src = textwrap.dedent('''
            def _off(x=None):
                return _conn(x)
            @router.post("/a")
            async def executor(request):
                loop = asyncio.get_running_loop()
                def _work():
                    return _conn(request)
                await loop.run_in_executor(None, _work)
                return await loop.run_in_executor(None, partial(_off, x=1))
            @router.post("/b")
            async def anyio_thread(request):
                def _work():
                    return _conn(request)
                await anyio.to_thread.run_sync(_work)
                return await anyio.to_thread.run_sync(_off)
            @router.post("/c")
            async def keyword_func(request):
                def _work():
                    return _conn(request)
                await asyncio.to_thread(func=_work)
                return await run_in_threadpool(func=partial(_off, x=1))
            @router.post("/d")
            async def partial_kw(request):
                def _work(x):
                    return _conn(x)
                return await run_in_threadpool(partial(_work, x=request))
            @router.post("/bad")
            async def executor_then_direct(request):
                loop = asyncio.get_running_loop()
                def _work():
                    return _conn(request)
                await loop.run_in_executor(None, _work)
                return _work()
            @router.post("/bad2")
            async def executor_arg_is_not_the_callable(request):
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(_conn(request), _off)
        ''')
        self.assertEqual(set(scan_module(src)),
                         {"executor_then_direct",
                          "executor_arg_is_not_the_callable"})

    def test_a_gate_call_reaches_the_base_gate(self):
        # `ext.gate` is a proxy object, so `ext.gate.<m>(…)` names no
        # module function; it runs Gate.<m> on an instance with no add-on,
        # and Gate.reactivate writes the tenant row
        src = textwrap.dedent('''
            from .. import ext
            from .. import ext as _ext
            from ..ext import gate
            import oikonome.ext
            @router.post("/a")
            async def via_module(request, admin):
                return ext.gate.reactivate(admin, 1)
            @router.post("/b")
            async def via_alias(request, admin):
                return _ext.gate.reactivate(admin, 1)
            @router.post("/c")
            async def via_gate_name(request, admin):
                return gate.reactivate(admin, 1)
            @router.post("/d")
            async def via_dotted_package(request, admin):
                return oikonome.ext.gate.reactivate(admin, 1)
            @router.post("/ok")
            async def pure_or_handed(request, admin):
                ext.gate.tier_name("x")
                return await run_in_threadpool(ext.gate.reactivate, admin, 1)
        ''')
        self.assertEqual(set(scan_module(src)),
                         {"via_module", "via_alias", "via_gate_name",
                          "via_dotted_package"})
        sc = Scan(Index())
        self.assertTrue(sc.hits(("oikonome.ext", "Gate.reactivate")))

    def _addon_root(self, gate_src):
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp)
        pkg = tmp / "_syn_addon"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "gate.py").write_text(textwrap.dedent(gate_src))
        (pkg / "store.py").write_text(
            "def count(admin):\n    return admin.execute('SELECT 1')\n")
        return pkg

    def test_a_gate_call_reaches_an_add_ons_override(self):
        # with an add-on's root the instance may run its Gate subclass: an
        # override that touches the database makes the same call on-loop
        # work, while the product alone (default answer 0) reads it clean
        pkg = self._addon_root('''
            from oikonome.ext import Gate
            from . import store
            class AddonGate(Gate):
                def accounts_used(self, admin):
                    return store.count(admin)
            class Stricter(AddonGate):
                pass
        ''')
        src = textwrap.dedent('''
            from .. import ext
            @router.post("/a")
            async def count(request, admin):
                return ext.gate.accounts_used(admin)
            @router.post("/ok")
            async def handed(request, admin):
                ext.gate.tier_name("x")
                return await run_in_threadpool(ext.gate.accounts_used, admin)
        ''')
        self.assertEqual(set(scan_module(src)), set())
        for roots in ({"_syn_addon": pkg}, [PKG, pkg]):
            ix = Index(roots=roots)
            self.assertIn(("_syn_addon.gate", "AddonGate"),
                          ix.gate_classes())
            self.assertIn(("_syn_addon.gate", "Stricter"), ix.gate_classes())
            self.assertEqual(
                set(scan_module(src, "oikonome.web._synthetic", ix)),
                {"count"}, roots)


if __name__ == "__main__":
    unittest.main()
