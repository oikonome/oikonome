"""The concurrency harness (specs/invariants-and-fuzzing.md, section 3).

A scenario is a few writers aimed at one synthetic household at once: each
an `Actor` thread, the household's own connections through
`tenancy.tenant_connect`. Every actor starts paused. A schedule says who
goes when: all together through one `threading.Barrier`, or one actor
first until it parks at a named `Seam` holding the locks it took there,
then each of the others until it is quiet (parked, finished, or queued
behind a lock, read from `pg_locks`, never guessed with a sleep), and only
then the parked one. The default is the fixed set `fixed_schedules` makes:
together, plus "this actor parks holding that seam while the others
arrive", for every actor and every seam. In long mode
(`OIKONOME_PROPERTY=long`) every scenario also runs `random_schedules`:
each actor parks at a random seam or none, and the actors' starts and
resumptions come in a random order drawn from the run's printed seed
(`props.run_seed`), which every such schedule's label names.

A seam is a pause point at a lock seam: a household lock by its rank in
the documented order (`tests/lock_order_hook.RANKS`), any statement by a
pattern (a row lock), or a function, before or after it runs. Statement
seams are read off the statements psycopg runs, as the L1 hook reads them,
so a helper bound into a module at import time cannot slip past.

After every schedule the oracle holds:

* L2 no deadlock: no statement an actor ran was killed as a deadlock (seen
  where it is raised, so a best-effort step that swallows it is still
  caught), and the database's deadlock count has not moved
  (`lock_order_util.deadlocks_since`, one settle per batch);
* L3 serial equivalence: the digest of the rows the scenario touches
  equals the digest of SOME serial order of the actors that took effect,
  each serial order run on a fresh household built the same way (n <= 4,
  so at most 24 of them);
* L4 busy, never broken: an actor that loses a race answers busy (a 409,
  or the LockNotAvailable the web layer answers 409) within
  `reanchor.REQUEST_LOCK_WAIT_MS` and a margin, and only an actor the
  scenario names as a door may; anything else raised is a failure, apart
  from the stated refusals the scenario lists;
* L5 single flight: with `single_flight`, an actor parked inside the door
  leaves every other caller skipping it;
* L6 pool hygiene: no backend of the database is left idle inside a
  transaction or idle holding an advisory lock
  (`lock_order_util.stuck_backends`);
* L7 the money invariants the scenario names, on the rows it touched.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import os
import random
import re
import threading
import time
import uuid
from typing import Any, Callable
from unittest import mock

import psycopg
from fastapi import HTTPException

from oikonome.db import tenancy
from oikonome.sync import reanchor

from . import lock_order_hook, props
from .lock_order_util import (deadlocks, deadlocks_since,
                              stuck_backends)
from .util import _ensure_db, join_product_threads, make_db, write_config

# a door gives up after REQUEST_LOCK_WAIT_MS; the margin is a loaded shard
BUSY_BOUND_S = reanchor.REQUEST_LOCK_WAIT_MS / 1000 + 3.0
# how long one actor may take to go quiet after it is let go
STEP_S = 15.0
# how long a schedule may take to finish once everyone is let go
FINISH_S = 60.0
POOL_MAX = int(os.environ.get("OIKONOME_POOL_MAX", "10") or 10)
MAX_ACTORS = 6
# random schedules per scenario in long mode
RANDOM_SCHEDULES = int(os.environ.get("OIKONOME_CONCURRENCY_SCHEDULES", "20")
                       or 20)

_local = threading.local()


class Seam:
    """A named pause point. Exactly one of:

    * `rank`: right after the household lock of that rank is granted
      (`lock_order_hook.rank_of` reads it off the statement);
    * `sql`: right after a statement matching the pattern (case-blind) ran,
      read with its parameters after it (" -- p1 p2");
    * `func`: (owner, attribute name), paused `when` "before" or "after"
      the call.
    """

    def __init__(self, label: str, *, rank: int | None = None,
                 sql: str | None = None, func: tuple | None = None,
                 when: str = "after"):
        assert sum(x is not None for x in (rank, sql, func)) == 1, label
        assert when in ("before", "after"), when
        self.label, self.rank, self.func, self.when = label, rank, func, when
        self.sql = re.compile(sql, re.I | re.S) if sql else None

    def matches(self, query, params) -> bool:
        if self.rank is not None:
            return lock_order_hook.rank_of(query, params) == self.rank
        if self.sql is not None:
            text = query if isinstance(query, str) else (
                query.decode() if isinstance(query, bytes) else str(query))
            # the parameters too: a lock's key is usually one
            if params:
                text += " -- " + " ".join(str(p) for p in (
                    params.values() if isinstance(params, dict) else params))
            return bool(self.sql.search(text))
        return False

    def __repr__(self):
        return f"Seam({self.label})"


@dataclasses.dataclass
class Household:
    """A fresh household: its tenant id, the fixture accounts and settings
    (tests.util), an open connection for setup and digests, and an owner
    `user` for calling doors directly."""

    tid: str
    conn: Any
    user: dict

    @classmethod
    def new(cls) -> "Household":
        conn = make_db()
        write_config(conn)
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        return cls(tid, conn, {"tenant_id": tid, "role": "owner", "id": None,
                               "email": f"own-{uuid.uuid4().hex[:8]}@example.dev"})

    def connect(self):
        return tenancy.tenant_connect(self.tid)

    def close(self):
        self.conn.close()


@dataclasses.dataclass
class Scenario:
    """`setup(hh)` builds the starting state (the same on every household
    it is given) and returns a context; each `actors[name](hh, ctx)` is
    one writer; `digest(hh, ctx)` reads the touched rows in a form that
    does not depend on the household's ids; `invariants(hh, ctx)` returns
    the L7 breaches it finds.

    `refused(name, result)` says a returned result did nothing (an error
    answer, a skip); `expected` are the exception types that are a stated
    refusal; `doors` are the actors that may answer busy.
    `single_flight`: the actors are callers of one single-flight door."""

    name: str
    setup: Callable
    actors: dict
    digest: Callable
    seams: tuple = ()
    invariants: Callable | None = None
    refused: Callable = lambda name, result: False
    expected: tuple = ()
    doors: frozenset = frozenset()
    single_flight: bool = False
    patches: Callable | None = None       # () -> context manager, every run


@dataclasses.dataclass
class Step:
    # "go" (one actor, until quiet), "together", "outlast" (one actor
    # still parked waits until this one finishes), or "resume" (an actor
    # parked at its seam goes on, until quiet; nothing if it is not parked)
    kind: str
    names: tuple


@dataclasses.dataclass
class Schedule:
    label: str
    pauses: dict              # actor -> Seam it parks at (once)
    steps: list
    expect: dict = dataclasses.field(default_factory=dict)  # actor -> kind
    # every other actor arrives while the parked one holds its seam, so a
    # single-flight door must have turned them away (the single-flight
    # invariant)
    others_arrive_held: bool = True


def outlasting(holder: str, seam: Seam, door: str) -> Schedule:
    """`holder` parks at `seam` and stays there until `door`, arriving
    behind it, has given up: L4's bound, not just its order."""
    return Schedule(f"{holder} holds {seam.label} until {door} gives up",
                    {holder: seam}, [Step("go", (holder,)),
                                     Step("go", (door,)),
                                     Step("outlast", (door,))],
                    expect={door: "busy"})


def fixed_schedules(names, seams) -> list[Schedule]:
    """Together, then for every seam and every actor: that actor parks
    holding the seam while each other actor in turn arrives."""
    names = tuple(names)
    out = [Schedule("together", {}, [Step("together", names)])]
    for s in seams:
        for x in names:
            rest = [n for n in names if n != x]
            out.append(Schedule(
                f"{x} parks at {s.label}", {x: s},
                [Step("go", (x,))] + [Step("go", (n,)) for n in rest]))
    return out


def random_schedules(names, seams, *, seed: int, n: int,
                     tag: str = "") -> list[Schedule]:
    """`n` schedules drawn from `seed` (and `tag`, the scenario's name, so
    two scenarios draw differently): each actor parks at a random seam or
    none, and every actor's start, and each parked actor's resumption
    after its start, come in a random order. The label names the seed."""
    names = tuple(names)
    rng = random.Random(f"{seed} {tag}")
    out = []
    for i in range(n):
        pauses = {x: rng.choice(seams) for x in names
                  if seams and rng.random() < 0.7}
        pending = [("go", x) for x in names]
        steps: list[Step] = []
        while pending:
            kind, x = pending.pop(rng.randrange(len(pending)))
            steps.append(Step(kind, (x,)))
            if kind == "go" and x in pauses:
                pending.append(("resume", x))
        order = [f"{s.kind[0]}:{s.names[0]}" for s in steps]
        held = any(
            all(order.index(f"g:{x}") < order.index(f"g:{y}")
                < order.index(f"r:{x}") for y in names if y != x)
            for x in pauses)
        where = ", ".join(f"{x}@{s.label}" for x, s in pauses.items())
        out.append(Schedule(
            f"random #{i} of OIKONOME_PROPERTY_SEED={seed}: "
            f"{' '.join(order)}" + (f" ({where})" if where else ""),
            pauses, steps, others_arrive_held=held))
    return out


class Actor(threading.Thread):
    """One writer. Starts parked; parks once more at its seam, if it has
    one; records what it returned or raised and how long it ran since it
    was last let go."""

    def __init__(self, name: str, fn: Callable, hh: Household, ctx):
        super().__init__(daemon=True, name=f"actor-{name}")
        self.label, self.fn, self.hh, self.ctx = name, fn, hh, ctx
        self.pause_at: Seam | None = None
        self.reached = False
        self.parks = 0
        self.parked = threading.Event()
        self.done = threading.Event()
        self.barrier: threading.Barrier | None = None
        self._go = threading.Semaphore(0)
        self.result = None
        self.error: BaseException | None = None
        self.deadlocked: list[str] = []
        self.let_go_at = 0.0
        self.ended_at = 0.0

    def park(self):
        self.parks += 1
        self.parked.set()
        if not self._go.acquire(timeout=FINISH_S):
            raise RuntimeError(f"{self.label} was never let go")
        self.parked.clear()

    def let_go(self):
        self.let_go_at = time.monotonic()
        self._go.release()

    def at_seam(self, seam: Seam) -> None:
        if self.pause_at is seam and not self.reached:
            self.reached = True
            self.park()

    def run(self):
        _local.actor = self
        try:
            self.park()                                  # the start gate
            if self.barrier is not None:
                self.barrier.wait(STEP_S)
            self.result = self.fn(self.hh, self.ctx)
        except BaseException as e:                       # noqa: BLE001
            self.error = e
        finally:
            self.ended_at = time.monotonic()
            _local.actor = None
            self.done.set()


@contextlib.contextmanager
def _seams_installed(seams):
    """Statement seams and the deadlock witness on every cursor, function
    seams on their functions, for the actors' threads only."""
    orig = psycopg.Cursor.execute
    stmt = [s for s in seams if s.func is None]

    def execute(self, query, params=None, *a, **kw):
        actor = getattr(_local, "actor", None)
        try:
            out = orig(self, query, params, *a, **kw)
        except psycopg.errors.DeadlockDetected as e:
            if actor is not None:
                actor.deadlocked.append(str(e).splitlines()[0])
            raise
        if actor is not None and actor.pause_at in stmt \
                and not actor.reached and actor.pause_at.matches(query, params):
            actor.at_seam(actor.pause_at)
        return out

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(psycopg.Cursor, "execute",
                                              execute))
        for s in seams:
            if s.func is None:
                continue
            owner, attr = s.func
            real = getattr(owner, attr)

            def paused(*a, _real=real, _s=s, **kw):
                actor = getattr(_local, "actor", None)
                if actor is not None and _s.when == "before":
                    actor.at_seam(_s)
                out = _real(*a, **kw)
                if actor is not None and _s.when == "after":
                    actor.at_seam(_s)
                return out
            stack.enter_context(mock.patch.object(owner, attr, paused))
        yield


def _dump_threads(why: str) -> None:
    """Where every thread is, for a run that hung."""
    import faulthandler
    import sys
    print(f"\n--- {why}: every thread ---", file=sys.stderr)
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def _lock_waiters(observer) -> set[int]:
    return {r["pid"] for r in observer.execute(
        """SELECT DISTINCT l.pid FROM pg_locks l
             JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE NOT l.granted AND a.datname = current_database()"""
    ).fetchall()}


def classify(sc: Scenario, actor: Actor) -> str:
    """done, refused, busy or broken."""
    e = actor.error
    if e is None:
        return "refused" if sc.refused(actor.label, actor.result) else "done"
    if isinstance(e, HTTPException):
        if e.status_code == 409:
            return "busy"
        return "refused" if e.status_code < 500 else "broken"
    if isinstance(e, psycopg.errors.LockNotAvailable):
        return "busy"
    if sc.expected and isinstance(e, sc.expected):
        return "refused"
    return "broken"


class Harness:
    """Runs scenarios, one batch at a time: `begin()`, any number of
    `run(scenario)`, then `end()`, which pays the one deadlock settle."""

    def __init__(self):
        self.observer = None
        self.deadlocks_before = 0
        self.baseline: set[int] = set()
        self.timings: dict[str, float] = {}

    def begin(self):
        _ensure_db()
        self.observer = tenancy.admin_connect()
        self.deadlocks_before = deadlocks()
        # an earlier test's backend is that test's, not a scenario's
        self.baseline = {int(s.split()[0])
                         for s in stuck_backends(self.observer)}

    def end(self) -> list[str]:
        out = []
        n = deadlocks_since(self.deadlocks_before)
        if n:
            out.append(f"L2: the database counted {n} deadlock(s) in the "
                       "batch")
        self.observer.close()
        return out

    # -- one scenario -------------------------------------------------
    def run(self, sc: Scenario, schedules=None) -> list[str]:
        """Every schedule of `sc`; returns the failures, each naming its
        scenario and schedule."""
        names = tuple(sc.actors)
        assert 2 <= len(names) <= MAX_ACTORS, names
        # each actor may hold its own connection and the door's, and the
        # setup connection and the serial households need one each
        assert 2 * len(names) + 2 <= POOL_MAX, "the scenario needs more " \
            "connections than the pool has"
        schedules = schedules or fixed_schedules(names, sc.seams)
        t0 = time.monotonic()
        serial: dict[tuple, Any] = {}
        failures: list[str] = []
        reached: set[str] = set()
        for sched in schedules:
            got = self._run_schedule(sc, sched, serial)
            reached |= got[0]
            failures += [f"{sc.name} [{sched.label}]: {f}" for f in got[1]]
        # a digest no serial order changes would pass L3 having read nothing
        hh = Household.new()
        try:
            initial = sc.digest(hh, sc.setup(hh))
        finally:
            hh.close()
        if self._serial(sc, names, serial) == initial:
            failures.append(f"{sc.name}: the actors run one after the other "
                            "change nothing the digest reads, so L3 checks "
                            "nothing")
        for s in sc.seams:
            if s.label not in reached:
                failures.append(f"{sc.name}: no actor ever reached the seam "
                                f"'{s.label}', so its schedules tested "
                                "nothing")
        self.timings[sc.name] = time.monotonic() - t0
        return failures

    def _serial(self, sc: Scenario, order: tuple, cache: dict):
        if order not in cache:
            hh = Household.new()
            try:
                ctx = sc.setup(hh)
                threads_before = set(threading.enumerate())
                with (sc.patches() if sc.patches else contextlib.nullcontext()):
                    for n in order:
                        # on a thread of its own, so a writer that hangs
                        # fails the run instead of stopping it; whatever
                        # it raises in this order is this order's outcome
                        a = Actor(n, sc.actors[n], hh, ctx)
                        a.start()
                        assert a.parked.wait(STEP_S)
                        a.let_go()
                        if not a.done.wait(FINISH_S):
                            _dump_threads(f"{n} hung run alone")
                            raise AssertionError(
                                f"{sc.name}: {n} hung run alone after "
                                f"{order}")
                join_product_threads(threads_before)
                cache[order] = sc.digest(hh, ctx)
            finally:
                hh.close()
        return cache[order]

    def _run_schedule(self, sc: Scenario, sched: Schedule, serial: dict):
        hh = Household.new()
        out: list[str] = []
        reached: set[str] = set()
        try:
            ctx = sc.setup(hh)
            threads_before = set(threading.enumerate())
            actors = {n: Actor(n, fn, hh, ctx) for n, fn in sc.actors.items()}
            for n, seam in sched.pauses.items():
                actors[n].pause_at = seam
            with (sc.patches() if sc.patches else contextlib.nullcontext()), \
                    _seams_installed(sc.seams):
                for a in actors.values():
                    a.start()
                for a in actors.values():
                    assert a.parked.wait(STEP_S), f"{a.label} never started"
                for step in sched.steps:
                    if step.kind == "together":
                        b = threading.Barrier(len(step.names))
                        for n in step.names:
                            actors[n].barrier = b
                        for n in step.names:
                            actors[n].let_go()
                    elif step.kind == "resume":
                        a = actors[step.names[0]]
                        if a.reached and a.parked.is_set() \
                                and not a.done.is_set():
                            self._go_until_quiet(a)
                    elif step.kind == "outlast":
                        # a parked actor holds on until this one is done:
                        # a door behind it must give up busy in time
                        actors[step.names[0]].done.wait(FINISH_S)
                    else:
                        self._go_until_quiet(actors[step.names[0]])
                self._finish(actors.values())
            # the work a door handed to a thread of its own (the categorize
            # pass after an import) is part of the schedule: it finishes
            # before the pool and the digest are read
            join_product_threads(threads_before)
            for a in actors.values():
                if a.reached:
                    reached.add(a.pause_at.label)
            out += self._judge(sc, sched, hh, ctx, actors, serial)
        finally:
            hh.close()
        return reached, out

    def _go_until_quiet(self, a: Actor) -> None:
        before = _lock_waiters(self.observer)
        parks = a.parks
        a.let_go()
        end = time.monotonic() + STEP_S
        while time.monotonic() < end:
            if a.done.is_set() or a.parks > parks:
                return
            if _lock_waiters(self.observer) - before:
                return
            time.sleep(0.01)

    def _finish(self, actors) -> None:
        end = time.monotonic() + FINISH_S
        actors = list(actors)
        while time.monotonic() < end and not all(a.done.is_set()
                                                 for a in actors):
            for a in actors:
                if a.parked.is_set() and not a.done.is_set():
                    a.let_go()
                    # let it leave the gate before it is looked at again
                    while a.parked.is_set() and not a.done.is_set():
                        time.sleep(0.001)
            time.sleep(0.005)
        if not all(a.done.is_set() for a in actors):
            _dump_threads("a schedule did not finish")
        for a in actors:
            a.join(1)

    def _stuck(self) -> list[str]:
        """L6: backends left stuck, apart from the batch's baseline. Read
        once every thread the schedule's doors started has finished, so a
        backend found here is stuck, not between two statements of live
        work."""
        return [s for s in stuck_backends(self.observer)
                if int(s.split()[0]) not in self.baseline]

    def _judge(self, sc, sched, hh, ctx, actors, serial) -> list[str]:
        out = []
        kinds = {}
        for a in actors.values():
            if not a.done.is_set():
                out.append(f"{a.label} hung")
                kinds[a.label] = "hung"
                continue
            k = kinds[a.label] = classify(sc, a)
            if a.deadlocked:
                out.append(f"L2: {a.label} was killed as a deadlock "
                           f"({a.deadlocked[0]})")
            if k == "broken":
                out.append(f"{a.label} broke: {type(a.error).__name__}: "
                           f"{a.error}")
            elif k == "busy":
                took = a.ended_at - a.let_go_at
                if a.label not in sc.doors:
                    out.append(f"L4: {a.label} is not a door and gave up "
                               f"busy ({a.error})")
                elif took > BUSY_BOUND_S:
                    out.append(f"L4: {a.label} answered busy after "
                               f"{took:.1f}s (bound {BUSY_BOUND_S:.1f}s)")
        held = all(actors[n].reached for n in sched.pauses)
        for n, want in sched.expect.items():
            if not held:
                out.append(f"the holder never reached its seam, so {n} was "
                           "never held up")
            elif kinds.get(n) != want:
                got = actors[n].error or actors[n].result
                out.append(f"{n} ended {kinds.get(n)}, not {want} "
                           f"({got!r:.200})")
        if sc.single_flight and sched.others_arrive_held \
                and any(a.reached for a in actors.values()):
            done = [n for n, k in kinds.items() if k == "done"]
            if len(done) != 1:
                out.append(f"L5: {len(done)} callers ran the single-flight "
                           f"door at once ({done})")
        if "hung" in kinds.values():
            return out
        stuck = self._stuck()
        if stuck:
            out.append(f"L6: the pool was left with {stuck}")
        took_effect = [n for n, k in kinds.items() if k == "done"]
        if os.environ.get("OIKONOME_CONCURRENCY_TRACE"):
            import sys
            print(f"\n  {sc.name} [{sched.label}]: {kinds} "
                  + "; ".join(f"{a.label}: {a.error or a.result!r:.120}"
                              for a in actors.values()), file=sys.stderr)
        got = sc.digest(hh, ctx)
        orders = list(itertools.permutations(took_effect))
        if not any(got == self._serial(sc, o, serial) for o in orders):
            want = {o: self._serial(sc, o, serial) for o in orders}
            out.append(f"L3: the outcome matches no serial order of "
                       f"{took_effect} (kinds {kinds}):\n    got  {got!r}\n"
                       + "\n".join(f"    {'→'.join(o) or '(none)'}: {w!r}"
                                   for o, w in want.items()))
        if sc.invariants:
            out += [f"L7: {m}" for m in sc.invariants(hh, ctx)]
        return out


def money_invariants(conn) -> list[str]:
    """The money invariants a race can break on the rows it touched: a
    split sums to its live row; no split, link or flag hangs on a retired
    row; no charge or deposit carries more partial links than its amount."""
    out = [f"split on {r['txn_id']} sums to {r['s']} against {r['a']}"
           f" (removed={r['removed']})" for r in conn.execute(
        """SELECT s.txn_id, t.removed, ROUND(SUM(s.amount)::numeric, 2) AS s,
                  ROUND(t.amount::numeric, 2) AS a
             FROM transaction_splits s JOIN transactions t ON t.id = s.txn_id
            GROUP BY s.txn_id, t.removed, t.amount
           HAVING t.removed <> 0
               OR ROUND(SUM(s.amount)::numeric, 2)
                  <> ROUND(t.amount::numeric, 2)""").fetchall()]
    out += [f"{r['what']} on retired row {r['id']}" for r in conn.execute(
        """SELECT 'a link' AS what, t.id FROM reimbursements r
             JOIN transactions t ON t.id IN (r.expense_id, r.reimburse_id)
            WHERE t.removed <> 0
           UNION ALL
           SELECT 'a flag', t.id FROM reimburse_flags f
             JOIN transactions t ON t.id = f.txn_id
            WHERE t.removed <> 0""").fetchall()]
    for col in ("expense_id", "reimburse_id"):
        out += [f"{r['id']} carries {r['s']} of links against {r['a']}"
                for r in conn.execute(
            f"""SELECT t.id, SUM(r.amount) AS s, ABS(t.amount) AS a
                  FROM reimbursements r JOIN transactions t
                    ON t.id = r.{col}
                 WHERE r.partial = 1
                 GROUP BY t.id, t.amount
                HAVING SUM(r.amount) > ABS(t.amount) + 0.005""").fetchall()]
    return out


class ScenarioCase:
    """Mixin for a scenario module's TestCase: one harness batch per class
    (one deadlock settle), `check(scenario)` runs it and fails with every
    breach at once."""

    @classmethod
    def setUpClass(cls):
        cls.harness = Harness()
        cls.harness.begin()

    @classmethod
    def tearDownClass(cls):
        found = cls.harness.end()
        if os.environ.get("OIKONOME_CONCURRENCY_TIMINGS"):
            import sys
            for k, v in cls.harness.timings.items():
                print(f"\n  {k}: {v:.1f}s", file=sys.stderr)
        if found:
            raise AssertionError("\n".join(found))

    def check(self, sc: Scenario, schedules=None):
        if props.LONG:
            schedules = (schedules or fixed_schedules(sc.actors, sc.seams)) \
                + random_schedules(sc.actors, sc.seams,
                                   seed=props.run_seed(),
                                   n=RANDOM_SCHEDULES, tag=sc.name)
        found = self.harness.run(sc, schedules)
        self.assertEqual(found, [], "\n" + "\n".join(found))
