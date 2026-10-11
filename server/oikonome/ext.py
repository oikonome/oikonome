"""Extension points: how an add-on package plugs into an instance.

The product ships everything a household needs to run its own instance.
Anything an operator layers on top for running the service FOR OTHER
PEOPLE — gating, intake, extra routes and jobs, operator console panes —
lives in a separate package that registers itself through the entry-point
groups below. Core code never imports such a package by
name; it asks this module, and when nothing is installed it gets the
permissive defaults of :class:`Gate` (nothing gated, nothing capped, every
feature allowed) and empty lists.

Groups (``importlib.metadata`` entry points):

``oikonome.gate``
    Exactly one object implementing the :class:`Gate` interface. The
    first registered wins; a second one is logged and ignored.
``oikonome.routers``
    FastAPI ``APIRouter`` objects the web app includes after its own.
``oikonome.jobs``
    Objects with ``functions`` (arq job callables) and ``cron_jobs``
    (``arq.cron`` entries) that the worker appends to its own.
``oikonome.migrations``
    Directories of numbered ``*.sql`` migrations applied after the core
    set and recorded in the same ``schema_migrations`` table.
``oikonome.templates``
    Directories searched by the Jinja environment after the core set.
``oikonome.cli``
    Callables ``register(subparsers)`` and objects with ``dispatch(args)
    -> bool`` for extra CLI verbs.
``oikonome.grants``
    SQL strings run after the product's own grant step (privileges on an
    add-on's tables).
``oikonome.admin_panes``
    Lists of pane dicts (``key``, ``label``, ``template``, ``after``,
    ``when``, optional ``icon`` — SVG shape markup for the console's
    24×24 nav frame) the operator console adds to its navigation and views.

Discovery runs once per process and never raises: a broken add-on logs
and the instance runs without it, which is the failure mode an operator
can see and fix. The one exception is a broken GATE on a hosted instance,
which runs under a gate that refuses rather than one that allows
everything (see :class:`_FailClosedGate`).
"""
from __future__ import annotations

import functools
import logging
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

log = logging.getLogger("oikonome.ext")


class Gate:
    """Policy questions the product asks before doing something an
    operator running the instance for other people may want to refuse or
    shape. Every default is the answer for an instance a household runs
    for itself."""

    name = "open"

    # ---- tenant status ----------------------------------------------------
    def reactivate(self, admin, tenant_id) -> str:
        """Lift an operator's suspension. Called inside the console's
        transaction with the tenant row locked; returns the status the
        tenant landed in. A household running for itself has no trial to
        outlive a suspension, so the answer is always active; an add-on
        that bills may land the tenant in its lockout instead."""
        admin.execute("UPDATE tenants SET status='active' WHERE id=%s",
                      (tenant_id,))
        return "active"

    # ---- plan and entitlement --------------------------------------------
    def billing_enabled(self) -> bool:
        return False

    def plan(self) -> str | None:
        return None

    def tiers(self) -> tuple[str, ...]:
        return ()

    def tier_name(self, tier: str | None) -> str:
        return tier or ""

    def effective_tier(self, conn) -> str | None:
        return None

    def tier_allows(self, conn, feature: str) -> bool:
        return True

    def institution_cap(self, conn) -> int | None:
        """None = uncapped."""
        return None

    def institution_cap_message(self, cap: int, used: int) -> str:
        """What a household is told when adding one more institution would
        pass the cap: the numbers and the way forward."""
        return (f"This instance allows up to {cap} connected institutions "
                f"and you're at {used}. Disconnect one on the Accounts page "
                f"to free a slot.")

    def enrich_rows_cap(self) -> int | None:
        """The most monthly enrichment rows a tenant may set for itself when
        the operator pays for them; None leaves the product's own ceiling."""
        return None

    def plaid_products(self, conn) -> list[str] | None:
        """The extra Plaid products this household may sync, or
        None to defer to the instance's environment."""
        return None

    def write_blocked(self, tenant_id) -> bool:
        """True when the tenant may read but not change anything."""
        return False

    def lockout_statuses(self) -> dict[str, tuple[int, str, tuple[str, ...]]]:
        """Tenant statuses the add-on freezes a household under, beyond the
        product's own (suspended, pending_delete): status -> (HTTP status,
        message, paths that stay open beyond sign-out). A status that leaves
        doors open is meant to be escaped inside the app, so /login lets a
        session in that state through to it."""
        return {}

    def unknown_status_lockout(self) -> tuple[int, str, tuple[str, ...]] | None:
        """The lockout for a tenant status that is not 'active' and that
        neither the product nor this gate names, in the shape of a
        lockout_statuses value; None lets it through. Such a status can only
        come from an add-on, and an installed gate names every status it
        sets.

        Every door that admits a household by its status must consult this
        after its own table — the session check (current_user), the
        script-token door, the write refusal, /login, a device mint and a
        share-link join — or a household frozen under an add-on's status
        walks back in whenever the add-on is not there to name it.
        `lockout_for` is that lookup, in one place."""
        return None

    def sync_priority_sql(self) -> str | None:
        """A SELECT returning ``id`` over active tenants in sync-priority
        order, or None for the natural order."""
        return None

    # ---- tenant lifecycle -------------------------------------------------
    def invite_options(self, form: dict, *, operator_created: bool = False) -> dict:
        """What the operator's invite (or create-instance) form asked for
        beyond the product's own fields, as the object the invite carries;
        `form` is every extra field posted, as strings. Nothing, by default:
        an invite is just a seat."""
        return {}

    def invite_summary(self, options: dict) -> str:
        """A clause for the operator's confirmation describing what the
        invite carries beyond a seat, if anything."""
        return ""

    def erasure_receipt(self, admin, tenant_id,
                        released: dict | None) -> tuple[str, str]:
        """Extra (plain, html) paragraphs for the mail that confirms an
        account's erasure — the place to tell the person about anything the
        erasure could NOT undo on their behalf. Read BEFORE the rows go."""
        return "", ""

    def provision(self, admin, tenant_id, *, invite: dict | None,
                  ref: str | None, verified: bool,
                  reclaim: bool) -> str | None:
        """Called inside the signup transaction once the tenant, its
        settings and owner exist. Returns the tenant id owed a referral
        credit for this signup, if any."""
        return None

    def deliver_referral(self, inviter_tenant_id) -> None:
        return None

    def on_demo_household(self, admin, tenant_id) -> None:
        """A demonstration household was seeded or refreshed: write
        whatever plan row it should carry (nothing, by default)."""
        return None

    def on_tenant_purge(self, admin, tenant_id) -> None:
        """Just before a tenant's rows are erased: archive whatever the
        add-on must keep about the account beyond its life (nothing here)."""
        return None

    def export_exclusions(self) -> dict[str, str]:
        """Control-plane tables of the add-on that a portability archive
        and a restore leave out, each with the reason."""
        return {}

    def money_attached(self, admin, tenant_id) -> bool:
        """Is something attached to this household that only its owner
        could have attached — the one kind of record a squatter cannot
        plant."""
        return False

    def machinery_tables(self) -> frozenset[str]:
        """Tables the add-on writes on a household's behalf, whose rows say
        nothing about whether the account is in use."""
        return frozenset()

    def demo_keep_tables(self) -> frozenset[str]:
        """Tables a demonstration household's refresh must leave alone."""
        return frozenset()

    def inventory(self) -> dict:
        """What the add-on adds that the product's inventory tests pin:
        ``no_rls_app_writes`` (table -> (grant letters, columns)),
        ``rls_exempt`` (table -> reason), ``member_routes`` (mutating routes
        a member may call), ``demo_guarded`` (routes a demo tenant is
        refused). Empty by default."""
        return {}

    def release_tenant(self, tenant_id) -> str:
        """Release anything the add-on holds for the tenant outside the
        database. Returns an audit word."""
        return "no_external_state"

    def nightly(self) -> dict:
        """The nightly sweeps that run between the tenant passes and the
        purge of scheduled deletions. Must return ``frozen``: the tenant ids
        the add-on locked out tonight under one of its lockout_statuses
        (empty when there is no such thing)."""
        return {"frozen": []}

    # ---- operator console ---------------------------------------------------
    def audit_noise(self) -> tuple[str, ...]:
        """Audit actions the add-on writes as machine bookkeeping, hidden
        from the console's recent-actions window."""
        return ()

    def mail_kinds(self) -> tuple[tuple[str, str], ...]:
        """(subject needle, kind) pairs for the add-on's own outbound mail,
        so the mail-health view can classify it beside the product's."""
        return ()

    def console_context(self, admin, tenants: list[dict], request) -> dict:
        """Extra template context for the console dashboard. May annotate
        the fleet rows in place and add whole panes' data."""
        return {}

    def tenant_context(self, admin, tenant_id) -> dict:
        """Extra template context for one tenant's console page."""
        return {}

    def health(self) -> dict:
        """Extra rows for the console's health table (which rails can take
        money, and the like)."""
        return {}

    def on_tenant_delete_scheduled(self, tenant_id) -> str:
        """Stop anything that charges the tenant while a grace deletion
        runs. Returns an audit word."""
        return "n/a"

    def on_tenant_restored(self, tenant_id) -> str | None:
        """The deletion was called off: lift whatever the schedule paused.
        Returns an audit word, or None when there is nothing to report."""
        return None

    # ---- intake capacity ---------------------------------------------------
    def intake_cap(self) -> int | None:
        return None

    def accounts_used(self, admin) -> int:
        return 0

    def accounts_used_guarded(self, admin) -> int:
        return 0

    def at_cap(self, accounts: int) -> bool:
        return False

    def capacity_message(self) -> str:
        return "this instance is not taking new accounts right now."


class GateUnavailable(RuntimeError):
    """A decision only the gate add-on can make was asked for while it is
    not loaded. Nothing was changed; the caller says so and the operator
    retries once the add-on loads.

    Its own type, raised only by the fail-closed gate (reactivate,
    on_tenant_purge, release_tenant, provision), so a caller can tell "the add-on is
    missing, stop" from the add-on's own failures, which several callers
    deliberately log and step past. A caller that swallows add-on failures
    with a broad except must catch this one first and re-raise or report
    it, or the stop it signals turns into the very quiet no-op it exists
    to prevent."""

    def __init__(self, what: str):
        super().__init__(f"{what} is unavailable while the billing add-on "
                         "is not loaded")


class _FailClosedGate(Gate):
    """What a HOSTED instance runs under when a gate add-on is registered
    but failed to load. The operator meant the instance to be gated, and
    the permissive defaults would lift every cap and lockout for every
    tenant at once, spending the operator's money on their behalf. So
    nothing billable is allowed, nothing is written, and no new account is
    taken, until the add-on loads again.

    Every policy answer fails closed, not only the ones that spend money:
    a household the add-on froze stays frozen (its status is one only the
    add-on names, so every such status is locked out), nothing is lifted
    into 'active' on the add-on's behalf, and every household is taken to
    have money attached, so no sweep freezes or deletes one that may be
    paying."""

    name = "unavailable"

    def reactivate(self, admin, tenant_id) -> str:
        # whether a trial ran out while the household was suspended is the
        # add-on's to say; landing it in 'active' could not be taken back
        raise GateUnavailable("reactivating a household")

    def unknown_status_lockout(self) -> tuple[int, str, tuple[str, ...]]:
        return (503, "this account is unavailable right now — please try "
                     "again shortly", ())

    def money_attached(self, admin, tenant_id) -> bool:
        return True

    def on_tenant_delete_scheduled(self, tenant_id) -> str:
        # nothing that charges the household could be paused
        return "unavailable"

    def on_tenant_purge(self, admin, tenant_id) -> None:
        # what the add-on keeps beyond the account's life cannot be
        # archived; the purge records the failure rather than a quiet no-op
        raise GateUnavailable("archiving a household's billing record")

    def release_tenant(self, tenant_id) -> str:
        # what the add-on holds outside the database (a subscription that
        # keeps charging) cannot be released: never report there is none
        raise GateUnavailable("releasing a household's billing")

    def provision(self, admin, tenant_id, *, invite: dict | None,
                  ref: str | None, verified: bool,
                  reclaim: bool) -> str | None:
        # a household created now would have no billing record of its own
        # (no trial end, none of the terms an invite carries), and nothing
        # writes one later, so it would keep the full plan for good. Every
        # door that creates a household calls this inside its transaction,
        # an invited signup and the console's create included, so refusing
        # here rolls the whole creation back
        raise GateUnavailable("taking a new account")

    def tier_allows(self, conn, feature: str) -> bool:
        return False

    def institution_cap(self, conn) -> int | None:
        return 0

    def institution_cap_message(self, cap: int, used: int) -> str:
        return ("Connecting institutions is unavailable on this instance "
                "right now.")

    def enrich_rows_cap(self) -> int | None:
        return 0                                # 0 is OFF, never uncapped

    def plaid_products(self, conn) -> list[str] | None:
        return []

    def write_blocked(self, tenant_id) -> bool:
        return True

    def intake_cap(self) -> int | None:
        return 0

    def at_cap(self, accounts: int) -> bool:
        return True


# entry-point groups in which a registered extension failed to load
_LOAD_FAILED: set[str] = set()


@functools.cache
def _group(name: str) -> list[Any]:
    out: list[Any] = []
    _LOAD_FAILED.discard(name)
    try:
        eps = entry_points(group=name)
    except Exception:                                    # noqa: BLE001
        log.exception("extension discovery failed for %s", name)
        _LOAD_FAILED.add(name)
        return out
    for ep in eps:
        try:
            out.append(ep.load())
        except Exception:                                # noqa: BLE001
            log.exception("extension %s (%s) failed to load; skipped",
                          ep.name, name)
            _LOAD_FAILED.add(name)
    return out


class _GateProxy:
    """Resolves the provider on first use and forwards every call, so
    modules can hold ``ext.gate`` at import time while discovery stays
    lazy (and patchable in tests through ``ext.set_gate``)."""

    _impl: Gate | None = None

    def _resolve(self) -> Gate:
        if self._impl is None:
            found = _group("oikonome.gate")
            if found:
                impl = found[0]
            elif ("oikonome.gate" in _LOAD_FAILED
                  and _env_flag("OIKONOME_HOSTED")):
                # A hosted instance with NO add-on is a documented mode and
                # keeps the defaults; one whose registered gate broke must
                # not silently become ungated.
                log.error("the gate add-on failed to load on a hosted "
                          "instance; refusing billable features, writes "
                          "and new accounts until it loads")
                impl = _FailClosedGate()
            else:
                impl = Gate()
            if len(found) > 1:
                log.warning("several gate providers registered; using %s",
                            getattr(impl, "name", impl))
            if isinstance(impl, type):
                impl = impl()
            self._impl = impl
        return self._impl

    def __getattr__(self, item):
        return getattr(self._resolve(), item)


def _env_flag(name: str) -> bool:
    from .envnum import env_flag
    return env_flag(name)


gate = _GateProxy()


def lockout_for(status: str | None, known: dict
                ) -> tuple[int, str, tuple[str, ...]] | None:
    """The lockout a household in `status` is under, for every door that
    admits one by its status: `known` is the product's own table (the web
    app's LOCKOUT_STATUSES, which carries the gate's lockout_statuses());
    'active' (or no status) passes; any other status no table names is
    the installed gate's unknown_status_lockout() — None from a permissive
    gate, a lockout from the fail-closed one."""
    if status in known:
        return known[status]
    if not status or status == "active":
        return None
    return gate.unknown_status_lockout()


HOSTED_BILLING_VAR = "OIKONOME_HOSTED_BILLING"


def require_hosted_billing() -> None:
    """A hosted instance says how it bills, or does not start.

    With a gate add-on installed, the add-on owns billing and this is
    satisfied. Without one, hosted mode would run in the documented
    no-add-on shape — gating nothing, capping nothing — which cannot be
    told apart from a hosted build that is missing its add-on, so an
    explicit opt-out is required: a hosted instance with no add-on must
    say `OIKONOME_HOSTED_BILLING=none` to run; anything else refuses to
    start, loudly, at import. (Self-host is untouched: the check applies
    only when the instance says it is hosted.) A registered add-on that
    failed to load is the other case, handled by the fail-closed gate."""
    import os
    if not _env_flag("OIKONOME_HOSTED"):
        return
    if _group("oikonome.gate") or "oikonome.gate" in _LOAD_FAILED:
        return
    want = (os.environ.get(HOSTED_BILLING_VAR) or "").strip().lower()
    if want == "none":
        log.warning("hosted instance running with no billing add-on "
                    "(%s=none): nothing is gated or billed", HOSTED_BILLING_VAR)
        return
    raise SystemExit(
        "OIKONOME_HOSTED=1 but no billing add-on is installed. Install the "
        f"add-on, or set {HOSTED_BILLING_VAR}=none to run a hosted instance "
        "that bills nobody. Refusing to start rather than silently serve "
        "paying households for free.")


def set_gate(impl: Gate | None) -> None:
    """Install a provider explicitly (tests, or an embedding process).
    None restores discovery."""
    gate._impl = impl


def routers() -> list[Any]:
    return list(_group("oikonome.routers"))


def job_functions() -> list[Any]:
    out: list[Any] = []
    for j in _group("oikonome.jobs"):
        out.extend(getattr(j, "functions", ()) or ())
    return out


def cron_jobs() -> list[Any]:
    out: list[Any] = []
    for j in _group("oikonome.jobs"):
        out.extend(getattr(j, "cron_jobs", ()) or ())
    return out


def migration_dirs() -> list[Path]:
    return [Path(p) for p in _group("oikonome.migrations")]


def template_dirs() -> list[str]:
    return [str(p) for p in _group("oikonome.templates")]


def cli_register(subparsers) -> None:
    for c in _group("oikonome.cli"):
        reg = getattr(c, "register", None) or (c if callable(c) else None)
        if reg is not None:
            reg(subparsers)


def cli_dispatch(args) -> bool:
    for c in _group("oikonome.cli"):
        d = getattr(c, "dispatch", None)
        if d is not None and d(args):
            return True
    return False


def grant_sql() -> list[str]:
    """Privilege statements an add-on runs after the product's own grant
    step, for the tables its migrations add (the app role gets ALL on
    every table by default; an add-on narrows what it must)."""
    return [str(g) for g in _group("oikonome.grants")]


def admin_panes() -> list[dict]:
    """Console panes an add-on contributes: dicts with ``key``, ``label``,
    ``template`` (a Jinja path the loader can find), optional ``after`` (the
    core tab it follows in the nav) and optional ``when`` (a context key
    that must be truthy for the pane to render)."""
    out: list[dict] = []
    for p in _group("oikonome.admin_panes"):
        out.extend(p)
    return out
