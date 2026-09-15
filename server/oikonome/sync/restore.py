"""Restore an Oikonome export ZIP into the current tenant — the other half
of /export: a self-hosted export imports into a hosted tenant and vice
versa, so nothing about the data is locked to one instance.

Restores: items (as sync-less shells — tokens never travel; reconnect
aggregators after migrating), accounts, transactions (original ids,
idempotent), recurring bills, category overrides, reimbursement pairs +
flags, business flags, tenant settings, and — converter v2 (migration
008) — liabilities, holdings, crypto_holdings, networth_recorded,
networth_snapshot, income_annual, income_documents, merchant_canonical,
merchant_renames, merchant_categories, amazon_orders/matches/summaries,
costco_receipts/matches.
Skipped: alerts history, proposals, batches (operational residue).
Every insert is ON CONFLICT DO NOTHING on the original ids — re-restoring
the same ZIP is a no-op. Counts report actual INSERTS: rows
the tenant already had are tallied under `already_present`, so a restore
into a populated tenant can't claim it imported the whole ZIP.
"""

from __future__ import annotations

import contextvars
import csv
import datetime as _dt
import io
import json
import logging
import math
import zipfile

from ..engine.compat import as_date, jsonb

log = logging.getLogger(__name__)


# A restore member's DECLARED byte size (bounded by
# _check_zip_sizes) does NOT bound peak memory — the old `list(csv.DictReader(
# z.read(name)))` materialized every row into a dict, so a 128 MB CSV of
# millions of tiny rows (compressing to well under the 50 MB upload cap)
# ballooned to multiple GB of live objects and OOM-killed the shared hosted
# process (cross-tenant DoS). Stream lazily from the ZIP (never read the whole
# member) and cap the row count. A real personal-finance export is a few tens
# of thousands of rows; the cap is orders of magnitude above that.
MAX_ROWS_PER_MEMBER = 2_000_000


# The largest legitimate cell is a receipt image: base64 of a MAX_IMAGE
# upload, ~6.8 MB. Python's csv module refuses fields over 128 KB by
# default, so restoring an export that carried one real photo receipt
# died on the whole receipts member. The limit is process-wide, so it is
# raised once, here, to exactly what the biggest honest cell needs; the
# per-row checks below still refuse anything bigger than a live upload.
_MAX_CELL = 8 * 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), _MAX_CELL))


# Progress reporting for a restore running as a background job. A
# ContextVar rather than a threaded-through argument because _rows() is
# called from three dozen places and the alternative was editing every
# one of them to carry a value only one caller ever sets. Set inside the
# worker thread (a new thread starts from an empty context), so a
# synchronous restore in the request path reports nothing and pays
# nothing.
_PROGRESS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_progress", default=None)
_TICK_EVERY = 500


def _tick(table: str, rows: int, *, done: bool = False) -> None:
    cb = _PROGRESS.get()
    if cb is not None:
        try:
            cb(table, rows, done)
        except Exception:                                # noqa: BLE001
            log.debug("restore progress callback failed", exc_info=True)


def _rows(z: zipfile.ZipFile, name: str):
    """Yield CSV rows one at a time — O(row) memory, hard row cap."""
    try:
        fh = z.open(name)
    except KeyError:
        return
    with fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
        _tick(name, 0)
        try:
            i = -1
            for i, row in enumerate(reader):
                if i >= MAX_ROWS_PER_MEMBER:
                    raise ValueError(
                        f"{name}: too many rows (limit "
                        f"{MAX_ROWS_PER_MEMBER:,}) — this doesn't look like "
                        "a genuine Oikonome export")
                if i and i % _TICK_EVERY == 0:
                    _tick(name, i)
                yield row
            _tick(name, i + 1, done=True)
        except csv.Error as e:
            # an over-wide cell (or a torn CSV) is a bad file, not a crash
            raise ValueError(f"{name}: {e} — this doesn't look like a "
                             "genuine Oikonome export")


def _j(v):
    if not v:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return None


def _f(v):
    """CSV cell → float or None ('' travels for NULL)."""
    return float(v) if v not in (None, "") else None


def _i(v, default=None):
    """CSV cell → int (accepts '1.0'-style floats) or default."""
    return int(float(v)) if v not in (None, "") else default


def _b(v):
    """CSV bool cell ("True"/"t"/"1") → Python bool. Module scope, so every
    reader in this file can reach it."""
    return str(v).strip().lower() in ("true", "t", "1")


def _ts(v):
    """CSV cell → datetime or None. Accepts sqlite datetime('now') form
    ('YYYY-MM-DD HH:MM:SS'), ISO-T, and bare dates. psycopg dumps str as
    text (which won't coerce to timestamptz) — so parse here."""
    if not v:
        return None
    try:
        return _dt.datetime.fromisoformat(str(v))
    except ValueError:
        try:
            return _dt.datetime.combine(as_date(v), _dt.time())
        except (ValueError, TypeError):
            return None


# credential-shaped config keys — the export scrubs these (pages.py), the
# restore keeps the DESTINATION's values. Shared by the merge below.
_SECRET_SUBSTR = ("secret", "token", "password", "client_id",
                  "access_url", "api_key", "extra_body")

# Policy keys neither an export nor a restore ZIP may carry —
# the demo lockdown pair (demo_login holds the shared plaintext password;
# flipping demo_mode opens every data door) and the SMTP cleartext opt-out
# (a ZIP setting smtp_starttls:false would silently downgrade mail to
# cleartext). The destination's own values always win.
# Keys a restore must never carry IN: they are decisions about THIS
# instance, not data about the household. taxdocs_allow_remote_llm is the
# sharpest of them — it is the consent that lets a W-2 image (Social
# Security number and all) leave for a remote model, and a ZIP that set it
# would grant that consent on the importing instance without anyone saying
# yes. Consent is given here or not at all.
# notify_phone is the same class: it is a VERIFIED, CONSENTED destination —
# the worker sends the household's budget verdict wherever it says, on the
# strength of `verified: true`. Verification and consent happen on this
# instance (a code sent to the number and typed back) or not at all; a ZIP
# that carried them would plant any number as verified. Both the record
# and its pending half stay behind; the owner re-verifies after a move.
_POLICY_KEYS = ("demo_mode", "demo_login", "smtp_starttls",
                "taxdocs_allow_remote_llm", "notify_phone",
                "notify_phone_pending")


def _secret_key(k) -> bool:
    """Is this config key credential-shaped? The one carve-out is the exact
    key `tokens` — savings-goal entries carry merchant match WORDS under it
    (user data living in list-of-dict config), and no credential store in
    this config uses that name."""
    return k != "tokens" and any(s in k for s in _SECRET_SUBSTR)


def _scrub_list(items: list) -> list:
    """Scrub list elements: dicts get the same key scrub as scrub_config
    (llm_backends is the live case — a LIST of dicts each carrying an
    encrypted `api_key` that must never ride an export), nested lists
    recurse, scalars pass through."""
    return [scrub_config(v) if isinstance(v, dict)
            else _scrub_list(v) if isinstance(v, list) else v
            for v in items]


def scrub_config(cfg: dict) -> dict:
    """Shared export/restore config scrub: drop credential-shaped and
    policy keys, recursing into nested dicts AND lists so neither a nested
    {"password":...} (demo_login's shape) nor a list of dicts each holding
    an api_key (llm_backends' shape) can ever ride along under a key the
    substring match misses."""
    out = {}
    for k, v in cfg.items():
        if _secret_key(k) or k in _POLICY_KEYS:
            continue
        # list-shaped-by-contract keys are normalized, not trusted: a
        # crafted/hand-edited ZIP carrying email_muted as a scalar 500'd
        # every Settings load and silently killed the tenant's scheduled
        # mail. The readers guard too — this keeps
        # the bad shape out of storage in the first place.
        if k == "email_muted":
            if isinstance(v, list):
                out[k] = sorted({str(m).lower() for m in v if str(m).strip()})
            continue
        out[k] = (scrub_config(v) if isinstance(v, dict)
                  else _scrub_list(v) if isinstance(v, list) else v)
    return out


def carry_forward_secrets(dest, incoming):
    """Put the destination's credential-shaped values back into an incoming
    config value the export scrubbed them out of.

    A ZIP's config is always credential-free. For a FLAT key that costs
    nothing: the key is absent from the ZIP entirely, so the destination's
    own value rides through the merge untouched. For a CONTAINER it is the
    opposite — `llm_backends` arrives as a list that IS present, only with
    every entry's `api_key`/`extra_body` stripped, so merging the ZIP's copy
    over the destination's replaces live credentials with nothing. Exporting
    an instance and restoring it onto itself then silently unconfigures
    every AI backend.

    So match the two sides up — dicts by key, lists of objects by `id` — and
    restore the destination's secrets onto the entries that still exist.
    Everything non-secret stays the ZIP's to define: renames, new URLs and
    removed entries all still land.
    """
    if isinstance(dest, dict) and isinstance(incoming, dict):
        out = dict(incoming)
        for k, v in dest.items():
            if _secret_key(k):
                out.setdefault(k, v)
            elif k in out:
                out[k] = carry_forward_secrets(v, out[k])
        return out
    if isinstance(dest, list) and isinstance(incoming, list):
        prior = {d["id"]: d for d in dest
                 if isinstance(d, dict) and d.get("id") is not None}
        return [carry_forward_secrets(prior[v["id"]], v)
                if isinstance(v, dict) and v.get("id") in prior else v
                for v in incoming]
    return incoming


# Every ZIP member the restore reads back. The export writes MORE than
# this — every RLS-scoped table minus a skip set — and the difference is
# deliberate: alerts, sync logs, job heartbeats, staged uploads, proposals
# and the like are this instance's operating state, not the household's
# data, and restoring them would replay stale alerts and half-finished
# jobs into the destination. The export's README names which files come
# back and which are kept for the person's records only, from this set.
# `bills` is read under a variable (older archives call it recurring.csv),
# so a source test that only scans literal `_rows(z, "…")` calls misses it.
RESTORED_MEMBERS: frozenset[str] = frozenset(['account_links', 'accounts', 'amazon_matches', 'amazon_orders', 'amazon_summaries', 'bills', 'budget_snapshots', 'business_entity', 'business_flags', 'business_txn_class', 'compliance_obligation', 'costco_matches', 'costco_receipts', 'crypto_holdings', 'entity_membership', 'equity_movement', 'holdings', 'income_annual', 'income_documents', 'items', 'liabilities', 'manual_categories', 'merchant_canonical', 'merchant_categories', 'merchant_merge_proposals', 'merchant_renames', 'merchants', 'mileage_log', 'networth_recorded', 'networth_snapshot', 'receipt_items', 'receipts', 'recipient_invites', 'recurring', 'reimburse_flags', 'reimbursements', 'tenant_settings', 'transaction_notes', 'transactions', 'vendor_1099'])


# A crafted archive must not be able to fill a control-plane table. A live
# recipient list is capped at 20 (mailguard.MAX_RECIPIENTS) and this table
# also keeps a row for every address ever asked, so the ceiling is generous
# — but finite, which is the point.
_MAX_RECIPIENT_INVITES = 500


def _check_config(cfg: dict) -> list[str]:
    """The ZIP's config is merged into this tenant's settings — apply
    the SAME netguard policy as the Settings save and the .oikx bundle
    import. Without this, a crafted export ZIP sets
    llm_url=http://169.254.169.254/… and the worker fetches it: the guard
    bypassed by a third door.

    Endpoints that the guard refuses are DROPPED here, not grounds to
    refuse the archive: an export is a person's whole financial history,
    and the endpoints in it are the most instance-local thing it carries —
    a self-hoster's `http://ollama:11434` or a LAN SMTP relay is
    unreachable from anywhere else by construction, so aborting on one
    would mean the documented self-host→hosted migration could never
    complete for a large real export. Dropping is strictly safer than
    merging — the URL never
    lands in stored config, so nothing can fetch it later — and the caller
    reports what was dropped. A MALFORMED payload still raises: that is a
    corrupt file, not an unreachable one.

    Returns human-readable notes naming what was dropped (empty when
    everything survived)."""
    from ..web.netguard import BlockedURL, check_host, check_url
    from .. import homepages
    # the home-page keys are a closed vocabulary the clients redirect to
    # blind — the Settings save checks them, and this door must too
    notes: list[str] = homepages.scrub(cfg)
    if cfg.get("llm_url"):
        try:
            check_url(str(cfg["llm_url"]), what="llm_url in this export")
        except BlockedURL:
            cfg.pop("llm_url", None)
            notes.append("the AI endpoint (llm_url) was dropped — not "
                         "reachable from this instance")
    if cfg.get("smtp_host"):
        try:
            check_host(str(cfg["smtp_host"]), what="smtp_host in this export")
        except BlockedURL:
            cfg.pop("smtp_host", None)
            notes.append("the mail server (smtp_host) was dropped — not "
                         "reachable from this instance")
    # The multi-backend list is where the AI endpoints actually live now —
    # the flat llm_url above is the legacy single-endpoint key. Merged in
    # raw, a ZIP could point categorize/vision/assistant at any URL and the
    # next sync or receipt parse would post merchants, amounts and receipt
    # images there. The .oikx bundle already validates the list (field
    # whitelist, ceiling, reserved/duplicate ids, netguard on every url);
    # the ZIP gets the identical rules, and the cleaned list replaces the
    # raw one so nothing else in the entries rides through.
    if "llm_backends" in cfg:
        from .connections_bundle import _clean_backends
        dropped: list[str] = []
        cfg["llm_backends"] = _clean_backends(cfg.get("llm_backends"),
                                              drop_unreachable=dropped)
        if dropped:
            notes.append(
                f"{len(dropped)} AI backend(s) dropped — not reachable from "
                f"this instance: " + ", ".join(sorted(dropped)))
    if "llm_roles" in cfg:
        if not isinstance(cfg["llm_roles"], dict):
            raise ValueError("the AI role routing in this export is malformed")
        # a role left pointing at a backend that did not survive would
        # route nowhere. Unset it, which is how this instance's own default
        # takes over — pinning it to "bundled" instead would silently
        # override a destination that routes that role somewhere better.
        kept = {str(b.get("id")) for b in (cfg.get("llm_backends") or [])
                if isinstance(b, dict)}
        rerouted = [r for r, v in cfg["llm_roles"].items()
                    if v and str(v) != "bundled" and str(v) not in kept]
        for r in rerouted:
            cfg["llm_roles"].pop(r, None)
        if rerouted:
            notes.append("AI routing for " + ", ".join(sorted(rerouted))
                         + " fell back to this instance's default")
    return notes


# The keys inside a `raw` blob that readers walk as LISTS. SQL's
# jsonb_array_elements / jsonb_array_length do not skip a row whose value
# is the wrong shape — they abort the entire statement — so one restored
# blob carrying an object under any of these can blank a query for every
# other row in the household.
_ARRAY_RAW_KEYS = ("counterparties", "companions", "_batches")


def _raw_dict(v) -> dict:
    """A `raw` column is read with `.get` and merged with `||` everywhere;
    a ZIP that carries a list, string or number there used to land as-is
    and crash the first reader. The list-shaped keys one level down get the
    same treatment: a value that is not a list is dropped, and every reader
    already treats an absent key as "the source never said"."""
    if not isinstance(v, dict):
        return {}
    bad = [k for k in _ARRAY_RAW_KEYS if k in v and not isinstance(v[k], list)]
    return {k: x for k, x in v.items() if k not in bad} if bad else v


def _json_list(v) -> list:
    """A column the schema declares a JSON array — the line items an order
    or a receipt carries. Anything else becomes an empty list, so the order
    still lands with its amount, date and category and only its items read
    as none. `x or []` is NOT this check: it substitutes on a falsy value
    and waves an object or a number straight through."""
    return v if isinstance(v, list) else []


def clean_bill_raw(v) -> dict:
    """A bill's raw config in the shape save_bill would have written: the
    recurrence lists as ints, envelope_months a number, companions a
    list. The normal write path validates these; a restore bypassed it,
    and one malformed anchor crashed every Today load for the household."""
    raw = dict(_raw_dict(v))
    rec = raw.get("recurrence")
    if rec is not None and not isinstance(rec, dict):
        raw.pop("recurrence", None)
        rec = None
    if isinstance(rec, dict):
        rec = dict(rec)
        for key in ("byMonthDay", "byMonth", "bySetPos"):
            if key in rec:
                vals = rec[key] if isinstance(rec[key], (list, tuple)) else []
                ints = []
                for x in vals:
                    try:
                        ints.append(int(x))
                    except (TypeError, ValueError):
                        continue
                if ints:
                    rec[key] = ints
                else:
                    rec.pop(key, None)
        if "byDay" in rec:
            codes = ([c for c in rec["byDay"] if isinstance(c, str)]
                     if isinstance(rec["byDay"], list) else [])
            if codes:
                rec["byDay"] = codes
            else:
                rec.pop("byDay", None)
        if "interval" in rec:
            try:
                rec["interval"] = max(1, min(240, int(rec["interval"])))
            except (TypeError, ValueError):
                rec.pop("interval", None)
        if "frequency" in rec and not isinstance(rec["frequency"], str):
            rec.pop("frequency", None)
        raw["recurrence"] = rec
    if "envelope_months" in raw:
        try:
            raw["envelope_months"] = 12 if int(raw["envelope_months"]) >= 12 else 1
        except (TypeError, ValueError):
            raw.pop("envelope_months", None)
    if "companions" in raw and not isinstance(raw["companions"], list):
        raw.pop("companions", None)
    if isinstance(raw.get("companions"), list):
        # each companion's amount is cast in SQL by the ledger query and
        # the daily report's due_soon — free text there aborted both, the
        # same blast radius as a liability's "N/A" statement balance
        kept = []
        for c in raw["companions"]:
            if not isinstance(c, dict):
                continue
            try:
                amt = float(c.get("amount"))
            except (TypeError, ValueError):
                continue
            if isinstance(c.get("amount"), bool) or not math.isfinite(amt):
                continue
            kept.append({**c, "amount": amt})
        raw["companions"] = kept
    return raw


_LIABILITY_AMOUNTS = ("last_statement_balance", "minimum_payment_amount",
                      "last_payment_amount")
_LIABILITY_DATES = ("next_payment_due_date", "last_payment_date",
                    "last_statement_issue_date")


def clean_liability_raw(v) -> dict:
    """A card's liabilities raw in the shape the aggregator pull writes:
    the balances numbers, the dates ISO days. The forecast casts
    last_statement_balance in SQL and parses next_payment_due_date in
    Python, and neither path was ever meant to see free text — one
    restored "N/A" raised out of every Today load, the Accounts page and
    the nightly email for the household. A value that is not a finite
    number / a real day is dropped; the readers treat a missing field as
    "the issuer never said", which is the conservative case."""
    raw = dict(_raw_dict(v))
    for key in _LIABILITY_AMOUNTS:
        if key not in raw:
            continue
        val = raw[key]
        try:
            num = float(val)
        except (TypeError, ValueError):
            num = None
        # bool is an int to float(); a True balance is not a number anyone
        # meant, and 'nan'/'inf' parse but poison the min()/max() walk
        if isinstance(val, bool) or num is None or not math.isfinite(num):
            raw.pop(key, None)
        elif isinstance(val, str):
            raw[key] = num
    for key in _LIABILITY_DATES:
        if key not in raw:
            continue
        try:
            day = as_date(raw[key])
        except ValueError:
            day = None
        if day is None:
            raw.pop(key, None)
        else:
            raw[key] = day.isoformat()
    return raw


def normalize_notify_shapes(cfg: dict) -> list[str]:
    """Coerce the notification settings a ZIP carries into the shapes the
    settings door would have written, dropping what cannot be. The worker
    reads these every hour; a value the door refuses (an hour of "x", a
    recipient list that is one string, a phone that is not an object)
    used to raise out of the due-check and silence every cadence for the
    household, or be read letter by letter. Returns the notes to show."""
    notes: list[str] = []
    sched = cfg.get("email_schedule")
    if "email_schedule" in cfg:
        if not isinstance(sched, dict) or not sched:
            cfg.pop("email_schedule", None)
            sched = None
    if isinstance(sched, dict):
        clean: dict = {}
        for cad in ("daily", "weekly", "monthly", "yearly"):
            c = sched.get(cad)
            if not isinstance(c, dict):
                continue
            try:
                hour = int(c.get("hour", 7))
                # weekday means something for the weekly cadence only; a
                # stray one on another cadence is ignored, not a reason
                # to drop the entry
                weekday = int(c.get("weekday", 0)) if cad == "weekly" else 0
            except (TypeError, ValueError):
                hour, weekday = -1, -1
            if not (0 <= hour <= 23 and 0 <= weekday <= 6):
                notes.append(f"the {cad} email schedule in this export was "
                             "malformed and was not restored")
                continue
            entry = {"on": bool(c.get("on")), "hour": hour}
            if cad == "weekly":
                entry["weekday"] = weekday
            for flag in ("sms", "push"):
                if c.get(flag):
                    entry[flag] = True
            if cad == "daily" and c.get("summary"):
                entry["summary"] = True
            clean[cad] = entry
        if clean:
            cfg["email_schedule"] = clean
        else:
            cfg.pop("email_schedule", None)
    if "email_send_hour_utc" in cfg:
        try:
            cfg["email_send_hour_utc"] = max(0, min(
                23, int(cfg["email_send_hour_utc"])))
        except (TypeError, ValueError):
            cfg.pop("email_send_hour_utc", None)
    rec = cfg.get("email_recipients")
    if isinstance(rec, str):
        rec = [x.strip() for x in rec.split(",")]
    if rec is not None and not isinstance(rec, list):
        rec = []
    if rec is not None:
        rec = [x.strip() for x in rec if isinstance(x, str) and x.strip()]
        if rec:
            cfg["email_recipients"] = rec
        else:
            cfg.pop("email_recipients", None)
    muted = cfg.get("email_muted")
    if muted is not None:
        # same cap as the settings door — a list of strings, bounded, so a
        # crafted ZIP cannot plant a document every config load re-parses
        from ..web.mailguard import MAX_RECIPIENTS
        muted = ([m.strip().lower() for m in muted
                  if isinstance(m, str) and m.strip()][:MAX_RECIPIENTS]
                 if isinstance(muted, list) else [])
        if muted:
            cfg["email_muted"] = muted
        else:
            cfg.pop("email_muted", None)
    for key in ("notify_phone", "notify_phone_pending"):
        if key in cfg and not isinstance(cfg[key], dict):
            cfg.pop(key, None)
    return notes


def _drop_sms_toggles_without_verified_phone(cfg: dict, dest: dict) -> None:
    sched = cfg.get("email_schedule")
    if not isinstance(sched, dict):
        return
    phone = dest.get("notify_phone") or {}
    if isinstance(phone, dict) and phone.get("verified") and phone.get("number"):
        return
    for entry in sched.values():
        if isinstance(entry, dict) and entry.get("sms"):
            entry["sms"] = False


# A restore ZIP is attacker-shaped input (script tokens can reach the
# import door) — a zip bomb declares gigabytes behind a few hundred bytes.
# Caps are checked against the directory's DECLARED sizes before any member
# is read; Python's zipfile never inflates past the declared size, so the
# declared sum is the real expansion bound.
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024


def _check_zip_sizes(z: zipfile.ZipFile) -> None:
    total = 0
    for info in z.infolist():
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(
                f"{info.filename} in this ZIP is too large to restore "
                f"({info.file_size // (1024 * 1024)} MB — the per-file "
                f"limit is {MAX_MEMBER_BYTES // (1024 * 1024)} MB)")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise ValueError(
            f"this ZIP expands to {total // (1024 * 1024)} MB — more than "
            f"the {MAX_TOTAL_BYTES // (1024 * 1024)} MB (512 MB) restore "
            f"limit")


def _txn_exists(conn, txn_id) -> bool:
    """Does this restored transaction exist yet?

    Migration 067 gave the transaction-child tables real foreign keys, which
    means restore can no longer insert an annotation whose transaction is
    absent. That is normally impossible — transactions.csv is restored first
    — but a backup taken BEFORE 067 can legitimately contain orphans, and a
    restore is exactly the wrong moment to abort on one. Skip the dangling
    row and carry on; it is counted in `skipped` like any other.
    """
    if not txn_id:
        return False
    return conn.execute("SELECT 1 FROM transactions WHERE id=%s",
                        (txn_id,)).fetchone() is not None


# A merged-away merchant points at its survivor; the pointer is not
# guaranteed to be one hop, and an archive can carry a chain or even a
# cycle. Same bound the resolver uses.
_MERGE_HOPS = 16


def _normalise_merchant_graph(conn) -> set[str]:
    """Make every merchant-to-merchant pointer name a merchant that is here.

    A restored `parent_id` or `merged_into` naming a merchant the archive
    did not carry becomes NULL, a merge CHAIN is collapsed to its terminal
    survivor, and a merge CYCLE is broken (nobody in it is merged away, so
    the ledger keeps showing all of them rather than resolving forever).

    Returns the ids that really exist, for screening the transaction and
    alias rows that follow."""
    rows = conn.execute(
        "SELECT id::text AS id, parent_id::text AS parent_id, "
        "       merged_into::text AS merged_into FROM merchants").fetchall()
    have = {r["id"] for r in rows}
    merged = {r["id"]: r["merged_into"] for r in rows}

    for r in rows:
        fixes = {}
        if r["parent_id"] is not None and r["parent_id"] not in have:
            fixes["parent_id"] = None
        if r["merged_into"] is not None:
            end = _terminal_survivor(merged, have, r["id"])
            if end != r["merged_into"]:
                fixes["merged_into"] = end
        if fixes:
            sets = ", ".join(f"{c} = %s" for c in fixes)
            conn.execute(f"UPDATE merchants SET {sets} WHERE id = %s",
                         (*fixes.values(), r["id"]))
    return have


def _known_merchant(value, known: set):
    """A merchant id from the archive, or NULL when it names no merchant.

    Compared lower-cased: Postgres renders a uuid lower-case, and a
    hand-edited CSV should not lose a pointer to letter case."""
    v = (value or "").strip().lower() or None
    return v if v in known else None


def _terminal_survivor(merged: dict, have: set, start: str):
    """The end of `start`'s merged-into chain, or None when the chain runs
    out of the archive or loops back on itself."""
    seen = {start}
    cur = merged.get(start)
    last = None
    while cur is not None:
        if cur not in have or cur in seen or len(seen) >= _MERGE_HOPS:
            return None                  # dangling, or a cycle: no survivor
        seen.add(cur)
        last = cur
        cur = merged[cur]
    return last


def _ein_last4_for_restore(r: dict) -> str | None:
    """The last four of a restored W-2's EIN, never the EIN.

    A pre-migration-077 export carries the full number in `ein`. Restoring
    it would put back exactly what 077 removed, so the value is reduced here
    at the door instead."""
    import re as _re
    v = (r.get("ein_last4") or "").strip()
    if v:
        return v[-4:]
    digits = _re.sub(r"\D", "", str(r.get("ein") or ""))
    return digits[-4:] if len(digits) >= 4 else None


def _unescape(v):
    # Reverse pages._csv_cell's spreadsheet-formula
    # neutralization. The export prefixes ' onto cells starting
    # = + - @ tab CR — correct for Excel, but this same ZIP is the
    # supported restore input, and without the reverse step a bank's
    # '-ACH DEBIT ...' descriptor landed as "'-ACH DEBIT" forever,
    # and exact-string merchant rules (llm_categorize joins on the
    # raw merchant) silently stopped matching after migration.
    # The exact rule lives in one place — api._csv_safe — so the two
    # sides cannot drift: undo the quote when what follows it is what
    # the export would have quoted.
    if isinstance(v, str) and v[:1] == "'":
        from ..web.api import _csv_safe
        if _csv_safe(v[1:]) == v:
            return v[1:]
    return v


def restore_zip(conn, data: bytes, *, progress=None) -> dict:
    """Merge an export ZIP into this tenant. `progress`, when given, is
    called as (table, rows, done) while the members stream past — the
    background-job door uses it to keep a status row fresh so a large
    restore reports where it is instead of looking hung."""
    token = _PROGRESS.set(progress) if progress is not None else None
    try:
        return _restore_zip(conn, data)
    finally:
        if token is not None:
            _PROGRESS.reset(token)


def _renumber_home_ranks(conn, groups: list[str]) -> int:
    """Make home_rank unique within the given link groups, keeping the
    order the ranks already express. An archive can carry two members of
    one group at the same rank (a link edited on one side, or two archives
    merged into one tenant), and nothing on the write side checks it —
    the failover read picks "the lowest healthy rank", which on a tie is
    whichever row the planner returns first, so the household's chosen
    primary could silently swap between reads. Scoped to the groups the
    archive touched: an unscoped UPDATE row-locked every link the tenant
    has for the rest of the restore transaction, and a reorder on the
    Accounts page for an unrelated group hung behind it. Returns rows
    changed."""
    if not groups:
        return 0
    cur = conn.execute(
        """UPDATE account_links l SET home_rank = s.rn
             FROM (SELECT account_id,
                          row_number() OVER (PARTITION BY group_id
                                             ORDER BY home_rank, created_at,
                                                      account_id) - 1 AS rn
                     FROM account_links
                    WHERE group_id = ANY(%s::uuid[])) s
            WHERE l.account_id = s.account_id AND l.home_rank <> s.rn""",
        (groups,))
    return cur.rowcount


_NO_INVITES = {"total": 0, "rows": 0, "accepted": 0, "declined": 0,
               "pending": 0}


def _restore_recipient_invites(conn, z) -> dict:
    """Restore what the household's daily-email recipients ANSWERED, under
    one rule: an acceptance is a fact about a mailbox, and only this
    instance's own invite flow may create one.

    This is the half of `email_recipients` that is not config. The address
    list rides in `tenant_settings` like any other setting, but the answer
    lives in a control-plane table, and `worker._recipients_raw` mails an
    extra recipient only if they accepted. So the file is read for answers —
    but a ZIP is untrusted input. Nothing signs it, nothing proves this
    instance produced it, and the person who uploads it is the same person
    whose proposal the invite exists to check. Copying `accepted_at` out of a
    CSV therefore manufactures the one thing the recipient alone may create:
    add a line for somebody's address with a timestamp in it and that mailbox
    starts receiving a household's balances tomorrow morning, having never
    been asked. That is a mail-enrolment primitive, so the file does not get
    to write acceptances.

    What travels, and why each direction is safe:

    * A DECLINE ALWAYS TRAVELS. It is the fail-safe answer — the worst a
      forged one can do is keep mail from being sent — and carrying it is
      what stops the destination's next settings save from re-inviting
      somebody who already said no.
    * AN ACCEPTANCE TRAVELS ONLY FOR AN ADDRESS THIS INSTANCE ALREADY KNOWS
      AS ONE OF ITS OWN USERS. That is the single exception, and it holds
      because the fact is checked HERE rather than read out of the file: the
      address holds an account on this very household, so the daily mail
      tells that person nothing they cannot already read by signing in, they
      can mute themselves in Settings, and on hosted they additionally had to
      prove control of the mailbox through this instance's own verification
      (`mailguard.members_only` is the same proof `_recipients_raw` demands
      of the owner). Forging one for a non-member — the whole attack — lands
      nothing at all.
    * NOTHING ELSE TRAVELS. An unanswered invite is a question, not an
      answer, so it lands no row: the archive's copy has no live link here
      (the raw token is in a recipient's mailbox and points at the source
      instance), and a row with no token reads in Settings → Email as "invite
      sent, waiting for them" or "invite expired" — both of which describe a
      link that does not exist, and both of which leave the owner waiting for
      a confirmation that can never arrive. With no row the same screen says
      "not invited" next to an Invite button, which is true and is the way
      out. `last_sent_at`/`send_count` do not travel either: they feed
      `recipient_invites.invite`'s live resend cooldown and hourly tenant
      ceiling, so an archive full of fresh send stamps could throttle the
      invites this instance is trying to mint.

    A DECISION MADE HERE ALWAYS WINS, and only a decision counts as one. The
    conflict clause used to be `DO NOTHING`, which fires on any existing row
    — including the merely PENDING row that `mailguard.invite_added` writes
    on an ordinary settings save. The ordinary migration shape (sign up on
    the destination, type your recipients, then restore) therefore threw away
    every answer the archive carried and reported nothing, because only
    successes were counted. An ANSWERED row still wins; an unanswered one is
    now updated in place, keeping its live token so a link already in
    somebody's mailbox is not quietly voided.

    An accepted row is not an authorization boundary and is not being
    treated as one: on hosted, `mailguard.members_only` is what keeps a
    household's balances from reaching an outside inbox, and the restored
    recipient list passes through that filter regardless of what this table
    says. This function is about consent, which is a different question.

    Runs on the ADMIN connection and after the data transaction commits:
    migration 074 grants the app role nothing here on purpose, and a
    consent row must not land for a restore that then failed.

    Returns a tally of what the household actually ends up with — `total`
    addresses in the archive, `rows` written, and how many of those addresses
    are `accepted`, `declined`, or still `pending` here — so the restore can
    say out loud who has to answer again."""
    seen: dict[str, dict] = {}
    for r in _rows(z, "recipient_invites.csv"):
        email = (r.get("email") or "").strip().lower()
        if not email:
            continue
        # one mailbox is one recipient however many times the file names it
        seen[email] = r
        if len(seen) >= _MAX_RECIPIENT_INVITES:
            break
    if not seen:
        return dict(_NO_INVITES)
    cur = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    tid = cur["tid"] if cur else None
    if not tid:
        return dict(_NO_INVITES)
    from ..db import tenancy
    from ..web import mailguard
    written = 0
    admin = tenancy.admin_connect()
    try:
        with admin.transaction():
            # The one fact this instance can establish for itself. On hosted
            # a user row is only proof of mailbox control once verification
            # has been answered, which is exactly the test the owner has to
            # pass in `worker._recipients_raw`.
            proof = (" AND verified_at IS NOT NULL"
                     if mailguard.members_only() else "")
            members = {r["email"].lower() for r in admin.execute(
                "SELECT email FROM users WHERE tenant_id = %s" + proof,
                (tid,)).fetchall()}
            for email, r in seen.items():
                declined_at = _ts(r.get("declined_at"))
                # a decline read out of the file is believed; an acceptance
                # only for an address this household already knows as a user
                accepted_at = (None if declined_at or email not in members
                               else _ts(r.get("accepted_at")))
                if not declined_at and not accepted_at:
                    continue
                inviter = (r.get("invited_by_email") or "").strip().lower()
                c = admin.execute(
                    """INSERT INTO recipient_invites
                           (tenant_id, email, token_hash, invited_by,
                            created_at, expires_at, accepted_at,
                            declined_at, last_sent_at, send_count)
                       VALUES (%s, %s, NULL,
                               (SELECT id FROM users
                                 WHERE tenant_id = %s
                                   AND lower(email) = %s),
                               COALESCE(%s, now()), %s, %s, %s, NULL, 0)
                       ON CONFLICT (tenant_id, email) DO UPDATE
                          SET accepted_at = EXCLUDED.accepted_at,
                              declined_at = EXCLUDED.declined_at
                        WHERE recipient_invites.accepted_at IS NULL
                          AND recipient_invites.declined_at IS NULL""",
                    (tid, email, tid, inviter or None,
                     _ts(r.get("created_at")), _ts(r.get("expires_at")),
                     accepted_at, declined_at))
                written += c.rowcount
            # Report the STATE, not the writes. A row the destination already
            # held answered writes nothing and is still good news; an address
            # that stayed pending writes nothing and is the thing the owner
            # has to act on. Counting inserts alone reported neither.
            final = {r["email"]: r for r in admin.execute(
                "SELECT email, accepted_at, declined_at FROM recipient_invites"
                " WHERE tenant_id = %s AND email = ANY(%s)",
                (tid, list(seen))).fetchall()}
    finally:
        admin.close()
    accepted = sum(1 for e in seen if (final.get(e) or {}).get("accepted_at"))
    declined = sum(1 for e in seen if (final.get(e) or {}).get("declined_at"))
    return {"total": len(seen), "rows": written, "accepted": accepted,
            "declined": declined,
            "pending": len(seen) - accepted - declined}


def _restore_zip(conn, data) -> dict:
    # bytes (the API's small-archive door) or a seekable mapping (the
    # streamed restore door): reading a mapping through BytesIO would copy
    # the whole archive back into the heap the streaming was built to avoid
    z = zipfile.ZipFile(data if hasattr(data, "seek") else io.BytesIO(data))
    _check_zip_sizes(z)          # refuse zip bombs before reading rows
    counts: dict[str, int] = {}
    skipped = 0                  # rows already present (idempotent no-ops)

    # Validate the incoming config FIRST — the connection is autocommit,
    # so a rejection after the row loops would still leave the ZIP's
    # transactions behind. Reject at the door, before anything lands.
    # tenant_settings is a single tiny row and we index it — materialize just
    # this one member (the big tables below stay streamed via the generator)
    settings = list(_rows(z, "tenant_settings.csv"))
    cfg_in = (_j(settings[0]["config"])
              if settings and settings[0].get("config") else None)
    notes: list[str] = []
    if isinstance(cfg_in, dict):
        notes = _check_config(cfg_in)

    # One transaction — the tenant connection is
    # autocommit, so a mid-restore failure (bad row deep in the
    # ZIP, connection drop) used to leave a partial restore
    # behind with no way to tell what landed. All-or-nothing now.
    with conn.transaction():
        for r in _rows(z, "items.csv"):
            cur = conn.execute(
                """INSERT INTO items (id, aggregator, institution_id,
                       institution_name, status, raw, logo, brand_color, url)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r.get("aggregator") or "manual",
                 r.get("institution_id") or None,
                 r.get("institution_name") or None,
                 # sync-less shell until the aggregator is reconnected —
                 # except archived items (push-fed plan-CSV/coinbase, archived
                 # simplefin children) which stay archived: 'restored' would
                 # re-enter them into connection-freshness checks and the
                 # hourly pull loop they were deliberately kept out of
                 ("archived" if (r.get("status") or "") == "archived"
                  else "restored"),
                 jsonb(_raw_dict(_j(r.get("raw")))),
                 r.get("logo") or None, r.get("brand_color") or None,
                 r.get("url") or None))
            counts["items"] = counts.get("items", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        # the merchant ROWS (migration 097) come before the transactions and
        # aliases that point at them; a ZIP from before 097 has no file and
        # the nightly resolve rebuilds identity from the raw records.
        # Bare ON CONFLICT: merchants also carries a partial unique index
        # on plaid_entity_id, and a column-list arbiter on the PK let a
        # restored row with a live entity id abort the whole restore
        for r in _rows(z, "merchants.csv"):
            if not r.get("id") or not r.get("name"):
                continue
            cur = conn.execute(
                """INSERT INTO merchants (id, plaid_entity_id, name, name_source,
                       kind, logo_url, website, phone, mcc, parent_id,
                       merged_into, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT DO NOTHING""",
                (r["id"], r.get("plaid_entity_id") or None,
                 _unescape(r.get("name")), r.get("name_source") or "layer1",
                 r.get("kind") or "merchant", r.get("logo_url") or None,
                 r.get("website") or None, r.get("phone") or None,
                 r.get("mcc") or None, r.get("parent_id") or None,
                 r.get("merged_into") or None, _ts(r.get("created_at"))))
            counts["merchants"] = counts.get("merchants", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        # An archive can point at merchants it does not carry — a partial
        # export, a hand-edited CSV, a ZIP written before merchants existed
        # for some of its rows. Every such pointer is a lie the rest of the
        # restore would faithfully copy in, and the nightly identity pass
        # then crashed on it for that household every night. Normalise the
        # graph against what is ACTUALLY in the table now, and keep the set
        # of real ids to screen the transaction and alias rows below.
        known_merchants = _normalise_merchant_graph(conn)

        # business_entity BEFORE accounts/transactions — their
        # entity_id FK-references it. ein_enc is intentionally absent from the
        # export (export invariant: the raw EIN only leaves via a gated
        # reveal), so it restores NULL; ein_last4 carries for display.
        for r in _rows(z, "business_entity.csv"):
            if not r.get("id"):
                continue
            cur = conn.execute(
                """INSERT INTO business_entity (id, name, structure, state,
                       formation_date, business_start_date, ein_last4,
                       registered_agent, fiscal_year_end, status,
                       home_office_sqft, income_tax_rate, filing_status,
                       tax_reserve_account_id, archived_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r.get("name"), r.get("structure"),
                 r.get("state") or None,
                 as_date(r["formation_date"]) if r.get("formation_date") else None,
                 as_date(r["business_start_date"]) if r.get("business_start_date") else None,
                 r.get("ein_last4") or None, r.get("registered_agent") or None,
                 r.get("fiscal_year_end") or None, r.get("status") or "active",
                 _i(r.get("home_office_sqft")), _f(r.get("income_tax_rate")),
                 r.get("filing_status") or None,
                 # The export is SELECT *, so the ZIP already carries this;
                 # the INSERT's fixed column list did not, so a round trip
                 # silently forgot which account the tax money sits in.
                 r.get("tax_reserve_account_id") or None,
                 # when the business was closed — an archived entity must
                 # restore as archived-on-the-same-date, not freshly closed
                 r.get("archived_at") or None))
            counts["business_entity"] = counts.get("business_entity", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "accounts.csv"):
            cur = conn.execute(
                """INSERT INTO accounts (id, item_id, name, display_name,
                       official_name, type, subtype, mask, balance_current,
                       balance_available, currency, entity_id, owner, raw,
                       user_removed_at, type_user_set, balance_limit)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r.get("item_id") or None, r.get("name"),
                 r.get("display_name") or None, r.get("official_name") or None,
                 r.get("type"), r.get("subtype") or None, r.get("mask") or None,
                 # _f, not truthiness — a non-string 0 must
                 # restore as 0, never NULL
                 _f(r.get("balance_current")), _f(r.get("balance_available")),
                 r.get("currency") or None, r.get("entity_id") or None,
                 # the account's default owner label ("yours/mine/ours") —
                 # dropping it reverted every account to unattributed on
                 # restore. An export taken before the column existed simply
                 # lacks it, which restores as unattributed (None).
                 r.get("owner") or None,
                 jsonb(_raw_dict(_j(r.get("raw")))),
                 # export writes user_removed_at (migration 066's
                 # soft-hide) but restore dropped it, so an export→restore
                 # round trip silently UN-hid every account the user had
                 # hidden — back in the lists, back in the money aggregates,
                 # with no indication anything changed.
                 _ts(r.get("user_removed_at")),
                 # type_user_set is the same omission one column over. It
                 # is the flag that stops sync
                 # from overwriting a type the USER corrected — restore
                 # landed it FALSE, so the restored account looked right
                 # until the next aggregator sync silently reclassified it
                 # back. balance_limit rides along: also exported, also
                 # dropped, and it is what credit-utilisation reads.
                 _b(r.get("type_user_set")),
                 _f(r.get("balance_limit"))))
            counts["accounts"] = counts.get("accounts", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        # Transactions are BATCHED. Every other member here is
        # bounded by user actions — accounts, bills, notes — but this one is
        # the ledger: a real restore is tens of thousands of rows, and a
        # per-row execute() paid a full client/server round trip for each.
        # psycopg pipelines executemany, so the same INSERT ... ON CONFLICT
        # DO NOTHING travels in chunks instead. Memory is unchanged: _rows
        # still streams, and only CHUNK rows are held at a time.
        def _txn_params(r):
            return (r["id"], r.get("account_id") or None, as_date(r["date"]),
                    as_date(r.get("authorized_date") or None),
                    _f(r.get("amount")),      # 0 is a real amount
                    _unescape(r.get("name")),
                    _unescape(r.get("merchant_name")) or None,
                    r.get("category_primary") or None,
                    r.get("category_detailed") or None,
                    # The aggregator's OWN verdict and its confidence. These
                    # are what llm_categorize's low-confidence gate reads,
                    # so a restore that dropped them silently changed which
                    # rows the model would revisit — see below.
                    r.get("category_plaid") or None,
                    r.get("category_plaid_detailed") or None,
                    # a TEXT tier ('VERY_HIGH'/'HIGH'/'LOW'/…), never a
                    # number — float-coercing it raised on the first real
                    # Plaid row and, because the restore is one transaction,
                    # aborted the whole migration
                    r.get("category_plaid_confidence") or None,
                    _unescape(r.get("category_override")) or None,
                    int(r.get("pending") or 0),
                    r.get("pending_transaction_id") or None,
                    r.get("payment_channel") or None,
                    int(r.get("removed") or 0),
                    r.get("entity_id") or None,   # per-txn override
                    # per-transaction owner exception (effective owner is
                    # COALESCE(owner_override, account.owner)) and the
                    # statement-derived outlet brand (payee display/search,
                    # the merchant rename screen). Both live on the table
                    # via ALTER TABLE migrations, both ride the SELECT *
                    # export, and both were silently dropped here.
                    r.get("owner_override") or None,
                    _unescape(r.get("merchant_outlet")) or None,
                    # the merchant ROW (097) — but only when the archive
                    # actually carried it. A pointer at a merchant that is
                    # not here is worse than no pointer: identity reads
                    # crash on it and the ledger shows nothing at all.
                    _known_merchant(r.get("merchant_id"), known_merchants),
                    jsonb(_raw_dict(_j(r.get("raw")))),
                    # migration 098: the aggregator's facts as columns
                    r.get("category_source") or None,
                    r.get("check_number") or None,
                    r.get("payment_processor") or None,
                    _unescape(r.get("location_city")) or None,
                    r.get("location_region") or None,
                    _unescape(r.get("location_address")) or None,
                    r.get("location_postal") or None,
                    _f(r.get("location_lat")) if r.get("location_lat") not in (None, "") else None,
                    _f(r.get("location_lon")) if r.get("location_lon") not in (None, "") else None,
                    r.get("location_store") or None,
                    r.get("mcc") or None,
                    r.get("override_source") or None,
                    # a hold the person retired stays retired through the next sync
                    r.get("retired_at") or None)

        # EVERY column the export writes. The export is `SELECT *`, so it
        # carries all twenty; the restore read thirteen and dropped the rest
        # without a word — authorized_date, the three category_plaid_*
        # fields, pending_transaction_id and payment_channel. Round-tripping
        # an instance therefore degraded categorisation and bill matching in
        # ways nothing reported. Same shape as the single dropped column
        # fixed; this is the whole set.
        _TXN_SQL = """INSERT INTO transactions (id, account_id, date,
                          authorized_date, amount,
                          name, merchant_name, category_primary,
                          category_detailed, category_plaid,
                          category_plaid_detailed, category_plaid_confidence,
                          category_override, pending, pending_transaction_id,
                          payment_channel, removed, entity_id, owner_override,
                          merchant_outlet, merchant_id, raw,
                          category_source, check_number, payment_processor,
                          location_city, location_region, location_address,
                          location_postal, location_lat, location_lon,
                          location_store, mcc, override_source, retired_at)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                              %s,%s,%s,%s,%s,%s,%s,
                              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (tenant_id, id) DO NOTHING"""
        CHUNK = 1000
        batch: list = []

        def _flush():
            nonlocal skipped, batch
            if not batch:
                return
            cur = conn.cursor()
            # returning=False keeps executemany in its fast pipelined path;
            # rowcount is then the TOTAL inserted across the batch, which is
            # exactly what the counters want.
            cur.executemany(_TXN_SQL, batch)
            inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            counts["transactions"] = counts.get("transactions", 0) + inserted
            skipped += len(batch) - inserted
            batch = []

        for r in _rows(z, "transactions.csv"):
            if not r.get("id") or not r.get("date"):
                continue
            batch.append(_txn_params(r))
            if len(batch) >= CHUNK:
                _flush()
        _flush()

        # bills.csv since the rename; older backups say recurring.csv
        bills_member = ("bills.csv" if "bills.csv" in z.namelist()
                        else "recurring.csv")
        for r in _rows(z, bills_member):
            cur = conn.execute(
                """INSERT INTO bills (id, type, payee, amount, frequency,
                       monthly_amount, due_on, category, merchant, is_completed,
                       active, synced_at, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r.get("type"), _unescape(r.get("payee")),
                 _f(r.get("amount")),          # same as above
                 r.get("frequency") or None,
                 _f(r.get("monthly_amount")),
                 as_date(r.get("due_on")) if r.get("due_on") else None,
                 r.get("category") or None, r.get("merchant") or None,
                 int(r.get("is_completed") or 0), int(r.get("active") or 1),
                 jsonb(clean_bill_raw(_j(r.get("raw"))))))
            counts["bills"] = counts.get("bills", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "manual_categories.csv"):
            if _txn_exists(conn, r.get("transaction_id")):
                # bill_id (a bill's transaction category, not a person's
                # correction) travels too, else a restore turns every
                # bill-stamped row into a hand edit the bill can't revert
                cur = conn.execute(
                    """INSERT INTO manual_categories (transaction_id, category,
                                                      bill_id)
                       VALUES (%s,%s,%s) ON CONFLICT (tenant_id, transaction_id)
                       DO NOTHING""", (r["transaction_id"], r["category"],
                                       r.get("bill_id") or None))
                counts["overrides"] = counts.get("overrides", 0) + cur.rowcount
                skipped += 1 - cur.rowcount
        # an archive written before overrides carried their kind: classify
        # them from the manual_categories rows just restored and the store
        # prefixes, exactly as migration 127 did for live ledgers
        from ..engine import categories as _cats
        _cats.backfill_override_source(conn)

        for r in _rows(z, "reimbursements.csv"):
            if (_txn_exists(conn, r.get("expense_id"))
                    and _txn_exists(conn, r.get("reimburse_id"))):
                cur = conn.execute(
                    """INSERT INTO reimbursements (expense_id, reimburse_id,
                           partial, amount)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, expense_id, reimburse_id)
                       DO NOTHING""",
                    (r["expense_id"], r["reimburse_id"],
                     _i(r.get("partial"), 0) or 0, _f(r.get("amount"))))
                counts["reimbursements"] = counts.get("reimbursements", 0) + cur.rowcount
                skipped += 1 - cur.rowcount

        for r in _rows(z, "reimburse_flags.csv"):
            if _txn_exists(conn, r.get("txn_id")):
                cur = conn.execute(
                    """INSERT INTO reimburse_flags (txn_id, partial, expected)
                       VALUES (%s,%s,%s)
                       ON CONFLICT (tenant_id, txn_id) DO NOTHING""",
                    (r["txn_id"], _i(r.get("partial"), 0) or 0,
                     _f(r.get("expected"))))
                counts["reimburse_flags"] = counts.get("reimburse_flags", 0) + cur.rowcount
                skipped += 1 - cur.rowcount

        # account_links IS the shadow set — without it a
        # round trip un-shadowed every duplicate source (Plaid + SimpleFIN
        # of the same real account) and every money aggregate double-counted.
        # Guarded on the account existing so a partial archive cannot plant
        # dangling links.
        touched_groups: set[str] = set()
        for r in _rows(z, "account_links.csv"):
            if not (r.get("group_id") and r.get("account_id")):
                continue
            touched_groups.add(str(r["group_id"]))
            cur = conn.execute(
                """INSERT INTO account_links (group_id, account_id, home_rank)
                   SELECT %s, %s, %s
                   WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.id = %s)
                   ON CONFLICT (tenant_id, account_id) DO NOTHING""",
                (r["group_id"], r["account_id"],
                 _i(r.get("home_rank"), 0), r["account_id"]))
            counts["account_links"] = (counts.get("account_links", 0)
                                       + cur.rowcount)
            skipped += 1 - cur.rowcount
        if touched_groups:
            _renumber_home_ranks(conn, sorted(touched_groups))

        # Receipt images (base64 in the CSV) + tagged line
        # items — the Schedule-C expense-report input. Guarded on the
        # transaction / receipt existing, mirroring manual_categories.
        import base64 as _b64

        from ..engine import receipts as _receipts
        for r in _rows(z, "receipts.csv"):
            if not (r.get("id") and r.get("txn_id") and r.get("image")):
                continue
            if not _txn_exists(conn, r.get("txn_id")):
                continue
            # A ZIP cell must satisfy exactly what a live upload must
            # (receipts.add): a mime on the allowlist and the same size cap.
            # The stored mime is what /api/receipts/{rid}/image serves the
            # bytes back as, so a crafted mime is a same-origin content
            # type of the attacker's choosing, and an unbounded cell is a
            # member-cell-sized allocation per row. Oversize or off-list
            # rows are skipped like corrupt ones — the rest still restore.
            mime = str(r.get("mime") or "image/jpeg").strip().lower()
            if mime not in _receipts.ALLOWED_MIMES:
                continue
            if len(r["image"]) > _receipts.MAX_IMAGE * 4 // 3 + 4:
                continue                     # cheaper than decoding first
            try:
                img = _b64.b64decode(r["image"], validate=True)
            except Exception:
                continue                     # corrupt cell — skip, not abort
            if not img or len(img) > _receipts.MAX_IMAGE:
                continue
            kind = r.get("kind") or "receipt"
            if kind not in _receipts.KINDS:
                kind = "receipt"
            cur = conn.execute(
                """INSERT INTO receipts (id, txn_id, image, mime, kind,
                       status, parsed, error, created_at, parsed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                           COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r["txn_id"], img, mime,
                 kind, r.get("status") or "uploaded",
                 (jsonb(_raw_dict(_j(r.get("parsed"))))
                  if r.get("parsed") else None),
                 r.get("error") or None,
                 _ts(r.get("created_at")), _ts(r.get("parsed_at"))))
            counts["receipts"] = counts.get("receipts", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "receipt_items.csv"):
            if not r.get("receipt_id"):
                continue
            cur = conn.execute(
                """INSERT INTO receipt_items (receipt_id, line, description,
                       qty, amount, tag)
                   SELECT %s, %s, %s, %s, %s, %s
                   WHERE EXISTS (SELECT 1 FROM receipts x WHERE x.id = %s)
                   ON CONFLICT (tenant_id, receipt_id, line) DO NOTHING""",
                (r["receipt_id"], _i(r.get("line"), 0),
                 r.get("description") or "", _f(r.get("qty")),
                 _f(r.get("amount")), r.get("tag") or "",
                 r["receipt_id"]))
            counts["receipt_items"] = (counts.get("receipt_items", 0)
                                       + cur.rowcount)
            skipped += 1 - cur.rowcount

        for r in _rows(z, "business_flags.csv"):
            if _txn_exists(conn, r.get("txn_id")):
                cur = conn.execute(
                    """INSERT INTO business_flags (txn_id)
                       VALUES (%s) ON CONFLICT (tenant_id, txn_id) DO NOTHING""",
                    (r["txn_id"],))
                counts["business_flags"] = counts.get("business_flags", 0) + cur.rowcount
                skipped += 1 - cur.rowcount

        # ---- business tables (entities restored above,
        # transactions above — these depend on both) ----
        for r in _rows(z, "entity_membership.csv"):
            if not r.get("id") or not r.get("entity_id"):
                continue
            cur = conn.execute(
                """INSERT INTO entity_membership (id, entity_id, member_name,
                       ownership_pct, is_manager)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r["entity_id"], r.get("member_name"),
                 _f(r.get("ownership_pct")), _b(r.get("is_manager"))))
            counts["entity_membership"] = counts.get("entity_membership", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "equity_movement.csv"):
            if not r.get("id") or not r.get("entity_id"):
                continue
            cur = conn.execute(
                # bare ON CONFLICT: beyond (tenant_id, id) this table has a
                # partial unique index on (tenant_id, txn_id, kind) — one
                # movement per transaction per kind. A destination that
                # already recorded the same movement under its OWN id passes
                # an id-targeted conflict clause and the raise would roll
                # back the entire all-or-nothing restore. Any unique match
                # means "already present", which is exactly the skip the
                # docstring promises.
                """INSERT INTO equity_movement (id, entity_id, kind, amount,
                       date, member_id, txn_id, form, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (r["id"], r["entity_id"], r.get("kind"), _f(r.get("amount")),
                 as_date(r["date"]) if r.get("date") else None,
                 r.get("member_id") or None,
                 # keep the movement even if its transaction is gone — the
                 # FK is ON DELETE SET NULL for exactly this reason
                 (r.get("txn_id") if _txn_exists(conn, r.get("txn_id"))
                  else None),
                 r.get("form") or None, r.get("note") or None))
            counts["equity_movement"] = counts.get("equity_movement", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        from ..engine.books import BUCKETS as _BUCKETS
        for r in _rows(z, "business_txn_class.csv"):
            if not _txn_exists(conn, r.get("txn_id")):
                skipped += 1
                continue
            # The bucket is an enum the app validates on write but the DB
            # does not constrain, so a hand-edited ZIP is the one way a
            # value outside the set can exist at all — and this column is
            # exported to CSV, where arbitrary text is a formula-injection
            # carrier. Refuse the row rather than plant it.
            bucket = (r.get("bucket") or "").strip()
            if bucket and bucket not in _BUCKETS:
                skipped += 1
                continue
            cur = conn.execute(
                """INSERT INTO business_txn_class (txn_id, bucket, sched_c_line,
                       note)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, txn_id) DO NOTHING""",
                (r["txn_id"], bucket or None, r.get("sched_c_line") or None,
                 r.get("note") or None))
            counts["business_txn_class"] = counts.get("business_txn_class", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "compliance_obligation.csv"):
            if not r.get("id") or not r.get("entity_id"):
                continue
            cur = conn.execute(
                """INSERT INTO compliance_obligation (id, entity_id, title,
                       due_date, recurrence, fee, url, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r["entity_id"], r.get("title"),
                 as_date(r["due_date"]) if r.get("due_date") else None,
                 r.get("recurrence") or "yearly", _f(r.get("fee")),
                 r.get("url") or None, r.get("note") or None))
            counts["compliance_obligation"] = counts.get("compliance_obligation", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "mileage_log.csv"):
            if not r.get("id") or not r.get("entity_id"):
                continue
            cur = conn.execute(
                """INSERT INTO mileage_log (id, entity_id, date, miles, purpose,
                       note)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], r["entity_id"],
                 as_date(r["date"]) if r.get("date") else None,
                 _f(r.get("miles")), r.get("purpose") or None,
                 r.get("note") or None))
            counts["mileage_log"] = counts.get("mileage_log", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "vendor_1099.csv"):
            if not r.get("id") or not r.get("entity_id"):
                continue
            cur = conn.execute(
                # bare ON CONFLICT: vendor_1099 is also UNIQUE on
                # (tenant_id, entity_id, merchant), so a destination that
                # already marked this vendor under a different id must be a
                # skip, not a UniqueViolation that aborts the whole restore.
                """INSERT INTO vendor_1099 (id, entity_id, merchant, reportable,
                       tin_last4, note)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (r["id"], r["entity_id"], r.get("merchant"),
                 _b(r.get("reportable")), r.get("tin_last4") or None,
                 r.get("note") or None))
            counts["vendor_1099"] = counts.get("vendor_1099", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "transaction_notes.csv"):
            if not _txn_exists(conn, r.get("txn_id")) or not r.get("note"):
                skipped += 1
                continue
            cur = conn.execute(
                """INSERT INTO transaction_notes (txn_id, note)
                   VALUES (%s,%s)
                   ON CONFLICT (tenant_id, txn_id) DO NOTHING""",
                (r["txn_id"], r["note"]))
            counts["transaction_notes"] = counts.get("transaction_notes", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        # ---- converter v2 (migration 008): history / reference tables ----

        for r in _rows(z, "liabilities.csv"):
            if not r.get("account_id"):
                continue
            cur = conn.execute(
                """INSERT INTO liabilities (account_id, as_of, raw)
                   VALUES (%s, COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, account_id) DO NOTHING""",
                (r["account_id"], _ts(r.get("as_of")),
                 jsonb(clean_liability_raw(_j(r.get("raw"))))))
            counts["liabilities"] = counts.get("liabilities", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "holdings.csv"):
            if not r.get("account_id") or not r.get("symbol"):
                continue
            cur = conn.execute(
                """INSERT INTO holdings (account_id, symbol, name, quantity,
                       price, value, as_of, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, account_id, symbol) DO NOTHING""",
                (r["account_id"], r["symbol"], r.get("name") or None,
                 _f(r.get("quantity")), _f(r.get("price")), _f(r.get("value")),
                 _ts(r.get("as_of")), jsonb(_raw_dict(_j(r.get("raw"))))))
            counts["holdings"] = counts.get("holdings", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "crypto_holdings.csv"):
            if not r.get("account_id") or not r.get("currency"):
                continue
            cur = conn.execute(
                """INSERT INTO crypto_holdings (account_id, currency, quantity,
                       native_usd, as_of, raw)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, account_id, currency) DO NOTHING""",
                (r["account_id"], r["currency"], _f(r.get("quantity")),
                 _f(r.get("native_usd")), _ts(r.get("as_of")),
                 jsonb(_raw_dict(_j(r.get("raw"))))))
            counts["crypto_holdings"] = counts.get("crypto_holdings", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "networth_recorded.csv"):
            if not r.get("month") or r.get("total") in (None, ""):
                continue
            cur = conn.execute(
                """INSERT INTO networth_recorded (month, total, source)
                   VALUES (%s,%s,%s)
                   ON CONFLICT (tenant_id, month) DO NOTHING""",
                (r["month"], _f(r["total"]), r.get("source") or None))
            counts["networth_recorded"] = counts.get("networth_recorded", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "networth_snapshot.csv"):
            if not r.get("date"):
                continue
            by_class = _j(r.get("by_class"))
            cur = conn.execute(
                """INSERT INTO networth_snapshot (date, total, by_class)
                   VALUES (%s,%s,%s)
                   ON CONFLICT (tenant_id, date) DO NOTHING""",
                (as_date(r["date"]), _f(r.get("total")),
                 jsonb(by_class) if by_class is not None else None))
            counts["networth_snapshot"] = counts.get("networth_snapshot", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        # per-month frozen budgets: without them every closed month on the
        # destination falls back to the no-verdict net view. Scrubbed on
        # the way in like the live settings blob — the snapshot only feeds
        # month_status math, so a crafted ZIP must not smuggle credential
        # or policy keys through this second config door. Destination rows
        # win on conflict (a month already frozen here stays frozen).
        for r in _rows(z, "budget_snapshots.csv"):
            y_, m_ = _i(r.get("year")), _i(r.get("month"))
            snap_cfg = _j(r.get("config"))
            if not y_ or not m_ or not (1 <= m_ <= 12) \
                    or not isinstance(snap_cfg, dict):
                continue
            # the budgets must be NUMBERS — the live save path _num()s
            # every write, and this ZIP is the one door that bypasses
            # it. A stored "x" used to 500 the history endpoint and the
            # month/year lenses with no UI to remove the row.
            from ..engine.budget import _snapshot_budgets_numeric
            if not _snapshot_budgets_numeric(snap_cfg):
                skipped += 1
                continue
            # the frozen bill schedule travels too; shape-checked here and
            # row-by-row on read (snapshot_bill_rows), so a crafted blob
            # degrades to "no frozen schedule", never a crash
            snap_bills = _j(r.get("bills"))
            if not (isinstance(snap_bills, dict)
                    and isinstance(snap_bills.get("rows"), list)):
                snap_bills = None
            cur = conn.execute(
                """INSERT INTO budget_snapshots (year, month, config, bills,
                       captured_at, source)
                   VALUES (%s,%s,%s,%s,COALESCE(%s, now()),%s)
                   ON CONFLICT (tenant_id, year, month) DO NOTHING""",
                (y_, m_, jsonb(scrub_config(snap_cfg)),
                 jsonb(snap_bills) if snap_bills is not None else None,
                 _ts(r.get("captured_at")),
                 r.get("source") if r.get("source") in ("close", "backfill")
                 else "close"))
            counts["budget_snapshots"] = \
                counts.get("budget_snapshots", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "income_annual.csv"):
            if r.get("year") in (None, ""):
                continue
            cur = conn.execute(
                """INSERT INTO income_annual (year, total_income, agi,
                       taxable_income, tax_paid, wages, primary_wages,
                       investment_income, capital_gain, spouse_wages,
                       ss_earnings, medicare_earnings, filing_status, joint,
                       source, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, year) DO NOTHING""",
                (_i(r["year"]), _f(r.get("total_income")), _f(r.get("agi")),
                 _f(r.get("taxable_income")), _f(r.get("tax_paid")),
                 _f(r.get("wages")), _f(r.get("primary_wages")),
                 _f(r.get("investment_income")), _f(r.get("capital_gain")),
                 _f(r.get("spouse_wages")), _f(r.get("ss_earnings")),
                 _f(r.get("medicare_earnings")), r.get("filing_status") or None,
                 _i(r.get("joint"), 0), r.get("source") or None,
                 r.get("note") or None))
            counts["income_annual"] = counts.get("income_annual", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "income_documents.csv"):
            if r.get("id") in (None, "") or r.get("year") in (None, ""):
                continue
            amounts = _j(r.get("amounts"))
            cur = conn.execute(
                # `ein` is DELIBERATELY not restored. Migration 077 nulled
                # that column and taxdocs now persists only ein_last4, but
                # every export taken BEFORE that — nightly backups, an older
                # /export or admin tenant-export ZIP — still carries the full
                # W-2 EIN in this CSV. Reading it back would silently undo
                # the removal on the next disaster recovery or self↔hosted
                # migration. The last four are restored instead, which is
                # what the live write path stores.
                """INSERT INTO income_documents (id, year, form, owner, payer,
                       ein_last4, primary_amount, amounts, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (_i(r["id"]), _i(r["year"]), r.get("form") or "",
                 r.get("owner") or "primary", r.get("payer") or None,
                 # prefer the column the live path writes; fall back to the
                 # last four OF a legacy plaintext value, never the value
                 _ein_last4_for_restore(r), _f(r.get("primary_amount")),
                 jsonb(amounts) if amounts is not None else None,
                 r.get("notes") or None))
            counts["income_documents"] = counts.get("income_documents", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "merchant_canonical.csv"):
            if not r.get("raw_merchant") or not r.get("canonical"):
                continue
            cur = conn.execute(
                """INSERT INTO merchant_canonical (raw_merchant, canonical,
                       method, as_of, merchant_id)
                   VALUES (%s,%s,%s, COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, raw_merchant) DO NOTHING""",
                # A NULL method is a frozen mapping: the layer1 recompute
                # only touches method='layer1' rows and the preservation
                # logic only recognises 'llm'/'manual', so a row with no
                # method is invisible to both forever. A hand-edited or
                # pre-method CSV defaults to 'manual' — the preservation-
                # safe choice, since manual rows are never recomputed over.
                (r["raw_merchant"], r["canonical"], r.get("method") or "manual",
                 _ts(r.get("as_of")),
                 # same screen as the transactions above: an alias whose
                 # merchant is not in the archive resolves by its canonical
                 # string on the next pass, which is what it did before 097
                 _known_merchant(r.get("merchant_id"), known_merchants)))
            counts["merchant_canonical"] = counts.get("merchant_canonical", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "merchant_renames.csv"):
            if not r.get("raw_merchant") or not r.get("to_canonical"):
                continue
            cur = conn.execute(
                """INSERT INTO merchant_renames (id, raw_merchant,
                       from_canonical, to_canonical, at)
                   VALUES (%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (_i(r["id"]), r["raw_merchant"],
                 r.get("from_canonical") or None, r["to_canonical"],
                 _ts(r.get("at"))))
            counts["merchant_renames"] = counts.get("merchant_renames", 0) + cur.rowcount
            skipped += 1 - cur.rowcount
        # The journal's id is an identity column and the rows above carried
        # their original ids, so advance the sequence past them — otherwise
        # the next rename after a restore collides on the PK and DO NOTHING
        # silently swallows the new journal row. The sequence is SHARED
        # across tenants while MAX(id) here sees only this tenant's rows
        # (RLS), so the sequence must only ever move FORWARD: anchoring on
        # its own last_value stops a restore into a low-id tenant from
        # dragging it backward under another tenant's ids, which would make
        # THAT tenant's next rename collide and vanish the same way.
        # (Reading last_value directly never needs nextval to have run,
        # and the migration grants the app role SELECT on the sequence.)
        conn.execute(
            # The floor reads the sequence by its literal name while the
            # target resolves it dynamically — the two must name the same
            # sequence, and migration 095 grants the app role SELECT and
            # UPDATE on exactly that name. Rename the sequence and both
            # halves have to move together.
            """SELECT setval(pg_get_serial_sequence('merchant_renames','id'),
                      GREATEST((SELECT last_value
                                  FROM merchant_renames_id_seq),
                               (SELECT COALESCE(MAX(id), 0)
                                  FROM merchant_renames), 1))""")

        for r in _rows(z, "merchant_categories.csv"):
            if not r.get("merchant") or not r.get("category_primary"):
                continue
            cur = conn.execute(
                """INSERT INTO merchant_categories (merchant, category_primary,
                       source, classified_at, disabled)
                   VALUES (%s,%s,%s, COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, merchant) DO NOTHING""",
                (r["merchant"], r["category_primary"], r.get("source") or "llm",
                 _ts(r.get("classified_at")), _b(r.get("disabled"))))
            counts["merchant_categories"] = counts.get("merchant_categories", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        # The merge offers and, above all, the DECISIONS on them. A
        # rejection is the household saying "these two are different
        # businesses"; the proposer's id is deterministic per pair, so the
        # rejection is the only thing keeping the pair from being offered
        # again. Left out of the archive, every pair the person has already
        # turned down comes back the first night after a restore.
        #
        # Both merchant ids must be merchants this archive carried — the
        # columns are NOT NULL, so unlike an alias there is no "resolve it
        # later" form of the row, and a proposal about a merchant that is
        # not here is about nothing. Merchant ids survive a restore (the
        # merchants member above carries `id`), so the pair is restored by
        # id rather than re-derived from names.
        for r in _rows(z, "merchant_merge_proposals.csv"):
            frm = _known_merchant(r.get("from_merchant_id"), known_merchants)
            into = _known_merchant(r.get("into_merchant_id"), known_merchants)
            if not r.get("id") or not frm or not into or frm == into:
                continue
            cur = conn.execute(
                """INSERT INTO merchant_merge_proposals (id, from_merchant_id,
                       into_merchant_id, evidence, status, created_at, decided_at)
                   VALUES (%s,%s,%s, COALESCE(%s,'{}'::jsonb), %s,
                           COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (r["id"], frm, into,
                 jsonb(_j(r.get("evidence")) or {}),
                 # a hand-edited status the app never writes would sit in
                 # the table unreadable by every query above; anything
                 # unrecognised is simply an undecided offer
                 (r.get("status") if r.get("status") in
                  ("pending", "approved", "rejected", "stale") else "pending"),
                 _ts(r.get("created_at")), _ts(r.get("decided_at"))))
            counts["merchant_merge_proposals"] = (
                counts.get("merchant_merge_proposals", 0) + cur.rowcount)
            skipped += 1 - cur.rowcount

        for r in _rows(z, "amazon_orders.csv"):
            if not r.get("dedup_key") or not r.get("date"):
                continue
            cur = conn.execute(
                """INSERT INTO amazon_orders (dedup_key, account, date, amount,
                       payee, seller, memo, category, category_source,
                       order_number, is_refund, payment_method, items_json,
                       inserted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, dedup_key) DO NOTHING""",
                (r["dedup_key"], r.get("account") or "", as_date(r["date"]),
                 _f(r.get("amount")) or 0.0, r.get("payee") or "Amazon",
                 r.get("seller") or "", r.get("memo") or "",
                 r.get("category") or "", r.get("category_source") or "rules",
                 r.get("order_number") or "", _i(r.get("is_refund"), 0),
                 r.get("payment_method") or "",
                 jsonb(_json_list(_j(r.get("items_json")))),
                 _ts(r.get("inserted_at"))))
            counts["amazon_orders"] = counts.get("amazon_orders", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "amazon_matches.csv"):
            if not _txn_exists(conn, r.get("transaction_id")):
                skipped += 1
                continue
            cur = conn.execute(
                """INSERT INTO amazon_matches (transaction_id, dedup_key,
                       matched_at)
                   VALUES (%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, transaction_id) DO NOTHING""",
                (r["transaction_id"], r.get("dedup_key") or None,
                 _ts(r.get("matched_at"))))
            counts["amazon_matches"] = counts.get("amazon_matches", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "costco_receipts.csv"):
            if not r.get("dedup_key") or not r.get("date"):
                continue
            cur = conn.execute(
                """INSERT INTO costco_receipts (dedup_key, account, date,
                       amount, receipt_type, warehouse, category,
                       category_source, summary, is_refund, payment_method,
                       items_json, inserted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, dedup_key) DO NOTHING""",
                (r["dedup_key"], r.get("account") or "main",
                 as_date(r["date"]), _f(r.get("amount")) or 0.0,
                 r.get("receipt_type") or "warehouse",
                 r.get("warehouse") or "", r.get("category") or "",
                 r.get("category_source") or "rules", r.get("summary") or "",
                 _i(r.get("is_refund"), 0), r.get("payment_method") or "",
                 jsonb(_json_list(_j(r.get("items_json")))),
                 _ts(r.get("inserted_at"))))
            counts["costco_receipts"] = counts.get("costco_receipts", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "costco_matches.csv"):
            if not _txn_exists(conn, r.get("transaction_id")):
                skipped += 1
                continue
            cur = conn.execute(
                """INSERT INTO costco_matches (transaction_id, dedup_key,
                       matched_at)
                   VALUES (%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, transaction_id) DO NOTHING""",
                (r["transaction_id"], r.get("dedup_key") or None,
                 _ts(r.get("matched_at"))))
            counts["costco_matches"] = counts.get("costco_matches", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        for r in _rows(z, "amazon_summaries.csv"):
            if not r.get("dedup_key") or not r.get("summary"):
                continue
            cur = conn.execute(
                """INSERT INTO amazon_summaries (dedup_key, summary, created_at)
                   VALUES (%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, dedup_key) DO NOTHING""",
                (r["dedup_key"], r["summary"], _ts(r.get("created_at"))))
            counts["amazon_summaries"] = counts.get("amazon_summaries", 0) + cur.rowcount
            skipped += 1 - cur.rowcount

        if isinstance(cfg_in, dict):
            # Belt to the netguard check above: a legit export never CARRIES
            # credential-shaped keys (the export scrubs them) — any present in
            # a ZIP are crafted. Drop them all so an injected key the
            # destination lacks (e.g. an access_url) can never land. Same for
            # policy keys (demo_mode /
            # demo_login / smtp_starttls) — a ZIP must not flip the demo
            # lockdown or downgrade SMTP to cleartext; deep-scrubbed so nested
            # shapes can't smuggle them either.
            cfg = scrub_config(cfg_in)
            notes.extend(normalize_notify_shapes(cfg))
            # scrub_config drops
            # secret/demo keys but NOT email_recipients — a crafted ZIP
            # could plant an outsider address that then receives the
            # household's cadence mail. Filter it to
            # tenant members on HOSTED, same boundary as the settings save;
            # a legit self-host export is untouched. This filter, not the
            # invite table, is what keeps a household's balances off an
            # outside inbox — see `_restore_recipient_invites`, which
            # carries each address's ANSWER across so the mail resumes for
            # the people who agreed to it and stays off for the ones who
            # declined.
            if cfg.get("email_recipients"):
                from ..web import mailguard
                kept = mailguard.filter_recipients(
                    conn, list(cfg["email_recipients"]))
                if kept:
                    cfg["email_recipients"] = kept
                else:
                    cfg.pop("email_recipients", None)
            # Merge OVER the destination's current config: keys present
            # only in the destination — settings added since the export was
            # taken — survive; the ZIP replaces exactly the keys it carries.
            # A flat secret is safe by absence (the export scrubs
            # plaid/mx/llm/smtp keys, crafted ones were dropped above), but a
            # secret nested inside a key the ZIP DOES carry — every AI
            # backend's api_key/extra_body under `llm_backends` — would be
            # merged away with the value it lives in, so it is carried
            # forward explicitly.
            # …and take the ROW LOCK first, for the same reason
            # `budget.config_txn` exists.
            # This is the settings lost-update shape written in raw SQL:
            # read the whole document, merge, write the whole document back.
            # `ON CONFLICT DO UPDATE` locks at WRITE time, which is too late
            # — a writer that commits between this read and that write (the
            # nightly budget seed is the live one) has its keys merged out
            # of the destination document we already read. The AST guard in
            # `test_settings_lost_update` never saw this site because it
            # matches `load_config`/`save_config` callers, and this predates
            # both. We are inside `conn.transaction()` (line ~209), so the
            # lock is held until the restore commits.
            cur = conn.execute(
                "SELECT config FROM tenant_settings FOR UPDATE").fetchone()
            dest = (cur["config"] if cur else {}) or {}
            # role routing may only name known roles and backends that
            # will exist after the merge (the ZIP's list if it brought one,
            # else the destination's own) — same rule as the .oikx bundle
            if "llm_roles" in cfg:
                from .connections_bundle import _clean_roles
                effective = (cfg["llm_backends"] if "llm_backends" in cfg
                             else dest.get("llm_backends") or [])
                ids = {str(b.get("id")) for b in effective
                       if isinstance(b, dict) and b.get("id")}
                cfg["llm_roles"] = _clean_roles(cfg["llm_roles"], ids)
            # a per-cadence SMS toggle only means something against a
            # number verified HERE; the ZIP never carries one, so unless the
            # destination already has a live verified number the toggle is
            # dropped rather than left armed for whatever number is verified
            # later. The owner re-ticks it after re-verifying.
            _drop_sms_toggles_without_verified_phone(cfg, dest)
            merged = dict(dest)
            for k, v in cfg.items():
                merged[k] = (carry_forward_secrets(dest[k], v)
                             if k in dest else v)
            conn.execute(
                """INSERT INTO tenant_settings (config) VALUES (%s)
                   ON CONFLICT (tenant_id) DO UPDATE
                   SET config = EXCLUDED.config, updated_at = now()""",
                (jsonb(merged),))
            counts["settings"] = 1
            # belt: this write bypasses budget.save_config — clear
            # the demoguard cache the same way so no stale flag survives
            from ..web import demoguard
            row = conn.execute(
                "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
            demoguard.invalidate(row["tid"] if row and row["tid"] else None)
    # Outside the transaction above, because this one table is written over
    # the admin connection (migration 074 grants the app role nothing on
    # it) — so it lands only once the household's data has actually
    # committed.
    try:
        inv = _restore_recipient_invites(conn, z)
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invites could not be restored")
        inv = dict(_NO_INVITES)
        notes.append("the daily email's recipients could not be restored — "
                     "check Settings → Email, anyone listed there may need "
                     "to be invited again")
    if inv["rows"]:
        counts["recipient_invites"] = inv["rows"]
    if inv["total"]:
        # Always said out loud, even when every answer carried. The failure
        # this replaced was invisible precisely because the summary only ever
        # mentioned successes, so the one number that matters — how many
        # people have to be asked again before their mail resumes — is
        # reported whether it is zero or not.
        bits = []
        if inv["accepted"]:
            bits.append(f"{inv['accepted']} keep the consent this instance "
                        f"can vouch for")
        if inv["declined"]:
            bits.append(f"{inv['declined']} had declined and stay off it")
        bits.append(f"{inv['pending']} must accept a fresh invite here "
                    f"before any mail reaches them")
        notes.append(f"daily email — the archive listed {inv['total']} "
                     f"recipient(s): " + "; ".join(bits)
                     + ". Settings → Email is where you invite them")

    # Counts above are ACTUAL inserts (rowcount), not rows
    # processed — a re-restore honestly reports 0, not the ZIP's size.
    # Rows the tenant already had surface separately.
    if skipped:
        counts["already_present"] = skipped
    # what the config check dropped, so the person restoring is told
    # rather than left to discover a missing AI backend later
    if notes:
        counts["_notes"] = notes                      # not a row count
    # rows from a pre-097 ZIP (no merchant_id) get their merchant now — the
    # ledger shows names and logos on the first load after a restore, not
    # after the next nightly
    try:
        from ..engine import merchant_identity
        merchant_identity.resolve(conn)
    except Exception:                                    # noqa: BLE001
        log.warning("post-restore merchant resolve skipped", exc_info=True)
    return counts
