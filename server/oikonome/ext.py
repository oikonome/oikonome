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
can see and fix.
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


@functools.cache
def _group(name: str) -> list[Any]:
    out: list[Any] = []
    try:
        eps = entry_points(group=name)
    except Exception:                                    # noqa: BLE001
        log.exception("extension discovery failed for %s", name)
        return out
    for ep in eps:
        try:
            out.append(ep.load())
        except Exception:                                # noqa: BLE001
            log.exception("extension %s (%s) failed to load; skipped",
                          ep.name, name)
    return out


class _GateProxy:
    """Resolves the provider on first use and forwards every call, so
    modules can hold ``ext.gate`` at import time while discovery stays
    lazy (and patchable in tests through ``ext.set_gate``)."""

    _impl: Gate | None = None

    def _resolve(self) -> Gate:
        if self._impl is None:
            found = _group("oikonome.gate")
            impl = found[0] if found else Gate()
            if len(found) > 1:
                log.warning("several gate providers registered; using %s",
                            getattr(impl, "name", impl))
            if isinstance(impl, type):
                impl = impl()
            self._impl = impl
        return self._impl

    def __getattr__(self, item):
        return getattr(self._resolve(), item)


gate = _GateProxy()


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
