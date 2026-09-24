"""arq worker — the product's scheduler. One worker serves every tenant; each task is tenant-scoped through
tenancy.tenant_connect so RLS applies inside jobs exactly as in the web app.

Schedule (cron, UTC — per-tenant send-time preference comes later):
  * sync_all        hourly/tenant — pull every simplefin/plaid item, heartbeat;
                                   fires every 5 min, each run serving one
                                   hash slot of tenants (SYNC_BUCKETS), so
                                   tenants never sync in one :00 burst;
                                   connection break/recovery email on item
                                   status transitions (jobs/link_alert.py);
                                   plaid liabilities+holdings ride this sweep
                                   roughly once per DAY per tenant (job_runs
                                   heartbeat 'plaid-products', >20h ⇒ due)
  * nightly_all     04:17        — recurring detection + drift per tenant,
                                   THEN the net-worth snapshot per tenant
                                   (must run from day one, or the recorded
                                   series gets a hole)
  * email_all       07:00        — daily verdict email per tenant

Task bodies are synchronous (the engine is sync); they run in a thread so
the worker loop stays responsive. Per-tenant error isolation: one tenant's
failure logs + continues, never kills the sweep.

Run: `python -m arq oikonome.jobs.worker.WorkerSettings` (compose `worker`
service). Env: REDIS_URL, OIKONOME_DSN/ADMIN_DSN, OIKONOME_MASTER_KEY,
OIKONOME_SMTP_*.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import threading

from arq import cron

from .. import ext
from ..db import tenancy
from ..engine import alerts, bills
from ..envnum import env_flag, env_num
from ..sync import base as sync_base
from ..sync import plaid, simplefin
from . import link_alert

log = logging.getLogger("oikonome.jobs")


def _tenant_ids(prioritize: bool = False) -> list[str]:
    admin = tenancy.admin_connect()
    try:
        if prioritize:
            # an installed gate may order live accounts ahead of stubs;
            # ordering only — every active tenant is synced
            sql = ext.gate.sync_priority_sql()
            if sql:
                return [str(r["id"]) for r in admin.execute(sql).fetchall()]
        return [str(r["id"]) for r in admin.execute(
            "SELECT id FROM tenants WHERE status='active'").fetchall()]
    finally:
        admin.close()


# ---- per-tenant task bodies (sync; unit-testable without redis) ------------


def _push_alert(tenant_id: str, title: str, body: str) -> None:
    """Out-of-band alert fan-out to the push channels, alongside the alert
    EMAIL that triggered it: web push carries the words, phones get the
    content-free `alert` tickle and pull the real story from this server
    when they wake. Riding the email's edge means the sender's existing
    dedup (status transition / alerted_at stamp) bounds this to one push
    per event — there is deliberately no push-side state. Failures log and
    never raise, the doctrine of every notify channel."""
    from ..db.tenancy import control_connect
    from ..notify import push as _push
    from ..notify import push_native as _native
    try:
        cc = control_connect()
        try:
            _push.send_tenant(cc, tenant_id, title, body, url="/app/alerts")
            _native.send_tenant(cc, tenant_id, "alert")
            cc.commit()
        finally:
            cc.close()
    except Exception as e:                       # noqa: BLE001
        log.warning("alert push fan-out failed for %s: %s", tenant_id, e)


def _recipients_raw(conn, tenant_id: str) -> list[str]:
    """Tenant alert/email recipients: the account OWNER always, plus anyone
    configured in `email_recipients`. With nothing configured, every
    verified user on the tenant (else every user).

    The owner is never DROPPED when the recipients field is filled in: read
    as a replacement rather than an addition, that field silently stops the
    owner's own mail — you add your partner's address, save, and quietly stop
    receiving your own daily verdict. The owner is the person whose money
    this is; they are on the list by construction.

    On a hosted instance, configured recipients are filtered to addresses
    that hold an account on the tenant — the settings save enforces this
    going forward; the filter covers configs written before the policy.
    Self-host keeps free-form recipients (the operator owns the SMTP).

    Every extra recipient must also have ACCEPTED their invite.
    Configuring an address proposes it; only the person who owns that
    mailbox can enrol it. The owner is exempt — nobody invites you to your
    own household's mail.

    Per-person opt-out: `email_muted` in tenant config
    lists addresses that asked not to receive the scheduled mail — the
    OWNER INCLUDED, so the person who configures the schedule can keep the
    daily going to a partner while opting out personally. Muting is
    explicit per-address state, so it cannot become a silent drop —
    Settings shows it.

    This is the UNFILTERED list — `_recipients` is what callers want."""
    from ..engine import budget
    cfg = budget.load_config(conn)
    recips = cfg.get("email_recipients")
    # tolerate any restored shape — a non-list here killed every
    # scheduled send for the tenant, silently
    _muted_raw = cfg.get("email_muted")
    muted = ({str(m).lower() for m in _muted_raw}
             if isinstance(_muted_raw, list) else set())

    def _unmuted(emails: list[str]) -> list[str]:
        return [e for e in emails if e.lower() not in muted]

    admin = tenancy.admin_connect()
    try:
        if recips:
            # one policy, one place — the settings doors and this sweep must
            # agree on who may be mailed, or a saved list quietly stops being
            # the list that gets sent to
            from ..web import mailguard
            if mailguard.members_only():
                members = {r["email"] for r in admin.execute(
                    "SELECT email FROM users WHERE tenant_id=%s",
                    (tenant_id,)).fetchall()}
                recips = [r for r in recips if r.lower() in members]
                if not recips:
                    log.warning("tenant %s: no configured recipient holds an "
                                "account here — sending to the owner only",
                                tenant_id)
            # An extra recipient is mailed only once THEY have said
            # yes. Being on the list is the owner's decision; receiving a
            # household's balances and spending is the recipient's. An
            # invited-but-unanswered address is skipped silently here —
            # Settings is where that state is reported, because the person
            # who needs to know is the owner, not the log.
            if recips:
                from ..notify import recipient_invites
                ok = recipient_invites.accepted(tenant_id, recips)
                recips = [r for r in recips if r.lower() in ok]
            # NOTE the missing `if recips:` — this branch is entered because
            # the household CONFIGURED a list, and it must be left with the
            # owner, never widened. The filters above can empty `recips`
            # (a pending invite, or on hosted an address with no account);
            # a configured list that filters to nothing means "just the
            # owner" — it can never mean "every user on the tenant".
            # the owner is on the list by construction — but on hosted only a
            # VERIFIED owner: household mail goes only where mailbox control
            # is proven, and both branches must apply the same rule
            owner_proof = (" AND verified_at IS NOT NULL"
                           if env_flag("OIKONOME_HOSTED") else "")
            owners = [r["email"] for r in admin.execute(
                "SELECT email FROM users WHERE tenant_id=%s "
                "AND role='owner'" + owner_proof + " ORDER BY created_at",
                (tenant_id,)
            ).fetchall()]
            # UNCONDITIONAL return. Guarded on owners-or-recips, a
            # tenant with no owner row AND an emptied list falls through to
            # the every-verified-user query below — the exact widening the
            # paragraph above says can never happen. A configured list that
            # filters to nothing means the owner; with no owner either it
            # means nobody, and nobody is the safe answer for a
            # household's balances.
            out, seen = [], set()
            for e in owners + list(recips):
                if e.lower() not in seen:
                    seen.add(e.lower())
                    out.append(e)
            return _unmuted(out)
        rows = admin.execute(
            "SELECT email FROM users WHERE tenant_id=%s "
            "AND verified_at IS NOT NULL", (tenant_id,)).fetchall()
        if not rows and not env_flag("OIKONOME_HOSTED"):
            # Self-host only: an install whose users never clicked a verify
            # link (there may be no mail set up to send one) must not go
            # silent. On hosted, verification is the proof of mailbox
            # control that gates household mail — an all-unverified tenant
            # (owner never confirmed, member claimed with a typed address)
            # gets nothing until someone verifies, because the typed
            # address may be a stranger's.
            rows = admin.execute("SELECT email FROM users WHERE tenant_id=%s",
                                 (tenant_id,)).fetchall()
        return _unmuted([r["email"] for r in rows])
    finally:
        admin.close()


def _recipients_and_raw(conn, tenant_id: str) -> tuple[list[str], list[str]]:
    """`(deliverable, raw)` from ONE computation.

    `email_tenant` needs both — the addresses to mail, and whether there was
    anyone at all before the undeliverable ones were held (the heartbeat
    keys off that). Calling `_recipients` and then `_recipients_raw` runs
    the whole thing twice per tenant per cadence, since the first calls the
    second: two unpooled admin connections, two hosted-member queries, two
    invite lookups."""
    from ..notify import delivery
    raw = _recipients_raw(conn, tenant_id)
    send_to, held = delivery.hold(raw)
    if held:
        log.warning("tenant %s: holding scheduled mail for %s — the provider "
                    "reports these addresses as undeliverable; the account "
                    "owner sees this in Settings → Email",
                    tenant_id, ", ".join(held))
    return send_to, raw


def _recipients(conn, tenant_id: str) -> list[str]:
    """`_recipients_raw`, minus the addresses known to be
    undeliverable.

    Every scheduled send to a hard-bounced address is a refusal at
    submission, once a day, forever, teaching nobody anything — and it
    spends the sender's reputation on mail that cannot arrive. This is the
    only place the filter belongs: every caller of this function is a
    SCHEDULED send. Transactional mail (verification, reset, sign-in
    unlock) never comes through here and is always attempted, because those
    are the messages someone needs while they are FIXING the address.

    Held addresses are logged every time rather than once, because the
    failure this guards against is one nobody could see: a silent filter
    would be the same fault wearing a different hat. The person's own
    signal is the banner and the Settings row, not this log line."""
    return _recipients_and_raw(conn, tenant_id)[0]


PRODUCTS_MAX_AGE_H = 20  # plaid liabilities/holdings refresh ~daily; 20h (not
#                          24) so the refresh doesn't creep an hour later each
#                          day on an hourly sweep


def _products_due(conn, now_utc: dt.datetime | None = None) -> bool:
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    hb = conn.execute(
        "SELECT ran_at FROM job_runs WHERE job='plaid-products'").fetchone()
    return hb is None or (now - hb["ran_at"]).total_seconds() \
        > PRODUCTS_MAX_AGE_H * 3600


def _item_needs_first_products(conn, item_id: str) -> bool:
    """True when this Item has investment (or credit/loan) accounts but no
    holdings/liabilities rows yet — first pull must not wait on the daily
    products heartbeat (a brokerage Item linked after the daily pass would
    otherwise show no positions for hours)."""
    has_inv = conn.execute(
        "SELECT 1 FROM accounts WHERE item_id=%s AND type='investment' "
        "LIMIT 1", (item_id,)).fetchone()
    if has_inv:
        has_h = conn.execute(
            "SELECT 1 FROM holdings h JOIN accounts a ON a.id=h.account_id "
            "WHERE a.item_id=%s LIMIT 1", (item_id,)).fetchone()
        if not has_h:
            return True
    has_debt = conn.execute(
        "SELECT 1 FROM accounts WHERE item_id=%s "
        "AND type IN ('credit','loan') LIMIT 1", (item_id,)).fetchone()
    if has_debt:
        has_l = conn.execute(
            "SELECT 1 FROM liabilities l JOIN accounts a ON a.id=l.account_id "
            "WHERE a.item_id=%s LIMIT 1", (item_id,)).fetchone()
        if not has_l:
            return True
    return False


def _sync_plaid_products(conn, tenant_id: str, plaid_items: list,
                         results: dict, mark=None) -> None:
    """Liabilities + investment-holdings for healthy plaid items.

    Runs when the daily products heartbeat is due (PRODUCTS_MAX_AGE_H) OR
    when an individual Item still needs its first products fill (new
    brokerage/credit Item after a prior tenant-wide products pass).
    Heartbeats only on a full daily pass that succeeds — first-fill-only
    runs leave the schedule due so the next hourly sweep can still do the
    routine refresh."""
    if not plaid_items:
        return
    daily = _products_due(conn)
    ok = True
    total = {"liabilities": 0, "holdings": 0}
    for it in plaid_items:
        if not (results.get(it["id"]) or "").startswith("ok:"):
            continue                 # broken item: txn sync already failed
        if not daily and not _item_needs_first_products(conn, it["id"]):
            continue
        try:
            # every phase after the item counter fills reads as "finishing
            # up…", and a silent phase longer than SYNC_STALE_AFTER makes
            # /connections call the run stale and drop the chip mid-sync.
            # Bumping updated_at per item keeps the run alive and the phase
            # honest.
            if mark:
                mark({"phase": "products"})
            r = plaid.sync_products(conn, it["id"])
            total["liabilities"] += r["liabilities"]
            total["holdings"] += r["holdings"]
        except Exception as e:                   # noqa: BLE001 — isolate items
            ok = False
            log.warning("plaid products failed tenant=%s item=%s: %s",
                        tenant_id, it["id"], e)
    # Daily pass: stamp the heartbeat when due, even if every item was
    # broken on txn sync (nothing to pull — still a completed products
    # cycle). First-fill-only runs (not daily) leave the schedule alone.
    if daily and ok:
        alerts.heartbeat(conn, "plaid-products",
                         f"{total['liabilities']} liabilities, "
                         f"{total['holdings']} holdings")


def _emit(progress, stage: str, payload: dict) -> None:
    """Best-effort progress callback (wizard sync step) — a
    progress-writer failure must never fail the sync itself."""
    if progress is None:
        return
    try:
        progress(stage, payload)
    except Exception as e:                       # noqa: BLE001
        log.warning("progress callback failed: %s", e)


def _item_txn_count(conn, item_id: str) -> int:
    """Ledger txn count for a connection (non-removed). Wizard UI shows
    this — not the per-run `added` delta — so a re-sync of already-full
    Items doesn't display '0 txns' after a successful first pull."""
    row = conn.execute(
        "SELECT count(*) AS n FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id "
        "WHERE a.item_id=%s AND COALESCE(t.removed, 0)=0",
        (item_id,)).fetchone()
    return int(row["n"] or 0) if row else 0


def _progress_mark(conn, state: str | None, merge: dict | None,
                   reset: bool = False) -> None:
    """Record sync liveness in job_progress so ANY door's sync is visible.

    Best-effort by design: a sync must never fail because its progress row
    could not be written. `merge` is concatenated onto the existing payload
    (jsonb ||) rather than assigned, so a caller writing its own richer
    snapshot into the same row keeps it."""
    from ..engine.compat import jsonb
    try:
        sets, args = [], []
        if state:
            sets.append("state=%s")
            args.append(state)
        if merge is not None:
            # job_progress.progress, QUALIFIED: inside ON CONFLICT DO UPDATE
            # a bare `progress` could mean the target row or excluded.progress,
            # and Postgres rejects it as ambiguous rather than guessing
            sets.append(
                "progress=%s::jsonb" if reset else
                "progress=COALESCE(job_progress.progress,'{}'::jsonb) || %s::jsonb")
            args.append(jsonb(merge))
        sets.append("updated_at=now()")
        if reset:
            sets.append("started_at=now()")
        conn.execute(
            f"INSERT INTO job_progress (id, state, progress) "
            f"VALUES ('sync', %s, %s::jsonb) "
            f"ON CONFLICT (tenant_id, id) DO UPDATE SET {', '.join(sets)}",
            (state or "running", jsonb(merge or {}), *args))
    except Exception as e:                                   # noqa: BLE001
        # WARNING, not debug. This is best-effort by design — a sync must
        # never fail because its progress row would not write — but at debug
        # level that design turns a hard SQL error into an invisible no-op:
        # the counter sits at 0 through a whole multi-bank pull and the only
        # symptom is a progress bar that never moves. Swallowing the failure
        # is right; hiding it is not.
        log.warning("sync progress mark failed: %s", e)


# The tenant sync lock is single-flight and SKIP-not-retry: a sync that
# finds it held returns "already-running" and goes away. That is right for
# the lock — waiting behind a multi-minute pull holds a worker slot — but
# wrong for the tenant, who would then wait up to an hour for the next
# sweep even though something (a Plaid webhook, a fresh bank link, the ↻
# button) has just said there is data to fetch.
#
# The nudge is the cheap half of the two designs that close it: the skipped
# caller records a request, and the holder answers it with one more pass
# before releasing the lock. The other design — a real queue, or a waiting
# lock with a deadline — buys ordering guarantees nothing here needs.
#
# It lives in `job_progress` under an id of its own rather than in Redis:
# the same transaction-free row every door can already reach, durable
# across a worker restart, tenant-scoped by RLS like everything else, and
# read by no other code (every consumer of that table looks its own id up).
# One bit per tenant is all it has to carry — WHO asked does not change the
# answer, which is always "one more pass".
_SYNC_NUDGE = "sync-nudge"


def _nudge_sync(conn) -> None:
    """Ask the sync in flight for one more pass before it lets go.

    Best-effort, like `_progress_mark`: a caller that was only being turned
    away politely must not turn into an error because a bookkeeping row
    would not write."""
    try:
        conn.execute(
            "INSERT INTO job_progress (id, state, progress) "
            f"VALUES ('{_SYNC_NUDGE}', 'requested', '{{}}'::jsonb) "
            "ON CONFLICT (tenant_id, id) DO UPDATE SET state='requested', "
            "updated_at=now()")
    except Exception as e:                                   # noqa: BLE001
        log.warning("sync nudge failed: %s", e)


def _clear_sync_nudge(conn) -> None:
    """Drop a pending request. Called once the holder has the lock and is
    about to pull: anything asked for before that pull begins is answered
    by the pull itself, and leaving it would make the NEXT sweep pay for a
    second pass that nobody is waiting for."""
    try:
        conn.execute(f"DELETE FROM job_progress WHERE id='{_SYNC_NUDGE}'")
    except Exception as e:                                   # noqa: BLE001
        log.warning("sync nudge clear failed: %s", e)


def _claim_sync_nudge(conn) -> bool:
    """True when a sync was turned away while this one ran — and the
    request is consumed in the same statement, so two claims can never
    answer one nudge twice."""
    try:
        return conn.execute(
            f"DELETE FROM job_progress WHERE id='{_SYNC_NUDGE}' RETURNING 1"
        ).fetchone() is not None
    except Exception as e:                                   # noqa: BLE001
        log.warning("sync nudge claim failed: %s", e)
        return False


def _alert_failovers(conn, tenant_id: str) -> None:
    """A linked account's favorite source is down and its backup is
    serving: log the in-app alert and mail the household once per outage.
    The alert is dated on the HOUSEHOLD's day — the Alerts page shows this
    date next to the Today page's, and both must name the same day. The
    mail is one message per recipient (`send_each`), never the
    To:sender+Bcc blast: a per-address delivery failure has to be visible
    and recorded, and the footer must offer the unsubscribe every other
    household mail carries."""
    from ..engine import budget, links as links_mod
    from ..web import report as report_mod
    from .. import localtime
    failovers = links_mod.failover_pending(conn)
    if not failovers:
        return
    names = {r["id"]: r["nm"] for r in conn.execute(
        "SELECT id, COALESCE(display_name, name) AS nm FROM accounts")}
    lines = [f"{names.get(f['down'], f['down'])}: primary source is down — "
             f"showing {names.get(f['using'], f['using'])} instead"
             for f in failovers]
    # the in-app alert always lands; the email only when SMTP is
    # configured, and the edge is stamped ONLY after a successful send so
    # a mail failure retries next sweep. partial=True: this contributes
    # the failover rows only — the full-snapshot default deactivated AND
    # un-dismissed every OTHER active alert, which the next Today load
    # then re-activated with dismissals reset.
    alerts.log(conn, [{"kind": "source-failover", "severity": "warn",
                       "message": ln} for ln in lines],
               localtime.now_local(budget.load_config(conn)).date(),
               partial=True)
    smtp = report_mod.resolve_smtp(conn)
    if not smtp["configured"]:
        return
    # nobody deliverable (the only address is held as bouncing) is the
    # same as no mail server: the in-app alert stands, the edge stays
    # unstamped, and the mail goes out on the first sweep after the
    # address is fixed
    recipients = _recipients(conn, tenant_id)
    if not recipients:
        return
    import html as _html2
    subject = "Oikonome: an account source failed over"
    results = report_mod.send_each(
        subject,
        "\n".join(lines) + "\n\nData keeps flowing from the backup source; "
        "fix the primary connection on the Accounts page.",
        "<p>" + "<br>".join(_html2.escape(x) for x in lines)
        + "</p><p>Data keeps flowing from the backup source; fix the "
        "primary connection on the Accounts page.</p>",
        recipients, smtp=smtp, unsubscribe=tenant_id)
    # every recipient refused = nothing went out, retry next sweep; a
    # partial failure is that address's own delivery record, not a reason
    # to mail the others again
    if not any(r["ok"] for r in results):
        return
    links_mod.mark_failover_alerted(conn, [f["group_id"] for f in failovers])
    # push rides the SAME stamped edge as the email — a push before the
    # stamp would repeat every sweep on an SMTP-less install
    _push_alert(tenant_id, subject,
                lines[0] if len(lines) == 1 else
                f"{len(lines)} accounts are serving from their backup source.")


def _sync_pass(conn, tenant_id: str, since_days: int, progress,
               results: dict[str, str], *, first: bool = True) -> int:
    """ONE pull over every aggregator item of a tenant. Returns the count
    of items that failed, and fills `results` in place.

    Split out of `sync_tenant` so the lock holder can run it a SECOND time
    when a sync was skipped while this one was in flight — see the nudge
    helpers above. A pass re-reads the item list, so the second one picks
    up a connection linked since the first started, which is often exactly
    why the skipped sync was asking.

    `transitions` is per PASS, never shared between them: the link
    break/recovery email is built from it, and carrying the first pass's
    observations into the second would mail the same outage twice.
    """
    transitions: list[dict] = []
    failures = 0

    # coinbase items are pull-synced only when CDP credentials are
    # stored — push-fed coinbase items (the script door) carry no
    # access_token and are normally status='archived', but a restored
    # export recreates them as 'restored'; never try to pull those.
    items = conn.execute(
        "SELECT id, aggregator, institution_name, status FROM items "
        "WHERE aggregator IN ('simplefin','plaid','coinbase') "
        "AND COALESCE(status,'') != 'archived' "
        "AND NOT (aggregator='coinbase' AND access_token IS NULL)"
    ).fetchall()
    # Every door into a sync records it, so the UI can say a sync is
    # happening no matter who started it — including the hourly cron, the
    # sync a user experiences most of the time; otherwise the header chip
    # goes on reading "synced 58m ago" while a pull is running.
    #
    # `progress || …` MERGES rather than replaces: the wizard's caller
    # writes its own richer per-item snapshot into this same row, and a
    # plain assignment here would wipe it mid-run.
    # only the FIRST pass resets the snapshot: the nudged second pass
    # continues the same sync and must not wipe the wizard's per-item merge
    _progress_mark(conn, "running", {"done": 0, "total": len(items)},
                   reset=first)
    done = 0
    for it in items:
        _emit(progress, "item", {
            "id": it["id"],
            "name": it["institution_name"] or it["id"],
            "status": "pending",
            "transactions": _item_txn_count(conn, it["id"])})
    for it in items:
        name = it["institution_name"] or it["id"]
        _emit(progress, "item", {
            "id": it["id"], "name": name, "status": "running",
            "transactions": _item_txn_count(conn, it["id"])})
        new_status = "ok"
        try:
            if it["aggregator"] == "plaid":
                r = plaid.sync(conn, it["id"])
                results[it["id"]] = f"ok:{r['added']}"
            elif it["aggregator"] == "coinbase":
                from ..sync import coinbase as _cb
                r = _cb.sync(conn, it["id"])
                results[it["id"]] = f"ok:{r['transactions']}"
            else:
                token = sync_base.get_access_token(conn, it["id"])
                if not token:
                    results[it["id"]] = "no-token"
                    _emit(progress, "item", {
                        "id": it["id"], "name": name,
                        "status": "error", "transactions": 0,
                        "error": "no token"})
                    continue                 # nothing observed: no transition
                r = simplefin.sync(
                    conn, it["id"], token,
                    since=dt.date.today() - dt.timedelta(days=since_days))
                results[it["id"]] = f"ok:{r['transactions']}"
            _emit(progress, "item", {
                "id": it["id"], "name": name, "status": "ok",
                "transactions": _item_txn_count(conn, it["id"])})
        except Exception as e:               # noqa: BLE001 — isolate items
            failures += 1
            results[it["id"]] = f"error:{type(e).__name__}"
            _emit(progress, "item", {
                "id": it["id"], "name": name, "status": "error",
                "transactions": _item_txn_count(conn, it["id"]),
                "error": type(e).__name__})
            new_status = (f"error:{e.code}"
                          if isinstance(e, plaid.PlaidError)
                          else f"error:{type(e).__name__}")
            # plaid/simplefin set this themselves on THEIR errors; this
            # covers exceptions that never reached their handlers (and
            # re-setting the same value is harmless)
            #
            # Never downgrade 'archived': the item list was
            # read at the top of this sweep, and an Item released by the
            # reaper — or disconnected by the owner — since then is GONE
            # upstream. Stamping 'error:...' over 'archived' resurrects it
            # into every future sweep, where it can only keep failing.
            #
            # Nor 'restored': a restore's credential-less shell fails here
            # every hour by design (NO_TOKEN) until the bank is linked again,
            # and the status is what keeps it out of the institution count
            # and shows it as waiting to be reconnected. Stamping an error
            # over it would put the shell back in the count within the hour.
            conn.execute("UPDATE items SET status=%s WHERE id=%s "
                         "AND COALESCE(status,'') NOT IN ('archived', 'restored')",
                         (new_status, it["id"]))
            # a SimpleFIN access token is a credentialed URL and httpx
            # embeds it in the exception text — scrub before logging
            # (the DB sink log_sync redacts on its own path)
            log.warning("sync failed tenant=%s item=%s: %s",
                        tenant_id, it["id"],
                        sync_base.redact_url_credentials(str(e)))
        transitions.append({"name": it["institution_name"] or it["id"],
                            "old": it["status"], "new": new_status})
        # count it here, not after the loop: a denominator that sits at
        # 0 until every bank has answered and then jumps straight to the
        # total is not progress, it is a spinner wearing a number
        done += 1
        _progress_mark(conn, None, {"done": done})
    # MX is tenant-scoped (one user, many members) — sync it
    # alongside the per-item aggregators when credentials exist
    try:
        from ..sync import mx as _mx
        if _mx.creds(conn) is not None:
            _emit(progress, "item", {"id": "mx", "name": "MX",
                                     "status": "running",
                                     "transactions": 0})
            # capture mx-* item statuses around the sync so MX
            # break/recovery feeds the SAME link_alert email as
            # plaid/simplefin, not only as an in-app status dot.
            def _mx_status():
                return {r["id"]: (r["institution_name"], r["status"])
                        for r in conn.execute(
                            "SELECT id, institution_name, status FROM "
                            "items WHERE aggregator='mx'").fetchall()}
            before = _mx_status()
            try:
                r = _mx.sync(conn)
                results["mx"] = f"ok:{r['transactions']}"
                # MX is multi-member — sum ledger under mx items
                n_mx = conn.execute(
                    "SELECT count(*) AS n FROM transactions t "
                    "JOIN accounts a ON a.id=t.account_id "
                    "JOIN items i ON i.id=a.item_id "
                    "WHERE i.aggregator='mx' "
                    "AND COALESCE(t.removed,0)=0").fetchone()["n"]
                _emit(progress, "item", {
                    "id": "mx", "name": "MX", "status": "ok",
                    "transactions": int(n_mx or 0)})
            finally:
                after = _mx_status()
                for iid, (name, new_st) in after.items():
                    old_st = before.get(iid, (None, None))[1]
                    transitions.append({
                        "name": name or iid,
                        "old": old_st, "new": new_st})
    except Exception as e:                   # noqa: BLE001
        failures += 1
        results["mx"] = f"error:{type(e).__name__}"
        _emit(progress, "item", {"id": "mx", "name": "MX",
                                 "status": "error", "transactions": 0,
                                 "error": type(e).__name__})
        log.warning("mx sync failed tenant=%s: %s", tenant_id,
                    sync_base.redact_url_credentials(str(e)))
    _progress_mark(conn, None, {"done": len(items)})   # incl. the MX pass
    if items and failures == 0:
        alerts.heartbeat(conn, "sync", f"{len(items)} items")
    # proactive email the moment a connection breaks or recovers —
    # a mail failure must never break the sync
    if transitions:
        from ..notify import webhooks as _wh
        _wh.emit(conn, "connection.changed", {
            "connections": [{"name": t["name"], "from": t["old"],
                             "to": t["new"]} for t in transitions]})
    try:
        built = link_alert.build_alert(transitions)
        if built:
            from ..web import report as _report
            sent = link_alert.notify(transitions,
                                     _recipients(conn, tenant_id),
                                     smtp=_report.resolve_smtp(conn),
                                     unsubscribe=tenant_id)
            if sent:
                log.info("link-alert emailed tenant=%s: %s",
                         tenant_id, sent)
            # same transition edge as the email, so a steady-state
            # outage pushes exactly once too
            _push_alert(tenant_id, built[0],
                        "Details on the Accounts page.")
    except Exception as e:                   # noqa: BLE001
        log.error("link-alert failed tenant=%s: %s", tenant_id, e)
    # a linked account's favorite source went down (its
    # backup is already serving) — tell the owner once per outage
    try:
        _alert_failovers(conn, tenant_id)
    except Exception as e:                   # noqa: BLE001
        log.error("failover alert failed tenant=%s: %s", tenant_id, e)
    _sync_plaid_products(
        conn, tenant_id,
        [it for it in items if it["aggregator"] == "plaid"], results,
        mark=lambda merge: _progress_mark(conn, None, merge))
    # categorize what we just pulled — classify any new unknown
    # merchants (the sparse-SimpleFIN case) and apply the cached map, so
    # rows are categorized within the hour and by the time onboarding's
    # force-sync returns, not only after the nightly run. Best-effort:
    # a categorization failure must never fail the sync.
    # the ↻ button runs the same pass on its own thread outside the
    # sync lock, so the pass has a lock of its own (sync_base
    # .categorize_lock); a held lock means skip, the holder covers it
    from ..sync import base as _sync_base
    with _sync_base.categorize_lock(conn, tenant_id) as _held:
      if not _held:
        log.info("sync-time categorize skipped tenant=%s — a pass is "
                 "already running", tenant_id)
      else:
        try:
            from ..engine import llm_categorize as _llm
            _progress_mark(conn, None, {"phase": "categorize"})
            cat = _llm.categorize_new(
                conn,
                progress=(lambda done, total: _emit(
                    progress, "categorize", {"done": done, "total": total})))
            if cat.get("pending_merchants"):
                # final emit carries the failure count so the wizard can
                # say "categorization failed" instead of a done spinner
                _emit(progress, "categorize", {
                    "done": cat["pending_merchants"],
                    "total": cat["pending_merchants"],
                    "failed": cat.get("unparseable", 0)})
            if cat.get("merchants_classified") or cat.get("rows_updated"):
                results["categorize"] = (
                    f"+{cat['merchants_classified']}m/{cat['rows_updated']}rows")
        except Exception as e:                       # noqa: BLE001
            log.warning("sync-time categorize failed tenant=%s: %s",
                        tenant_id, e)
        # a bill's transaction category lands on this hour's new charges
        # too (recent rows only — the nightly pass does the whole ledger)
        try:
            bills.apply_txn_categories(conn, days=120)
        except Exception as e:                       # noqa: BLE001
            log.warning("bill txn-category apply failed tenant=%s: %s",
                        tenant_id, e)
        # a hand split whose row just posted for a different amount no
        # longer adds up; the parts are dropped rather than counted as a
        # total the bank never charged
        try:
            from ..engine import splits as _splits
            n = _splits.drop_stale(conn)
            if n:
                log.info("dropped %d stale split parts tenant=%s", n, tenant_id)
        except Exception as e:                       # noqa: BLE001
            log.warning("stale split sweep failed tenant=%s: %s", tenant_id, e)
    return failures


def _recent_txn_ids(conn) -> set[str] | None:
    """The ids of the recent rows before a sync, so the rows the sync adds
    can be named afterwards. None when no webhook wants them — the set is
    a few hundred ids at most (the sync window is 30 days; 45 covers a
    backdated post), but there is no reason to read it for nobody."""
    from ..notify import webhooks as _wh
    if not (_wh.wanted(conn, "transactions.new")
            or _wh.wanted(conn, "sync.completed")):
        return None
    return {r["id"] for r in conn.execute(
        "SELECT id FROM transactions "
        "WHERE date >= CURRENT_DATE - 45").fetchall()}


def _webhooks_after_sync(conn, results: dict, before_ids: set | None,
                         tenant_id: str = "") -> None:
    """sync.completed and transactions.new for the household's webhooks.
    Best-effort — a failure here never fails the sync."""
    if before_ids is None or not results:
        return
    try:
        from ..notify import webhooks as _wh
        names = {r["id"]: r["institution_name"] for r in conn.execute(
            "SELECT id, institution_name FROM items").fetchall()}
        items = []
        for iid, st in results.items():
            ok = str(st).startswith("ok:")
            items.append({"id": iid, "name": names.get(iid, iid),
                          "ok": ok,
                          "error": None if ok else str(st).partition(":")[2] or str(st)})
        # the payee as the ledger displays it (rename, merchant row), the
        # one definition every surface reads
        from ..engine import merchant_sql as _msql
        new_rows = conn.execute(
            f"""SELECT t.id, t.date, t.amount,
                       {_msql.DISPLAY_MERCHANT} AS payee,
                       t.name AS bank_text, t.account_id,
                       COALESCE(a.display_name, a.name) AS account, t.pending,
                       COALESCE(t.category_override, t.category_primary) AS category
                  FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
                  {_msql.MC_JOIN}
                 WHERE t.date >= CURRENT_DATE - 45 AND t.removed = 0
                   AND NOT (t.id = ANY(%s))
                 ORDER BY t.date DESC, ABS(t.amount) DESC""",
            (list(before_ids),)).fetchall()
        _wh.emit(conn, "sync.completed", {
            "connections": items, "new_transactions": len(new_rows),
            "ok": all(i["ok"] for i in items)})
        if new_rows:
            _wh.emit(conn, "transactions.new", {
                "count": len(new_rows),
                "transactions": [dict(r) for r in new_rows[:_wh.LIST_MAX]]})
    except Exception as e:                                   # noqa: BLE001
        log.warning("webhooks after sync failed tenant=%s: %s",
                    tenant_id, e)


def sync_tenant(tenant_id: str, since_days: int = 30, progress=None) -> dict:
    """Pull every aggregator item for one tenant. Returns per-item results.
    Observes item-status transitions and emails the connection
    break/recovery alert (origin link_alert.py semantics); roughly once a
    day also refreshes plaid liabilities + holdings.

    `progress(stage, payload)` (optional) receives live updates for the
    wizard's Sync step: stage 'item' {id, name, status, transactions} per
    connection as it runs, stage 'categorize' {done, total} during the
    post-pull LLM pass. ``transactions`` is the **ledger total** for the
    item (not this-run added-only)."""
    conn = tenancy.tenant_connect(tenant_id)
    results: dict[str, str] = {}
    failures = 0
    locked = False
    try:
        # Single-flight per tenant. Four doors reach this function
        # — the hourly cron sweep, /api/jobs/sync, /api/jobs/sync/start and
        # the wizard's sync-now — with nothing stopping two from running at
        # once for the same tenant. Overlapping runs clobber items.tx_cursor
        # (each writes the cursor it started from, so one run's progress is
        # lost and rows are re-pulled or skipped) and both observe the same
        # status transition, so the connection break/recovery email goes out
        # twice.
        #
        # A session-level TRY lock, not a waiting one: a second sync arriving
        # while one is in flight should return immediately saying so, not
        # queue up behind a job that may take minutes. Released in `finally`.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            # Skipping is right — queueing behind a run that can take
            # minutes holds a worker slot — but skipping SILENTLY left this
            # caller's reason to sync (a webhook's new charges, a fresh
            # link, a person pressing ↻) waiting for the next hourly sweep.
            # Leave a nudge instead: the holder answers it before it lets go.
            _nudge_sync(conn)
            log.info("sync already running for tenant %s — skipping, and "
                     "asking the run in flight for one more pass", tenant_id)
            return {"_status": "already-running"}
        # Any nudge recorded BEFORE this moment is already answered by the
        # pass about to run — clear it, or a leftover request would buy
        # every later sweep a second pass for ever.
        _clear_sync_nudge(conn)
        before_ids = _recent_txn_ids(conn)
        try:
            failures += _sync_pass(conn, tenant_id, since_days, progress,
                                   results)
            # A sync that arrived DURING that pass recorded a nudge instead
            # of going away for an hour. Claiming it clears it; answering it
            # is one more pass, here, while the lock is still held — so the
            # webhook, the ↻ button or the wizard that was turned away gets
            # its data now. Exactly one extra pass: a nudge arriving during
            # THAT one is left for the next sweep, because a holder that
            # kept chasing nudges would never release the lock on a busy
            # tenant.
            if _claim_sync_nudge(conn):
                log.info("sync for tenant %s was asked for again while it "
                         "ran — making one more pass", tenant_id)
                failures += _sync_pass(conn, tenant_id, since_days, progress,
                                       results, first=False)
        except Exception:                                    # noqa: BLE001
            # a pass that died outside its own per-item handlers is still a
            # failure, and the progress row in the `finally` must not settle
            # as "done" over it
            failures += 1
            raise
        _webhooks_after_sync(conn, results, before_ids, tenant_id)
        return results
    finally:
        # settle the progress row for whoever is watching. State only —
        # never the payload, because the wizard's caller writes its results
        # into `progress` after this returns and must not be clobbered.
        if locked:
            try:
                _progress_mark(conn, "error" if failures else "done", None)
            except Exception:                                # noqa: BLE001
                pass
        # release the single-flight lock before returning the connection to
        # the pool — a session-level advisory lock outlives the transaction,
        # and a pooled connection handed back still holding it would lock
        # that tenant out of syncing until the backend died
        if locked:
            if tenancy.release_lock(conn, f"oikonome:sync:{tenant_id}"):
                conn = None                      # type: ignore[assignment]
        if conn is not None:
            conn.close()


def nightly_tenant(tenant_id: str) -> dict:
    """Recurring detection + drift for one tenant."""
    conn = tenancy.tenant_connect(tenant_id)
    locked = False
    try:
        # Single-flight per tenant — the same TRY-lock shape as sync_tenant
        # and _email_if_due, for the same reason: two doors reach this
        # function (the nightly cron sweep and the admin console's run-job
        # enqueue, which creates a fresh job per click) with nothing stopping
        # two runs for the same tenant overlapping. Both read the same
        # savings_goal_state before either writes it back, so both observe
        # the same milestone crossing and each sends the milestone email —
        # the config write dedupes state, not a send that already fired.
        # Skipping is right rather than waiting: the run already in flight
        # is doing this exact idempotent sweep, so a second entrant queueing
        # behind it would only repeat the work it just watched finish.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:nightly:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            log.info("nightly already running for tenant %s — skipping",
                     tenant_id)
            return {"_status": "already-running"}
        # freeze the CURRENT month's budget + bill schedule (local date —
        # the month must
        # not turn at UTC midnight while the household is still in the
        # 31st). Upserted every night, so the last write before the month
        # rolls is the budget the month closed under; past months are never
        # touched. First so a failure deeper in the sweep can't skip it.
        try:
            from ..engine import budget as _bsnap
            from .. import localtime
            _bsnap.snapshot_month(
                conn, localtime.now_local(_bsnap.load_config(conn)).date())
        except Exception as e:                   # noqa: BLE001
            log.warning("budget snapshot failed tenant=%s: %s", tenant_id, e)
        # the activity log keeps two years; older rows go quietly
        try:
            from ..engine import activity as _activity
            _activity.prune(conn)
        except Exception as e:                   # noqa: BLE001
            log.warning("activity prune failed tenant=%s: %s", tenant_id, e)
        # settled webhook deliveries keep a week
        try:
            from ..notify import webhooks as _wh
            _wh.prune(conn)
        except Exception as e:                   # noqa: BLE001
            log.warning("webhook prune failed tenant=%s: %s", tenant_id, e)
        try:
            stats = bills.run(conn)
        except Exception as e:                   # noqa: BLE001
            # Unguarded, this is the one step that can cost a household
            # EVERYTHING else: a single tenant-specific data defect here
            # aborts nightly_tenant outright, so the Plaid cross-check, the
            # amazon/dedup/LLM chain, the receipt parse, the retrain and
            # the savings-goal emails are all skipped — every night, for as
            # long as the defect stands, with one log line to show for it.
            # Isolated like its siblings, carrying empty counters so the
            # rest of the sweep still runs.
            log.warning("recurring detection failed tenant=%s: %s",
                        tenant_id, e)
            stats = {"proposed_add": 0, "proposed_remove": 0,
                     "proposed_change": 0, "proposed_income": 0,
                     "drift_applied": 0, "superseded": 0, "due_advanced": 0,
                     "groups": 0, "error": "recurring detection failed"}
        # Plaid's own recurring streams as a cross-check on detection —
        # proposals only, never auto-applied; off with plaid_recurring=false
        try:
            from ..engine import budget as _bcfg
            from ..sync import plaid as _plaid
            if _bcfg.load_config(conn).get("plaid_recurring", True) is not False:
                streams = []
                # Healthy items only. The hourly sync deliberately retries
                # broken ones — a re-auth or an outage clears by being
                # tried again, so trying IS how recovery is noticed. This
                # cross-check has no such duty: an item sitting in
                # login_required or error:* would spend a Plaid round trip
                # per night to be told the same thing the sync already
                # knows, and a shell item restored from an export has no
                # access token at all.
                for it in conn.execute(
                        "SELECT id FROM items WHERE aggregator='plaid' "
                        "AND COALESCE(status,'ok') = 'ok'").fetchall():
                    # per-item isolation: one item's transient error would
                    # otherwise abort the whole tenant's cross-check, so a
                    # single sick connection silently costs every OTHER
                    # connection its proposals, every night. (The call itself cannot
                    # be given a shorter deadline from here — the Plaid
                    # client fixes its 120 s timeout at construction and
                    # exposes no per-call override.)
                    try:
                        streams += _plaid.recurring_streams(conn, it["id"])
                    except Exception as e:       # noqa: BLE001
                        log.warning("recurring streams failed tenant=%s "
                                    "item=%s: %s", tenant_id, it["id"], e)
                if streams:
                    stats["plaid_streams"] = bills.propose_from_plaid_streams(conn, streams)
        except Exception as e:                   # noqa: BLE001
            log.warning("plaid recurring cross-check failed tenant=%s: %s",
                        tenant_id, e)
        # what a person wrote on a retired row follows the live charge —
        # the backstop for every retirement path the sync hooks missed
        try:
            from ..sync import reanchor as _ra
            ra = _ra.reanchor_stranded(conn)
            if ra["moved"] or ra["ambiguous"]:
                stats["reanchored"] = ra["moved"]
                stats["reanchor_ambiguous"] = ra["ambiguous"]
        except Exception as e:                       # noqa: BLE001
            log.warning("re-anchor sweep failed tenant=%s: %s", tenant_id, e)
        from ..engine import amazon_match as _am
        from ..engine import costco_match as _cm
        from ..engine import llm_categorize as _llm
        from ..engine import merchant_dedup as _md
        # the Amazon chain (match + the LLM run's classify/summarize) shares
        # a single-flight lock with the hourly sync's categorize_new; the
        # nightly WAITS rather than skips — it is the reconciliation pass
        with _llm.amazon_chain_lock(conn, wait=True):
            _am.run_match(conn)
            _cm.run_match(conn)
            _md.apply(conn)
            # and converge any payee an earlier, unevenly-enriched sync
            # split across two merchants. Bounded per run and journalled
            # like a rename, so it cannot run long and cannot be
            # irreversible; isolated because a repair failing must not cost
            # the household the categorizer run behind it.
            try:
                from ..engine import merchant_split_repair as _msr
                merged = _msr.repair(conn, apply=True)["merges"]
                if merged:
                    stats["merchant_splits_merged"] = len(merged)
            except Exception as e:                   # noqa: BLE001
                log.warning("merchant split repair failed tenant=%s: %s",
                            tenant_id, e)
            # and two live merchants sharing one name become one row
            try:
                from ..engine import merchant_identity as _mi
                folded = _mi.fold_twins(conn)["folded"]
                if folded:
                    stats["merchant_twins_folded"] = folded
            except Exception as e:                   # noqa: BLE001
                log.warning("merchant twin fold failed tenant=%s: %s",
                            tenant_id, e)
            # merchants that look like one business are OFFERED to the
            # person (bounded per night so a big ledger does not flood
            # the Merchants page on the first run)
            try:
                from ..engine import merchant_merge as _mm
                offered = _mm.run(conn)["inserted"]
                if offered:
                    stats["merchant_merges_offered"] = offered
            except Exception as e:                   # noqa: BLE001
                log.warning("merchant merge proposals failed tenant=%s: %s",
                            tenant_id, e)
            _llm.run(conn)
        # parse any stored-but-unparsed receipts (no-op sans LLM)
        try:
            from ..engine import receipts as _receipts
            _receipts.parse_pending(conn)
        except Exception as e:                   # noqa: BLE001
            log.warning("receipt parse sweep failed: %s", e)
        # retrain the categorizer's learned overlay from this tenant's
        # accumulated answers — the loop-closer: a correction now teaches
        # the classifier about the NEXT lookalike merchant, not only the
        # corrected one. Cheap when nothing changed (one count).
        try:
            from ..engine import model_train as _mt
            r = _mt.maybe_train(conn)
            if r.get("status") == "trained":
                log.info("categorizer overlay retrained tenant=%s: %s",
                         tenant_id, r)
            # a shrunken labeled set retires the stored overlay — say so,
            # or a household loses its model with no trace in the logs
            elif r.get("retired"):
                log.info("categorizer overlay retired tenant=%s: %s",
                         tenant_id, r)
        except Exception as e:                   # noqa: BLE001
            log.warning("categorizer retrain failed tenant=%s: %s",
                        tenant_id, e)
        # savings-goal milestones — edge-triggered (25/50/75/100%
        # crossings + on→off pace flips), state in config, email best-effort
        try:
            from ..engine import budget as _budget
            from ..engine import savings as _savings
            cfg = _budget.load_config(conn)
            if _savings.goals(cfg):
                msgs, new_state = _savings.check_milestones(conn, cfg)
                if new_state != (cfg.get("savings_goal_state") or {}):
                    # `config_txn` exists for exactly this call site — "the
                    # settings page saving while the nightly worker seeds
                    # budgets" is the sentence in its own docstring. `cfg`
                    # was read at the top of a sweep that has since run the
                    # LLM categorizer, the merchant dedup and the receipt
                    # parser, so writing the whole document back would
                    # revert anything the owner changed during those
                    # minutes.
                    with _budget.config_txn(conn) as live:
                        live["savings_goal_state"] = new_state
                if msgs:
                    from ..web import report as _report
                    recipients = _recipients(conn, tenant_id)
                    if recipients:
                        plain = "Savings goals:\n\n  " + "\n  ".join(msgs)
                        import html as _html
                        html = ("<p>Savings goals:</p><ul>"
                                + "".join(f"<li>{_html.escape(m)}</li>"
                                          for m in msgs) + "</ul>")
                        _report.send_each("Oikonome: savings goal update",
                                          plain, html, recipients,
                                          smtp=_report.resolve_smtp(conn),
                                          unsubscribe=tenant_id)
        except Exception as e:                   # noqa: BLE001
            log.warning("savings milestone sweep failed: %s", e)
        alerts.heartbeat(conn, "nightly-detect",
                         f"+{stats['proposed_add']} proposals")
        return stats
    finally:
        if locked:
            # MUST unlock explicitly. tenant_connect is POOLED, so close()
            # hands the session back with a session-level advisory lock
            # still on it — the next borrower inherits a lock nobody meant
            # to hold, and every later nightly for this tenant is refused.
            if tenancy.release_lock(conn, f"oikonome:nightly:{tenant_id}"):
                conn = None                      # type: ignore[assignment]
        if conn is not None:
            conn.close()


def script_alert_tenant(tenant_id: str) -> dict:
    """Opt-in stale-collector email. Edge-triggered: one email
    when a watched source crosses 2× its expected interval, guarded by
    alerted_at (which stamp clears on the next push, so a later relapse
    re-alerts)."""
    from ..sync import heartbeat
    from ..web import report
    conn = tenancy.tenant_connect(tenant_id)
    locked = False
    try:
        # Single-flight per tenant, the same TRY-lock shape as
        # `_email_if_due` and for the identical reason: the due-read and
        # the alerted_at stamp that suppresses the next one are separate
        # statements, and two doors reach this sweep (the 04:00 cron and
        # the admin console's run-job enqueue). Two overlapping runs both
        # see alerted_at IS NULL and both send — the conditional stamp
        # dedupes the FLAG, not a mail that already left. A second entrant
        # skips rather than waits: waiting only to send the duplicate this
        # guard exists to prevent would miss the point.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:script-alert:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            log.info("stale-collector alert already running for tenant %s "
                     "— skipping", tenant_id)
            return {"alerted": 0}
        due = [r for r in heartbeat.overdue(conn)
               if r["alerts"] and r["alerted_at"] is None]
        if not due:
            return {"alerted": 0}
        recipients = _recipients(conn, tenant_id)
        if not recipients:
            log.warning("stale collectors for tenant %s but no recipients",
                        tenant_id)
            return {"alerted": 0}
        lines = [
            f"{r['label'] or r['source']} — last push "
            f"{r['age_hours'] / 24:.1f} days ago (expected every "
            f"{r['expected_hours']}h)" for r in due]
        subject = ("Oikonome: a data collector has gone quiet"
                   if len(due) == 1 else
                   f"Oikonome: {len(due)} data collectors have gone quiet")
        plain = ("These scripts stopped pushing data:\n\n  "
                 + "\n  ".join(lines)
                 + "\n\nCheck the host-side script's timer/journal. "
                   "You'll be emailed again only if it recovers and "
                   "then goes quiet again — details on the Doctor page.")
        import html as _html
        html = ("<p>These scripts stopped pushing data:</p><ul>"
                + "".join(f"<li>{_html.escape(line)}</li>" for line in lines)
                + "</ul><p>Check the host-side script's timer/journal. "
                  "You'll be emailed again only if it recovers and then "
                  "goes quiet again — details on the Doctor page.</p>")
        report.send_each(subject, plain, html, recipients,
                         smtp=report.resolve_smtp(conn), unsubscribe=tenant_id)
        _push_alert(tenant_id, subject,
                    lines[0] if len(due) == 1 else
                    "Details on the Doctor page.")
        # Stamp ONLY the rows that are still the ones we alerted about. A
        # collector that pushed during the SMTP conversation cleared its own
        # alerted_at and moved last_push; an unconditional stamp puts the
        # flag straight back on a now-HEALTHY source, and since the flag is
        # only ever cleared by a push, the next time that source goes quiet
        # — when there are by definition no more pushes — it is skipped
        # forever. Edge-triggered means edge-triggered.
        conn.execute(
            """UPDATE script_heartbeats h SET alerted_at=now()
               FROM unnest(%s::text[], %s::timestamptz[])
                    AS t(source, last_push)
               WHERE h.source = t.source
                 AND h.last_push = t.last_push
                 AND h.alerted_at IS NULL""",
            ([r["source"] for r in due], [r["last_push"] for r in due]))
        return {"alerted": len(due)}
    finally:
        # release before the connection goes back to the pool — a
        # session-level advisory lock outlives the transaction, and a
        # pooled connection handed back still holding it would silence
        # this tenant's collector alerts until that backend died
        if locked:
            if tenancy.release_lock(conn, f"oikonome:script-alert:{tenant_id}"):
                conn = None                      # type: ignore[assignment]
        if conn is not None:
            conn.close()


def snapshot_tenant(tenant_id: str) -> dict:
    """Nightly net-worth snapshot + historical-anchor recompute for one
    tenant (reporting_api.snapshot_tenant does the work + heartbeats
    'networth-snapshot'). This is what makes the REAL net-worth trend
    accrue — it must run from day one or the recorded series gets a hole."""
    from ..web import reporting_api
    out = reporting_api.snapshot_tenant(tenant_id)
    if out.get("snapshot"):
        conn = tenancy.tenant_connect(tenant_id)
        try:
            from ..notify import webhooks as _wh
            if _wh.wanted(conn, "networth.snapshot"):
                from ..engine import reporting as _rep
                row = conn.execute(
                    "SELECT date, total FROM networth_snapshot "
                    "ORDER BY date DESC LIMIT 1").fetchone()
                nw = _rep.compute_networth(conn)
                _wh.emit(conn, "networth.snapshot", {
                    "date": row["date"] if row else None,
                    "total": round(float(row["total"]), 2) if row else None,
                    "with_property": round(float(nw.get("full_total") or 0), 2),
                    "by_institution": [{"institution": k, "total": v}
                                       for k, v in nw.get("by_institution") or []]})
        except Exception as e:                   # noqa: BLE001
            log.warning("networth webhook failed tenant=%s: %s", tenant_id, e)
        finally:
            conn.close()
    return out


def _tz():
    """The INSTANCE zone. Household-scoped work must prefer the
    household's own (localtime.tenant_tz(cfg)) — see emails_due."""
    from .. import localtime
    return localtime.instance_tz()


CADENCE_JOBS = {"daily": "daily-email", "weekly": "weekly-email",
                "monthly": "monthly-email", "yearly": "yearly-email"}


def _sched_entry(sched, cadence: str) -> dict:
    """The cadence's schedule entry, or {}. A restore can plant a non-dict
    here (settings are validated on the API write path, not on restore),
    and .get on it crashed the hourly sweep / the send itself."""
    v = sched.get(cadence) if isinstance(sched, dict) else None
    return v if isinstance(v, dict) else {}


def _channels_on(entry: dict) -> bool:
    """A cadence is scheduled when ANY channel wants it — `on`
    stays the email flag (back-compat), sms/push are their own."""
    return bool(entry.get("on") or entry.get("sms") or entry.get("push"))


def _sched_int(entry: dict, key: str, default: int, hi: int) -> int | None:
    """An hour/weekday out of a schedule entry, or None when it is not a
    number in range. The settings door validates these, a restore ZIP
    does not; a bad value raising out of the whole due-check would silence
    EVERY cadence for the household on every sweep, forever."""
    try:
        v = int(entry.get(key, default))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= hi else None


def _stamp_fresh(conn, cfg: dict, now_utc: dt.datetime, job: str,
                 since: dt.datetime) -> bool:
    """Has the household's local day turned since `job` last stamped
    job_runs? `since` is today's local midnight in the zone in effect now.
    Shared by every once-a-local-day send (the cadence emails and the
    daily webhook event) so all of them read "today" the same way."""
    from .. import localtime
    hb = conn.execute("SELECT ran_at, zone FROM job_runs WHERE job=%s",
                      (job,)).fetchone()
    if hb is None:
        return True
    if hb["ran_at"] >= since:
        return False
    # The heartbeat is older than today's local midnight in the zone
    # in effect NOW. It must also be older than that midnight in the
    # zone it was STAMPED in: moving the household to a zone far
    # enough ahead that "now" is already tomorrow's date pushes
    # today's midnight past this morning's send, and the next sweep
    # sends the same day's verdict again. A day has only turned when
    # both zones say so.
    if hb["zone"] and hb["zone"] != localtime.tenant_tz_name(cfg) \
            and localtime.valid_zone(hb["zone"]):
        import zoneinfo
        then = now_utc.astimezone(zoneinfo.ZoneInfo(hb["zone"]))
        then_start = dt.datetime.combine(
            then.date(), dt.time.min, then.tzinfo
        ).astimezone(dt.timezone.utc)
        return hb["ran_at"] < then_start
    return True


def emails_due(conn, now_utc: dt.datetime) -> list[str]:
    """Which cadences should send THIS hour. email_schedule =
    {daily:{on,hour}, weekly:{on,hour,weekday 0=Mon}, monthly:{on,hour}},
    hours in the HOUSEHOLD's local time — its own `timezone` setting when it
    has one, the instance's (OIKONOME_TZ) otherwise. Absent schedule =
    notify.schedule.effective_email_schedule: daily at 07:00 local, the
    same default every client shows; an old document that only carries
    email_send_hour_utc keeps that UTC hour. Each cadence fires at most
    once per local day (job_runs guard)."""
    from ..engine import budget
    from .. import localtime
    cfg = budget.load_config(conn)
    local = now_utc.astimezone(localtime.tenant_tz(cfg))
    day_start = dt.datetime.combine(local.date(), dt.time.min,
                                    local.tzinfo).astimezone(dt.timezone.utc)

    # A brand-new tenant must NOT get its first scheduled email on the day it
    # was created — otherwise signing up mid-afternoon fires the daily verdict
    # within the hour. Hold every cadence until the next local day; the normal
    # hourly sweep then delivers the first one at the configured hour tomorrow.
    owner = conn.execute(
        "SELECT MIN(created_at) AS c FROM users WHERE tenant_id = "
        "current_setting('app.tenant_id', true)::uuid").fetchone()
    if owner and owner["c"] and owner["c"] >= day_start:
        return []

    def fresh(job, since):
        return _stamp_fresh(conn, cfg, now_utc, job, since)

    # fire on the first sweep AT OR AFTER the configured hour (>=,
    # not ==) that hasn't run today. The `fresh(job, day_start)` guard keeps
    # it once per local day, so >= safely catches up when the exact hour was
    # skipped — DST spring-forward (the local hour never existed) or a worker
    # that was down during that hour.
    sched = cfg.get("email_schedule")
    if not isinstance(sched, dict) and "email_send_hour_utc" in cfg:
        hour = _sched_int(cfg, "email_send_hour_utc", 14, 23)
        if hour is None:
            log.warning("email_send_hour_utc is malformed — the daily email "
                        "is skipped until it is re-saved in Settings")
            return []
        if now_utc.hour >= hour and fresh(
                "daily-email",
                dt.datetime.combine(now_utc.date(), dt.time.min,
                                    dt.timezone.utc)):
            return ["daily"]
        return []
    # nothing saved: the default the clients promise, in local time
    from ..notify.schedule import effective_email_schedule
    sched = effective_email_schedule(cfg)
    due = []

    # a webhook subscribed to the daily verdict is a channel too: the
    # event rides the daily cadence's hour even when no email/SMS/push
    # wants it
    from ..notify import webhooks as _wh
    hook_daily = _wh.wanted(conn, "report.daily")

    def slot(entry, cadence):
        # the cadence's local send hour, or None when the entry is off or
        # malformed — a malformed one is skipped and logged rather than
        # allowed to take the other cadences down with it
        if not _channels_on(entry) and not (cadence == "daily"
                                            and hook_daily):
            return None
        hour = _sched_int(entry, "hour", 7, 23)
        if hour is None:
            log.warning("email_schedule.%s has a malformed hour — skipped "
                        "until it is re-saved in Settings", cadence)
        return hour

    d = _sched_entry(sched, "daily")
    h = slot(d, "daily")
    if h is not None and local.hour >= h and fresh("daily-email", day_start):
        due.append("daily")
    w = _sched_entry(sched, "weekly")
    h = slot(w, "weekly")
    wd = _sched_int(w, "weekday", 0, 6)
    if h is not None and wd is not None and local.weekday() == wd \
            and local.hour >= h and fresh("weekly-email", day_start):
        due.append("weekly")
    m = _sched_entry(sched, "monthly")
    h = slot(m, "monthly")
    if h is not None and local.day == 1 and local.hour >= h \
            and fresh("monthly-email", day_start):
        due.append("monthly")
    # annual report card — Jan 1 (the once-per-local-day guard
    # keeps it to one send; the month+day test keeps it to one per year)
    y = _sched_entry(sched, "yearly")
    h = slot(y, "yearly")
    if h is not None and local.month == 1 and local.day == 1 \
            and local.hour >= h and fresh("yearly-email", day_start):
        due.append("yearly")
    return due


def email_due(conn, now_utc: dt.datetime) -> bool:
    """Back-compat shim (the daily cadence only)."""
    return "daily" in emails_due(conn, now_utc)


# the job_runs stamp of the day's report.daily webhook event, separate
# from the daily email's: the two succeed and fail independently
DAILY_HOOK_JOB = "daily-webhook"


def _daily_hook_queued_today(conn, cfg: dict) -> bool:
    """Was today's report.daily already queued — today being the
    household's local day, read exactly as the daily email reads it
    (including the zone the stamp was written in, so moving the household
    east does not make the same day look new and post the report twice)."""
    from .. import localtime
    now_utc = dt.datetime.now(dt.timezone.utc)
    local = now_utc.astimezone(localtime.tenant_tz(cfg))
    day_start = dt.datetime.combine(local.date(), dt.time.min,
                                    local.tzinfo).astimezone(dt.timezone.utc)
    return not _stamp_fresh(conn, cfg, now_utc, DAILY_HOOK_JOB, day_start)


class NoRecipients(RuntimeError):
    """The manual "email me now" button found nobody to mail.

    The scheduled send stays silent in that state on purpose (a household
    with no deliverable address is a decision already made, not work
    pending), but a person who just pressed the button must not be told
    "sent" when nothing left the box — that is how an unverified owner
    sat waiting for a mail that was never going to come."""


def _needs_you(conn, tenant_id: str, d: dict):
    """The daily mail's closing "Needs you" section as a per-address
    function for report.send_each — the household's part (proposals,
    uncategorized charges, alerts) gathered once here while the tenant
    connection is open, the actor list read once, and each copy cut at send
    time with buttons signed for its own address. An address without an
    owner or member account gets nothing: no buttons, no alerts."""
    from ..notify import mailact
    try:
        acts = mailact.gather(conn, d)
        who = mailact.actors(tenant_id)
    except Exception:                                      # noqa: BLE001
        log.exception("tenant %s: needs-you section unavailable", tenant_id)
        return None

    def personal(address: str) -> tuple[str, str]:
        if address.strip().lower() not in who:
            return "", ""
        return mailact.render(acts, tenant_id, address)
    return personal


def email_tenant(tenant_id: str, send_it: bool = True,
                 cadence: str = "daily", force_email: bool = False) -> str:
    """Build (and send) one cadence's email for one tenant. daily = the
    verdict email; weekly/monthly = the lens report.
    Recipients = tenant config `email_recipients`, else every user email
    on the tenant."""
    from ..web import lens_email, report
    from ..web import lenses as lenses_mod
    from .. import localtime
    conn = tenancy.tenant_connect(tenant_id)
    try:
        from ..engine import budget as _budget
        cfg = _budget.load_config(conn)
        # LOCAL date, in the household's own zone — the same clock
        # emails_due schedules by. UTC date.today() would render last
        # week / last month on installs whose local midnight hasn't crossed
        # UTC yet, and the instance's zone does the same to a household
        # living west of it.
        today_local = localtime.now_local(cfg).date()
        if cfg.get("demo_mode"):
            # Doctor tells demo tenants "email disabled — demo
            # instance"; the sweep must agree — the data is synthetic and
            # the credentials are shared, so any mail could only mislead
            log.info("tenant %s: email disabled — demo instance", tenant_id)
            return ""
        # SMS/push short forms derive from the SAME source dicts
        # the email renders (the mirror doctrine) — build them here so one
        # computation feeds every channel
        from ..notify import render as _short
        entry = _sched_entry(cfg.get("email_schedule"), cadence)
        short = ""
        personal = None
        hooked = False
        if cadence == "weekly":
            subject, plain, html = lens_email.build_weekly(conn, today_local)
            start = lenses_mod.monday_of(today_local - dt.timedelta(days=7))
            short = _short.short_weekly(
                lenses_mod.week_summary(conn, start, today=today_local), start)
        elif cadence == "monthly":
            subject, plain, html = lens_email.build_monthly(conn, today_local)
            prev_last = today_local.replace(day=1) - dt.timedelta(days=1)
            short = _short.short_monthly(
                lenses_mod.month_summary(conn, prev_last.year, prev_last.month,
                                         today=today_local),
                f"{prev_last:%B %Y}")
        elif cadence == "yearly":
            subject, plain, html = lens_email.build_yearly(conn, today_local)
            short = _short.short_yearly(
                lenses_mod.year_summary(conn, today_local.year - 1,
                                        today=today_local),
                today_local.year - 1)
        else:
            # No budget, no verdict — the same rule the Today page and the
            # native app follow. With nothing planned the engine still
            # produces a consistent "On budget · $0 of $0 left", which is
            # arithmetic rather than news, so a household still in setup
            # would be mailed a judgement on a plan it has not made. The
            # manual "email me now" button (force_email) still sends, so
            # nothing here becomes unreachable.
            if not force_email and not (cfg.get("food_monthly")
                                        or cfg.get("other_monthly")):
                log.info("tenant %s: daily email skipped — no budget set",
                         tenant_id)
                return ""
            d = report.gather(conn, today_local)
            # daily has two shapes: summary = the verdict pane only
            # (email_schedule.daily.summary), else the full report
            subject, plain, html = report.build(
                d, summary=bool(entry.get("summary")))
            short = _short.short_daily(d)
            personal = _needs_you(conn, tenant_id, d)
            # the daily webhook event: the same gather the mail renders,
            # in the integrations summary shape; queued here, sent by the
            # drain. Not on the manual "email me now" button — that is a
            # mail, not the day's report. It has its own once-a-day stamp:
            # the mail below may fail and come due again every hourly
            # sweep until the relay is back, and each retry must not post
            # the receiver another copy of the day's report
            if send_it and not force_email:
                try:
                    from ..notify import webhooks as _wh
                    if _wh.wanted(conn, "report.daily"):
                        hooked = _daily_hook_queued_today(conn, cfg)
                        if not hooked:
                            from ..web import integrations as _integ
                            hooked = _wh.emit(
                                conn, "report.daily",
                                _integ.summary(conn, today_local, st=d)) > 0
                            if hooked:
                                alerts.heartbeat(
                                    conn, DAILY_HOOK_JOB, subject[:80],
                                    zone=localtime.tenant_tz_name(cfg))
                except Exception as e:           # noqa: BLE001
                    log.warning("daily webhook failed tenant=%s: %s",
                                tenant_id, e)
        # force_email (the manual "email me now" button): send the email,
        # skip the other channels + the cadence heartbeat entirely
        want_email = True if force_email else entry.get("on", True)
        # SMS is a feature an installed add-on may withhold. tier_allows
        # returns True when nothing is installed, so an instance with its
        # own Twilio credentials is ungated; a refusal falls back to
        # email/push.
        sms_ok = ext.gate.tier_allows(conn, "sms")
        want_push = bool(entry.get("push")) and not force_email
        # a restore can plant any shape here; a non-dict must not crash the
        # send (it is "no number", which is what the consent check reads)
        phone = cfg.get("notify_phone")
        if not isinstance(phone, dict):
            phone = {}
        # The scheduled verdict is the "summary" message program and carries
        # its own opt-in — the cadence toggle says the user
        # wants it, the consent says we are allowed to text it. Both required.
        from ..notify import sms as _smsmod
        want_sms = (bool(entry.get("sms")) and not force_email and sms_ok
                    and _smsmod.consented(phone, "summary"))
        # Re-deriving recipients from raw config here would skip the hosted
        # filter, so an outsider address in email_recipients would receive a
        # household's daily/weekly/monthly financial mail on a multi-tenant
        # box. Route through _recipients() like every other send
        # (link-alert, savings, collector-stale) so the tenant-membership
        # filter always applies.
        recipients, raw_recipients = _recipients_and_raw(conn, tenant_id)
        # whether there was ANYONE to mail, before the
        # undeliverable ones were held back. The cadence heartbeat keys off
        # this, not off `recipients`: a household whose only address is
        # bouncing would otherwise never stamp "today's mail is done", so
        # the job would come due again on every hourly sweep and re-decide
        # not to send — 24 wake-ups and 24 warning lines a day instead of
        # one. Held is a decision that was MADE, not work still pending.
        had_recipients = bool(raw_recipients)
        smtp = report.resolve_smtp(conn)   # Settings-card config wins
    finally:
        conn.close()
    if send_it and want_email and force_email and not recipients:
        raise NoRecipients(
            "Nothing sent — every address on this household is held as "
            "undeliverable right now (Settings → Email shows which)."
            if had_recipients else
            "Nothing sent — household mail only goes to a confirmed address "
            "that hasn't muted it, and this household has none yet. Open the "
            "confirmation link in your welcome email (Settings → Email can "
            "resend it).")
    if send_it and want_email and recipients:
        # One message per recipient. A Bcc blast reports success whenever
        # the relay accepts the envelope, so an address that silently never
        # receives the verdict looks identical to one that did, and a
        # missing recipient goes unnoticed for days with nothing logging a
        # problem.
        outcome = report.send_each(subject, plain, html, recipients, smtp=smtp,
                                   unsubscribe=tenant_id, personal=personal)
        failed = [r["email"] for r in outcome if not r["ok"]]
        if failed:
            log.warning("tenant %s: daily email failed for %d/%d recipients: %s",
                        tenant_id, len(failed), len(outcome), ", ".join(failed))
        if failed and len(failed) == len(outcome):
            # every address failed — that is a broken relay, not a bad
            # address, and it must NOT stamp the heartbeat as a good send
            raise RuntimeError(
                f"email failed for every recipient: {outcome[0]['error']}")
    # the other channels — each a no-op unless configured AND
    # toggled AND (for SMS) the phone is verified; failures log, never raise
    if send_it and short:
        if want_sms and phone.get("verified") and phone.get("number"):
            from ..notify import sms as _sms
            _sms.send(phone["number"], short)
        if want_push:
            from ..db.tenancy import control_connect
            from ..notify import push as _push
            from ..notify import push_native as _native
            try:
                cc = control_connect()
                try:
                    _push.send_tenant(cc, tenant_id, subject, short)
                    # the phones get a tickle on the same user-facing
                    # "push" preference; the daily one carries the verdict
                    # line so the lock screen answers the question, the
                    # app fetches the rest from this server when it wakes
                    _native.send_tenant(
                        cc, tenant_id, cadence,
                        text=(_short.push_daily(d) if cadence == "daily"
                              else None))
                    cc.commit()
                finally:
                    cc.close()
            except Exception as e:               # noqa: BLE001
                log.warning("push fan-out failed for %s: %s", tenant_id, e)
    if send_it and not force_email and (had_recipients or want_sms
                                        or want_push or hooked):
        conn2 = tenancy.tenant_connect(tenant_id)
        try:
            from .. import localtime
            alerts.heartbeat(conn2, CADENCE_JOBS.get(cadence, "daily-email"),
                             subject[:80], zone=localtime.tenant_tz_name(cfg))
        finally:
            conn2.close()
    return subject


def _sweep(fn, label: str, prioritize: bool = False,
           bucket: int | None = None) -> dict:
    """Run a per-tenant task across every active tenant, isolating failures.

    `bucket` (0..SYNC_BUCKETS-1) restricts the sweep to the tenants whose
    stable hash falls in that slot — the hourly sync's load-spreading."""
    out: dict[str, str] = {}
    for tid in _tenant_ids(prioritize=prioritize):
        if bucket is not None and _tenant_bucket(tid) != bucket:
            continue
        try:
            fn(tid)
            out[tid] = "ok"
        except Exception as e:                   # noqa: BLE001
            out[tid] = f"error:{type(e).__name__}"
            log.error("%s failed for tenant %s: %s", label, tid, e)
    return out


# The hour sliced into 5-minute slots. Each tenant hashes to ONE slot and
# syncs there every hour — same hourly cadence, but tenants never pile
# onto :00 together. With every tenant on the hour, the sweep being
# sequential, at N tenants the last one's "hourly" sync starts N-1
# pull-durations late, every Plaid call lands in one burst against the
# same client_id rate limit, and the spike shares its second with the
# other :00 crons. The hash is stable (crc32 of the
# tenant id), so a tenant's slot never moves and the cadence stays exactly
# hourly; new tenants scatter uniformly by construction. Webhook-driven
# and button-driven syncs are untouched, and the per-tenant advisory lock
# already makes any overlap harmless.
# 12 five-minute slots = one poll per tenant per hour. This is the ONE
# lever on total aggregator traffic: buckets × 5 minutes IS the polling
# interval, so 24 makes it two-hourly and halves the calls. Raise
# OIKONOME_SYNC_BUCKETS to lengthen the polling interval on a large
# instance — the poll is a safety net under the webhooks, not the primary
# path, and its real cost is balance/holdings freshness, which is the only
# thing no webhook covers. Env-overridable so it can be tuned without a
# deploy.
SYNC_BUCKETS = max(1, int(env_num("OIKONOME_SYNC_BUCKETS", "12")))


def _tenant_bucket(tid) -> int:
    import zlib
    return zlib.crc32(str(tid).encode()) % SYNC_BUCKETS


# ---- arq surface -----------------------------------------------------------


async def sync_all(ctx):
    # runs every 5 minutes; each run serves one slot's tenants (see
    # SYNC_BUCKETS). The slot comes from epoch five-minute ticks, NOT the
    # minute-within-the-hour: minute//5 only ever reaches 0..11, so any
    # SYNC_BUCKETS above 12 — the documented way to stretch the polling
    # interval — permanently starved every tenant hashed into buckets
    # 12+, silently. Epoch ticks visit all N buckets in N×5 minutes.
    slot = int(dt.datetime.now(dt.timezone.utc).timestamp() // 300) \
        % SYNC_BUCKETS
    return await asyncio.to_thread(_sweep, sync_tenant, "sync", True, slot)


async def nightly_all(ctx):
    """Recurring detection, then the net-worth snapshot, then the stale-
    collector alert, then the Plaid zombie-Item reaper — independent
    sweeps so one tenant's failure in one never skips the others."""
    detect = await asyncio.to_thread(_sweep, nightly_tenant, "nightly")
    snapshot = await asyncio.to_thread(_sweep, snapshot_tenant,
                                       "networth-snapshot")
    stale = await asyncio.to_thread(_sweep, script_alert_tenant,
                                    "script-alerts")
    # release Items dead 30+ days (they bill monthly for
    # as long as they exist at Plaid) + reconcile Items already gone at
    # Plaid's side. jobs/reaper.py; OIKONOME_PLAID_REAP_DAYS=0 disables.
    from . import reaper as _reaper
    reap = await asyncio.to_thread(_sweep, _reaper.reap_tenant, "plaid-reap")
    # the installed gate's own sweeps run BEFORE the purge below, so an
    # account the gate froze tonight gets its full grace window rather than
    # racing this same run
    frozen = (await asyncio.to_thread(ext.gate.nightly)).get("frozen", [])
    # signups nobody ever confirmed: the day-7 reminder and the day-30
    # freeze (jobs/unverified.py; hosted only, OIKONOME_UNVERIFIED_REAP_DAYS=0
    # disables). Before the purge for the same reason as the gate's sweeps.
    from . import unverified as _unverified
    stale_signups = await asyncio.to_thread(_unverified.sweep)
    # finish deletions whose grace window has lapsed
    purged = await asyncio.to_thread(purge_scheduled_deletions)
    return {"detect": detect, "snapshot": snapshot, "script_alerts": stale,
            "plaid_reap": reap, "frozen": frozen, "purged": purged,
            "unverified": stale_signups}


def purge_scheduled_deletions() -> list[str]:
    """Irreversibly wipe tenants whose delete-with-grace
    window has elapsed (status='pending_delete' AND delete_after <= now).
    Also covers accounts an add-on froze under one of its lockout
    statuses and that were never restored — same grace-purge shape, same
    worker sweep, so no separate purge path is needed for them. Reuses the
    console's purge path so external-service release + audit are
    identical. Returns the tenant ids purged.

    Each tenant is purged under a row lock with the eligibility re-checked
    inside the transaction (SELECT … FOR UPDATE), so a restore landing
    mid-run cannot be half-wiped: whichever of purge/restore takes the
    lock first wins, the other observes the result. A
    purge that fails leaves the tenant in its current status (retried
    next night) and writes an admin_audit row so a permanently-stuck
    tenant is visible in the console, not silently frozen forever."""
    from ..web.adminconsole import _audit, _frozen_statuses, _purge_tenant
    admin = tenancy.admin_connect()
    try:
        # every status a purge may finish: the product's own grace
        # deletion and its never-confirmed freeze, plus whatever an
        # installed add-on freezes a household under (asked rather than
        # hardcoded, so a new lockout status never leaves tenants frozen
        # forever with nothing to sweep them) — the console's restore
        # lifts exactly this set
        statuses = _frozen_statuses()
        candidates = [str(r["id"]) for r in admin.execute(
            "SELECT id FROM tenants WHERE status = ANY(%s) "
            "AND delete_after IS NOT NULL AND delete_after <= now()",
            (statuses,)).fetchall()]
        done = []
        for tid in candidates:
            owner, owner_bounced = None, False
            try:
                with admin.transaction():
                    # re-assert under the row lock — a restore/reactivation
                    # since the candidate scan flips status away
                    locked = admin.execute(
                        "SELECT id FROM tenants WHERE id=%s "
                        "AND status = ANY(%s) "
                        "AND delete_after <= now() FOR UPDATE",
                        (tid, statuses)).fetchone()
                    if locked is None:
                        continue          # restored or already handled
                    # Hold the tenant's sync lock until this transaction
                    # COMMITS. _purge_tenant takes (and releases) the same
                    # lock session-scoped, but here its unlock lands before
                    # the enclosing commit — a sync starting in that gap
                    # would read the not-yet-deleted rows and resurrect
                    # them after the commit. Same-session acquisition never
                    # self-blocks, so stacking both scopes is safe.
                    admin.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                  (f"oikonome:sync:{tid}",))
                    # the receipt goes to the OWNER — the earliest user is
                    # usually them, but an owner who was re-invited after a
                    # member joined is not the earliest row
                    owner_addr = tenancy.owner_email(admin, tid)
                    owner = {"email": owner_addr} if owner_addr else None
                    # read BEFORE the purge takes the add-on's rows: its
                    # receipt paragraphs are the place a person is told
                    # about anything the erasure could not undo on their
                    # behalf, and some of that can only be read while the
                    # rows are still there. The self-serve door asks the
                    # same hook, so both receipts say the same things.
                    xp, xh = ext.gate.erasure_receipt(admin, tid, None)
                    # read BEFORE the purge scrubs the delivery table: an
                    # address the provider already bounces must not be
                    # mailed again after erasure, which would write a fresh
                    # bounce row for a household that no longer exists
                    from ..notify import delivery as _delivery
                    owner_bounced = bool(owner and _delivery.failing(
                        [owner["email"]]))
                    _purge_tenant(admin, tid,
                                  owner["email"] if owner else tid,
                                  ip="worker")
            except Exception as e:                        # noqa: BLE001
                log.error("scheduled purge failed for %s: %s", tid, e)
                try:
                    _audit(admin, "tenant_purge_failed", target=tid,
                           detail=f"{type(e).__name__}: {e}", ip="worker")
                except Exception:                         # noqa: BLE001
                    pass
                continue
            done.append(tid)
            # The one mail that outlives the account — sent after the rows
            # are gone, to the address that owned them. OUTSIDE the purge's
            # try on purpose: once the transaction above committed, the
            # purge succeeded, and a failure from here on is a mail
            # problem — recording it as "purge failed" (with a
            # tenant_purge_failed audit row) would make the audit trail claim
            # an erasure had not happened when it irreversibly had. The mail
            # gets its own guard, its own log line, and no retry — there is
            # no tenant left to re-purge. The account_deleted wording about
            # what was released is deliberately conditional, so it reads
            # correctly for an account that held nothing outside.
            if owner and owner.get("email") and not owner_bounced:
                try:
                    from ..notify import account_mail
                    account_mail.account_deleted(owner["email"],
                                                 extra_plain=xp,
                                                 extra_html=xh)
                except Exception as e:                    # noqa: BLE001
                    log.warning("deletion receipt mail failed for %s "
                                "(the purge itself succeeded): %s", tid, e)
        return done
    finally:
        admin.close()


def _email_if_due(tenant_id: str) -> None:
    """The due-check and the job_runs heartbeat that suppresses it are two
    separate statements, so two overlapping hourly sweeps could both read
    "due" before either stamped — and the household gets its daily verdict
    twice.

    Same guard as `sync_tenant`, for the same reason and in the same shape: a
    session-level TRY lock per tenant. A second sweep arriving while one is
    mid-send returns immediately rather than queueing behind it — a duplicate
    email is the failure being prevented, so waiting to send one anyway would
    miss the point."""
    now = dt.datetime.now(dt.timezone.utc)
    conn = tenancy.tenant_connect(tenant_id)
    try:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:email:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            log.info("email sweep already running for tenant %s — skipping",
                     tenant_id)
            return
        try:
            due = emails_due(conn, now)
            for cadence in due:
                email_tenant(tenant_id, cadence=cadence)
        finally:
            tenancy.release_lock(conn, f"oikonome:email:{tenant_id}")
    finally:
        conn.close()


def _webhook_tenants() -> list[str]:
    """Tenants with a delivery due — one admin query instead of a
    tenant-scoped connection per household per minute. Longest-waiting
    first, so a pass that runs out of minute does not always leave the
    same households for last."""
    admin = tenancy.admin_connect()
    try:
        return [str(r["tenant_id"]) for r in admin.execute(
            "SELECT tenant_id FROM webhook_deliveries "
            "WHERE state = 'pending' AND next_attempt_at <= now() "
            "GROUP BY tenant_id ORDER BY min(next_attempt_at)").fetchall()]
    finally:
        admin.close()


def webhooks_drain_tenant(tenant_id: str) -> dict:
    from ..notify import webhooks as _wh
    conn = tenancy.tenant_connect(tenant_id)
    try:
        return _wh.deliver_pending(conn)
    finally:
        conn.close()


# held for the length of one drain pass. arq's job timeout cancels the
# coroutine but not the thread it awaits, so a pass that overran its
# minute would otherwise have the next one start beside it, and every
# minute another thread would join the default executor that the sync,
# email and nightly sweeps also run on
_WEBHOOK_PASS = threading.Lock()


def webhooks_drain() -> dict:
    """Send every due webhook delivery, per tenant, isolating failures.
    Does nothing while the previous pass is still running."""
    if not _WEBHOOK_PASS.acquire(blocking=False):
        log.info("webhook drain skipped — the previous pass is still "
                 "running")
        return {}
    try:
        out: dict[str, str] = {}
        for tid in _webhook_tenants():
            try:
                r = webhooks_drain_tenant(tid)
                out[tid] = f"sent:{r['sent']} failed:{r['failed']}"
            except Exception as e:               # noqa: BLE001
                out[tid] = f"error:{type(e).__name__}"
                log.error("webhook drain failed for tenant %s: %s", tid, e)
        return out
    finally:
        _WEBHOOK_PASS.release()


async def webhooks_all(ctx):
    """Every minute: the webhook outbox. A delivery is queued by whatever
    observed the event and sent here, so a slow receiver never holds a
    request or a sync."""
    return await asyncio.to_thread(webhooks_drain)


async def email_all(ctx):
    """Hourly sweep — each tenant sends at ITS configured hour, once/day."""
    return await asyncio.to_thread(_sweep, _email_if_due, "email")


def _redis_settings():
    from arq.connections import RedisSettings
    return RedisSettings.from_dsn(os.environ.get("REDIS_URL",
                                                 "redis://localhost:6379/0"))


async def sync_one(ctx, tenant_id: str):
    """The admin console's per-tenant 'sync now' — one tenant,
    immediately, off the hourly cadence."""
    return await asyncio.to_thread(sync_tenant, tenant_id)


def _sync_item_body(tenant_id: str, item_id: str) -> dict:
    """One plaid item, one tenant — the webhook-driven scoped sync
    (SYNC_UPDATES_AVAILABLE). Pull, then the same best-effort sync-time
    categorize pass the hourly sweep runs, so webhook-fresh rows don't sit
    uncategorized until the next poll."""
    conn = tenancy.tenant_connect(tenant_id)
    locked = False
    try:
        # THE SAME per-tenant single-flight lock sync_tenant takes. Without
        # it, a webhook arriving during the hourly sweep pulls the same item
        # concurrently and clobbers items.tx_cursor — the precise damage
        # the lock's own comment describes. Skipping is right rather than
        # waiting: the run already in flight covers this item, and a webhook
        # job must not sit holding a worker slot behind a sync that can take
        # minutes.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            # "will cover it" is only true if the run in flight has not
            # already passed this item's cursor — Plaid's webhook fires
            # because there is something NEW, and a sweep that pulled this
            # item a minute ago will not see it. So the same nudge the
            # skipped full sync leaves: the holder makes one more pass,
            # which does cover it.
            _nudge_sync(conn)
            log.info("webhook sync for tenant %s item %s skipped — a sync is "
                     "already running and was asked to cover it",
                     tenant_id, item_id)
            return {"skipped": "already-running"}
        # a job already in the queue when the item was disconnected must
        # not sync a released Item (ITEM_NOT_FOUND would clobber
        # status='archived' — the webhook receiver filters archived too,
        # but an enqueued job outlives that check)
        row = conn.execute("SELECT status FROM items WHERE id=%s",
                           (item_id,)).fetchone()
        if row is None or row["status"] == "archived":
            return {"skipped": "archived" if row else "gone"}
        r = plaid.sync(conn, item_id)
        from ..sync import base as _sync_base
        with _sync_base.categorize_lock(conn, tenant_id) as _held:
            if _held:
                try:
                    from ..engine import llm_categorize as _llm
                    _llm.categorize_new(conn)
                except Exception as e:               # noqa: BLE001
                    log.warning("webhook sync categorize failed tenant=%s: %s",
                                tenant_id, e)
            else:
                log.info("webhook categorize skipped tenant=%s — a pass is "
                         "already running", tenant_id)
        return r
    finally:
        if locked:
            # MUST unlock explicitly. tenant_connect is POOLED, so close()
            # returns the connection with its session intact — a session
            # advisory lock left on it leaks to whoever borrows it next, and
            # from then on every sync for this tenant is refused by a lock
            # nobody holds on purpose.
            if tenancy.release_lock(conn, f"oikonome:sync:{tenant_id}"):
                conn = None                      # type: ignore[assignment]
        if conn is not None:
            conn.close()


async def sync_item(ctx, tenant_id: str, item_id: str):
    """Webhook receiver → scoped sync (web/plaid_webhook.py enqueues)."""
    return await asyncio.to_thread(_sync_item_body, tenant_id, item_id)


async def demo_reset_all(ctx):
    """A PUBLIC demo instance (OIKONOME_DEMO=1 in the env)
    reverts to pristine every hour — re-seed, not restore. A no-op
    everywhere else, so the cron costs nothing on real installs."""
    # env_flag, not a bare get: OIKONOME_DEMO=0 plainly means "not a
    # demo", and reading it as truthy would run the destructive hourly
    # re-seed against a real instance's data.
    if not env_flag("OIKONOME_DEMO"):
        return "not-a-demo"
    from .. import demo
    r = await asyncio.to_thread(demo.reset)
    log.info("demo reset: seed=%s transactions=%s",
             r.get("seed"), r.get("transactions"))
    return f"reset seed={r.get('seed')}"


def _nightly_reboot_check() -> str:
    """Graceful automated maintenance reboot: when the host reports
    reboot_required, run the SAME choreography as the
    console button — countdown broadcast with a 60s deadline, then the
    ops-cmd flag (the root watcher adds its own 60s grace before
    rebooting). Enabled only where OIKONOME_AUTO_REBOOT=1; a no-op when
    the host has nothing pending, so most nights nothing happens."""
    import json
    from pathlib import Path
    # env_flag, not a bare get: OIKONOME_AUTO_REBOOT=0 means "never
    # reboot this host", and reading it as truthy would schedule an
    # unattended restart the operator explicitly turned off.
    if not env_flag("OIKONOME_AUTO_REBOOT"):
        return "disabled"
    base = Path(os.environ.get("OIKONOME_STATE_DIR", "/state"))
    hh = base / "ops" / "host-health.json"
    cmd = base / "cmd"
    if not hh.is_file():
        return "no-host-health"
    try:
        health = json.loads(hh.read_text())
    except ValueError:
        return "bad-host-health"
    if not health.get("reboot_required"):
        return "not-needed"
    if not cmd.is_dir() or not os.access(cmd, os.W_OK):
        return "no-cmd-dir"
    from ..db import tenancy
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            """INSERT INTO broadcast (id, message, severity, set_at, deadline)
               VALUES (1, 'Nightly maintenance restart in about a minute — '
                          'your session reconnects automatically.', 'warn',
                       now(), now() + interval '60 seconds')
               ON CONFLICT (id) DO UPDATE SET message = EXCLUDED.message,
                 severity = 'warn', set_at = now(),
                 deadline = now() + interval '60 seconds'""")
    finally:
        admin.close()
    (cmd / "reboot-requested").write_text(
        dt.datetime.now(dt.timezone.utc).isoformat())
    log.info("nightly maintenance reboot requested (kernel/libc pending)")
    return "reboot-requested"


async def nightly_reboot(ctx):
    return await asyncio.to_thread(_nightly_reboot_check)


async def smtp_probe(ctx):
    """half-hourly: is the relay reachable? Records the answer for the
    console banner / mint door / Doctor, logs a structured failure line
    (the operator mailbox is behind the broken path), and mails the
    operator once when the path comes back. See notify/mailhealth.py."""
    def _run():
        from ..notify import mailhealth
        if not mailhealth.configured():
            return "no SMTP configured"
        return mailhealth.run_probe(mail_operator=_mail_operator)
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:                               # noqa: BLE001
        log.error("smtp probe failed to run: %s", e)
        return f"error:{type(e).__name__}"


def _mail_operator(subject: str, plain: str, html: str) -> bool:
    """Send to OIKONOME_OPERATOR_EMAIL if mail is configured; otherwise log
    the alert so it is never silently dropped. Never raises."""
    operator = os.environ.get("OIKONOME_OPERATOR_EMAIL", "").strip()
    try:
        from ..web import report as _report
        smtp = _report._env_smtp()
        if operator and smtp["configured"]:
            _report.send(subject, plain, html, [operator], smtp=smtp,
                         bcc=False)
            return True
    except Exception:                                     # noqa: BLE001
        log.exception("operator mail failed; logging instead")
    log.warning("OPERATOR ALERT — %s\n%s", subject, plain)
    return False


def _blocklist_refresh_body() -> str:
    from ..web import spamdefense
    res = spamdefense.refresh_lists()
    if res.get("skipped"):
        return f"skipped:{res['skipped']}"
    if not res.get("ok"):
        return f"error:fetch:{res.get('errors')}"
    return f"ok:{res['count']}"


async def blocklist_refresh(ctx):
    """Daily: refetch the merged public spam-domain feed (disposable-email +
    StopForumSpam toxic), dedupe, drop safelisted domains, bulk-replace. A
    failed fetch keeps the previous list. Also triggerable from the admin
    console. Blocking HTTP/DB runs off-thread."""
    try:
        return await asyncio.to_thread(_blocklist_refresh_body)
    except Exception as e:                               # noqa: BLE001
        log.error("blocklist refresh failed: %s", e)
        return f"error:{type(e).__name__}"


async def broadcast_sweep(ctx):
    """Delete reboot broadcasts whose deadline is comfortably past. A
    startup-only sweep misses the normal case (worker back ~20s after the
    deadline, inside the 2-minute guard) and leaves the banner forever."""
    def _clear():
        from ..db import tenancy
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM broadcast WHERE deadline IS NOT NULL "
                          "AND deadline < now() - interval '2 minutes'")
        finally:
            admin.close()
    return await asyncio.to_thread(_clear)


def _startup_grants_ready() -> bool:
    """Can the admin role already do what the startup sweeps do — DELETE
    stale rows from broadcast and read the tenant list? Both grants land in
    the same GRANT … ON ALL TABLES statement of the app's migrate step, but
    the predicate names the actual privileges so it can't drift from the
    sweeps. A missing table (fresh install, migrate mid-flight) raises and
    counts as not ready."""
    from ..db import tenancy
    admin = tenancy.admin_connect()
    try:
        row = admin.execute(
            "SELECT has_table_privilege(current_user, 'broadcast', 'DELETE') "
            "AND has_table_privilege(current_user, 'tenants', 'SELECT') "
            "AS ok").fetchone()
        return bool(row and row["ok"])
    except Exception:                                       # noqa: BLE001
        return False
    finally:
        admin.close()


# ≤ ~60s of waiting for the grants (tries × sleep); module-level so a test
# can shrink the wait instead of sleeping through it
STARTUP_GRANT_TRIES = 30
STARTUP_GRANT_SLEEP = 2.0


async def _startup(ctx):
    """Post-boot hygiene: a reboot broadcast whose deadline has passed by
    the time the worker is up means the restart is OVER — clear it so
    users aren't stuck reading a stale countdown."""
    from .. import logsafe
    logsafe.install()          # one line per record, as in the web process
    def _clear():
        from ..db import tenancy
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM broadcast WHERE deadline IS NOT NULL "
                          "AND deadline < now() - interval '2 minutes'")
        finally:
            admin.close()
    # After a restore or a major-version migration the tables exist but
    # the app's own migrate step — which re-applies the role grants — may
    # still be running in the app container when this worker boots. Both
    # sweeps below then fail with InsufficientPrivilege and a traceback
    # nobody can act on. Wait for the grants
    # rather than racing them; skip quietly if they never come, the next
    # boot repeats the sweeps anyway. Nothing else runs in this hook, so
    # the early return skips exactly the two sweeps.
    for _ in range(STARTUP_GRANT_TRIES):
        if await asyncio.to_thread(_startup_grants_ready):
            break
        await asyncio.sleep(STARTUP_GRANT_SLEEP)
    else:
        log.warning("startup sweeps skipped: table grants not in place "
                    "after %ss (migrate still running?)",
                    int(STARTUP_GRANT_TRIES * STARTUP_GRANT_SLEEP))
        return
    try:
        await asyncio.to_thread(_clear)
    except Exception:                                       # noqa: BLE001
        log.warning("startup broadcast sweep failed", exc_info=True)
    # re-encrypt any tenant secrets left in
    # plaintext from a pre-master-key era (idempotent; no-op once swept)
    try:
        from ..db import migrate
        n = await asyncio.to_thread(migrate.reencrypt_tenant_secrets)
        if n:
            log.info("re-encrypted %d plaintext tenant secret(s) at rest", n)
    except Exception:                                       # noqa: BLE001
        log.warning("startup secret re-encryption sweep failed", exc_info=True)


class WorkerSettings:
    on_startup = _startup
    functions = [sync_all, nightly_all, email_all, demo_reset_all, sync_one,
                 sync_item, broadcast_sweep, nightly_reboot,
                 blocklist_refresh, webhooks_all] + ext.job_functions()
    cron_jobs = [
        # every 5 minutes, each firing serving one tenant hash-slot — every
        # tenant still syncs exactly hourly, just not all at :00 (see
        # SYNC_BUCKETS)
        cron(sync_all, minute=set(range(0, 60, 5))),
        cron(smtp_probe, minute={10, 40}),               # is outbound mail alive?
        # 04:17, deliberately OFF the sync grid — sync_all fires on
        # every multiple of 5, so 04:20 would start in the same
        # second as slot 4's sweep. They contend for the same per-tenant
        # lock and whoever loses skips the tenant; colliding by
        # construction every single night would mean the Plaid reaper
        # almost never ran for that slot's tenants.
        cron(nightly_all, hour=4, minute=17),
        cron(blocklist_refresh, hour=3, minute=30),     # daily spam-list refresh
        cron(email_all, minute=5),                      # hourly; per-tenant hour
        cron(demo_reset_all, minute=0),                 # hourly pristine
        cron(broadcast_sweep,                           # stale reboot banners
             minute={2, 12, 22, 32, 42, 52}),
        cron(nightly_reboot, hour=9, minute=4),         # graceful maint window
        cron(webhooks_all, minute=set(range(60))),      # the webhook outbox
    ] + ext.cron_jobs()
    redis_settings = _redis_settings()
