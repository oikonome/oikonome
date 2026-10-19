"""arq worker — the product's scheduler. One worker serves every tenant; each task is tenant-scoped through
tenancy.tenant_connect so RLS applies inside jobs exactly as in the web app.

Schedule (cron, in the instance's zone OIKONOME_TZ — each entry a _ZonedCronJob;
the maintenance reboot alone is pinned to UTC):
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
                                   series gets a hole), the reaper, the
                                   gate's sweeps, the never-confirmed
                                   freeze and the purge — each step under
                                   its own budget (NIGHTLY_STEP_BUDGET)
  * email_all       hourly :05   — each household's cadences at ITS local
                                   send hour (emails_due)
  * staging_sweep   hourly :47   — every household's expired staged uploads
  * bulk_import_sweep every minute — queued folder imports, and ones whose
                                      worker went quiet
  * nightly_reboot  09 UTC hour  — maintenance reboot, OIKONOME_AUTO_REBOOT;
                                   at local :37 (09:37 UTC in whole-hour
                                   zones), clear of email_all's :05

Task bodies are synchronous (the engine is sync); they run in a thread so
the worker loop stays responsive. Per-tenant error isolation: one tenant's
failure logs + continues, never kills the sweep.

Locks, and the orders every holder keeps (each written down once, where
cited; two holders taking a pair the other way round deadlock):
  * per-tenant writer locks (sync, categorize, nightly, email, snapshot,
    script-alert) — try-locked by each job, all waited on in one fixed
    order by an erasure: erasure.WRITER_LOCKS;
  * the reimbursement-pairing lock before any transaction row a write
    retires or carries: sync.reanchor.hold_pairing_lock;
  * pairing, then merchant identity, then rows; bills before rows; category
    writes and renames both under identity: erasure (the paragraph under
    WRITER_LOCKS);
  * the settings row before any accounts row: engine.budget.config_txn;
  * the settings row before the household-member lock
    (oikonome:tenant-members), then the inviter's invite rows, then a
    removed member's factor lock, then users rows — an owner's rotation or
    recovery pauses the outbound settings first; a member's own rotation
    never takes the member lock: auth/reset._cancel_invites_and_read_invitees;
  * the per-user factor lock before the users row — the account-security
    doors only, never the worker: the canonical lock order in web/app.py.
And no lock another household's request can queue behind is held across
a call to an outside service: the grace purge releases a household's
links with no tenants-row lock held, the webhook drain and the email fan-out
hold no pooled connection while a receiver or the mail relay answers,
and the SMS re-read lets its row go before the provider is called.

Run: `python -m arq oikonome.jobs.worker.WorkerSettings` (compose `worker`
service). Env: REDIS_URL, OIKONOME_DSN/ADMIN_DSN, OIKONOME_MASTER_KEY,
OIKONOME_SMTP_*.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import os
import threading
import time

from arq import cron
from arq.cron import CronJob
from arq.worker import func as arq_func

from .. import ext
from ..db import tenancy
from ..engine import alerts, bills
from ..envnum import env_flag, env_num
from ..sync import base as sync_base
from ..sync import plaid, simplefin
from . import link_alert
from . import bulk_import_job

log = logging.getLogger("oikonome.jobs")

# A hosted instance says how it bills before this process polls a bank,
# mails a household or runs a nightly sweep, the same check the web app
# runs at import (ext.require_hosted_billing). The worker never imports
# the web app, so without it a hosted image built without the add-on
# would refuse to serve pages while this process kept doing the work that
# costs money and reaches households, under a gate that bills nobody.
ext.require_hosted_billing()


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


def _household_active(conn, tenant_id) -> bool:
    """Whether the household is still active, read NOW on `conn`. Every
    send path asks this right before something leaves the box: the sweeps
    list the active households once and then work through them for a
    minute or more, and a suspension, or an erasure (which flips the
    status first and only then waits for the household's writers), can
    land anywhere in that stretch. A household that is gone or no longer
    active is sent nothing; a read that fails sends nothing either."""
    try:
        # in a savepoint of its own, so a failed read cannot leave a
        # caller's open transaction aborted
        with conn.transaction():
            row = conn.execute(
                "SELECT coalesce(status, 'active') AS s FROM tenants "
                "WHERE id = %s", (str(tenant_id),)).fetchone()
    except Exception as e:                       # noqa: BLE001
        log.warning("tenant %s: status re-read failed, nothing sent: %s",
                    tenant_id, e)
        return False
    return bool(row) and row["s"] == "active"


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
            if not _household_active(cc, tenant_id):
                log.info("tenant %s: alert push skipped — the household is "
                         "no longer active", tenant_id)
                return
            _push.send_tenant(cc, tenant_id, title, body, url="/app/alerts")
            _native.send_tenant(cc, tenant_id, "alert")
            cc.commit()
        finally:
            cc.close()
    except Exception as e:                       # noqa: BLE001
        log.warning("alert push fan-out failed for %s: %s", tenant_id, e)


def _recipients_raw(conn, tenant_id: str,
                    kind: str | None = None) -> list[str]:
    """See `_recipient_lists`; the list after the per-kind choices."""
    return _recipient_lists(conn, tenant_id, kind)[1]


def _recipient_lists(conn, tenant_id: str, kind: str | None = None
                     ) -> tuple[list[str], list[str]]:
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

    `kind` (one of `email_prefs.KINDS`) names what is being sent, and an
    address that turned that kind off is left out too; the mute still
    wins over it. No kind leaves those choices out of it.

    This is the UNFILTERED list — `_recipients` is what callers want.

    Returns `(before the per-kind choices, after them)`: everyone this
    household would mail, and who still wants `kind`. A first list with an
    empty second one is a household whose people all turned this kind off —
    a decision, distinct from having nobody to mail at all."""
    from ..engine import budget
    cfg = budget.load_config(conn)
    recips = cfg.get("email_recipients")
    # tolerate any restored shape — a non-list here killed every
    # scheduled send for the tenant, silently
    _muted_raw = cfg.get("email_muted")
    muted = ({str(m).lower() for m in _muted_raw}
             if isinstance(_muted_raw, list) else set())

    from ..notify import email_prefs

    def _unmuted(emails: list[str]) -> tuple[list[str], list[str]]:
        audible = [e for e in emails if e.lower() not in muted]
        return audible, email_prefs.keep(cfg, audible, kind)

    admin = tenancy.admin_connect()
    try:
        if recips:
            # one policy, one place — the settings doors and this sweep must
            # agree on who may be mailed, or a saved list quietly stops being
            # the list that gets sent to
            from ..web import mailguard
            from ..notify import recipient_invites
            # An entry stored before addresses were checked one by one
            # (`a@x;b@y`, `Name <a@x>`) is the mailboxes it names, so every
            # rule below is applied per MAILBOX, after the split: a member
            # stored as `Name <member@x>` is still a member, and one
            # consent recorded against a joined entry is not two people
            # saying yes.
            entries = recips if isinstance(recips, list) else [recips]
            pairs, junk = [], 0
            for entry in entries:
                boxes, bad = mailguard.split_mailboxes([entry])
                junk += len(bad)
                raw = str(entry or "").strip().lower()
                for b in boxes:
                    # a display-form entry names one person, so a consent
                    # recorded under it is that person's; a joined one
                    # names several and each must answer for themselves
                    pairs.append((b, raw if len(boxes) == 1 else None))
            if junk:
                log.warning("tenant %s: %d stored recipient entr%s not an "
                            "email address, skipped", tenant_id, junk,
                            "y is" if junk == 1 else "ies are")
            if mailguard.members_only():
                members = {str(r["email"]).lower() for r in admin.execute(
                    "SELECT email FROM users WHERE tenant_id=%s",
                    (tenant_id,)).fetchall()}
                pairs = [(b, raw) for b, raw in pairs
                         if b.lower() in members]
                if not pairs:
                    log.warning("tenant %s: no configured recipient holds an "
                                "account here — sending to the owner only",
                                tenant_id)
            # An extra recipient is mailed only once THEY have said
            # yes. Being on the list is the owner's decision; receiving a
            # household's balances and spending is the recipient's. An
            # invited-but-unanswered address is skipped silently here —
            # Settings is where that state is reported, because the person
            # who needs to know is the owner, not the log.
            recips = []
            if pairs:
                st = recipient_invites.states(
                    tenant_id, [b for b, _ in pairs]
                    + [raw for _, raw in pairs if raw])
                seen = set()
                for b, raw in pairs:
                    if b.lower() in seen:
                        continue
                    # the mailbox's own answer wins (a decline included);
                    # the entry's consent stands in only when the mailbox
                    # was never asked on its own
                    mine = st.get(b.lower()) or (st.get(raw) if raw else None)
                    if mine and mine.get("state") == "accepted":
                        seen.add(b.lower())
                        recips.append(b)
            # An address paused by the owner's credential rotation is left
            # off until the owner confirms it — the list a hijacked session
            # could have extended stops reaching its additions at the
            # moment the owner recovers (notify/outbound_pause).
            from ..notify import outbound_pause
            _p = outbound_pause.paused(cfg)
            if _p and _p["recipients"]:
                held = set(_p["recipients"])
                recips = [b for b in recips if b.lower() not in held]
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


def _recipients_and_raw(conn, tenant_id: str, kind: str | None = None,
                        ) -> tuple[list[str], list[str]]:
    """`(deliverable, raw)` from ONE computation.

    `email_tenant` needs both — the addresses to mail, and whether there was
    anyone at all before the undeliverable ones were held (the heartbeat
    keys off that). Calling `_recipients` and then `_recipients_raw` runs
    the whole thing twice per tenant per cadence, since the first calls the
    second: two unpooled admin connections, two hosted-member queries, two
    invite lookups."""
    send_to, raw, _ = _recipients_detail(conn, tenant_id, kind)
    return send_to, raw


def _recipients_detail(conn, tenant_id: str, kind: str | None = None,
                       ) -> tuple[list[str], list[str], bool]:
    """`(deliverable, raw, opted_out)`: `_recipients_and_raw`, plus whether
    the raw list is empty only because every address turned `kind` off."""
    from ..notify import delivery
    audible, raw = _recipient_lists(conn, tenant_id, kind)
    opted_out = bool(audible) and not raw
    send_to, held = delivery.hold(raw)
    if held:
        log.warning("tenant %s: holding scheduled mail for %s — the provider "
                    "reports these addresses as undeliverable; the account "
                    "owner sees this in Settings → Email",
                    tenant_id, ", ".join(held))
    return send_to, raw, opted_out


def _recipients(conn, tenant_id: str, kind: str | None = None) -> list[str]:
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
    signal is the banner and the Settings row, not this log line.

    Every caller mails what this returns straight away, from inside a sweep
    that listed the household as active some time before, so a household
    suspended or put up for erasure since gets nobody."""
    if not _household_active(conn, tenant_id):
        log.info("tenant %s: alert mail skipped — the household is no "
                 "longer active", tenant_id)
        return []
    return _recipients_and_raw(conn, tenant_id, kind)[0]


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


def answer_sync_nudge(conn, tenant_id: str) -> bool:
    """For a holder of the tenant sync lock that does not pull every bank
    itself — a disconnect, a reconnect, a first sync of one new link, the
    webhook's one-item pull. A full sync turned away while it held the lock
    left a nudge and went away, trusting the holder to answer before it
    lets go; only sync_tenant answers by making another pass, so every
    other holder queues that sync instead, once it has released the lock.

    Called after the release, so the queued run can take the lock. True
    when a request was waiting (and is now consumed)."""
    if not _claim_sync_nudge(conn):
        return False
    log.info("a sync was turned away while tenant %s's lock was held — "
             "queueing it now", tenant_id)
    _enqueue_tenant_sync(tenant_id)
    return True


async def _drop_finished_result(pool, job_id: str) -> bool:
    """Forget the kept result of a job under `job_id` that is no longer
    queued or running, so the id can be queued again. False when the job
    is still waiting or in flight — that one stands for the new ask."""
    from arq.constants import job_key_prefix, result_key_prefix
    if await pool.exists(job_key_prefix + job_id):
        return False
    await pool.delete(result_key_prefix + job_id)
    return True


def _enqueue_in_background(what: str, job: str, *args,
                           job_id: str | None = None,
                           requeue_finished: bool = False) -> None:
    """Queue one job on the worker without making the door that asked wait
    on Redis: a pool that cannot connect retries for seconds, and the
    request it answers already waited. A queue that is down is logged; the
    hourly sweep stays the floor.

    `job_id` makes the job one of a kind: arq refuses a second job under
    an id that is still queued, running, or whose result it still keeps,
    so two doors asking for the same pull queue it once.

    `requeue_finished` lets the id be queued again once its job has
    finished: arq keeps a finished job's result under its id for an hour
    and refuses the id meanwhile, without a word to the caller. A pull
    owed until it succeeds (a new bank's first sync) must not be refused
    for an hour because an earlier try gave up."""
    async def _enqueue():
        from arq import create_pool
        pool = await create_pool(_redis_settings())
        try:
            if job_id is None:
                await pool.enqueue_job(job, *args)
            else:
                queued = await pool.enqueue_job(job, *args, _job_id=job_id)
                if (queued is None and requeue_finished
                        and await _drop_finished_result(pool, job_id)):
                    await pool.enqueue_job(job, *args, _job_id=job_id)
        finally:
            await pool.close()

    def _run():
        try:
            asyncio.run(_enqueue())
        except Exception as e:                               # noqa: BLE001
            log.warning("could not queue %s: %s", what, e)

    threading.Thread(target=_run, name=f"queue-{job}",
                     daemon=True).start()


def _enqueue_tenant_sync(tenant_id: str) -> None:
    """Queue one full sync for the tenant on the worker."""
    _enqueue_in_background(f"the sync tenant {tenant_id} asked for",
                           "sync_one", tenant_id)


# A sweep's result for a new bank whose first pull it left to that bank's
# first-sync job rather than pulling it itself.
FIRST_SYNC_QUEUED = "queued:first-sync"


def first_sync_job_id(item_id: str) -> str:
    """The one arq job id a new Item's first pull is queued under, by every
    door that asks for it (the link poll, the welcome wizard's nudge)."""
    return f"first-sync:{item_id}"


def queue_first_sync(tenant_id: str, item_id: str) -> None:
    """Queue a new bank's first pull (transactions, then holdings and
    liabilities) on the worker — the link poll saves the Item and answers
    at once. Queued under one id per Item, so a door asking again while it
    waits or runs adds nothing; once a try has finished without a pull
    (it gave up behind a busy lock or failed), the next ask queues it
    again rather than meeting the finished job's kept result. Lost (Redis
    down), the wizard's nudge and the hourly sync queue it again."""
    _enqueue_in_background(f"the first sync of item {item_id}",
                           "sync_item", str(tenant_id), item_id, True,
                           job_id=first_sync_job_id(item_id),
                           requeue_finished=True)


# How long one wizard nudge's pull of an Item stands for every other nudge
# of it: both wizards nudge every 30 s and may be open at once, and a
# backed-up queue would otherwise collect one pull per nudge.
BACKFILL_NUDGE_WINDOW_S = 60


def queue_backfill_sync(tenant_id: str, item_id: str,
                        now: float | None = None) -> None:
    """Queue one pull of an Item whose history is still arriving — the
    welcome wizard's nudge, for a self-host that hears no Plaid webhook.
    Runs on the worker, never in the web process; at most one per Item per
    BACKFILL_NUDGE_WINDOW_S."""
    bucket = int((now if now is not None else time.time())
                 // BACKFILL_NUDGE_WINDOW_S)
    _enqueue_in_background(f"the backfill pull of item {item_id}",
                           "sync_item", str(tenant_id), item_id, False,
                           job_id=f"backfill-nudge:{item_id}:{bucket}")


# One arq pool per event loop for the async doors (the Plaid webhook): a
# pool per event opened and closed a Redis connection for every webhook,
# three of them per new bank.
_ASYNC_POOLS: dict = {}


async def enqueue_job(job: str, *args, job_id: str | None = None) -> bool:
    """Queue one job from async code on a pool kept for this event loop.
    True when Redis took it (or already holds a job under `job_id`); False,
    logged, when the queue could not be reached — the caller decides
    whether the sender should try again."""
    from arq import create_pool
    loop = asyncio.get_running_loop()
    entry = _ASYNC_POOLS.get(id(loop))
    if entry is not None and entry[0] is not loop:
        entry = None
    pool = None
    try:
        if entry is None:
            lock = asyncio.Lock()
            _ASYNC_POOLS[id(loop)] = entry = (loop, lock, [None])
        _loop, lock, holder = entry
        async with lock:
            if holder[0] is None:
                holder[0] = await create_pool(_redis_settings())
            pool = holder[0]
        kw = {"_job_id": job_id} if job_id is not None else {}
        await pool.enqueue_job(job, *args, **kw)
        return True
    except Exception as e:                                   # noqa: BLE001
        log.warning("could not queue %s: %s", job, e)
        # a pool whose connection broke is rebuilt by the next event —
        # only the pool THIS call used, and only while it is still the
        # current one: another event may already have rebuilt it, and
        # closing that healthy pool would fail every event mid-enqueue on it
        if entry is not None and pool is not None:
            async with entry[1]:
                if entry[2][0] is not pool:
                    return False
                entry[2][0] = None
                try:
                    await pool.close()
                except Exception:                            # noqa: BLE001
                    pass
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
    subject = "Oikonome: an account source failed over"
    push_body = (lines[0] if len(lines) == 1 else
                 f"{len(lines)} accounts are serving from their backup "
                 f"source.")
    if not smtp["configured"]:
        # no relay at all: the email channel is off on this instance, a
        # decision rather than a failure to retry, so the push still goes
        # and the edge is stamped with it (unstamped, the push would repeat
        # every sweep; unsent, a push-only household never hears of it)
        links_mod.mark_failover_alerted(conn,
                                        [f["group_id"] for f in failovers])
        _push_alert(tenant_id, subject, push_body)
        return
    # nobody deliverable (the only address is held as bouncing) is the
    # same as no mail server: the in-app alert stands, the edge stays
    # unstamped, and the mail goes out on the first sweep after the
    # address is fixed
    recipients = _recipients(conn, tenant_id, "alerts")
    if not recipients:
        return
    import html as _html2
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
    _push_alert(tenant_id, subject, push_body)


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
            if it["aggregator"] == "plaid" and never_pulled(conn, it["id"]):
                # A bank no pull has finished yet is owed its first pull:
                # minutes of history, then its holdings and liabilities.
                # Only its first-sync job makes that pull, a few at a time,
                # announcing none of the history as new charges; made here
                # it would run outside those turns, skip the holdings and
                # hand the backfill to the household's webhooks. One job id
                # per Item, so a sweep asking again adds nothing.
                queue_first_sync(tenant_id, it["id"])
                results[it["id"]] = FIRST_SYNC_QUEUED
                _emit(progress, "item", {
                    "id": it["id"], "name": name, "status": "ok",
                    "transactions": _item_txn_count(conn, it["id"])})
                # nothing observed: no status transition to judge
                done += 1
                _progress_mark(conn, None, {"done": done})
                continue
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
        transitions.append({"id": it["id"],
                            "name": it["institution_name"] or it["id"],
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
                        "id": iid, "name": name or iid,
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
    # The heartbeat says the WORKER ran its sync pass, which is what
    # Doctor's row asks. One connection that keeps failing (a restored
    # shell waiting to be relinked, a bank wanting a fresh login) has its
    # own status, sync_log row and link alert; it must not keep the row red
    # while every other connection syncs. The failure count rides in the
    # note. A pass where nothing synced at all is not stamped. A new bank
    # whose first pull was handed to its own job was not synced by this
    # pass, so it is not counted as one of its items.
    synced = sum(1 for v in results.values() if str(v).startswith("ok:"))
    pulled = sum(1 for it in items
                 if results.get(it["id"]) != FIRST_SYNC_QUEUED)
    if synced or (pulled and failures == 0):
        alerts.heartbeat(conn, "sync",
                         f"{synced} ok, {failures} failed" if failures
                         else f"{synced or pulled} items")
    # proactive email the moment a connection breaks or recovers —
    # a mail failure must never break the sync
    # The edge is judged against what was last ANNOUNCED for each item,
    # not the status this pass read at its start: a webhook pull or a
    # one-bank refresh writes the same status without announcing anything,
    # and the sweep reading their write as "old" found no edge at all.
    try:
        edges = link_alert.announced_edges(conn, transitions)
    except Exception as e:                   # noqa: BLE001
        log.error("link-alert state unreadable tenant=%s: %s", tenant_id, e)
        edges = transitions
    # connection.changed fires on the same kind of edge as the email: a
    # connection that broke or recovered, including through a pull that
    # announced nothing itself — not every connection on every pass. It
    # keeps its own announced state, stamped as soon as it is queued: the
    # mail's edge stays pending while the relay refuses it, and judging the
    # webhook by that edge posted the same break again on every retry pass.
    # An item the webhook has no state for yet falls back to the mail's.
    try:
        hook_edges = link_alert.announced_edges(
            conn, edges, prefix=link_alert.HOOK_ANNOUNCED)
        # the mail's pending edge pins its own `old`; the receiver is told
        # the status the connection really left, where the pass read one
        hook_edges = [
            dict(h, old=t["old"]) if (link_alert._is_link_issue(h.get("old"))
                                      and link_alert._is_link_issue(
                                          t.get("old"))) else h
            for h, t in zip(hook_edges, transitions)]
        broke, fixed = link_alert._transitions_split(hook_edges)
        if broke or fixed:
            from ..notify import webhooks as _wh
            _wh.emit(conn, "connection.changed", {
                "connections": [{"name": t["name"], "from": t["old"],
                                 "to": t["new"]} for t in broke + fixed]})
        link_alert.stamp_announced(conn, transitions,
                                   prefix=link_alert.HOOK_ANNOUNCED)
    except Exception as e:                   # noqa: BLE001
        log.warning("connection.changed webhook failed tenant=%s: %s",
                    tenant_id, e)
    built = None
    mail_failed = False
    try:
        built = link_alert.build_alert(edges)
        if built:
            from ..web import report as _report
            sent = link_alert.notify(edges,
                                     _recipients(conn, tenant_id, "alerts"),
                                     smtp=_report.resolve_smtp(conn),
                                     unsubscribe=tenant_id)
            if sent and not sent.get("delivered", True):
                if link_alert.mail_retry_exhausted(conn):
                    log.warning("link-alert not delivered to anyone "
                                "tenant=%s; retries used up, announced "
                                "without the mail", tenant_id)
                else:
                    mail_failed = True
                    log.warning("link-alert not delivered to anyone "
                                "tenant=%s; retrying next sweep", tenant_id)
            elif sent:
                log.info("link-alert emailed tenant=%s: %s", tenant_id, sent)
    except Exception as e:                   # noqa: BLE001
        log.error("link-alert failed tenant=%s: %s", tenant_id, e)
    # Any pass that is not holding an unsent edge for retry ends the retry
    # window — a delivered mail, but also an edge that went away before the
    # relay came back (the bank recovered, the item was relinked) or an
    # alert nobody was left to receive. Left in place, the window's first
    # refusal would date from that old episode, and the next, unrelated
    # alert's first transient refusal would count as retries used up.
    if not mail_failed:
        try:
            link_alert.clear_mail_retry(conn)
        except Exception as e:               # noqa: BLE001
            log.error("link-alert retry state not cleared tenant=%s: %s",
                      tenant_id, e)
    # same transition edge as the email, so a steady-state outage pushes
    # exactly once too — in its own guard, so a mail problem never costs
    # the household the push (_push_alert never raises). A mail every
    # recipient refused leaves the edge unstamped so the next sweep sends
    # it again; the push waits for that same stamped edge, or it would
    # repeat every hour of the outage. The retry is bounded
    # (link_alert.mail_retry_exhausted): once used up, the edge is stamped
    # and the push goes out, so it is neither lost nor re-sent forever.
    if built and not mail_failed:
        _push_alert(tenant_id, built[0], "Details on the Accounts page.")
    try:
        to_stamp = transitions
        if mail_failed:
            # pin the state from BEFORE the unsent edge, so the next pass
            # still sees the edge (an item never announced would otherwise
            # fall back to the status this pass just wrote, and find none)
            broke, fixed = link_alert._transitions_split(edges)
            pending = {t["id"] for t in broke + fixed}
            to_stamp = [t for t in transitions if t.get("id") not in pending]
            to_stamp += [dict(t, new="ok") for t in broke]
            to_stamp += [dict(t, new=t["old"]) for t in fixed]
        link_alert.stamp_announced(conn, to_stamp)
    except Exception as e:                   # noqa: BLE001
        log.error("link-alert state not recorded tenant=%s: %s",
                  tenant_id, e)
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
    Best-effort — a failure here never fails the sync.

    A connection whose first pull was handed to its first-sync job was not
    pulled by this sync, and is neither a success nor a failure of it."""
    results = {k: v for k, v in (results or {}).items()
               if v != FIRST_SYNC_QUEUED}
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
        # The rows every other read door counts: a card reached through two
        # aggregators lands once, and an account the household hid
        # contributes nothing — its charges are not sent out either.
        from ..engine import links as _links
        hidden = _links.shadow_ids(conn)
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
                   AND NOT (t.account_id = ANY(%s))
                 ORDER BY t.date DESC, ABS(t.amount) DESC""",
            (list(before_ids), hidden)).fetchall()
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
        # A household that turned automatic bill detection off (it enters
        # its bills by hand) gets no new-bill or new-income offers from the
        # nightly — neither from the ledger finder nor from Plaid's streams.
        # Upkeep of the bills it has, amount drift included, still runs, and
        # the on-demand "Find bills & income" door is not this path.
        try:
            from ..engine import budget as _bdet
            detect_new = _bdet.load_config(conn).get(
                "bill_detection_auto") is not False
        except Exception as e:                   # noqa: BLE001
            log.warning("bill detection setting unreadable tenant=%s: %s",
                        tenant_id, e)
            detect_new = True
        if not detect_new:
            log.info("bill detection off for tenant %s — no new bill or "
                     "income offers tonight", tenant_id)
        try:
            stats = bills.run(conn, propose=detect_new)
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
        # proposals only, never auto-applied; off with plaid_recurring=false.
        # It is a separately billed aggregator product, like liabilities
        # and investments, so the installed gate is asked first: the
        # household's own setting can turn it off but never back on past
        # the gate. With nothing installed tier_allows says yes.
        try:
            from ..engine import budget as _bcfg
            from ..sync import plaid as _plaid
            if detect_new and ext.gate.tier_allows(conn, "plaid_recurring") \
                    and _bcfg.load_config(conn).get("plaid_recurring",
                                                    True) is not False:
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
            from ..sync import adopt as _adopt
            from ..sync import reanchor as _ra
            # a withdrawn row's pair waits the reconcile window for its
            # re-numbered twin, then lets the surviving side go
            ra = _ra.reanchor_stranded(
                conn, release_after_days=_adopt.RECONCILE_DAYS)
            if ra["released"]:
                stats["withdrawn_pairs_released"] = ra["released"]
            if ra.get("fronted_released"):
                stats["withdrawn_fronted_released"] = ra["fronted_released"]
            if ra["moved"] or ra["ambiguous"]:
                stats["reanchored"] = ra["moved"]
                stats["reanchor_ambiguous"] = ra["ambiguous"]
        except Exception as e:                       # noqa: BLE001
            log.warning("re-anchor sweep failed tenant=%s: %s", tenant_id, e)
        # a primary-checking pin left on an account a relink renumbered or
        # a disconnect removed is re-pinned where the forecast already
        # anchors, so the Today checklist does not reopen a settled choice
        try:
            from ..engine import forecast as _forecast
            repinned = _forecast.auto_pin_primary_checking(conn)
            if repinned:
                stats["primary_checking_pinned"] = repinned
        except Exception as e:                       # noqa: BLE001
            log.warning("primary checking re-pin failed tenant=%s: %s",
                        tenant_id, e)
        from ..engine import amazon_match as _am
        from ..engine import costco_match as _cm
        from ..engine import llm_categorize as _llm
        from ..engine import merchant_dedup as _md
        # the Amazon chain (match + the LLM run's classify/summarize) shares
        # a single-flight lock with the hourly sync's categorize_new; the
        # nightly waits rather than skips — it is the reconciliation pass —
        # but only so long: a sync whose model is slow can hold it past the
        # wait, and then this household loses only tonight's chain (every
        # step in it is pending-gated, so the next night catches up), never
        # the receipt, retrain and savings steps after it
        try:
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
                # isolated like the steps before it: a model that is down or a
                # reply that will not parse must not cost the household the
                # receipt pass, the matching and the sweeps that follow
                try:
                    _llm.run(conn)
                except Exception as e:                   # noqa: BLE001
                    log.warning("categorizer run failed tenant=%s: %s",
                                tenant_id, e)
        except _llm.LockBusy:
            log.warning("amazon chain skipped tonight tenant=%s: the lock "
                        "is still held by another pass", tenant_id)
            stats["amazon_chain_skipped"] = "busy"
        # parse any stored-but-unparsed receipts (no-op sans LLM)
        try:
            from ..engine import receipts as _receipts
            _receipts.parse_pending(conn)
        except Exception as e:                   # noqa: BLE001
            log.warning("receipt parse sweep failed: %s", e)
        # and pair any receipt still waiting for its charge — the sync and
        # the parse each try as they happen; this is the backstop for a
        # pass either of them skipped
        try:
            from ..engine import receipt_match as _rmatch
            _rmatch.match_waiting(conn)
        except Exception as e:                   # noqa: BLE001
            log.warning("waiting-receipt match sweep failed: %s",
                        type(e).__name__)
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
                # the crossing is recorded only once its mail went out (or
                # there was nobody / no relay to send it to): recorded
                # first, a relay that refused every recipient lost the
                # milestone for good, since the next night finds no edge
                delivered = True
                if msgs:
                    from ..web import report as _report
                    recipients = _recipients(conn, tenant_id, "alerts")
                    if recipients:
                        plain = "Savings goals:\n\n  " + "\n  ".join(msgs)
                        import html as _html
                        html = ("<p>Savings goals:</p><ul>"
                                + "".join(f"<li>{_html.escape(m)}</li>"
                                          for m in msgs) + "</ul>")
                        smtp = _report.resolve_smtp(conn)
                        sent = _report.send_each(
                            "Oikonome: savings goal update", plain, html,
                            recipients, smtp=smtp, unsubscribe=tenant_id)
                        delivered = (
                            not isinstance(smtp, dict)
                            or smtp.get("configured") is False
                            or any(r.get("ok") for r in sent or []))
                        if not delivered:
                            log.warning("savings milestone mail not "
                                        "delivered to anyone tenant=%s; "
                                        "retrying next night", tenant_id)
                if delivered and new_state != (cfg.get("savings_goal_state")
                                               or {}):
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
        except Exception as e:                   # noqa: BLE001
            log.warning("savings milestone sweep failed: %s", e)
        # the heartbeat is what Doctor reads as "nightly detection works":
        # a night whose recurring detection failed leaves it unstamped, so
        # a failure that repeats every night turns the row red instead of
        # hiding behind a fresh "+0 proposals"
        if stats.get("error"):
            log.error("nightly detection not recorded as healthy tenant=%s: "
                      "%s", tenant_id, stats["error"])
        else:
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
        recipients = _recipients(conn, tenant_id, "alerts")
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
        smtp = report.resolve_smtp(conn)
        results = report.send_each(subject, plain, html, recipients,
                                   smtp=smtp, unsubscribe=tenant_id)
        # every recipient refused on a relay that is configured = nothing
        # went out: leave the edge unstamped so the next sweep mails it
        # again (a quiet collector never pushes, and only a push clears the
        # stamp, so a stamp here would lose the alert for good). No relay
        # at all is a setting, not a failure, so that edge is stamped. The
        # push waits for the stamped edge, or it would repeat every sweep.
        relay = isinstance(smtp, dict) and smtp.get("configured") is not False
        if relay and not any(r.get("ok") for r in results or []):
            log.warning("stale-collector alert not delivered to anyone "
                        "tenant=%s; retrying next sweep", tenant_id)
            return {"alerted": 0}
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


CADENCE_JOBS = {"daily": "daily-email", "monthly": "monthly-email",
                "yearly": "yearly-email"}


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


# how many days after the 1st a missed monthly / yearly card is still sent
CADENCE_CATCHUP_DAYS = 3


def emails_due(conn, now_utc: dt.datetime) -> list[str]:
    """Which cadences should send THIS hour. email_schedule =
    {daily:{on,hour}, monthly:{on,hour}, yearly:{on,hour}} (a stored
    `weekly` entry is ignored: there is no weekly report any more),
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
    # The monthly and yearly cards are due from the configured hour on
    # the 1st, and keep being due for a few days after it until this
    # period's send is stamped: a relay outage or a worker that was down
    # from the send hour to midnight must not lose the month (or the year)
    # for good. The catch-up is bounded so a card weeks late never arrives,
    # and a household created after the period began is never sent a card
    # for a period it was not here for.
    month_start = dt.datetime.combine(
        local.date().replace(day=1), dt.time.min,
        local.tzinfo).astimezone(dt.timezone.utc)
    born_before = not (owner and owner["c"] and owner["c"] >= month_start)

    def period_due(job, hour, first_day):
        if first_day:
            return local.hour >= hour and fresh(job, day_start)
        # a catch-up day still waits for the household's send hour: the
        # first sweep after midnight must not text or mail at 00:05
        if not born_before or local.day > CADENCE_CATCHUP_DAYS + 1 \
                or local.hour < hour:
            return False
        # any stamp in the last few weeks is this period's send: the
        # previous one is at least four weeks old (a zone change can
        # move "midnight on the 1st" by a day either way)
        hb = conn.execute("SELECT ran_at FROM job_runs WHERE job=%s",
                          (job,)).fetchone()
        return hb is None or \
            hb["ran_at"] < month_start - dt.timedelta(days=2)

    m = _sched_entry(sched, "monthly")
    h = slot(m, "monthly")
    if h is not None and period_due("monthly-email", h, local.day == 1):
        due.append("monthly")
    # annual report card — Jan 1 (the once-per-local-day guard
    # keeps it to one send; the month+day test keeps it to one per year),
    # with the same bounded catch-up as the monthly card
    y = _sched_entry(sched, "yearly")
    h = slot(y, "yearly")
    if h is not None and local.month == 1 \
            and period_due("yearly-email", h, local.day == 1):
        due.append("yearly")
    return due


def email_due(conn, now_utc: dt.datetime) -> bool:
    """Back-compat shim (the daily cadence only)."""
    return "daily" in emails_due(conn, now_utc)


# the job_runs stamp of the day's report.daily webhook event, separate
# from the daily email's: the two succeed and fail independently
DAILY_HOOK_JOB = "daily-webhook"


def _daily_hook_wanted(conn) -> bool:
    """Whether an integration subscribed to the daily report event."""
    try:
        from ..notify import webhooks as _wh
        return bool(_wh.wanted(conn, "report.daily"))
    except Exception:                                      # noqa: BLE001
        return False


def _daily_hook_queued_today(conn, cfg: dict) -> bool:
    """Was today's report.daily already queued — today being the
    household's local day, read exactly as the daily email reads it
    (including the zone the stamp was written in, so moving the household
    east does not make the same day look new and post the report twice)."""
    return _stamped_today(conn, cfg, DAILY_HOOK_JOB)


def _stamped_today(conn, cfg: dict, job: str) -> bool:
    """Has `job` stamped job_runs since the household's local midnight —
    "today" read exactly as the cadence emails read it."""
    from .. import localtime
    now_utc = dt.datetime.now(dt.timezone.utc)
    local = now_utc.astimezone(localtime.tenant_tz(cfg))
    day_start = dt.datetime.combine(local.date(), dt.time.min,
                                    local.tzinfo).astimezone(dt.timezone.utc)
    return not _stamp_fresh(conn, cfg, now_utc, job, day_start)


def _stamped_this_period(conn, cfg: dict, job: str) -> bool:
    """Has `job` stamped job_runs since this month began — the window the
    monthly and yearly catch-up retries in. "Today" is the wrong window
    there: the card stays due for a few days after the 1st while the mail
    keeps failing, and the SMS/push half already sent on the 1st must not
    go out again on each catch-up day. Same two-day allowance for a zone
    change as emails_due's period test (the previous period's stamp is
    always weeks older)."""
    from .. import localtime
    local = dt.datetime.now(dt.timezone.utc).astimezone(
        localtime.tenant_tz(cfg))
    month_start = dt.datetime.combine(
        local.date().replace(day=1), dt.time.min,
        local.tzinfo).astimezone(dt.timezone.utc)
    row = conn.execute("SELECT ran_at FROM job_runs WHERE job=%s",
                       (job,)).fetchone()
    return bool(row) and row["ran_at"] >= month_start - dt.timedelta(days=2)


# Heartbeat notes of a scheduled cadence the worker decided not to send:
# the period's decision was made, not work still pending, so the cadence is
# stamped done rather than coming due again every hour.
NO_RECIPIENTS = "no recipients"
NO_BUDGET = "no budget set"


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
    verdict email; monthly/yearly = the lens report.
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
        # household's daily/monthly/yearly financial mail on a multi-tenant
        # box. Route through _recipients() like every other send
        # (link-alert, savings, collector-stale) so the tenant-membership
        # filter always applies.
        recipients, raw_recipients, opted_out = _recipients_detail(
            conn, tenant_id, cadence if cadence in CADENCE_JOBS else None)
        # whether there was ANYONE to mail, before the
        # undeliverable ones were held back. The cadence heartbeat keys off
        # this, not off `recipients`: a household whose only address is
        # bouncing would otherwise never stamp "today's mail is done", so
        # the job would come due again on every hourly sweep and re-decide
        # not to send — 24 wake-ups and 24 warning lines a day instead of
        # one. Held is a decision that was MADE, not work still pending.
        had_recipients = bool(raw_recipients)
        # Nobody to mail is also a decision made — everyone turned this
        # kind off, or there is no address at all (a hosted owner who has
        # not confirmed theirs, a household that muted scheduled mail).
        # With no other channel wanting the cadence there is nothing to
        # build, so the report is not rendered at all and the period is
        # stamped done with a note saying why — else it would come due, be
        # rebuilt over the whole ledger and send nothing, every hour (and
        # every hour of the monthly and yearly catch-up days).
        if (not raw_recipients and send_it and not force_email
                and not (want_sms or want_push)
                and not (cadence == "daily" and _daily_hook_wanted(conn))):
            from ..notify import email_prefs as _prefs
            alerts.heartbeat(conn, CADENCE_JOBS.get(cadence, "daily-email"),
                             _prefs.NOBODY_OPTED_IN if opted_out
                             else NO_RECIPIENTS,
                             zone=localtime.tenant_tz_name(cfg))
            log.info("tenant %s: %s email skipped — %s", tenant_id, cadence,
                     "every recipient turned it off" if opted_out
                     else "no confirmed, unmuted address")
            return ""
        if cadence == "monthly":
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
            # No budget, no verdict — the same rule, read the same way
            # (budget.budgets_set: the STORED row), as the Today page and
            # the native app. With nothing planned the engine still
            # produces a consistent "On budget · $0 of $0 left", which is
            # arithmetic rather than news; and under a month of history
            # the loaded config carries a provisional seed nobody saved,
            # which would mail "Over budget" on the app's own guess. The
            # manual "email me now" button (force_email) still sends, so
            # nothing here becomes unreachable.
            if not force_email and not _budget.budgets_set(conn):
                # the skip is the day's decision, so the day is stamped
                # with it: unstamped, the cadence came due every hour and
                # Doctor read the silence as a relay that never sent
                if send_it:
                    alerts.heartbeat(conn, "daily-email", NO_BUDGET,
                                     zone=localtime.tenant_tz_name(cfg))
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
        smtp = report.resolve_smtp(conn)   # Settings-card config wins
        # the SMS/push half of this cadence has its own once-a-day stamp,
        # like the daily webhook: when every email fails, the cadence stays
        # due and the sweep retries it each hour until the relay is back,
        # and each retry must not text or push the household again
        side_job = CADENCE_JOBS.get(cadence, "daily-email") + ":sms-push"
        side_done = (send_it and not force_email and (want_sms or want_push)
                     and (_stamped_this_period(conn, cfg, side_job)
                          if cadence in ("monthly", "yearly")
                          else _stamped_today(conn, cfg, side_job)))
    finally:
        conn.close()
    if send_it and want_email and force_email and not recipients:
        raise NoRecipients(
            "Nothing sent — every address on this household is held as "
            "undeliverable right now (Settings → Email shows which)."
            if had_recipients else
            f"Nothing sent — everyone on this household has turned off the "
            f"{cadence} email (Settings → Email → Who gets which email)."
            if opted_out else
            "Nothing sent — household mail only goes to a confirmed address "
            "that hasn't muted it, and this household has none yet. Open the "
            "confirmation link in your welcome email (Settings → Email can "
            "resend it).")
    # No relay at all means the email channel is off on this instance, the
    # same reading _alert_failovers gives it — trying anyway only walks
    # every recipient into a refused localhost connection and fails the
    # cadence every hour. The manual button still tries, so the person who
    # pressed it is told why nothing arrived.
    email_failed = None
    if send_it and want_email and recipients and (smtp.get("configured")
                                                  or force_email):
        # The list and the relay were read before the build, which can take
        # minutes; an outbound pause committed since must hold this mail too.
        try:
            recipients, smtp = _live_mail_route(
                tenant_id, recipients,
                cadence if cadence in CADENCE_JOBS else None)
        except Exception as e:                   # noqa: BLE001
            log.warning("tenant %s: recipient re-read failed, %s email not "
                        "sent: %s", tenant_id, cadence, e)
            email_failed = RuntimeError(f"recipient re-read failed: {e}")
            recipients = []
    if send_it and want_email and recipients and (smtp.get("configured")
                                                  or force_email):
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
            # address, and it must NOT stamp the heartbeat as a good send.
            # Raised only after the other channels below: neither SMS nor
            # push depends on the relay, so its outage must not hold them.
            email_failed = RuntimeError(
                f"email failed for every recipient: {outcome[0]['error']}")
    # the other channels — each a no-op unless configured AND
    # toggled AND (for SMS) the phone is verified; failures log, never raise
    if send_it and short and not side_done:
        if want_sms and phone.get("verified") and phone.get("number"):
            from ..notify import sms as _sms
            # `phone` was read before the gather and the mail fan-out, which
            # can take minutes. An owner closing a hijack in that window —
            # a credential change that pauses the number, a clear, a
            # recovery that drops it — must stop this text too, so the
            # number is read again right before it is used.
            live = _live_sms_number(tenant_id)
            if live:
                _sms.send(live, short)
            else:
                log.info("tenant %s: SMS skipped — the number was paused "
                         "or removed during this send", tenant_id)
        if want_push:
            from ..db.tenancy import control_connect
            from ..notify import push as _push
            from ..notify import push_native as _native
            try:
                cc = control_connect()
                try:
                    # re-read right before the fan-out, like the mail and
                    # the text: a suspension or erasure that landed while
                    # the report was built must stop this too
                    if not _household_active(cc, tenant_id):
                        raise _HouseholdGone()
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
            except _HouseholdGone:
                log.info("tenant %s: push skipped — the household is no "
                         "longer active", tenant_id)
            except Exception as e:               # noqa: BLE001
                log.warning("push fan-out failed for %s: %s", tenant_id, e)
    if send_it and not force_email and (had_recipients or want_sms
                                        or want_push or hooked):
        conn2 = tenancy.tenant_connect(tenant_id)
        try:
            from .. import localtime
            zone = localtime.tenant_tz_name(cfg)
            if (want_sms or want_push) and short and not side_done:
                alerts.heartbeat(conn2, side_job, subject[:80], zone=zone)
            if email_failed is None:
                alerts.heartbeat(conn2,
                                 CADENCE_JOBS.get(cadence, "daily-email"),
                                 subject[:80], zone=zone)
        finally:
            conn2.close()
    if email_failed is not None:
        raise email_failed
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
# 12 five-minute slots = one poll per tenant per hour. Buckets × 5
# minutes IS the polling interval, so 24 makes it two-hourly. Raise
# OIKONOME_SYNC_BUCKETS to lengthen the interval on a large instance —
# the poll is a safety net under the webhooks, not the primary path, and
# a longer one trades only balance/holdings freshness, which no webhook
# covers. Env-overridable so it can be tuned without a
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


# How long each nightly step may take before the run moves on to the next
# one, in seconds. One arq job has one timeout and a cron job is never
# retried, so without a budget per step a detection pass that ran long
# (a sick institution holding Plaid calls to their client timeout, a big
# model backlog) cancelled the job at its next await and every sweep
# behind it — snapshot, reaper, freezes, the purge of lapsed deletions —
# was skipped for every tenant. A step past its budget keeps running in its
# thread (a thread cannot be cancelled); the run just stops waiting for it.
# Module-level so a test can shrink a budget instead of sleeping through it.
NIGHTLY_STEP_BUDGET = {
    "nightly": 3 * 3600,
    "networth-snapshot": 3600,
    "script-alerts": 1800,
    "plaid-reap": 3600,
    "gate": 1800,
    "unverified": 1800,
    "purge": 3600,
    "plaid-release-retry": 1800,
}
# the job's own timeout: every budget plus a margin, so arq never cancels
# the run from outside before the steps' own budgets have had their say
NIGHTLY_TIMEOUT = sum(NIGHTLY_STEP_BUDGET.values()) + 600


# How many nightly step THREADS are still running. A step past its budget
# outlives the run that started it (the run stops waiting; the thread goes
# on), so the in-flight run count alone would let the maintenance reboot
# kill it mid-tenant. Raised and lowered inside the thread, under a lock.
_STEP_THREADS = 0
_STEP_THREADS_LOCK = threading.Lock()


def _counted_step(fn, *args):
    global _STEP_THREADS
    with _STEP_THREADS_LOCK:
        _STEP_THREADS += 1
    try:
        return fn(*args)
    finally:
        with _STEP_THREADS_LOCK:
            _STEP_THREADS -= 1


def _step_threads_running() -> int:
    with _STEP_THREADS_LOCK:
        return _STEP_THREADS


async def _nightly_step(label: str, fn, *args):
    """Run one nightly step in a thread under its own budget. A step that
    raises or overruns is logged and reported in the result, never allowed
    to stop the steps after it."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_counted_step, fn, *args),
            NIGHTLY_STEP_BUDGET.get(label, 3600))
    except TimeoutError:
        log.error("nightly step %s overran its %ss budget — moving on; it "
                  "keeps running in the background", label,
                  NIGHTLY_STEP_BUDGET.get(label, 3600))
        return "timeout"
    except Exception as e:                       # noqa: BLE001
        log.error("nightly step %s failed: %s", label, e)
        return f"error:{type(e).__name__}"


# how many nightly runs are in flight in this worker (the cron's and a
# console run-now can overlap), so the maintenance reboot can wait for them.
# Touched only from the event loop, so a plain counter is enough.
_NIGHTLY_RUNS = 0


async def nightly_all(ctx):
    global _NIGHTLY_RUNS
    _NIGHTLY_RUNS += 1
    try:
        return await _nightly_all_body()
    finally:
        _NIGHTLY_RUNS -= 1


async def _nightly_all_body():
    """Recurring detection, then the net-worth snapshot, then the stale-
    collector alert, then the Plaid zombie-Item reaper, the gate's sweeps,
    the never-confirmed freeze and the purge — independent steps, each with
    its own budget and failure guard, so one tenant's failure (or one slow
    sweep) never skips the others."""
    detect = await _nightly_step("nightly", _sweep, nightly_tenant,
                                 "nightly")
    snapshot = await _nightly_step("networth-snapshot", _sweep,
                                   snapshot_tenant, "networth-snapshot")
    stale = await _nightly_step("script-alerts", _sweep, script_alert_tenant,
                                "script-alerts")
    # release Items dead 30+ days (they bill monthly for
    # as long as they exist at Plaid) + reconcile Items already gone at
    # Plaid's side. jobs/reaper.py; OIKONOME_PLAID_REAP_DAYS=0 disables.
    from . import reaper as _reaper
    reap = await _nightly_step("plaid-reap", _sweep, _reaper.reap_tenant,
                               "plaid-reap")
    # Items whose release failed while their household was being erased
    # or reset — no tenant sweep reaches them (jobs/reaper.py)
    release_retry = await _nightly_step("plaid-release-retry",
                                        _reaper.retry_queued_releases)
    if isinstance(release_retry, dict) and any(release_retry.values()):
        log.info("plaid release retry: %s", {k: len(v) for k, v in
                                             release_retry.items()})
    # the installed gate's own sweeps run BEFORE the purge below, so an
    # account the gate froze tonight gets its full grace window rather than
    # racing this same run
    gated = await _nightly_step("gate", ext.gate.nightly)
    frozen = gated.get("frozen", []) if isinstance(gated, dict) else gated
    # signups nobody ever confirmed: the day-7 reminder and the day-30
    # freeze (jobs/unverified.py; hosted only, OIKONOME_UNVERIFIED_REAP_DAYS=0
    # disables). Before the purge for the same reason as the gate's sweeps.
    from . import unverified as _unverified
    stale_signups = await _nightly_step("unverified", _unverified.sweep)
    # finish deletions whose grace window has lapsed
    purged = await _nightly_step("purge", purge_scheduled_deletions)
    return {"detect": detect, "snapshot": snapshot, "script_alerts": stale,
            "plaid_reap": reap, "frozen": frozen, "purged": purged,
            "unverified": stale_signups}


def _gate_unavailable() -> bool:
    """Is the instance running under the fail-closed stand-in for a gate
    add-on that failed to load? `ext.gate` is a lazy proxy, or the
    implementation itself where a test or an embedding process set one."""
    impl = getattr(ext.gate, "_resolve", None)
    return isinstance(impl() if impl else ext.gate, ext._FailClosedGate)


# A purge holds its claim for as long as the vendor releases take, which is
# minutes. A claim this old belongs to a worker that was killed mid-purge
# (a redeploy, an out-of-memory kill), and nothing else will ever clear it.
PURGE_CLAIM_STALE_HOURS = 6


def purge_claim_is_stale_sql() -> str:
    """The SQL condition, over a tenants row, for a claim old enough to be
    treated as abandoned."""
    return (f"purge_claimed_at < now() - interval "
            f"'{int(PURGE_CLAIM_STALE_HOURS)} hours'")


def archive_released_plaid_items(tid: str) -> int:
    """Archive the household's Plaid Items that a purge already removed at
    Plaid, for a household the purge gives back instead of erasing.

    The release removes every Item at Plaid and stamps the Item ledger,
    but leaves the local rows holding their tokens, because the erase
    that normally follows takes the rows anyway. A household handed back
    after that would look connected while every sync failed against an
    Item that no longer exists. Archived as gone at Plaid, the Accounts
    page and the alert strip ask for a reconnect instead, and the books
    stay. An Item whose removal failed is still live at Plaid and keeps
    its token. Returns the number archived; never raises."""
    try:
        conn = tenancy.tenant_connect(tid)
    except Exception as e:                                 # noqa: BLE001
        log.warning("could not open %s to archive released items: %s",
                    tid, e)
        return 0
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE aggregator='plaid' "
            "AND access_token IS NOT NULL "
            "AND COALESCE(status,'') != 'archived'").fetchall()]
        if not ids:
            return 0
        admin = tenancy.admin_connect()
        try:
            gone = [r["item_id"] for r in admin.execute(
                "SELECT item_id FROM plaid_item_ledger "
                "WHERE item_id = ANY(%s) AND removed_at IS NOT NULL",
                (ids,)).fetchall()]
        finally:
            admin.close()
        if gone:
            conn.execute(
                "UPDATE items SET status='archived', "
                "archived_reason='plaid-gone', archived_at=now(), "
                "access_token=NULL WHERE id = ANY(%s)", (gone,))
        return len(gone)
    except Exception as e:                                 # noqa: BLE001
        log.warning("could not archive released items of %s: %s", tid, e)
        return 0
    finally:
        conn.close()


def _purge_one(admin, tid: str, statuses: list[str], _audit):
    """Erase one household whose grace window has lapsed. Returns None when
    it no longer qualifies (restored since the scan, or claimed by a run
    still in progress), else (owner,
    owner_bounced, receipt_plain, receipt_html) for the deletion receipt.

    Three phases, and no lock on the tenants row held across the vendor
    calls in the middle. FOR UPDATE on that row conflicts with the
    FOR KEY SHARE every insert into a table keyed to it takes — a
    sign-in's session row, a device token — so held across Plaid, MX and
    the billing add-on's release (minutes, at their timeouts) it would
    park those requests, each on one of the few control-pool connections
    every request on the instance shares.

    1. CLAIM, committed at once: re-check eligibility under the row lock
       and stamp `purge_claimed_at`. The status and status_before_delete
       are left exactly as they were: together they say whether somebody
       asked for this deletion, and the billing add-on reads them to
       decide whether a payment or gift may lift the household, so a
       lockout nobody asked for stays liftable while the purge runs. The
       stamp is the claim's token, and the console's restore door refuses
       while it is set.
    2. RELEASE, with the tenant's writer locks held (session-scoped, until
       the end): read the receipt while the rows exist, release what the
       household holds outside, and commit the pending-release marker on
       its own, so a failure after an irreversible release still leaves
       the retry handle behind.
    3. ERASE, in its own transaction: re-check under the row lock that the
       household is still the one claimed — still frozen, still due, same
       stamp — and that nothing only its owner could attach (a payment, a
       gift) arrived for a household frozen without being asked. A
       household that was lifted, rescheduled or became entitled is kept,
       and the audit says what was released; otherwise every row goes.

    Any failure after the claim clears the stamp, so the next night's run
    and the restore door both see the household as it is."""
    from .. import erasure as _erasure
    with admin.transaction():
        # A live claim belongs to the run that made it: two purges can
        # overlap (the nightly sweep and one started from the console), and
        # a second run that overwrote the stamp would make the first give
        # the household up while taking its own money baseline after a
        # payment that arrived mid-purge, so a household that paid would be
        # erased. Only a stale claim, left by a worker killed mid-purge, is
        # taken over.
        locked = admin.execute(
            "SELECT id, status FROM tenants WHERE id=%s "
            "AND status = ANY(%s) AND delete_after <= now() "
            "AND (purge_claimed_at IS NULL OR "
            + purge_claim_is_stale_sql() + ") FOR UPDATE",
            (tid, statuses)).fetchone()
        if locked is None:
            return None
        claimed_status = locked["status"]
        claim = admin.execute(
            "UPDATE tenants SET purge_claimed_at = clock_timestamp() "
            "WHERE id=%s RETURNING purge_claimed_at",
            (tid,)).fetchone()["purge_claimed_at"]
        # A freeze nobody asked for (an add-on's lockout, the unconfirmed
        # signup freeze) is lifted by money arriving; a requested deletion
        # is not. What was attached at the claim is the baseline, so only
        # something new mid-purge counts.
        asked_for = claimed_status == "pending_delete"
        money_before = (True if asked_for
                        else bool(ext.gate.money_attached(admin, tid)))

    def still_claimed(lock: bool):
        return admin.execute(
            "SELECT status, COALESCE(status_before_delete,'active') AS prev "
            "FROM tenants WHERE id=%s AND status = ANY(%s) "
            "AND delete_after <= now() AND purge_claimed_at = %s"
            + (" FOR UPDATE" if lock else ""),
            (tid, statuses, claim)).fetchone()

    def unclaim():
        # best effort: the connection may be the thing that failed, and a
        # stamp left behind is taken over once it is stale
        try:
            with admin.transaction():
                admin.execute(
                    "UPDATE tenants SET purge_claimed_at = NULL "
                    "WHERE id=%s AND purge_claimed_at = %s", (tid, claim))
        except Exception as e:                            # noqa: BLE001
            log.warning("could not clear the purge claim on %s: %s", tid, e)

    # Block on every background writer's lock: a sync in flight finishes
    # first, and none can start until the erase has committed (the domain
    # tables have no FK to tenants, so a writer racing the delete would
    # upsert rows for a household that no longer exists).
    try:
        _erasure.hold_writers(admin, tid)
    except BaseException:
        unclaim()
        raise
    try:
        # the receipt goes to the OWNER — the earliest user is usually
        # them, but an owner who was re-invited after a member joined is
        # not the earliest row
        owner_addr = tenancy.owner_email(admin, tid)
        owner = {"email": owner_addr} if owner_addr else None
        # read BEFORE the purge takes the add-on's rows: its receipt
        # paragraphs are the place a person is told about anything the
        # erasure could not undo on their behalf, and some of that can
        # only be read while the rows are still there. The self-serve door
        # asks the same hook, so both receipts say the same things.
        xp, xh = ext.gate.erasure_receipt(admin, tid, None)
        # read BEFORE the purge scrubs the delivery table: an address the
        # provider already bounces must not be mailed again after erasure,
        # which would write a fresh bounce row for a household that no
        # longer exists
        from ..notify import delivery as _delivery
        owner_bounced = bool(owner and _delivery.failing([owner["email"]]))
        # the writer wait can be long: a household lifted meanwhile keeps
        # its links rather than losing them to a purge that will not erase
        if still_claimed(lock=False) is None:
            unclaim()
            return None
        released = _erasure.release_external(tid)
        retry_ids = _erasure.collect_retry_ids(tid)
        _erasure.record_pending_release(admin, tid, released, retry_ids)
        released_note = (f"plaid_items_released={released['plaid_items']} "
                         f"released={released['released']} "
                         f"mx={released['mx_user']}")
        with admin.transaction():
            # the writer locks again, transaction-scoped: re-entrant on this
            # session, and released by the COMMIT itself, so no later
            # change to the session unlock's place can open a gap between
            # an unlock and the delete becoming visible
            _erasure.hold_writers(admin, tid, xact=True)
            still = still_claimed(lock=True)
            if still is None:
                admin.execute(
                    "UPDATE tenants SET purge_claimed_at = NULL "
                    "WHERE id=%s AND purge_claimed_at = %s", (tid, claim))
                archived = archive_released_plaid_items(tid)
                _audit(admin, "tenant_purge_abandoned", target=tid,
                       detail="lifted or rescheduled while its links were "
                              f"being released: {released_note} "
                              f"archived={archived}",
                       ip="worker")
                return None
            if not money_before and ext.gate.money_attached(admin, tid):
                # paid for or gifted while the purge ran: put it back where
                # the restore door would, and let the gate say whether
                # 'active' is where its billing lands it
                admin.execute(
                    "UPDATE tenants SET status=%s, delete_after=NULL, "
                    "status_before_delete=NULL, purge_claimed_at=NULL "
                    "WHERE id=%s", (still["prev"], tid))
                landed = still["prev"]
                if landed == "active":
                    landed = ext.gate.reactivate(admin, tid)
                archived = archive_released_plaid_items(tid)
                _audit(admin, "tenant_purge_abandoned", target=tid,
                       detail=f"entitled while its links were being "
                              f"released: restored_to={landed} "
                              f"{released_note} archived={archived}",
                       ip="worker")
                return None
            tenancy.delete_tenant_rows(admin, tid)
            _audit(admin, "tenant_delete", target=tid,
                   detail=f"owner={owner_addr or tid} "
                          f"{released_note} "
                          f"sms={released.get('sms')}"
                          + (f" plaid_failed={released['plaid_failed']}"
                             if released.get('plaid_failed') else ""),
                   ip="worker")
    except BaseException:
        unclaim()
        raise
    finally:
        # session-level, so it must not be left to the connection's close
        _erasure.release_writers(admin, tid)
    return owner, owner_bounced, xp, xh


def purge_scheduled_deletions() -> list[str]:
    """Irreversibly wipe tenants whose delete-with-grace
    window has elapsed (status='pending_delete' AND delete_after <= now).
    Also covers accounts an add-on froze under one of its lockout
    statuses and that were never restored — same grace-purge shape, same
    worker sweep, so no separate purge path is needed for them. Reuses the
    console's purge path so external-service release + audit are
    identical. Returns the tenant ids purged.

    Each tenant is claimed under a row lock with the eligibility re-checked
    inside the transaction (SELECT … FOR UPDATE) and a claim stamp written,
    so a restore landing mid-run cannot be half-wiped: a restore that takes
    the lock first wins, and once the stamp is set the restore door
    refuses until the purge finishes or gives the household up
    (`_purge_one` has the phases). A purge that fails leaves the tenant frozen (retried next
    night) and writes an admin_audit row so a permanently-stuck tenant is
    visible in the console, not silently frozen forever."""
    from ..web.adminconsole import _audit, _frozen_statuses
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
        if _gate_unavailable():
            # Nothing can archive or release the billing add-on's record
            # while it is not loaded, and the erasure would take the only
            # copy of it. Nothing is claimed or released; tomorrow's run
            # finishes once the add-on loads.
            if candidates:
                log.error("scheduled purge of %d household(s) held: the "
                          "billing add-on is not loaded", len(candidates))
            return done
        for tid in candidates:
            owner, owner_bounced = None, False
            try:
                got = _purge_one(admin, tid, statuses, _audit)
                if got is None:
                    continue          # restored or already handled
                owner, owner_bounced, xp, xh = got
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


def _close_last_month(conn) -> bool:
    """Re-freeze the month that just closed from the budget live now — the
    budget it ended under.

    The nightly upserts the current month's snapshot at one fixed hour of
    the instance's clock, so for most households its last write of a month
    lands before that month's final local day is over, so without a
    re-freeze an edit made on the last evening (Today said "on budget")
    would be missing from the closed month's verdict. This hourly pass re-freezes it at
    local 00:xx on the 1st, so an edit made later on the 1st never reaches
    back into the month before.

    That hour can be missed: the worker down or redeploying at 00:05, the
    previous sweep still holding this household's lock, or a zone whose
    clocks skip local midnight on the 1st. A closed month whose snapshot
    was taken before the month ended (captured_at earlier than the local
    midnight that closed it) has never had this re-freeze, so the next
    sweep that finds it so takes it then. Once re-frozen, captured_at is
    past the close and it is never touched again. Either way it re-freezes
    only from settings nobody has saved since the month ended."""
    from ..engine import budget as _b
    from .. import localtime
    local = localtime.now_local(_b.load_config(conn))
    closed = (local.replace(day=1) - dt.timedelta(days=1)).date()
    # local midnight that opened this month, as an instant: in a zone
    # whose midnight does not exist that day, zoneinfo resolves the wall
    # time with the offset in force before the gap, which is the moment
    # the old month ended
    opened = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if not (local.day == 1 and local.hour == 0):
        row = conn.execute(
            "SELECT captured_at FROM budget_snapshots "
            "WHERE year=%s AND month=%s",
            (closed.year, closed.month)).fetchone()
        if row is None or row["captured_at"] >= opened:
            # no snapshot means the budget was not in use when the month
            # ran (the nightly skips a $0 plan); writing one now would
            # judge it against today's numbers
            return False
    # The live settings are the ones the month ended under only while
    # nobody has saved them since it ended. A late run (the catch-up), and
    # even the on-time one when the owner edits in the first minutes of the
    # 1st, would otherwise freeze a new-month edit into the closed month and
    # flip its verdict. Such a run writes nothing, and the nightly's last
    # snapshot of the month stands. A month whose re-freeze was skipped
    # this way is asked again each hour, which costs one row read and stays
    # a no-op once the settings have moved on.
    return _b.snapshot_month(conn, closed, unchanged_since=opened)


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
            # before the cadences: a monthly report set for the first hour
            # must read the closed month's final snapshot
            try:
                _close_last_month(conn)
            except Exception as e:               # noqa: BLE001
                log.warning("month-close snapshot failed tenant=%s: %s",
                            tenant_id, e)
            due = emails_due(conn, now)
            # each cadence on its own: a failing daily must not stop the
            # monthly (or, on Jan 1, the yearly) from being tried, since
            # those come due on that one day only. The first
            # failure is re-raised once every cadence has had its turn, so
            # the sweep still records the tenant as failed.
            first_error = None
            for cadence in due:
                try:
                    email_tenant(tenant_id, cadence=cadence)
                except Exception as e:           # noqa: BLE001
                    log.error("%s report failed for tenant %s: %s",
                              cadence, tenant_id, e)
                    first_error = first_error or e
            if first_error is not None:
                raise first_error
        finally:
            tenancy.release_lock(conn, f"oikonome:email:{tenant_id}")
    finally:
        conn.close()


def _webhook_tenants() -> list[str]:
    """Tenants with a delivery due — one admin query instead of a
    tenant-scoped connection per household per minute. Longest-waiting
    first, so a pass that runs out of minute does not always leave the
    same households for last.

    Only live households: a suspended or pending-delete household is locked
    out of every other door and sweep, so its queued deliveries must not
    keep carrying ledger rows to an outside URL either. The rows stay
    pending, so a reactivation resumes them."""
    admin = tenancy.admin_connect()
    try:
        return [str(r["tenant_id"]) for r in admin.execute(
            "SELECT d.tenant_id FROM webhook_deliveries d "
            "JOIN tenants t ON t.id = d.tenant_id "
            "AND coalesce(t.status, 'active') = 'active' "
            "WHERE d.state = 'pending' AND d.next_attempt_at <= now() "
            "GROUP BY d.tenant_id "
            "ORDER BY min(d.next_attempt_at)").fetchall()]
    finally:
        admin.close()


def webhooks_drain_tenant(tenant_id: str) -> dict:
    # a connection per database step, none while a receiver answers: the
    # drain runs several households at once out of the pool every other
    # sweep and webhook-triggered sync borrows from (deliver_pending says
    # why that matters)
    from ..notify import webhooks as _wh
    return _wh.deliver_pending(
        connect=lambda: tenancy.tenant_connect(tenant_id))


# held for the length of one drain pass. arq's job timeout cancels the
# coroutine but not the thread it awaits, so a pass that overran its
# minute would otherwise have the next one start beside it, and every
# minute another thread would join the default executor that the sync,
# email and nightly sweeps also run on
_WEBHOOK_PASS = threading.Lock()
# how many households one drain pass sends for at once, and how long a pass
# keeps starting new ones: inside the minute, so the next pass is not
# skipped behind it (a household already sending finishes its own budget)
WEBHOOK_DRAIN_THREADS = 4
WEBHOOK_PASS_BUDGET = 40.0


def webhooks_drain() -> dict:
    """Send every due webhook delivery, per tenant, isolating failures.
    Does nothing while the previous pass is still running."""
    if not _WEBHOOK_PASS.acquire(blocking=False):
        log.info("webhook drain skipped — the previous pass is still "
                 "running")
        return {}
    try:
        out: dict[str, str] = {}
        due = iter(_webhook_tenants())
        pick = threading.Lock()
        started = time.monotonic()

        def run() -> None:
            # Each household's receivers answer in their own time, so the
            # tenants are shared out over a few threads: one household's
            # slow-but-successful receiver keeps one thread busy for its
            # budget, not every other household waiting behind it. A
            # household not started before the pass budget runs out stays
            # due for the next minute, where longest-waiting goes first.
            while True:
                with pick:
                    if time.monotonic() - started >= WEBHOOK_PASS_BUDGET:
                        return
                    tid = next(due, None)
                if tid is None:
                    return
                try:
                    r = webhooks_drain_tenant(tid)
                    out[tid] = f"sent:{r['sent']} failed:{r['failed']}"
                except Exception as e:           # noqa: BLE001
                    out[tid] = f"error:{type(e).__name__}"
                    log.error("webhook drain failed for tenant %s: %s",
                              tid, e)
        threads = [threading.Thread(target=run, name=f"webhook-drain-{i}",
                                    daemon=True)
                   for i in range(WEBHOOK_DRAIN_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out
    finally:
        _WEBHOOK_PASS.release()


async def webhooks_all(ctx):
    """Every minute: the webhook outbox. A delivery is queued by whatever
    observed the event and sent here, so a slow receiver never holds a
    request or a sync."""
    return await asyncio.to_thread(webhooks_drain)


# How many email sweep threads are sending in this worker. A household's
# send is several steps (each recipient's mail, then SMS and push, then the
# stamp that says it went), and a restart between them sends it all again
# after the restart; the maintenance reboot waits while this is above zero.
# Counted inside the thread, so a sweep arq has stopped waiting for still
# counts until it really ends.
_EMAIL_THREADS = 0
_EMAIL_THREADS_LOCK = threading.Lock()


def _counted_email_sweep():
    global _EMAIL_THREADS
    with _EMAIL_THREADS_LOCK:
        _EMAIL_THREADS += 1
    try:
        return _sweep(_email_if_due, "email")
    finally:
        with _EMAIL_THREADS_LOCK:
            _EMAIL_THREADS -= 1


def _email_threads_running() -> int:
    with _EMAIL_THREADS_LOCK:
        return _EMAIL_THREADS


async def email_all(ctx):
    """Hourly sweep — each tenant sends at ITS configured hour, once/day."""
    return await asyncio.to_thread(_counted_email_sweep)


def _redis_settings():
    from arq.connections import RedisSettings
    return RedisSettings.from_dsn(os.environ.get("REDIS_URL",
                                                 "redis://localhost:6379/0"))


async def sync_one(ctx, tenant_id: str):
    """The admin console's per-tenant 'sync now' — one tenant,
    immediately, off the hourly cadence."""
    return await asyncio.to_thread(sync_tenant, tenant_id)


def never_pulled(conn, item_id: str) -> bool:
    """A newly linked Item no pull has finished yet, well or badly: every
    Plaid pull writes a sync_log row when it ends (success or Plaid error).
    Linked within the last day — an older Item without a row (a restored
    household's, say) is an ordinary one."""
    return conn.execute(
        "SELECT 1 FROM items i WHERE i.id=%s "
        "AND i.linked_at > now() - interval '1 day' "
        "AND NOT EXISTS (SELECT 1 FROM sync_log s WHERE s.item_id = i.id)",
        (item_id,)).fetchone() is not None


def _sync_item_body(tenant_id: str, item_id: str,
                    first: bool = False) -> dict:
    """One plaid item, one tenant — the webhook-driven scoped sync
    (SYNC_UPDATES_AVAILABLE). Pull, then the same best-effort sync-time
    categorize pass the hourly sweep runs, so webhook-fresh rows don't sit
    uncategorized until the next poll.

    `first`: a newly linked Item's first pull, queued by the link poll.
    It also pulls holdings and liabilities (a new brokerage must not wait
    for the daily products pull), and announces nothing to the household's
    webhooks — the rows it lands are history, not new charges."""
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
        # An Item no pull has touched yet gets its first pull whichever job
        # reaches it: Plaid's INITIAL_UPDATE / HISTORICAL_UPDATE can beat
        # the queued first-sync job, and a plain pull would announce the
        # new bank's recent history to the household's webhooks as new
        # charges and leave its holdings to the daily products pull.
        if not first and never_pulled(conn, item_id):
            first = True
        # Plaid's webhook is how most charges arrive where it is wired, so
        # this pull announces what it landed exactly as the full sweep
        # does; the sweep's own before-snapshot already holds these rows
        # and would never name them. A first-sync job that arrives after
        # another job already made the Item's first pull announces too:
        # what it lands now is new, not history.
        pulled = first and conn.execute(
            "SELECT 1 FROM sync_log WHERE item_id=%s LIMIT 1",
            (item_id,)).fetchone() is not None
        before_ids = None if first and not pulled else _recent_txn_ids(conn)
        try:
            r = plaid.sync(conn, item_id)
        except Exception as e:
            _webhooks_after_sync(conn, {item_id: f"error:{type(e).__name__}"},
                                 before_ids, tenant_id)
            raise
        _webhooks_after_sync(conn, {item_id: f"ok:{(r or {}).get('added', 0)}"},
                             before_ids, tenant_id)
        if first:
            try:
                plaid.sync_products(conn, item_id)
            except Exception as e:               # noqa: BLE001
                log.warning("plaid first products failed item=%s: %s",
                            item_id, e)
            # the first bank in: with one checking account the cash
            # forecast's anchor is obvious, so it is pinned here rather
            # than left as an open step on the Today checklist
            try:
                from ..engine import forecast as _forecast
                _forecast.auto_pin_primary_checking(conn)
            except Exception as e:               # noqa: BLE001
                log.warning("primary checking auto-pick failed item=%s: %s",
                            item_id, e)
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
            else:
                # this pull covered one item; a full sync turned away
                # while it ran asked for every other one
                answer_sync_nudge(conn, tenant_id)
        if conn is not None:
            conn.close()


def _is_never_pulled(tenant_id: str, item_id: str) -> bool:
    """never_pulled on a short connection of its own, given straight back."""
    conn = tenancy.tenant_connect(tenant_id)
    try:
        return never_pulled(conn, item_id)
    finally:
        conn.close()


async def sync_item(ctx, tenant_id: str, item_id: str, first: bool = False):
    """Webhook receiver → scoped sync (web/plaid_webhook.py enqueues), and
    a new Item's first pull (the link poll queues it, queue_first_sync).

    A first pull that finds another sync holding the household's lock is
    tried again later: the holder covers the transactions (the nudge), but
    only this job pulls the new Item's holdings and liabilities. At most
    FIRST_SYNC_CONCURRENCY first pulls run at once — each holds a pooled
    connection for minutes — and one beyond that is tried again shortly
    rather than holding a job slot while it waits. A plain job that
    reaches an Item no pull has finished IS its first pull (Plaid's
    INITIAL_UPDATE often beats the queued first-sync job), so it waits for
    a turn the same way; behind the household's lock it is covered by the
    holder like any webhook pull. Waiting for a turn spends none of the job's
    attempts, and turns go oldest job first.

    Every way out of the first-sync road but a wait gives up the job's
    place in the turn queue (the finally below): a webhook that comes back
    to find another job pulled its Item, a pull that raises, a give-up. A
    place left behind keeps its old enqueue time ahead of every younger
    first pull and holds one of the few turns while nothing runs."""
    from arq.worker import Retry
    asked_first = first
    key = ctx.get("job_id") or f"sync_item:{tenant_id}:{item_id}"
    waiting = False
    try:
        if not first:
            first = await asyncio.to_thread(_is_never_pulled, tenant_id,
                                            item_id)
            if not first:
                return await asyncio.to_thread(_sync_item_body, tenant_id,
                                               item_id)
        queued = ctx.get("enqueue_time")
        queued_at = queued.timestamp() if isinstance(queued, dt.datetime) \
            else None
        if not _FIRST_SYNC_SLOTS.take(key, queued_at):
            if _first_sync_wait_spent(key, queued_at):
                log.warning("first sync of item %s gave up waiting for a "
                            "turn; the hourly sync queues it again", item_id)
                return {"skipped": "no-turn"}
            waiting = True
            raise Retry(defer=FIRST_SYNC_SLOT_WAIT_S)
        r = await _first_pull_with_turn(tenant_id, item_id)
        if asked_first and isinstance(r, dict) \
                and r.get("skipped") == "already-running":
            # (a webhook's pull is covered by the holder's nudge, and the
            # queued first-sync job still comes for the holdings)
            tries = await _first_sync_real_try(ctx, key)
            if tries < SYNC_ITEM_MAX_TRIES:
                waiting = True
                raise Retry(defer=min(FIRST_SYNC_RETRY_S * tries,
                                      FIRST_SYNC_RETRY_MAX_S))
            log.warning("first sync of item %s gave up behind a busy "
                        "household lock; the hourly sync queues it again",
                        item_id)
        return r
    finally:
        if not waiting:
            _FIRST_SYNC_SLOTS.forget(key)
            if asked_first:
                await _forget_first_sync_tries(ctx, key)


async def _first_pull_with_turn(tenant_id: str, item_id: str):
    """The first pull, on the turn sync_item just took.

    The turn is the thread's to give back: a job arq cancels at its
    timeout leaves its thread pulling, and the pull still counts. Given
    back here only when the thread never started."""
    guard = threading.Lock()
    state = {"held": True, "started": False}

    def _give_back():
        with guard:
            if state["held"]:
                state["held"] = False
                _FIRST_SYNC_SLOTS.release()

    def _run():
        with guard:
            if not state["held"]:
                return {"skipped": "abandoned"}
            state["started"] = True
        try:
            return _sync_item_body(tenant_id, item_id, True)
        finally:
            _give_back()
    try:
        return await asyncio.to_thread(_run)
    except BaseException:
        with guard:
            started = state["started"]
        if not started:
            _give_back()
        raise


def _first_sync_wait_spent(key: str, queued_at: float | None) -> bool:
    """Has this job waited out its time for a turn? Measured from when the
    job was queued, which arq keeps with the job: the waits the process
    counted start again at zero when the worker restarts, and a job that
    had already waited hours would get a fresh budget and run into arq's
    max_tries instead. The count is the fallback for a job with no
    enqueue time."""
    if queued_at is not None:
        return time.time() - queued_at > FIRST_SYNC_MAX_WAIT_S
    return _FIRST_SYNC_SLOTS.waits(key) > FIRST_SYNC_MAX_SLOT_WAITS


def _first_sync_tries_key(key: str) -> str:
    return f"oikonome:first-sync-tries:{key}"


async def _first_sync_real_try(ctx, key: str) -> int:
    """Which real attempt behind a busy household lock this is, its waits
    for a turn aside. Counted in Redis beside arq's own try count, so a
    worker restart, which forgets the waits the process counted, cannot
    make a job that waited a hundred times look like it tried a hundred
    times. Without Redis it is arq's try count less the waits."""
    redis = ctx.get("redis")
    if redis is not None:
        name = _first_sync_tries_key(key)
        try:
            n = await redis.incr(name)
            await redis.expire(name, FIRST_SYNC_TRIES_TTL_S)
            return max(1, int(n))
        except Exception as e:                     # noqa: BLE001
            log.warning("first-sync try count unavailable for %s: %s",
                        key, e)
    return max(1, ctx.get("job_try", 1) - _FIRST_SYNC_SLOTS.waits(key))


async def _forget_first_sync_tries(ctx, key: str) -> None:
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        await redis.delete(_first_sync_tries_key(key))
    except Exception as e:                         # noqa: BLE001
        # the count expires on its own; a job that ended keeps nothing
        log.info("first-sync try count not cleared for %s: %s", key, e)


class _FirstSyncTurns:
    """FIRST_SYNC_CONCURRENCY turns at first pulls, handed out oldest job
    first. A job that finds no turn free asks again FIRST_SYNC_SLOT_WAIT_S
    later (arq re-scores it behind newer jobs, so without the queue a new
    Item could take every turn an older one was waiting for); its place is
    its original enqueue time, kept while it keeps asking. Counts each
    job's waits, so they never spend the attempts the job has for real
    work. Per worker process, like the turns themselves."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._busy = 0
        self._waiting: dict = {}     # job -> [enqueued_at, last asked]
        self._waits: dict = {}       # job -> [waits, last asked]

    def _prune(self, now: float) -> None:
        stale = now - FIRST_SYNC_WAITER_STALE_S
        for k in [k for k, v in self._waiting.items() if v[1] < stale]:
            del self._waiting[k]
        old = now - 86400
        for k in [k for k, v in self._waits.items() if v[1] < old]:
            del self._waits[k]

    def take(self, key: str, enqueued_at: float | None = None,
             now: float | None = None) -> bool:
        """A turn for job `key` if one is free and no older job still
        asking is ahead of it for it; else its wait is counted."""
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            w = self._waiting.get(key)
            if w is None:
                w = self._waiting[key] = [
                    enqueued_at if enqueued_at is not None else now, now]
            w[1] = now
            mine = (w[0], key)
            ahead = sum(1 for k, v in self._waiting.items()
                        if (v[0], k) < mine)
            if self._busy + ahead < self.capacity:
                self._busy += 1
                del self._waiting[key]
                return True
            c = self._waits.setdefault(key, [0, now])
            c[0] += 1
            c[1] = now
            return False

    def waits(self, key: str) -> int:
        with self._lock:
            c = self._waits.get(key)
            return c[0] if c else 0

    def forget(self, key: str) -> None:
        with self._lock:
            self._waiting.pop(key, None)
            self._waits.pop(key, None)

    def acquire(self, blocking: bool = False) -> bool:
        """A turn regardless of who waits — for a caller outside the queue."""
        with self._lock:
            if self._busy < self.capacity:
                self._busy += 1
                return True
            return False

    def release(self) -> None:
        with self._lock:
            if self._busy <= 0:
                raise ValueError("first-sync turn released too many times")
            self._busy -= 1


# How long a first pull waits before trying again behind a sync that holds
# the household's lock; each retry waits one step longer, up to the cap.
FIRST_SYNC_RETRY_S = 60
FIRST_SYNC_RETRY_MAX_S = 300
# How many new Items' first pulls run at once, and how long one waits for
# a turn. A burst of new households linking banks would otherwise put
# every job slot — and as many pooled connections — on multi-minute
# backfills while ordinary syncs and webhooks queue behind them.
def _first_sync_concurrency() -> int:
    try:
        return max(1, int(env_num("OIKONOME_FIRST_SYNC_CONCURRENCY", "3")))
    except ValueError:
        log.warning("OIKONOME_FIRST_SYNC_CONCURRENCY is not a whole "
                    "number — using 3")
        return 3


FIRST_SYNC_CONCURRENCY = _first_sync_concurrency()
FIRST_SYNC_SLOT_WAIT_S = 30
# A waiting job that stopped asking (dropped, its worker restarted) holds
# no place in the queue after this; it gets its place back if it asks again.
FIRST_SYNC_WAITER_STALE_S = 4 * FIRST_SYNC_SLOT_WAIT_S
# How long a first pull waits for a turn before leaving the Item to the
# hourly sync: a burst of new households can queue dozens of multi-minute
# first pulls behind a handful of turns.
FIRST_SYNC_MAX_WAIT_S = 6 * 3600
FIRST_SYNC_MAX_SLOT_WAITS = FIRST_SYNC_MAX_WAIT_S // FIRST_SYNC_SLOT_WAIT_S
# A job's count of real attempts outlives no job: longer than any job can
# wait and retry, short enough that a job arq dropped leaves nothing.
FIRST_SYNC_TRIES_TTL_S = 24 * 3600
_FIRST_SYNC_SLOTS = _FirstSyncTurns(FIRST_SYNC_CONCURRENCY)
# The attempts a sync_item job gets at real work. A first pull retries
# behind the household's lock; under a burst of new links arq's default of
# five attempts can be used up before the lock frees, which would leave the
# new Item's history to the daily sync. Waits for a turn are counted apart
# (_FirstSyncTurns) and arq is given room for both.
SYNC_ITEM_MAX_TRIES = 40
# arq's own ceiling for a sync_item job: every wait for a turn the time
# budget allows, every real attempt, and a few tries arq re-runs when a
# worker shuts down under a job. The job's own budgets must run out first,
# so it ends with its give-up log rather than arq's "max retries exceeded".
SYNC_ITEM_ARQ_MAX_TRIES = SYNC_ITEM_MAX_TRIES + FIRST_SYNC_MAX_SLOT_WAITS + 20
# One Item's pull — a new Item's first is up to two years of history, page
# by page with merchant resolution — and one household's full sync both
# take minutes on a large ledger; arq's 300-second default cancelled them
# while their thread ran on.
SYNC_TIMEOUT = 30 * 60


# A finish exchanges a token and runs the new Item's first sync, which for
# a multi-year ledger takes minutes, and a sweep may finish several in a
# row. arq's 300-second default would cancel the job while its thread kept
# running; the outcome is recorded when the Item's row commits, so a
# cancelled finish is safe, but the window is sized to the work anyway.
PLAID_LINK_TIMEOUT = 30 * 60


async def plaid_link_finish(ctx, tenant_id: str, link_token: str):
    """LINK webhook → finish a Hosted Link add session nobody may be
    polling (web/plaid_webhook.py enqueues; jobs/plaid_links.py)."""
    from . import plaid_links
    return await asyncio.to_thread(plaid_links.finish_session, tenant_id,
                                   link_token)


async def plaid_link_sweep(ctx):
    """Every 5 minutes: finish open Hosted Link add sessions the webhook
    did not (jobs/plaid_links.sweep)."""
    from . import plaid_links
    return await asyncio.to_thread(plaid_links.sweep)


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


class _HouseholdGone(Exception):
    """The household stopped being active while a send was being built."""


def _live_mail_route(tenant_id, recipients: list[str],
                     kind: str | None) -> tuple[list[str], dict]:
    """`recipients` cut to the addresses still deliverable NOW, and the
    relay as resolved NOW, read on a fresh connection right before the
    fan-out. An outbound pause (a hijacked recipient, a relay a hijacker
    may have pointed) committed while the report was being built is
    honoured; an address added since is not picked up, since the report
    was not built for it.

    FOR SHARE on the settings row first, for the reason `_live_sms_number`
    gives: a pause still in flight is waited for, then read. The lock is
    released before any mail is sent."""
    from ..web import report
    conn = tenancy.tenant_connect(tenant_id)
    try:
        with conn.transaction():
            conn.execute("SELECT 1 FROM tenant_settings WHERE tenant_id=%s "
                         "FOR SHARE", (tenant_id,))
            live = set(_recipients_detail(conn, tenant_id, kind)[0])
            smtp = report.resolve_smtp(conn)
            # a household suspended or put up for erasure since the sweep
            # listed it is mailed nothing
            if not _household_active(conn, tenant_id):
                log.info("tenant %s: mail skipped — the household is no "
                         "longer active", tenant_id)
                live = set()
    finally:
        conn.close()
    return [r for r in recipients if r in live], smtp


def _live_sms_number(tenant_id) -> str | None:
    """The household's SMS number as stored NOW, or None unless it is still
    verified and still consented to the scheduled summary. Read on a fresh
    connection, right before a send, so a pause or removal committed after
    the sweep loaded its config is honoured.

    FOR SHARE, so a pause still IN FLIGHT is honoured too: a credential
    rotation marks the number unverified inside its own transaction and
    commits only when the rest of the rotation has, and a plain read in
    that window still sees the number as verified. The share lock waits
    for that commit and then reads the paused number. It is released
    before the text is sent — held across the provider's answer, it would
    keep the rotation, and the control-plane connection the rotation runs
    on, waiting for it."""
    from ..notify import sms as _smsmod
    try:
        conn = tenancy.tenant_connect(tenant_id)
        try:
            with conn.transaction():
                row = conn.execute(
                    "SELECT config->'notify_phone' AS p FROM tenant_settings "
                    "WHERE tenant_id=%s FOR SHARE", (tenant_id,)).fetchone()
                # nor is a household that is no longer active texted
                active = _household_active(conn, tenant_id)
        finally:
            conn.close()
    except Exception as e:                       # noqa: BLE001
        log.warning("tenant %s: SMS number re-read failed: %s", tenant_id, e)
        return None
    if not active:
        return None
    p = row["p"] if row else None
    if not (isinstance(p, dict) and p.get("verified") and p.get("number")
            and _smsmod.consented(p, "summary")):
        return None
    return str(p["number"])


# the host command runner's own ceiling on how long a run holds its lock;
# a result still reading "running" past it is one a crash or a reboot
# left behind, and must not defer the maintenance reboot forever
_HOST_COMMAND_MAX_S = 6 * 3600


def _host_command_running(base) -> bool:
    """Whether the host command watcher has a console command queued or
    in flight, as far as the container can see: a request still in the
    drop-box, or a result file the watcher wrote as "running" within its
    lock's lifetime. Unreadable files are skipped, not trusted."""
    import json
    if (base / "cmd" / "command-requested").exists():
        return True
    res = base / "ops" / "cmd-results"
    if not res.is_dir():
        return False
    now = time.time()
    for j in res.glob("*.json"):
        try:
            d = json.loads(j.read_text())
            if (isinstance(d, dict) and d.get("state") == "running"
                    and now - float(d.get("started") or 0)
                    < _HOST_COMMAND_MAX_S):
                return True
        except (OSError, ValueError, TypeError):
            continue
    return False


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
    # the reboot watcher specifically: the command watcher alone also
    # makes the drop-box writable, and then the countdown would be
    # broadcast every night for a restart nothing performs
    from . import host_watchers
    if not host_watchers.ready("reboot"):
        return "no-cmd-dir"
    # nor a console command the host is still running: a reboot under an
    # upgrade kills it mid-build or mid-migrate, leaving the schema part
    # upgraded under the old containers and the command "running" forever
    if _host_command_running(base):
        log.info("maintenance reboot deferred — a console command is "
                 "running on the host")
        return "host-command-running"
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


# the maintenance reboot's hour, UTC — what OIKONOME_AUTO_REBOOT's
# documentation promises operators, whatever zone the instance runs in
REBOOT_HOUR_UTC = 9


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def nightly_reboot(ctx):
    # the cron fires every hour at :37 in the instance's zone; only the
    # firing that lands in the UTC hour does anything, so the reboot stays
    # in the 09:00 UTC hour on every install and across DST changes
    if _utcnow().hour != REBOOT_HOUR_UTC:
        return "not-the-hour"
    # the nightly runs at a LOCAL hour and the reboot at a UTC one, so in
    # some zones the two meet; a reboot never cuts a nightly short (the
    # pending reboot is still pending tomorrow)
    if _NIGHTLY_RUNS > 0 or _step_threads_running() > 0:
        log.info("maintenance reboot deferred — the nightly run is in "
                 "progress")
        return "nightly-running"
    # nor a household's email half-sent: cut between its mail and the stamp
    # that records it, the whole send (mail, SMS, push) goes out again
    # after the restart
    if _email_threads_running() > 0:
        log.info("maintenance reboot deferred — the email sweep is sending")
        return "email-sending"
    return await asyncio.to_thread(_nightly_reboot_check)


def _cron_zone():
    """The zone the cron hours are read in: the instance's, by NAME, so a
    DST change moves the clock with it instead of shifting every job an
    hour."""
    from .. import localtime
    return localtime.instance_tz()


@dataclasses.dataclass
class _ZonedCronJob(CronJob):
    """A cron entry whose fields are wall-clock times in `zone` but whose
    next run is an absolute UTC instant.

    arq compares next_run with "now" as plain datetimes; given the same
    named zone on both sides Python compares them as wall times and
    ignores the fold, so on the night the clocks go back the next slot
    after 01:55 (02:00, an hour and five minutes away in real time) looks
    later than every repeated 01:xx and every cron stalls for the hour.
    Here the worker's clock runs in UTC and the next run is found by
    walking real instants forward and reading each one's local wall time,
    so nothing stalls or doubles when the offset changes:
      * fall-back: interval jobs keep firing through the repeated hour;
        a job pinned to an hour runs only on the first pass through it.
      * spring-forward: the skipped wall times simply never occur; a job
        pinned to a skipped hour does not run that day."""
    zone: dt.tzinfo | None = None

    def calculate_next(self, prev_run: dt.datetime) -> None:
        self.next_run = _next_zoned_run(self, prev_run)


def _as_set(v):
    return {v} if isinstance(v, int) else set(v)


def _next_zoned_run(job, prev_run: dt.datetime) -> dt.datetime:
    utc = dt.timezone.utc
    zone = job.zone or utc
    after = prev_run.astimezone(utc)
    weekday = job.weekday
    if isinstance(weekday, str):
        from arq.typing import WEEKDAYS
        weekday = WEEKDAYS.index(weekday.lower())
    month = None if job.month is None else _as_set(job.month)
    day = None if job.day is None else _as_set(job.day)
    wday = None if weekday is None else _as_set(weekday)
    hour = None if job.hour is None else _as_set(job.hour)
    minute = None if job.minute is None else _as_set(job.minute)
    seconds = (range(60) if job.second is None
               else sorted(_as_set(job.second)))
    t = after.replace(second=0, microsecond=0)
    # a year and a bit of hour-steps bounds the walk for any satisfiable
    # entry (the rarest, a fixed day of one month, recurs within a year)
    for _ in range(24 * 400 + 120):
        local = t.astimezone(zone)
        if ((month is not None and local.month not in month)
                or (day is not None and local.day not in day)
                or (wday is not None and local.weekday() not in wday)
                or (hour is not None
                    and (local.hour not in hour or local.fold))):
            # nothing in the rest of this local hour can match: jump to
            # the next local :00
            t += dt.timedelta(minutes=60 - local.minute)
            continue
        if minute is not None and local.minute not in minute:
            t += dt.timedelta(minutes=1)
            continue
        for sec in seconds:
            cand = t.replace(second=sec, microsecond=job.microsecond)
            if cand > after:
                return cand
        t += dt.timedelta(minutes=1)
    raise RuntimeError(f"cron {job.name} has no next run")


def _in_zone(job: CronJob, zone) -> _ZonedCronJob:
    """The same cron entry, read as wall times in `zone`."""
    fields = {f.name: getattr(job, f.name)
              for f in dataclasses.fields(CronJob)}
    return _ZonedCronJob(**fields, zone=zone)


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
    try:
        from ..web import mailguard, report as _report
        operator = mailguard.operator_addresses()
        smtp = _report._env_smtp()
        if operator and smtp["configured"]:
            _report.send(subject, plain, html, operator, smtp=smtp,
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


async def staging_sweep(ctx):
    """Hourly: drop every household's expired staged uploads. The sweep
    inside the staging calls reaches only the household making the call,
    and a staged file still carries the full account numbers the importers
    cut down, so an abandoned one must not wait for its household to come
    back. Runs here and never on a request: it reads every household's
    rows on the owner connection."""
    def _run():
        from ..db import staging
        return staging.sweep_all()
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:                               # noqa: BLE001
        log.error("staging sweep failed: %s", e)
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


# Every job runs its work in a thread (asyncio.to_thread), so the loop's
# default executor is the worker's real capacity — and asyncio sizes it
# from the CPU count (min(32, cpu_count + 4)), which is few threads on a
# small host. A job waiting for a thread is already running as far as arq
# is concerned, its timeout counting down, so when the executor is
# saturated a queued job's timeout can expire before its work starts,
# cancelling syncs and cron sweeps that never ran. The executor is sized here, and arq takes
# fewer jobs at once than there are threads: a nightly step that overran
# its budget keeps its thread after its job has moved on (_nightly_step),
# and the spare threads keep it from crowding out the jobs behind it.
# Each running job holds about one pooled tenant connection, so unset the
# thread count follows the worker's own pool: OIKONOME_POOL_MAX as the
# worker sees it (compose maps OIKONOME_WORKER_POOL_MAX onto it) plus the
# headroom, which keeps the jobs arq takes at once within the pool.
WORKER_THREAD_HEADROOM = 2


def _pool_max() -> int:
    try:
        return max(1, int(os.environ.get("OIKONOME_POOL_MAX") or "10"))
    except ValueError:
        return 10


def _worker_threads() -> int:
    default = _pool_max() + WORKER_THREAD_HEADROOM
    try:
        return max(2, int(env_num("OIKONOME_WORKER_THREADS", str(default))))
    except ValueError:
        log.warning("OIKONOME_WORKER_THREADS is not a whole number — "
                    "using %s", default)
        return default


WORKER_THREADS = _worker_threads()
WORKER_MAX_JOBS = max(1, WORKER_THREADS - WORKER_THREAD_HEADROOM)


def _size_executor() -> None:
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=WORKER_THREADS,
                           thread_name_prefix="job"))


async def _startup(ctx):
    """Post-boot hygiene: a reboot broadcast whose deadline has passed by
    the time the worker is up means the restart is OVER — clear it so
    users aren't stuck reading a stale countdown."""
    _size_executor()
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
    # The cron hours below are the instance's local time (OIKONOME_TZ), as
    # its documentation says — the one exception is the maintenance reboot,
    # which keeps its documented UTC hour itself (nightly_reboot). arq's
    # own clock runs in UTC and each entry carries the named zone
    # (_ZonedCronJob): a fixed offset captured at start slid every job an
    # hour across a DST change, and handing arq the named zone stalled
    # every cron for the repeated hour at fall-back.
    timezone = dt.timezone.utc
    # nightly_all is listed with its own timeout too: the console's run-now
    # button enqueues it by name, and that path takes the function's
    # timeout, not the cron entry's
    max_jobs = WORKER_MAX_JOBS
    functions = [sync_all, arq_func(nightly_all, timeout=NIGHTLY_TIMEOUT),
                 email_all, demo_reset_all,
                 arq_func(sync_one, timeout=SYNC_TIMEOUT),
                 arq_func(sync_item, timeout=SYNC_TIMEOUT,
                          max_tries=SYNC_ITEM_ARQ_MAX_TRIES),
                 broadcast_sweep, nightly_reboot,
                 blocklist_refresh, webhooks_all,
                 staging_sweep,
                 arq_func(plaid_link_finish, timeout=PLAID_LINK_TIMEOUT),
                 arq_func(plaid_link_sweep, timeout=PLAID_LINK_TIMEOUT),
                 arq_func(bulk_import_job.bulk_import_run,
                          timeout=bulk_import_job.ARQ_TIMEOUT)] + ext.job_functions()
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
        cron(nightly_all, hour=4, minute=17, timeout=NIGHTLY_TIMEOUT),
        cron(blocklist_refresh, hour=3, minute=30),     # daily spam-list refresh
        cron(email_all, minute=5),                      # hourly; per-tenant hour
        cron(demo_reset_all, minute=0),                 # hourly pristine
        cron(staging_sweep, minute=47),                 # abandoned uploads
        cron(bulk_import_job.bulk_import_sweep,         # queued folder imports,
             minute=set(range(60)),                     # and ones whose worker
             timeout=bulk_import_job.ARQ_TIMEOUT),     # went quiet
        cron(broadcast_sweep,                           # stale reboot banners
             minute={2, 12, 22, 32, 42, 52}),
        # graceful maint window, the 09:00 UTC hour: hourly here because the
        # crons run in local time, and the job itself keeps the UTC hour.
        # :37, clear of the email sweep at :05 (the host goes down about a
        # minute after the flag, so that minute must not overlap an hourly
        # email start) and between sync slots
        cron(nightly_reboot, minute=37),
        cron(webhooks_all, minute=set(range(60))),      # the webhook outbox
        cron(plaid_link_sweep,                          # unpolled bank links
             minute=set(range(3, 60, 5)), timeout=PLAID_LINK_TIMEOUT),
    ] + ext.cron_jobs()
    cron_jobs = [_in_zone(j, _cron_zone()) for j in cron_jobs]
    redis_settings = _redis_settings()
