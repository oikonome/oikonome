"""Transitional Jinja pages: account classification + file import (the
wizard's follow-on steps). The SPA replaces these; the routes stay thin
over web/data + sync importers.
"""

from __future__ import annotations

import csv as csvmod
import io
import time

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, \
    UploadFile
from urllib.parse import quote

from fastapi.responses import HTMLResponse, RedirectResponse

from ..db import tenancy
from ..envnum import env_flag
from . import demoguard, mailguard, permissions
from .security import limit
import datetime as _dt


from ..engine import bills, budget
from ..engine.compat import as_dict
from ..sync import base as sync_base
from ..sync import batches, competitors, csvimport, mintimport, \
    ofximport, pdfimport, plaid, qifimport, simplefin, ynabimport

router = APIRouter()

KINDS = [("depository/checking", "checking"),
         ("depository/savings", "savings"),
         ("credit/credit card", "credit card"),
         ("investment/brokerage", "investment"),
         ("loan/", "loan")]

# CSV header auto-detection (lowercased): first match wins per field
CSV_HEADERS = {
    "date": ("date", "transaction date", "posted date", "post date"),
    "amount": ("amount", "transaction amount", "amount (usd)"),
    "name": ("description", "name", "payee", "memo", "details"),
    "merchant": ("merchant", "payee"),
    "category": ("category",),
    # some exports carry two one-sided columns instead of a signed
    # Amount — used only when no amount column matched (see _auto_map)
    "debit": ("debit", "debit amount", "withdrawal", "withdrawals",
              "money out"),
    "credit": ("credit", "credit amount", "deposit", "deposits",
               "money in"),
}


def _user():
    from .app import current_user
    return current_user


def _tpl(name: str, **ctx) -> HTMLResponse:
    from . import todayview
    return HTMLResponse(todayview._env.get_template(name).render(**ctx))


def _accounts(conn):
    return conn.execute(
        """SELECT a.id, COALESCE(a.display_name, a.name) AS name, a.mask,
                  a.type || '/' || COALESCE(a.subtype,'') AS kind,
                  a.balance_current, i.institution_name
           FROM accounts a JOIN items i ON i.id = a.item_id
           ORDER BY i.institution_name, name""").fetchall()


def _connections(conn):
    """Aggregator items with health for the Accounts page (status dot,
    last successful pull, per-item sync/fix affordances)."""
    # effective status folds in webhook_status: an out-of-band ITEM webhook
    # (PENDING_EXPIRATION, revoked, error) must surface the fix/re-auth
    # door even while polling still succeeds
    # auto-removed (reaped / gone-at-Plaid) items stay
    # visible with status 'reaped' — the persistent "connection removed —
    # reconnect" state on the Accounts page. User-initiated disconnects
    # (archived, no reason) stay hidden as before.
    rows = conn.execute(
        """SELECT i.id, i.aggregator, i.institution_name,
                  CASE WHEN COALESCE(i.status,'') = 'archived' THEN 'reaped'
                       ELSE COALESCE(NULLIF(i.status, 'ok'),
                                     i.webhook_status, 'ok') END AS status,
                  MAX(s.ran_at) FILTER (WHERE s.error IS NULL) AS last_ok,
                  -- two different clocks, and only one of them was ever
                  -- shown: last_ok is when WE last read Plaid's copy,
                  -- bank_updated_at is when the BANK last refreshed that
                  -- copy. A quiet sync is explained by the second one.
                  i.bank_updated_at, i.bank_failed_at,
                  -- why a billed product returns nothing here (consent
                  -- never granted vs the bank cannot provide it), stamped
                  -- by the sync — the card explains a forecast with no
                  -- autopay line instead of leaving it silently absent
                  i.raw->'product_issues' AS product_issues,
                  -- Plaid's fleet-wide login health for the bank, cached
                  -- by the hourly sync; only a fresh reading counts — a
                  -- stale DEGRADED from a stopped sync would outlive the
                  -- outage it described
                  CASE WHEN (i.raw->'institution_health'->>'as_of')
                                ::timestamptz > now() - interval '24 hours'
                       THEN i.raw->'institution_health'->>'logins'
                  END AS institution_health,
                  (SELECT COUNT(*) FROM accounts a
                   WHERE a.item_id = i.id
                     AND a.user_removed_at IS NULL) AS accounts
           FROM items i LEFT JOIN sync_log s ON s.item_id = i.id
           -- simplefin-org = the per-institution child items that actually
           -- OWN the accounts (the 'simplefin' bridge holds only the token,
           -- no accounts). Without these the SPA couldn't match an account
           -- to its connection and mislabeled SimpleFIN accounts "Import".
           WHERE i.aggregator IN ('plaid','simplefin','simplefin-org','mx')
             AND (COALESCE(i.status,'') != 'archived'
                  OR i.archived_reason IN ('auto-reap','plaid-gone','sub-ended'))
           GROUP BY i.id, i.aggregator, i.institution_name, i.status,
                    i.webhook_status, i.archived_reason,
                    i.bank_updated_at, i.bank_failed_at, i.raw
           ORDER BY i.institution_name, i.id""").fetchall()
    # What the status MEANS, decided once here rather than by each client
    # re-deriving it from a raw Plaid code. Treating every non-'ok' status
    # identically — red, "sync failing", and a re-auth button — is wrong
    # for an institution outage: update mode cannot fix a bank that is
    # down, so the button looks like it works and teaches people the
    # warning is noise.
    out = []
    for r in rows:
        d = dict(r)
        d["status_kind"] = plaid.status_kind(d.get("status"))
        out.append(d)
    return out


@router.get("/accounts")
def accounts_page(user: dict = Depends(_user()), msg: str = ""):
    # Jinja page retired: the SPA owns the UI — a second server-rendered
    # menu is only a second thing to keep in step. Body below is dead.
    return RedirectResponse("/app/accounts" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@router.post("/accounts/classify")
def classify(user: dict = Depends(_user()), account_id: str = Form(...),
             kind: str = Form(...), primary_checking: str = Form("")):
    typ, _, sub = kind.partition("/")
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        # the legacy form door, same rules as /api/accounts/classify: one
        # transaction, the account row locked, and a business account is
        # never pinned as the personal runway's checking unless the
        # household combines entities
        with budget.config_txn(conn) as cfg:
            row = conn.execute(
                "SELECT entity_id FROM accounts WHERE id=%s FOR UPDATE",
                (account_id,)).fetchone()
            if row is None:
                return RedirectResponse("/accounts?msg=account+not+found",
                                        status_code=303)
            if primary_checking and row["entity_id"] \
                    and not cfg.get("combine_entities"):
                return RedirectResponse(
                    "/accounts?msg=" + quote(
                        "a business account cannot be the household's "
                        "primary checking"), status_code=303)
            # the same door the API's classify uses: pins the type AND
            # re-signs a SimpleFIN balance when the kind crosses into or out
            # of debt — a bare UPDATE left the bank's sign until the next pull
            sync_base.mark_type_user_set(conn, account_id, typ, sub or None)
            if primary_checking:
                cfg["checking_account_id"] = account_id
            elif cfg.get("checking_account_id") == account_id:
                cfg.pop("checking_account_id")
    finally:
        conn.close()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/simplefin")
def connect_simplefin(user: dict = Depends(_user()),
                      simplefin_token: str = Form(...)):
    """Connect (or retry) a SimpleFIN bank feed after setup — the wizard is
    one-shot, so this is the only post-setup path."""
    demoguard.deny(user)
    from .api import _no_byo_on_hosted
    _no_byo_on_hosted()
    import datetime as _dt
    import urllib.parse as _u

    from ..sync import simplefin as _sf
    from ..sync.base import tenant_sync_lock
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
      with tenant_sync_lock(conn, user["tenant_id"]) as held:
        if not held:
            return RedirectResponse(
                "/accounts?msg=" + _u.quote("A sync is already running — "
                                            "give it a moment."),
                status_code=303)
        try:
            access = _sf.claim_setup_token(simplefin_token)
            r = _sf.sync(conn, "sfin-main", access,
                         since=_dt.date.today() - _dt.timedelta(days=90))
            msg = (f"Bank connected — {r.get('accounts', 0)} accounts, "
                   f"{r.get('transactions', 0)} transactions pulled.")
        except Exception:  # noqa: BLE001 — show, don't 500
            import logging
            logging.getLogger("oikonome.simplefin").exception(
                "simplefin connect failed")
            msg = ("Connection failed — check the token (one-time use, "
                   "unexpired) and try again.")
    finally:
        conn.close()
    return RedirectResponse("/accounts?msg=" + _u.quote(msg),
                            status_code=303)


@router.post("/accounts/add")
def add_account(user: dict = Depends(_user()), name: str = Form(...),
                kind: str = Form("depository/checking"),
                balance: str = Form("")):
    """Manual account for CSV/OFX-only setups (no aggregator item exists).
    Starting balance defaults to 0 — a NULL balance would file the brand-
    new account under 'archived/closed' and make the cash runway scream
    'out of money' on day one."""
    import re
    typ, _, sub = kind.partition("/")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "account"
    import math
    try:
        bal = float(balance.replace(",", "").replace("$", "").strip() or 0)
        if not math.isfinite(bal):
            bal = 0.0
    except ValueError:
        bal = 0.0
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        conn.execute(
            """INSERT INTO items (id, aggregator, institution_name, access_token)
               VALUES ('manual','manual','Manual',NULL)
               ON CONFLICT (tenant_id, id) DO NOTHING""")
        conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype,
                                     balance_current, updated_at)
               VALUES (%s,'manual',%s,%s,%s,%s,now())
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (f"manual:{slug}", name.strip(), typ, sub or None, bal))
    finally:
        conn.close()
    return RedirectResponse("/accounts", status_code=303)


@router.get("/alerts")
def alerts_page(user: dict = Depends(_user())):
    # Jinja page retired: the SPA owns the UI — a second server-rendered
    # menu is only a second thing to keep in step. Body below is dead.
    return RedirectResponse("/app/alerts", status_code=303)


def _csv_cell(v):
    """CSV-safe cell. JSONB columns come back as dict/list; DictWriter's
    str() would write the Python repr (single quotes) which json.loads on
    the restore side rejects — so raw/evidence/by_class round-tripped as
    NULL through product→product export/restore. Serialize real JSON."""
    if isinstance(v, (dict, list)):
        import json as _j
        return _j.dumps(v)
    # Spreadsheet formula injection: a cell beginning = + - @ — with or
    # without an invisible character in front of it (tab, CR, LF, BOM,
    # space) — is prefixed with a quote so Excel/Calc treat it as text.
    # payee / merchant_name / Amazon order titles are counterparty-
    # controlled and ride this full-data export. Delegated to api._csv_safe
    # rather than restated here: two copies of the rule is how this export
    # ended up with the narrower half of it.
    if isinstance(v, str):
        from .api import _csv_safe
        return _csv_safe(v)
    # Receipt images are BYTEA — base64 in the CSV (restore
    # decodes); str() on bytes would write the Python repr and destroy them
    if isinstance(v, (bytes, memoryview)):
        import base64 as _b64
        return _b64.b64encode(bytes(v)).decode()
    return v


def _tenant_count() -> int:
    """Tenants on this instance (control plane, no RLS — needs the admin
    connection, same DSN pg_dump itself uses below). Module-level so tests
    can model a true single-tenant box against the shared multi-tenant
    test database."""
    from ..db import tenancy as _tenancy
    conn = _tenancy.admin_connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM tenants").fetchone()["n"]
    finally:
        conn.close()


# ---- one-shot export tickets -------------------------------------------
# The two data doors below must not be plain GETs on the session cookie.
# That cookie is SameSite=Lax, and Lax cookies DO ride a top-level
# cross-site GET — so `<a href="https://<instance>/export">` on any page
# the signed-in owner visits would drop the household ZIP (receipt images
# included) into their Downloads, and on a single-tenant self-host the
# whole pg_dump. The origin-check middleware only wraps write methods, so
# nothing there stands in the way. The credential bundle next door demands
# password + live TOTP; the data door needs no less.
#
# Downloads still want a GET (browsers save a navigation, and the mobile
# app fetches a URL), so the shape is: POST /api/export/token steps up exactly
# like the connections bundle and mints a one-shot ticket; the GET redeems
# it within a minute. The ticket is a bearer credential like the passkey
# step-up ticket, so it lives the same way: sha256 at rest, bound to the
# user, deleted on read.
EXPORT_TICKET_TTL = _dt.timedelta(seconds=60)
_EXPORT_KINDS = ("zip", "dump")


def _mint_export_ticket(user_id, kind: str) -> str:
    import hashlib
    import secrets

    from .app import _control_conn
    ticket = "oikx-" + secrets.token_urlsafe(24)
    with _control_conn() as conn:
        conn.execute(
            """INSERT INTO webauthn_challenges (purpose, user_id, challenge,
                                                expires_at)
               VALUES (%s, %s, %s, %s)""",
            (f"export-ticket:{kind}", user_id,
             hashlib.sha256(ticket.encode()).hexdigest(),
             _dt.datetime.now(_dt.timezone.utc) + EXPORT_TICKET_TTL))
        conn.execute("DELETE FROM webauthn_challenges WHERE expires_at < now()")
    return ticket


def _redeem_export_ticket(user_id, kind: str, ticket: str) -> None:
    """Burn the ticket or refuse the download. Deleting on read is what
    makes it single-use — a leaked URL replays to nothing."""
    import hashlib

    from .app import _control_conn
    # 403, not 401: a page-route 401 bounces to /login, which would tell a
    # signed-in owner nothing about what actually happened
    if not ticket or not ticket.startswith("oikx-"):
        raise HTTPException(403, "export_ticket_required: POST "
                                 "/api/export/token with your password first")
    with _control_conn() as conn:
        row = conn.execute(
            """DELETE FROM webauthn_challenges
               WHERE purpose = %s AND user_id = %s AND challenge = %s
                 AND expires_at > now()
               RETURNING id""",
            (f"export-ticket:{kind}", user_id,
             hashlib.sha256(ticket.encode()).hexdigest())).fetchone()
    if row is None:
        raise HTTPException(403, "export ticket expired or already used — "
                                 "request the download again")


@router.post("/api/export/token")
def export_token(user: dict = Depends(_user()), body: dict = Body(...)):
    """Re-prove identity and mint the one-shot ticket that GET /export or
    /export/dump will redeem. Same proof the connections bundle demands —
    a FRESH elevation (seconds old), or the legacy in-body password (+ a
    live code on a TOTP account): a stolen cookie alone must not walk off
    with the household's data either, and everything leaves through this
    door, so a proof given minutes ago for something else is too stale."""
    from .app import _require_elevation
    kind = str(body.get("kind") or "zip")
    if kind not in _EXPORT_KINDS:
        raise HTTPException(400, "kind must be zip or dump")
    if kind == "dump" and user["role"] != "owner":
        raise HTTPException(403, "owner only")
    _require_elevation(user, fresh_seconds=60,
                       password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    ticket = _mint_export_ticket(user["user_id"], kind)
    path = "/export/dump" if kind == "dump" else "/export"
    return {"ok": True, "token": ticket,
            "url": f"{path}?t={quote(ticket)}",
            "expires_in": int(EXPORT_TICKET_TTL.total_seconds())}


@router.get("/export/dump")
def export_dump(user: dict = Depends(_user()), t: str = ""):
    """Exact-fidelity database dump (pg_dump, plain SQL, gzipped) — the
    SAME format the manager's backups produce, so `./oikonome.sh restore
    <file>` consumes it directly. Owner-only: the dump is the whole
    instance, not one user's view. Restore stays in the manager on purpose
    (confirm-gated, stops the app first)."""
    import datetime as _dt2
    import shutil
    import subprocess
    import zlib

    from fastapi.responses import StreamingResponse


    from ..db import tenancy as _tenancy
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    # HOSTED gate: pg_dump bypasses RLS and dumps EVERY
    # tenant's rows (incl. password hashes + encrypted secrets). Safe on a
    # single-tenant self-hosted box; on the shared hosted DB an owner would
    # download every household's data. Blocked there — hosted export is the
    # per-tenant CSV/restore ZIP only.
    if env_flag("OIKONOME_HOSTED"):
        raise HTTPException(
            403, "the full database dump is a self-hosted feature; use the "
                 "CSV export for a per-account copy of your data")
    # The HOSTED flag is an operator PROMISE, not a fact
    # about the data — a multi-tenant instance without the flag would still
    # hand any single owner every household's rows. Gate on the actual
    # tenant count too; single-tenant self-host (the supported dump case)
    # is unchanged.
    if _tenant_count() > 1:
        raise HTTPException(
            403, "this instance has more than one tenant — the full "
                 "database dump would include every household's data; use "
                 "the CSV export for a copy of your own")
    if shutil.which("pg_dump") is None:
        raise HTTPException(500, "pg_dump not available in this image — "
                                 "upgrade the instance (git pull + install)")
    # last, so a refused gate above does not burn the ticket
    _redeem_export_ticket(user["user_id"], "dump", t)
    stamp = f"{_dt2.datetime.now():%Y%m%d-%H%M%S}"

    def stream():
        import threading
        proc = subprocess.Popen(
            ["pg_dump", "--dbname", _tenancy.ADMIN_DSN, "--no-owner"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # drain stderr in a thread — pg_dump warnings past the ~64KB pipe
        # buffer would otherwise block its stdout and hang the stream
        errbuf: list[bytes] = []
        errt = threading.Thread(
            target=lambda: errbuf.append(proc.stderr.read() if proc.stderr
                                         else b""), daemon=True)
        errt.start()
        gz = zlib.compressobj(wbits=31)          # gzip container
        try:
            # not `assert` — python -O strips asserts, turning a narrowing
            # aid into an AttributeError inside a streaming response
            if proc.stdout is None:
                raise RuntimeError("pg_dump produced no stdout pipe")
            while chunk := proc.stdout.read(64 * 1024):
                if out := gz.compress(chunk):
                    yield out
            # verify success BEFORE flushing the gzip trailer, so a failed
            # dump never looks like a complete file
            rc = proc.wait(timeout=60)
            errt.join(timeout=5)
            if rc != 0:
                raise RuntimeError(
                    "pg_dump failed: "
                    + (b"".join(errbuf)[:400]).decode(errors="replace"))
            yield gz.flush()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)             # reap — no zombie

    return StreamingResponse(
        stream(), media_type="application/gzip",
        headers={"Content-Disposition":
                 f'attachment; filename="oikonome-{stamp}.sql.gz"'})


# /export table membership is SCHEMA-DRIVEN: every RLS-enabled tenant_id
# table travels unless it is on EXPORT_SKIP — the same discovery the
# portable archive uses (tenant_export.rls_tenant_tables). A hand-
# maintained include-list fails silently: a table promised elsewhere in
# the codebase can be missing here for as long as nobody exports it and
# looks. A new domain table exports by default; forgetting is not
# possible, only deliberately skipping is — each skip states its reason,
# mirroring test_tenant_table_coverage's doctrine.
EXPORT_SKIP = {
    "tenant_keys",       # envelope key material — secret, never travels
    "import_staging",    # import scratch
    "job_progress",      # job bookkeeping
    "job_runs",          # job bookkeeping / heartbeats
    "feedback_reports",  # operational ring buffer (may embed log lines)
    "sync_log",          # sync history (may embed provider error text)
    "script_heartbeats",  # collector liveness, meaningless off-instance
    # learned categorizer overlay — a binary model artifact, not user-
    # legible data; derived from merchant_categories (which exports fully)
    # and retrained nightly, so the export carries the source, not the cache
    "categorizer_model",
    # Which Plaid account adopted which restored one, and the date before
    # which the restore is authoritative. Instance-LOCAL by nature: the
    # ids are this instance's, and carrying it into a fresh one would tell
    # that instance to drop a backfill it has never held.
    "account_adoptions",
}

# PRESENTATION only — a human-friendly file order for the ZIP and its
# README. Membership never depends on this list; discovered tables it
# doesn't name simply follow it alphabetically.
_EXPORT_ORDER = ["items", "accounts", "transactions", "bills",
                 "bill_proposals", "reimbursements", "reimburse_flags",
                 "business_flags",
                 "manual_categories", "alerts_log", "import_batches",
                 "liabilities", "holdings", "crypto_holdings",
                 "networth_recorded", "networth_snapshot",
                 "income_annual", "income_documents",
                 "merchant_canonical", "merchant_renames",
                 "merchant_categories", "merchant_merge_proposals",
                 "amazon_orders", "amazon_matches", "amazon_summaries",
                 "business_entity", "entity_membership", "equity_movement",
                 "business_txn_class", "compliance_obligation", "mileage_log",
                 "vendor_1099", "transaction_notes",
                 "account_links",
                 "receipts", "receipt_items",
                 "budget_snapshots",
                 "tenant_settings"]


@router.get("/export")
def export_data(user: dict = Depends(_user()), t: str = ""):
    """Full-data export: one ZIP of CSVs — data portability (self↔hosted
    migration, CCPA). Streams the tenant's own rows only (RLS). Redeems
    the one-shot ticket POST /api/export/token minted (see the note above
    _mint_export_ticket for why a bare cookie is not enough).

    Owner-only, stated here as well as on the ticket door. A member cannot
    mint a ticket, so this is belt to that brace — but a GET never passes
    the write gate that carries the rest of the role policy, and a download
    door that reads its own authorization is one that stays right when the
    ticket flow is next rearranged."""
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    _redeem_export_ticket(user["user_id"], "zip", t)

    import os as _os
    import tempfile as _tf

    from fastapi.responses import StreamingResponse

    from ..sync import export as _export
    # Built on disk and streamed back — the archive carries receipt
    # images, so holding it as a bytes object would be the process's
    # biggest allocation. The file is UNLINKED as soon as it is reopened
    # for streaming: the inode lives until the handle closes, so an
    # interrupted download (a phone backgrounding mid-transfer) can never
    # leave a household's full ledger behind on disk — a background-task
    # cleanup would only run after a COMPLETE send.
    tmp = _tf.NamedTemporaryFile(prefix="oik-export-", suffix=".zip",
                                 delete=False)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        _export.build_zip(conn, out=tmp)
        tmp.close()
        fh = open(tmp.name, "rb")
    except BaseException:
        tmp.close()
        _os.unlink(tmp.name)
        raise
    finally:
        conn.close()
    _os.unlink(tmp.name)

    def _stream():
        try:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            fh.close()
    return StreamingResponse(
        _stream(), media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="oikonome-export.zip"'})


@router.get("/settings")
def settings_page(user: dict = Depends(_user()), msg: str = ""):
    # Jinja page retired: the SPA owns the UI — a second server-rendered
    # menu is only a second thing to keep in step. Body below is dead.
    return RedirectResponse("/app/settings" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@router.post("/settings")
def settings_save(user: dict = Depends(_user()),
                  food_monthly: float = Form(0.0),
                  other_monthly: float = Form(0.0),
                  budgeted_income_monthly: float = Form(0.0),
                  dynamic_variable_budget: str = Form("")):
    # Demo must not reconfigure shared budgets via the legacy
    # form path (SPA /api/settings already demoguards).
    demoguard.deny(user)
    # Pydantic parses 'inf'/'Infinity' as a valid float, and
    # json.dumps then emits a literal Postgres rejects — a 500 out of the
    # jsonb write. api.py's _num guards its door; this sibling form door
    # (and set_balance two functions down) must match.
    import math as _math
    for _v, _n in ((food_monthly, "food_monthly"),
                   (other_monthly, "other_monthly"),
                   (budgeted_income_monthly, "budgeted_income_monthly")):
        if not _math.isfinite(_v):
            raise HTTPException(400, f"{_n} must be a finite number")
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        with budget.config_txn(conn) as cfg:
            cfg["food_monthly"] = max(0.0, food_monthly)
            cfg["other_monthly"] = max(0.0, other_monthly)
            if budgeted_income_monthly > 0:
                cfg["budgeted_income_monthly"] = budgeted_income_monthly
            else:
                cfg.pop("budgeted_income_monthly", None)
            cfg["dynamic_variable_budget"] = bool(dynamic_variable_budget)
    finally:
        conn.close()
    return RedirectResponse("/settings?msg=Saved.", status_code=303)


@router.post("/settings/email")
def settings_email(user: dict = Depends(_user()),
                   email_recipients: str = Form(""),
                   email_send_hour_utc: int = Form(14)):
    # recipient config is an egress door — demo instances never
    # send mail (Doctor already says "email disabled — demo instance"),
    # exactly like the /api/settings door's refused-keys list
    demoguard.deny(user)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        recips = mailguard.parse_recipients(email_recipients)
        # this form writes the same config key as POST /api/settings, so
        # it owes the same hosted member-only policy — the shared check
        # keeps both doors identical
        mailguard.check_recipients(conn, recips)
        with budget.config_txn(conn) as cfg:
            prev = cfg.get("email_recipients")
            if recips:
                cfg["email_recipients"] = recips
            else:
                cfg.pop("email_recipients", None)
            cfg["email_send_hour_utc"] = max(0, min(23, email_send_hour_utc))
        # and the same for the invite half: a policy that lives in one
        # door drifts out of the other one. Added addresses are invited,
        # never enrolled.
        # Computed AFTER the lock commits, like the /api/settings door: the
        # invite describes a save that must already be durable.
        added = mailguard.added_recipients(prev, cfg.get("email_recipients"))
    finally:
        conn.close()
    if added:
        mailguard.invite_added(user, added)
    return RedirectResponse("/settings?msg=Saved.", status_code=303)


@router.post("/accounts/balance")
def set_balance(user: dict = Depends(_user()), account_id: str = Form(...),
                balance: str = Form("")):
    """Manual balance for import-only accounts (aggregator accounts get
    balances from sync — this only touches manual items)."""
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        owned = conn.execute(
            "SELECT 1 FROM accounts WHERE id=%s AND item_id='manual'",
            (account_id,)).fetchone()
        if owned:
            val = None
            s = balance.replace(",", "").replace("$", "").strip()
            if s:
                import math
                try:
                    v = float(s)
                    # reject NaN/Infinity: they store fine in a DOUBLE column
                    # but then permanently 500 every JSON response containing
                    # the row (allow_nan=False) — same class as _money_float.
                    val = v if math.isfinite(v) else None
                except ValueError:
                    val = None
            conn.execute("UPDATE accounts SET balance_current=%s, "
                         "balance_available=%s, updated_at=now() WHERE id=%s",
                         (val, val, account_id))
    finally:
        conn.close()
    return RedirectResponse("/accounts", status_code=303)


# ---- Plaid: hosted-link add / re-auth + per-item manual sync ---------------
#
# The link flow: create a Hosted
# Link session (no client-side Plaid SDK needed), send the user to Plaid's
# hosted page in a new tab, poll /link/token/get server-side, exchange the
# public token (add) or verify + clear the error status (update/re-auth).
# Session state is in-memory keyed by link_token — fine for the 30-minute
# link lifetime (an app restart mid-link just means retrying); each session
# is pinned to the tenant that created it.

# In-process because a Hosted Link session lives for one browser visit and
# the container runs a SINGLE uvicorn worker (cli.serve passes no --workers).
# If that ever changes this has to move to Redis or the staging table, or a
# link page served by the other process will bounce straight back to
# /accounts with the session apparently lost.
_PLAID_LINK: dict[str, dict] = {}
# PER TENANT, not global. One shared 20-entry cap would let a busy tenant's
# link flows evict a different household's in-flight session — that person
# just sees their link page redirect away with nothing said. 20 concurrent
# link sessions for ONE tenant is already far past plausible.
_PLAID_LINK_MAX = 20
# Plaid's own hosted-link tokens expire in hours; anything older than this is
# an abandoned tab, and evicting by age beats evicting the oldest survivor.
_PLAID_LINK_TTL = 6 * 3600


def _start_plaid_session(user: dict, item_id: str = "",
                         manage_accounts: bool = False) -> dict:
    """Create a Hosted Link session and pin it in `_PLAID_LINK`.

    Returns ``{link_token, hosted_link_url, kind, institution}``. Raises
    HTTPException (404) or ValueError (institution cap) or plaid.PlaidError.

    manage_accounts (update mode only) opens Link's shared-accounts
    checklist so the user can DESELECT an account — the only lever that
    stops Plaid billing it. A deselected account simply stops being
    returned; the status route auto-hides its local row on completion.
    """
    demoguard.deny(user)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        access_token, inst, kind = None, None, "add"
        if item_id:
            row = conn.execute(
                "SELECT id, institution_name, status FROM items "
                "WHERE id=%s AND aggregator='plaid'", (item_id,)).fetchone()
            if not row:
                raise HTTPException(404, "unknown item")
            # An archived item is not re-authenticated, it is re-LINKED: a
            # disconnect whose Plaid release failed keeps its token for the
            # reaper's retry, and an update session on it would hand that
            # token a new lease. The clients already send no item_id for
            # these; the server now refuses the door as well.
            # (ValueError: the form route turns it into the same redirect-
            # with-message the institution cap uses; JSON callers get a 400)
            if (row["status"] or "") == "archived":
                raise ValueError(
                    "That connection was removed — link the bank again.")
            access_token = sync_base.get_access_token(conn, item_id)
            if not access_token:
                raise ValueError(
                    "That connection has no credentials to refresh — link "
                    "the bank again.")
            inst, kind = row["institution_name"], "update"
        else:
            # hosted soft-cap on connected institutions — only the
            # ADD door; update/re-auth of an existing item is always allowed
            sync_base.check_institution_cap(conn)
        client = plaid.Client.for_tenant(conn)
        created = client.create_hosted_link(
            str(user["tenant_id"]), client_name="Oikonome",
            access_token=access_token,
            account_selection=bool(item_id and manage_accounts), conn=conn)
    finally:
        conn.close()
    tid = str(user["tenant_id"])
    now = time.time()
    # drop anything expired, whoever owns it
    for k in [k for k, v in _PLAID_LINK.items()
              if now - v.get("at", now) > _PLAID_LINK_TTL]:
        _PLAID_LINK.pop(k, None)
    # then trim THIS tenant's own oldest, never anyone else's
    mine = [k for k, v in _PLAID_LINK.items() if v["tenant_id"] == tid]
    while len(mine) >= _PLAID_LINK_MAX:
        _PLAID_LINK.pop(mine.pop(0), None)
    lt = created["link_token"]
    url = created["hosted_link_url"]
    _PLAID_LINK[lt] = {"tenant_id": tid,
                       "item_id": item_id or None, "kind": kind,
                       "manage_accounts": bool(item_id and manage_accounts),
                       "institution": inst, "at": now,
                       "url": url, "done": False}
    return {"link_token": lt, "hosted_link_url": url,
            "kind": kind, "institution": inst}


@router.post("/accounts/plaid/link")
def plaid_link_start(request: Request, user: dict = Depends(_user()),
                     item_id: str = Form(""),
                     manage_accounts: str = Form("")):
    """Start a Hosted Link session: add mode (no item_id) or update/re-auth.

    SPA (Accept: application/json): returns the hosted URL so the client can
    open **only** Plaid and poll /status while staying on Welcome/Accounts —
    no intermediate oikonome waiting tab.

    Form / no Accept: keeps the legacy 303 → waiting-page flow for tests and
    non-SPA callers.
    """
    want_json = "application/json" in (request.headers.get("accept") or "")
    try:
        created = _start_plaid_session(user, item_id,
                                       manage_accounts=manage_accounts == "1")
    except ValueError as e:
        if want_json:
            raise HTTPException(400, str(e))
        return RedirectResponse("/accounts?msg=" + quote(str(e)),
                                status_code=303)
    except plaid.PlaidError as e:
        msg = f"Plaid link failed — {e.code}: {e.message}"
        if want_json:
            raise HTTPException(400, msg)
        return RedirectResponse("/accounts?msg=" + quote(msg), status_code=303)
    if want_json:
        return created
    return RedirectResponse(
        f"/accounts/plaid/link/{created['link_token']}", status_code=303)


def _plaid_session(link_token: str, user: dict) -> dict | None:
    # The POST that starts a session is owner-only via the global write
    # gate (bank connections are the owner's, members included); these
    # completion routes are GETs, which that gate waves through — yet the
    # status poll is where the token exchange, first sync and item-status
    # writes actually happen. Same role rule, enforced here where the gate
    # can't see it.
    if user.get("role") != "owner":
        raise HTTPException(
            403, "connecting a bank is owner-only — ask the account owner")
    sess = _PLAID_LINK.get(link_token)
    if sess is None or sess["tenant_id"] != str(user["tenant_id"]):
        return None                              # cross-tenant guard
    return sess


@router.get("/accounts/plaid/link/{link_token}")
def plaid_link_page(link_token: str, user: dict = Depends(_user())):
    sess = _plaid_session(link_token, user)
    if not sess:
        return RedirectResponse("/accounts", status_code=303)
    return _tpl("link.html", title="Link account", link_token=link_token, centered=False,
                url=sess["url"], kind=sess["kind"],
                institution=sess["institution"])


def _apply_account_selection(conn, client, token, item_id: str) -> None:
    """After a shared-accounts (account-selection) update completes, hide
    the item's LIVE accounts that Plaid no longer returns: the user just
    deselected them, so Plaid stops serving (and billing) them, and their
    local rows would otherwise linger looking connected with a
    forever-stale balance.

    Live-served only — balance_current IS NOT NULL — because legacy
    history imports ride under the same item with hand-shaped ids and a
    NULL balance, and hiding those would pull years of closed-account
    history out of net worth and cash flow.

    Hide-only, never unhide: an account re-selected later stays hidden
    until the user unhides it on the Accounts page, because this cannot
    tell a row IT hid apart from one the user hid on purpose for display.
    Sync already refuses hidden accounts' rows, so this also stops the
    data at our door. Best-effort: a Plaid hiccup here leaves rows
    visible-but-stale, which the next completed session cleans up."""
    try:
        returned = {a["account_id"] for a in client.post(
            "/accounts/get", {"access_token": token})["accounts"]}
    except Exception:                            # noqa: BLE001
        return
    if not returned:
        return
    n = conn.execute(
        "UPDATE accounts SET user_removed_at=now(), balance_current=NULL, "
        "balance_available=NULL "
        "WHERE item_id=%s AND user_removed_at IS NULL "
        "AND balance_current IS NOT NULL AND NOT (id = ANY(%s))",
        (item_id, list(returned))).rowcount
    if n:
        # a hidden member of a linked group must stop serving it — the
        # ambient shadow set follows the change within this request
        from ..engine import links as _links
        _links.set_shadow_scope(conn)


@router.get("/accounts/plaid/link/{link_token}/status")
def plaid_link_status(link_token: str, user: dict = Depends(_user())):
    """Polled by the link page. Completes the exchange + first sync (add)
    or verifies the item and clears its error status (update) the moment
    Plaid reports the session finished."""
    sess = _plaid_session(link_token, user)
    if not sess:
        raise HTTPException(404, "unknown link session")
    # The billing read-only gate is METHOD-based (app.current_user only
    # applies it to non-GET), and this GET is where the token exchange, the
    # item row and the first sync are actually written. A session started
    # while the tenant was in good standing stays pollable for its whole TTL,
    # so a lapse mid-session would otherwise add a billed Plaid item to a
    # read-only account. Same rule as the POST that opened the session.
    if sess["done"]:
        # nothing left to write — the exchange already happened while
        # the tenant was in good standing, so a lapse since then must not
        # turn a finished link into an error
        return {"done": True, "kind": sess["kind"],
                "institution": sess.get("institution")}
    from .app import _billing_write_blocked
    if _billing_write_blocked({"tenant_id": user["tenant_id"]}):
        raise HTTPException(402, "this account is read-only right now")
    from ..sync.base import tenant_sync_lock
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
      # the exchange + first sync (and the adopt-onto-restored path inside
      # it) write item/account/transaction state — under the same
      # per-tenant lock every scheduled sync and the background restore
      # take, or a link landing during a restore interleaves with its
      # uncommitted insert-only transaction. Busy = keep polling.
      with tenant_sync_lock(conn, user["tenant_id"]) as held:
        if not held:
            return {"done": False, "kind": "busy"}
        client = plaid.Client.for_tenant(conn)
        try:
            data = client.get_link_session(link_token)
        except plaid.PlaidError as e:
            return {"done": False, "error": str(e)}
        sessions = data.get("link_sessions") or []
        add_results = [
            (s.get("link_session_id"), r) for s in sessions
            for r in ((s.get("results") or {}).get("item_add_results") or [])]
        if add_results:
            names = []
            err = None
            for lsid, r in add_results:
                # link_session_id rides along: it is the identifier Plaid
                # support asks for when a link is investigated
                try:
                    iid = plaid.link_item(conn, r["public_token"],
                                          link_session_id=lsid)
                except plaid.PlaidError as e:
                    # Mark done so the waiting page stops polling; never 500
                    # (a raised error is re-tried every 3s and hammers Plaid).
                    err = f"{e.code}: {e.message}"
                    continue
                except ValueError as e:
                    # institution cap, re-checked at exchange time.
                    # Same "stop polling, show why" contract as a PlaidError —
                    # link_item has already handed the Item back to Plaid.
                    err = str(e)
                    continue
                row = conn.execute(
                    "SELECT institution_name FROM items WHERE id=%s",
                    (iid,)).fetchone()
                names.append(row["institution_name"] if row else None)
            sess["done"] = True
            sess["institution"] = (", ".join(n for n in names if n)
                                   or sess.get("institution"))
            if not names and err:
                sess["kind"] = "add_failed"
                return {"done": True, "kind": "add_failed",
                        "institution": sess.get("institution"),
                        "error": err}
            return {"done": True, "kind": "add",
                    "institution": sess["institution"]}
        if (not sess["item_id"]
                and any(s.get("finished_at") for s in sessions)):
            # ADD mode, session finished, nothing added: the user exited
            # Link without connecting a bank (the exit button stamps
            # finished_at too). Without this the poll falls through to
            # {"done": False} forever and the add-account button sits on
            # "Connecting…" for its full 15-minute deadline. Results can
            # lag the finished stamp by a beat, so require a second look
            # a few seconds later before declaring the session abandoned.
            now = time.time()
            first = sess.get("finished_empty_at")
            if first is None:
                sess["finished_empty_at"] = now
            elif now - first > 6:
                sess["done"] = True
                sess["kind"] = "exited"
                return {"done": True, "kind": "exited",
                        "institution": sess.get("institution")}
        if sess["item_id"] and any(s.get("finished_at") for s in sessions):
            # finished_at alone doesn't mean SUCCESS — Plaid stamps it when
            # the user abandons/errors out of the Link flow too. Verify
            # against the Item's live error state before declaring the
            # connection healthy; a false 'ok' here flips the dot green,
            # then the next hourly sync flips it back and the link alert
            # emails a bogus break notification.
            token = sync_base.get_access_token(conn, sess["item_id"])
            try:
                item_err = client.get_item(token)["item"].get("error")
            except Exception as e:               # noqa: BLE001
                item_err = {"error_code": str(e)[:80]}
            sess["done"] = True
            if item_err is None:
                # A successful update-mode re-auth clears the stale
                # webhook_status too (not just status) — otherwise the reaper,
                # which falls back to webhook_status when status='ok', could
                # reap this just-repaired Item before the next clean sync
                # clears the flag (both fire at 04:00 UTC).
                # ...but never an item a disconnect archived while this
                # session was open — the same guard every other status
                # writer carries, or a re-auth finishing late silently
                # reverts the user's disconnect
                revived = conn.execute(
                    "UPDATE items SET status='ok', webhook_status=NULL, "
                    "webhook_status_at=NULL WHERE id=%s "
                    "AND COALESCE(status,'') != 'archived'",
                    (sess["item_id"],)).rowcount
                if not revived:
                    item_err = {"error_code": "archived"}
                if sess.get("manage_accounts") and item_err is None:
                    _apply_account_selection(conn, client,
                                             sync_base.get_access_token(
                                                 conn, sess["item_id"]),
                                             sess["item_id"])
                return {"done": True, "kind": "update",
                        "institution": sess.get("institution")}
            sess["kind"] = "update_failed"
            return {"done": True, "kind": "update_failed",
                    "institution": sess.get("institution"),
                    "error": (item_err or {}).get("error_code")}
        return {"done": False}
    finally:
        conn.close()


def _when_phrase(ts) -> str:
    """'2 hours ago' / 'at 1:49 PM today' — coarse on purpose, and always
    relative to now, because the point is how STALE the bank's copy is."""
    now = _dt.datetime.now(ts.tzinfo) if ts.tzinfo else _dt.datetime.now()
    mins = max(0, int((now - ts).total_seconds() // 60))
    if mins < 90:
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    if mins < 48 * 60:
        return f"{mins // 60} hours ago"
    return f"{mins // (24 * 60)} days ago"


@router.post("/accounts/sync")
def account_sync(user: dict = Depends(_user()), item_id: str = Form(...)):
    """Manual per-item 'sync now' — same pulls the hourly worker does for
    this one item (plaid: transactions + liabilities/holdings products)."""
    demoguard.deny(user)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        # This door must take the per-tenant advisory lock every sibling
        # door (hourly sweep, /api/jobs/sync*, webhook scoped sync)
        # takes. Without it, two
        # tabs' "↻ sync" racing the same item both read one tx_cursor and
        # the loser's cursor write silently discards the winner's pull.
        # Same lock, same "already running" answer as the other doors.
        got = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{user['tenant_id']}",)).fetchone()["ok"]
        if not got:
            # the holder answers this request with one more pass before it
            # lets go, instead of the tenant waiting for the hourly catch-up
            from ..jobs.worker import _nudge_sync
            _nudge_sync(conn)
            return RedirectResponse(
                "/accounts?msg=" + quote("A sync is already running — "
                                         "give it a moment."),
                status_code=303)
        row = conn.execute(
            "SELECT id, aggregator, institution_name FROM items WHERE id=%s",
            (item_id,)).fetchone()
        if not row:
            msg = "unknown connection"
        else:
            name = row["institution_name"] or row["id"]
            try:
                if row["aggregator"] == "plaid":
                    r = plaid.sync(conn, item_id)
                    try:
                        plaid.sync_products(conn, item_id)
                    except Exception:            # noqa: BLE001 — best-effort
                        import logging
                        logging.getLogger("oikonome.plaid").exception(
                            "manual product sync failed item=%s", item_id)
                    _categorize_after_pull_async(user["tenant_id"])
                    n = r["added"]
                    if n:
                        msg = (f"{name} synced — {n} new transaction"
                               f"{'s' if n != 1 else ''}.")
                    else:
                        # "0 new transactions" reads as a failure. It
                        # almost never is: a sync reads the aggregator's
                        # copy, and the bank refreshes that copy a few
                        # times a day, so the useful answer is WHEN the
                        # bank last sent anything — the one figure that
                        # distinguishes "nothing happened" from "your
                        # bank has not reported since this morning".
                        when = conn.execute(
                            "SELECT bank_updated_at FROM items WHERE id=%s",
                            (item_id,)).fetchone()["bank_updated_at"]
                        msg = (f"{name}: nothing new. Your bank last sent "
                               f"data {_when_phrase(when)}."
                               if when else
                               f"{name}: nothing new to bring in.")
                elif row["aggregator"] == "simplefin":
                    token = sync_base.get_access_token(conn, item_id)
                    if not token:
                        raise ValueError("connection has no token — reconnect")
                    r = simplefin.sync(
                        conn, item_id, token,
                        since=_dt.date.today() - _dt.timedelta(days=30))
                    _categorize_after_pull_async(user["tenant_id"])
                    msg = (f"{name} synced — {r['transactions']} "
                           "transactions pulled.")
                else:
                    msg = f"{name} is a manual connection — nothing to sync."
            except Exception as e:               # noqa: BLE001 — show, don't 500
                # redact BEFORE truncating (a chopped credential must not
                # survive) — this msg rides a redirect query string into
                # browser history + access logs. Belt-and-suspenders with the
                # source-side _url_auth fix in simplefin.
                clean = sync_base.redact_url_credentials(str(e)) or ""
                msg = f"{name} sync failed: {clean[:160]}"
    finally:
        # explicit unlock: the connection is pooled, and a session-level
        # advisory lock survives a return-to-pool; a failed unlock drops
        # the connection instead (tenancy.release_lock)
        tenancy.release_lock(conn, f"oikonome:sync:{user['tenant_id']}")
        conn.close()
    return RedirectResponse("/accounts?msg=" + quote(msg), status_code=303)


def _spawn(fn) -> None:
    """Run `fn` off the request thread.

    Module-level and replaceable so a test can run the callable inline
    instead of racing a daemon thread."""
    import threading
    threading.Thread(target=fn, daemon=True).start()


def _categorize_after_pull_async(tenant_id: str) -> None:
    """Hand the sync-time categorize pass to a background thread and return.

    Run inline, the pass can spend one LLM call per merchant batch, each
    with a ten-minute client timeout — far longer than a reverse proxy in
    front of the app will wait. The proxy would time the request out while
    the work (and the per-tenant sync advisory lock it holds) carried on,
    so the person's natural retry would be told "a sync is already running".
    The pull is what the button promised; categorization is follow-up work,
    so it goes where the webhook already puts it — a worker with its OWN
    tenant connection, because the request's is pooled and goes back to the
    pool the moment the route answers.

    The pass is serialized on a dedicated per-tenant advisory lock, taken
    try-style and skipped when held. Two presses a few seconds apart would
    otherwise run two full passes over one ledger: wasted work, and
    apply()'s ledger-wide UPDATE racing itself is a deadlock candidate.
    Skipping is the bargain the merchant and Amazon locks inside
    categorize_new already strike — every step is pending-gated, so the
    holder, or failing that the hourly sweep, picks up whatever this pass
    would have done."""
    def _run() -> None:
        import logging
        _log = logging.getLogger("oikonome.sync")
        conn = None
        try:
            # a WEB pool slot for the length of the pass (LLM batches with
            # ten-minute timeouts): one of OIKONOME_POOL_MAX, released when
            # the pass ends — acceptable for a button, not for a sweep
            conn = tenancy.tenant_connect(tenant_id)
            with sync_base.categorize_lock(conn, tenant_id) as held:
                if not held:
                    _log.info("sync categorize already running — skipped")
                    return
                _categorize_after_pull(conn)
        except Exception as e:                   # noqa: BLE001 — best-effort
            _log.warning("background sync categorize failed: %s", e)
        finally:
            if conn is not None:
                conn.close()

    _spawn(_run)


def _categorize_after_pull(conn) -> None:
    """The same sync-time pass the hourly sweep and the webhook run after a
    pull: classify new merchants, apply the cached map, and stamp bill
    categories on this month's charges. Without it the rows a person just
    pulled with the ↻ button would sit under the aggregator's generic category —
    counted as variable spend and missing their bill match — until the
    next hourly run. Best-effort: categorization must never fail the sync
    that delivered the money."""
    import logging
    _log = logging.getLogger("oikonome.sync")
    try:
        from ..engine import llm_categorize as _llm
        _llm.categorize_new(conn)
    except Exception as e:                       # noqa: BLE001
        _log.warning("manual sync categorize failed: %s", e)
    try:
        from ..engine import bills as _bills
        _bills.apply_txn_categories(conn, days=120)
    except Exception as e:                       # noqa: BLE001
        _log.warning("manual sync bill txn-category apply failed: %s", e)


@router.get("/transactions")
def transactions_page(request: Request, user: dict = Depends(_user()),
                      y: int = 0, m: int = 0, q: str = "", acct: str = "",
                      cat: str = "", page: int = 1):
    # Jinja page retired: the SPA owns the UI — a second server-rendered
    # menu is only a second thing to keep in step. Body below is dead.
    return RedirectResponse("/app/transactions" + (f"?{request.url.query}" if request.url.query else ""), status_code=303)


@router.post("/transactions/recategorize")
def transactions_recategorize(user: dict = Depends(_user()),
                              txn_id: str = Form(...),
                              category: str = Form(""),
                              back: str = Form("/transactions")):
    from . import data
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        c = category.strip()
        if c == "__clear__":
            data.clear_category(conn, txn_id)
        elif c:
            data.set_category(conn, txn_id, c)
    finally:
        conn.close()
    dest = (back if back.startswith("/") and not back.startswith("//")
            and "\\" not in back else "/transactions")
    return RedirectResponse(dest, status_code=303)


def _cadence_label(frequency: str | None, interval) -> str:
    f = (frequency or "").upper()
    base = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly",
            "YEARLY": "yearly", "ENVELOPE": "envelope",
            "EVERY_WEEK": "weekly", "EVERY_MONTH": "monthly",
            "EVERY_YEAR": "yearly", "EVERY_DAY": "daily"}.get(f)
    try:
        iv = int(interval or 1)
    except (TypeError, ValueError):
        iv = 1        # raw can arrive verbatim from a restore ZIP
    if base == "envelope":
        # an envelope's interval is its pool length in months
        return "annual envelope" if iv >= 12 else "envelope"
    if not base:
        return "one-time" if not f else f.lower()
    if f.startswith("EVERY_X"):
        base = {"EVERY_X_WEEKS": "weeks", "EVERY_X_MONTHS": "months",
                "EVERY_X_DAYS": "days", "EVERY_X_YEARS": "years"}.get(f, "")
        return f"every {iv} {base}"
    return base if iv == 1 else f"every {iv} {base.rstrip('ly')}s"


def _proposal_summary(p) -> str:
    ev = as_dict(p["evidence"])
    bits = []
    if ev.get("source") == "plaid_recurring":
        # Plaid's own recurring-stream detection, offered as a cross-check
        n = ev.get("n") or 0
        bits.append(f"Plaid sees this recurring — {n} charge{'' if n == 1 else 's'}"
                    + (" (early)" if ev.get("status") == "EARLY_DETECTION" else ""))
    if ev.get("hits"):
        bits.append(f"{ev['hits']} charges on a {ev.get('cycle_days','?')}-day cycle")
    if ev.get("monthly_totals"):
        vals = list(ev["monthly_totals"].values())
        bits.append(f"{len(vals)} monthly totals ${min(vals):,.0f}–${max(vals):,.0f}")
    if ev.get("last_seen"):
        bits.append(f"last seen {ev['last_seen']}")
    if ev.get("income"):
        bits.append("looks like a paycheck")
    return " · ".join(bits) or "detected from your ledger"


@router.get("/bills")
def bills_page(user: dict = Depends(_user()), msg: str = ""):
    # Jinja page retired: the SPA owns the UI — a second server-rendered
    # menu is only a second thing to keep in step. Body below is dead.
    return RedirectResponse("/app/bills" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@router.post("/bills/detect")
def bills_detect(user: dict = Depends(_user())):
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        stats = bills.run(conn)
    finally:
        conn.close()
    n_inc = stats.get("proposed_income", 0)
    n_add = stats["proposed_add"]
    found = (f"{n_inc} income + {n_add - n_inc} bills"
             if n_inc else f"{n_add} new proposals")
    msg = (f"Scan complete: {found}, "
           f"{stats['drift_applied']} amounts updated.")
    return RedirectResponse(f"/bills?msg={quote(msg)}", status_code=303)


@router.post("/bills/proposal")
def bills_proposal(user: dict = Depends(_user()), pid: str = Form(...),
                       action: str = Form(...)):
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        r = bills.apply_proposal(conn, pid, action)
    finally:
        conn.close()
    verb = {"approve": "approved", "reject": "rejected"}.get(action, action)
    msg = r.get("error") or f"{verb}: {r.get('approved') or r.get('rejected')}"
    return RedirectResponse(f"/bills?msg={quote(msg)}", status_code=303)


@router.get("/import")
def import_page(user: dict = Depends(_user()), msg: str = ""):
    # Jinja page retired: the SPA owns the UI — a second server-rendered
    # menu is only a second thing to keep in step. Body below is dead.
    return RedirectResponse("/app/import" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


# CSV files awaiting a manual column mapping (auto-detect failed). Staged
# in the tenant-bound import_staging table so any worker can
# finish the mapping a different worker started.
def _stash_csv(conn, account_id: str, amount_sign: str, filename: str,
               text: str, header: list[str]) -> str:
    from ..db import staging
    return staging.put(conn, "csv",
                       files=[(filename, text.encode("utf-8"))],
                       meta={"account_id": account_id,
                             "amount_sign": amount_sign,
                             "header": header})


def _auto_map(header: list[str]) -> dict | None:
    low = {h.strip().lower(): h for h in header}
    mapping = {}
    for field, names in CSV_HEADERS.items():
        for n in names:
            if n in low:
                mapping[field] = low[n]
                break
    if "amount" in mapping:
        # a single signed Amount column wins; stray Debit/Credit matches
        # (e.g. a card-type column) must not flip the importer into
        # dual-column mode
        mapping.pop("debit", None)
        mapping.pop("credit", None)
    if "date" in mapping and "name" in mapping and (
            "amount" in mapping
            or "debit" in mapping or "credit" in mapping):
        return mapping
    return None


OVERSIZE_MSG = "file is larger than 50 MB — export a smaller range"
MAX_UPLOAD = 50 * 1024 * 1024


async def read_capped(file, cap: int = MAX_UPLOAD) -> bytes | None:
    """Stream the upload; None when it blows the cap (never buffers past
    it). Callers with a smaller product limit (receipts: 5 MB) pass their
    own cap so the server never buffers 50 MB just to say no."""
    chunks: list[bytes] = []
    got = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        got += len(chunk)
        chunks.append(chunk)
        if got > cap:
            return None
    return b"".join(chunks)


# A RESTORE archive is not "a file to import": it is the household's whole
# history — every transaction, every receipt image, every attachment — and
# it is the one upload this product promises will work, because it is how
# somebody moves an instance to a new machine. The 50 MB import ceiling
# would make exactly the big ledgers this is FOR unrestorable, and it cannot
# simply be raised because read_capped holds the WHOLE upload in
# memory (a chunk list, then a joined copy: two times the file at the
# moment of the join) — a 500 MB archive would OOM a shared container
# before the restore began.
#
# So the restore door reads to DISK instead (read_restore_capped below) and
# gets its own, larger ceiling. The cost of the ceiling is now temp space,
# not RAM.
#
# NOTE the edge: security.UPLOAD_BODY_LIMIT is the ASGI ceiling on the
# whole multipart body and is the SMALLER of the two — it has to move with
# this number, or the request is refused before the handler sees a byte.
RESTORE_MAX_UPLOAD = 500 * 1024 * 1024
RESTORE_OVERSIZE_MSG = ("archive is larger than 500 MB — restore it on the "
                        "host with ./oikonome.sh restore instead")


async def read_restore_capped(file, cap: int = RESTORE_MAX_UPLOAD):
    """Stream a restore archive to a temp file and return a bytes-like view
    of it; None when it blows the cap.

    The handler never holds the archive: chunks go straight to disk, and
    what comes back is a read-only memory MAP of that file. A mapping is
    virtual — the bytes stay on disk and are paged in as the ZIP is walked
    — and it is both bytes-like and file-like, so it satisfies a callee
    that wraps its argument in `io.BytesIO` today and (zero-copy) one that
    hands a seekable object straight to `zipfile` tomorrow.

    The temp file is unlinked as soon as it is mapped: the mapping keeps
    the data alive for as long as the restore holds it, and a crash mid-way
    leaves nothing behind to clean up.
    """
    import mmap
    import os
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    got = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            got += len(chunk)
            if got > cap:
                return None
            tmp.write(chunk)
        tmp.flush()
        if not got:
            return b""               # an empty file cannot be mapped
        return mmap.mmap(tmp.fileno(), 0, access=mmap.ACCESS_READ)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:              # noqa: BLE001 — already gone
            pass
        tmp.close()


def restore_result(counts: dict) -> dict:
    """Shape a restore_zip() tally into the import hub's result dict.

    Two things the naive version gets wrong on a large restore:
    `already_present` summed into `rows`, so re-restoring an archive that
    inserted nothing announces tens of thousands of rows; and the notes the
    config check returns (a dropped AI backend, a rerouted role) having
    nowhere to surface, leaving the person to discover the loss later."""
    notes = counts.get("_notes") or []
    present = counts.get("already_present", 0)
    inserted = {k: v for k, v in counts.items()
                if k not in ("_notes", "already_present")}
    parts = [f"{v} {k}" for k, v in inserted.items()]
    if present:
        parts.append(f"{present} already present")
    return {"imported": counts.get("transactions", 0),
            "rows": sum(inserted.values()),
            "warnings": list(notes) or None,
            "source": "Oikonome export restored: " + ", ".join(parts)}


def _dispatch_import_unlocked(conn, account_id: str, amount_sign: str,
                              filename: str, data: bytes,
                              role: str = "") -> tuple[dict, dict | None]:
    """One import dispatch for BOTH the Jinja route and /api/import.
    Returns (result, mapping_needed). Errors come back in result["error"]
    — never raises, except the two authorization refusals below.

    `role` is the caller's household role. It decides one thing: a `.zip`
    is a whole-ledger RESTORE, which is owner-only. It defaults to the
    empty string — a caller that does not say who it is does not get to
    restore, because the failure that matters here is a new call site
    silently inheriting the most privileged answer."""
    name = (filename or "").lower()
    if name.endswith(".zip") and not permissions.may_restore(role):
        raise HTTPException(403, permissions.RESTORE_DENIED)
    # A `.zip` here is a full-ledger RESTORE
    # (restore_zip merges an entire export into the tenant). Script tokens
    # push rows through this hub (a collector sends CSVs), but a
    # leaked collector token must never be able to inject an attacker's
    # export ZIP. The API import handler flags script-token pushes via the
    # heartbeat PUSH_VIA contextvar ("token"); refuse restore for those.
    # Raised OUTSIDE the try below so it surfaces as a 403, not a swallowed
    # {"error": ...}. Session owners (via="app"/"exec") still restore.
    from ..sync import heartbeat
    if name.endswith(".zip") and heartbeat.PUSH_VIA.get() == "token":
        raise HTTPException(403, "script tokens cannot restore an export ZIP "
                                 "— use a signed-in session to restore")
    # Without this a script token could file-import into
    # ANY account, including ones a live aggregator (plaid/simplefin/mx)
    # owns — polluting a pull source's ledger. Same shape as the ZIP guard:
    # token principals only, raised outside the try so it surfaces as 403.
    if account_id and heartbeat.PUSH_VIA.get() == "token":
        from ..sync import base as sync_base
        try:
            sync_base.assert_token_importable(conn, account_id)
        except PermissionError as e:
            raise HTTPException(403, str(e))
    result: dict = {}
    bid = None
    try:
        if not name.endswith(".zip") and not account_id:
            raise ValueError("pick the account these transactions belong to")
        if name.endswith(".zip"):
            # Inline, on purpose: this is the no-JS door, which has nothing
            # to poll a background job with. Both API doors (/api/import and
            # the bulk runner, i.e. every path the apps use) start
            # sync/restore_job instead, because a large archive outlives the
            # edge timeout in front of a hosted instance.
            from ..sync import restore
            result = restore_result(restore.restore_zip(conn, data))
        elif name.endswith((".ofx", ".qfx")):
            bid = batches.create(conn, "ofx", account_id, filename)
            result = ofximport.import_ofx(conn, account_id, data,
                                          batch_id=bid)
        elif name.endswith(".pdf"):
            bid = batches.create(conn, "pdf", account_id, filename)
            # statement → bank heuristic; investment keeps its own keywords
            pdf_sign = (amount_sign if amount_sign in pdfimport.STATEMENT_KINDS
                        else "bank")
            result = pdfimport.import_pdf(
                conn, account_id, data, amount_sign=pdf_sign, batch_id=bid)
            conf = result.get("confidence")
            label = {"card": "credit-card statement",
                     "investment": "investment statement",
                     "statement": "bank statement",
                     "bank": "bank statement"}.get(pdf_sign, "PDF statement")
            result["source"] = (f"PDF {label} parsed"
                                + (f" (confidence {conf:.0%})"
                                   if conf is not None else ""))
        elif name.endswith(".qif"):
            text = data.decode("utf-8-sig", errors="replace")
            bid = batches.create(conn, "qif", account_id, filename)
            result = qifimport.import_qif(conn, account_id, text,
                                          batch_id=bid)
        else:
            if amount_sign in ("card", "statement"):
                raise ValueError(
                    "PDF statement conventions (card / bank statement) "
                    "apply to PDFs — for a CSV pick bank or positive=out "
                    "CSV, or investment for brokerage activity CSVs")
            text = data.decode("utf-8-sig", errors="replace")
            header = next(csvmod.reader(io.StringIO(text)), [])
            from ..sync import plan_csv as _plan
            # Never auto-import a workplace-plan CSV here: the generic path
            # cannot know which retirement plan the file belongs to, and
            # guessing one window-deletes and overwrites a different plan's
            # ledger and holdings. Refuse and point at the dedicated endpoint
            # that takes an explicit plan.
            if _plan.looks_like_plan_activity(header):
                raise ValueError(
                    "This looks like a workplace-plan activity CSV. Import "
                    "it through POST /api/import/plan-activity with the "
                    "explicit plan (e.g. 403B, 401A) — the generic importer "
                    "can't tell which plan it belongs to.")
            detected = [
                (mintimport.looks_like_mint, "mint",
                 mintimport.import_mint, "Mint export detected"),
                (ynabimport.looks_like_ynab, "ynab",
                 ynabimport.import_ynab, "YNAB register detected"),
                (competitors.looks_like_monarch, "monarch",
                 competitors.import_monarch, "Monarch export detected"),
                (competitors.looks_like_copilot, "copilot",
                 competitors.import_copilot, "Copilot export detected"),
                (competitors.looks_like_simplifi, "simplifi",
                 competitors.import_simplifi, "Simplifi export detected"),
            ]
            for looks, src, imp, label in detected:
                if looks(header):
                    bid = batches.create(conn, src, account_id, filename)
                    result = imp(conn, account_id, text, batch_id=bid)
                    result["source"] = label
                    break
            else:
                mapping = _auto_map(header)
                if mapping is None:
                    token = _stash_csv(conn, account_id, amount_sign,
                                       filename or "upload.csv",
                                       text, header)
                    # A mapping card of bare selects asks you to confirm a
                    # column mapping blind — at the one moment the file's own
                    # rows are the answer. Ship a few of them; the text is
                    # already parsed and in hand.
                    rdr = csvmod.reader(io.StringIO(text))
                    next(rdr, None)              # the header, already sent
                    sample = [r for _, r in zip(range(5), rdr)]
                    return {}, {"token": token, "header": header,
                                "sample": sample,
                                "filename": filename}
                bid = batches.create(conn, "csv", account_id, filename)
                result = csvimport.import_csv(
                    conn, account_id, text, mapping,
                    amount_sign=amount_sign, batch_id=bid)
        if bid is not None and "imported" in result:
            batches.finish(conn, bid, result["imported"])
    except ValueError as e:                   # deliberate importer guidance
        # wrong file type, missing account, card-convention hint … — meant for
        # the user, show it verbatim (these are NOT internal errors).
        batches.discard(conn, bid)
        result = {"error": str(e)}
    except Exception as e:                     # noqa: BLE001 — UNEXPECTED
        # log the real cause (matches connect_simplefin/account_sync) —
        # import is the most bug-prone path here, and an unlogged failure in
        # it is invisible; keep the raw exception text OUT of the user
        # response (it can carry psycopg column/table detail).
        import logging
        logging.getLogger("oikonome.import").exception("import failed")
        batches.discard(conn, bid)
        result = {"error": f"import failed ({type(e).__name__})"}
    return result, None


def dispatch_import(conn, account_id: str, amount_sign: str,
                    filename: str, data: bytes,
                    role: str = "") -> tuple[dict, dict | None]:
    """`_dispatch_import_unlocked` under the per-tenant sync lock.

    Every scheduled and ad-hoc aggregator pull takes this lock before it
    writes to `transactions`, and a file import owes the same: one racing
    the hourly sync would land a row the sync's dedup had not yet seen —
    and count it twice. Held for the whole parse + upsert, like a sync."""
    from ..sync.base import tenant_sync_lock
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    with tenant_sync_lock(conn, tid) as held:
        if not held:
            return ({"error": "a sync is running for this household — try "
                              "the import again in a moment"}, None)
        return _dispatch_import_unlocked(conn, account_id, amount_sign,
                                         filename, data, role=role)


def _mapping_retry(conn, stash: dict, message: str) -> dict:
    """A refusal the person can act on WITHOUT re-uploading the file.

    The mapping card exists because a column mapping was guessed wrong, and
    the commonest wrong guess of all — pointing `date` at a column that
    holds no dates — is only discovered once the importer is parsing rows,
    which is after the stash has been consumed. Refusing there without
    re-staging leaves the card on screen holding a token that can never
    work again: the same "upload expired" message on every click, and
    nothing telling the person to go back to the drop zone.

    So park the same bytes again and hand back the NEW claim token — the
    card keeps its state, the person fixes the one select that was wrong,
    and submits against the token in this reply. Callers must only reach
    here for a refusal raised BEFORE anything was written; the CSV importer
    parses every row before it touches the database, so a parse refusal
    qualifies and a half-finished import never does.

    Re-staging can itself fail (the tenant's staging budget is full, the
    connection is gone). Then the upload really is unrecoverable, and the
    honest answer says so and marks the reply `expired` — the clients drop
    the card on that, because a card that cannot submit must not stay up.
    """
    from ..db import staging
    try:
        fresh = staging.put(conn, "csv", stash["files"], stash["meta"])
    except Exception:                          # noqa: BLE001 — budget, DB…
        import logging
        logging.getLogger("oikonome.import").exception("re-stash failed")
        return {"error": f"{message} — the file is no longer held here, "
                         f"please upload it again",
                "expired": True}
    meta = stash["meta"] or {}
    # header/filename ride along so the no-JS page can redraw its form from
    # the reply alone; the SPA and the app already hold them.
    return {"error": message, "token": fresh,
            "header": meta.get("header") or [],
            "filename": (stash["files"][0][0] if stash["files"] else "")}


def run_mapped_import(conn, token: str, cols: dict) -> dict:
    """`_run_mapped_import_unlocked` under the per-tenant sync lock — the
    mapped path writes rows like any other import (see dispatch_import).
    A held lock is refusal shape 1: nothing consumed, resubmit as-is."""
    from ..sync.base import tenant_sync_lock
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    with tenant_sync_lock(conn, tid) as held:
        if not held:
            return {"error": "a sync is running for this household — try "
                             "the import again in a moment"}
        return _run_mapped_import_unlocked(conn, token, cols)


def _run_mapped_import_unlocked(conn, token: str, cols: dict) -> dict:
    """Finish a stashed CSV upload with the user-picked column mapping.

    Three shapes of refusal, because the clients have to tell them apart:
      {"error"}                    — nothing was consumed; resubmit as-is.
      {"error", "token": <new>}    — the upload was consumed and re-staged;
                                     resubmit against the token in the reply.
      {"error", "expired": True}   — the upload is gone; re-upload the file.
    """
    for k in ("date_col", "name_col"):
        if not cols.get(k):          # validate BEFORE consuming the stash —
            return {"error": f"{k} is required"}   # a bad request must not
    # The money can be named three ways and the engine takes all of them: a
    # single signed Amount, or a Debit/Credit pair, or ONE side of that pair
    # alone — a withdrawals-only export is a real bank file, and demanding
    # the deposits column it does not have would make it unimportable.
    # Refuse only when nothing at all says how much moved.
    dual = not cols.get("amount_col")
    if dual and not (cols.get("debit_col") or cols.get("credit_col")):
        return {"error": "map a money column: Amount, or Debit (money out) "
                         "and/or Credit (money in)"}
    from ..db import staging
    stash = staging.peek(conn, "csv", token)       # RLS = tenant-bound
    if stash is None:
        # Nothing left to correct a mapping against — an hour-old stage
        # swept by the TTL, a token already spent, or another tenant's.
        return {"error": "upload expired — please upload the file again",
                "expired": True}
    meta = stash["meta"] or {}
    # belt: the mapped-completion path skips dispatch_import's
    # guard — apply the same token rule to the stashed destination.
    # Checked BEFORE the stash is consumed: a 403 raised after the pop is
    # a fourth refusal shape the contract above never names — the client
    # sees neither a new token nor `expired`, and keeps a card holding a
    # token that can never work again. Refusing first leaves the upload
    # where the token still points, which is shape 1.
    from ..sync import heartbeat
    if meta.get("account_id") and heartbeat.PUSH_VIA.get() == "token":
        from ..sync import base as sync_base
        try:
            sync_base.assert_token_importable(conn, meta["account_id"])
        except PermissionError as e:
            raise HTTPException(403, str(e))
    stash = staging.pop(conn, "csv", token)        # destroy the upload
    if stash is None:                              # spent between the two
        return {"error": "upload expired — please upload the file again",
                "expired": True}
    filename, text = stash["files"][0]
    text = text.decode("utf-8", errors="replace")
    mapping = {"date": cols["date_col"],
               "amount": cols.get("amount_col") or None,
               "name": cols["name_col"],
               "merchant": cols.get("merchant_col") or None,
               "category": cols.get("category_col") or None}
    if dual:   # engine signs come straight from the pair (debit = out);
        # an unmapped side is None, which the engine reads as "this file
        # has no rows in that direction", not as a missing mapping
        mapping["debit"] = cols.get("debit_col") or None
        mapping["credit"] = cols.get("credit_col") or None
    bid = None
    try:
        bid = batches.create(conn, "csv", meta.get("account_id"),
                             filename)
        result = csvimport.import_csv(
            conn, meta.get("account_id"), text, mapping,
            # the CSV conventions only — card/statement are PDF conventions
            # and the drop zone refuses them for a CSV before any stash
            amount_sign=meta.get("amount_sign")
            if meta.get("amount_sign") in ("bank", "plaid", "investment")
            else "bank",
            batch_id=bid)
        batches.finish(conn, bid, result["imported"])
        return result
    except ValueError as e:                   # deliberate importer guidance
        # A mapping the file does not support: an unrecognized date, an
        # amount column of prose, more rows than the cap. import_csv parses
        # every row before it writes any, so none of these has changed the
        # ledger — the mapping is still correctable, and the stash is put
        # back so it can be corrected. The batch row is claimed before the
        # parse, so drop it: it owns no rows, and correcting a mapping
        # twice must not leave a trail of empty imports in the batch list.
        if bid is not None:
            conn.execute("DELETE FROM import_batches WHERE id=%s "
                         "AND row_count = 0", (bid,))
        return _mapping_retry(conn, stash, str(e))
    except Exception as e:                     # noqa: BLE001 — UNEXPECTED
        import logging
        logging.getLogger("oikonome.import").exception("mapped import failed")
        # An unexpected failure may have written part of the import, so
        # this is NOT a correctable mapping — re-running it could double
        # rows. The upload does not come back; the card closes and the
        # person starts over, which is the only safe advice.
        return {"error": f"import failed ({type(e).__name__}) — upload the "
                         f"file again to retry",
                "expired": True}


# The no-JS form door is the same 50 MB upload as the API hub next to it,
# and an expensive door without a limiter is a door an attacker walks
# through — same budget, so switching flavors buys them nothing.
@router.post("/import",
             dependencies=[Depends(limit("import_upload", 60, 3600))])
async def import_submit(user: dict = Depends(_user()),
                        account_id: str = Form(""),
                        amount_sign: str = Form("bank"),
                        file: UploadFile = File(...)):
    demoguard.deny(user)
    filename = file.filename or ""
    # cap uploads BEFORE buffering — one tenant must not be able to OOM a
    # shared node with a huge or decompression-bomb file. A ZIP is a
    # whole-ledger RESTORE and reads to disk under its own, larger ceiling
    # (read_restore_capped); everything else is one statement file and
    # stays in memory under the import ceiling.
    is_restore = (filename.lower().endswith(".zip")
                  and not user.get("script_token"))
    if is_restore:
        data = await read_restore_capped(file)
        oversize_msg = RESTORE_OVERSIZE_MSG
    else:
        data = await read_capped(file)
        oversize_msg = OVERSIZE_MSG

    # A ZIP is a whole-ledger restore — minutes of work that must not hold
    # this POST open. Same background job as /api/import's zip branch (and
    # the same role gate: a MEMBER may import, only owners restore); the
    # no-JS page can't poll, so the note says where progress shows up.
    if data is not None and is_restore:
        if not permissions.may_restore(user["role"]):
            raise HTTPException(403, permissions.RESTORE_DENIED)
        from ..sync import restore_job
        started = restore_job.start(str(user["tenant_id"]), data)
        if not started.get("error"):
            started = {"source": "restore started — it keeps running in "
                                 "the background; reload this page to see "
                                 "it land ('restored' shows in the recent "
                                 "imports below when done)"}
        conn = tenancy.tenant_connect(user["tenant_id"])
        try:
            return _tpl("import.html", title="Import", centered=False,
                        accounts=_accounts(conn), result=started,
                        recent=batches.recent(conn))
        finally:
            conn.close()

    # parse + upsert are CPU- and DB-bound: off the event loop, exactly
    # like /api/import — the container runs ONE uvicorn worker, so a
    # multi-minute import on it holds the whole instance (every tenant,
    # readyz included) hostage behind this coroutine.
    def _run():
        conn = tenancy.tenant_connect(user["tenant_id"])
        try:
            if data is None:
                return _tpl("import.html", title="Import", centered=False,
                            accounts=_accounts(conn),
                            result={"error": oversize_msg},
                            recent=batches.recent(conn))
            result, mapping_needed = dispatch_import(
                conn, account_id, amount_sign, filename, data,
                role=user["role"])
            if mapping_needed:
                return _tpl("import.html", title="Import", centered=False,
                            accounts=_accounts(conn), result=None,
                            recent=batches.recent(conn),
                            mapping_needed=mapping_needed)
            return _tpl("import.html", title="Import", centered=False,
                        accounts=_accounts(conn), result=result,
                        recent=batches.recent(conn))
        finally:
            conn.close()

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_run)


@router.post("/import/mapped")
def import_mapped(user: dict = Depends(_user()), token: str = Form(...),
                  date_col: str = Form(...), amount_col: str = Form(""),
                  name_col: str = Form(...), merchant_col: str = Form(""),
                  category_col: str = Form(""), debit_col: str = Form(""),
                  credit_col: str = Form("")):
    demoguard.deny(user)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        result = run_mapped_import(conn, token, {
            "date_col": date_col, "amount_col": amount_col,
            "name_col": name_col, "merchant_col": merchant_col,
            "category_col": category_col, "debit_col": debit_col,
            "credit_col": credit_col})
        # A correctable refusal re-stashes the upload under a new token, so
        # draw the form again with it — the no-JS page keeps the same
        # promise the SPA does: fix the column that was wrong and submit
        # again, without hunting for the file a second time.
        again = None
        if result.get("error") and result.get("token"):
            again = {"token": result["token"],
                     "header": result.get("header") or [],
                     "filename": result.get("filename") or ""}
        elif result.get("error") and not result.get("expired"):
            # shape 1 — nothing was consumed and the SAME token still
            # works, so the form must come back with it. Reachable from
            # this page: only date and name are marked required, so a
            # submit with no money column lands here. Without the redraw
            # the refusal drops the form and the still-valid upload is
            # unreachable except by uploading it again.
            from ..db import staging
            held = staging.peek(conn, "csv", token)
            if held is not None:
                meta = held["meta"] or {}
                again = {"token": token,
                         "header": meta.get("header") or [],
                         "filename": (held["files"][0][0]
                                      if held["files"] else "")}
        return _tpl("import.html", title="Import", centered=False,
                    accounts=_accounts(conn), result=result,
                    mapping_needed=again,
                    recent=batches.recent(conn))
    finally:
        conn.close()


@router.post("/import/rollback")
def import_rollback(user: dict = Depends(_user()), batch_id: str = Form(...)):
    demoguard.deny(user)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        n = batches.rollback(conn, batch_id)
    finally:
        conn.close()
    msg = f"{int(n)} rows removed."
    if getattr(n, "warning", None):
        msg += f" {n.warning}"
    return RedirectResponse(f"/import?msg={quote(msg)}", status_code=303)
