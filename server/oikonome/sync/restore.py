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
costco_receipts/matches. Also the offers the household has already
answered — bill_proposals and merchant_merge_proposals — and the
import batches its file imports are tagged with, which is what keeps
those imports reversible on the destination.
Skipped: alerts history, sync logs, job heartbeats, staged uploads
(operational residue).
Every insert is ON CONFLICT on the original ids — re-restoring the same
ZIP is a no-op. A row already here is kept, with one exception: where the
destination holds a MACHINE row (a seed or model category rule, a derived
alias, a bill's pin, an offer still pending) and the archive holds the
PERSON's row for the same key, the person's wins. A merchant the
destination already holds under its own id for the same aggregator entity
is the archive's merchant, and the archive's pointers are mapped onto it.
Counts report rows WRITTEN, each one a row its insert returned (`_Outcomes.
land`); every archived row not written is counted once, under the reason
(REFUSALS: already present, orphaned, out of range, malformed, over cap,
merged, rebuilt), so a restore into a populated tenant can't claim it
imported the whole ZIP and a refusal can't pass for a duplicate.
An archive older than a migration that rewrote stored rows is brought up
to date by the steps `archive_upgrades` keys by migration name. The passes
apply them to every archive, whatever its `manifest.json` claims: a cut
of card numbers or an amount rule binds every row a restore writes, and
an archive is text anyone can edit. No migration file is ever run here.
"""

from __future__ import annotations

import contextvars
import csv
import datetime as _dt
import io
import json
import logging
import math
import re
import uuid
import zipfile
from collections import defaultdict

from . import account_sources as _account_sources
from . import base as _sync_base
from . import archive_upgrades as _upgrades
from . import card_number
from ..engine.entities import _hold_household_mark
from ..engine.compat import MAX_AMOUNT, as_date, strip_nul
from ..engine.compat import jsonb as _compat_jsonb
from ..engine import bills as _bills
from ..engine import budget as _budget

log = logging.getLogger(__name__)


# A restore member's DECLARED byte size (bounded by _check_zip_sizes) does
# NOT bound peak memory: materialized into dicts, a 128 MB CSV of millions
# of tiny rows (compressing to well under the 50 MB upload cap) balloons to
# multiple GB of live objects. Stream lazily from the ZIP (never read the
# whole member) and cap the row count. A personal-finance export is a few
# tens of thousands of rows; the cap is orders of magnitude above that.
MAX_ROWS_PER_MEMBER = 2_000_000


# The largest legitimate cell is a receipt image: base64 of a MAX_IMAGE
# upload, ~6.8 MB. Python's csv module refuses fields over 128 KB by
# default, so one photo receipt would fail the whole receipts member. The limit is process-wide, so it is
# raised once, here, to exactly what the biggest honest cell needs; the
# per-row checks below still refuse anything bigger than a live upload.
_MAX_CELL = 8 * 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), _MAX_CELL))


# Progress reporting for a restore running as a background job. A
# ContextVar rather than a threaded-through argument because _rows() is
# called from three dozen places and the alternative is editing every
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


# csv limits each field, never the number of fields, and a member's
# declared size can sit at MAX_MEMBER_BYTES while the ZIP is a few hundred
# KB on the wire: one line of commas deflates to almost nothing and becomes
# tens of millions of empty cells — gigabytes, in the web process every
# household shares — before the row cap has counted it as one row. So each
# member is read through _MemberLines below: the header under a budget no
# real header comes near and a column limit far above the widest table the
# export writes, every data RECORD (all its lines, since a quoted field may
# hold newlines) under a budget sized to the largest honest cell, and a
# record whose commas alone would make it absurdly wide is refused before
# csv builds a cell of it. Counting every comma, quoted ones included,
# over-counts only for a cell holding a list of a million values, which no
# export writes; what it bounds is the list csv would otherwise build.
MAX_HEADER_CHARS = 64 * 1024
MAX_COLUMNS = 200
MAX_RECORD_CHARS = _MAX_CELL + 1024 * 1024
MAX_RECORD_COMMAS = 1_000_000
# an unquoted comma or two in a hand-edited cell is the honest overflow
MAX_ROW_OVERFLOW = 50


def _not_an_export(name: str, why: str) -> ValueError:
    return ValueError(f"{name}: {why} — this doesn't look like a genuine "
                      "Oikonome export")


class _MemberLines:
    """The lines of one archive member, read under a character and comma
    budget per CSV record. csv pulls lines only while it assembles one
    record, and _MemberReader calls reset() before asking for each, so
    everything spent between two resets belongs to that record. Every
    read asks the text layer for at most what is left of the budget, so an
    oversized line is discovered without ever being held whole."""

    def __init__(self, fh, name: str):
        self.fh, self.name = fh, name
        self.reset(MAX_HEADER_CHARS)

    def reset(self, chars: int = MAX_RECORD_CHARS) -> None:
        self.budget = self.left = chars
        self.commas = MAX_RECORD_COMMAS

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self.fh.readline(self.left + 1)
        if not line:
            raise StopIteration
        self.left -= len(line)
        self.commas -= line.count(",")
        if self.left < 0:
            raise _not_an_export(
                self.name, "a record is longer than "
                f"{self.budget // 1024:,} KB")
        if self.commas < 0:
            raise _not_an_export(
                self.name, f"a record has more than {MAX_RECORD_COMMAS:,} "
                "separators")
        return line


class _MemberReader:
    """The csv.reader under a DictReader, refreshing the line budget before
    each record and refusing a header wider than MAX_COLUMNS or a row more
    than MAX_ROW_OVERFLOW cells past it — before it becomes a dict."""

    def __init__(self, it, lines: _MemberLines):
        self.it, self.lines = it, lines
        self.width: int | None = None

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.it)
        if self.width is None:
            if len(row) > MAX_COLUMNS:
                raise _not_an_export(
                    self.lines.name, f"the header has {len(row):,} columns "
                    f"(max {MAX_COLUMNS})")
            self.width = len(row)
        elif len(row) > self.width + MAX_ROW_OVERFLOW:
            raise _not_an_export(
                self.lines.name, f"line {self.it.line_num:,} has "
                f"{len(row):,} cells under a {self.width}-column header")
        self.lines.reset()              # a fresh budget for the next record
        return row

    @property
    def line_num(self):
        return self.it.line_num

    @property
    def dialect(self):
        return self.it.dialect


def _rows(z: zipfile.ZipFile, name: str):
    """Yield CSV rows one at a time — O(row) memory, hard row cap.

    Every cell comes back with the export's spreadsheet-formula quote
    removed. The export writes EVERY text cell through the same guard, so
    undoing it for a few named columns left the apostrophe on everything
    else — a note, a receipt line, a memo that began with a minus sign came
    back with a quote in front of it after every round trip."""
    try:
        fh = z.open(name)
    except KeyError:
        return
    moved_txns = _MOVED_TXNS.get()
    id_cells = (("id", "pending_transaction_id") + _TXN_ID_CELLS
                if name == "transactions.csv" else _TXN_ID_CELLS)
    with fh:
        # newline="": csv does its own line handling, and a universal-
        # newline read would turn a CR inside a quoted cell into a line feed
        lines = _MemberLines(io.TextIOWrapper(fh, encoding="utf-8",
                                              newline=""), name)
        reader = csv.DictReader(lines)
        reader.reader = _MemberReader(reader.reader, lines)
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
                # U+0000 cannot be stored in text and means nothing in
                # any cell; a jsonb cell's escaped one is dropped on write
                out = {strip_nul(k): _unescape(strip_nul(v))
                       for k, v in row.items()}
                if moved_txns:
                    for cell in id_cells:
                        v = out.get(cell)
                        if v and v in moved_txns:
                            out[cell] = moved_txns[v]
                yield out
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
    except (ValueError, RecursionError):
        # a cell the reader cannot walk is malformed, never fatal
        return None


def jsonb(v):
    """Every jsonb value this module writes, with non-finite numbers read
    as null. The ONE pass for the class: PostgreSQL refuses NaN and
    Infinity in jsonb, the restore is a single transaction, and one such
    token in any column an archive fills would cost the household every
    other row of the export. A cleaner that judges a number (a goal's
    target, a frozen budget) sees the NaN and refuses its entry first;
    everything no cleaner reads is nulled here, at the write."""
    return _compat_jsonb(_finite_only(v))


def _finite_only(v):
    """`v` with every non-finite number in it read as null. Python's JSON
    reader takes NaN and 1e999 and PostgreSQL's jsonb refuses them (see
    jsonb above); the cleaners have already judged the amounts they
    read. A value nested past what the walk can reach is malformed as a
    whole and reads as null: one cell, never the whole archive. Every
    caller (the jsonb writes and the snapshot readers) gets that here."""
    try:
        return _finite_walk(v)
    except RecursionError:
        return None


def _finite_walk(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite_walk(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite_walk(x) for x in v]
    return v


def _f(v):
    """CSV cell → float or None ('' travels for NULL).

    float() happily reads 'nan', 'inf' and '1e999'. Every other ingest door
    refuses them, and they must not slip in here: a non-finite value in a
    money column makes every JSON reply that carries the row fail
    (allow_nan=False) and every total over it NaN or infinite. No genuine
    export writes one, so the archive is refused — the restore is a single
    transaction, so nothing lands."""
    if v in (None, ""):
        return None
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"a number in this archive is not finite ({str(v)[:20]!r}) "
                         "— this doesn't look like a genuine Oikonome export")
    return f


class _AmountOutOfRange(ValueError):
    """One row's amount is larger than any real sum (see `_money`)."""


def _money(v):
    """A money cell: `_f`, and no larger than any real sum. 1e308 is
    finite and survives _f, but every cents conversion of it overflows —
    the matchers then fail the household's hourly and nightly passes on
    every run. Older import doors accepted such values, so a genuine
    archive can hold one: it raises _AmountOutOfRange, and the reader
    skips that one row and counts it (see `_out_of_range_note`) rather
    than refusing the whole archive."""
    f = _f(v)
    if f is not None and abs(f) > MAX_AMOUNT:
        raise _AmountOutOfRange(str(v)[:20])
    return f


def _balance(v):
    """An account balance cell: `_money`, except that one beyond any real
    sum is left empty rather than the row skipped. The account has to land,
    or every transaction and bill on it goes too; the next sync or a hand
    edit fills the balance in again."""
    try:
        return _money(v)
    except _AmountOutOfRange:
        log.warning("restore: an account balance beyond any real sum "
                    "(%s) was left empty", str(v)[:20])
        return None


def _out_of_range_note(n: int) -> str:
    return (f"{n} row{'s' if n != 1 else ''} with an amount larger than "
            f"any real sum (over {MAX_AMOUNT:,.0f}) "
            f"{'were' if n != 1 else 'was'} left out")


def _i(v, default=None):
    """CSV cell → int (accepts '1.0'-style floats) or default. A non-finite
    cell refuses the archive the way _f does ('inf' would otherwise escape
    as OverflowError, which the restore door does not report as a bad file)."""
    return int(_f(v)) if v not in (None, "") else default


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
# plaid_enrich_used is this instance's own running count of the Enrich rows
# Plaid billed this month — the one thing holding a household to its cap.
# It describes what the instance spent, not what the household owns, so the
# destination's count always wins: an old export would otherwise hand back a
# spent month, and a hand-edited negative count would lift the cap entirely,
# billed to whoever owns the Plaid keys.
# outbound_paused is the record a credential rotation writes: the mail
# relay, recipients and AI endpoint stand down until the owner presses
# Resume (notify/outbound_pause). It is this instance's decision about
# this instance's settings, never data: an archive that carried it could
# lift a pause by merging a record that names nothing (the Resume door
# asks for a step-up; a restore must not be the way round it), or plant a
# stale pause from the instance it was taken on.
# The merchant city list is an operator's knob, set in the stored settings
# where the operator works: the settings door neither shows nor takes it,
# and the bounds it used to apply went with it, so an archive (hand-edited,
# or another household's) must not be the way to set it either — a long
# list is expanded into variants and run against every merchant name in
# the nightly pass. The list of cities the pass has seen is its own
# bookkeeping, rewritten by the pass.
_POLICY_KEYS = ("demo_mode", "demo_login", "smtp_starttls",
                "taxdocs_allow_remote_llm", "notify_phone",
                "notify_phone_pending", "plaid_enrich_used",
                "outbound_paused", "merchant_strip_cities",
                "merchant_cities_seen")

# Settings that describe a CREDENTIAL rather than the household: which
# aggregator environment a set of keys belongs to, and the user those keys
# created at the aggregator. An archive never carries the keys themselves
# (see _SECRET_SUBSTR), so a restore that let these in would pair the
# destination's own keys with the source's environment and user — MX calls
# made with this instance's keys against another client's user, or
# production Plaid keys sent to the sandbox host. They stay with the keys
# they describe: the destination's own value, or none.
# The `<provider>_keys_saved_at` stamps are the same class: they date when
# THIS instance's keys were saved, and the bank-link hold's banner shows
# them so the owner can tell whether the held keys predate the pause. An
# archive's stamp would date keys it never carried — held keys would read
# as saved at a time they were not.
_CREDENTIAL_COMPANION_KEYS = ("plaid_env", "mx_env", "mx_user_guid",
                              "plaid_keys_saved_at", "mx_keys_saved_at")


# What makes up the relay and the AI endpoint: a restore or bundle that
# leaves all of these as they were brought no relay or endpoint in, so it
# has nothing new to pause — whatever is there is the destination's own,
# set (and, if it was saved after the pause, confirmed) by the owner here.
_RELAY_KEYS = ("smtp_host", "smtp_user", "smtp_from")
_LLM_KEYS = ("llm_url", "llm_roles", "llm_backends")


def _owner_addresses(conn) -> set[str]:
    """The current tenant's owner addresses, lowercased. The owner is
    mailed by construction, so their own address is never a recipient a
    pause holds back."""
    if conn is None:
        return set()
    row = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    tid = row["tid"] if row else None
    if not tid:
        return set()
    from ..web import mailguard
    return mailguard.owner_addresses(tid)


def widen_armed_pause(before: dict, after: dict, conn=None) -> None:
    """While a credential rotation's pause is armed, whatever outbound
    setting an import brings in is paused with the rest.

    The pause names what was set when the credential rotated; a restore or
    a connections bundle applied during it can bring a mail relay, extra
    recipients or an AI endpoint the record never named, and those would
    send at once while the settings the owner has yet to confirm stay
    held. So the record is widened (never narrowed, as a second rotation
    widens it) by what the import CHANGED: the relay when `after`'s relay
    differs from `before`'s, the AI endpoint likewise, and every recipient
    `before` did not already list — the ones already there were the
    owner's own or confirmed since. A relay the owner saved here after the
    pause is theirs, so an import that leaves it as it was must not stand
    it down. The owner's own address is never held (`conn`, the tenant
    connection, is how it is found). The record's time and reason are
    kept, so the banner still names the rotation the owner has to answer.
    Mutates `after`; nothing when no pause is armed in `before`."""
    from ..notify import outbound_pause
    prior = outbound_pause.paused(before)
    if prior is None:
        return
    now = outbound_pause._what_is_set(after, "")
    was = outbound_pause._what_is_set(before, "")
    added = set(now["recipients"]) - set(was["recipients"])
    if added:
        added -= _owner_addresses(conn)
    relay_changed = any(before.get(k) != after.get(k) for k in _RELAY_KEYS)
    llm_changed = any(before.get(k) != after.get(k) for k in _LLM_KEYS)
    after[outbound_pause.KEY] = {
        **(before.get(outbound_pause.KEY) or {}),
        "at": prior["at"], "reason": prior["reason"],
        "smtp": prior["smtp"] or (now["smtp"] and relay_changed),
        "recipients": sorted(set(prior["recipients"]) | added),
        "llm": prior["llm"] or (now["llm"] and llm_changed)}


# Who started this restore. A restore runs for minutes and applies the
# settings LAST, long after the step-up that let it start; a credential
# rotation in between ends the starter's session. If the session (or
# device token) that started the restore is gone by the settings step,
# whoever started it is no longer signed in, and what the archive brings in
# for the mail relay, recipients or AI endpoint must not start sending: a
# rotation with nothing outbound set leaves no pause record for
# widen_armed_pause to widen, so this is the only place that can tell.
# It must be the STARTING credential, not the household's sessions: a
# password change keeps the changer's own session and leaves every other
# member's alone, so "some session survives" is true through exactly the
# rotation this exists for. The value is ("cred", fingerprint) whenever the
# restore was started by a request that carried a credential, or
# ("household", {session ids}) only when it was started outside one (a
# command-line restore: every session gone still means the starter is
# gone; a survivor proves less). A starter whose credential is already gone
# when the restore begins stops it outright (StarterSignedOut) — never a
# fall back to the household's sessions, whose survivor is the very
# session a rotation keeps.
_START_SESSIONS: contextvars.ContextVar = contextvars.ContextVar(
    "restore_start_sessions", default=None)


def _tenant_of(conn) -> str | None:
    row = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    return (row["tid"] if row else None) or None


def _live_sessions(conn) -> frozenset:
    """Ids of the current tenant's unexpired sessions, read on the control
    connection (sessions are control-plane). Empty when the tenant is not
    pinned or the read fails, which disarms the check rather than pausing
    a restore nobody signed in started (a CLI or test restore)."""
    try:
        tid = _tenant_of(conn)
        if not tid:
            return frozenset()
        from ..db import tenancy
        cc = tenancy.control_connect()
        try:
            return frozenset(str(r["id"]) for r in cc.execute(
                "SELECT id FROM sessions WHERE tenant_id=%s "
                "AND expires_at > now()", (uuid.UUID(str(tid)),)).fetchall())
        finally:
            cc.close()
    except Exception:                                    # noqa: BLE001
        log.warning("restore: could not read the household's sessions",
                    exc_info=True)
        return frozenset()


def _live_credentials(conn, fingerprint: str) -> frozenset:
    """Token hashes of this tenant's unexpired sessions and device tokens
    whose hash begins with `fingerprint` (the request's credential
    fingerprint is that prefix — see web.security.credential_fingerprint).
    Empty when nothing matches or the read fails.

    A rotation DELETEs sessions but only marks device tokens revoked (the
    row stays for the audit trail, its expiry untouched), so a device
    token counts only while unrevoked — the same rule its resolver
    applies — or a restore started from the phone would never be paused."""
    try:
        tid = _tenant_of(conn)
        if not tid or not fingerprint:
            return frozenset()
        from ..db import tenancy
        cc = tenancy.control_connect()
        try:
            return frozenset(r["h"] for r in cc.execute(
                "SELECT token_hash AS h FROM sessions WHERE tenant_id=%s"
                " AND expires_at > now() AND left(token_hash, %s) = %s"
                " UNION ALL "
                "SELECT token_hash FROM device_tokens WHERE tenant_id=%s"
                " AND revoked_at IS NULL"
                " AND expires_at > now() AND left(token_hash, %s) = %s",
                (uuid.UUID(str(tid)), len(fingerprint), fingerprint,
                 uuid.UUID(str(tid)), len(fingerprint), fingerprint),
            ).fetchall())
        finally:
            cc.close()
    except Exception:                                    # noqa: BLE001
        log.warning("restore: could not read the starter's credential",
                    exc_info=True)
        return frozenset()


class StarterSignedOut(ValueError):
    """The credential that started this restore was gone before the restore
    began. A ValueError, so the job reports the sentence to the person."""

    def __init__(self):
        super().__init__(
            "the restore did not start: you were signed out after uploading "
            "(a password, login email or second-factor change ends other "
            "sessions), so nothing was applied — sign in and upload the file "
            "again")


def _starter_watch(conn, starter: str | None):
    """What pause_if_starter_signed_out watches for this restore (see
    _START_SESSIONS). `starter` is the starting request's credential
    fingerprint; None reads it from the request in flight.

    A starter whose credential no longer resolves raises StarterSignedOut
    before a row is written. The restore runs on a worker thread that
    first waits for its connections and the household's sync lock, and a
    rotation landing in that wait ends the starter's session while keeping
    the rotating owner's own; looking past the starter to "any live
    session" would read that survivor as the starter still being here."""
    if starter is None:
        try:
            from ..web import security
            starter = security.current_credential()
        except Exception:                                # noqa: BLE001
            starter = ""
    if starter:
        if not _live_credentials(conn, starter):
            raise StarterSignedOut()
        return ("cred", starter)
    live = _live_sessions(conn)
    return ("household", live) if live else None


def pause_if_starter_signed_out(before: dict, after: dict, conn) -> None:
    """Arm an outbound pause over what this restore brought in when every
    session that started it has since gone (see _START_SESSIONS). Only
    what the restore CHANGED is held, exactly as widen_armed_pause holds it
    during an armed pause; the owner's own address is never held. Nothing
    when a pause is already armed (widen_armed_pause covers it), when the
    restore started with no session to watch, or when one survives."""
    from ..notify import outbound_pause
    watch = _START_SESSIONS.get()
    if not watch or outbound_pause.paused(before) is not None:
        return
    kind, what = watch
    if kind == "cred":
        if _live_credentials(conn, what):
            return
    elif what & _live_sessions(conn):
        return
    now = outbound_pause._what_is_set(after, "")
    was = outbound_pause._what_is_set(before, "")
    added = set(now["recipients"]) - set(was["recipients"])
    if added:
        added -= _owner_addresses(conn)
    smtp = bool(now["smtp"]) and any(
        before.get(k) != after.get(k) for k in _RELAY_KEYS)
    llm = bool(now["llm"]) and any(
        before.get(k) != after.get(k) for k in _LLM_KEYS)
    if not (smtp or added or llm):
        return
    after[outbound_pause.KEY] = {
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "reason": outbound_pause.ROTATION,
        "smtp": smtp, "recipients": sorted(added), "llm": llm}


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


# Config keys whose value is an endpoint URL. A URL can carry a credential
# of its own (a password in the userinfo, a key in the query string), which
# no key-name rule can see; Settings masks those parts on every display
# (llm_categorize.redact_url) and the scrub masks them the same way, so an
# export never carries more than Settings would show a viewer.
_URL_KEYS = ("llm_url", "url")


def scrub_config(cfg: dict) -> dict:
    """Shared export/restore config scrub: drop credential-shaped and
    policy keys, recursing into nested dicts AND lists so neither a nested
    {"password":...} (demo_login's shape) nor a list of dicts each holding
    an api_key (llm_backends' shape) can ever ride along under a key the
    substring match misses. Endpoint URLs keep their host and path, with
    any credential inside them masked."""
    out = {}
    for k, v in cfg.items():
        if _secret_key(k) or k in _POLICY_KEYS:
            continue
        if k in _URL_KEYS and isinstance(v, str):
            from ..engine.llm_categorize import redact_url
            out[k] = redact_url(v)
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


def _settle_masked_urls(cfg: dict, dest: dict) -> None:
    """Never save a masked endpoint URL as a live one.

    An export masks the credential inside every endpoint URL, so what comes
    back holds the mask in its place. When the destination's own URL is the
    one that mask was made from (a restore onto the instance that wrote the
    archive), that URL is kept, credential and all. Otherwise the masked
    value is dropped — the destination's own top-level URL stands, and a
    backend is left without one — because saving the literal mask would
    break the backend with nothing to say why."""
    from ..engine.llm_categorize import URL_SECRET_MASK, unredact_url

    def settle(sent, stored):
        if not isinstance(sent, str) or URL_SECRET_MASK not in sent:
            return sent
        try:
            return unredact_url(sent, [s for s in stored
                                       if isinstance(s, str)])
        except ValueError:
            return None

    if "llm_url" in cfg:
        kept = settle(cfg["llm_url"], [dest.get("llm_url")])
        if kept is None:
            cfg.pop("llm_url")
        else:
            cfg["llm_url"] = kept
    if isinstance(cfg.get("llm_backends"), list):
        prior = {d.get("id"): d for d in dest.get("llm_backends") or []
                 if isinstance(d, dict) and d.get("id") is not None}
        for b in cfg["llm_backends"]:
            if isinstance(b, dict) and "url" in b:
                mine = prior.get(b.get("id")) or {}
                kept = settle(b["url"], [mine.get("url")])
                b["url"] = "" if kept is None else kept


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
# deliberate: sync logs, job heartbeats and staged uploads are this
# instance's operating state, not the household's data, and restoring them
# would replay half-finished jobs into the destination. The alert log is
# read for its dismissed rows only (the mute is the household's own; the
# rest the next Today build rebuilds). The export's README names which files come
# back and which are kept for the person's records only, from this set.
# `bills` is read under a variable (older archives call it recurring.csv),
# so a source test that only scans literal `_rows(z, "…")` calls misses it.
RESTORED_MEMBERS: frozenset[str] = frozenset(['account_adoptions', 'alerts_log', 'account_links', 'account_sources', 'accounts', 'amazon_matches', 'amazon_orders', 'amazon_summaries', 'bill_proposals', 'bills', 'budget_snapshots', 'business_entity', 'business_flags', 'business_txn_class', 'compliance_obligation', 'costco_matches', 'costco_receipts', 'crypto_holdings', 'entity_membership', 'equity_movement', 'holdings', 'import_batches', 'income_annual', 'income_documents', 'items', 'liabilities', 'manual_categories', 'merchant_canonical', 'merchant_categories', 'merchant_merge_proposals', 'merchant_renames', 'merchants', 'mileage_log', 'networth_recorded', 'networth_snapshot', 'receipt_items', 'receipts', 'recipient_invites', 'recurring', 'reimburse_flags', 'reimbursements', 'tenant_settings', 'transaction_notes', 'transaction_splits', 'transactions', 'vendor_1099', 'activity_log'])


# A restored activity-log row is held to the size of one the app writes. The
# feed returns every row's target and detail on each read, and a CSV cell may
# be megabytes (_MAX_CELL is sized for a receipt photo), so one crafted row
# would be paid for on every read of the feed from then on. A real target is
# an id; a real detail is a before/after pair of short values. The row is
# the household's record and still lands — clipped, never refused.
ACTIVITY_TARGET_MAX = 200
ACTIVITY_DETAIL_MAX = 16 * 1024          # bytes of JSON
# An alert message is one sentence the app builds; a longer one is crafted.
ALERT_MESSAGE_MAX = 500


# A restored account-id lineage row is read on every file import into its
# account (sync/base FileImportIds), so a crafted archive gets a fixed
# number of them, far above any household's reconnect history, and an id
# no longer than an account id is ever sensibly written.
_MAX_ADOPTION_LINEAGE = 1000
_MAX_ACCOUNT_ID = 512


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
    would mean a migration between instances could never
    complete for a large export. Dropping is strictly safer than
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
    # The multi-backend list is where the AI endpoints actually live —
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


# A receipt's total and dates feed the waiting-receipt matcher, which does
# cents and calendar arithmetic on them for every waiting receipt at once.
# A total past the app-wide money ceiling, or a date off the calendar Python
# can represent, is no genuine receipt; the row still restores, without it.
_RECEIPT_TOTAL_MAX = 1e12
_RECEIPT_YEAR_MIN = 1900


def _receipt_parsed(v) -> dict:
    parsed = _raw_dict(v)
    if "total" in parsed:
        t = parsed["total"]
        try:
            ok = (not isinstance(t, bool)
                  and math.isfinite(float(t))
                  and abs(float(t)) <= _RECEIPT_TOTAL_MAX)
        except (TypeError, ValueError, OverflowError):
            ok = False
        if not ok and t is not None:
            parsed = {**parsed, "total": None}
    return parsed


def _receipt_optimized(r) -> tuple[bytes | None, str | None]:
    """The cleaned-up copy of a receipt photo from its archive row, or
    (None, None). Only an upload makes one, so a restore that dropped it
    would leave the receipt serving and re-reading the raw photo for good.
    Held to what the optimizer itself writes — a JPEG, under the upload's
    size cap — because the stored type is what the image door serves the
    bytes back as; anything else is dropped and the original stands in, as
    it does for a receipt that never had a copy."""
    import base64

    from ..engine import receipts as _receipts
    cell = r.get("image_optimized")
    if not cell or str(r.get("optimized_mime") or "").strip().lower() \
            != "image/jpeg":
        return None, None
    if len(cell) > _receipts.MAX_IMAGE * 4 // 3 + 4:
        return None, None
    try:
        data = base64.b64decode(cell, validate=True)
    except Exception:                                   # noqa: BLE001
        return None, None
    if not data or len(data) > _receipts.MAX_IMAGE \
            or not data.startswith(b"\xff\xd8\xff"):
        return None, None
    return data, "image/jpeg"


def _receipt_ts(v):
    """_ts, or None (the column's default stands in) for a timestamp no
    receipt could carry: before 1900 or after next year."""
    t = _ts(v)
    if t is None:
        return None
    if not _RECEIPT_YEAR_MIN <= t.year <= _dt.date.today().year + 1:
        return None
    return t


def _raw_dict(v) -> dict:
    """A `raw` column is read with `.get` and merged with `||` everywhere;
    a ZIP that carries a list, string or number there would land as-is
    and crash the first reader. The list-shaped keys one level down get the
    same treatment: a value that is not a list is dropped, and every reader
    already treats an absent key as "the source never said"."""
    if not isinstance(v, dict):
        return {}
    bad = [k for k in _ARRAY_RAW_KEYS if k in v and not isinstance(v[k], list)]
    return {k: x for k, x in v.items() if k not in bad} if bad else v


# A full account, loan or routing number never belongs in a plaintext
# `raw` column: raw leaves the instance verbatim through /export and the
# portability archive. Every ingest path keeps them out now (the MX account
# pull, the Plaid liabilities pull, the CSV and OFX importers), but an
# archive taken before those rules carries them, so a restore applies the
# same cuts on the way in. Migration 146 applied them once to rows already
# stored. Keep the three in step.
# the identifiers a Plaid object carries (sync/plaid.PLAID_RAW_IDENTIFIERS,
# the one list every write and the export cut), plus a routing number
from .plaid import PLAID_RAW_IDENTIFIERS as _PLAID_RAW_IDS
_ACCOUNT_RAW_IDS = ("routing_number",) + _PLAID_RAW_IDS
_LIABILITY_RAW_IDS = _PLAID_RAW_IDS


def scrub_account_raw(raw: dict) -> dict:
    """An account's raw without the full account and routing numbers (the
    last-4 mask is its own column)."""
    return {k: v for k, v in raw.items() if k not in _ACCOUNT_RAW_IDS}


def scrub_liability_raw(raw: dict) -> dict:
    """A liability's raw without the loan's own account number."""
    return {k: v for k, v in raw.items() if k not in _LIABILITY_RAW_IDS}


def _is_file_row(raw: dict) -> bool:
    """Whether a raw is a row a file importer stored: every value text,
    empty, or a list of text (a CSV line longer than its header, the batch
    tags). An aggregator's raw always carries a number, a flag or a nested
    object, and its keys are the aggregator's own field names — cutting
    flagged as identifiers. The notes the app itself keeps on a file row
    (that a bank feed set a row aside against it, in this format or the
    earlier one) are not the source's columns and do not decide it: a file
    row carrying one is cut like any other, as migration 146 cut it."""
    return all(v is None or isinstance(v, str)
               or (isinstance(v, list)
                   and all(isinstance(x, str) for x in v))
               for k, v in raw.items() if k not in _FILE_ROW_NOTES)


# the app's own notes on a file row (base.STOOD_IN_KEY and the copies it
# replaced), which are objects where the source's columns are text
_FILE_ROW_NOTES = (_sync_base.STOOD_IN_KEY, _sync_base._LEGACY_COPIES_KEY)


def scrub_transaction_raw(raw: dict) -> dict:
    """A transaction's raw with identifier-named source columns cut to
    their last 4, as the CSV importers write them (file-import rows only),
    the OFX statement account hint likewise, and the statement importer's
    kept line with every card or long number masked to its last 4."""
    from .csvimport import scrub_identifiers
    from .pdfimport import _mask_line
    out = (scrub_identifiers(raw) if _is_file_row(raw)
           else {k: v for k, v in raw.items() if k not in _PLAID_RAW_IDS})
    _sync_base.stood_in_from_legacy_copies(out)
    acct = out.get("acct")
    if (isinstance(acct, (str, int, float)) and not isinstance(acct, bool)
            and len(str(acct)) > 4):
        out["acct"] = str(acct)[-4:]
    # the statement importer's rule: a long digit run in the kept line
    # (an account or card number printed on the statement) keeps its last 4
    if isinstance(out.get("line"), str):
        out["line"] = _mask_line(out["line"])
    return out


def _json_list(v) -> list:
    """A column the schema declares a JSON array — the line items an order
    or a receipt carries. Anything else becomes an empty list, so the order
    still lands with its amount, date and category and only its items read
    as none. `x or []` is NOT this check: it substitutes on a falsy value
    and waves an object or a number straight through."""
    return v if isinstance(v, list) else []


def clean_excluded_accounts(cfg: dict) -> None:
    """Coerce the Accounts page's "excl" list in a restored config into the
    shape the toggle itself would have written, dropping what cannot be an
    account id and removing the key when nothing is left.

    Same class as `clean_bill_raw` on the field beside it: the settings blob
    goes in verbatim, and every money surface iterates this list. A `true`
    or a number under the key is TRUTHY, so the `value or []` idiom hands it
    straight to `list()` and the forecast, the calendar strip and every
    report raise instead of rendering — with no UI for the household to
    take the value back out."""
    if "excluded_accounts" not in cfg:
        return
    from ..engine.budget import excluded_account_ids
    ids = excluded_account_ids(cfg)
    if ids:
        cfg["excluded_accounts"] = ids
    else:
        cfg.pop("excluded_accounts", None)


def _amount_or_none(v):
    """A settings amount as the settings door accepts it: a real number
    (a bool is not one), finite, and no larger than any real sum; None for
    anything else. A numeric string passes, read by the parser the door
    uses, so a decimal-comma "12,50" restores as 12.5 — deleting every
    comma would bring it back as 1250."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        if isinstance(v, str):
            from .csvimport import parse_money_text
            if not v.strip():
                return None
            n = parse_money_text(v)
        else:
            n = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(n) or abs(n) > MAX_AMOUNT:
        return None
    return n


def clean_budget_amounts(cfg: dict) -> list[str]:
    """Bound and type-check the budget amounts in a restored config the way
    the settings door does, and say what was dropped.

    The settings blob goes in verbatim, and Today, Budget and the daily
    email compare and sum these values directly: a string, a bool or a
    non-finite number in one of them makes those pages answer 500, with no
    door the household can use to take it back out. The per-month pools
    are dropped when unusable (the instance then seeds them as for a new
    household); a bucket keeps its name and rules and only loses an amount
    that cannot be one."""
    notes: list[str] = []
    for k in ("food_monthly", "other_monthly"):
        if k in cfg:
            n = _amount_or_none(cfg[k])
            if n is None:
                cfg.pop(k)
                notes.append(f"the {k.split('_')[0]} budget was not a usable "
                             "amount and was left out")
            else:
                cfg[k] = max(0.0, n)
    if "budgeted_income_monthly" in cfg:
        n = _amount_or_none(cfg["budgeted_income_monthly"])
        if n is None or n <= 0:
            if n is None:
                notes.append("the budgeted monthly income was not a usable "
                             "amount and was left out")
            cfg.pop("budgeted_income_monthly")
        else:
            cfg["budgeted_income_monthly"] = n
    if "custom_buckets" in cfg:
        buckets = cfg["custom_buckets"]
        if not isinstance(buckets, list):
            cfg.pop("custom_buckets")
            notes.append("the budget categories were not a list and were "
                         "left out")
        else:
            bad = 0
            for e in buckets:
                if isinstance(e, dict) and "monthly" in e:
                    n = _amount_or_none(e["monthly"])
                    if n is None and e["monthly"] not in (None, ""):
                        bad += 1
                    e["monthly"] = max(0.0, n or 0.0)
            if bad:
                notes.append(f"{bad} budget categor"
                             f"{'ies' if bad != 1 else 'y'} had an amount "
                             "that was not usable and now budget $0")
    return notes


# The list caps the settings door applies; a restored list is cut to the
# same length, so an archive cannot hand the pages a list no save could.
_SCENARIO_CAP, _GOAL_CAP, _ASSET_CAP = 20, 30, 30


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _named(e, seen: set) -> str | None:
    """The entry's name as the settings door stores it, or None when the
    entry is not an object, has no name, or repeats one already kept."""
    if not isinstance(e, dict):
        return None
    name = str(e.get("name") or "").strip()[:60]
    if not name or name.lower() in seen:
        return None
    seen.add(name.lower())
    return name


def _stated_amount(e: dict, key: str):
    """An optional amount field: 0.0 when absent or blank, None when it is
    present but no usable amount (the entry is then dropped, as the door
    refuses it)."""
    v = e.get(key)
    if v is None or v == "":
        return 0.0
    return _amount_or_none(v)


def clean_income_scenarios(cfg: dict) -> list[str]:
    """The income scenarios of a restored config, kept to what the settings
    door accepts, with a note per drop.

    Today, the forecast and Settings read the active scenario's take-home
    straight into a multiplication: a string, NaN or a non-list here makes
    them answer 500 with no door left to take it back out. An entry the
    door would refuse (no name, a repeat, no take-home above zero, an
    unknown cadence) is dropped; the list is cut at the door's cap; the
    active name falls back to the first kept scenario. The legacy
    saving/not-saving pair is read into the same shape, so it is held to
    the same amount rule."""
    from ..engine.budget import CADENCE_MONTHLY
    notes: list[str] = []
    for k in ("income_biweekly_saving", "income_biweekly_not_saving"):
        if k in cfg and cfg[k] not in (None, ""):
            n = _amount_or_none(cfg[k])
            if n is None or n <= 0:
                cfg.pop(k)
                notes.append("a legacy income amount was not usable and "
                             "was left out")
            else:
                cfg[k] = round(n, 2)
    if "active_scenario" in cfg and not isinstance(
            cfg["active_scenario"], (str, type(None))):
        cfg.pop("active_scenario")
    if "income_scenarios" not in cfg:
        return notes
    scen_in = cfg["income_scenarios"]
    if not isinstance(scen_in, list):
        cfg.pop("income_scenarios")
        cfg.pop("active_scenario", None)
        notes.append("the income scenarios were not a list and were left out")
        return notes
    kept, seen = [], set()
    for e in scen_in[:_SCENARIO_CAP]:
        name = _named(e, seen)
        if name is None:
            continue
        take = _amount_or_none(e.get("take_home"))
        cadence = str(e.get("cadence") or "biweekly").strip().lower()
        if take is None or take <= 0 or cadence not in CADENCE_MONTHLY:
            seen.discard(name.lower())
            continue
        kept.append({**e, "name": name, "take_home": round(take, 2),
                     "cadence": cadence})
    dropped = len(scen_in) - len(kept)
    if dropped:
        notes.append(f"{_plural(dropped, 'income scenario', 'income scenarios')}"
                     " could not be used and were left out")
    if kept:
        cfg["income_scenarios"] = kept
        if cfg.get("active_scenario") not in {s["name"] for s in kept}:
            cfg["active_scenario"] = kept[0]["name"]
    else:
        cfg.pop("income_scenarios")
        cfg.pop("active_scenario", None)
    return notes


def clean_savings_goals(cfg: dict) -> list[str]:
    """The savings goals of a restored config, kept to what the settings
    door accepts, with a note per drop.

    Today, the forecast, the savings card and the email read target, plan
    and starting balance through float() and the match words as a list: a
    non-list, a NaN or a string there is a 500, and a bare string of match
    words would be read letter by letter and match nearly every transfer.
    A goal the door would refuse (no name, a repeat, an amount that is not
    one, match words that are not a list, a target date that is not a
    date) is dropped; the list is cut at the door's cap."""
    notes: list[str] = []
    if "savings_goals" not in cfg:
        return notes
    goals_in = cfg["savings_goals"]
    if not isinstance(goals_in, list):
        cfg.pop("savings_goals")
        cfg.pop("savings_goal_state", None)
        notes.append("the savings goals were not a list and were left out")
        return notes
    kept, seen = [], set()
    for g in goals_in[:_GOAL_CAP]:
        name = _named(g, seen)
        if name is None:
            continue
        target = _stated_amount(g, "target")
        plan = _stated_amount(g, "monthly_plan")
        start = _stated_amount(g, "start_balance")
        tokens = g.get("tokens") or []
        td = str(g.get("target_date") or "").strip()
        try:
            td = _dt.date.fromisoformat(td).isoformat() if td else None
            date_ok = True
        except ValueError:
            date_ok = False
        if (target is None or plan is None or start is None
                or not isinstance(tokens, list) or not date_ok):
            seen.discard(name.lower())
            continue
        goal = {**g, "name": name, "target": round(max(0.0, target), 2),
                "monthly_plan": round(max(0.0, plan), 2),
                "account_id": str(g.get("account_id") or "") or None,
                "tokens": [str(t).strip()[:60] for t in tokens[:8]
                           if str(t).strip()],
                "start_balance": round(start, 2)}
        goal.pop("mode", None)
        if str(g.get("mode") or "").strip().lower() == "sweep":
            goal["mode"] = "sweep"
        goal.pop("target_date", None)
        if td:
            goal["target_date"] = td
        kept.append(goal)
    dropped = len(goals_in) - len(kept)
    if dropped:
        notes.append(f"{_plural(dropped, 'savings goal', 'savings goals')}"
                     " could not be used and were left out")
    if kept:
        cfg["savings_goals"] = kept
    else:
        cfg.pop("savings_goals")
        cfg.pop("savings_goal_state", None)
    return notes


def clean_manual_assets(cfg: dict) -> list[str]:
    """The Net Worth page's hand-entered assets, held to the settings
    door's rules: a list of named objects whose value is a usable amount
    and whose as-of is a date, cut at the door's cap. A NaN or huge value
    would otherwise ride into the net-worth total."""
    notes: list[str] = []
    if "manual_assets" not in cfg:
        return notes
    assets_in = cfg["manual_assets"]
    if not isinstance(assets_in, list):
        cfg.pop("manual_assets")
        notes.append("the hand-entered assets were not a list and were left "
                     "out")
        return notes
    kept = []
    for a in assets_in[:_ASSET_CAP]:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()[:60]
        value = _stated_amount(a, "value")
        try:
            as_of = as_date(a["as_of"]) if a.get("as_of") else None
            date_ok = True
        except (TypeError, ValueError):
            date_ok = False
        if not name or value is None or not date_ok:
            continue
        # only what the settings door stores: any other key, of any size
        # or value (a NaN aborts the settings write), would ride in unread
        entry = {"name": name,
                 "kind": str(a.get("kind") or "other").strip()[:30],
                 "value": round(value, 2)}
        if as_of is not None:
            entry["as_of"] = as_of.isoformat()
        kept.append(entry)
    dropped = len(assets_in) - len(kept)
    if dropped:
        notes.append(f"{_plural(dropped, 'hand-entered asset', 'hand-entered assets')}"
                     " could not be used and were left out")
    if kept:
        cfg["manual_assets"] = kept
    else:
        cfg.pop("manual_assets")
    return notes


# ---- the shape of every other known settings key ---------------------------
#
# Each key below is written by its own door (bill save, the hint dismiss,
# the wizard step save, the retirement settings), which type-checks it. The
# restore merge bypasses every door, so the restored value is held here to
# the shape that door writes, once, before the merge: a list where a dict
# belongs lands verbatim otherwise, and every page that reads the key (and
# the door that could repair it) trips on it on every load. A coercer
# answers the cleaned value plus how many entries it dropped, or _DROP when
# the key as a whole is unusable. A key the table does not name passes as
# the archive carried it, past _finite_only.

_DROP = object()
# per-key entry cap: no door writes more entries than the household has
# bills, and an archive must not hand the pages a list no save could
_SHAPE_ENTRY_CAP = 2000
_WIZARD_STEPS = {
    "wizard_steps": ("appearance", "connect", "sync", "import", "bills",
                     "budgets", "email", "finish"),
    "biz_wizard_steps": ("what", "when", "accounts", "startup", "finish"),
    "ret_wizard_steps": ("age", "spend", "saving", "assumptions", "done"),
}


def _payee_map(entry):
    """A dict keyed by bill payee whose values pass `entry` (which answers
    the kept value or _DROP)."""
    def coerce(v):
        if not isinstance(v, dict):
            return _DROP, 0
        kept = {}
        for k, x in list(v.items())[:_SHAPE_ENTRY_CAP]:
            y = entry(x) if isinstance(k, str) and k.strip() else _DROP
            if y is not _DROP:
                kept[k] = y
        return kept, len(v) - len(kept)
    return coerce


def _occurrence_cap(x):
    # the bill door stores int(cap) >= 1; budget reads it as a slice bound
    if isinstance(x, bool) or not isinstance(x, (str, int)):
        return _DROP
    try:
        n = int(str(x).strip())
    except ValueError:
        return _DROP
    return n if n >= 1 else _DROP


def _payee_list(v):
    if not isinstance(v, list):
        return _DROP, 0
    kept = list(dict.fromkeys(x for x in v[:_SHAPE_ENTRY_CAP]
                              if isinstance(x, str) and x.strip()))
    return kept, len(v) - len(kept)


def _steps(names):
    def coerce(v):
        if not isinstance(v, dict):
            return _DROP, 0
        kept = {k: x for k, x in v.items()
                if k in names and x in ("done", "skipped")}
        return kept, len(v) - len(kept)
    return coerce


def _flag(v):
    return bool(v), 0


def _past_date(v):
    try:
        d = _dt.date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        d = None
    if d is None or d >= _dt.date.today():
        return _DROP, 0
    return d.isoformat(), 0


def _hint_dismissal(x):
    """A 💡-hint dismissal as the dismiss door writes it: an object whose
    amount, when present, is a finite number (bills.fit_matches_dismissal
    compares it)."""
    import math
    if not isinstance(x, dict):
        return _DROP
    a = x.get("amount")
    if a is None:
        return x
    try:
        if isinstance(a, bool) or not isinstance(a, (int, float)) \
                or not math.isfinite(a):
            return _DROP
    except OverflowError:                 # an int too large for a float
        return _DROP
    return x


_CONFIG_SHAPES = {
    "occurrence_caps": _payee_map(_occurrence_cap),
    "disabled_bills": _payee_list,
    "dismissed_hints": _payee_map(_hint_dismissal),
    "dismissed_health": _payee_map(lambda x: x if isinstance(x, dict)
                                   else _DROP),
    **{k: _steps(names) for k, names in _WIZARD_STEPS.items()},
    "wizard_done": _flag,
    "retirement_saving": _flag,
    "dynamic_variable_budget": _flag,
    "birthdate": _past_date,
}


def clean_config_shapes(cfg: dict) -> list[str]:
    """Every key of `_CONFIG_SHAPES` the restored config carries, held to
    the shape its door writes; one note naming what was dropped."""
    dropped_keys, dropped_entries = [], 0
    for key, coerce in _CONFIG_SHAPES.items():
        if key not in cfg:
            continue
        value, lost = coerce(cfg[key])
        if value is _DROP:
            cfg.pop(key)
            dropped_keys.append(key)
        else:
            cfg[key] = value
            dropped_entries += lost
    notes = []
    if dropped_keys or dropped_entries:
        what = ", ".join(k.replace("_", " ") for k in dropped_keys)
        parts = ([f"{what} left out"] if what else []) + (
            [_plural(dropped_entries, "entry", "entries") + " dropped"]
            if dropped_entries else [])
        notes.append("some settings were not in the shape this version "
                     "keeps them in: " + "; ".join(parts))
    return notes


# The comma-free name each comma id in the archive being restored takes,
# decided once per restore by _plan_comma_renames before any row lands.
# A ContextVar for the same reason as _PROGRESS: _account_id is called from
# every table's loop, and only restore_zip ever sets it.
_COMMA_RENAMES: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_comma_renames", default=None)


# Account ids this household moved away from — adopted onto a new link, or
# merged into the live account a link already held — and the account each
# now lives on. Read once per restore before any row lands, so restoring
# the same archive again files its rows under the account they moved to
# instead of re-creating the old id as an empty second copy of it.
_MOVED_ACCOUNTS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_moved_accounts", default=None)


def _plan_moved_accounts(conn) -> dict[str, str]:
    """old id -> the account it lives on now, following a chain of moves
    (restored, adopted, merged again) to the account that still exists."""
    rows = conn.execute(
        "SELECT old_account_id, account_id FROM account_adoptions"
        " ORDER BY adopted_at").fetchall()
    step = {r["old_account_id"]: r["account_id"] for r in rows
            if r["old_account_id"] and r["account_id"]
            and r["old_account_id"] != r["account_id"]}
    # A split re-key records the old id with its number cut
    # (split_lineage_id), so two numbered ids sharing their last four leave
    # one key. Which account each went to is then unknown here, and
    # filing both onto one would merge two accounts' rows: such a key is
    # not followed, and the archive's account lands and is re-keyed again
    # on its own (its rows already here stay where they are).
    targets: dict[str, set] = {}
    for r in rows:
        if "\u2026" in (r["old_account_id"] or ""):
            targets.setdefault(r["old_account_id"], set()).add(
                r["account_id"])
    for k, v in targets.items():
        if len(v) > 1:
            step.pop(k, None)     # _resolve_cut_lineage may still place it
    live = {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id = ANY(%s)",
        (list(step),)).fetchall()} if step else set()
    out: dict[str, str] = {}
    for old in step:
        if old in live:
            continue                 # the old id is an account again
        cur, hops = step[old], 0
        while cur in step and cur not in live and hops < 16:
            cur, hops = step[cur], hops + 1
        if cur != old:
            out[old] = cur
    return out


# Archived rows per numbered account read to place it under a cut lineage
# key that names several accounts: a few are enough to find them here.
_CUT_LINEAGE_PROBE = 2_000


def _ambiguous_key(aid: str, ambiguous: dict) -> str | None:
    """The cut lineage key (base.lineage_keys) of `aid` naming several
    accounts, if any."""
    return next((k for k in _sync_base.lineage_keys(aid) if k in ambiguous),
                None)


def _resolve_cut_lineage(conn, z, moved: dict) -> None:
    """Place each archived split account whose id spells a full number
    under a cut lineage key naming several accounts (two cards sharing
    their last four, both moved): onto the one account here holding its
    archived rows' ids, which hash the full id and so tell the cards
    apart. One no account, or several, hold stays unplaced, lands, and is
    re-keyed on its own, as _plan_moved_accounts leaves it — never filed
    onto the other card's account."""
    rows = conn.execute(
        "SELECT old_account_id, account_id FROM account_adoptions"
        " WHERE item_id = 'manual' AND strpos(old_account_id, %s) > 0",
        ("\u2026",)).fetchall()
    targets: dict[str, set] = {}
    for r in rows:
        targets.setdefault(r["old_account_id"], set()).add(r["account_id"])
    ambiguous = {k: v for k, v in targets.items() if len(v) > 1}
    if not ambiguous:
        return
    ids: dict[str, list] = {}
    for r in _rows(z, "transactions.csv"):
        tid, aid = r.get("id"), r.get("account_id")
        if not tid or not aid:
            continue
        aid = _comma_free(str(aid))
        if (aid in moved or not _sync_base.NUMBERED_MANUAL_ID_RE.search(aid)
                or _ambiguous_key(aid, ambiguous) is None):
            continue
        got = ids.setdefault(aid, [])
        if len(got) < _CUT_LINEAGE_PROBE:
            got.append((str(tid), r.get("date"), r.get("amount"),
                        r.get("name")))
    # rows re-id'd here under the household's secret answer to the keyed
    # form of the archived id (base.key_file_import_ids), and rows a re-key
    # re-hashed onto the account they moved to to the same line hashed
    # under that account (base.rehash_file_rows)
    secret = _sync_base.txn_id_secret(conn)
    for aid, got in ids.items():
        same: dict[tuple, int] = defaultdict(int)
        for tid, day, amount, name in got:
            same[_sync_base.file_row_shape(tid, day, amount, name)] += 1
        lines = [(tid, day, amount, name,
                  same[_sync_base.file_row_shape(tid, day, amount, name)])
                 for tid, day, amount, name in got]
        held = set()
        for target in sorted(ambiguous[_ambiguous_key(aid, ambiguous)]):
            tids = [t for t, _, _, _ in got]
            if secret is not None:
                tids += [_sync_base.keyed_file_id(secret, t) for t in tids]
            # each line hashed once, however often the archive repeats it
            tids += list(_sync_base.rehashed_file_ids(
                aid, target, lines).values())
            if conn.execute(
                    "SELECT 1 FROM transactions WHERE id = ANY(%s)"
                    "   AND account_id = %s LIMIT 1",
                    (tids, target)).fetchone():
                held.add(target)
        if len(held) == 1:
            moved[aid] = held.pop()


def _account_id(v):
    """An archived account id as it restores: comma-free, the same rename
    the database migration gave ids already stored (see
    adopt.stored_account_id), and then onto the account this household
    already moved it to, if any (see _MOVED_ACCOUNTS). The accounts table
    refuses a comma, and inside the one restore transaction a single
    refused row would abort the whole archive. Blank stays blank for the
    caller's `or None`."""
    if v is None or v == "":
        return v
    v = _comma_free(str(v))
    moved = _MOVED_ACCOUNTS.get()
    if not moved:
        return v
    if v not in moved and _sync_base.NUMBERED_MANUAL_ID_RE.search(v):
        # an id spelling a full number moved under its cut form, its
        # trailing group read as a door's suffix or as the number's own
        for key in _sync_base.lineage_keys(v):
            if key in moved:
                return moved[key]
        return v
    return moved.get(v, v)


# Where a transaction this household already moved now lives: archived id
# -> the live row's id. When a bank is linked after a restore, the
# reconnect guard moves each restored charge the backfill re-delivered onto
# the id the bank gave it, deleting the old id (adopt._rewrite_txn_id), and
# nothing records the old one. Restoring an archive that holds the old id
# again — the same archive, or an older one after a newer whose account
# lineage came with it — files the row onto the live account through
# _MOVED_ACCOUNTS, and an insert that recognises a row only by its id puts
# it back beside its twin: a month of charges counted twice, reported as
# imported. So before the rows land, each archived row on a moved account
# whose id is gone is paired with a live row on that account that the
# archive does not name (the carry copied the row whole, so the date and
# the amount agree and the names share a word), and _rows hands every
# member that names the old id the live one instead: the insert then meets
# the row it already holds, and the row's notes, splits and pins land on
# the live row rather than on nothing.
# The line an export's README carries when its cells were written by the
# lossless guard (pages._csv_cell: the head rule with its doubled
# apostrophe, then the piece rule). Read once per restore into _GUARD_V2,
# which _unescape consults for every cell; absent, the archive is an older
# one and gets the reverse it was written for.
GUARD_MARK = "Spreadsheet guard: v2"
_GUARD_V2: contextvars.ContextVar = contextvars.ContextVar(
    "restore_guard_v2", default=False)


def archive_guard_v2(z: zipfile.ZipFile) -> bool:
    """Was this archive written with the lossless spreadsheet guard?"""
    try:
        with z.open("README.txt") as fh:
            return GUARD_MARK.encode() in fh.read(256 * 1024)
    except KeyError:
        return False


_MOVED_TXNS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_moved_txns", default=None)
# the cells of each member that name a transaction by its id
_TXN_ID_CELLS = ("txn_id", "transaction_id", "expense_id", "reimburse_id")


def _txn_key(date, amount) -> tuple | None:
    try:
        day = (date if isinstance(date, _dt.date)
               else _dt.date.fromisoformat(str(date)[:10]))
        return (day, round(float(amount) * 100))
    except (TypeError, ValueError, OverflowError):
        return None


# What the pairing below may hold is sized by the DATABASE, never by the
# archive: a member may declare millions of rows while the ZIP is a few
# hundred KB on the wire (one repeated line deflates to almost nothing),
# and the restore runs inside the web process every household shares.
# So the live rows of the moved accounts — already held, and bounded by
# what the household holds — are what is indexed, and the archive is
# streamed past them, checked against the database a batch at a time.
_MOVED_BATCH = 5000
# More live charges than this on one account, one day, one amount is no
# real ledger; past it the extra ones are simply not offered as twins.
_TWINS_PER_KEY = 32
# An archive may carry more rows on a moved account than the household
# still holds there (charges deleted since), but not without limit: past
# this many, or a few times the live rows, it is not a real export.
_MOVED_ROWS_FLOOR = 100_000
_MOVED_ROWS_PER_LIVE = 4


def _moved_rows(z, moved: dict):
    """(archived id, live account, row) for each archived row on a moved
    account, streamed."""
    for r in _rows(z, "transactions.csv"):
        tid, aid = r.get("id"), r.get("account_id")
        if not tid or not aid:
            continue
        dest = moved.get(_comma_free(str(aid)))
        if dest:
            yield tid, dest, r


def _plan_moved_txns(conn, z) -> dict[str, str]:
    moved = _MOVED_ACCOUNTS.get()
    if not moved:
        return {}
    from .dedup import _tokens
    dests = sorted(set(moved.values()))
    # every live row on a moved account, by (account, date, cents); an
    # entry is [id, tokens], and a taken or disqualified one has id None
    pool: dict[tuple, list] = {}
    by_id: dict[str, list] = {}
    live = 0
    # The live ids on the moved-to accounts (bounded by the database), so
    # the first pass can mark the archive's own rows before the pool is
    # built: a row the archive still names is held, not a carried twin, and
    # must not take a place among the twins kept per key.
    live_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE account_id = ANY(%s)", (dests,))}
    live = len(live_ids)
    if not live:
        return {}
    # First pass: count the archive's rows on moved accounts and collect
    # the live rows the archive itself names. It has to finish before any
    # pairing, since a row may name a live id after an earlier row could
    # have paired with it.
    cap = max(_MOVED_ROWS_FLOOR, _MOVED_ROWS_PER_LIVE * live)
    n = 0
    named: set[str] = set()
    for tid, _dest, _r in _moved_rows(z, moved):
        n += 1
        if n > cap:
            raise ValueError(
                f"transactions.csv: too many rows on a moved account (limit "
                f"{cap:,}) — this doesn't look like a genuine Oikonome "
                "export")
        if tid in live_ids:
            named.add(tid)
    del live_ids
    for r in conn.execute(
            """SELECT id, account_id, date, amount, name, merchant_name
                 FROM transactions
                WHERE account_id = ANY(%s)
                ORDER BY id""", (dests,)):
        if r["id"] in named:
            continue
        key = _txn_key(r["date"], r["amount"])
        if key is None:
            continue
        twins = pool.setdefault((r["account_id"], key), [])
        if len(twins) < _TWINS_PER_KEY:
            entry = [r["id"], frozenset(_tokens(r["name"],
                                                r["merchant_name"]))]
            twins.append(entry)
            by_id[r["id"]] = entry
    if not by_id:
        return {}
    out: dict[str, str] = {}

    def _pair(batch: dict) -> None:
        held = {r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE id = ANY(%s)",
            (list(batch),)).fetchall()}
        for tid, (dest, key, toks) in batch.items():
            if tid in held:
                continue
            for live_id, live_toks in pool.get((dest, key), ()):
                if live_id is None:
                    continue
                if (toks & live_toks) or not (toks or live_toks):
                    out[tid] = live_id
                    entry = by_id[live_id]
                    entry[0] = None   # each live row stands for one charge
                    break

    # Second pass: pair a batch at a time, keeping only the id map. A row
    # whose key no live row shares cannot pair, so it is never held.
    batch: dict[str, tuple] = {}
    for tid, dest, r in _moved_rows(z, moved):
        if tid in out or tid in batch or tid in by_id:
            continue                  # paired, queued, or itself live
        key = _txn_key(r.get("date"), r.get("amount"))
        if key is None or (dest, key) not in pool:
            continue
        batch[tid] = (dest, key, frozenset(_tokens(
            _descriptor(r.get("name")),
            _descriptor(r.get("merchant_name")))))
        if len(batch) >= _MOVED_BATCH:
            _pair(batch)
            batch = {}
    if batch:
        _pair(batch)
    return out


def _plan_keyed_txns(conn, z) -> dict[str, str]:
    """Archived id -> the id this household holds the same row under,
    for each archived file row it holds only under another: its keyed
    form, or the id a re-key re-hashed it to.

    A row whose id hashed a full account number is re-id'd under the
    household's secret (base.key_file_import_ids), or re-hashed under the
    id its account moved to (base.rehash_file_rows), and an archive
    written before that names it by the old id: an insert that knows a
    row only by its id would put the line back beside itself, and its
    notes and pins would land on the copy. Both new forms are functions
    of the old id and the row's own fields, so this map is exact; a
    household with no secret has keyed none of its rows."""
    out = _plan_rehashed_txns(conn, z)
    if _sync_base.txn_id_secret(conn) is None:
        return out
    unkeyed = re.compile(_sync_base.UNKEYED_FILE_ID_PATTERN)
    batch: list[str] = []

    def _flush():
        held = _sync_base.held_forms(conn, batch)
        out.update({a: h for a, h in held.items() if h != a})
        batch.clear()

    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if not tid or tid in out or not unkeyed.match(tid):
            continue
        batch.append(tid)
        if len(batch) >= _MOVED_BATCH:
            _flush()
    if batch:
        _flush()
    return out


def _archived_numbered_formers(z) -> dict[str, list[str]]:
    """Archived account id -> every id spelling a full number its rows may
    have been hashed under: itself, when it spells one, and each id the
    archive's adoption records moved onto it in full, followed back
    through earlier moves."""
    back: dict[str, set] = defaultdict(set)
    for r in _rows(z, "account_adoptions.csv"):
        new, old = r.get("account_id"), r.get("old_account_id")
        if new and old and new != old:
            back[_comma_free(str(new))].add(str(old).strip())
    out: dict[str, list[str]] = {}
    for aid in {_comma_free(str(r["id"])) for r in _rows(z, "accounts.csv")
                if r.get("id")} | set(back):
        seen, todo, found = {aid}, [aid], []
        while todo and len(seen) < 32:
            cur = todo.pop(0)
            if _sync_base.NUMBERED_MANUAL_ID_RE.search(cur) \
                    and not _sync_base.SPLIT_ID_RE.search(cur):
                found.append(cur)
            for old in sorted(back.get(cur, ())):
                if old not in seen:
                    seen.add(old)
                    todo.append(old)
        if found:
            out[aid] = found
    return out


def _plan_rehashed_txns(conn, z) -> dict[str, str]:
    """Archived id -> the id a re-key here re-hashed the same row to, for
    each archived file row hashed under a hand-made id that spelled a full
    number which this household has since moved off (base.
    rekey_numbered_accounts): the same line hashed under the account the
    row lands on, or an OFX FITID row renamed onto it, proved from the
    archived row's own fields (base.rehashed_file_id) and taken only when
    held there. A restore of an older archive into a household that
    already moved then meets every row it holds; one it does not hold
    lands under its archived id and is re-hashed with the rest after."""
    formers = _archived_numbered_formers(z)
    if not formers:
        return {}
    got: list[tuple] = []
    same: dict[tuple, int] = defaultdict(int)
    for r in _rows(z, "transactions.csv"):
        tid, aid = r.get("id"), r.get("account_id")
        if not tid or not aid:
            continue
        aid = _comma_free(str(aid))
        if aid not in formers:
            continue
        got.append((str(tid), aid, r.get("date"), r.get("amount"),
                    r.get("name")))
        same[(aid,) + _sync_base.file_row_shape(
            str(tid), r.get("date"), r.get("amount"), r.get("name"))] += 1
    secret = _sync_base.txn_id_secret(conn)
    out: dict[str, str] = {}
    want: dict[str, tuple[str, str]] = {}

    def _flush():
        by_target: dict[str, list[str]] = defaultdict(list)
        for new, target in want.values():
            by_target[target].append(new)
        for target, news in by_target.items():
            held = _sync_base.held_forms(conn, news, target)
            for tid, (new, t) in want.items():
                if t == target and new in held:
                    out[tid] = held[new]
        want.clear()

    # one pass per archived account and former id, each line hashed once
    # however often the archive repeats it (base.rehashed_file_ids); a row
    # the first former proves is not asked of the next
    by_aid: dict[str, list] = defaultdict(list)
    for tid, aid, day, amount, name in got:
        by_aid[aid].append((tid, day, amount, name, same[
            (aid,) + _sync_base.file_row_shape(tid, day, amount, name)]))
    for aid in sorted(by_aid):
        target = _account_id(aid)
        if not target:
            continue
        rest = by_aid[aid]
        for former in formers[aid]:
            if not rest:
                break
            proved = _sync_base.rehashed_file_ids(former, target, rest,
                                                  secret)
            for tid, new in proved.items():
                if new != tid:
                    want[tid] = (new, target)
                if len(want) >= _MOVED_BATCH:
                    _flush()
            rest = [x for x in rest if x[0] not in proved]
    if want:
        _flush()
    return out


def _plan_foreign_keyed_txns(conn, z, carried: dict) -> dict[str, str]:
    """Archived id -> the id it lands under here, for each archived file
    row under a keyed id (base.keyed_file_id) this household does not hold.

    Such an id was keyed under the secret of the household the archive
    came from, which never travels, so nothing here can recompute it: a
    re-import would find the row only by content (FileImportIds.
    _adopt_by_content), and every door that looks a row up by the id an
    earlier reading gave it would miss it. So the row lands under the id
    an import here would key it by: the plain id an import mints for that
    line on the account it lands on (base.file_row_id — its date, cents,
    masked name, and its place among identical lines of the archive),
    keyed under this household's secret, minted for it if there is none.
    Where this household already holds that line (plain, or keyed by
    itself), the archived row is that row, as an archive under plain ids
    meets it, and its notes and pins land there. Where it holds the line
    under an id nothing here recomputes — keyed by itself from an id the
    archive cannot spell (the pre-masking name, the old numbered account),
    or minted over a name the ledger stores cut or without its NULs — the
    archived row takes an unclaimed row of the same importer, account and
    line (base.rows_by_line_shape), as a re-import would. Each row here
    stands for one archived line, and a row the archive itself holds is
    never another line's. A row whose account, date or amount cannot be
    read keeps its archived id."""
    keyed_re = re.compile(_sync_base.KEYED_FILE_ID_PATTERN)
    out: dict[str, str] = {}
    # every row here the archive names is that archived line's own
    claimed = set(carried.values())
    named: list[str] = []
    foreign = False
    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if _sync_base.is_file_import_id(tid):
            named.append(tid)
            foreign = foreign or bool(keyed_re.match(tid))
    if not foreign:
        return out
    claimed |= _sync_base._held_ids(conn, named)
    del named
    places: dict[tuple, int] = {}
    secret: bytes | None = None
    batch: list[tuple] = []

    def _flush():
        nonlocal secret
        held = _sync_base._held_ids(conn, [b[0] for b in batch])
        dates: dict[tuple, set] = defaultdict(set)
        for tid, prefix, aid, shape, _amount, _name in batch:
            if tid not in held:
                dates[(prefix, aid)].add(shape[0])
        pools: dict[tuple, dict] = {}

        def _same_line(prefix, aid, shape):
            if (prefix, aid) not in pools:
                pools[(prefix, aid)] = _sync_base.rows_by_line_shape(
                    conn, prefix, aid, dates[(prefix, aid)], plain=True,
                    skip=claimed)
            rows = pools[(prefix, aid)].get(shape) or []
            while rows and rows[0] in claimed:
                rows.pop(0)
            return rows.pop(0) if rows else None

        for tid, prefix, aid, shape, amount, name in batch:
            if tid in held or tid in out:
                continue
            key = (prefix, aid, shape)
            while True:
                n = places.get(key, 0) + 1
                places[key] = n
                plain = _sync_base.file_row_id(prefix, aid, shape[0], amount,
                                               name, n)
                have = _sync_base.held_forms(conn, [plain], aid).get(plain)
                if have is not None:
                    if have in claimed:
                        continue     # an earlier archived line took it
                    target = have
                else:
                    target = _same_line(prefix, aid, shape)
                    if target is None:
                        if secret is None:
                            secret = _sync_base.txn_id_secret(conn,
                                                              create=True)
                        target = _sync_base.keyed_file_id(secret, plain)
                        if target in claimed:
                            continue
                break
            claimed.add(target)
            out[tid] = target
        batch.clear()

    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if not tid or not keyed_re.match(tid):
            continue
        aid = _account_id(r.get("account_id"))
        shape = _sync_base.line_shape(r.get("date"), r.get("amount"),
                                      r.get("name"))
        if not aid or shape is None:
            continue
        batch.append((tid, tid.split(":", 1)[0], aid, shape,
                      float(r.get("amount")), r.get("name") or ""))
        if len(batch) >= _MOVED_BATCH:
            _flush()
    if batch:
        _flush()
    return out


def _plan_plain_by_content(conn, z, carried: dict) -> dict[str, str]:
    """Archived id -> a row of the same importer, account and line this
    household holds under an id nothing here recomputes, for each archived
    file row under a plain id it holds in no form and no exact map placed.

    The last pass, after every exact one. An archive names a line by the
    id an import minted for it, its name hashed masked; a household that
    imported the same line before names were masked holds it under the id
    hashed over the whole name, which the archive (storing the name cut)
    cannot spell. Without this the line lands beside itself and its notes
    and pins land on the copy. The archived row takes an unclaimed row of
    the same line (base.rows_by_line_shape), as a re-import would
    (FileImportIds._adopt_by_content): each row stands for one archived
    line, in id order, and a row the archive itself names, or an earlier
    pass placed a row on, is never taken."""
    unkeyed = re.compile(_sync_base.UNKEYED_FILE_ID_PATTERN)
    out: dict[str, str] = {}
    named: list[str] = []
    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if _sync_base.is_file_import_id(tid):
            named.append(tid)
    if not any(unkeyed.match(t) and t not in carried for t in named):
        return out
    claimed = set(carried.values()) | _sync_base._held_ids(conn, named)
    del named
    batch: list[tuple] = []

    def _flush():
        dates: dict[tuple, set] = defaultdict(set)
        for tid, prefix, aid, shape in batch:
            if tid not in claimed:
                dates[(prefix, aid)].add(shape[0])
        pools = {k: _sync_base.rows_by_line_shape(
                     conn, k[0], k[1], ds, plain=True, skip=claimed)
                 for k, ds in dates.items()}
        for tid, prefix, aid, shape in batch:
            if tid in claimed:
                continue                  # held under its own id
            rows = pools[(prefix, aid)].get(shape) or []
            while rows and rows[0] in claimed:
                rows.pop(0)
            if rows:
                target = rows.pop(0)
                claimed.add(target)
                out[tid] = target
        batch.clear()

    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if not tid or tid in carried or not unkeyed.match(tid):
            continue
        aid = _account_id(r.get("account_id"))
        shape = _sync_base.line_shape(r.get("date"), r.get("amount"),
                                      r.get("name"))
        if not aid or shape is None:
            continue
        batch.append((tid, tid.split(":", 1)[0], aid, shape))
        if len(batch) >= _MOVED_BATCH:
            _flush()
    if batch:
        _flush()
    return out


def _comma_free(v: str) -> str:
    if "," not in v:
        return v
    planned = _COMMA_RENAMES.get()
    if planned and v in planned:
        return planned[v]
    from .adopt import account_id_without_comma
    return account_id_without_comma(v)


def _plan_comma_renames(conn, z) -> dict[str, str]:
    """Where each comma id in the archive restores. The plain rename,
    unless the archive (or the household) already holds a different
    account under that name: then the id takes the migration's other
    spelling, adopt.account_id_when_taken — otherwise the two accounts
    would restore as one, the second row skipped as already present and
    its transactions filed under the first."""
    from .adopt import (account_id_when_taken, account_id_without_comma,
                        stored_account_id)
    ids = {str(r["id"]) for r in _rows(z, "accounts.csv") if r.get("id")}
    claimed = {i for i in ids if "," not in i}
    out: dict[str, str] = {}
    for aid in sorted(i for i in ids if "," in i):
        new = (account_id_when_taken(aid)
               if account_id_without_comma(aid) in claimed
               else stored_account_id(conn, aid))
        claimed.add(new)
        out[aid] = new
    return out


def _rename_account_keys(cfg: dict, renamed: dict[str, str]) -> None:
    """Carry the comma renames into a restored settings document, in the
    keys that hold an account id — otherwise an excluded or pinned account
    silently stops being excluded or pinned."""
    if not renamed:
        return
    from .adopt import _rewrite_account_keys
    for old, new in renamed.items():
        _rewrite_account_keys(cfg, old, new)


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
                    # a parsed JSON number can be Infinity (int() raises
                    # OverflowError) or a 400-digit integer (float() does),
                    # and either must drop the field like any other bad
                    # value rather than abort the whole restore — so every
                    # cleaner here also catches OverflowError
                    try:
                        ints.append(int(x))
                    except (TypeError, ValueError, OverflowError):
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
            except (TypeError, ValueError, OverflowError):
                rec.pop("interval", None)
        if "frequency" in rec and not isinstance(rec["frequency"], str):
            rec.pop("frequency", None)
        # 240 is the count cap, not the length cap: a YEARLY:240 bill rolls
        # its due date centuries out, the nightly skips it for good and it
        # reads overdue forever. The count is brought down to the longest
        # cycle the engine accepts for this frequency — the nearest bill
        # that can still be maintained and edited.
        if isinstance(rec.get("interval"), int):
            while (rec["interval"] > 1
                   and _bills.cadence_too_long(rec.get("frequency"),
                                               rec["interval"])):
                rec["interval"] -= 1
        raw["recurrence"] = rec
    if "envelope_months" in raw:
        try:
            raw["envelope_months"] = 12 if int(raw["envelope_months"]) >= 12 else 1
        except (TypeError, ValueError, OverflowError):
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
            except (TypeError, ValueError, OverflowError):
                continue
            if (isinstance(c.get("amount"), bool) or not math.isfinite(amt)
                    or abs(amt) > MAX_AMOUNT):
                continue
            kept.append({**c, "amount": amt})
        raw["companions"] = kept
    if "amount_history" in raw:
        # every bill matcher reads this; the engine's own reader decides
        # what a real past price is, so the restore keeps exactly that
        hist = _budget.amount_history_of(raw)
        if hist:
            raw["amount_history"] = hist
        else:
            raw.pop("amount_history", None)
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
        except (TypeError, ValueError, OverflowError):
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
    would raise out of the due-check and silence every cadence for the
    household, or be read letter by letter. Returns the notes to show."""
    notes: list[str] = []
    sched = cfg.get("email_schedule")
    if "email_schedule" in cfg:
        if not isinstance(sched, dict) or not sched:
            cfg.pop("email_schedule", None)
            sched = None
    if isinstance(sched, dict):
        clean: dict = {}
        for cad in ("daily", "monthly", "yearly"):
            c = sched.get(cad)
            if not isinstance(c, dict):
                continue
            try:
                hour = int(c.get("hour", 7))
            except (TypeError, ValueError, OverflowError):
                hour = -1
            if not 0 <= hour <= 23:
                notes.append(f"the {cad} email schedule in this export was "
                             "malformed and was not restored")
                continue
            # a stray field (an old weekly entry's weekday, say) is ignored,
            # not a reason to drop the entry
            entry = {"on": bool(c.get("on")), "hour": hour}
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
        except (TypeError, ValueError, OverflowError):
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

# The members whose size follows the household's photos rather than its
# ledger. Every receipt travels as base64 of its image and its optimised
# copy, so a few dozen phone photos pass the per-file cap, and a household
# that keeps receipts could not restore its own export. What these members
# hold is already compressed (JPEG, PNG, WebP, PDF), so the honest member
# expands to about a third more than it takes in the ZIP; a bomb expands a
# thousandfold. They are bounded by that ratio instead of by size, each row
# is still held to a live upload's limit as it streams in, and the rest of
# the archive keeps the caps above.
BLOB_MEMBERS = frozenset({"receipts.csv"})
BLOB_MEMBER_MAX_RATIO = 4


def zip_size_problem(infos) -> str | None:
    """Why a restore would refuse an archive with these members, or None.
    The one rule both sides read: the restore refuses on it, and the export
    checks what it just wrote against it."""
    total = 0
    for info in infos:
        if info.filename in BLOB_MEMBERS:
            if (info.file_size > MAX_MEMBER_BYTES
                    and info.file_size > BLOB_MEMBER_MAX_RATIO
                    * max(info.compress_size, 1)):
                return (f"{info.filename} in this ZIP expands "
                        f"{info.file_size // max(info.compress_size, 1)} "
                        "times over — more than photos ever do")
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            return (f"{info.filename} in this ZIP is too large to restore "
                    f"({info.file_size // (1024 * 1024)} MB — the per-file "
                    f"limit is {MAX_MEMBER_BYTES // (1024 * 1024)} MB)")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        return (f"this ZIP expands to {total // (1024 * 1024)} MB — more "
                f"than the {MAX_TOTAL_BYTES // (1024 * 1024)} MB restore "
                "limit")
    return None


def _check_zip_sizes(z: zipfile.ZipFile) -> None:
    problem = zip_size_problem(z.infolist())
    if problem:
        raise ValueError(problem)


def _txn_exists(conn, txn_id) -> bool:
    """Does this restored transaction exist yet?

    Migration 067 gave the transaction-child tables real foreign keys, which
    means restore can no longer insert an annotation whose transaction is
    absent. That is normally impossible — transactions.csv is restored first
    — but a backup taken BEFORE 067 can legitimately contain orphans, and a
    restore is exactly the wrong moment to abort on one. Skip the dangling
    row and carry on; the caller counts it as orphaned, apart from the
    rows that were simply already present.
    """
    if not txn_id:
        return False
    return conn.execute("SELECT 1 FROM transactions WHERE id=%s",
                        (txn_id,)).fetchone() is not None


#: Why an archived row did not land as a row of its own. Every row a pass
#: reads ends in exactly one place: written (counted under its table), or
#: under one of these. Each is the whole truth of what its counter holds.
REFUSALS: dict[str, str] = {
    "present": "the destination already holds the row, or one that takes "
               "its place: the same key, the same income document, the one "
               "fronted movement a charge may carry, the person's own split "
               "on that charge",
    "orphaned": "its parent (a transaction, account, business or receipt) "
                "is not here: the archive did not carry it, or this restore "
                "left it out",
    "out_of_range": "an amount beyond any real sum",
    "malformed": "a row no door of the app writes: a required cell blank, an "
                 "id that is no id, a value outside its set, an image that "
                 "does not decode, a split that does not add up to its charge",
    "over_cap": "past a ceiling every household is held to: a receipt image "
                "over the upload size or the household's file storage, an "
                "account's lineage past its bound",
    "merged": "the same thing the destination holds under an id of its own, "
              "so the archive's pointers now name that one instead",
    "rebuilt": "left for the destination to derive again: a store receipt's "
               "split parts, which the store match rebuilds from the "
               "restored receipts, and an alert nobody dismissed, which the "
               "next Today build raises again if it still holds",
}

#: Written rows that were changed on the way in. Each is a subset of its
#: table's written count, never a refusal.
REPAIRS: dict[str, str] = {
    "amount_repaired": "transactions the archive gave no amount, restored at "
                       "0 so their receipts, notes and splits keep a charge "
                       "to hang on",
}

# the result key a table or refusal is reported under, where it is not its
# own name: the names the import hub has always shown
_RESULT_KEY = {"present": "already_present", "manual_categories": "overrides",
               "tenant_settings": "settings"}


class _Outcomes:
    """The one tally of a restore. Rows are written through `land`, which
    is the only thing that counts a row written or present; a pass that
    refuses a row says why through `refuse`. So a counter means what its
    entry in REFUSALS says, and nothing a pass adds can make it lie."""

    def __init__(self) -> None:
        self.written: dict[str, int] = {}
        self.refused: dict[tuple[str, str], int] = {}
        self.repaired: dict[tuple[str, str], int] = {}

    def land(self, conn, table: str, sql: str, rows: list,
             *, as_one: bool = False) -> list:
        """Run `sql` once per parameter tuple in `rows` and return, per
        row and in order, the first column its RETURNING clause gave back,
        or None for a row the database did not write. `sql` must end in
        RETURNING. A row not written is one the destination already held:
        a pass checks every other reason before it gets here.

        `as_one`: the tuples are spellings of ONE archived row (an alias
        written under its cut and its full key): it counts once, written
        if any spelling was."""
        if not rows:
            return []
        if len(rows) == 1:
            got = [conn.execute(sql, rows[0]).fetchone()]
        else:
            cur = conn.cursor()
            # returning=True keeps executemany pipelined and hands back one
            # result per tuple, in order: which rows landed, not how many
            cur.executemany(sql, rows, returning=True)
            got = [cur.fetchone() for _ in cur.results()]
        if len(got) != len(rows):           # pragma: no cover — psycopg
            raise RuntimeError("restore: a write lost track of its rows")
        # counted by the row coming back, never by its key's value
        n = sum(1 for g in got if g is not None)
        out = [None if g is None else next(iter(g.values())) for g in got]
        if as_one:
            self.wrote(table, 1 if n else 0)
            if not n:
                self._add(self.refused, table, "present", 1)
        else:
            self.wrote(table, n)
            if len(out) - n:
                self._add(self.refused, table, "present", len(out) - n)
        return out

    def wrote(self, table: str, n: int) -> None:
        """`n` rows of `table` written by a statement `land` cannot run
        (a split replaced whole, the settings document)."""
        self.written[table] = self.written.get(table, 0) + n

    def refuse(self, table: str, reason: str, n: int = 1) -> None:
        if reason not in REFUSALS:
            raise ValueError(f"no such restore outcome: {reason}")
        self._add(self.refused, table, reason, n)

    def repair(self, table: str, reason: str, n: int = 1) -> None:
        if reason not in REPAIRS:
            raise ValueError(f"no such restore repair: {reason}")
        self._add(self.repaired, table, reason, n)

    @staticmethod
    def _add(into: dict, table: str, reason: str, n: int) -> None:
        if n:
            into[(table, reason)] = into.get((table, reason), 0) + n

    def total(self, reason: str) -> int:
        src = self.repaired if reason in REPAIRS else self.refused
        return sum(n for (_, r), n in src.items() if r == reason)

    def counts(self) -> dict:
        """The restore's result: rows written per table, then each outcome
        that happened, under its result key."""
        out: dict = {_RESULT_KEY.get(t, t): n for t, n in self.written.items()}
        for reason in (*REFUSALS, *REPAIRS):
            n = self.total(reason)
            if n:
                out[_RESULT_KEY.get(reason, reason)] = n
        return out


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


def _known_merchant(value, known: set, mapped: dict | None = None):
    """A merchant id from the archive, or NULL when it names no merchant.

    `mapped` sends an archive id the restore folded onto a live merchant
    (same aggregator entity, the destination's own id) to that merchant.

    Compared lower-cased: Postgres renders a uuid lower-case, and a
    hand-edited CSV should not lose a pointer to letter case."""
    v = (value or "").strip().lower() or None
    if v is not None and mapped:
        v = mapped.get(v, v)
    return v if v in known else None


def _uuid_or_none(v):
    try:
        return str(uuid.UUID(str(v).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def _fold_mapped_merchants(conn, mapped: dict, folded: list) -> None:
    """Carry what the household decided about an archived merchant onto the
    live merchant it was mapped to, and re-point the restored merchants
    that named it.

    The live row is the aggregator's view of the merchant; the archive's
    row is the household's. A name a PERSON gave it (name_source 'manual')
    replaces an aggregator or derived name, and a merge or parent the
    archive recorded is set where the live row has none. Without this a
    restore would keep the destination's name and leave the merchant
    un-merged, silently undoing renames and merges the household made."""
    arch = list(mapped)
    live = [mapped[a] for a in arch]
    for col in ("merged_into", "parent_id"):
        conn.execute(
            f"""UPDATE merchants m SET {col} = v.live::uuid
                  FROM unnest(%s::text[], %s::text[]) AS v(arch, live)
                 WHERE m.{col}::text = v.arch""", (arch, live))
    for live_id, r in folded:
        if (r.get("name_source") or "") == "manual" and r.get("name"):
            conn.execute(
                """UPDATE merchants SET name = %s, name_source = 'manual',
                          updated_at = now()
                    WHERE id = %s AND name_source <> 'manual'""",
                (_descriptor(r["name"]), live_id))
        for col in ("merged_into", "parent_id"):
            target = (r.get(col) or "").strip().lower()
            target = _uuid_or_none(mapped.get(target, target))
            if target is None or target == live_id:
                continue
            conn.execute(
                f"""UPDATE merchants SET {col} = %s, updated_at = now()
                     WHERE id = %s AND {col} IS NULL""", (target, live_id))


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
    # An archive written with GUARD_MARK also guards the pieces after a
    # ';', tab or line break (api._csv_piece_safe, after the head rule) and
    # adds a quote in front of a household's own leading apostrophe, so
    # both steps there are exact inverses and lose nothing. An older
    # archive was written by the head rule alone, without that doubling:
    # it gets the old head-only reverse, and no piece step that would eat
    # an apostrophe the household typed after a ';'.
    if not isinstance(v, str) or "'" not in v:
        return v
    if not _GUARD_V2.get():
        return _unescape_head_v1(v)
    from ..web.api import _csv_piece_unsafe, _csv_safe
    v = _csv_piece_unsafe(v)
    if v[:1] == "'" and _csv_safe(v[1:]) == v:
        return v[1:]
    return v


def _unescape_head_v1(v: str) -> str:
    """The reverse of the head rule archives carried before GUARD_MARK: a
    quote put in front of = + - @ (behind optional invisible characters)
    or a leading tab/CR, with no doubling of an apostrophe already there."""
    if v[:1] != "'":
        return v
    rest = v[1:]
    lead = ("\t", "\r", "\n", "\ufeff", " ")
    head = rest.lstrip("".join(lead))
    if (rest[:1] in lead + ("=", "+", "-", "@")
            and head[:1] in ("=", "+", "-", "@")) or rest[:1] in ("\t", "\r"):
        return rest
    return v


# The longest a feed's own words about a payee can honestly be. A bank
# descriptor is a fixed-width statement field, an aggregator's merchant name
# is shorter still, and an outlet brand shorter again — the biggest real ones
# run to a couple of hundred characters. A CSV CELL, though, may be megabytes
# (_MAX_CELL is sized for a receipt photo), and these are the strings the
# identity pass at the end of a restore reads word by word, the ones the
# generated search column concatenates, and the ones every payee list shows,
# so one crafted cell is paid for on every read of that ledger from then on.
#
# It is also the difference between a restore and no restore. Several of
# these strings are btree keys — the alias map's own key, a merchant's
# lower(name), a check number — and Postgres refuses an index entry past
# about 2,700 bytes, so ONE absurd cell aborts the whole all-or-nothing
# restore rather than landing oddly. The cap is in characters and a character
# may be four bytes, so it is set well inside that limit.
#
# Clamped rather than refused: a transaction is the household's record of
# money moving, and dropping the row (or the archive) over a long descriptor
# is the worse trade. Nothing honest is ever touched.
MAX_DESCRIPTOR = 600


def _descriptor(v):
    """A restored free-text descriptor: un-escaped, and never longer than a
    real one."""
    # the cell was un-escaped once already, by _rows: a second pass would
    # take a quote off text the household typed
    return v[:MAX_DESCRIPTOR] if isinstance(v, str) else v


def _merchant_name_cells(r) -> tuple:
    """`(merchant_name, merchant_name_set_aside)` for a restored row.

    When the resolver files a charge under the merchant its bank line means,
    the aggregator's name for that one charge MOVES to
    `merchant_name_set_aside` and the row keys on the line from then on, like
    a row the aggregator never named. An archive written by the build that
    instead left the name in place and marked it with a boolean carries the
    stray name in `merchant_name` and `merchant_name_overruled` = true; the
    name is moved here on the way in, so the row means on the destination
    exactly what it meant on the source. An archive older than either
    spelling says nothing about any of this and restores unchanged.
    """
    name = _descriptor(r.get("merchant_name")) or None
    aside = _descriptor(r.get("merchant_name_set_aside")) or None
    if aside is None and _b(r.get("merchant_name_overruled")):
        name, aside = None, name
    return name, aside


# who decided a row's category, when it was a movement of money (migration 098)
_FLOW_PRIMARIES = ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "INCOME")
# the aggregator's facts a row carries as columns (base.enrichment_from_raw)
_ENRICHMENT_TEXT = ("check_number", "payment_channel", "payment_processor",
                    "location_city", "location_region", "location_address",
                    "location_postal", "location_store", "mcc")


def upgrade_archived_txn(row: dict, raw: dict, *, on_card: bool,
                         has_merchant: bool) -> dict:
    """A restored transaction's cells as the migrations that rewrote stored
    rows left them, applied in the order they ran, to every archived row.
    Each step is the rule the ingest applies to a new row, so on a row an
    archive written after the migration carries it changes nothing.

    `row` holds the cells by column name, `raw` the raw record as it lands;
    `on_card` says the row is on a credit card, `has_merchant` that the
    archive pointed it at a merchant row (an archive old enough to need the
    name steps was written before merchant rows existed, and a name is
    never rewritten under a merchant the household already filed it with).

      064  Plaid's own category, from raw.personal_finance_category
      075  a money-in row on a card that reads as a payment is one
      081  a Venmo row the aggregator left unnamed takes its counterparty,
      083  with the rail kept ("Venmo — …"),
      082  and a Zelle row its counterparty from the statement line
      092  the fuel suffix 091 wrote into merchant_name is taken back off,
      093  and the pump goes in merchant_outlet instead (base.fuel_arm)
      094  a proven pump is filed as fuel
      098  the aggregator's facts in raw become columns, and the row says
           who decided its category

    A person's category (category_override) is never read or written, a
    category a rule decided (category_source rule_*) is left as the rule
    left it, and a row the aggregator withdrew (removed) keeps what it had,
    as the migrations left it."""
    from .flowmap import CC_PAYMENT_DETAILED, looks_like_card_payment_on_card
    out = dict(row)
    live = not out.get("removed")
    amount = out.get("amount") or 0
    line = out.get("name")
    desc = raw.get("original_description")
    desc = _descriptor(desc) if isinstance(desc, str) else None
    by_rule = str(out.get("category_source") or "").startswith("rule")

    pfc = raw.get("personal_finance_category")
    if out.get("category_plaid") is None and isinstance(pfc, dict):
        for col, key in (("category_plaid", "primary"),
                         ("category_plaid_detailed", "detailed"),
                         ("category_plaid_confidence", "confidence_level")):
            v = pfc.get(key)
            if out.get(col) is None and isinstance(v, str) and v:
                out[col] = _descriptor(v)

    name = out.get("merchant_name")
    if not has_merchant and out.get("merchant_name_set_aside") is None:
        venmo = _sync_base.venmo_counterparty(desc)
        if venmo and (name is None or name == venmo.split(" — ", 1)[1]):
            name = venmo
        zelle = _sync_base.zelle_counterparty(desc or line, amount < 0)
        if zelle and (name is None
                      or _sync_base._ZELLE_DESCRIPTOR.search(name)):
            name = zelle
        given = raw.get("merchant_name")
        if (live and name and isinstance(given, str) and given
                and name.lower() in (given.lower() + " gas",
                                     given.lower() + " fuel")):
            name = _descriptor(given)
    out["merchant_name"] = name

    if (on_card and live and amount < 0 and not by_rule
            and out.get("category_detailed") != CC_PAYMENT_DETAILED
            and looks_like_card_payment_on_card(name or line)):
        out["category_primary"] = "LOAN_PAYMENTS"
        out["category_detailed"] = CC_PAYMENT_DETAILED
        out["category_source"] = "flow"

    if out.get("merchant_outlet") is None and live and name:
        out["merchant_outlet"] = _descriptor(
            _sync_base.fuel_arm(name, desc, line))
    if (out.get("merchant_outlet") and live and not by_rule
            and out.get("category_override") is None
            and (out.get("category_primary") or "") != "TRANSPORTATION"):
        out["category_primary"] = "TRANSPORTATION"
        out["category_detailed"] = "TRANSPORTATION_GAS"
        out["category_source"] = "fuel"

    try:
        en = _sync_base.enrichment_from_raw(raw)
    except OverflowError:
        # a bare integer too large for a float (json reads one as an int)
        # is no coordinate; the row's other facts still land
        loc = {k: v for k, v in raw["location"].items()
               if k not in ("lat", "lon") or _floats(v)}
        en = _sync_base.enrichment_from_raw({**raw, "location": loc})
    for col in _ENRICHMENT_TEXT:
        if out.get(col) is None and en[col] is not None:
            out[col] = _descriptor(en[col])
    # a coordinate off the globe is no coordinate either
    for col, bound in (("location_lat", 90.0), ("location_lon", 180.0)):
        v = en[col]
        if (out.get(col) is None and v is not None and math.isfinite(v)
                and abs(v) <= bound):
            out[col] = v
    if out.get("authorized_date") is None and en["authorized_date"]:
        try:
            out["authorized_date"] = as_date(en["authorized_date"])
        except ValueError:
            pass
    if out.get("category_source") is None and out.get("category_primary"):
        plaid = out.get("category_plaid")
        out["category_source"] = (
            "plaid" if plaid and out["category_primary"] == plaid
            else "rule" if plaid
            else "flow" if out["category_primary"] in _FLOW_PRIMARIES
            else "import")
    return out


def _floats(v) -> bool:
    """Whether float() of `v` gives a number rather than overflowing."""
    try:
        float(v)
    except OverflowError:
        return False
    except (TypeError, ValueError):
        pass
    return True


def _persons_names(conn, z) -> set[str]:
    """The lowered names a person gave a merchant through an alias (a
    manual one, or one an archive writes with no method, which restores
    as manual), here or in the archive: the names the cut of a machine's
    merchant name leaves as typed. Only names that hold a number matter."""
    names = {r["c"] for r in conn.execute(
        "SELECT lower(canonical) AS c FROM merchant_canonical"
        " WHERE method = 'manual' AND canonical ~ %s",
        (card_number.sql_selector(),)).fetchall()}
    for r in _rows(z, "merchant_canonical.csv"):
        canon = _descriptor(r.get("canonical"))
        if ((r.get("method") or "manual") == "manual"
                and card_number.holds_number(canon)):
            names.add(canon.lower())
    return names


def _cut_unless_held(conn, sql: str, value):
    """`value` cut, unless a row `sql` finds by the whole `value` still
    names it so (then nothing would find that row by the cut one)."""
    if not card_number.holds_number(value):
        return value
    if conn.execute(sql, (value,)).fetchone():
        return value
    return card_number.cut(value)


_BILL_BY_PAYEE = "SELECT 1 FROM bills WHERE payee = %s"


def _mark_name(conn, entity_id, name, merchant_id, machine_cut: dict,
               persons_names: set):
    """The merchant name a restored 1099 mark lands under. A mark's name
    follows its merchant's, cut where a machine named the merchant: the
    merchant it points at, or with no pointer the live merchant of its
    name. A name no merchant carries is what a person typed on the mark,
    and stays; so does one another mark of the business already holds
    cut (the nightly fold joins the two)."""
    if not card_number.holds_number(name):
        return name
    cut = card_number.cut(name)
    if merchant_id is not None:
        m = conn.execute("SELECT name, name_source FROM merchants"
                         " WHERE id = %s", (merchant_id,)).fetchone()
        follows = bool(m) and m["name_source"] != "manual" and (
            (m["name"] or "").lower() not in persons_names)
    else:
        follows = name.lower() in machine_cut
    if not follows or conn.execute(
            "SELECT 1 FROM vendor_1099 WHERE entity_id = %s"
            " AND merchant = %s", (entity_id, cut)).fetchone():
        return name
    return cut


def _mark_merchant(conn, entity_id, name):
    """The merchant a 1099 mark that names none is for, as migration 164
    found it for the stored marks: the one live merchant of that name, when
    no other mark of the same business already holds it (that one keeps
    it, and this mark keeps its name alone, which the read side still
    finds). None when no single merchant answers to the name."""
    if not isinstance(name, str) or not name:
        return None
    hits = conn.execute(
        "SELECT id::text AS id FROM merchants"
        " WHERE merged_into IS NULL AND name = %s LIMIT 2",
        (name,)).fetchall()
    if len(hits) != 1:
        return None
    held = conn.execute(
        "SELECT 1 FROM vendor_1099 WHERE entity_id = %s AND merchant_id = %s",
        (entity_id, hits[0]["id"])).fetchone()
    return None if held else hits[0]["id"]


_RULES_FROM_OVERRIDES = "035_canonical_category_backfill.sql"
_MODEL_RULES_ON_THE_MERCHANT = "044_llm_rule_canonical_rekey.sql"
# a correction to one of these is a movement of money, never a spend rule
_FLOW_OVERRIDES = _FLOW_PRIMARIES + ("BANK_FEES",)


def derive_user_rules_from_overrides(conn, landed: list[str]) -> int:
    """The person's merchant rules migration 035 derived from their own
    corrections, for the merchants of the corrected rows a restore just
    wrote: a merchant (through merchant_canonical) whose every correction
    by a person agrees gets that category as their rule. A correction of a
    movement of money, or on one, derives nothing; a rule a person already
    holds is never replaced, and a machine's rule at the same key gives way
    to theirs. Only a person's corrections count (override_source 'user'):
    a bill's or a store's category is not the person teaching a merchant.
    Returns rules written."""
    if not landed:
        return 0
    return conn.execute(
        """WITH named AS (
               SELECT t.id, t.removed, t.category_override,
                      t.category_primary, t.override_source,
                      COALESCE(mc.canonical, t.merchant_name, t.name) AS canon
                 FROM transactions t
                 LEFT JOIN merchant_canonical mc
                        ON mc.raw_merchant = COALESCE(t.merchant_name, t.name)),
           touched AS (
               SELECT DISTINCT canon FROM named WHERE id = ANY(%(ids)s)),
           agreed AS (
               SELECT canon, MAX(category_override) AS cat
                 FROM named
                WHERE removed = 0
                  AND override_source = 'user'
                  AND COALESCE(category_override, '') <> ''
                  AND category_override <> ALL(%(flows)s)
                  AND COALESCE(category_primary, '') <> ALL(%(primaries)s)
                  AND canon IN (SELECT canon FROM touched)
                GROUP BY canon
               HAVING COUNT(DISTINCT category_override) = 1)
           INSERT INTO merchant_categories
                  (merchant, category_primary, source, classified_at)
           SELECT canon, cat, 'user', now() FROM agreed
            WHERE canon IS NOT NULL AND canon <> ''
           ON CONFLICT (tenant_id, merchant) DO UPDATE
              SET category_primary = EXCLUDED.category_primary,
                  source = 'user', classified_at = now()
            WHERE merchant_categories.source <> 'user'""",
        {"ids": list(landed), "flows": list(_FLOW_OVERRIDES),
         "primaries": list(_FLOW_PRIMARIES)}).rowcount


def rekey_model_rules_on_the_merchant(conn) -> int:
    """Migration 044 over the household: a model's rules keyed on a raw
    spelling of a merchant fold into one rule on the merchant
    (merchant_canonical), only where every rule resolving to that merchant
    agrees on the category and on being disabled — the condition under
    which the rules already act as one, so no transaction's category
    changes. A merchant whose rules disagree keeps them all; a person's or
    a seed rule is never moved, and a rule already at the merchant's own
    key absorbs the others. Returns the rules removed."""
    return conn.execute(
        """WITH resolved AS (
               SELECT m.merchant, m.source, m.category_primary, m.disabled,
                      m.classified_at,
                      COALESCE(mc.canonical, m.merchant) AS canon
                 FROM merchant_categories m
                 LEFT JOIN merchant_canonical mc ON mc.raw_merchant = m.merchant),
           grp AS (
               SELECT canon,
                      COUNT(*) FILTER (WHERE merchant = canon) AS at_canon,
                      COUNT(DISTINCT category_primary) AS n_cats,
                      COUNT(DISTINCT disabled) AS n_flags,
                      MIN(category_primary) AS cat,
                      BOOL_AND(disabled) AS all_disabled,
                      MAX(classified_at) FILTER (WHERE source = 'llm') AS latest
                 FROM resolved
                GROUP BY canon
               HAVING COUNT(*) FILTER (WHERE merchant <> canon
                                         AND source = 'llm') > 0),
           mergeable AS (
               SELECT * FROM grp WHERE n_cats = 1 AND n_flags = 1),
           del AS (
               DELETE FROM merchant_categories m
                USING merchant_canonical mc, mergeable g
                WHERE mc.raw_merchant = m.merchant
                  AND m.merchant <> mc.canonical
                  AND g.canon = mc.canonical
                  AND m.source = 'llm'
               RETURNING 1),
           ins AS (
               INSERT INTO merchant_categories
                      (merchant, category_primary, source, classified_at,
                       disabled)
               SELECT canon, cat, 'llm', latest, all_disabled
                 FROM mergeable WHERE at_canon = 0
               ON CONFLICT (tenant_id, merchant) DO NOTHING
               RETURNING 1)
           SELECT (SELECT COUNT(*) FROM del) AS n""").fetchone()["n"]


def _follow_moved_bills(cfg: dict, bill_payees: dict) -> None:
    """The settings keyed on a bill's payee (the bills switched off, the
    occurrence caps, the dismissed hints and health flags) renamed onto
    the payee each moved bill landed as."""
    if not bill_payees:
        return
    off = cfg.get("disabled_bills")
    if isinstance(off, list):
        cfg["disabled_bills"] = [bill_payees.get(v, v)
                                 if isinstance(v, str) else v for v in off]
    for key in ("occurrence_caps", "dismissed_hints", "dismissed_health"):
        by_payee = cfg.get(key)
        if isinstance(by_payee, dict):
            cfg[key] = {bill_payees.get(k, k): v
                        for k, v in by_payee.items()}


def _offer_cut(conn, kind, payee, evidence: dict,
               bill_payees: dict) -> tuple:
    """(payee, evidence) of a restored bill offer with every card number
    cut as the bill pass now writes them, once the bills have landed.

    An offer names its bill by payee: one naming a bill this restore moved
    (`bill_payees`, archived payee to the one it landed as) follows it. Its
    own label is the ledger's spelling of a series and is cut where no
    bill is named by it. An offer to join a merchant names the merchant by
    its stored name, and follows it only where a live merchant now holds
    the cut name and none the whole one. The stream's description in the
    evidence is always cut."""
    evidence = dict(evidence)
    joins = kind in ("attach", "merchant")
    bp = evidence.get("bill_payee")
    if joins and isinstance(bp, str):
        bp = bill_payees.get(bp, bp)
        evidence["bill_payee"] = _cut_unless_held(conn, _BILL_BY_PAYEE, bp)
    if kind != "merchant" and isinstance(payee, str):
        if not joins and payee in bill_payees:
            payee = bill_payees[payee]
        payee = _cut_unless_held(conn, _BILL_BY_PAYEE, payee)
    elif kind == "merchant" and card_number.holds_number(payee):
        cut = card_number.cut(payee)
        live = ("SELECT 1 FROM merchants WHERE merged_into IS NULL"
                " AND lower(name) = lower(%s)")
        if (conn.execute(live, (cut,)).fetchone()
                and not conn.execute(live, (payee,)).fetchone()):
            payee = cut
            if isinstance(evidence.get("merchant_name"), str):
                evidence["merchant_name"] = card_number.cut(
                    evidence["merchant_name"])
    if isinstance(evidence.get("description"), str):
        evidence["description"] = card_number.cut(evidence["description"])
    return payee, evidence


def _plan_bill_moves(conn, rows: list) -> list:
    """Where each archived bill lands, decided for all of them before any
    is written: `rows` is (archived id, payee, made by a person) per bill,
    and the answer is (id, payee, held) per row, in order, where `held` is
    the household's (id, payee) the row IS (counted present) or None.

    A detected bill whose payee spells a card number moves to the cut name
    and its id, as migration 206 moved the stored ones, and only where
    that name is free: two bills are never made one. So a row stays as it
    is when the household holds its own id (it is that bill), and keeps
    its own id and payee when another archived bill holds the cut id or
    name, when a person's bill here holds either, or when another
    archived bill cuts to the same id first (the lowest archived id moves, as 206's
    DISTINCT ON). A detected bill the household holds under the cut id or
    name, and no archived bill does, is the same bill an upgrade moved
    before this older archive came back: the row is that bill."""
    here = conn.execute(
        "SELECT id, payee, raw->>'source' IS NOT DISTINCT FROM 'manual'"
        "       AS hand_made FROM bills ORDER BY id").fetchall()
    here_ids = {h["id"] for h in here}
    here_by_payee: dict = defaultdict(list)
    for h in here:
        here_by_payee[h["payee"]].append(h)
    here_by_id = {h["id"]: h for h in here}
    plan: list = [(bid, payee, None) for bid, payee, _hm in rows]
    archived_ids = {bid for bid, _p, _hm in rows}
    archived_payees = {payee for _b, payee, _hm in rows}
    wants: dict[str, list[int]] = defaultdict(list)
    for i, (bid, payee, hand_made) in enumerate(rows):
        if (hand_made or bid in here_ids
                or not card_number.holds_number(payee)):
            continue
        cut_payee = card_number.cut(payee)
        cut_id = _bills._slug(cut_payee)
        # (the row's own payee spells the number, so it is never the cut
        # one; its own id can be, and is no other bill)
        if ((cut_id != bid and cut_id in archived_ids)
                or cut_payee in archived_payees):
            continue
        holders = ([here_by_id[cut_id]] if cut_id in here_by_id else []) + [
            h for h in here_by_payee.get(cut_payee, ()) if h["id"] != cut_id]
        if any(h["hand_made"] for h in holders):
            continue
        wants[cut_id].append(i)
        held = (holders[0]["id"], holders[0]["payee"]) if holders else None
        plan[i] = (cut_id, cut_payee, held)
    for cut_id, takers in wants.items():
        first = min(takers, key=lambda i: rows[i][0])
        for i in takers:
            if i != first:
                plan[i] = (rows[i][0], rows[i][1], None)
    return plan


def restore_zip(conn, data: bytes, *, progress=None,
                starter: str | None = None) -> dict:
    """Merge an export ZIP into this tenant. `progress`, when given, is
    called as (table, rows, done) while the members stream past — the
    background-job door uses it to keep a status row fresh so a large
    restore reports where it is instead of looking hung. `starter` is the
    credential fingerprint of the request that started the restore (the
    background job captures it before leaving the request); None reads the
    request in flight."""
    return _restore(conn, data, _Outcomes(), progress=progress,
                    starter=starter)


def restore_outcomes(conn, data: bytes) -> tuple[dict, _Outcomes]:
    """`restore_zip`, also handing back the tally behind its counts: per
    table, the rows written and each outcome of the rows that were not."""
    out = _Outcomes()
    return _restore(conn, data, out), out


def _restore(conn, data, out: _Outcomes, *, progress=None,
             starter: str | None = None) -> dict:
    token = _PROGRESS.set(progress) if progress is not None else None
    renames = _COMMA_RENAMES.set({})
    moved_txns = _MOVED_TXNS.set({})
    moved = _MOVED_ACCOUNTS.set(_plan_moved_accounts(conn))
    sessions = _START_SESSIONS.set(_starter_watch(conn, starter))
    guard = _GUARD_V2.set(False)
    try:
        return _restore_zip(conn, data, out)
    finally:
        _START_SESSIONS.reset(sessions)
        _GUARD_V2.reset(guard)
        _MOVED_TXNS.reset(moved_txns)
        _COMMA_RENAMES.reset(renames)
        _MOVED_ACCOUNTS.reset(moved)
        if token is not None:
            _PROGRESS.reset(token)


def _push_fed_items(z) -> dict[str, str]:
    """The push doors' connections an archive written before migration 022
    calls 'csv', by id, with the aggregator 022 gave them. The coinbase door
    names its connections `coinbase-<label>`; the plan door's mark (its
    access token) never travels, but every account it creates is named
    `<connection>-<plan>` for one of plan_csv.PLAN_CODES, and an ordinary
    file import's accounts never are. A push door's connection is
    archived: its script sends the rows, so it stays out of the pull loop
    and the stale-connection checks."""
    from .plan_csv import PLAN_CODES
    plans = {c.lower() for c in PLAN_CODES}
    accounts: dict[str, list[str]] = defaultdict(list)
    for r in _rows(z, "accounts.csv"):
        if r.get("item_id") and r.get("id"):
            accounts[str(r["item_id"])].append(str(r["id"]))
    out: dict[str, str] = {}
    for r in _rows(z, "items.csv"):
        iid = str(r.get("id") or "")
        if not iid or (r.get("aggregator") or "") != "csv":
            continue
        if iid.startswith("coinbase-"):
            out[iid] = "coinbase"
        elif accounts.get(iid) and all(
                a.startswith(iid + "-") and a[len(iid) + 1:] in plans
                for a in accounts[iid]):
            out[iid] = "plan_csv"
    return out


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


_INVITES_MEMBER = "recipient_invites.csv"
_RECIPIENT_INVITES = "074_recipient_invites.sql"
_NO_INVITES = {"total": 0, "rows": 0, "accepted": 0, "declined": 0,
               "pending": 0}


def _restore_recipient_invites(conn, z, configured=()) -> dict:
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
      can mute themselves in Settings, and on hosted they additionally have to
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

    A DECISION MADE HERE ALWAYS WINS, and only a decision counts as one. A
    `DO NOTHING` conflict clause would fire on any existing row — including
    the merely PENDING row that `mailguard.invite_added` writes on an
    ordinary settings save — so the ordinary migration shape (sign up on
    the destination, type your recipients, then restore) would throw away
    every answer the archive carried. An ANSWERED row wins; an unanswered
    one is updated in place, keeping its live token so a link already in
    somebody's mailbox is not quietly voided.

    An accepted row is not an authorization boundary and is not being
    treated as one: on hosted, `mailguard.members_only` is what keeps a
    household's balances from reaching an outside inbox, and the restored
    recipient list passes through that filter regardless of what this table
    says. This function is about consent, which is a different question.

    Runs on the ADMIN connection and after the data transaction commits:
    migration 074 grants the app role nothing here on purpose, and a
    consent row must not land for a restore that then failed.

    AN ADDRESS THE ARCHIVE LISTS BUT NEVER ASKED is a recipient from before
    invites existed: `configured` is the archive's own `email_recipients`,
    as it lands, and an archive written before migration 074 carries no
    answers at all. 074 kept those households' mail flowing by recording
    every configured address as having agreed, with no link; the restore
    does the same for each such address under the same one exception as an
    acceptance above — it holds an account here — so a household moved
    from an old install keeps mailing its own members, and an archive
    stripped of its answers cannot enrol anyone else. Every other such
    address must be invited here, and the tally says so — as it does for
    a configured address an archive from after 074 never answered for,
    which is counted pending and written nowhere.

    Returns a tally of what the household actually ends up with — `total`
    addresses in the archive, `rows` written, and how many of those addresses
    are `accepted`, `declined`, or still `pending` here — so the restore can
    say out loud who has to answer again."""
    from ..web.mailguard import split_mailboxes as mailguard_split
    from ..web.mailguard import valid_address
    seen: dict[str, dict] = {}
    for r in _rows(z, "recipient_invites.csv"):
        email = (r.get("email") or "").strip().lower()
        # one bare mailbox, the test every door that stores an address
        # applies: a row is a decision about exactly one person
        if not email or not valid_address(email):
            continue
        # one mailbox is one recipient however many times the file names it
        seen[email] = r
        if len(seen) >= _MAX_RECIPIENT_INVITES:
            break
    # the archive's recipients it holds no answer for, one bare mailbox
    # per entry (a joined entry names several people, and one consent is
    # not several people saying yes)
    unasked: list[str] = []
    # only an archive with no answers member at all is from before invites:
    # one that has it says, by leaving an address out, that it was never
    # asked, and nobody here may answer for it. The export began carrying
    # the member some time after the _RECIPIENT_INVITES migration, so an
    # archive without it whose rows show that migration or later
    # (archive_upgrades.archive_schema) held answers it did not carry: a
    # decline among them is not consent, and its
    # recipients are invited again. Recipients nobody here may answer for
    # are still counted, unwritten, as pending: Settings goes on listing
    # them while the worker mails only who accepted, so the restore says
    # who must be invited again rather than let their mail stop in silence
    enrol = (_INVITES_MEMBER not in z.namelist()
             and _upgrades.older_than(_upgrades.archive_schema(z),
                                      _RECIPIENT_INVITES))
    if not isinstance(configured, (list, tuple)):
        configured = ()
    # the owner is mailed by construction and is never invited (nor counted)
    owners = _owner_addresses(conn) if configured else set()
    for entry in configured:
        boxes, _bad = mailguard_split([entry])
        if len(boxes) != 1:
            continue
        email = boxes[0].strip().lower()
        if (email in seen or email in unasked or email in owners
                or not valid_address(email)
                or len(seen) + len(unasked) >= _MAX_RECIPIENT_INVITES):
            continue
        unasked.append(email)
    if not seen and not unasked:
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
            # the consent migration 074 recorded for a recipient configured
            # before invites existed: agreed, as of now, with no link, so it
            # reads as consent that predates the invite and never had one
            for email in unasked:
                if not enrol or email not in members:
                    continue
                c = admin.execute(
                    """INSERT INTO recipient_invites
                           (tenant_id, email, accepted_at)
                       VALUES (%s, %s, now())
                       ON CONFLICT (tenant_id, email) DO UPDATE
                          SET accepted_at = EXCLUDED.accepted_at
                        WHERE recipient_invites.accepted_at IS NULL
                          AND recipient_invites.declined_at IS NULL""",
                    (tid, email))
                written += c.rowcount
            # Report the STATE, not the writes. A row the destination already
            # held answered writes nothing and is still good news; an address
            # that stayed pending writes nothing and is the thing the owner
            # has to act on. Counting inserts alone reported neither.
            final = {r["email"]: r for r in admin.execute(
                "SELECT email, accepted_at, declined_at FROM recipient_invites"
                " WHERE tenant_id = %s AND email = ANY(%s)",
                (tid, list(seen) + unasked)).fetchall()}
    finally:
        admin.close()
    listed = list(seen) + unasked
    accepted = sum(1 for e in listed
                   if (final.get(e) or {}).get("accepted_at"))
    declined = sum(1 for e in listed
                   if (final.get(e) or {}).get("declined_at"))
    return {"total": len(listed), "rows": written, "accepted": accepted,
            "declined": declined,
            "pending": len(listed) - accepted - declined}


def _outcome_notes(out: _Outcomes) -> list[str]:
    """What the person restoring is told about the rows that did not come
    back as they were. `present` is said by the count alone."""
    notes = []
    orphaned = out.total("orphaned")
    if orphaned:
        # not "already present": these rows were never here and are not
        # now — the transaction, account, business or receipt they belong
        # to is not in the archive (one written before the child tables had
        # real foreign keys can carry them), or was itself left out above
        notes.append(f"{orphaned} row(s) referred to a transaction, account, "
                     f"business or receipt the archive does not carry, or "
                     f"that this restore left out, and were left out too")
    if out.total("out_of_range"):
        notes.append(_out_of_range_note(out.total("out_of_range")))
    malformed = out.total("malformed")
    if malformed:
        notes.append(f"{malformed} row(s) were in a shape no part of the app "
                     f"writes (a required cell blank, an unreadable id or "
                     f"image, a split that does not add up) and were left out")
    over_cap = out.total("over_cap")
    if over_cap:
        notes.append(f"{over_cap} row(s) were past a limit every household "
                     f"is held to (a receipt image over the upload size, an "
                     f"account's id history) and were left out")
    repaired = out.total("amount_repaired")
    if repaired:
        notes.append(f"{repaired} transaction(s) had no amount in the "
                     f"archive and were restored with amount 0, keeping "
                     f"their receipts, notes and splits — edit the amount "
                     f"on each to correct it")
    return notes


PORTABILITY_REFUSAL = (
    "This is a data-portability archive, not a backup to restore; restore "
    "the export ZIP from Settings → Data (“Download export ZIP”) instead. "
    "Nothing was imported.")
# the portability archive's manifest names its format; it carries the
# household's users and control rows, so it is read under a cap of its own
_PORTABILITY_FORMAT = "oikonome-tenant-export/"
_PORTABILITY_MANIFEST_MAX = 16 * 1024 * 1024
_PORTABILITY_MEMBER = re.compile(r"^data/[^/]+\.json$")


def _refuse_portability_archive(z: zipfile.ZipFile) -> None:
    """Refuse the data-portability archive (tenant_export.build_archive)
    before anything is read or written. It is JSON under data/ for a
    person to read, not the CSVs a restore reads, so it would land nothing
    and report success. Known by its manifest's format, or, with the
    manifest gone, by its shape: data/*.json members and no CSV at the
    top."""
    names = z.namelist()
    if (any(_PORTABILITY_MEMBER.match(n) for n in names)
            and not any("/" not in n and n.endswith(".csv") for n in names)):
        raise ValueError(PORTABILITY_REFUSAL)
    try:
        info = z.getinfo(_upgrades.MANIFEST)
    except KeyError:
        return
    if info.file_size > _PORTABILITY_MANIFEST_MAX:
        return
    try:
        doc = json.loads(z.read(info).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, zipfile.BadZipFile):
        return
    if isinstance(doc, dict) and str(doc.get("export_format") or "") \
            .startswith(_PORTABILITY_FORMAT):
        raise ValueError(PORTABILITY_REFUSAL)


FOREIGN_ARCHIVE_REFUSAL = (
    "This ZIP isn't an Oikonome export, so there is nothing in it to "
    "restore. If it came from another budgeting app, unzip it and import "
    "the transactions CSV inside (a YNAB export: its Register.csv). "
    "Nothing was imported.")
# The export itself, zipped with the folder around it: Safari unzips a
# download, and compressing that folder puts every member one level down.
# It is the household's own file, so the sentence must not send it toward
# the transactions-CSV import, which would pour every account into one.
NESTED_EXPORT_REFUSAL = (
    "This ZIP holds an Oikonome export inside a folder, so the restore "
    "can't read it. Unzip it, open the folder, select the files in it and "
    "zip the files themselves (not the folder), then upload that ZIP. "
    "Nothing was imported.")
# the members a restore reads (every member named below; a test holds
# the two lists together), any one of which marks the household export —
# besides the manifest, which an older one lacks. transactions.csv alone
# is not enough: a zipped spending export may carry one of that name.
_EXPORT_MEMBERS = frozenset({
    "account_adoptions.csv",
    "account_links.csv",
    "account_sources.csv",
    "accounts.csv",
    "activity_log.csv",
    "alerts_log.csv",
    "amazon_matches.csv",
    "amazon_orders.csv",
    "amazon_summaries.csv",
    "bill_proposals.csv",
    "bills.csv",
    "budget_snapshots.csv",
    "business_entity.csv",
    "business_flags.csv",
    "business_txn_class.csv",
    "compliance_obligation.csv",
    "costco_matches.csv",
    "costco_receipts.csv",
    "crypto_holdings.csv",
    "entity_membership.csv",
    "equity_movement.csv",
    "holdings.csv",
    "import_batches.csv",
    "income_annual.csv",
    "income_documents.csv",
    "items.csv",
    "liabilities.csv",
    "manual_categories.csv",
    "merchant_canonical.csv",
    "merchant_categories.csv",
    "merchant_merge_proposals.csv",
    "merchant_renames.csv",
    "merchants.csv",
    "mileage_log.csv",
    "networth_recorded.csv",
    "networth_snapshot.csv",
    "receipt_items.csv",
    "receipts.csv",
    "recipient_invites.csv",
    "recurring.csv",
    "reimburse_flags.csv",
    "reimbursements.csv",
    "tenant_settings.csv",
    "transaction_notes.csv",
    "transaction_splits.csv",
    "vendor_1099.csv",
})


def _is_export(z, names: set[str], prefix: str) -> bool:
    """Do these member names (read under `prefix`) make the export?"""
    if _upgrades.MANIFEST in names or names & _EXPORT_MEMBERS:
        return True
    if "transactions.csv" in names:
        with z.open(prefix + "transactions.csv") as f:
            head = f.readline(4096).decode("utf-8-sig", "replace")
        return "account_id" in next(csv.reader([head]), [])
    return False


def _one_shared_folder(names: set[str]) -> str | None:
    """'folder/' when every member that matters sits in that one folder —
    the litter a Mac adds (__MACOSX/, .DS_Store) aside — else None."""
    tops = {n.split("/", 1)[0] + "/" for n in names
            if "/" in n and not n.startswith("__MACOSX/")}
    loose = [n for n in names if "/" not in n and n != ".DS_Store"]
    return tops.pop() if len(tops) == 1 and not loose else None


def foreign_archive_refusal(data) -> str | None:
    """The sentence refusing a ZIP that is not the household export, or
    None for one that is. Read from the members' names (and the header of
    a lone transactions.csv, which a zipped spending export may share), so
    every door asks the same question of the same file before it writes."""
    try:
        z = (data if isinstance(data, zipfile.ZipFile) else zipfile.ZipFile(
            data if hasattr(data, "seek") else io.BytesIO(data)))
        names = set(z.namelist())
        if _is_export(z, names, ""):
            return None
        prefix = _one_shared_folder(names)
        if prefix and _is_export(z, {n[len(prefix):] for n in names
                                     if n.startswith(prefix)}, prefix):
            return NESTED_EXPORT_REFUSAL
    except Exception:
        # an encrypted, damaged or oddly compressed archive: the restore's
        # own checks say what broke, rather than this sniff failing the door
        return None
    return FOREIGN_ARCHIVE_REFUSAL


def _restore_zip(conn, data, out: _Outcomes) -> dict:
    # bytes (the API's small-archive door) or a seekable mapping (the
    # streamed restore door): reading a mapping through BytesIO would copy
    # the whole archive back into the heap the streaming was built to avoid
    z = zipfile.ZipFile(data if hasattr(data, "seek") else io.BytesIO(data))
    _check_zip_sizes(z)          # refuse zip bombs before reading rows
    _refuse_portability_archive(z)
    refusal = foreign_archive_refusal(z)
    if refusal:
        raise ValueError(refusal)
    # how this archive's cells were guarded; restore_zip scopes the value
    _GUARD_V2.set(archive_guard_v2(z))
    planned = _COMMA_RENAMES.get()
    if planned is not None:
        planned.update(_plan_comma_renames(conn, z))
    moved_accounts = _MOVED_ACCOUNTS.get()
    if moved_accounts is not None:
        _resolve_cut_lineage(conn, z, moved_accounts)
    carried = _MOVED_TXNS.get()
    if carried is not None:
        # read before any row lands: the pairing is against what the
        # household held when the restore began. The exact map first: the
        # pairing below reads the archive through it, so a row it places
        # is never offered as a twin of another
        carried.update(_plan_keyed_txns(conn, z))
        carried.update(_plan_foreign_keyed_txns(conn, z, carried))
        carried.update(_plan_moved_txns(conn, z))
        carried.update(_plan_plain_by_content(conn, z, carried))

    # Validate the incoming config FIRST — the connection is autocommit,
    # so a rejection after the row loops would still leave the ZIP's
    # transactions behind. Reject at the door, before anything lands.
    # tenant_settings is a single tiny row and we index it — materialize just
    # this one member (the big tables below stay streamed via the generator)
    settings = list(_rows(z, "tenant_settings.csv"))
    cfg_in = (_j(settings[0]["config"])
              if settings and settings[0].get("config") else None)
    notes: list[str] = []
    # the daily email's recipients the archive brings, as they land
    archived_recipients: list = []
    if isinstance(cfg_in, dict):
        # The parsed cell can hold a NUL its CSV text spelled as an escape.
        # Settled here, before any key check reads it: values lose theirs,
        # and a key holding one refuses the file, so the spelling every
        # validator sees is the one that would be stored.
        cfg_in = strip_nul(cfg_in)
        notes = _check_config(cfg_in)

    # One transaction: a mid-restore failure (bad row deep in the ZIP,
    # connection drop) leaves nothing behind.
    with conn.transaction():
        # storage, pairing, identity, before any row lands: the order
        # every other writer takes them in (bulk_import)
        from ..web.bulk_import import hold_restore_write_locks
        hold_restore_write_locks(conn)
        push_fed = None
        for r in _rows(z, "items.csv"):
            if not r.get("id"):
                out.refuse("items", "malformed")
                continue
            aggregator = r.get("aggregator") or "manual"
            if aggregator == "csv":
                if push_fed is None:
                    push_fed = _push_fed_items(z)
                # a push door's connection an archive from before the doors
                # stamped their own name still calls 'csv' (migration 022)
                aggregator = push_fed.get(str(r["id"]), aggregator)
            archived = ((r.get("status") or "") == "archived"
                        or aggregator != (r.get("aggregator") or "manual"))
            out.land(conn, "items",
                """INSERT INTO items (id, aggregator, institution_id,
                       institution_name, status, raw, logo, brand_color, url,
                       archived_reason, archived_at, linked_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(r["id"], aggregator,
                 r.get("institution_id") or None,
                 r.get("institution_name") or None,
                 # sync-less shell until the aggregator is reconnected —
                 # except archived items (push-fed plan-CSV/coinbase, archived
                 # simplefin children) which stay archived: 'restored' would
                 # re-enter them into connection-freshness checks and the
                 # hourly pull loop they were deliberately kept out of
                 ("archived" if archived else "restored"),
                 jsonb(_raw_dict(_j(r.get("raw")))),
                 r.get("logo") or None, r.get("brand_color") or None,
                 r.get("url") or None,
                 # Why and when a disconnected bank went. The reason is what
                 # refuses a reconnect that would bring its accounts in
                 # twice (a newer link adopted them) or bring back one the
                 # owner deleted, and what lets a new bridge token revive
                 # only the banks that went down with the bridge; the date
                 # is where its catch-up pull starts. Dropped, every
                 # disconnected bank restores as an ordinary one and those
                 # refusals stop. Only a disconnected row carries them: a
                 # live connection restores as a shell, and a stray reason
                 # on it would mean nothing. resync_from stays behind on
                 # purpose — it is this instance's own pull bookkeeping.
                 (r.get("archived_reason") or None) if archived else None,
                 _ts(r.get("archived_at")) if archived else None,
                 # when the bank was linked is the household's history, not
                 # this instance's; an archive without it restores as now
                 _ts(r.get("linked_at")))])

        # the merchant ROWS come before the transactions and aliases that
        # point at them; an archive with no merchants file restores the
        # rest, and the nightly resolve rebuilds identity from the raw
        # records. Bare ON CONFLICT: merchants also has a partial unique
        # index on plaid_entity_id, so an arbiter naming only the PK would
        # let a row with a live entity id raise and abort the restore.
        #
        # A restore MERGES into the tenant, and a household that linked its
        # banks before restoring already holds a merchant for every entity
        # the aggregator named, under an id of its own. The archive's row
        # for the same entity is the same merchant: it is not inserted, its
        # id is mapped onto the live one, and everything later in the
        # archive that names it (transactions, aliases, 1099 marks, merge
        # pointers, merge decisions) follows the map. Without the map, every
        # pointer at a folded merchant would look dangling and be dropped,
        # undoing merges and resurfacing declined offers.
        live_entities = {
            r["plaid_entity_id"]: r["id"] for r in conn.execute(
                "SELECT id::text AS id, plaid_entity_id FROM merchants "
                "WHERE plaid_entity_id IS NOT NULL").fetchall()}
        merchant_map: dict[str, str] = {}
        folded: list[tuple[str, dict]] = []
        # A machine's merchant name keeps only the last four of a card
        # number, as the resolver writes it; a name a person gave (on the
        # merchant, or as a manual alias's canonical) stays as they typed
        # it. `machine_cut` maps each live machine name cut here, lowered,
        # to its cut spelling: the rules and 1099 marks keyed on the whole
        # name follow it below.
        persons_names = _persons_names(conn, z)
        machine_cut: dict[str, str] = {}
        persons_live: set[str] = set()
        for r in _rows(z, "merchants.csv"):
            if not r.get("id") or not r.get("name"):
                out.refuse("merchants", "malformed")
                continue
            m_name = _descriptor(r.get("name"))
            m_source = r.get("name_source") or "layer1"
            live_row = not r.get("merged_into")
            if (m_source == "manual"
                    or (isinstance(m_name, str)
                        and m_name.lower() in persons_names)):
                if live_row and isinstance(m_name, str):
                    persons_live.add(m_name.lower())
            else:
                cut_name = card_number.cut(m_name)
                if live_row and cut_name != m_name:
                    machine_cut[m_name.lower()] = cut_name
                m_name = cut_name
            aid = str(r["id"]).strip().lower()
            entity = r.get("plaid_entity_id") or None
            live = live_entities.get(entity) if entity else None
            if live is not None and live != aid:
                merchant_map[aid] = live
                folded.append((live, r))
                out.refuse("merchants", "merged")
                continue
            landed = out.land(conn, "merchants",
                """INSERT INTO merchants (id, plaid_entity_id, name, name_source,
                       kind, logo_url, website, phone, mcc, parent_id,
                       merged_into, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                [(r["id"], entity,
                 # the same clamp the ledger's descriptors get: this name is
                 # matched word by word against them
                 m_name, m_source,
                 r.get("kind") or "merchant", r.get("logo_url") or None,
                 r.get("website") or None, r.get("phone") or None,
                 r.get("mcc") or None, r.get("parent_id") or None,
                 r.get("merged_into") or None, _ts(r.get("created_at")))])
            if landed[0] is not None and entity:
                live_entities[entity] = aid
        if merchant_map:
            _fold_mapped_merchants(conn, merchant_map, folded)

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
                out.refuse("business_entity", "malformed")
                continue
            out.land(conn, "business_entity",
                """INSERT INTO business_entity (id, name, structure, state,
                       formation_date, business_start_date, ein_last4,
                       registered_agent, fiscal_year_end, status,
                       home_office_sqft, income_tax_rate, filing_status,
                       tax_reserve_account_id, archived_at, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(r["id"], r.get("name"), r.get("structure"),
                 r.get("state") or None,
                 as_date(r["formation_date"]) if r.get("formation_date") else None,
                 as_date(r["business_start_date"]) if r.get("business_start_date") else None,
                 r.get("ein_last4") or None, r.get("registered_agent") or None,
                 r.get("fiscal_year_end") or None, r.get("status") or "active",
                 _i(r.get("home_office_sqft")), _f(r.get("income_tax_rate")),
                 r.get("filing_status") or None,
                 # The export is SELECT *, so the ZIP carries this; without it
                 # a round trip forgets which account the tax money sits in.
                 _account_id(r.get("tax_reserve_account_id")) or None,
                 # when the business was closed — an archived entity must
                 # restore as archived-on-the-same-date, not freshly closed.
                 # One archived before the date was kept was last touched
                 # by the archiving itself, so that is its date, as
                 # migration 089 dated the stored ones
                 r.get("archived_at") or (
                     _ts(r.get("updated_at"))
                     if (r.get("status") or "") == "archived" else None),
                 # when the business was set up is its history, not this
                 # instance's; an archive without it restores as now
                 _ts(r.get("created_at")))])

        # An archive written table by table, each at its own moment, can hold
        # a row whose parent it missed: an account opened after the
        # connections were read, a business deleted after the ledger was. The
        # export now reads one snapshot, but archives from before that
        # exist, and one dangling pointer would roll back the whole restore.
        # So parents are checked against what is here: a pointer to a
        # missing business or connection is dropped (what deleting that
        # parent does to it anyway), and a row that cannot exist without
        # its missing parent is skipped.
        known_entities = {str(r["id"]) for r in conn.execute(
            "SELECT id FROM business_entity").fetchall()}
        known_items = {r["id"] for r in conn.execute(
            "SELECT id FROM items").fetchall()}

        def _entity(v):
            v = (v or "").strip().lower()
            return v if v in known_entities else None

        # ids the archive spells with a comma, and the name each restores
        # under — the settings keys naming them are rewritten to match
        renamed: dict[str, str] = {}
        # the accounts this restore actually created, for the merge below
        landed_accounts: set[str] = set()
        # hand-made accounts whose id still spells a full account number
        numbered_ids: list[str] = []
        for r in _rows(z, "accounts.csv"):
            if not r.get("id"):
                out.refuse("accounts", "malformed")
                continue
            if _account_id(r["id"]) != r["id"]:
                renamed[r["id"]] = _account_id(r["id"])
            a_name = r.get("name")
            # a display name is typed by a person whatever the account's
            # source, so a card number typed into one is cut on every
            # account, as the rename door and `oikonome migrate` cut it
            a_display = _account_sources.stored_account_name(
                r.get("display_name") or None)
            a_official = r.get("official_name") or None
            if r.get("item_id") == "manual":
                # A folder import named the account after its file, and an
                # archive written before the import cut the number from
                # that name brings the full account number back with it —
                # typed in groups too, in any of its names. `oikonome
                # migrate` makes the same cut to stored names
                # (base.mask_manual_account_names).
                a_name, a_display, a_official = (
                    _account_sources.stored_account_name(v)
                    for v in (a_name, a_display, a_official))
                # a hand-made id spelling a full number is re-keyed once
                # the rows have landed (below); any other keeps it, and
                # is named
                restored_id = str(_account_id(r["id"]) or "")
                if (card_number.LONG_RUN_RE.search(restored_id)
                        and not _sync_base.NUMBERED_MANUAL_ID_RE.search(
                            restored_id)):
                    numbered_ids.append(a_name or "an account")
            landed = out.land(conn, "accounts",
                """INSERT INTO accounts (id, item_id, name, display_name,
                       official_name, type, subtype, mask, balance_current,
                       balance_available, currency, entity_id, owner, raw,
                       user_removed_at, type_user_set, balance_limit)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(_account_id(r["id"]),
                 r.get("item_id") if r.get("item_id") in known_items else None,
                 a_name, a_display, a_official,
                 r.get("type"), r.get("subtype") or None, r.get("mask") or None,
                 # _f, not truthiness — a non-string 0 must
                 # restore as 0, never NULL
                 _balance(r.get("balance_current")),
                 _balance(r.get("balance_available")),
                 r.get("currency") or None, _entity(r.get("entity_id")),
                 # the account's default owner label ("yours/mine/ours"); an
                 # archive without the column restores as unattributed (None).
                 r.get("owner") or None,
                 jsonb(scrub_account_raw(_raw_dict(_j(r.get("raw"))))),
                 # user_removed_at (the soft-hide): without it an
                 # export→restore round trip UN-hides every account the user
                 # hid — back in the lists, back in the money aggregates,
                 # with no indication anything changed.
                 _ts(r.get("user_removed_at")),
                 # type_user_set stops sync from overwriting a type the USER
                 # corrected; landed FALSE, the restored account looks right
                 # until the next aggregator sync silently reclassifies it.
                 # balance_limit rides along: it is what credit-utilisation
                 # reads.
                 _b(r.get("type_user_set")),
                 _balance(r.get("balance_limit")))])
            if landed[0] is not None:
                landed_accounts.add(landed[0])
        if numbered_ids:
            # the id is permanent (history, adoptions and settings all name
            # it), so it is not re-keyed; the note names each account by
            # its masked name, never by the id that holds the number
            notes.append(
                f"{_plural(len(numbered_ids), 'account', 'accounts')} "
                f"created from a file named after its full account number "
                f"still carr{'ies' if len(numbered_ids) == 1 else 'y'} that "
                f"number in an internal id that travels in exports: "
                + ", ".join(numbered_ids)
                + ". The importing guide says how to replace one")

        # restored accounts may carry entity ids; the connection's entity
        # scope was stamped at checkout, before they existed
        from ..db import tenancy as _tenancy
        _tenancy.set_entity_scope(conn)
        # the accounts a restored stand-in note may name: those here now,
        # under any id an adoption here moved them from
        noted_accounts = {r["id"] for r in conn.execute(
            "SELECT id FROM accounts").fetchall()} | {
            r["old_account_id"] for r in conn.execute(
                "SELECT old_account_id FROM account_adoptions").fetchall()}

        def _txn_raw(r, amount_missing=False):
            raw = scrub_transaction_raw(_raw_dict(_j(r.get("raw"))))
            if amount_missing:
                raw["_amount_missing"] = True
            # an archive written before file rows' raw was masked still
            # carries the full number in the cell the name was cut from
            if _sync_base.is_file_import_id(r.get("id")):
                raw = _sync_base.mask_file_raw(raw, r.get("name"),
                                               r.get("merchant_name"))
            # the row an earlier guard noted this one was carried from is
            # named by id with no key to follow: it follows that row to
            # the id it lands under here (see base._MENTIONS_SQL)
            was = raw.get("_carried_from")
            if carried and isinstance(was, str) and was in carried:
                raw["_carried_from"] = carried[was]
            return _sync_base.stood_in_for_restore(
                raw, _account_id, noted_accounts)
        # Transactions are BATCHED. Every other member here is
        # bounded by user actions — accounts, bills, notes — but this one is
        # the ledger: a restore can be tens of thousands of rows, and a
        # per-row execute() pays a full client/server round trip for each.
        # psycopg pipelines executemany, so the same INSERT ... ON CONFLICT
        # DO NOTHING travels in chunks instead. Memory is unchanged: _rows
        # still streams, and only CHUNK rows are held at a time.
        def _txn_params(r):
            # every live door writes a number here, and every ledger reader
            # turns it into cents — the duplicate matchers first, so a blank
            # row would fail every later import onto its account. Leaving
            # the row out would drop its receipt, note and split as orphans,
            # so it lands at 0 and its raw says the archive had no amount.
            amount = _money(r.get("amount"))  # 0 is a real amount
            amount_missing = amount is None
            m_name, m_aside = _merchant_name_cells(r)
            raw = _txn_raw(r, amount_missing)
            merchant_id = _known_merchant(r.get("merchant_id"),
                                          known_merchants, merchant_map)
            # the migrations that rewrote stored rows, applied to this one
            # (upgrade_archived_txn): an archive written before any of them
            # lands in the shape they left the household's own rows in
            c = upgrade_archived_txn({
                "name": _descriptor(r.get("name")),
                "merchant_name": m_name,
                "merchant_name_set_aside": m_aside,
                "merchant_outlet": _descriptor(r.get("merchant_outlet")) or None,
                "amount": 0.0 if amount_missing else amount,
                "removed": int(r.get("removed") or 0),
                "category_primary": r.get("category_primary") or None,
                "category_detailed": r.get("category_detailed") or None,
                # The aggregator's OWN verdict and its confidence. These
                # are what llm_categorize's low-confidence gate reads,
                # so dropping them would silently change which rows the
                # model revisits. The confidence is a TEXT tier
                # ('VERY_HIGH'/'HIGH'/'LOW'/…), never a number —
                # float-coercing it would raise on any Plaid row and, the
                # restore being one transaction, abort all of it
                "category_plaid": r.get("category_plaid") or None,
                "category_plaid_detailed":
                    r.get("category_plaid_detailed") or None,
                "category_plaid_confidence":
                    r.get("category_plaid_confidence") or None,
                "category_override":
                    _descriptor(r.get("category_override")) or None,
                # migration 098: the aggregator's facts as columns
                "category_source": r.get("category_source") or None,
                "check_number": _descriptor(r.get("check_number")) or None,
                "payment_channel": r.get("payment_channel") or None,
                "payment_processor": r.get("payment_processor") or None,
                "location_city": _descriptor(r.get("location_city")) or None,
                "location_region": r.get("location_region") or None,
                "location_address":
                    _descriptor(r.get("location_address")) or None,
                "location_postal": r.get("location_postal") or None,
                "location_lat": (_f(r.get("location_lat"))
                                 if r.get("location_lat") not in (None, "")
                                 else None),
                "location_lon": (_f(r.get("location_lon"))
                                 if r.get("location_lon") not in (None, "")
                                 else None),
                "location_store": r.get("location_store") or None,
                "mcc": r.get("mcc") or None,
                "authorized_date": as_date(r.get("authorized_date") or None),
            }, raw, on_card=_account_id(r.get("account_id")) in cards,
                has_merchant=merchant_id is not None)
            t_name, m_name, m_aside, outlet = (
                c["name"], c["merchant_name"], c["merchant_name_set_aside"],
                c["merchant_outlet"])
            # a file import's names keep only the last four of any long
            # digit run, as the upsert writes them — an archive written
            # before that rule would otherwise bring a full account or card
            # number back into the most travelled field of the row
            if _sync_base.is_file_import_id(r["id"]):
                t_name, m_name, m_aside, outlet = (
                    card_number.cut(v)
                    for v in (t_name, m_name, m_aside, outlet))
            return (r["id"], _account_id(r.get("account_id")) or None,
                    as_date(r["date"]),
                    c["authorized_date"],
                    c["amount"],
                    t_name,
                    m_name,
                    c["category_primary"],
                    c["category_detailed"],
                    c["category_plaid"],
                    c["category_plaid_detailed"],
                    c["category_plaid_confidence"],
                    c["category_override"],
                    int(r.get("pending") or 0),
                    r.get("pending_transaction_id") or None,
                    c["payment_channel"],
                    c["removed"],
                    _entity(r.get("entity_id")),   # per-txn override
                    # per-transaction owner exception (effective owner is
                    # COALESCE(owner_override, account.owner)) and the
                    # statement-derived outlet brand (payee display/search,
                    # the merchant rename screen). Both ride the SELECT *
                    # export.
                    r.get("owner_override") or None,
                    outlet,
                    # the aggregator's name for this one charge, which the
                    # resolver set aside in favour of the merchant the bank
                    # line means. NULL when the archive never had one —
                    # dropping it would re-key the row on a name the ledger
                    # had already judged a misreading.
                    m_aside,
                    # the merchant ROW — but only when the archive
                    # actually carried it. A pointer at a merchant that is
                    # not here is worse than no pointer: identity reads
                    # crash on it and the ledger shows nothing at all.
                    merchant_id,
                    jsonb(raw),
                    c["category_source"],
                    c["check_number"],
                    c["payment_processor"],
                    c["location_city"],
                    c["location_region"],
                    c["location_address"],
                    c["location_postal"],
                    c["location_lat"],
                    c["location_lon"],
                    c["location_store"],
                    c["mcc"],
                    r.get("override_source") or None,
                    # a hold the person retired stays retired through the next sync
                    r.get("retired_at") or None)

        # EVERY column the export writes. The export is `SELECT *`, so a
        # column left out here is silently lost on a round trip, degrading
        # categorisation and bill matching in ways nothing reports.
        _TXN_SQL = """INSERT INTO transactions (id, account_id, date,
                          authorized_date, amount,
                          name, merchant_name, category_primary,
                          category_detailed, category_plaid,
                          category_plaid_detailed, category_plaid_confidence,
                          category_override, pending, pending_transaction_id,
                          payment_channel, removed, entity_id, owner_override,
                          merchant_outlet, merchant_name_set_aside,
                          merchant_id, raw,
                          category_source, check_number, payment_processor,
                          location_city, location_region, location_address,
                          location_postal, location_lat, location_lon,
                          location_store, mcc, override_source, retired_at)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                              %s,%s,%s,%s,%s,%s,%s,%s,
                              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (tenant_id, id) DO NOTHING
                      RETURNING id"""
        CHUNK = 1000
        batch: list = []
        overridden: list[str] = []
        # beside `batch`, whether each row lands at 0 for want of an amount
        batch_repaired: list[bool] = []

        def _flush():
            nonlocal batch, batch_repaired
            landed = out.land(conn, "transactions", _TXN_SQL, batch)
            # a repaired row counts only when ITS insert wrote it: one the
            # destination already held keeps whatever amount it has there,
            # and of an id the archive carries twice only the copy that
            # landed says whether the amount was missing
            out.repair("transactions", "amount_repaired", sum(
                1 for got, fixed in zip(landed, batch_repaired)
                if got is not None and fixed))
            # the rows this restore wrote carrying a category override,
            # which the rules derived from overrides are derived over
            overridden.extend(got for got, params in zip(landed, batch)
                              if got is not None and params[12])
            batch = []
            batch_repaired = []

        known_accounts = {r["id"] for r in conn.execute(
            "SELECT id FROM accounts").fetchall()}
        # the context that lets a bare "payment" read as a card settlement
        cards = _sync_base.credit_account_ids(conn)
        # A PERSON's category on a row the destination already holds under
        # the same id (a statement imported on both sides hashes to the same
        # ids) replaces a bill's, a store's or no override there; the insert
        # above skips the row whole, so these are applied after it. Only
        # the id and the category are held, a few strings per correction.
        user_ids: list[str] = []
        user_cats: list = []
        for r in _rows(z, "transactions.csv"):
            if not r.get("id") or not r.get("date"):
                out.refuse("transactions", "malformed")
                continue
            aid = _account_id(r.get("account_id"))
            if aid and aid not in known_accounts:
                out.refuse("transactions", "orphaned")
                continue
            try:
                params = _txn_params(r)
            except _AmountOutOfRange:
                out.refuse("transactions", "out_of_range")
                continue
            # lands at 0, see _txn_params
            batch_repaired.append(_f(r.get("amount")) is None)
            batch.append(params)
            if r.get("override_source") == "user":
                user_ids.append(r["id"])
                user_cats.append(_descriptor(r.get("category_override")) or None)
            if len(batch) >= CHUNK:
                _flush()
        _flush()
        if user_ids:
            conn.execute(
                """UPDATE transactions t
                      SET category_override = v.cat, override_source = 'user'
                     FROM unnest(%s::text[], %s::text[]) AS v(id, cat)
                    WHERE t.id = v.id
                      AND t.override_source IS DISTINCT FROM 'user'""",
                (user_ids, user_cats))
        del user_ids, user_cats

        # bills.csv since the rename; older backups say recurring.csv
        bills_member = ("bills.csv" if "bills.csv" in z.namelist()
                        else "recurring.csv")
        bad_due = 0
        # A restore is held to the same ceiling on active bills as a save:
        # an archive is just another way to put rows in front of every
        # Today load. Bills past it still come back, archived — the
        # household loses nothing and can bring them back one by one —
        # and the note says how many.
        _bills.lock_active_bills(conn)
        active_room = _bills.MAX_ACTIVE_BILLS - _bills.active_bill_count(conn)
        over_cap = 0
        # A detected bill is named by the ledger, and its id is the slug of
        # its payee: one whose payee spells a card number lands under the
        # cut name and that name's id, as the bill pass names it now. Where
        # each archived bill lands is decided for all of them at once, before
        # any is written (_plan_bill_moves), so the order the archive lists
        # them in never changes the answer. What names a bill by its archived
        # id or payee (pins, offers, two settings) follows `bill_ids` /
        # `bill_payees` onto the one it landed as.
        bill_ids: dict[str, str] = {}
        bill_payees: dict[str, str] = {}
        bill_rows: list[tuple] = []
        for r in _rows(z, bills_member):
            if not r.get("id"):
                out.refuse("bills", "malformed")
                continue
            try:
                amount = _money(r.get("amount"))
                monthly = _money(r.get("monthly_amount"))
            except _AmountOutOfRange:
                out.refuse("bills", "out_of_range")
                continue
            bill_raw = clean_bill_raw(_j(r.get("raw")))
            # an archive made before the mean-year rate carries the old
            # 52-week figure; restate it as migration 183 did for stored rows
            monthly = _bills.restated_monthly(amount, monthly, bill_raw)
            # A due date past the years the product reads (a bill rolled
            # on a centuries-long cycle by an older install) would abort
            # the whole archive here, and every nightly pass after it; the
            # bill is kept without its due date, and the note says so.
            due_on = None
            if r.get("due_on"):
                due_on = _dt.date.fromisoformat(str(r["due_on"])[:10])
                try:
                    as_date(due_on)
                except ValueError:
                    due_on = None
                    bill_raw.pop("dueOn", None)
                    bad_due += 1
            bill_rows.append((r, amount, monthly, bill_raw, due_on))
        bill_plan = _plan_bill_moves(
            conn, [(r["id"], _descriptor(r.get("payee")),
                    bill_raw.get("source") == "manual")
                   for r, _a, _m, bill_raw, _d in bill_rows])
        for (r, amount, monthly, bill_raw, due_on), (bill_id, payee, held) \
                in zip(bill_rows, bill_plan):
            archived_id, archived_payee = r["id"], _descriptor(r.get("payee"))
            if held is not None:
                bill_ids[archived_id] = held[0]
                bill_payees[archived_payee] = held[1]
                out.refuse("bills", "present")
                continue
            if (bill_id, payee) != (archived_id, archived_payee):
                bill_ids[archived_id] = bill_id
                bill_payees[archived_payee] = payee
            bill_active = int(r.get("active") or 1)
            capped = bool(bill_active and active_room <= 0)
            if capped:
                bill_active = 0
            landed = out.land(conn, "bills",
                """INSERT INTO bills (id, type, payee, amount, frequency,
                       monthly_amount, due_on, category, merchant, is_completed,
                       active, synced_at, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(bill_id, r.get("type"), payee,
                 amount,
                 r.get("frequency") or None,
                 monthly,
                 due_on,
                 r.get("category") or None, r.get("merchant") or None,
                 int(r.get("is_completed") or 0), bill_active,
                 jsonb(bill_raw))])
            wrote = 1 if landed[0] is not None else 0
            if bill_active:
                active_room -= wrote
            if capped:
                # written, archived: the note below says how many
                over_cap += wrote
        if over_cap:
            notes.append(f"{over_cap} active bill{'s' if over_cap != 1 else ''}"
                         f" past the limit of {_bills.MAX_ACTIVE_BILLS} "
                         "came back archived")
        if bad_due:
            notes.append(f"{bad_due} bill{'s' if bad_due != 1 else ''} "
                         "carried a due date outside the supported years "
                         "and came back without one")

        # The detected-bill offers and, above all, the ANSWERS to them —
        # the same reasoning as the merchant merge offers below. A rejection
        # is the household saying "that run of charges is not a bill I want
        # tracked", and the detector's id is derived from the evidence
        # (payee group, cycle, rounded amount), so the rejected row is the
        # only thing keeping the offer from coming back: the nightly pass
        # inserts ON CONFLICT DO NOTHING, so it re-derives the same id and
        # writes nothing. Left out of a restore, every candidate the person
        # has already turned down is offered again the first night on the
        # destination, and an approved one is offered again beside the bill
        # it already became.
        #
        # Undecided rows travel too, and cost nothing: the nightly recomputes
        # them under the same ids. (The exception is an offer derived from an
        # aggregator's own recurring stream, whose id IS the stream id —
        # a re-linked connection numbers its streams afresh, so that one can
        # be re-offered whatever this does.) A proposal names its bill by
        # PAYEE STRING rather than by id, and the bills above carry their
        # payees, so nothing has to be re-pointed; where the bill is missing
        # entirely, approving already answers "that bill no longer exists"
        # and leaves the offer standing.
        for r in _rows(z, "bill_proposals.csv"):
            if not r.get("id") or not r.get("payee"):
                out.refuse("bill_proposals", "malformed")
                continue
            try:
                amount = _money(r.get("amount"))
            except _AmountOutOfRange:
                out.refuse("bill_proposals", "out_of_range")
                continue
            p_payee, p_evidence = _offer_cut(
                conn, r.get("kind") or None, _descriptor(r.get("payee")),
                _raw_dict(_j(r.get("evidence"))), bill_payees)
            out.land(conn, "bill_proposals",
                """INSERT INTO bill_proposals (id, kind, bill_type, payee,
                       amount, frequency, interval, next_due, evidence,
                       llm_tag, status, created_at, decided_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           COALESCE(%s, now()),%s)
                   ON CONFLICT (tenant_id, id) DO UPDATE
                      SET status = EXCLUDED.status,
                          decided_at = COALESCE(EXCLUDED.decided_at, now())
                    WHERE bill_proposals.status = 'pending'
                      AND EXCLUDED.status IN ('approved', 'rejected')
                   RETURNING id""",
                # the destination's nightly may already have offered the
                # same candidate under the same derived id: an offer it is
                # still asking about takes the household's archived answer
                [(r["id"], r.get("kind") or None,
                 r.get("bill_type") or "occurrence",
                 p_payee, amount,
                 r.get("frequency") or None, _i(r.get("interval"), 1),
                 as_date(r["next_due"]) if r.get("next_due") else None,
                 # read with `.get` wherever it is used, so a cell carrying
                 # a list or a number becomes no evidence rather than a
                 # crash on the Bills screen
                 jsonb(p_evidence),
                 r.get("llm_tag") or None,
                 # a hand-edited status the app never writes would sit in the
                 # table invisible to every query that reads it; anything
                 # unrecognised is simply an offer nobody has answered yet
                 (r.get("status") if r.get("status") in
                  ("pending", "approved", "rejected", "auto", "superseded")
                  else "pending"),
                 _ts(r.get("created_at")), _ts(r.get("decided_at")))])

        # The import batches the restored ledger rows are tagged with. Batch
        # ownership rides in each transaction's `raw._batches`, which the
        # rows above carry, so without this table the tags point at batches
        # that are not here: Recent imports lists nothing, and the one-click
        # undo for a file the person imported — the only way back out of a
        # bad import, since those rows come from no feed — is gone for good,
        # while the rows themselves keep counting in every total. Restoring
        # the record makes the door work on the destination exactly as it did
        # on the source; `account_id` is the account the file was imported
        # into, for display, and is not a pointer anything follows.
        for r in _rows(z, "import_batches.csv"):
            if not r.get("id") or not r.get("source"):
                out.refuse("import_batches", "malformed")
                continue
            out.land(conn, "import_batches",
                """INSERT INTO import_batches (id, source, account_id,
                       filename, row_count, created_at)
                   VALUES (%s,%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(r["id"], r["source"],
                  _account_id(r.get("account_id")) or None,
                  card_number.cut_as_slug_reads(
                      _descriptor(r.get("filename"))) or None,
                  _i(r.get("row_count"), 0) or 0, _ts(r.get("created_at")))])

        link_marked = None
        # the pins THIS restore inserted — the only ones an archive without
        # the link mark may be classified over (below)
        restored_pins: list[str] = []
        for r in _rows(z, "manual_categories.csv"):
            if link_marked is None:
                link_marked = "link_made" in r
            # link_prior's empty string (the person's "no category" pin the
            # link replaced) and NULL (no pin replaced) are one empty cell
            # in a CSV; an archive that says which it was carries
            # link_prior_set beside it. Without it an empty cell means none,
            # as it always has.
            prior = r.get("link_prior") or None
            if prior is None and _b(r.get("link_prior_set")):
                prior = ""
            if r.get("category") is None:
                out.refuse("manual_categories", "malformed")
            elif _txn_exists(conn, r.get("transaction_id")):
                # bill_id (a bill's transaction category, not a person's
                # correction) travels too, else a restore turns every
                # bill-stamped row into a hand edit the bill can't revert.
                # So does the link mark and the pin it replaced: without
                # them an unlink after a restore would leave a link-made
                # transfer behind, or lose the person's category the link
                # had set aside. An archive written before pins carried the
                # mark is classified below, once the pairs are back.
                landed = out.land(conn, "manual_categories",
                    """INSERT INTO manual_categories (transaction_id, category,
                                                      bill_id, set_at,
                                                      link_made, link_prior)
                       VALUES (%s,%s,%s, COALESCE(%s, now()), %s, %s)
                       ON CONFLICT (tenant_id, transaction_id) DO UPDATE
                          SET category = EXCLUDED.category,
                              bill_id = NULL,
                              set_at = EXCLUDED.set_at,
                              link_made = FALSE,
                              link_prior = EXCLUDED.link_prior
                        WHERE manual_categories.bill_id IS NOT NULL
                          AND NOT manual_categories.link_made
                          AND EXCLUDED.bill_id IS NULL
                          AND NOT EXCLUDED.link_made
                       RETURNING transaction_id""",
                    # a pin a BILL wrote on the destination gives way to the
                    # person's own pin for the same row from the archive; a
                    # person's pin already here, or a link's, stays
                    [(r["transaction_id"], r["category"],
                      # a bill this restore landed under its cut id
                      bill_ids.get(r.get("bill_id"), r.get("bill_id") or None),
                      _ts(r.get("set_at")),
                      _b(r.get("link_made")), prior)])
                if landed[0] is not None:
                    restored_pins.append(landed[0])
            else:
                out.refuse("manual_categories", "orphaned")
        # an archive written before overrides carried their kind: classify
        # them from the manual_categories rows just restored and the store
        # prefixes, exactly as migration 127 did for live ledgers
        from ..engine import categories as _cats
        _cats.backfill_override_source(conn)
        # an archive written before the feed's withdrawals were stamped:
        # recognise them by the rule migration 157 applied to live ledgers
        from .base import stamp_unstamped_feed_withdrawals
        stamp_unstamped_feed_withdrawals(conn)

        for r in _rows(z, "reimbursements.csv"):
            if (_txn_exists(conn, r.get("expense_id"))
                    and _txn_exists(conn, r.get("reimburse_id"))):
                try:
                    amount = _money(r.get("amount"))
                except _AmountOutOfRange:
                    out.refuse("reimbursements", "out_of_range")
                    continue
                # created_at is what an archive without the link mark is
                # classified by (below). Without it the pair gets
                # clock_timestamp(), later than the now() any undated pin
                # was given: the tie breaks toward a pin looking hand-set,
                # so an unlink may leave a link-made transfer behind but
                # never clears one a person chose.
                out.land(conn, "reimbursements",
                    """INSERT INTO reimbursements (expense_id, reimburse_id,
                           partial, amount, created_at)
                       VALUES (%s,%s,%s,%s, COALESCE(%s, clock_timestamp()))
                       ON CONFLICT (tenant_id, expense_id, reimburse_id)
                       DO NOTHING
                       RETURNING expense_id""",
                    [(r["expense_id"], r["reimburse_id"],
                      _i(r.get("partial"), 0) or 0, amount,
                      _ts(r.get("created_at")))])
            else:
                out.refuse("reimbursements", "orphaned")
        if link_marked is False and restored_pins:
            from ..web.data import classify_link_made_pins
            classify_link_made_pins(conn, restored_pins)

        for r in _rows(z, "reimburse_flags.csv"):
            if _txn_exists(conn, r.get("txn_id")):
                try:
                    expected = _money(r.get("expected"))
                except _AmountOutOfRange:
                    out.refuse("reimburse_flags", "out_of_range")
                    continue
                out.land(conn, "reimburse_flags",
                    """INSERT INTO reimburse_flags (txn_id, partial, expected,
                                                   flagged_at)
                       VALUES (%s,%s,%s, COALESCE(%s, now()))
                       ON CONFLICT (tenant_id, txn_id) DO NOTHING
                       RETURNING txn_id""",
                    # when it was flagged is what the waiting list shows
                    [(r["txn_id"], _i(r.get("partial"), 0) or 0, expected,
                      _ts(r.get("flagged_at")))])
            else:
                out.refuse("reimburse_flags", "orphaned")

        # account_links IS the shadow set — without it a
        # round trip un-shadows every duplicate source (Plaid + SimpleFIN
        # of the same real account) and every money aggregate double-counts.
        # Guarded on the account existing so a partial archive cannot plant
        # dangling links.
        touched_groups: set[str] = set()
        link_accounts = {r["id"] for r in conn.execute(
            "SELECT id FROM accounts").fetchall()}
        for r in _rows(z, "account_links.csv"):
            if not (r.get("group_id") and r.get("account_id")):
                out.refuse("account_links", "malformed")
                continue
            if _account_id(r["account_id"]) not in link_accounts:
                out.refuse("account_links", "orphaned")
                continue
            touched_groups.add(str(r["group_id"]))
            out.land(conn, "account_links",
                """INSERT INTO account_links (group_id, account_id, home_rank)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (tenant_id, account_id) DO NOTHING
                   RETURNING account_id""",
                [(r["group_id"], _account_id(r["account_id"]),
                  _i(r.get("home_rank"), 0))])
        if touched_groups:
            _renumber_home_ranks(conn, sorted(touched_groups))

        # Which ids each account stood under before an adoption moved it.
        # A file row keeps the id it was hashed under, so a statement
        # re-imported after this restore finds its earlier rows only
        # through this lineage (sync/base FileImportIds); without it every
        # pre-feed line of the statement lands twice. Only the lineage is
        # loaded: each row arrives reconciled with no cutoff, so the
        # one-shot reconnect guard it once drove stays off here — this
        # instance never held that backfill. Bounded, and only for an
        # account the archive restored.
        lineage = 0
        adoptable = {r["id"] for r in conn.execute(
            "SELECT id FROM accounts WHERE item_id IS NOT NULL").fetchall()}
        for r in _rows(z, "account_adoptions.csv"):
            new_id = _account_id(r.get("account_id"))
            old_id = str(r.get("old_account_id") or "").strip()
            if not new_id or not old_id or old_id == new_id:
                out.refuse("account_adoptions", "malformed")
                continue
            if lineage >= _MAX_ADOPTION_LINEAGE:
                out.refuse("account_adoptions", "over_cap")
                continue
            if new_id not in adoptable:
                # the account is not here, or came back without the
                # connection the lineage names it under
                out.refuse("account_adoptions", "orphaned")
                continue
            landed = out.land(conn, "account_adoptions",
                """INSERT INTO account_adoptions
                       (account_id, old_account_id, item_id, adopted_at,
                        reconciled_at)
                   SELECT a.id, %s, a.item_id, COALESCE(%s, now()), now()
                     FROM accounts a
                    WHERE a.id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM account_adoptions x
                           WHERE x.account_id = a.id
                             AND x.old_account_id = %s)
                   ON CONFLICT DO NOTHING
                   RETURNING account_id""",
                [(old_id[:_MAX_ACCOUNT_ID], _ts(r.get("adopted_at")), new_id,
                  old_id[:_MAX_ACCOUNT_ID])])
            lineage += landed[0] is not None

        # Which account each source name fills (sync/account_sources).
        # Keyed rows are HMACs under the secret of the household that
        # wrote them, which never travels: restored into the same
        # household they match again; restored elsewhere they match
        # nothing, and its sources find their accounts by the stored-name
        # rows, asked once when a name leads to several. Only for an
        # account the archive restored; never re-pointed onto another.
        source_accounts = {r["id"] for r in conn.execute(
            "SELECT id FROM accounts").fetchall()}
        for r in _rows(z, "account_sources.csv"):
            door = str(r.get("door") or "").strip()
            digest = str(r.get("source_hmac") or "").strip()
            aid = _account_id(r.get("account_id"))
            if not (door and digest and aid) or len(door) > 32 \
                    or len(digest) > 80:
                out.refuse("account_sources", "malformed")
                continue
            if aid not in source_accounts:
                out.refuse("account_sources", "orphaned")
                continue
            # a stored-name row is a claim only while it is the digest of
            # the cut name its account stores; any other is pruned below
            # (account_sources.backfill), so it is refused, never counted
            if digest.startswith("c:") and not _account_sources.c_row_holds(
                    conn, digest, aid):
                out.refuse("account_sources", "malformed")
                continue
            out.land(conn, "account_sources",
                """INSERT INTO account_sources
                       (door, source_hmac, account_id, created_at)
                   VALUES (%s, %s, %s, COALESCE(%s, now()))
                   ON CONFLICT DO NOTHING
                   RETURNING account_id""",
                [(door, digest, aid, _ts(r.get("created_at")))])

        # Receipt images (base64 in the CSV) + tagged line
        # items — the Schedule-C expense-report input. Guarded on the
        # transaction / receipt existing, mirroring manual_categories.
        import base64 as _b64

        from ..engine import receipts as _receipts
        for r in _rows(z, "receipts.csv"):
            if not (r.get("id") and r.get("image")):
                out.refuse("receipts", "malformed")
                continue
            # a receipt with no transaction is one WAITING for its charge
            # (engine/receipt_match) and restores as waiting; one that names
            # a transaction the archive does not carry is still skipped,
            # never turned into a waiting receipt the household did not have
            if r.get("txn_id") and not _txn_exists(conn, r.get("txn_id")):
                out.refuse("receipts", "orphaned")
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
                out.refuse("receipts", "malformed")
                continue
            if len(r["image"]) > _receipts.MAX_IMAGE * 4 // 3 + 4:
                # cheaper than decoding first
                out.refuse("receipts", "over_cap")
                continue
            try:
                img = _b64.b64decode(r["image"], validate=True)
            except Exception:
                # corrupt cell — skip, not abort
                out.refuse("receipts", "malformed")
                continue
            if not img:
                out.refuse("receipts", "malformed")
                continue
            if len(img) > _receipts.MAX_IMAGE:
                out.refuse("receipts", "over_cap")
                continue
            kind = r.get("kind") or "receipt"
            if kind not in _receipts.KINDS:
                kind = "receipt"
            txn_id = r.get("txn_id") or None
            if txn_id is None and kind != "receipt":
                # only a receipt can wait
                out.refuse("receipts", "malformed")
                continue
            method = r.get("match_method") or None
            if method not in (None, "auto", "manual"):
                method = None
            # a waiting receipt keeps 'manual' only as the mark of a hand
            # match whose charge went (the matcher leaves it to the person);
            # a waiting 'auto' is not a state anything writes
            if txn_id is None and method != "manual":
                method = None
            parsed = (_receipt_parsed(_j(r.get("parsed")))
                      if r.get("parsed") else None)
            unmatched = _j(r.get("unmatched_txn_ids"))
            unmatched = ([str(x)[:200] for x in unmatched[:200]
                          if isinstance(x, (str, int))]
                         if isinstance(unmatched, list) else [])
            opt, opt_mime = _receipt_optimized(r)
            # the household's file storage is held to the same ceiling a
            # snap is; a receipt already here costs nothing new
            if not conn.execute("SELECT 1 FROM receipts WHERE id = %s",
                                (r["id"],)).fetchone():
                from ..engine import storage_quota as _sq
                try:
                    _sq.reserve(conn, len(img) + len(opt or b""))
                except _sq.StorageQuotaExceeded:
                    out.refuse("receipts", "over_cap")
                    continue
            out.land(conn, "receipts",
                """INSERT INTO receipts (id, txn_id, image, mime, kind,
                       status, parsed, error, created_at, parsed_at,
                       matched_at, match_method, unmatched_txn_ids,
                       image_optimized, optimized_mime)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                           COALESCE(%s, now()), %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(r["id"], txn_id, img, mime,
                 kind, r.get("status") or "uploaded",
                 jsonb(parsed) if parsed is not None else None,
                 r.get("error") or None,
                 _receipt_ts(r.get("created_at")),
                 _receipt_ts(r.get("parsed_at")),
                 (_receipt_ts(r.get("matched_at"))
                  if txn_id and method else None),
                 method, unmatched, opt, opt_mime)])

        for r in _rows(z, "receipt_items.csv"):
            if not r.get("receipt_id"):
                out.refuse("receipt_items", "malformed")
                continue
            try:
                amount = _money(r.get("amount"))
            except _AmountOutOfRange:
                out.refuse("receipt_items", "out_of_range")
                continue
            if amount is None:
                # a line item is a priced line: reading a receipt drops any
                # line without a finite amount, and every client renders
                # the amount as a number — so the archive may not bring
                # in a shape the app itself never stores
                out.refuse("receipt_items", "malformed")
                continue
            if not conn.execute("SELECT 1 FROM receipts WHERE id = %s",
                                (r["receipt_id"],)).fetchone():
                out.refuse("receipt_items", "orphaned")
                continue
            out.land(conn, "receipt_items",
                """INSERT INTO receipt_items (receipt_id, line, description,
                       qty, amount, tag)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, receipt_id, line) DO NOTHING
                   RETURNING receipt_id""",
                [(r["receipt_id"], _i(r.get("line"), 0),
                  r.get("description") or "", _f(r.get("qty")),
                  amount, r.get("tag") or "")])

        for r in _rows(z, "business_flags.csv"):
            if _txn_exists(conn, r.get("txn_id")):
                out.land(conn, "business_flags",
                    """INSERT INTO business_flags (txn_id, flagged_at)
                       VALUES (%s, COALESCE(%s, now()))
                       ON CONFLICT (tenant_id, txn_id) DO NOTHING
                       RETURNING txn_id""",
                    [(r["txn_id"], _ts(r.get("flagged_at")))])
            else:
                out.refuse("business_flags", "orphaned")

        # ---- business tables (entities restored above,
        # transactions above — these depend on both) ----
        for r in _rows(z, "entity_membership.csv"):
            if not r.get("id") or not r.get("entity_id"):
                out.refuse("entity_membership", "malformed")
                continue
            if _entity(r["entity_id"]) is None:
                out.refuse("entity_membership", "orphaned")
                continue
            # The household mark comes back with the member: without it the
            # estimate silently widens to every member's tax and the capital
            # account loses the household's share. A business has at most one
            # household member (the database enforces it), so a mark arrives
            # only while its business has none — the destination's own mark,
            # or the archive's first, is the one kept. A mark is taken under
            # the same lock the Settings door marks under: the NOT EXISTS
            # guard alone cannot see a mark another transaction has not
            # committed, and the unique index would then refuse this insert
            # and abort the whole restore.
            if _b(r.get("is_household")):
                _hold_household_mark(conn, r["entity_id"])
            out.land(conn, "entity_membership",
                """INSERT INTO entity_membership (id, entity_id, member_name,
                       ownership_pct, is_manager, created_at, is_household)
                   VALUES (%s,%s,%s,%s,%s, COALESCE(%s, now()),
                           %s AND NOT EXISTS (
                               SELECT 1 FROM entity_membership
                                WHERE entity_id = %s AND is_household))
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(r["id"], r["entity_id"], r.get("member_name"),
                  _f(r.get("ownership_pct")), _b(r.get("is_manager")),
                  _ts(r.get("created_at")),
                  _b(r.get("is_household")), r["entity_id"])])

        # One fronted movement (a contribution or a reimbursement) per
        # transaction: the database refuses a second one on an install
        # that has the index, and the engine refuses one everywhere. An
        # archive from an install that predates the rule can carry two;
        # the one kept is the one the upgrade keeps — the movement for the
        # business the cost is booked to now (the transaction's own
        # assignment, else its account's), and the earliest recorded only
        # when neither matches — so the result does not depend on the
        # archive's row order or on whether this install has the index yet.
        fronted_kinds = ("contribution", "reimbursement")
        movements = [r for r in _rows(z, "equity_movement.csv")]
        booked = {r["id"]: str(r["ent"]) if r["ent"] else None
                  for r in conn.execute(
                      "SELECT t.id, COALESCE(t.entity_id, a.entity_id) AS ent"
                      "  FROM transactions t"
                      "  LEFT JOIN accounts a ON a.id = t.account_id"
                      " WHERE t.id = ANY(%s)",
                      ([m["txn_id"] for m in movements
                        if m.get("txn_id")],)).fetchall()}

        def _keep_first(r):
            ent = (r.get("entity_id") or "").strip().lower() or None
            return (booked.get(r.get("txn_id")) != ent,
                    str(r.get("created_at") or ""), str(r.get("id") or ""))
        movements.sort(key=_keep_first)
        fronted_clash = 0
        for r in movements:
            if not (r.get("id") and r.get("entity_id") and r.get("date")
                    and r.get("kind") and _f(r.get("amount")) is not None):
                out.refuse("equity_movement", "malformed")
                continue
            if _entity(r["entity_id"]) is None:
                out.refuse("equity_movement", "orphaned")
                continue
            try:
                amount = _money(r.get("amount"))
            except _AmountOutOfRange:
                out.refuse("equity_movement", "out_of_range")
                continue
            m_txn = (r.get("txn_id") if _txn_exists(conn, r.get("txn_id"))
                     else None)
            if (m_txn and r.get("kind") in fronted_kinds
                    and conn.execute(
                        "SELECT 1 FROM equity_movement WHERE txn_id = %s"
                        " AND kind = ANY(%s) AND id::text <> %s",
                        (m_txn, list(fronted_kinds), str(r["id"]))
                    ).fetchone()):
                # the charge's one fronted movement is already here
                fronted_clash += 1
                out.refuse("equity_movement", "present")
                continue
            out.land(conn, "equity_movement",
                # bare ON CONFLICT: beyond (tenant_id, id) this table has a
                # partial unique index on (tenant_id, txn_id, kind) — one
                # movement per transaction per kind. A destination that
                # already recorded the same movement under its OWN id passes
                # an id-targeted conflict clause and the raise would roll
                # back the entire all-or-nothing restore. Any unique match
                # means "already present", which is exactly the skip the
                # docstring promises.
                # created_at too: it is what decides which of two fronted
                # movements is the earlier one, here and on every upgrade
                """INSERT INTO equity_movement (id, entity_id, kind, amount,
                       date, member_id, txn_id, form, note, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                [(r["id"], r["entity_id"], r.get("kind"), amount,
                 as_date(r["date"]) if r.get("date") else None,
                 r.get("member_id") or None,
                 # keep the movement even if its transaction is gone — the
                 # FK is ON DELETE SET NULL for exactly this reason
                 m_txn,
                 r.get("form") or None, r.get("note") or None,
                 _ts(r.get("created_at")))])
        if fronted_clash:
            notes.append(
                f"{fronted_clash} business movement"
                f"{'s' if fronted_clash != 1 else ''} left out: each was a "
                "second contribution or reimbursement for a transaction "
                "that already has one (the one for the business the "
                "charge is booked to is kept, else the earliest)")

        from ..engine.books import BUCKETS as _BUCKETS
        for r in _rows(z, "business_txn_class.csv"):
            if not _txn_exists(conn, r.get("txn_id")):
                out.refuse("business_txn_class", "orphaned")
                continue
            # The bucket is an enum the app validates on write but the DB
            # does not constrain, so a hand-edited ZIP is the one way a
            # value outside the set can exist at all — and this column is
            # exported to CSV, where arbitrary text is a formula-injection
            # carrier. Refuse the row rather than plant it.
            bucket = (r.get("bucket") or "").strip()
            if bucket not in _BUCKETS:
                out.refuse("business_txn_class", "malformed")
                continue
            out.land(conn, "business_txn_class",
                """INSERT INTO business_txn_class (txn_id, bucket, sched_c_line,
                       note)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, txn_id) DO NOTHING
                   RETURNING txn_id""",
                [(r["txn_id"], bucket, r.get("sched_c_line") or None,
                  r.get("note") or None)])

        for r in _rows(z, "compliance_obligation.csv"):
            if not (r.get("id") and r.get("entity_id") and r.get("due_date")
                    and r.get("title")):
                out.refuse("compliance_obligation", "malformed")
                continue
            if _entity(r["entity_id"]) is None:
                out.refuse("compliance_obligation", "orphaned")
                continue
            out.land(conn, "compliance_obligation",
                """INSERT INTO compliance_obligation (id, entity_id, title,
                       due_date, recurrence, fee, url, note, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(r["id"], r["entity_id"], r.get("title"),
                  as_date(r["due_date"]) if r.get("due_date") else None,
                  r.get("recurrence") or "yearly", _f(r.get("fee")),
                  r.get("url") or None, r.get("note") or None,
                  _ts(r.get("created_at")))])

        for r in _rows(z, "mileage_log.csv"):
            if not (r.get("id") and r.get("entity_id") and r.get("date")
                    and _f(r.get("miles")) is not None):
                out.refuse("mileage_log", "malformed")
                continue
            if _entity(r["entity_id"]) is None:
                out.refuse("mileage_log", "orphaned")
                continue
            out.land(conn, "mileage_log",
                """INSERT INTO mileage_log (id, entity_id, date, miles, purpose,
                       note, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(r["id"], r["entity_id"],
                  as_date(r["date"]) if r.get("date") else None,
                  _f(r.get("miles")), r.get("purpose") or None,
                  r.get("note") or None, _ts(r.get("created_at")))])

        for r in _rows(z, "vendor_1099.csv"):
            if not r.get("id") or not r.get("entity_id"):
                out.refuse("vendor_1099", "malformed")
                continue
            if _entity(r["entity_id"]) is None:
                out.refuse("vendor_1099", "orphaned")
                continue
            v_merchant_id = _known_merchant(r.get("merchant_id"),
                                            known_merchants, merchant_map)
            v_merchant = _mark_name(conn, r["entity_id"], r.get("merchant"),
                                    v_merchant_id, machine_cut,
                                    persons_names)
            if v_merchant_id is None:
                v_merchant_id = _mark_merchant(conn, r["entity_id"],
                                               v_merchant)
            out.land(conn, "vendor_1099",
                # bare ON CONFLICT: vendor_1099 is also UNIQUE on
                # (tenant_id, entity_id, merchant), so a destination that
                # already marked this vendor under a different id must be a
                # skip, not a UniqueViolation that aborts the whole restore.
                # The mark follows its merchant (a rename must not drop a
                # contractor off the year-end list), and merged_from is what
                # undoing a merge puts back. A pointer to a merchant the
                # archive does not carry is dropped; the name still finds it.
                """INSERT INTO vendor_1099 (id, entity_id, merchant, reportable,
                       tin_last4, note, merchant_id, merged_from, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                [(r["id"], r["entity_id"], v_merchant,
                 _b(r.get("reportable")), r.get("tin_last4") or None,
                 r.get("note") or None,
                 v_merchant_id,
                 jsonb([n for n in _json_list(_j(r.get("merged_from")))
                        if isinstance(n, dict)]),
                 _ts(r.get("created_at")))])

        # A note is cut at the length the API stores when it is written: a
        # longer one is a value no edit could have produced, and the next
        # edit of it would silently cut it anyway. Cut, not refused — the
        # rest of the note, and the archive, are still the household's.
        from ..web.data import NOTE_MAX
        cut_notes = 0
        for r in _rows(z, "transaction_notes.csv"):
            if not _txn_exists(conn, r.get("txn_id")):
                out.refuse("transaction_notes", "orphaned")
                continue
            note = r.get("note")
            if not note:
                # clearing a note deletes its row: no door stores a blank one
                out.refuse("transaction_notes", "malformed")
                continue
            if len(note) > NOTE_MAX:
                note = note[:NOTE_MAX]
                cut_notes += 1
            out.land(conn, "transaction_notes",
                """INSERT INTO transaction_notes (txn_id, note)
                   VALUES (%s,%s)
                   ON CONFLICT (tenant_id, txn_id) DO NOTHING
                   RETURNING txn_id""",
                [(r["txn_id"], note)])
        if cut_notes:
            log.warning("restore: %d transaction note(s) longer than %d "
                        "characters were cut to that length",
                        cut_notes, NOTE_MAX)

        # A hand split is one statement about one charge — these parts,
        # adding up to it — so it restores per transaction, whole or not at
        # all. Merging an archive's lines into a split the row already has
        # produced parts from both versions summing past the charge, and the
        # sync's stale-split pass then deleted every part, the person's
        # current split included. So: a row that already has a person's
        # split keeps it; otherwise the archive's parts must pass the same validation a
        # person's edit does against the restored amount, or none are
        # written. A part whose row is absent (a pre-FK archive) is skipped
        # like any other annotation.
        from ..engine import splits as _splits
        groups: dict[str, list] = {}
        for r in _rows(z, "transaction_splits.csv"):
            if not _txn_exists(conn, r.get("txn_id")):
                out.refuse("transaction_splits", "orphaned")
                continue
            if not r.get("category") or _f(r.get("amount")) is None:
                out.refuse("transaction_splits", "malformed")
                continue
            groups.setdefault(r["txn_id"], []).append(
                (_i(r.get("line"), 0) or 0, r["category"], _f(r["amount"]),
                 (r.get("source") or "user").strip() or "user"))
        for txn_id, parts in groups.items():
            parts.sort(key=lambda p: p[0])
            # a store's parts are regenerated by its matcher from the
            # restored records; only a person's are worth carrying
            if any(src != "user" for *_, src in parts):
                out.refuse("transaction_splits", "rebuilt", len(parts))
                continue
            # only a PERSON's split here outranks the archive's: a store
            # matcher's parts are the receipt's reading, recomputed on every
            # match run, and a person's split outranks those wherever it
            # comes from — so they give way to the archive's hand split
            have = conn.execute(
                "SELECT 1 FROM transaction_splits WHERE txn_id = %s"
                " AND source = 'user' LIMIT 1",
                (txn_id,)).fetchone()
            row = conn.execute(
                "SELECT amount FROM transactions WHERE id = %s",
                (txn_id,)).fetchone()
            # the door's own rule: a split on a row the spend rollups do not
            # read is counted by nothing and dropped by the next sync's
            # stale sweep, so it is skipped rather than reported restored
            if have:
                out.refuse("transaction_splits", "present", len(parts))
                continue
            if not row or not _splits.splittable(conn, txn_id):
                out.refuse("transaction_splits", "malformed", len(parts))
                continue
            try:
                clean = _splits.validate(
                    row["amount"], [{"category": c, "amount": a}
                                    for _, c, a, _src in parts])
            except _splits.SplitError:
                out.refuse("transaction_splits", "malformed", len(parts))
                continue
            conn.execute(
                "DELETE FROM transaction_splits WHERE txn_id = %s"
                " AND source <> 'user'", (txn_id,))
            for line, p in enumerate(clean, 1):
                conn.execute(
                    """INSERT INTO transaction_splits (txn_id, line, category,
                                                      amount, source)
                       VALUES (%s,%s,%s,%s,'user')""",
                    (txn_id, line, p["category"], p["amount"]))
            # validate returns one line per part, or raises
            out.wrote("transaction_splits", len(clean))

        # the activity log travels whole: it is the household's own record
        # of who changed what, and nothing can regenerate it. The actor's
        # user id is dropped (people are numbered per instance) and the
        # actor TEXT kept, which is what the page shows anyway; a row
        # already here (same UUID) is left alone, so restoring the same
        # archive twice does not double the log.
        for r in _rows(z, "activity_log.csv"):
            if not (r.get("id") and r.get("actor") and r.get("kind")
                    and r.get("action") and r.get("summary")):
                out.refuse("activity_log", "malformed")
                continue
            at = _ts(r.get("at"))
            if at is None:
                out.refuse("activity_log", "malformed")
                continue
            # the whole restore is one transaction, so a malformed id must
            # be caught here rather than left to the driver
            try:
                rid = str(uuid.UUID(str(r["id"])))
            except ValueError:
                out.refuse("activity_log", "malformed")
                continue
            detail = _j(r.get("detail"))
            if not isinstance(detail, dict) or len(json.dumps(
                    detail, default=str).encode()) > ACTIVITY_DETAIL_MAX:
                detail = None
            # an entry names a row by id with no key to follow: it follows
            # the row to the id it lands under here, as every child does
            # (a re-id'd row's old id beside the entry's sentence would
            # give away what the re-id hid)
            target = r.get("target")
            if carried and target:
                target = carried.get(target, target)
            if (carried and detail is not None
                    and isinstance(detail.get("ids"), list)):
                detail["ids"] = [carried.get(x, x) if isinstance(x, str)
                                 else x for x in detail["ids"]]
            out.land(conn, "activity_log",
                """INSERT INTO activity_log (id, at, actor_user_id, actor,
                                             kind, action, target, label,
                                             summary, detail)
                   VALUES (%s::uuid,%s,NULL,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(rid, at, str(r["actor"])[:120], str(r["kind"])[:40],
                 str(r["action"])[:40],
                 (str(target)[:ACTIVITY_TARGET_MAX]
                  if target else None),
                 # cut, then clipped, as activity.record writes them: the
                 # label and sentence quote a payee or a row as it read then
                 card_number.cut(str(r.get("label") or ""))[:120],
                 card_number.cut(str(r["summary"]))[:300],
                 jsonb(detail) if detail is not None else None)])

        # The alert log is this instance's operating state and the next
        # Today build rebuilds it, so it stays behind, except for what in it
        # is the household's own: the alerts the person dismissed. Dropped,
        # every muted alert lights up again here. A dismissed row lands as
        # it was (still active or cleared); over a row this household holds
        # it only sets the mute, and never one the person un-muted here
        # (mute_changed_at set), and with no `seen_at`: it is no view of the world, so the
        # next snapshot decides it as usual (a still-active alert keeps the
        # mute, a recurrence after a clearance is news). Its dates are held
        # to today, or a crafted future `last_seen` would make every
        # snapshot read as stale and freeze the row.
        today = _dt.date.today()
        for r in _rows(z, "alerts_log.csv"):
            if not _b(r.get("dismissed")):
                out.refuse("alerts_log", "rebuilt")
                continue
            kind, sev, msg = (str(r.get(k) or "").strip()
                              for k in ("kind", "severity", "message"))
            try:
                first = min(as_date(r.get("first_seen")), today)
                last = min(as_date(r.get("last_seen")), today)
            except (TypeError, ValueError):
                first = last = None
            if (not kind or not sev or not msg or first is None
                    or len(kind) > 60 or len(sev) > 20
                    or len(msg) > ALERT_MESSAGE_MAX):
                out.refuse("alerts_log", "malformed")
                continue
            # a restore is a merge: the same alert raised here since keeps
            # its own dates and state, and only takes the person's mute
            out.land(conn, "alerts_log",
                """INSERT INTO alerts_log (kind, severity, message,
                                           first_seen, last_seen, active,
                                           dismissed)
                   VALUES (%s,%s,%s,%s,%s,%s,1)
                   ON CONFLICT (tenant_id, kind, message) DO UPDATE
                      SET dismissed = 1
                    WHERE alerts_log.dismissed = 0
                      AND alerts_log.mute_changed_at IS NULL
                   RETURNING id""",
                [(kind, sev, card_number.cut(msg), min(first, last), last,
                  1 if _b(r.get("active")) else 0)])

        # ---- converter v2 (migration 008): history / reference tables ----

        for r in _rows(z, "liabilities.csv"):
            if not r.get("account_id"):
                out.refuse("liabilities", "malformed")
                continue
            out.land(conn, "liabilities",
                """INSERT INTO liabilities (account_id, as_of, raw)
                   VALUES (%s, COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, account_id) DO NOTHING
                   RETURNING account_id""",
                [(_account_id(r["account_id"]), _ts(r.get("as_of")),
                 jsonb(scrub_liability_raw(
                     clean_liability_raw(_j(r.get("raw"))))))])

        for r in _rows(z, "holdings.csv"):
            if not r.get("account_id") or not r.get("symbol"):
                out.refuse("holdings", "malformed")
                continue
            try:
                quantity, price, value = (_money(r.get("quantity")),
                                          _money(r.get("price")),
                                          _money(r.get("value")))
            except _AmountOutOfRange:
                out.refuse("holdings", "out_of_range")
                continue
            out.land(conn, "holdings",
                """INSERT INTO holdings (account_id, symbol, name, quantity,
                       price, value, as_of, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, account_id, symbol) DO NOTHING
                   RETURNING account_id""",
                [(_account_id(r["account_id"]), r["symbol"], r.get("name") or None,
                 quantity, price, value,
                 _ts(r.get("as_of")), jsonb({k: v for k, v in _raw_dict(_j(r.get("raw"))).items()
                        if k not in _PLAID_RAW_IDS}))])

        for r in _rows(z, "crypto_holdings.csv"):
            if not r.get("account_id") or not r.get("currency"):
                out.refuse("crypto_holdings", "malformed")
                continue
            try:
                quantity, native = (_money(r.get("quantity")),
                                    _money(r.get("native_usd")))
            except _AmountOutOfRange:
                out.refuse("crypto_holdings", "out_of_range")
                continue
            out.land(conn, "crypto_holdings",
                """INSERT INTO crypto_holdings (account_id, currency, quantity,
                       native_usd, as_of, raw)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, account_id, currency) DO NOTHING
                   RETURNING account_id""",
                [(_account_id(r["account_id"]), r["currency"], quantity,
                 native, _ts(r.get("as_of")),
                 jsonb(_raw_dict(_j(r.get("raw")))))])

        for r in _rows(z, "networth_recorded.csv"):
            if not r.get("month") or r.get("total") in (None, ""):
                out.refuse("networth_recorded", "malformed")
                continue
            try:
                total = _money(r["total"])
            except _AmountOutOfRange:
                out.refuse("networth_recorded", "out_of_range")
                continue
            out.land(conn, "networth_recorded",
                """INSERT INTO networth_recorded (month, total, source)
                   VALUES (%s,%s,%s)
                   ON CONFLICT (tenant_id, month) DO NOTHING
                   RETURNING month""",
                [(r["month"], total, r.get("source") or None)])

        for r in _rows(z, "networth_snapshot.csv"):
            if not r.get("date"):
                out.refuse("networth_snapshot", "malformed")
                continue
            by_class = _j(r.get("by_class"))
            try:
                total = _money(r.get("total"))
            except _AmountOutOfRange:
                out.refuse("networth_snapshot", "out_of_range")
                continue
            out.land(conn, "networth_snapshot",
                """INSERT INTO networth_snapshot (date, total, by_class)
                   VALUES (%s,%s,%s)
                   ON CONFLICT (tenant_id, date) DO NOTHING
                   RETURNING date""",
                [(as_date(r["date"]), total,
                 jsonb(by_class) if by_class is not None else None)])

        # per-month frozen budgets: without them every closed month on the
        # destination falls back to the no-verdict net view. Scrubbed on
        # the way in like the live settings blob — the snapshot only feeds
        # month_status math, so a crafted ZIP must not smuggle credential
        # or policy keys through this second config door. Destination rows
        # win on conflict (a month already frozen here stays frozen).
        for r in _rows(z, "budget_snapshots.csv"):
            y_, m_ = _i(r.get("year")), _i(r.get("month"))
            # non-finite budgets read as null BEFORE the numeric check, so
            # that check refuses the month rather than the write aborting
            snap_cfg = _finite_only(_j(r.get("config")))
            if not y_ or not m_ or not (1 <= m_ <= 12) \
                    or not isinstance(snap_cfg, dict):
                out.refuse("budget_snapshots", "malformed")
                continue
            # the budgets must be NUMBERS — the live save path _num()s
            # every write, and this ZIP is the one door that bypasses
            # it. A stored "x" would 500 the history endpoint and the
            # month/year lenses with no UI to remove the row.
            from ..engine.budget import _snapshot_budgets_numeric
            if not _snapshot_budgets_numeric(snap_cfg):
                out.refuse("budget_snapshots", "malformed")
                continue
            # a frozen month is read back as a config like any other — the
            # week lens hands its excluded list straight to the spend query
            clean_excluded_accounts(snap_cfg)
            _rename_account_keys(snap_cfg, renamed)
            # the frozen bill schedule travels too; shape-checked here and
            # row-by-row on read (snapshot_bill_rows), so a crafted blob
            # degrades to "no frozen schedule", never a crash
            snap_bills = _finite_only(_j(r.get("bills")))
            if not (isinstance(snap_bills, dict)
                    and isinstance(snap_bills.get("rows"), list)):
                snap_bills = None
            else:
                # each frozen row's raw config gets the pass a live bill
                # gets (clean_bill_raw): the lenses expand these rows the
                # same way, and a recurrence that is a string or an
                # interval that is text would 500 that month and its year.
                # A row whose payee or amount is malformed is dropped.
                snap_bills = {**snap_bills, "rows": [
                    {**b, "raw": clean_bill_raw(b.get("raw"))}
                    for b in snap_bills["rows"]
                    if isinstance(b, dict)
                    and isinstance(b.get("payee"), str)
                    and isinstance(b.get("amount"), (int, float))
                    and not isinstance(b.get("amount"), bool)]}
            out.land(conn, "budget_snapshots",
                """INSERT INTO budget_snapshots (year, month, config, bills,
                       captured_at, source)
                   VALUES (%s,%s,%s,%s,COALESCE(%s, now()),%s)
                   ON CONFLICT (tenant_id, year, month) DO NOTHING
                   RETURNING year""",
                [(y_, m_, jsonb(scrub_config(snap_cfg)),
                 jsonb(snap_bills) if snap_bills is not None else None,
                 _ts(r.get("captured_at")),
                 r.get("source") if r.get("source") in ("close", "backfill")
                 else "close")])

        for r in _rows(z, "income_annual.csv"):
            if r.get("year") in (None, ""):
                out.refuse("income_annual", "malformed")
                continue
            out.land(conn, "income_annual",
                """INSERT INTO income_annual (year, total_income, agi,
                       taxable_income, tax_paid, wages, primary_wages,
                       investment_income, capital_gain, spouse_wages,
                       ss_earnings, medicare_earnings, filing_status, joint,
                       source, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, year) DO NOTHING
                   RETURNING year""",
                [(_i(r["year"]), _f(r.get("total_income")), _f(r.get("agi")),
                 _f(r.get("taxable_income")), _f(r.get("tax_paid")),
                 _f(r.get("wages")), _f(r.get("primary_wages")),
                 _f(r.get("investment_income")), _f(r.get("capital_gain")),
                 _f(r.get("spouse_wages")), _f(r.get("ss_earnings")),
                 _f(r.get("medicare_earnings")), r.get("filing_status") or None,
                 _i(r.get("joint"), 0), r.get("source") or None,
                 r.get("note") or None)])

        for r in _rows(z, "income_documents.csv"):
            if r.get("id") in (None, "") or r.get("year") in (None, ""):
                out.refuse("income_documents", "malformed")
                continue
            amounts = _j(r.get("amounts"))
            # `ein` is DELIBERATELY not restored. Taxdocs persists only
            # ein_last4, but an older archive may still carry the full W-2
            # EIN in this CSV, and reading it back would store it again.
            # The last four are restored instead, which is what the live
            # write path stores (preferring the column it writes, falling
            # back to the last four OF a legacy plaintext value).
            doc = (_i(r["year"]), r.get("form") or "",
                   r.get("owner") or "primary", r.get("payer") or None,
                   _ein_last4_for_restore(r), _f(r.get("primary_amount")),
                   jsonb(amounts) if amounts is not None else None,
                   r.get("notes") or None)
            # A document's id is only a number within its household (each
            # one counts 1, 2, 3...), so an archive's ids collide with the
            # documents a populated household already has, and keying on
            # them would drop the archive's different documents as present.
            # Present means the same document; one that is not lands under
            # the next free id, as the live write path allocates it.
            if conn.execute(
                    """SELECT 1 FROM income_documents
                        WHERE year = %s AND form = %s
                          AND COALESCE(owner, 'primary') = %s
                          AND payer IS NOT DISTINCT FROM %s
                          AND ein_last4 IS NOT DISTINCT FROM %s
                          AND primary_amount IS NOT DISTINCT FROM %s
                          AND amounts IS NOT DISTINCT FROM %s::jsonb
                          AND notes IS NOT DISTINCT FROM %s""",
                    doc).fetchone():
                # the same document is already here, under whatever id
                out.refuse("income_documents", "present")
                continue
            doc_id = _i(r["id"])
            if conn.execute("SELECT 1 FROM income_documents WHERE id = %s",
                            (doc_id,)).fetchone():
                doc_id = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 AS id "
                    "FROM income_documents").fetchone()["id"]
            out.land(conn, "income_documents",
                """INSERT INTO income_documents (id, year, form, owner, payer,
                       ein_last4, primary_amount, amounts, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING
                   RETURNING id""",
                [(doc_id, *doc)])

        # An alias is keyed on the row's identity string, which for a file
        # import is masked above (and by the live upsert). Its key is cut
        # the same way whenever a restored file-import row now carries the
        # masked string, so the alias goes on joining its rows and the full
        # number does not survive in it; the full spelling is kept only
        # while some row still keys on it (an aggregator row keeps its
        # name). Migration 153 applies the same rule to stored aliases.
        alias_keys: dict[str, tuple[bool, bool]] = {}

        def _alias_where(raw):
            """(a file-import row keys on the masked `raw`, some row still
            keys on `raw` itself) — both False-True for a short raw."""
            masked = card_number.cut(raw)
            if masked == raw:
                return False, True
            if raw not in alias_keys:
                from ..engine.merchant_sql import RAW_KEY
                hit = conn.execute(
                    f"""SELECT
                          EXISTS (SELECT 1 FROM transactions t
                                   WHERE split_part(t.id, ':', 1) = ANY(%s)
                                     AND {RAW_KEY} = %s) AS filed,
                          EXISTS (SELECT 1 FROM transactions t
                                   WHERE {RAW_KEY} = %s) AS kept""",
                    ([p.rstrip(":") for p in
                      _sync_base.FILE_IMPORT_ID_PREFIXES], masked, raw)
                ).fetchone()
                alias_keys[raw] = (bool(hit["filed"]), bool(hit["kept"]))
            return alias_keys[raw]

        # a machine's alias names its merchant as the resolver does, cut;
        # a person's says what they typed. (raw, whole canonical) of each
        # machine alias cut here, for the journal below
        cut_aliases: set[tuple[str, str]] = set()
        for r in _rows(z, "merchant_canonical.csv"):
            if not r.get("raw_merchant") or not r.get("canonical"):
                out.refuse("merchant_canonical", "malformed")
                continue
            raw = _descriptor(r["raw_merchant"])
            canonical = _descriptor(r["canonical"])
            if (r.get("method") or "manual") != "manual":
                cut_canonical = card_number.cut(canonical)
                if cut_canonical != canonical:
                    cut_aliases.add((r["raw_merchant"], canonical))
                canonical = cut_canonical
            filed, kept = _alias_where(raw)
            keys = ([card_number.cut(raw)] if filed else []) + (
                [raw] if kept or not filed else [])
            # one archive row, restored or not, however many keys it took
            out.land(conn, "merchant_canonical",
                """INSERT INTO merchant_canonical (raw_merchant, canonical,
                       method, as_of, merchant_id)
                   VALUES (%s,%s,%s, COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, raw_merchant) DO UPDATE
                      SET canonical = EXCLUDED.canonical,
                          method = EXCLUDED.method,
                          as_of = EXCLUDED.as_of,
                          merchant_id = EXCLUDED.merchant_id
                    WHERE EXCLUDED.method = 'manual'
                      AND merchant_canonical.method
                          IS DISTINCT FROM 'manual'
                RETURNING raw_merchant""",
                # A person's alias (method 'manual') replaces the
                # destination's machine alias for the same descriptor;
                # anything else already here stays.
                # A NULL method is a frozen mapping: the layer1 recompute
                # only touches method='layer1' rows and the preservation
                # logic only recognises 'llm'/'manual', so a row with no
                # method is invisible to both forever. A hand-edited or
                # pre-method CSV defaults to 'manual' — the preservation-
                # safe choice, since manual rows are never recomputed over.
                # The key is a bank descriptor, so it takes the descriptor
                # clamp: the alias and the ledger string it has to equal are
                # cut the same way and go on matching.
                [(key, canonical,
                 r.get("method") or "manual",
                 _ts(r.get("as_of")),
                 # same screen as the transactions above: an alias whose
                 # merchant is not in the archive resolves by its canonical
                 # string on the next pass, as an alias with no merchant does
                 _known_merchant(r.get("merchant_id"), known_merchants,
                                 merchant_map)) for key in keys],
                as_one=True)

        def _renamed_key(raw):
            # the journal row follows its alias: onto the masked key when
            # file-import rows carry it and nothing keys on the full one
            filed, kept = _alias_where(raw)
            return (card_number.cut(raw) if filed and not kept
                    else raw)

        # The journal's ids come from ONE sequence every household shares,
        # and an archive's ids are whatever the file says. Kept, and the
        # sequence advanced past them, one archive could spend the shared
        # counter to the edge of its range and every household's next
        # rename would fail. Nothing points at a journal row by id; the id
        # only orders the rows. So each row gets a fresh id here, inserted
        # in the archive's id order, and the sequence moves only as far as
        # these inserts take it. A row already in the journal (the same
        # archive restored twice) is recognised by what it says, not by id.
        def _order(r):
            try:
                return (0, int(_f(r.get("id")) or 0))
            except (TypeError, ValueError, OverflowError):
                return (1, 0)

        def _journalled(r) -> bool:
            if r.get("raw_merchant") and r.get("to_canonical"):
                return True
            out.refuse("merchant_renames", "malformed")
            return False

        # A journal row's result is cut where it is what a machine's alias
        # (cut above) still reads; then a `from` no journal row names as
        # somebody's result is a machine's, and cut too. A person's stays.
        journal = sorted((r for r in _rows(z, "merchant_renames.csv")
                          if _journalled(r)),
                         key=_order)
        for r in journal:
            if (r["raw_merchant"], r["to_canonical"]) in cut_aliases:
                r["to_canonical"] = card_number.cut(r["to_canonical"])
        results = {r["to_canonical"] for r in journal}
        for r in journal:
            frm = r.get("from_canonical") or None
            if (card_number.holds_number(frm) and frm not in results
                    and not conn.execute(
                        "SELECT 1 FROM merchant_renames"
                        " WHERE to_canonical = %s", (frm,)).fetchone()):
                r["from_canonical"] = card_number.cut(frm)
        for r in journal:
            at = _ts(r.get("at"))
            # what the rename did to the category rules it moved, so undoing
            # it on this instance can put them back; anything but the object
            # the rename writes is no note at all
            moved_rules = _j(r.get("rules"))
            moved_rules = (jsonb(moved_rules)
                           if isinstance(moved_rules, dict) else None)
            out.land(conn, "merchant_renames",
                """INSERT INTO merchant_renames (raw_merchant,
                       from_canonical, to_canonical, at, rules)
                   SELECT %s, %s, %s, COALESCE(%s, now()), %s
                    WHERE NOT EXISTS (
                          SELECT 1 FROM merchant_renames j
                           WHERE j.raw_merchant = %s
                             AND j.from_canonical IS NOT DISTINCT FROM %s
                             AND j.to_canonical = %s
                             AND j.at = %s)
                   RETURNING id""",
                [(_renamed_key(r["raw_merchant"]),
                 r.get("from_canonical") or None, r["to_canonical"], at,
                 moved_rules,
                 _renamed_key(r["raw_merchant"]),
                 r.get("from_canonical") or None, r["to_canonical"], at)])

        # A person's rule beats the destination's guess. A household that
        # linked a bank before restoring already holds seed and model rules
        # for the merchants its first sync saw; the archive's rule for the
        # same merchant, when a PERSON set it, replaces the machine's. Two
        # rules of the same kind leave the destination's in place.
        #
        # A rule is keyed on a merchant string, and follows the cut as the
        # alias does: onto the cut key where file rows now carry it, the
        # whole one kept only while a row still keys on it. A rule keyed on
        # a machine merchant's whole name, with no alias of that key, is
        # read through the merchant's name and follows it to the cut name
        # (`machine_cut`); a person's merchant keeps its rule.
        def _rule_keys(key):
            filed, kept = _alias_where(key)
            keys = ([card_number.cut(key)] if filed else []) + (
                [key] if kept or not filed else [])
            out_keys = []
            for k in keys:
                follow = machine_cut.get(k.lower())
                if (follow is not None and k.lower() not in persons_live
                        and not conn.execute(
                            "SELECT 1 FROM merchant_canonical"
                            " WHERE raw_merchant = %s", (k,)).fetchone()):
                    k = follow
                if k not in out_keys:
                    out_keys.append(k)
            return out_keys

        for r in _rows(z, "merchant_categories.csv"):
            if not r.get("merchant") or not r.get("category_primary"):
                out.refuse("merchant_categories", "malformed")
                continue
            out.land(conn, "merchant_categories",
                """INSERT INTO merchant_categories (merchant, category_primary,
                       source, classified_at, disabled)
                   VALUES (%s,%s,%s, COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, merchant) DO UPDATE
                      SET category_primary = EXCLUDED.category_primary,
                          source = EXCLUDED.source,
                          classified_at = EXCLUDED.classified_at,
                          disabled = EXCLUDED.disabled
                    WHERE EXCLUDED.source = 'user'
                      AND merchant_categories.source <> 'user'
                   RETURNING merchant""",
                [(key, r["category_primary"],
                 r.get("source") or "llm",
                 _ts(r.get("classified_at")), _b(r.get("disabled")))
                 for key in _rule_keys(_descriptor(r["merchant"]))],
                as_one=True)

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
        # id rather than re-derived from names — except a side the restore
        # mapped onto a live merchant: the proposer keys a pair by its two
        # ids, so the decision is re-keyed onto the live pair, where the
        # next proposal run will look for it.
        #
        # A pair the destination already offered and the household has not
        # answered there takes the archive's ANSWER: a pending offer is the
        # machine asking, a decision is the person's reply.
        from ..engine.merchant_merge import _sig as _merge_pair_id
        for r in _rows(z, "merchant_merge_proposals.csv"):
            frm = _known_merchant(r.get("from_merchant_id"), known_merchants,
                                  merchant_map)
            into = _known_merchant(r.get("into_merchant_id"), known_merchants,
                                   merchant_map)
            if not r.get("id"):
                out.refuse("merchant_merge_proposals", "malformed")
                continue
            if not frm or not into:
                out.refuse("merchant_merge_proposals", "orphaned")
                continue
            if frm == into:
                # both sides are one merchant here: the restore folded them
                # onto the same live one, or the file names a pair of one
                out.refuse("merchant_merge_proposals",
                           "merged" if any(
                               str(r.get(k) or "").strip().lower()
                               in merchant_map
                               for k in ("from_merchant_id",
                                         "into_merchant_id"))
                           else "malformed")
                continue
            pair_id = r["id"]
            if any(str(r.get(k) or "").strip().lower() in merchant_map
                   for k in ("from_merchant_id", "into_merchant_id")):
                pair_id = _merge_pair_id(frm, into)
            out.land(conn, "merchant_merge_proposals",
                """INSERT INTO merchant_merge_proposals (id, from_merchant_id,
                       into_merchant_id, evidence, status, created_at, decided_at)
                   VALUES (%s,%s,%s, COALESCE(%s,'{}'::jsonb), %s,
                           COALESCE(%s, now()), %s)
                   ON CONFLICT (tenant_id, id) DO UPDATE
                      SET status = EXCLUDED.status,
                          decided_at = COALESCE(EXCLUDED.decided_at, now())
                    WHERE merchant_merge_proposals.status = 'pending'
                      AND EXCLUDED.status <> 'pending'
                   RETURNING id""",
                [(pair_id, frm, into,
                 jsonb(_ev if isinstance(_ev := _j(r.get("evidence")), dict)
                       else {}),
                 # a hand-edited status the app never writes would sit in
                 # the table unreadable by every query above; anything
                 # unrecognised is simply an undecided offer
                 (r.get("status") if r.get("status") in
                  ("pending", "approved", "rejected", "stale") else "pending"),
                 _ts(r.get("created_at")), _ts(r.get("decided_at")))])

        for r in _rows(z, "amazon_orders.csv"):
            if not r.get("dedup_key") or not r.get("date"):
                out.refuse("amazon_orders", "malformed")
                continue
            try:
                _money(r.get("amount"))
            except _AmountOutOfRange:
                out.refuse("amazon_orders", "out_of_range")
                continue
            out.land(conn, "amazon_orders",
                """INSERT INTO amazon_orders (dedup_key, account, date, amount,
                       payee, seller, memo, category, category_source,
                       order_number, is_refund, payment_method, items_json,
                       inserted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, dedup_key) DO NOTHING
                   RETURNING dedup_key""",
                [(r["dedup_key"], r.get("account") or "", as_date(r["date"]),
                 _money(r.get("amount")) or 0.0, r.get("payee") or "Amazon",
                 r.get("seller") or "", r.get("memo") or "",
                 r.get("category") or "", r.get("category_source") or "rules",
                 r.get("order_number") or "", _i(r.get("is_refund"), 0),
                 r.get("payment_method") or "",
                 jsonb(_json_list(_j(r.get("items_json")))),
                 _ts(r.get("inserted_at")))])

        for r in _rows(z, "amazon_matches.csv"):
            if not _txn_exists(conn, r.get("transaction_id")):
                out.refuse("amazon_matches", "orphaned")
                continue
            out.land(conn, "amazon_matches",
                """INSERT INTO amazon_matches (transaction_id, dedup_key,
                       matched_at)
                   VALUES (%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, transaction_id) DO NOTHING
                   RETURNING transaction_id""",
                [(r["transaction_id"], r.get("dedup_key") or None,
                 _ts(r.get("matched_at")))])

        for r in _rows(z, "costco_receipts.csv"):
            if not r.get("dedup_key") or not r.get("date"):
                out.refuse("costco_receipts", "malformed")
                continue
            try:
                _money(r.get("amount"))
            except _AmountOutOfRange:
                out.refuse("costco_receipts", "out_of_range")
                continue
            out.land(conn, "costco_receipts",
                """INSERT INTO costco_receipts (dedup_key, account, date,
                       amount, receipt_type, warehouse, category,
                       category_source, summary, is_refund, payment_method,
                       items_json, inserted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, dedup_key) DO NOTHING
                   RETURNING dedup_key""",
                [(r["dedup_key"], r.get("account") or "main",
                 as_date(r["date"]), _money(r.get("amount")) or 0.0,
                 r.get("receipt_type") or "warehouse",
                 r.get("warehouse") or "", r.get("category") or "",
                 r.get("category_source") or "rules", r.get("summary") or "",
                 _i(r.get("is_refund"), 0), r.get("payment_method") or "",
                 jsonb(_json_list(_j(r.get("items_json")))),
                 _ts(r.get("inserted_at")))])

        for r in _rows(z, "costco_matches.csv"):
            if not _txn_exists(conn, r.get("transaction_id")):
                out.refuse("costco_matches", "orphaned")
                continue
            out.land(conn, "costco_matches",
                """INSERT INTO costco_matches (transaction_id, dedup_key,
                       matched_at)
                   VALUES (%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, transaction_id) DO NOTHING
                   RETURNING transaction_id""",
                [(r["transaction_id"], r.get("dedup_key") or None,
                 _ts(r.get("matched_at")))])

        for r in _rows(z, "amazon_summaries.csv"):
            if not r.get("dedup_key") or not r.get("summary"):
                out.refuse("amazon_summaries", "malformed")
                continue
            out.land(conn, "amazon_summaries",
                """INSERT INTO amazon_summaries (dedup_key, summary, created_at)
                   VALUES (%s,%s, COALESCE(%s, now()))
                   ON CONFLICT (tenant_id, dedup_key) DO NOTHING
                   RETURNING dedup_key""",
                [(r["dedup_key"], r["summary"], _ts(r.get("created_at")))])

        # an archive written before the household's rules were derived from
        # its own corrections (035) and its model rules keyed on the
        # merchant (044) gets both, over what it brought. Read from the
        # archive's own word about its age: waived, a household only keeps
        # rules in the shape it exported them in, and applied to a newer
        # archive the derivation could bring back a rule its person deleted
        schema = _upgrades.archive_schema(z)
        if _upgrades.older_than(schema, _RULES_FROM_OVERRIDES):
            derive_user_rules_from_overrides(conn, overridden)
        if _upgrades.older_than(schema, _MODEL_RULES_ON_THE_MERCHANT):
            rekey_model_rules_on_the_merchant(conn)

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
            for k in _CREDENTIAL_COMPANION_KEYS:
                cfg.pop(k, None)
            notes.extend(normalize_notify_shapes(cfg))
            clean_excluded_accounts(cfg)
            notes.extend(clean_budget_amounts(cfg))
            notes.extend(clean_income_scenarios(cfg))
            notes.extend(clean_savings_goals(cfg))
            notes.extend(clean_manual_assets(cfg))
            notes.extend(clean_config_shapes(cfg))
            _rename_account_keys(cfg, renamed)
            _follow_moved_bills(cfg, bill_payees)
            # The Enrich cap is the household's preference and restores, but
            # only inside the range the settings door accepts: above the
            # ceiling it would authorise spend the operator never allowed,
            # and a value that is not a number would break every Enrich call
            # until someone happened to save settings again. A malformed one
            # is dropped, which leaves this instance's own value in place.
            if "plaid_enrich_cap" in cfg:
                from ..web.api import _enrich_cap_ceiling
                try:
                    cfg["plaid_enrich_cap"] = max(0, min(
                        int(cfg["plaid_enrich_cap"]), _enrich_cap_ceiling()))
                except (TypeError, ValueError, OverflowError):
                    cfg.pop("plaid_enrich_cap")
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
                    archived_recipients = list(kept)
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
            # of the destination document we already read. We are inside
            # `conn.transaction()`, so the lock is held until the restore
            # commits.
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
            _settle_masked_urls(cfg, dest)
            merged = dict(dest)
            for k, v in _finite_only(cfg).items():
                merged[k] = (carry_forward_secrets(dest[k], v)
                             if k in dest else v)
            # an archived config without today_view chose the summary face
            # (the settings door stores that choice as no key); the face a
            # new account was seeded with is provisioning, not a choice
            if "today_view" not in cfg:
                merged.pop("today_view", None)
            # the destination's pause record is kept as it was (the ZIP's
            # never reaches here — scrub_config drops it), and while it is
            # armed it also covers whatever outbound setting this restore
            # brought in
            widen_armed_pause(dest, merged, conn)
            pause_if_starter_signed_out(dest, merged, conn)
            conn.execute(
                """INSERT INTO tenant_settings (config) VALUES (%s)
                   ON CONFLICT (tenant_id) DO UPDATE
                   SET config = EXCLUDED.config, updated_at = now()""",
                (jsonb(merged),))
            out.wrote("tenant_settings", 1)
            # belt: this write bypasses budget.save_config — clear
            # the demoguard cache the same way so no stale flag survives
            from ..web import demoguard
            row = conn.execute(
                "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
            demoguard.invalidate(row["tid"] if row and row["tid"] else None)
        # A hand-made account an archive written before the re-key holds
        # under an id spelling a full account number is moved onto its
        # masked id here, as `oikonome migrate` moved the stored ones, once
        # its rows, lineage and settings have landed — its rows re-hashed
        # under the new id, and every adoption record naming it in full
        # cut; the adoption it leaves is what a later restore of the same
        # archive follows to it.
        rekeyed = _sync_base.rekey_numbered_accounts(conn)
        landed_accounts = {rekeyed.get(a, a) for a in landed_accounts}
        # and the rows such an archive brings, each id an unkeyed hash over
        # the full number, are re-id'd under this household's own secret
        # (with every row whose id was hashed before its name was masked),
        # as `oikonome migrate` re-ids the stored ones; the secret itself
        # never travels, so another household's archive is keyed anew here
        _sync_base.key_file_import_ids(conn)
        # every hand-made account the archive brought without a
        # stored-name row (one written before the table, or one a re-key
        # just moved) gets one, as migration 207 gave the stored ones
        _account_sources.backfill(conn)
        # a detected bill's match key, and the names the log quotes, are
        # cut as migration 208 cut the stored ones; after the merchants
        # landed, because a name a live merchant holds whole stays whole
        from ..engine import activity as _activity
        _bills.cut_detected_keys(conn)
        _activity.cut_stored_names(conn)
    # Restored AFTER the same bank was linked here: the link already holds
    # each account under its new Plaid id, and the archive just brought the
    # same account back under its old one on a tokenless shell. The link
    # side cannot see this order (it adopts only accounts it does not hold
    # yet), so the restore merges them from its own side. After the commit,
    # one transaction per account, so a merge that fails costs that one
    # account its merge and never the restore.
    try:
        from .adopt import merge_restored_onto_live
        _merged, merge_notes = merge_restored_onto_live(conn, landed_accounts)
        notes.extend(merge_notes)
    except Exception:                                    # noqa: BLE001
        log.exception("restore: merging onto linked accounts failed")
        notes.append("accounts already linked here could not be checked "
                     "against the restored ones — some may appear twice; "
                     "check the Accounts page")
    # Outside the transaction above, because this one table is written over
    # the admin connection (migration 074 grants the app role nothing on
    # it) — so it lands only once the household's data has actually
    # committed.
    try:
        inv = _restore_recipient_invites(conn, z, archived_recipients)
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invites could not be restored")
        inv = dict(_NO_INVITES)
        notes.append("the daily email's recipients could not be restored — "
                     "check Settings → Email, anyone listed there may need "
                     "to be invited again")
    if inv["rows"]:
        out.wrote("recipient_invites", inv["rows"])
    if inv["total"]:
        # Always said out loud, even when every answer carried: the one
        # number that matters — how many people have to be asked again
        # before their mail resumes — is reported whether it is zero or not.
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

    # Counts are rows WRITTEN, each one a row its insert returned — a
    # re-restore honestly reports 0, not the ZIP's size — and every row not
    # written is under the one outcome that says why (REFUSALS).
    counts = out.counts()
    notes.extend(_outcome_notes(out))
    # what the config check dropped, so the person restoring is told
    # rather than left to discover a missing AI backend later
    if notes:
        counts["_notes"] = notes                      # not a row count
    # Rows from an archive without merchant_id get their merchant here — the
    # ledger shows names and logos on the first load after a restore, not
    # after the next nightly
    try:
        from ..engine import merchant_identity
        merchant_identity.resolve(conn)
    except Exception:                                    # noqa: BLE001
        log.warning("post-restore merchant resolve skipped", exc_info=True)
    return counts
