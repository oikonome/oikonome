"""Aggregator interface — every sync source speaks this, the engine sees
only normalized rows, so no part of the product depends on a single
aggregator.

Normalized conventions (= the engine's contract):
  * amount sign: POSITIVE = money out (Plaid convention). Aggregators that
    use bank convention (negative = out; SimpleFIN does) are flipped HERE,
    at the boundary — never downstream.
  * transactions carry a stable per-source id; upserts are idempotent.
  * removed rows are flagged, never deleted.
  * `raw` keeps the aggregator's full object (JSONB) — extracted columns
    are conveniences.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import logging
import math
from ..envnum import env_flag
import re
from collections import defaultdict
from typing import Protocol

from ..db import crypto, tenancy
from ..engine.compat import MAX_AMOUNT, jsonb, strip_nul
from . import flowmap, rowcap

log = logging.getLogger(__name__)

# An instance run for other people may soft-cap connected INSTITUTIONS,
# because aggregators bill per Item ≈ per institution. An instance a
# household runs on its own aggregator keys is never capped.
def institution_cap(conn=None) -> int | None:
    """None = uncapped. An instance run for other people asks the installed
    gate, which knows whatever limit it enforces and any per-tenant lift; a
    household's own instance is never capped, and a gate that cannot answer
    caps nothing."""
    if not env_flag("OIKONOME_HOSTED"):
        return None
    try:
        from .. import ext
        return ext.gate.institution_cap(conn)
    except Exception:      # noqa: BLE001 — a failed read never refuses a bank
        return None


def institution_count(conn) -> int:
    """Live aggregator institutions for the current tenant: Plaid/MX Items
    plus SimpleFIN per-bank children (a bridge holds many banks — the
    bridge row itself isn't an institution). Archived connections freed
    their slot; manual accounts and collector scripts never count.

    A 'restored' item is the shell an export restore leaves behind — its
    history with no credentials, waiting for the bank to be linked again —
    so it is not a connection yet. Counting it would refuse the very
    reconnect that turns it back into one. (Not keyed on access_token:
    SimpleFIN children and MX members hold none even when live.)"""
    return conn.execute(
        """SELECT COUNT(*) AS n FROM items
           WHERE COALESCE(status, 'ok') NOT IN ('archived', 'restored')
             AND aggregator IN ('plaid', 'mx', 'simplefin-org')"""
    ).fetchone()["n"]


# How long a released Item keeps holding its place under the cap.
RELEASED_HOLD_DAYS = 30


def released_count(conn) -> int:
    """Items on the instance's own aggregator keys that this tenant created
    and released within the last RELEASED_HOLD_DAYS. The aggregator bills an
    Item for the month it existed in, and removing it refunds nothing, so a
    cap on live Items alone lets link, disconnect, link again mint a new
    billed Item every pass while the live count never moves. A release
    whose stamp never landed counts from the day the Item was created.
    Live Items are left out — institution_count already has them — and an
    Item on the household's own keys is their bill, not the instance's."""
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM plaid_item_ledger l
           WHERE l.tenant = current_setting('app.tenant_id', true)
             AND l.credential_source = 'platform'
             AND COALESCE(l.removed_at, l.created_at)
                 >= now() - make_interval(days => %s)
             AND NOT EXISTS (
                 SELECT 1 FROM items i WHERE i.id = l.item_id
                    AND COALESCE(i.status, 'ok')
                        NOT IN ('archived', 'restored'))""",
        (RELEASED_HOLD_DAYS,)).fetchone()
    return int(row["n"])


def institutions_held(conn) -> int:
    """What the institution cap is measured against: live institutions
    plus Items released within the hold window (released_count)."""
    return institution_count(conn) + released_count(conn)


def check_institution_cap(conn) -> None:
    """Raise ValueError (friendly copy) when connecting one more
    institution would pass the instance's cap. Call before opening any
    ADD-a-connection door; update/repair flows are exempt. The cap counts
    live institutions AND Items released in the last RELEASED_HOLD_DAYS,
    so a disconnected slot frees once that window has passed."""
    cap = institution_cap(conn)
    if cap is None:
        return
    n = institutions_held(conn)
    if n >= cap:
        # the installed gate words the refusal — it knows what the cap is
        # and what, if anything, lifts it
        from .. import ext
        raise ValueError(ext.gate.institution_cap_message(cap, n))


@dataclasses.dataclass
class Account:
    id: str                    # source-stable account id
    name: str
    type: str                  # depository | credit | investment | loan
    subtype: str | None = None
    mask: str | None = None
    balance_current: float | None = None
    balance_available: float | None = None
    currency: str | None = None
    raw: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Transaction:
    id: str                    # source-stable txn id (engine-unique)
    account_id: str
    date: dt.date
    amount: float              # POSITIVE = money out
    name: str
    merchant_name: str | None = None
    pending: bool = False
    category_primary: str | None = None
    category_detailed: str | None = None
    # Plaid PFC mirror — sync-only; refinements never write these
    category_plaid: str | None = None
    category_plaid_detailed: str | None = None
    category_plaid_confidence: str | None = None
    raw: dict = dataclasses.field(default_factory=dict)


class Aggregator(Protocol):
    """One connected institution/login. Implementations: simplefin, plaid,
    csvimport."""

    def list_accounts(self) -> list[Account]: ...

    def pull_transactions(self, since: dt.date | None
                          ) -> tuple[list[Transaction], list[str]]:
        """Returns (upserts, removed_ids)."""
        ...


# ---- normalized upserts (shared by every aggregator) -----------------------


def upsert_item(conn, item_id: str, aggregator: str, institution_name: str,
                access_token: str | None, raw: dict | None = None, *,
                keep_archived: bool = False) -> None:
    """access_token is ENCRYPTED at rest (per-tenant envelope, db/crypto);
    read it back only through get_access_token.

    A person linking or re-linking a connection means it to be live, so by
    default the row comes back as 'ok'. A SYNC re-writing its own item passes
    keep_archived=True: the owner can disconnect while a pull is already
    running (that door does not wait for the sync), and a pull that finishes
    afterwards must not bring the connection back to life."""
    status_sql = ("CASE WHEN items.status='archived' THEN 'archived' "
                  "ELSE 'ok' END" if keep_archived else "'ok'")
    conn.execute(
        f"""INSERT INTO items (id, aggregator, institution_name, access_token, raw)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id, id) DO UPDATE SET
               institution_name=EXCLUDED.institution_name,
               access_token=EXCLUDED.access_token, status={status_sql},
               raw=EXCLUDED.raw""",
        (item_id, aggregator, strip_nul(institution_name),
         crypto.encrypt(conn, access_token), jsonb(raw or {})))


def get_access_token(conn, item_id: str) -> str | None:
    row = conn.execute("SELECT access_token FROM items WHERE id=%s",
                       (item_id,)).fetchone()
    return crypto.decrypt(conn, row["access_token"]) if row else None


# archived_reason on a SimpleFIN bank that went down with its bridge (and
# with nothing else): the only kind of disconnected bank a new setup token
# brings back
BRIDGE_DISCONNECT = "bridge-disconnect"


# Aggregators whose access_token is a non-secret marker the importer reads
# back, not a credential: a disconnect leaves it in place. Keep in step with
# migration 158.
DISCONNECT_KEEPS_TOKEN = ("plan_csv",)


def disconnect_item(conn, item_id: str) -> dict:
    """Archive a connection so it stops syncing; history is kept.

    For Plaid this ALSO releases the Item on Plaid's side (``/item/remove``)
    — a local archive alone never frees a billing slot. Returns
    ``{ok, plaid_released, release_failed, note, aggregator}``.
    Raises ``KeyError`` if the item does not exist.
    """
    it = conn.execute(
        "SELECT id, aggregator, status FROM items WHERE id=%s",
        (item_id,)).fetchone()
    if it is None:
        raise KeyError(item_id)
    plaid_released, note, release_failed = False, "", False
    agg = it["aggregator"] or ""
    if agg == "plaid":
        from . import plaid as plaid_mod
        token = get_access_token(conn, item_id)
        if token:
            try:
                plaid_mod.Client.for_tenant(conn).remove_item(token)
                plaid_released = True
            except plaid_mod.PlaidError as e:
                # ITEM_NOT_FOUND means the Item is already gone at Plaid —
                # a concurrent disconnect click, the reaper's straggler
                # retry, or an orphan-reconcile removed it first. That is a
                # successful release, not a failure: reaper.py treats it the
                # same way. Falling into release_failed instead would show
                # the user "we'll keep retrying" for a connection that is
                # genuinely gone and leave a dead access_token in the row.
                if e.code == "ITEM_NOT_FOUND":
                    plaid_released = True
                else:
                    release_failed = True
                    note = (f"Plaid didn't confirm the release "
                            f"({type(e).__name__}) — archived locally; "
                            f"we'll keep retrying the release automatically")
            except Exception as e:                           # noqa: BLE001
                # a dead/already-removed Item must not block the local
                # archive — reaper retries failed releases
                release_failed = True
                note = (f"Plaid didn't confirm the release "
                        f"({type(e).__name__}) — archived locally; "
                        f"we'll keep retrying the release automatically")
        # no token: already released or never had one; treat as clean
    elif agg in ("simplefin", "simplefin-org"):
        note = ("SimpleFIN billing follows your bridge.simplefin.org "
                "account — remove the bank there too if you're done "
                "with it")
    # A FAILED Plaid release must not hide a still-billing Item. Keep
    # the access_token and mark the archive so the reaper's archived-token
    # retry pass finds it. A clean Plaid release sheds the token and stamps
    # the audit ledger. Every other connection sheds its credential too: an
    # exchange key or a bridge URL is read by nothing once the connection
    # is archived (it comes back only by linking anew), and a kept one is a
    # live secret in every backup and bundle. A plan item's token is a
    # marker, not a credential (see DISCONNECT_KEEPS_TOKEN).
    if agg == "plaid" and release_failed:
        conn.execute(
            "UPDATE items SET status='archived', "
            "archived_reason='disconnect-release-failed', "
            "archived_at=COALESCE(archived_at, now()) WHERE id=%s",
            (item_id,))
    elif agg == "plaid":
        conn.execute(
            "UPDATE items SET status='archived', access_token=NULL, "
            "archived_reason=NULL, "
            "archived_at=COALESCE(archived_at, now()) WHERE id=%s",
            (item_id,))
        if plaid_released:
            from . import plaid as plaid_mod
            plaid_mod.stamp_ledger_removed(item_id, "disconnect")
    elif agg == "simplefin-org":
        # One bank cut on its own is the owner's choice about that bank (a
        # duplicate of another feed, one they are done with), so it carries
        # no bridge mark even if the bridge's disconnect stamped it earlier:
        # a new setup token brings back only what the bridge took down.
        conn.execute(
            "UPDATE items SET status='archived', archived_reason=NULL, "
            "access_token=NULL, "
            "archived_at=COALESCE(archived_at, now()) WHERE id=%s",
            (item_id,))
    else:
        conn.execute(
            "UPDATE items SET status='archived', "
            "access_token=CASE WHEN aggregator = ANY(%s) THEN access_token "
            "END, "
            "archived_at=COALESCE(archived_at, now()) WHERE id=%s",
            (list(DISCONNECT_KEEPS_TOKEN), item_id))
    if agg == "simplefin":
        # archiving the bridge silences all its per-bank children too, and
        # marks the ones it took down so a new setup token knows which to
        # bring back; a bank already disconnected on its own keeps its
        # unmarked state and stays out
        conn.execute(
            "UPDATE items SET status='archived', access_token=NULL, "
            "archived_reason=CASE WHEN COALESCE(status,'') != 'archived' "
            "THEN %s ELSE archived_reason END, "
            "archived_at=COALESCE(archived_at, now()) "
            "WHERE aggregator='simplefin-org' AND id LIKE %s",
            (BRIDGE_DISCONNECT, item_id + ":%"))
    return {"ok": True, "plaid_released": plaid_released,
            "release_failed": release_failed, "note": note,
            "aggregator": agg}


def release_matched_receipts(conn, txn_ids: list[str]) -> int:
    """Put every receipt that was PAIRED with one of these rows back to
    waiting. For writers about to hard-delete rows: a receipt snapped
    before its charge is a thing of its own that the matcher or a person
    later hung on the row, and the foreign key's cascade would destroy the
    image along with the row — an import undone, an account purged, and
    the receipt the person photographed is gone with no one having chosen
    that. Waiting again, it meets the charge if the rows come back (the
    same file re-imported) and stays on the waiting list otherwise.

    A receipt attached FROM the transaction itself (no match_method) was
    uploaded onto that charge and is part of it; it goes with the row as
    it always has — the same line `release_stranded` draws for retired
    rows. A check image only ever lives on its transaction. And the same
    line on a hand pairing: one a PERSON made keeps its 'manual' mark while
    it waits, so the rules never re-pair it behind their back and the list
    shows it released for them to choose again.

    The caller holds the pairing lock (`reanchor.hold_pairing_lock`) in the
    same transaction as the delete that follows, so the matcher cannot pair
    a receipt with one of these rows between the release and the delete.
    Returns how many receipts went back to waiting."""
    ids = [i for i in dict.fromkeys(txn_ids) if i]
    if not ids:
        return 0
    return conn.execute(
        """UPDATE receipts
              SET txn_id = NULL, matched_at = NULL,
                  match_method = CASE WHEN match_method = 'manual'
                                      THEN 'manual' END
            WHERE txn_id = ANY(%s) AND kind = 'receipt'
              AND match_method IS NOT NULL""", (ids,)).rowcount


def carry_unmatch_memory(conn, old_ids: list[str],
                         new_ids: list[str]) -> int:
    """Rewrite "a person took this receipt OFF that charge" from each
    retired id onto the id the same charge lives on now.

    The matcher never offers a receipt a charge it was taken off, but it
    knows the charge only by id. When a hold settles into its posted row,
    or a retired row is re-anchored onto its live twin, the charge carries
    on under a new id — and without this the next matcher pass sees the
    new id as a fresh exact match and pairs back the receipt the person
    rejected. The retired id is REPLACED, not kept beside the new one: it
    names a row every read hides, and a memory still naming it would keep
    that row looking like it had something left to re-anchor.

    Pairs are positional (`old_ids[i]` became `new_ids[i]`); an old id
    listed twice keeps its first successor. Returns receipts rewritten."""
    mapping: dict[str, str] = {}
    for o, n in zip(old_ids, new_ids):
        if o and n and o != n:
            mapping.setdefault(o, n)
    if not mapping:
        return 0
    olds, news = list(mapping), list(mapping.values())
    return conn.execute(
        """UPDATE receipts r
              SET unmatched_txn_ids = ARRAY(
                  SELECT x.id FROM (
                      SELECT COALESCE(m.new_id, u.id) AS id,
                             min(u.ord) AS ord
                        FROM unnest(r.unmatched_txn_ids)
                             WITH ORDINALITY AS u(id, ord)
                        LEFT JOIN unnest(%s::text[], %s::text[])
                             AS m(old_id, new_id) ON m.old_id = u.id
                       GROUP BY 1) x
                   ORDER BY x.ord)
            WHERE r.unmatched_txn_ids && %s::text[]""",
        (olds, news, olds)).rowcount


def purge_account_data(conn, account_id: str,
                       force_kept: bool = False) -> dict:
    """Hard-delete one account and its local data (transactions cascade;
    snapshot tables that lack FKs are cleaned explicitly). Does NOT touch
    the parent Item or Plaid — caller handles disconnect when needed.
    Returns counts of deleted rows. Refuses with SyncRunning while a sync
    holds the household's lock (see household_delete_lock).

    Imported rows a bank feed on another account relies on (see
    relied_on_by_feed) are not deleted, and neither is the account holding
    them: the result's `kept_transactions` counts them and `note` says
    why. `force_kept` deletes the rows kept only by a note naming no
    account (see kept_without_account); the owner was told first."""
    with household_delete_lock(conn):
        return _purge_account_data_locked(conn, account_id, (account_id,),
                                          force_kept)


def _purge_account_data_locked(conn, account_id: str,
                               dropping: tuple[str, ...],
                               force_kept: bool = False) -> dict:
    # a pair on a row about to go is unlinked first, so its counterpart
    # lets go of the transfer category the link gave it (see detach_pairs)
    from ..web.data import detach_pairs
    from .reanchor import hold_pairing_lock
    with conn.transaction():
        # the pairing lock before any row is touched, and the release and
        # the delete in one transaction under it: the matcher cannot hang
        # a receipt on one of these rows in between
        hold_pairing_lock(conn)
        doomed = [r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE account_id=%s", (account_id,))]
        keep = relied_on_by_feed(conn, doomed, dropping,
                                 release_accountless=force_kept)
        if keep:
            doomed = [d for d in doomed if d not in keep]
        detach_pairs(conn, doomed)
        release_matched_receipts(conn, doomed)
        n_txn = conn.execute(
            "DELETE FROM transactions WHERE account_id=%s"
            " AND NOT id = ANY(%s)", (account_id, list(keep))).rowcount
        if keep:
            # ordinary ledger rows from here on: no import owns them
            conn.execute(
                "UPDATE transactions SET raw = raw - '_batch' - '_batches'"
                " WHERE id = ANY(%s) AND jsonb_typeof(raw) = 'object'",
                (list(keep),))
    n_hold = conn.execute(
        "DELETE FROM holdings WHERE account_id=%s",
        (account_id,)).rowcount
    n_crypto = conn.execute(
        "DELETE FROM crypto_holdings WHERE account_id=%s",
        (account_id,)).rowcount
    conn.execute("DELETE FROM liabilities WHERE account_id=%s", (account_id,))
    if keep:
        # The account stays to hold the kept rows, so everything that
        # names it — its links, the budget settings — stays as it was.
        note = kept_rows_note(len(keep))
        log_sync(conn, "account-purge", 0,
                 error=f"account {account_id}: {note}")
        return {"accounts": 0, "transactions": n_txn, "holdings": n_hold,
                "crypto_holdings": n_crypto, "kept_transactions": len(keep),
                "note": note + " The account stays to hold them."}
    conn.execute("DELETE FROM account_links WHERE account_id=%s", (account_id,))
    # budget excluded list may name this account — scrub it. Under the row
    # lock: this is a load-modify-save on the settings blob, and a
    # concurrent settings save would otherwise discard one side outright.
    try:
        from ..engine import budget
        with budget.config_txn(conn) as cfg:
            excl = list(cfg.get("excluded_accounts") or [])
            if account_id in excl:
                cfg["excluded_accounts"] = [a for a in excl if a != account_id]
                if not cfg["excluded_accounts"]:
                    cfg.pop("excluded_accounts", None)
            if cfg.get("checking_account_id") == account_id:
                cfg.pop("checking_account_id", None)
            # the same class, two keys further on: a savings goal keeps
            # its account by id, and a dismissed link pairing keys on a
            # sorted id pair. Left behind, the goal counts nothing for
            # ever and the rejected pairing starts being suggested again
            # a goal that named this account keeps naming it. Dropping the
            # key does NOT neutralise the goal — _matched_net simply omits
            # the account filter, so a goal that also carries match tokens
            # starts summing transfers across every account (the
            # dashboard-wide phantom progress is_plan_only exists to
            # prevent). A dead id matches nothing, which is the honest
            # answer for a goal whose destination the person deleted.
            goals = cfg.get("savings_goals")
            if isinstance(goals, list):
                for g in goals:
                    if isinstance(g, dict) and g.get("account_id") == account_id:
                        g["account_deleted"] = True
            dismissed = cfg.get("link_dismissed")
            if isinstance(dismissed, list):
                kept = [k for k in dismissed
                        if account_id not in str(k).split("|")]
                if len(kept) != len(dismissed):
                    if kept:
                        cfg["link_dismissed"] = kept
                    else:
                        cfg.pop("link_dismissed", None)
    except Exception:                                        # noqa: BLE001
        pass
    n_acct = conn.execute(
        "DELETE FROM accounts WHERE id=%s", (account_id,)).rowcount
    # the per-connection entity scope was stamped at checkout; an account
    # leaving a business must leave the scope too before a later read here
    tenancy.set_entity_scope(conn)
    return {"accounts": n_acct, "transactions": n_txn,
            "holdings": n_hold, "crypto_holdings": n_crypto,
            "kept_transactions": 0}


def purge_item_accounts(conn, item_id: str,
                        force_kept: bool = False) -> dict:
    """Delete every account hanging off an item (and, for a SimpleFIN
    bridge, every account under its per-bank children). `force_kept` as
    for purge_account_data."""
    with household_delete_lock(conn):
        return _purge_item_accounts_locked(conn, item_id, force_kept)


def item_account_ids(conn, item_id: str) -> list[str]:
    """Every account under an item, a SimpleFIN bridge's children
    included."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE item_id=%s OR item_id LIKE %s",
        (item_id, item_id + ":%")).fetchall()]


def _purge_item_accounts_locked(conn, item_id: str,
                                force_kept: bool = False) -> dict:
    ids = item_account_ids(conn, item_id)
    totals = {"accounts": 0, "transactions": 0, "holdings": 0,
              "crypto_holdings": 0, "kept_transactions": 0}
    for aid in ids:
        # every account of the item goes in this one delete, so a note
        # naming any of them protects nothing (see relied_on_by_feed)
        part = _purge_account_data_locked(conn, aid, tuple(ids),
                                          force_kept)
        for k in totals:
            totals[k] += part[k]
    if totals["kept_transactions"]:
        totals["note"] = (kept_rows_note(totals["kept_transactions"])
                          + " The accounts holding them stay.")
    # drop empty child items (bridge itself stays archived for audit)
    conn.execute(
        "DELETE FROM items WHERE aggregator='simplefin-org' AND id LIKE %s "
        "AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.item_id=items.id)",
        (item_id + ":%",))
    # A purged SimpleFIN bank keeps its archived row: it is the tombstone
    # every pull skips, and without it the bridge, which still carries the
    # bank, would bring it back as new and deep-pull the history the owner
    # just deleted. The bridge's mark comes off it, so a new setup token
    # does not bring it back either.
    conn.execute(
        "UPDATE items SET archived_reason=NULL WHERE id=%s "
        "AND aggregator='simplefin-org' AND archived_reason=%s",
        (item_id, BRIDGE_DISCONNECT))
    # if the item itself has no accounts left and is archived/manual-ish,
    # leave the archived row (history of connection); only delete empty
    # non-aggregator shells when nothing references them
    return totals


def item_is_live(status: str | None) -> bool:
    """True when the connection is still syncing (not user/reaper archived)."""
    return (status or "ok") != "archived"


def _without_nul(rec):
    """A Transaction or Account with U+0000 dropped from its text and its
    raw record (see upsert_transactions)."""
    changes = {f.name: strip_nul(v) for f in dataclasses.fields(rec)
               if isinstance(v := getattr(rec, f.name), (str, dict))}
    changes = {k: v for k, v in changes.items() if v != getattr(rec, k)}
    return dataclasses.replace(rec, **changes) if changes else rec


def _finite_or_none(v, account_id: str) -> float | None:
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a JSON integer too wide for a double
        n = math.nan
    if isinstance(v, bool) or not math.isfinite(n):
        log.warning("account %s: balance %r is not a finite number; "
                    "stored as unknown", account_id, v)
        return None
    return n


def upsert_accounts(conn, item_id: str, accounts: list[Account]) -> None:
    # Every aggregator's accounts arrive here, and accounts.id refuses a
    # comma: an id holding one is stored under the name the rename
    # migration gave it (adopt.stored_account_id), so no source's pull can
    # fail the constraint or split off a second copy of a renamed account.
    from .adopt import stored_account_id
    renamed: dict[str, str] = {}
    for a in accounts:
        a = _without_nul(a)
        aid = stored_account_id(conn, a.id, renamed)
        # a balance is a finite number or unknown: NaN or infinity from a
        # feed would be stored, and then every reply carrying the account
        # fails to serialise; a string that is not a number would fail the
        # insert and the pull with it
        cur_bal = _finite_or_none(a.balance_current, a.id)
        avail_bal = _finite_or_none(a.balance_available, a.id)
        conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                   balance_current, balance_available, currency, updated_at, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   item_id=EXCLUDED.item_id,
                   name=EXCLUDED.name,
                   type=CASE WHEN accounts.type_user_set
                             THEN accounts.type ELSE EXCLUDED.type END,
                   subtype=CASE WHEN accounts.type_user_set
                                THEN accounts.subtype ELSE EXCLUDED.subtype END,
                   mask=EXCLUDED.mask,
                   -- user_removed accounts stay out of active lists: sync must
                   -- not re-surface a balance after the user hid the account
                   balance_current=CASE WHEN accounts.user_removed_at IS NOT NULL
                                        THEN NULL
                                        ELSE EXCLUDED.balance_current END,
                   balance_available=CASE WHEN accounts.user_removed_at IS NOT NULL
                                          THEN NULL
                                          ELSE EXCLUDED.balance_available END,
                   currency=EXCLUDED.currency, updated_at=now(),
                   raw=EXCLUDED.raw""",
            (aid, item_id, a.name, a.type, a.subtype, a.mask,
             cur_bal, avail_bal, a.currency, jsonb(a.raw)))
        # display_name deliberately untouched: a hand override is sync-proof
        # type/subtype untouched when type_user_set: the classification a
        # person set is sacred (same rule as category_override on
        # transactions)
        # user_removed_at deliberately untouched — hiding is sacred too


# Types whose balance the engine stores as a POSITIVE amount owed (Plaid's
# convention): a card's debt, and a loan's — the net-worth property layer
# subtracts a loan balance, and the runway reads a card's the same way.
# SimpleFIN instead passes the institution's own sign through, which is
# negative for money owed, so crossing into or out of this set re-signs the
# balance. Both the classify door below and simplefin.sync key off this one
# tuple, or the hourly pull would write the bank's sign back over a
# correction within the hour.
DEBT_TYPES = ("credit", "loan")


def mark_type_user_set(conn, account_id: str, type_: str,
                       subtype: str | None = None) -> None:
    """Persist the user's account classification and PIN it: once
    type_user_set is true, upsert_accounts never overwrites type/subtype and
    the SimpleFIN debt-balance sign flip follows this persisted type
    instead of the name heuristic. The web /accounts/classify route calls
    this rather than a bare UPDATE.

    SimpleFIN reports debt negative and the sync flips it by the persisted
    type, so a classification that crosses the debt boundary (see
    DEBT_TYPES) re-signs the stored balances here, in the same statement.
    Left to the next pull, a card the heuristic missed reads as an asset in
    net worth (the debt sign-flipped) and as no debt at all in the runway
    for up to an hour — right after the user corrected it. Plaid and MX
    store debt positive already, so their balances are never touched.

    The re-sign covers every balance this schema keeps for an account:
    there is no per-account balance-history table for it to miss (a test
    asserts that, so adding one forces the author back here). The recorded
    history — `networth_snapshot`, one aggregate row per night — needs no
    rewrite either: the stored sign and the reading convention flip
    together, so a night recorded under the wrong type still holds the
    right dollar total. What those nights got wrong is which asset CLASS
    the money sat in, and that is a sum over every account with no
    per-account contribution kept, so it cannot be recomputed; it is also
    never read back (only `total` feeds the trend) and the nightly
    snapshot files the corrected account correctly from its next run.
    Proved in tests/test_account_type_change_resigns_history.py."""
    row = conn.execute(
        "SELECT a.type, i.aggregator FROM accounts a "
        "JOIN items i ON i.id = a.item_id WHERE a.id=%s",
        (account_id,)).fetchone()
    flip = (row is not None
            and row["aggregator"] in ("simplefin", "simplefin-org")
            and (row["type"] in DEBT_TYPES) != (type_ in DEBT_TYPES))
    conn.execute(
        "UPDATE accounts SET type=%s, subtype=%s, type_user_set=TRUE, "
        "balance_current = CASE WHEN %s THEN -balance_current "
        "                       ELSE balance_current END, "
        "balance_available = CASE WHEN %s THEN -balance_available "
        "                         ELSE balance_available END "
        "WHERE id=%s", (type_, subtype, flip, flip, account_id))


def user_set_types(conn) -> dict[str, str]:
    """{account_id: type} for every account the user has classified —
    the persisted type wins over any sync-side heuristic."""
    return {r["id"]: r["type"] for r in conn.execute(
        "SELECT id, type FROM accounts WHERE type_user_set").fetchall()}


def hidden_account_ids(conn) -> set[str]:
    """Accounts the user hid. Aggregators fetch per ITEM, so a hidden
    account's rows still arrive on every sync — we simply refuse to store
    them, which is what "don't pull data for this account" means in
    practice for a connection we are deliberately keeping alive."""
    return {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE user_removed_at IS NOT NULL").fetchall()}


def credit_account_ids(conn) -> set[str]:
    """Accounts that are credit cards — the context that lets a bare
    'payment' description be read as a card settlement (see flowmap)."""
    return {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE type = 'credit'").fetchall()}


# Venmo rows arrive with the merchant swallowed: the aggregator normalizes
# name to "Venmo" and leaves merchant_name empty, while the bank's own
# statement line — kept verbatim in raw.original_description — says who the
# money actually went to ("VENMO *TAYLOR SAMPLE 1234 EXAMPLE PKWY
# 5555550100, NY, US"). Recover the counterparty and make it the merchant,
# so each person groups, searches, and totals like any other payee. Noise
# stripped: the tail from the first digit run (Venmo's processing street
# number / phone), a trailing "<ST>, US" geo suffix, and — when the name
# itself is mixed-case — trailing ALL-CAPS city words ("Taylor Sample NEW
# YORK"). Bare descriptors ("VENMO PAYMENT", "VENMO CASHOUT") name nobody
# and yield None. Migration 081 applied the same transform to stored rows.
_VENMO_ADDRESS_TAIL = re.compile(r"\s+\d.*$")
_VENMO_GEO_TAIL = re.compile(r",?\s+[A-Z]{2},?\s+US\s*$")


def venmo_counterparty(orig_desc: str | None) -> str | None:
    if not orig_desc:
        return None
    s = orig_desc.strip()
    if not s.upper().startswith("VENMO"):
        return None
    rest = s[len("VENMO"):].strip()
    if not rest.startswith("*"):
        return None
    name = _VENMO_GEO_TAIL.sub("", _VENMO_ADDRESS_TAIL.sub("", rest[1:]))
    words = name.split()
    if any(any(c.islower() for c in w) for w in words):
        while words and words[-1].isupper() and len(words[-1]) >= 2:
            words.pop()
    return _finish_counterparty("Venmo", " ".join(words))


# Zelle statement lines name BOTH ends ("Zelle payment from A (Acme
# Checking XXXXXX1234) to B"), and banks prefix them with the
# transfer kind ("ACH DEBIT — …"). The amount sign says which end is
# the counterparty: money out → the "to" side, money in → the "from" side
# — the account holder's own name never has to be known. The own side's
# "(Bank … Account …)" parenthetical and an ACH "RECEIVER" tag are noise.
# Some imports truncate the line before " to " ever appears; those rows
# name nobody recoverable and yield None. Migration 082 applied the same
# transform to stored rows.
#
# Which " to " is the boundary matters: a counterparty can be named
# "Farm to Table LLC", and always splitting at the first " to " turned
# such a payment into merchant "Zelle — Farm". When the own side's
# "(Bank … Account …)" parenthetical is present it anchors the true
# boundary — the " to " right after the closing paren. Without one, the
# split is chosen so the side being RETURNED stays whole (last " to "
# when the from side is the counterparty, first when the to side is),
# which sacrifices only the account holder's own name — never shown.
_ZELLE_REST = re.compile(r"zelle payment from (?P<rest>.+)$", re.IGNORECASE)
_ZELLE_PAREN_SPLIT = re.compile(
    r"^(?P<frm>.+?\([^)]*\))\s+to\s+(?P<to>.+)$", re.IGNORECASE)
_ZELLE_DESCRIPTOR = re.compile(r"zelle payment from", re.IGNORECASE)
_ZELLE_PAREN_TAIL = re.compile(r"\s*\(.*$")
_ZELLE_RECEIVER_TAG = re.compile(r"\s+RECEIVER\s*$", re.IGNORECASE)


def zelle_counterparty(desc: str | None, money_in: bool) -> str | None:
    if not desc:
        return None
    m = _ZELLE_REST.search(desc)
    if not m:
        return None
    rest = m.group("rest")
    pm = _ZELLE_PAREN_SPLIT.match(rest)
    if pm:
        frm, to = pm.group("frm"), pm.group("to")
    else:
        seps = list(re.finditer(r" to ", rest, flags=re.IGNORECASE))
        if not seps:
            return None
        sep = seps[-1] if money_in else seps[0]
        frm, to = rest[:sep.start()], rest[sep.end():]
    side = frm if money_in else to
    side = _ZELLE_RECEIVER_TAG.sub("", _ZELLE_PAREN_TAIL.sub("", side))
    return _finish_counterparty("Zelle", side)


# A warehouse club or grocery chain that also sells fuel is two merchants
# wearing one name. A tank of petrol and a warehouse run are different
# sizes of purchase; blended under one payee the ledger can answer neither
# "what does a tank cost" nor "what does a warehouse run cost", and the
# recurring detector chases a payee whose amount never settles.
#
# Plaid already knows. It sends merchant_name "Northwind Club" (the brand)
# beside name "Northwind Club Gas" (the outlet), and the bank's own statement
# line says "NORTHWIND CLUB GAS #1234" vs "NORTHWIND CLUB WHSE #1234". What the ledger drops is the
# name: payee is COALESCE(merchant_name, name), so the brand wins and the
# specific label is discarded. Promoting `name` wholesale is not the fix —
# it is usually the shouted statement string ("NORTHWIND MARKET #1234 SPRINGFIELD",
# "STREAMFLIX *SUBSCRIPTION"), which is exactly why
# merchant_name is preferred in the first place. So read the narrow signal
# out of either text instead: the BRAND followed by a fuel word, which is
# what a pump's own label looks like in both fields.
#
# What cannot do this job:
#   * merchant_entity_id — identical for the pump, the warehouse and the
#     website. Plaid's entity model says the chain is one merchant.
#   * the category — TRANSPORTATION_GAS is right on every one of these
#     rows, but fuel is also the category most often wrong elsewhere, and
#     a merchant derived from a category lets one bad row rename a payee.
#   * the MCC (5542 is the automated fuel dispenser) — mostly null.
# A chain whose fuel rows carry neither label is left alone rather than
# split on a hunch.
# Brand and text are matched on their ALPHANUMERICS only, character by
# character, so punctuation and spacing cannot hide a match: "K-B-X" finds
# "K-B-X GAS" and "KBX GAS STATION" alike, and "Lowe's" would find
# "LOWES FUEL". Nothing brand-specific — the pattern is built from whatever
# the merchant is called, and because only its alphanumerics survive, a
# merchant name can never inject regex syntax.
#
# The fuel word must be the NEXT token. That single requirement is what
# keeps the rule honest: "NORTHWIND CLUB WHSE" and "WWW NORTHWIND CLUB COM" fail on the
# following token, "SHELL OIL … AUTO FUEL DISPEN" fails on the words in
# between (and Shell is a filling station anyway — there is no arm to split
# off), and "VEGAS BUFFET" fails for a brand "Vega" because the S is still
# alphanumeric where a separator has to be.
_FUEL_WORD = "(gas|fuel)"
_MIN_BRAND = 3          # two letters match too much to be evidence of anything


def _brand_pattern(brand: str) -> str | None:
    """A regex for this brand followed immediately by a fuel word."""
    letters = re.sub(r"[^A-Za-z0-9]", "", brand)
    if len(letters) < _MIN_BRAND:
        return None
    spaced = r"[^A-Za-z0-9]*".join(letters)
    return (r"(?:^|[^A-Za-z0-9])" + spaced
            + r"[^A-Za-z0-9]+" + _FUEL_WORD + r"(?:[^A-Za-z0-9]|$)")


def fuel_arm(merchant: str | None, orig_desc: str | None,
             name: str | None = None) -> str | None:
    """"Northwind Club" + "NORTHWIND CLUB GAS #1234" (or Plaid name
    "Northwind Club Gas") -> "Northwind Club Gas". None when neither text proves the pump."""
    brand = (merchant or "").strip()
    if not brand:
        return None
    # already the fuel arm's own name — nothing to promote. This is also
    # what keeps a natural-gas utility ("Northwind Gas") from being
    # renamed "Northwind Gas Gas".
    if re.search(rf"\b{_FUEL_WORD}\b", brand, re.IGNORECASE):
        return None
    pattern = _brand_pattern(brand)
    if not pattern:
        return None
    for text in (name, orig_desc):
        if not text:
            continue
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return f"{brand} {m.group(1).capitalize()}"
    return None


def _finish_counterparty(channel: str, name: str) -> str | None:
    """"Venmo — Casey Example": the person is their own merchant, but the
    rail stays visible — the same person via Venmo and via Zelle are
    different things to a reader (and to grouping)."""
    name = name.strip(" ,")
    if not name:
        return None
    if name.isupper():
        name = name.title()
    # .title() mangles the one legal suffix people actually send money to
    return f"{channel} — " + re.sub(r"\bLlc\b", "LLC", name)


def enrichment_from_raw(raw: dict | None) -> dict:
    """The aggregator's facts about a row, read from its raw record the same
    way for every source (Plaid's shape; anything else yields NULLs):
    check number, payment channel, the payment app in front of the merchant,
    location, MCC, authorized date. Pure; the migration-098 backfill does the
    same in SQL."""
    out = {k: None for k in ("check_number", "payment_channel", "payment_processor",
                             "location_city", "location_region", "location_address",
                             "location_postal", "location_lat", "location_lon",
                             "location_store", "mcc", "authorized_date")}
    if not isinstance(raw, dict):
        return out

    def _s(v):
        v = (str(v).strip() if v is not None else "")
        return v or None
    out["check_number"] = _s(raw.get("check_number"))
    out["payment_channel"] = _s(raw.get("payment_channel"))
    out["mcc"] = _s(raw.get("merchant_category_code"))
    for c in raw.get("counterparties") or []:
        if isinstance(c, dict) and c.get("type") in ("payment_app", "payment_terminal"):
            out["payment_processor"] = _s(c.get("name"))
            break
    loc = raw.get("location") or {}
    if isinstance(loc, dict):
        out["location_city"] = _s(loc.get("city"))
        out["location_region"] = _s(loc.get("region"))
        out["location_address"] = _s(loc.get("address"))
        out["location_postal"] = _s(loc.get("postal_code"))
        out["location_store"] = _s(loc.get("store_number"))
        for k in ("lat", "lon"):
            try:
                out[f"location_{k}"] = float(loc[k]) if loc.get(k) is not None else None
            except (TypeError, ValueError):
                out[f"location_{k}"] = None
    ad = _s(raw.get("authorized_date"))
    if ad:
        try:
            out["authorized_date"] = dt.date.fromisoformat(ad[:10])
        except ValueError:
            out["authorized_date"] = None
    return out


# A bank descriptor and a payee name are a few dozen characters. What
# arrives here can be anything: a file import maps two of the source's
# free-text columns onto them, and a tenant-supplied bridge streams what it
# likes. Two absurd strings cost far more than their own row — the identity
# resolver compares them word against word, which is quadratic and runs
# under the household's identity lock, and the alias map keys on whichever
# of them the row is identified by, where Postgres refuses a btree entry
# past a couple of thousand bytes and so aborts the resolve for every row in
# that sync. Clamped in characters (a character may be four bytes), well
# inside that limit, and clamped rather than refused: the row is the
# household's record of money moving, and dropping it over a long
# descriptor is the worse trade. Nothing honest is ever touched. The restore
# door clamps its own cells the same way, for the same reasons.
MAX_DESCRIPTOR = 600


# What a re-reported row does to its merchant name, as the SET clauses of an
# ON CONFLICT DO UPDATE on transactions. One spelling, because every ingest
# door that writes merchant_name has to agree: a row whose aggregator name
# was set aside keeps it set aside while the feed repeats the same two
# strings, and goes back for a fresh resolve (name in, set-aside cleared,
# merchant dropped) the moment the feed says something new. A door that wrote
# merchant_name alone would leave the name in BOTH columns.
MERCHANT_NAME_ON_CONFLICT = """merchant_name = CASE
    WHEN transactions.merchant_name_set_aside IS NOT NULL
         AND EXCLUDED.name
             IS NOT DISTINCT FROM transactions.name
         AND EXCLUDED.merchant_name IS NOT DISTINCT FROM
             transactions.merchant_name_set_aside
    THEN NULL ELSE EXCLUDED.merchant_name END,
merchant_name_set_aside = CASE
    WHEN transactions.merchant_name_set_aside IS NOT NULL
         AND EXCLUDED.name
             IS NOT DISTINCT FROM transactions.name
         AND EXCLUDED.merchant_name IS NOT DISTINCT FROM
             transactions.merchant_name_set_aside
    THEN transactions.merchant_name_set_aside
    ELSE NULL END,
merchant_id = CASE
    WHEN transactions.merchant_name_set_aside IS NOT NULL
         AND NOT (EXCLUDED.name
                      IS NOT DISTINCT FROM transactions.name
                  AND EXCLUDED.merchant_name IS NOT DISTINCT FROM
                      transactions.merchant_name_set_aside)
    THEN NULL ELSE transactions.merchant_id END"""


def _descriptor(v):
    return v[:MAX_DESCRIPTOR] if isinstance(v, str) else v


# A statement line routinely prints the full account or card number, and a
# file importer puts that line (or the description cut from it) straight
# into the row's name. The name is the most travelled field there is — the
# export, the portability archive, the categorizer's prompt, webhooks and
# every member's ledger view — so a run of eight or more digits keeps only
# its last four. Amounts, dates and short check or store numbers all break
# under eight consecutive digits, so genuine ledger content survives.
# Aggregator rows are left alone: their name is the feed's own word, which
# every sync restates. The id prefixes are the file importers' own; keep
# migrations 153, 178 and 181 and restore in step with this list.
LONG_DIGITS_RE = re.compile(r"\d{8,}")
FILE_IMPORT_ID_PREFIXES = ("pdf:", "csv:", "ofx:", "qif:", "mint:", "ynab:",
                           "monarch:", "copilot:", "simplifi:")


def mask_long_digits(text):
    """`text` with every run of 8+ digits cut to "…" and its last four."""
    if not isinstance(text, str):
        return text
    return LONG_DIGITS_RE.sub(lambda m: "…" + m.group()[-4:], text)


# The source cells of a file row that hold free text a statement prints a
# number into, by header; any other cell holding the row's own name or
# merchant is matched by value, whatever the file called its column.
# A header is free text when it is one of the exact names below, or when
# it contains one of the words after them ("Transaction Details",
# "Extended Description", "Memo/Notes", "Payee Name"): banks spell their
# free-text columns every way, and an exact list kept the full number
# under each spelling it missed. The cells a row has beyond its header
# arrive under the key None (an unquoted comma in the last free-text
# column) — stored as "null" — and are that column's text by position,
# so they count as free text too. Migrations 178 and 181 apply the same
# keys to rows already stored; keep them in step.
_FREE_TEXT_KEYS = frozenset((
    "name", "memo", "note", "notes", "payee", "description",
    "original description", "original statement"))
_FREE_TEXT_WORDS = ("memo", "note", "description", "details", "payee",
                    "narrative", "particulars")
_OVERFLOW_KEYS = frozenset(("", "null"))


def _free_text_key(k) -> bool:
    if k is None:
        return True
    key = str(k).strip().lower()
    return (key in _FREE_TEXT_KEYS or key in _OVERFLOW_KEYS
            or any(w in key for w in _FREE_TEXT_WORDS))


def mask_file_raw(raw, *texts):
    """A file row's raw with long digit runs masked (mask_long_digits) in
    every cell that holds the row's name or merchant, or that a statement
    fills with free text (a memo, a payee, a description, the overflow
    cells past the header).

    The stored name is masked, and raw is plaintext JSONB that leaves the
    instance verbatim through /export and the portability archive, so a
    raw cell must not keep the full number the name lost. Cells are
    compared masked, so a name already masked still finds the cell that
    holds it whole. A cell holding a list (the overflow cells) is read
    element by element under the same rule. Every other cell is left as
    it was."""
    if not isinstance(raw, dict):
        return raw
    want = {mask_long_digits(x.strip()).lower() for x in texts
            if isinstance(x, str) and x.strip()}

    def cut(v, free):
        if isinstance(v, str) and (
                free or mask_long_digits(v.strip()).lower() in want):
            return mask_long_digits(v)
        return v

    out = {}
    for k, v in raw.items():
        free = _free_text_key(k)
        if isinstance(v, (list, tuple)):
            v = [cut(x, free) for x in v]
        else:
            v = cut(v, free)
        out[k] = v
    return out


class FileImportIds:
    """Row ids for one file import into one account.

    A file row carries no id of its own, so it gets a hash of
    account|date|cents|name plus its occurrence among identical rows. The
    name is hashed MASKED (mask_long_digits): hashed over the full number,
    the id would put a card number (issuer prefix known, last four shown
    in the stored name) a small search away from anyone holding an export.

    Rows imported before names were masked were keyed on the full name.
    `resolve` swaps a new id for that older one when the older row exists,
    so re-importing an old file updates the row it made then instead of
    adding a second copy beside it.
    """

    def __init__(self, prefix: str, account_id: str):
        self.prefix = prefix
        self.account_id = account_id
        self._occ: dict[str, int] = {}
        self._legacy_occ: dict[str, int] = {}
        self._legacy_of: dict[str, str] = {}
        # every id this import minted -> the hash inputs after the account
        # id, so the same line can be re-hashed under a former account id
        self._tails: dict[str, list[str]] = {}

    def _hash(self, base: str, n: int) -> str:
        return f"{self.prefix}:" + hashlib.sha1(
            f"{base}|{n}".encode()).hexdigest()

    def id_for(self, date, amount, name: str) -> str:
        tail = f"|{date.isoformat()}|{round(amount * 100)}|"
        head = f"{self.account_id}{tail}"
        masked = mask_long_digits(name)
        base = head + masked.lower()
        self._occ[base] = self._occ.get(base, 0) + 1
        tid = self._hash(base, self._occ[base])
        tails = [f"{tail}{masked.lower()}|{self._occ[base]}"]
        if masked != name:
            old = head + name.lower()
            self._legacy_occ[old] = self._legacy_occ.get(old, 0) + 1
            self._legacy_of[tid] = self._hash(old, self._legacy_occ[old])
            tails.append(f"{tail}{name.lower()}|{self._legacy_occ[old]}")
        self._tails[tid] = tails
        return tid

    def legacy_id(self, tid: str) -> str | None:
        """The pre-masking id `id_for` paired with `tid`, if the name
        needed masking."""
        return self._legacy_of.get(tid)

    def resolve(self, conn, txns: list[Transaction]) -> list[Transaction]:
        """`txns` with each id swapped for the id its row already stands
        under: the pre-masking id, or the id an earlier import of the same
        line gave it while the account had another id."""
        if self._legacy_of:
            have = {x["id"] for x in conn.execute(
                "SELECT id FROM transactions WHERE id = ANY(%s)",
                (list(self._legacy_of.values()),)).fetchall()}
            txns = [dataclasses.replace(t, id=self._legacy_of[t.id])
                    if self._legacy_of.get(t.id) in have else t
                    for t in txns]
        return self._adopt_renamed(conn, txns)

    def _former_account_ids(self, conn) -> list[str]:
        """Every id this account has stood under before: the ids adoption
        moved it off (followed back through earlier adoptions), then the
        comma spellings a comma rename could have started from.

        Recorded adoptions come first, so a capped search keeps them. An id
        the app minted itself (`manual:`, slugged to [a-z0-9-]) never held
        a comma, so its dashes are not taken for renamed commas: that would
        multiply every imported line by up to 63 spellings for nothing."""
        seen = {self.account_id}
        recorded: list[str] = []
        spelled_ids: list[str] = []
        todo = [self.account_id]
        while todo:
            cur = todo.pop()
            if not cur.startswith("manual:"):
                for spelled in _comma_spellings(cur):
                    if spelled not in seen:
                        seen.add(spelled)
                        spelled_ids.append(spelled)
            for r in conn.execute(
                    "SELECT old_account_id FROM account_adoptions"
                    " WHERE account_id = %s", (cur,)).fetchall():
                if r["old_account_id"] not in seen:
                    seen.add(r["old_account_id"])
                    recorded.append(r["old_account_id"])
                    todo.append(r["old_account_id"])
        return sorted(recorded) + sorted(spelled_ids)

    def _adopt_renamed(self, conn, txns: list[Transaction]
                       ) -> list[Transaction]:
        """Swap in the id of an earlier import of the same line on the
        same account when this import would otherwise add it again.

        The id hashes the account id, and an account can change id while
        its rows keep theirs: a reconnect adopts restored history onto the
        new link's id, and a comma id is renamed. Neither rewrites the
        rows' ids, and the cross-source dedup deliberately skips the
        importer's own rows, so without this a re-imported or overlapping
        statement doubles every line the feed does not also cover.

        The earlier id is recomputed exactly: the same date, cents, name
        and occurrence number, hashed under each id the account has stood
        under before. Only an id this scheme produced can be adopted, never
        a row that merely looks the same, so two genuinely distinct
        charges (a bank's FITID-keyed rows above all, which this scheme
        never minted) stay two rows. A third identical line in a new file
        hashes to an occurrence no earlier import made, so it is added.

        The work is bounded: nothing is hashed unless the account already
        holds rows of this importer that this file did not mint, the
        candidate count is capped (ADOPT_MAX_GUESSES), and the lookups run
        in slices, so memory never scales with rows times former ids.
        """
        missing = [t for t in txns if t.id in self._tails
                   and t.account_id == self.account_id]
        if not missing:
            return txns
        present = {x["id"] for x in conn.execute(
            "SELECT id FROM transactions WHERE id = ANY(%s)",
            ([t.id for t in missing],)).fetchall()}
        missing = [t for t in missing if t.id not in present]
        if not missing:
            return txns
        # an account holding no row of this importer beyond the ones this
        # file made has nothing an earlier import could have left behind —
        # the first import of a file always stops here
        if not conn.execute(
                "SELECT 1 FROM transactions WHERE account_id = %s"
                "   AND left(id, %s) = %s AND id <> ALL(%s) LIMIT 1",
                (self.account_id, len(self.prefix) + 1, self.prefix + ":",
                 [t.id for t in txns])).fetchone():
            return txns
        former = self._former_account_ids(conn)
        if not former:
            return txns
        per_former = sum(len(self._tails[t.id]) for t in missing)
        keep = max(1, ADOPT_MAX_GUESSES // max(1, per_former))
        if keep < len(former):
            log.warning("file import into %s: id adoption limited to %d of "
                        "%d former account ids", self.account_id, keep,
                        len(former))
            former = former[:keep]
        budget = ADOPT_MAX_GUESSES
        taken = {t.id for t in txns}
        swap: dict[str, str] = {}
        for i in range(0, len(missing), ADOPT_SLICE):
            if budget <= 0:
                break
            part = []
            guess: dict[str, list[str]] = {}
            for t in missing[i:i + ADOPT_SLICE]:
                gs = [self._hash(a + tail.rsplit("|", 1)[0],
                                 int(tail.rsplit("|", 1)[1]))
                      for a in former for tail in self._tails[t.id]]
                budget -= len(gs)
                guess[t.id] = gs
                part.append(t)
                if budget <= 0:
                    break
            have = {x["id"] for x in conn.execute(
                "SELECT id FROM transactions WHERE id = ANY(%s)"
                "   AND account_id = %s",
                (sorted({g for gs in guess.values() for g in gs}),
                 self.account_id)).fetchall()}
            for t in part:
                for g in guess[t.id]:
                    if g in have and g not in taken:
                        swap[t.id] = g
                        taken.add(g)
                        break
        if not swap:
            return txns
        return [dataclasses.replace(t, id=swap[t.id]) if t.id in swap else t
                for t in txns]


# Ceiling on the ids one file import may hash while looking for the rows an
# earlier import left under a former account id, and the rows looked up per
# query. A 50,000-row file under one or two former ids fits whole; an id
# with many dashes cannot turn one upload into millions of hashes.
ADOPT_MAX_GUESSES = 200_000
ADOPT_SLICE = 2_000


def _comma_spellings(aid: str, limit: int = 6) -> list[str]:
    """The ids a comma rename could have turned into `aid`: each '-' may
    have been a ',' (the plain rename), and an id ending in '-' plus eight
    hex digits may be the taken-name form, kept only when the md5 agrees.
    Bounded, so an id with many dashes costs nothing much."""
    from .adopt import account_id_when_taken
    out: list[str] = []
    stems = [aid]
    m = re.fullmatch(r"(.+)-[0-9a-f]{8}", aid)
    if m:
        stems.append(m.group(1))
    for stem in stems:
        pos = [i for i, c in enumerate(stem) if c == "-"]
        if not pos or len(pos) > limit:
            continue
        for mask in range(1, 1 << len(pos)):
            chars = list(stem)
            for k, i in enumerate(pos):
                if mask >> k & 1:
                    chars[i] = ","
            cand = "".join(chars)
            if stem is aid or account_id_when_taken(cand) == aid:
                out.append(cand)
    return out


def is_file_import_id(txn_id) -> bool:
    return isinstance(txn_id, str) and txn_id.startswith(
        FILE_IMPORT_ID_PREFIXES)


# The keys Plaid Enrich writes into a row's raw (sync/plaid.ENRICH_FIELDS)
# and its marker. A row that comes in again keeps them; see the upsert.
ENRICHED_RAW_KEYS = ("_enriched_at", "merchant_name", "website", "logo_url",
                     "check_number", "payment_channel", "merchant_entity_id",
                     "counterparties", "location",
                     "personal_finance_category")


def _write_rows(conn, txns: list[Transaction], hidden: set,
                cards: set) -> int:
    """Insert or refresh each row of a sync page; the count written."""
    n = 0
    for t in txns:
        if t.account_id in hidden:
            continue
        # every door funnels here, so this is where a NaN/inf amount — a
        # tenant-supplied bridge, a collector push, a hand-edited file —
        # is refused before it reaches DOUBLE PRECISION and poisons every
        # sum that touches the row
        #
        # Finite is not enough either: 1e308 fits a double, and every cents
        # conversion of it downstream (the import-overlap guard, the store
        # matchers, splits) overflows, failing that household's every sync.
        try:
            amt = float(t.amount)
            if not math.isfinite(amt) or abs(amt) > MAX_AMOUNT:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            log.warning("dropping transaction %s: amount %r is not a "
                        "finite number", t.id, t.amount)
            continue
        # A money-in row on a CARD that says "payment" is the card being
        # settled, whatever the aggregator called it. An aggregator can file
        # one settlement as LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT while its
        # siblings the same day arrive as card payments; disbursement is not
        # spend-excluded, so such a row counts as money in. Normalizing here
        # covers every aggregator at once, and category_override (the user's
        # word) is still never touched.
        cat_primary, cat_detailed = t.category_primary, t.category_detailed
        # the two identity strings, at a length a bank descriptor and a
        # payee name actually come in (see MAX_DESCRIPTOR) — read from here
        # on, so every derivation below and the row itself agree
        filed = is_file_import_id(t.id)
        line = _descriptor(mask_long_digits(t.name) if filed else t.name)
        named = _descriptor(mask_long_digits(t.merchant_name) if filed
                            else t.merchant_name)
        # who decided category_primary — the row can say WHY it is what it
        # is (Plaid's own PFC / an importer's map / a flow normalization /
        # the fuel-arm outlet). Machine and user rules re-stamp it later.
        cat_source = (None if cat_primary is None
                      else "plaid" if t.category_plaid else "import")
        if (t.account_id in cards and (t.amount or 0) < 0
                and flowmap.looks_like_card_payment_on_card(named or line)):
            cat_primary = "LOAN_PAYMENTS"
            cat_detailed = flowmap.CC_PAYMENT_DETAILED
            cat_source = "flow"
        # No merchant from the aggregator → recover the P2P counterparty
        # from the bank's statement line. Deterministic on the same input,
        # so a re-sync (pending → posted) re-derives the same merchant.
        desc = (t.raw or {}).get("original_description")
        merchant = named or venmo_counterparty(desc)
        # Zelle detail can live in `name` too (file imports put the whole
        # statement line there), and an aggregator-supplied merchant that
        # is itself just the truncated Zelle descriptor is noise, not a
        # merchant — the parsed counterparty beats it.
        zelle = zelle_counterparty(desc or line, (t.amount or 0) < 0)
        if zelle and (not merchant or _ZELLE_DESCRIPTOR.search(merchant)):
            merchant = zelle
        # The outlet, when a chain's fuel arm is identifiable, is recorded
        # BESIDE the merchant — never over it. merchant_name is the source's
        # word (merchant_dedup's doctrine, which the display layer follows):
        # overwriting it would discard what the aggregator said, and a
        # re-sync would have nothing to restore it from. Venmo and Zelle
        # above do write merchant_name, and the difference is real — they
        # fire only when the aggregator supplied nothing usable, so they
        # recover a merchant rather than override a correct one.
        merchant = _descriptor(mask_long_digits(merchant) if filed
                               else merchant)
        outlet = fuel_arm(merchant, desc, line)
        # A row the bank itself labels as this brand's pump is fuel,
        # whatever the aggregator guessed. An aggregator can file a grocery
        # chain's fuel-station line under groceries, which is how a tank of
        # petrol ends up inside a food budget. This is the same move the card-
        # payment normalization above makes: the statement line is evidence,
        # the aggregator's category is inference. category_override — the
        # user's own word — is still never touched anywhere here.
        # (Deliberately NOT behind the Plaid-confidence trust gate the machine
        # rules honour: the statement line saying GAS is direct evidence of
        # what was bought, and Plaid files those lines under groceries at
        # HIGH confidence — the gate exists to stop name-guessing rules
        # from contradicting a concrete label, not to stop the receipt.
        # category_source='fuel' says so on the row.)
        if outlet:
            cat_primary, cat_detailed = "TRANSPORTATION", "TRANSPORTATION_GAS"
            cat_source = "fuel"
        # what the aggregator knows about the row, as columns (migration
        # 098): every source's raw record is read the same way, so an
        # importer that has none of it simply writes NULLs
        en = enrichment_from_raw(t.raw)
        # Plaid fields: only overwrite when the importer supplied them
        # (Plaid). CSV/OFX leave them NULL on EXCLUDED → keep prior.
        # category_primary: still refreshed from the importer when present
        # (Plaid re-sync), then seed/LLM/user-merchant apply() re-refines;
        # category_override is never written here.
        cur = conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, merchant_outlet,
                   category_primary, category_detailed,
                   category_plaid, category_plaid_detailed,
                   category_plaid_confidence, pending, removed, raw,
                   category_source, check_number, payment_channel,
                   payment_processor, location_city, location_region,
                   location_address, location_postal, location_lat,
                   location_lon, location_store, mcc, authorized_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   date=EXCLUDED.date, amount=EXCLUDED.amount,
                   name=EXCLUDED.name,
                   merchant_outlet=EXCLUDED.merchant_outlet,
                   category_primary=COALESCE(EXCLUDED.category_primary,
                                             transactions.category_primary),
                   -- the source follows the primary: a re-sync that brings
                   -- a category re-stamps who decided it; one that brings
                   -- none keeps both
                   category_source=CASE WHEN EXCLUDED.category_primary IS NULL
                                        THEN transactions.category_source
                                        ELSE EXCLUDED.category_source END,
                   check_number=COALESCE(EXCLUDED.check_number, transactions.check_number),
                   payment_channel=COALESCE(EXCLUDED.payment_channel, transactions.payment_channel),
                   payment_processor=COALESCE(EXCLUDED.payment_processor, transactions.payment_processor),
                   location_city=COALESCE(EXCLUDED.location_city, transactions.location_city),
                   location_region=COALESCE(EXCLUDED.location_region, transactions.location_region),
                   location_address=COALESCE(EXCLUDED.location_address, transactions.location_address),
                   location_postal=COALESCE(EXCLUDED.location_postal, transactions.location_postal),
                   location_lat=COALESCE(EXCLUDED.location_lat, transactions.location_lat),
                   location_lon=COALESCE(EXCLUDED.location_lon, transactions.location_lon),
                   location_store=COALESCE(EXCLUDED.location_store, transactions.location_store),
                   mcc=COALESCE(EXCLUDED.mcc, transactions.mcc),
                   authorized_date=COALESCE(EXCLUDED.authorized_date, transactions.authorized_date),
                   category_detailed=COALESCE(EXCLUDED.category_detailed,
                                              transactions.category_detailed),
                   category_plaid=COALESCE(EXCLUDED.category_plaid,
                                           transactions.category_plaid),
                   category_plaid_detailed=COALESCE(
                       EXCLUDED.category_plaid_detailed,
                       transactions.category_plaid_detailed),
                   category_plaid_confidence=COALESCE(
                       EXCLUDED.category_plaid_confidence,
                       transactions.category_plaid_confidence),
                   pending=EXCLUDED.pending,
                   -- a row the aggregator re-reports comes back — unless it
                   -- was retired by a decision (a person's stuck hold, a
                   -- dedup's twin), which the feed re-reporting it every
                   -- hour must not undo. Every importer's upsert reads it
                   -- the same way (sync/coinbase.py, sync/plan_csv.py).
                   removed=CASE WHEN transactions.retired_at IS NOT NULL THEN 1 ELSE 0 END,
                   -- Setting the aggregator's name aside (engine/
                   -- merchant_identity) MOVED it to
                   -- merchant_name_set_aside and left merchant_name NULL,
                   -- on a judgement about the two strings this row then
                   -- carried. So a feed re-sending exactly those two
                   -- strings — which is what every hourly sync does for
                   -- every row it has — must not hand the name back; the
                   -- name it was compared against is the set-aside one,
                   -- not the NULL now standing in merchant_name.
                   --
                   -- A feed that restates EITHER string has said something
                   -- new about the payee: the judgement lapses, the new
                   -- name lands in merchant_name, and the row goes back for
                   -- a fresh resolve under what it now reads — which means
                   -- dropping the merchant too, since keeping it would file
                   -- the row under a merchant its own key no longer points
                   -- at. A row that was never set aside takes the feed's
                   -- name and keeps its merchant, as it always did.
                   """ + MERCHANT_NAME_ON_CONFLICT + """,
                   -- What Enrich learned about the row survives the source
                   -- sending the row again: a re-imported overlapping file
                   -- and a feed that re-sends its window both rewrite raw,
                   -- and without its marker the row is offered to Enrich,
                   -- and billed, a second time.
                   --
                   -- The same holds for the mark left on a row whose
                   -- Enrich request may have been billed with the answer
                   -- lost (sync/plaid): it holds the row out of Enrich for
                   -- a day, and a feed re-sending its window every hour
                   -- would otherwise wipe it and have the row paid for
                   -- again on the next run. Only the mark is carried — the
                   -- row was never enriched, so the rest of raw is still
                   -- the source's to restate.
                   raw=CASE WHEN jsonb_typeof(transactions.raw) = 'object'
                                 AND transactions.raw ? '_enriched_at'
                                 AND jsonb_typeof(EXCLUDED.raw) = 'object'
                                 AND NOT EXCLUDED.raw ? '_enriched_at'
                            THEN EXCLUDED.raw || COALESCE((
                                 SELECT jsonb_object_agg(e.key, e.value)
                                   FROM jsonb_each(transactions.raw) e
                                  WHERE e.key = ANY(%s)), '{}'::jsonb)
                            WHEN jsonb_typeof(transactions.raw) = 'object'
                                 AND transactions.raw ? '_enrich_unknown_at'
                                 AND jsonb_typeof(EXCLUDED.raw) = 'object'
                                 AND NOT EXCLUDED.raw ? '_enrich_unknown_at'
                            THEN EXCLUDED.raw || jsonb_build_object(
                                 '_enrich_unknown_at',
                                 transactions.raw->'_enrich_unknown_at')
                            ELSE EXCLUDED.raw END""",
            (t.id, t.account_id, t.date, t.amount, line, merchant, outlet,
             cat_primary, cat_detailed,
             t.category_plaid, t.category_plaid_detailed,
             t.category_plaid_confidence,
             1 if t.pending else 0,
             jsonb(mask_file_raw(t.raw, t.name, t.merchant_name)
                   if filed else t.raw),
             cat_source, en["check_number"], en["payment_channel"],
             en["payment_processor"], en["location_city"], en["location_region"],
             en["location_address"], en["location_postal"], en["location_lat"],
             en["location_lon"], en["location_store"], en["mcc"],
             en["authorized_date"], list(ENRICHED_RAW_KEYS)))
        n += cur.rowcount
        # category_override deliberately untouched — sacred, never synced
    return n


def _settle_pendings(conn, links: list[tuple[str, str]]) -> None:
    # pending→posted settlement: the posted transaction arrives as a NEW
    # row (new id) and the pending row it replaces is marked removed in
    # the same sync — so anything the user did to the pending row
    # (recategorize, owner, entity, a note) would silently die with it, one
    # to two days after they did it. Plaid names the predecessor
    # (pending_transaction_id), so carry the user's state across the swap
    # — never overwriting anything already set on the posted row — and
    # persist the link itself.
    #
    # SET-BASED, not once per settling row: a morning sync delivering a
    # day's card activity settles dozens of pendings at once, and a
    # per-row version would spend nine round trips on each of them inside the
    # path that delivers the money. Every statement below carries the
    # WHOLE page from one `unnest` pair, so the cost is a fixed handful of
    # statements per sync however many rows settle.
    #
    # The predecessor a row names is believed only where an aggregator could
    # have named it: a hold on the SAME account, and never from a file
    # import. `raw` is whatever the source sent, and for a file import or a
    # collector push that is the caller's own text, so without these limits
    # a push token or an uploaded column could retire any live hold and
    # take the receipts, splits and pairings of any row it can name.
    if links:
        own = {(r["new_id"], r["old_id"]) for r in conn.execute(
            """SELECT v.new_id, v.old_id
                 FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)
                 JOIN transactions t ON t.id = v.new_id
                 JOIN transactions p ON p.id = v.old_id
                WHERE p.account_id = t.account_id AND p.pending = 1
                  AND p.id <> t.id""",
            ([n for n, _ in links], [o for _, o in links])).fetchall()}
        links = [pair for pair in links if pair in own]
    if links:
        # The whole settlement — every carry and the retire — runs under
        # the one lock every reimbursement pairing write takes. A link
        # being made on the pending row meanwhile either commits first, and
        # its pair and flag are then here to be carried, or waits and finds
        # the row retired. Unlocked, the carry could look for pairs before
        # the link committed and the retire land after, leaving the pair on
        # a removed row no list shows. Each best-effort carry runs in its
        # own savepoint, so a failed one still cannot take the retire with
        # it.
        from ..web.data import _REIMB_LOCK
        with conn.transaction():
            conn.execute(_REIMB_LOCK)
            pairs = list(dict.fromkeys(links))     # a replayed page repeats pairs
            new_ids = [p[0] for p in pairs]
            old_ids = [p[1] for p in pairs]
            # The posted row's own id is the upsert key, so it appears once per
            # page: one target row per pair, and the COALESCE below reads the
            # pending twin exactly as the per-row UPDATE did.
            # The pin travels WITH the kind that wrote it: a person's category
            # landing on the posted row without its kind reads as one nobody
            # set, and the next store or bill pass is entitled to overwrite it.
            # A person's "use automatic" is an empty pin wearing their kind, so
            # the posted row having a kind is itself an answer to keep.
            took = "t.category_override IS NULL AND t.override_source IS NULL"
            conn.execute(
                f"""UPDATE transactions t SET
                       pending_transaction_id = v.old_id,
                       category_override = CASE WHEN {took}
                            THEN p.category_override ELSE t.category_override END,
                       override_source = CASE WHEN {took}
                            THEN p.override_source ELSE t.override_source END,
                       owner_override = COALESCE(t.owner_override,
                                                 p.owner_override),
                       entity_id = COALESCE(t.entity_id, p.entity_id)
                   FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)
                   JOIN transactions p ON p.id = v.old_id
                   WHERE t.id = v.new_id""",
                (new_ids, old_ids))
            conn.execute(
                """INSERT INTO transaction_notes (txn_id, note)
                   SELECT v.new_id, n.note
                     FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)
                     JOIN transaction_notes n ON n.txn_id = v.old_id
                   ON CONFLICT (tenant_id, txn_id) DO NOTHING""",
                (new_ids, old_ids))
            # The pending row's OTHER children follow the settlement too — a
            # receipt, reimbursement pairing, business classification, equity
            # link, or category pin made during the 1-2 day pending window
            # otherwise stays chained to a row every read hides once the
            # pending is retired. Each move is guarded so a re-delivered page
            # (which replays these statements) or a child the user already gave
            # the posted row can never collide; best-effort, because losing one
            # carry must not fail the sync that delivers the money itself —
            # and guarded PER STATEMENT, so a receipts failure still leaves the
            # reimbursements and classifications their move.
            #
            # These key on the PENDING id, so one pending row can hand its
            # children to at most one successor: keep the first pair claiming
            # each old id, which is the row the per-pair loop let win before a
            # later pair found nothing left to move.
            claimed: set = set()
            c_new: list = []
            c_old: list = []
            for new_id, old_id in pairs:
                if old_id in claimed:
                    continue
                claimed.add(old_id)
                c_new.append(new_id)
                c_old.append(old_id)

            def _carry(sql: str) -> None:
                try:
                    with conn.transaction():
                        conn.execute(sql, (c_new, c_old))
                except Exception:                            # noqa: BLE001
                    log.warning("pending→posted child carry failed for %s",
                                list(zip(c_old, c_new)), exc_info=True)

            _carry("UPDATE receipts r SET txn_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE r.txn_id=v.old_id")
            _carry("UPDATE reimbursements r SET expense_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE r.expense_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM reimbursements x WHERE x.expense_id=v.new_id"
                   "   AND x.reimburse_id=r.reimburse_id)")
            _carry("UPDATE reimbursements r SET reimburse_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE r.reimburse_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM reimbursements x WHERE x.reimburse_id=v.new_id"
                   "   AND x.expense_id=r.expense_id)")
            _carry("UPDATE reimburse_flags f SET txn_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE f.txn_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM reimburse_flags x WHERE x.txn_id=v.new_id)")
            _carry("UPDATE business_flags b SET txn_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE b.txn_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM business_flags x WHERE x.txn_id=v.new_id)")
            _carry("UPDATE business_txn_class b SET txn_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE b.txn_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM business_txn_class x WHERE x.txn_id=v.new_id)")
            # a contribution and a reimbursement are two answers to ONE
            # question (one fronted movement per transaction), so either on
            # the posted row already answers it; anything else is per kind
            _carry("UPDATE equity_movement e SET txn_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE e.txn_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM equity_movement k WHERE k.txn_id=v.new_id"
                   "   AND " + SAME_EQUITY_ANSWER + ")")
            # A fronted movement mirrors its expense in full, and the books
            # read the posted row: a hold capitalized at $50 that posts at
            # $46.81 would otherwise credit the capital account $3.19 more
            # than the P&L spent. One fronted on the posted row is booked at
            # the posted amount (equity._front_expense), so the carried one
            # is re-booked to match, with the change written into its note.
            # Only a movement still at the hold's amount: one somebody typed
            # by hand for part of the cost is theirs, not a mirror.
            _carry("UPDATE equity_movement e SET amount = round(t.amount::numeric, 2),"
                   " note = concat_ws('; ', NULLIF(e.note, ''),"
                   "   'held at ' || to_char(p.amount::numeric, 'FM999999999990.00')"
                   "   || ', posted at '"
                   "   || to_char(t.amount::numeric, 'FM999999999990.00'))"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " JOIN transactions t ON t.id = v.new_id"
                   " JOIN transactions p ON p.id = v.old_id"
                   " WHERE e.txn_id = v.new_id"
                   "   AND e.kind IN ('contribution', 'reimbursement')"
                   "   AND t.amount > 0"
                   "   AND abs(t.amount - p.amount) >= 0.005"
                   "   AND abs(e.amount - p.amount) < 0.005")
            _carry("UPDATE manual_categories m SET transaction_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE m.transaction_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM manual_categories x"
                   "   WHERE x.transaction_id=v.new_id)")
            # A hand split is several rows per charge and only means anything
            # whole, so it moves all together or not at all: never onto a
            # posted row that has parts of its own. A charge that posts for a
            # different amount takes the parts with it and the stale-split
            # sweep then drops them against the POSTED amount — left on the
            # retired row they would still add up and nothing would notice
            # they had stopped counting.
            _carry("UPDATE transaction_splits s SET txn_id=v.new_id"
                   " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE s.txn_id=v.old_id AND NOT EXISTS"
                   "  (SELECT 1 FROM transaction_splits x WHERE x.txn_id=v.new_id)")
            # A pair made on the pending row was weighed against the pending
            # amount: full or partial, and how much a partial link records. A
            # charge that posts with a tip, or a deposit that posts for less,
            # can leave a full pair repaying more than the deposit has or a
            # deposit read as a whole transfer that its links no longer use up,
            # so the pairs on a row whose amount moved are weighed again.
            try:
                with conn.transaction():
                    moved = [r["id"] for r in conn.execute(
                        """SELECT t.id
                             FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)
                             JOIN transactions t ON t.id = v.new_id
                             JOIN transactions p ON p.id = v.old_id
                            WHERE abs(t.amount - p.amount) >= 0.005
                              AND EXISTS (SELECT 1 FROM reimbursements r
                                           WHERE r.expense_id = t.id
                                              OR r.reimburse_id = t.id)""",
                        (c_new, c_old)).fetchall()]
                    if moved:
                        from ..web import data as _pairs
                        _pairs.recheck_pairs(conn, moved)
            except Exception:                                # noqa: BLE001
                log.warning("reimbursement recheck after settlement failed for %s",
                            list(zip(c_old, c_new)), exc_info=True)
            # And retire the predecessors: waiting for the aggregator's own
            # removal delta leaves a phantom pending double-counting spend
            # whenever that delta is missed. OUTSIDE the best-effort carries
            # above, on purpose — a child move that fails must not also keep
            # the pending row live next to its posted twin (the money would
            # then count twice until a removal delta that may never come).
            # Each carry is its own savepoint, so a failed one cannot poison
            # this.
            conn.execute(
                "UPDATE transactions SET removed=1"
                " WHERE id = ANY(%s) AND pending=1 AND removed=0", (old_ids,))
            # A person's "not this charge" on the hold is about the posted
            # row now. Only where the hold really was retired: a row that
            # stays live keeps its own memory. Before the matcher runs below,
            # which would otherwise pair the rejected receipt straight back.
            try:
                with conn.transaction():
                    gone = {r["id"] for r in conn.execute(
                        "SELECT id FROM transactions"
                        " WHERE id = ANY(%s) AND removed <> 0",
                        (c_old,)).fetchall()}
                    keep = [(o, n) for o, n in zip(c_old, c_new) if o in gone]
                    carry_unmatch_memory(conn, [o for o, _ in keep],
                                         [n for _, n in keep])
            except Exception:                                # noqa: BLE001
                log.warning("pending→posted unmatch carry failed for %s",
                            list(zip(c_old, c_new)), exc_info=True)


# On a file-import row a script token pushed: the overlap guard lets it
# stand in only for a feed row on an account linked to its own. A token is
# barred from importing into a feed account, and without this a manual-
# account row it planted (any amount, any name, any date) would set aside the
# matching real charge on whichever feed account it fell next to. A signed-in
# re-import of the same line rewrites raw and drops the mark: the household
# vouched for it then.
TOKEN_PUSHED_KEY = "_token_pushed"  # noqa: S105 — a raw-record key, not a credential


def _mark_token_pushed(txns: list[Transaction]) -> list[Transaction]:
    """File rows a script token pushed, marked; any dated past tomorrow
    dropped, so none can be planted ahead of a charge still to come."""
    from . import heartbeat
    if heartbeat.PUSH_VIA.get() != "token":
        # A file import stores its source row as raw, keyed by the file's own
        # headers, so a column could carry the mark itself: a signed-in import
        # would then steer the overlap guard. Only this function sets it.
        return [dataclasses.replace(
                    t, raw={k: v for k, v in t.raw.items()
                            if k != TOKEN_PUSHED_KEY})
                if isinstance(t.raw, dict) and TOKEN_PUSHED_KEY in t.raw
                and isinstance(t.id, str)
                and t.id.startswith(IMPORT_ID_PREFIXES) else t
                for t in txns]
    latest = dt.date.today() + dt.timedelta(days=1)
    out = []
    for t in txns:
        if not (isinstance(t.id, str) and t.id.startswith(IMPORT_ID_PREFIXES)):
            out.append(t)
            continue
        if isinstance(t.date, dt.date) and t.date > latest:
            log.warning("dropping token-pushed row %s: dated %s, after "
                        "tomorrow", t.id, t.date)
            continue
        out.append(dataclasses.replace(
            t, raw={**(t.raw if isinstance(t.raw, dict) else {}),
                    TOKEN_PUSHED_KEY: True}))
    return out


def upsert_transactions(conn, txns: list[Transaction]) -> int:
    # every aggregator (plaid per page, mx and simplefin whole-pull) and
    # every importer funnels through here — the one place a generous
    # ceiling covers a hostile provider payload (a tenant-supplied
    # SimpleFIN bridge can stream any list it likes) without any door
    # having to remember its own cap
    rowcap.check_sync_len(txns)
    # Postgres text cannot hold U+0000 and one would fail the whole batch;
    # it carries no meaning in a name, a category or the raw record the
    # enrichment columns are read from (a UTF-16 file decoded as UTF-8 has
    # one after every letter). Every importer funnels here, so it goes here.
    txns = [_without_nul(t) for t in txns]
    # the account a row names is spelled the way upsert_accounts stored it
    # (a comma id renamed), or the row would point at no account at all
    if any(t.account_id and "," in t.account_id for t in txns):
        from .adopt import stored_account_id
        renamed: dict[str, str] = {}
        txns = [dataclasses.replace(
                    t, account_id=stored_account_id(conn, t.account_id,
                                                    renamed))
                if t.account_id and "," in t.account_id else t
                for t in txns]
    txns = _mark_token_pushed(txns)
    hidden = hidden_account_ids(conn)
    cards = credit_account_ids(conn)
    links = [(t.id, t.raw.get("pending_transaction_id")) for t in txns
             if t.account_id not in hidden and isinstance(t.raw, dict)
             and isinstance(t.raw.get("pending_transaction_id"), str)
             and t.raw.get("pending_transaction_id")
             and not is_file_import_id(t.id)]
    if links:
        # A settling page lands as ONE commit: its posted rows and the
        # retirement of the holds they replace. Written row by row on this
        # autocommit connection, each posted row would be visible on its own
        # while its pending twin was still live, and a Today load, a widget
        # poll or the hourly email read in that window would count the charge
        # twice. Lock order is the one every pairing writer keeps: the pairing
        # lock first, then the accounts the rows point at (the foreign-key
        # check would otherwise take them mid-page, after transaction rows,
        # the reverse of the classify doors), then the rows themselves.
        from ..web.data import _REIMB_LOCK
        with conn.transaction():
            conn.execute(_REIMB_LOCK)
            conn.execute(
                "SELECT 1 FROM accounts WHERE id = ANY(%s)"
                " ORDER BY id FOR KEY SHARE",
                (sorted({t.account_id for t in txns
                         if t.account_id and t.account_id not in hidden}),))
            n = _write_rows(conn, txns, hidden, cards)
            _settle_pendings(conn, links)
    else:
        n = _write_rows(conn, txns, hidden, cards)

    # every new row gets its merchant ROW right here (Plaid identity,
    # counterparties, logo; else the string clean) — the ledger shows the
    # logo on the next load, not after the nightly pass. Best-effort: a
    # resolver failure must never fail the sync (the nightly resolve is
    # the backstop).
    if txns:
        try:
            from ..engine import merchant_identity
            merchant_identity.resolve(conn, txn_ids=[t.id for t in txns],
                                      only_unresolved=False)
        except Exception:                                # noqa: BLE001
            log.warning("merchant resolve skipped", exc_info=True)
        # a merchant this sync minted that looks like one the ledger already
        # has is offered the same hour, not after the nightly pass
        try:
            from ..engine import merchant_merge
            fresh = [r["m"] for r in conn.execute(
                "SELECT DISTINCT merchant_id::text AS m FROM transactions "
                "WHERE id = ANY(%s) AND merchant_id IS NOT NULL",
                ([t.id for t in txns],)).fetchall()]
            if fresh:
                merchant_merge.run(conn, only_ids=fresh, limit=10)
        except Exception:                                # noqa: BLE001
            log.warning("merchant merge proposals skipped", exc_info=True)
    # a receipt snapped before its charge meets the charge here — the one
    # place every importer and aggregator passes — after the settlement
    # above has carried any receipt already on a pending row to its posted
    # twin, and after the resolver has named the new rows' merchants.
    # Best-effort in its own savepoint: a matcher failure must never fail
    # the sync that delivers the money, and the next sync or parse tries
    # again. One indexed probe when nothing is waiting.
    if txns:
        try:
            from ..engine import receipt_match
            with conn.transaction():
                receipt_match.match_waiting(conn)
        except Exception:                                # noqa: BLE001
            log.warning("waiting-receipt match skipped", exc_info=True)

    # stamp a push-heartbeat per script-fed item. Aggregator
    # items (simplefin/plaid) are excluded — the worker's bank-sync
    # doctor check owns their freshness.
    if txns:
        from . import heartbeat
        items = {r["aid"]: (r["id"], r["institution_name"]) for r in conn.execute(
            """SELECT a.id AS aid, i.id, i.institution_name
               FROM accounts a JOIN items i ON i.id = a.item_id
               WHERE a.id = ANY(%s)
                 AND i.aggregator NOT IN ('simplefin','simplefin-org','plaid','mx')""",
            (list({t.account_id for t in txns}),)).fetchall()}
        counts: dict[tuple, int] = {}
        for t in txns:
            it = items.get(t.account_id)
            if it:
                counts[it] = counts.get(it, 0) + 1
        for (item_id, name), c in counts.items():
            heartbeat.stamp(conn, item_id, rows=c, label=name)
    return n


# ---- split accounts (multi-account file imports: Mint, OFX, …) -----


def _split_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "x"


def infer_account_type(name: str | None) -> tuple[str, str | None]:
    """Conservative account type from a source-side account NAME.

    Creating every split account as type=depository breaks spend math on
    the cards among them — the credit sign and exclusion rules never
    apply. Checking/savings words win over credit/card so "First Credit
    Union Checking" stays depository; the default is depository/checking
    with a subtype. The user's own classification always beats this (the
    insert is ON CONFLICT DO NOTHING and type_user_set pins it)."""
    s = (name or "").lower()
    if re.search(r"\bcheck(?:ing)?\b", s):
        return "depository", "checking"
    if re.search(r"\bsavings?\b", s):
        return "depository", "savings"
    if re.search(r"\bcredit\b(?!\s+union)|\bcard\b", s):
        return "credit", "credit card"
    return "depository", "checking"


def ensure_split_account(conn, prefix: str, name: str, *,
                         key: str | None = None,
                         type_: str | None = None,
                         subtype: str | None = None) -> str:
    """A manual account for one source-side account of a multi-account
    file (the competitor exports, Mint, OFX). Stable id from `key`
    (default: the source's account name) → re-imports land on the same
    account; existing rows — including a user rename or reclassification —
    are never overwritten. Type defaults to the name-based inference
    unless the caller knows better (OFX statements do)."""
    if type_ is None:
        type_, subtype = infer_account_type(name)
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, access_token)
           VALUES ('manual','manual','Manual',NULL)
           ON CONFLICT (tenant_id, id) DO NOTHING""")
    aid = f"manual:{prefix}-{_split_slug(key or name)}"
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, updated_at)
           VALUES (%s,'manual',%s,%s,%s,now())
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (aid, name.strip(), type_, subtype))
    return aid


# Two equity movements on one transaction that answer the same question:
# the same kind, or both fronted kinds (a personally-paid business cost is
# a contribution OR reimbursable, never both — the database holds one per
# transaction). Every carry of equity_movement.txn_id tests this against
# the destination row, so a move the index would refuse is left behind
# instead of aborting the carry. `e` is the moving row, `k` the one there.
SAME_EQUITY_ANSWER = (
    "(k.kind = e.kind OR (e.kind IN ('contribution', 'reimbursement')"
    " AND k.kind IN ('contribution', 'reimbursement')))")


# ---- reverse cross-source dedup (aggregator vs. earlier file imports) ------
#
# The file importers dedup against aggregator rows on the SAME account
# (csvimport and its siblings: account, ±3 days, exact amount). The
# reverse case —
# user imports bank files FIRST (rows land on manual:<slug> accounts), THEN
# connects SimpleFIN/Plaid — could never match that way: the aggregator's
# account ids differ, so every overlapping charge would double-count and
# the hourly worker would keep recreating the overlap. Aggregator ingest therefore
# checks tenant-WIDE for a file-import row covering the incoming charge,
# but only inside the date window the imports actually cover — genuinely
# new activity after the import coverage always inserts. Long-term account
# linking is out of scope; this window-scoped guard covers the overlap.

IMPORT_ID_PREFIXES = ("csv:", "ofx:", "qif:", "mint:", "ynab:",
                      "monarch:", "copilot:", "simplifi:", "pdf:")
IMPORT_DEDUP_WINDOW_DAYS = 3


# token/conflict rules shared with the import-side matcher (sync.dedup):
# substantive tokens only (stopwords/digits filtered), and a same-day
# no-overlap match is refused when both sides carry conflicting merchant
# tokens (two different $4.50 purchases on one day are not dupes)
from .dedup import _distinct_names as _name_conflict  # noqa: E402
from .dedup import _tokens as _name_tokens  # noqa: E402


class ImportOverlapGuard:
    """Per-sync-run index of the tenant's file-import rows. A match is an
    import row with the same amount within ±3 days AND name-token overlap,
    or same-day exact amount when a side carries no substantive name
    tokens (opaque bank descriptors — "POS DEBIT 4417" — is exactly the
    migration case this guard exists for; the name-conflict rule stops
    same-day matching from eating rows whose names conflict outright).
    Matches are CONSUMED — one
    import row suppresses at most one aggregator row per sync run, so two
    genuine same-amount charges survive.

    Only per run. A later sync offers every file row again, which is what
    lets a feed that re-sends its window, or settles a pending row under a
    new id, keep finding the file row that already represents the charge.
    The price is that a genuine same-amount charge at the same shop inside
    the import window, arriving on a later sync, is also taken as covered."""

    def __init__(self, conn):
        # NB: matching is deliberately
        # NOT scoped to the incoming account. Before linking, a
        # CSV-imported account and the aggregator account for the SAME real
        # account carry DIFFERENT ids, and opaque bank-CSV names ("POS DEBIT
        # 4417") force amount+date matching — so cross-account matching IS the
        # migration linkage (test_simplefin ImportOverlapTests encode this).
        # Once accounts ARE linked, shadow exclusion also prevents any
        # double-count at query time. Scoping here would regress that path;
        # the residual cross-institution coincidence is one-to-one, window-
        # bounded, and surfaced as skipped_import_duplicates.
        #
        # Loaded LAZILY, and only for the dates the incoming rows can match
        # (see `load_for`): a household with a decade of imported history
        # would otherwise have every one of those rows read and tokenised on
        # every pull of every feed item — hourly, for each — to match a few
        # days of new charges. Nothing is read until a page brings rows.
        self._conn = conn
        self._by_cents: dict[int, list[dict]] = defaultdict(list)
        # the one contiguous date span already read; a later page only
        # reads what lies outside it, so a row consumed on an earlier page
        # is never offered again by a re-read
        self._lo: dt.date | None = None
        self._hi: dt.date | None = None
        # each incoming account's link group, read once per run
        self._groups: dict = {}

    def load_for(self, txns) -> None:
        """Make sure every file row that could match one of `txns` (the
        same amount within ±IMPORT_DEDUP_WINDOW_DAYS of its date) is
        indexed. Called before matching a page; `match` calls it for a
        single row too, so a caller that never does still gets the right
        answer."""
        dates = [t.date for t in txns if getattr(t, "date", None) is not None]
        if not dates:
            return
        slack = dt.timedelta(days=IMPORT_DEDUP_WINDOW_DAYS)
        lo, hi = min(dates) - slack, max(dates) + slack
        if self._lo is None:
            self._read(lo, hi)
            self._lo, self._hi = lo, hi
            return
        if lo < self._lo:
            self._read(lo, self._lo - dt.timedelta(days=1))
            self._lo = lo
        if hi > self._hi:
            self._read(self._hi + dt.timedelta(days=1), hi)
            self._hi = hi

    def _read(self, lo: dt.date, hi: dt.date) -> None:
        clauses = " OR ".join("t.id LIKE %s" for _ in IMPORT_ID_PREFIXES)
        rows = self._conn.execute(
            "SELECT t.id, t.date, t.amount, t.name, t.merchant_name,"
            # read type-safely: a row restored or mirrored from elsewhere
            # may still carry a text value under the key, and a cast error
            # here would fail every feed sync whose window covers it
            "       COALESCE(jsonb_typeof(t.raw) = 'object'"
            "                AND t.raw @> jsonb_build_object(%s::text, true),"
            "                false) AS pushed,"
            "       l.group_id"
            "  FROM transactions t"
            "  LEFT JOIN account_links l ON l.account_id = t.account_id"
            " WHERE t.removed = 0 AND t.date BETWEEN %s AND %s "
            f"  AND ({clauses})",
            (TOKEN_PUSHED_KEY, lo, hi,
             *(p + "%" for p in IMPORT_ID_PREFIXES))).fetchall()
        for r in rows:
            if r["date"] is None or r["amount"] is None:
                continue
            # a token-pushed row with no link group can stand in for nothing
            if r["pushed"] and r["group_id"] is None:
                continue
            self._by_cents[round(r["amount"] * 100)].append(
                {"id": r["id"], "date": r["date"],
                 "group": r["group_id"] if r["pushed"] else None,
                 "tokens": _name_tokens(r["name"], r["merchant_name"])})

    def _group_of(self, account_id):
        cache = self._groups
        if account_id not in cache:
            r = self._conn.execute(
                "SELECT group_id FROM account_links WHERE account_id = %s",
                (account_id,)).fetchone()
            cache[account_id] = r["group_id"] if r else None
        return cache[account_id]

    @property
    def empty(self) -> bool:
        """No file row inside what has been read so far."""
        return not any(self._by_cents.values())

    def covers(self, t: Transaction) -> bool:
        """True (and the matched import row is consumed) when a file-import
        row already represents this incoming aggregator transaction."""
        return self.match(t) is not None

    def match(self, t: Transaction) -> str | None:
        """The id of the file-import row that represents this incoming
        aggregator transaction (consumed for this run), or None."""
        if t.date is None or t.amount is None:
            return None
        self.load_for([t])
        if self.empty:
            return None
        # an amount that is not a real sum matches nothing here; the
        # upsert after this refuses the row with a logged reason
        try:
            cents = round(float(t.amount) * 100)
        except (TypeError, ValueError, OverflowError):
            return None
        cands = self._by_cents.get(cents)
        if not cands:
            return None
        toks = _name_tokens(t.name, t.merchant_name)
        best = None                      # (rank, candidate); lower rank wins
        for c in cands:
            gap = abs((c["date"] - t.date).days)
            if gap > IMPORT_DEDUP_WINDOW_DAYS:
                continue
            # see TOKEN_PUSHED_KEY: only beside an account linked to it
            if c.get("group") is not None and (
                    self._group_of(t.account_id) != c["group"]):
                continue
            if toks & c["tokens"]:
                rank = (0, gap)          # name overlap: best, nearest date
            elif gap == 0 and not _name_conflict(toks, c["tokens"]):
                rank = (1, 0)            # same-day exact amount, no evidence
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, c)
        if best is None:
            return None
        cands.remove(best[1])            # one-to-one: consume the import row
        return best[1]["id"]


# On a file-import row: the note that a bank feed set one of its own rows
# aside because this row already represents it, as
# {"source": aggregator, "accounts": [feed account ids]}. The feed will not
# send that row again, so this row is the ledger's only copy of the charge:
# undoing the import, or deleting the account the row is on, keeps it (see
# relied_on_by_feed). Only the fact is stored, never the feed's row.
STOOD_IN_KEY = "_stood_in"


# An earlier format kept a full copy of each feed row on the file row,
# under this key. Migration 161 turned every stored one into the note
# above; an archive written then still carries them.
_LEGACY_COPIES_KEY = "_covers"


def stood_in_from_legacy_copies(raw: dict) -> dict:
    """Replace, in place, the earlier format's stored feed-row copies with
    the STOOD_IN_KEY note they imply, so a restored archive keeps what the
    copies protected without carrying the feed's rows. Keep in step with
    migration 161."""
    copies = raw.pop(_LEGACY_COPIES_KEY, None)
    if not isinstance(copies, dict) or not copies:
        return raw
    accts = sorted({c["account_id"] for c in copies.values()
                    if isinstance(c, dict)
                    and isinstance(c.get("account_id"), str)
                    and c["account_id"]})
    note = raw.get(STOOD_IN_KEY)
    if isinstance(note, dict) and isinstance(note.get("accounts"), list):
        accts = sorted(set(accts) | {a for a in note["accounts"]
                                     if isinstance(a, str) and a})
    raw[STOOD_IN_KEY] = {"source": (note.get("source")
                                    if isinstance(note, dict)
                                    and isinstance(note.get("source"), str)
                                    else "feed"),
                         "accounts": accts}
    return raw


def mark_stood_in(conn, by_file_row: dict[str, list[str]],
                  source: str) -> int:
    """Note on each file row that a feed row on these accounts was set
    aside against it. Idempotent: a row already naming every account is
    not rewritten, so a feed that re-sends its window each pull does not
    churn the ledger. Returns how many rows were changed."""
    n = 0
    for fid, accounts in by_file_row.items():
        accts = sorted({a for a in accounts if a})
        n += conn.execute(
            """UPDATE transactions
                  SET raw = COALESCE(raw, '{}'::jsonb) || jsonb_build_object(
                        %(k)s::text, jsonb_build_object(
                          'source', %(src)s::text,
                          'accounts', (
                            SELECT COALESCE(jsonb_agg(DISTINCT a ORDER BY a),
                                            '[]'::jsonb)
                              FROM (SELECT jsonb_array_elements_text(
                                             CASE WHEN jsonb_typeof(
                                                    raw->%(k)s->'accounts')
                                                    = 'array'
                                                  THEN raw->%(k)s->'accounts'
                                                  ELSE '[]'::jsonb END) AS a
                                    UNION SELECT unnest(%(accts)s::text[])) u)))
                WHERE id = %(id)s
                  AND (raw IS NULL OR jsonb_typeof(raw) = 'object')
                  AND NOT COALESCE(
                        jsonb_typeof(raw->%(k)s->'accounts') = 'array'
                        AND raw->%(k)s->>'source' = %(src)s::text
                        AND raw->%(k)s->'accounts' @> to_jsonb(
                              %(accts)s::text[]), false)""",
            {"k": STOOD_IN_KEY, "src": source or "feed", "accts": accts,
             "id": fid}).rowcount
    return n


def carry_stood_in_accounts(conn, old_id: str, new_id: str) -> int:
    """Rename one account id inside every STOOD_IN_KEY note that names it,
    so a note keeps naming its feed account after the account is moved to a
    new id. Without this the note rests on the adoption record alone, and
    that record does not travel in an archive: restored elsewhere, the note
    would name an account that never existed there and protect nothing.
    Returns how many rows changed."""
    return conn.execute(
        """UPDATE transactions
              SET raw = jsonb_set(raw, ARRAY[%(k)s, 'accounts'], (
                    SELECT COALESCE(jsonb_agg(DISTINCT
                             CASE WHEN a = %(old)s THEN %(new)s ELSE a END),
                           '[]'::jsonb)
                      FROM jsonb_array_elements_text(
                             CASE WHEN jsonb_typeof(raw->%(k)s->'accounts')
                                       = 'array'
                                  THEN raw->%(k)s->'accounts'
                                  ELSE '[]'::jsonb END) AS e(a)))
            WHERE jsonb_typeof(raw->%(k)s->'accounts') = 'array'
              AND raw->%(k)s->'accounts' ? %(old)s""",
        {"k": STOOD_IN_KEY, "old": old_id, "new": new_id}).rowcount


def stood_in_for_restore(raw: dict, account_id, known: set[str]) -> dict:
    """A restored row's STOOD_IN_KEY note, in place, naming accounts as the
    destination holds them: each id through `account_id` (the restore's own
    renames), and an id the destination has no account for dropped. A note
    left naming none becomes the note that names no account, which is kept
    while any bank feed remains: `known` holds the destination's accounts
    and the older ids the lineage ALREADY on the destination ties to them
    (the archive's own lineage is loaded after the ledger, and an archive
    written before the lineage travelled carries none), and keeping a row
    too many is recoverable where losing one is not."""
    note = raw.get(STOOD_IN_KEY)
    if not isinstance(note, dict) or not isinstance(note.get("accounts"),
                                                    list):
        return raw
    named = [a for a in note["accounts"] if isinstance(a, str) and a]
    accts = sorted({account_id(a) for a in named} & known)
    raw[STOOD_IN_KEY] = {**note, "accounts": accts}
    return raw


# The connections whose pulls set a charge aside against a file row
# (filter_import_duplicates runs inside each of their syncs).
FEED_AGGREGATORS = ("plaid", "mx", "simplefin", "simplefin-org")


def _any_feed_account(conn, dropping: tuple[str, ...] = ()) -> bool:
    """Could any bank feed still rely on a note: a feed connection that
    syncs, or any bank-feed account other than the ones going in this same
    delete? An archived connection's accounts count: they keep their
    history, and a re-link adopts them and skips what they are taken to
    hold already, so a charge the feed set aside is still missing from it."""
    feeds = list(FEED_AGGREGATORS)
    return conn.execute(
        "SELECT 1 FROM items WHERE aggregator = ANY(%s)"
        " AND COALESCE(status, 'ok') != 'archived'"
        " UNION ALL "
        "SELECT 1 FROM accounts a JOIN items i ON i.id = a.item_id"
        " WHERE i.aggregator = ANY(%s) AND NOT (a.id = ANY(%s)) LIMIT 1",
        (feeds, feeds, list(dropping))).fetchone() is not None


def relied_on_by_feed(conn, ids: list[str],
                      dropping_accounts: tuple[str, ...] = (),
                      release_accountless: bool = False) -> set[str]:
    """Which of these rows a bank feed still relies on: rows carrying the
    STOOD_IN_KEY note for a feed account that still exists, or that lives
    on under another id (a re-link or a restore renamed it). A writer about
    to hard-delete rows keeps these: the feed set its own copy aside and
    will not send it again. A note naming only accounts that are going in
    this same delete (`dropping_accounts`), or that are gone with nothing
    carrying them on, protects nothing — that feed's rows are gone too.

    A note that names no account (an archive from elsewhere, or one read
    back from the activity log) cannot be checked against any feed, so it
    is kept while the household has any bank-feed account at all, archived
    connections included, and released when it has none: then nothing can
    be relying on it. Otherwise only an owner told how many such rows a
    delete would keep releases them (`release_accountless`)."""
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return set()
    rows = conn.execute(
        """SELECT id, raw->%s->'accounts' AS accts FROM transactions
            WHERE id = ANY(%s) AND jsonb_typeof(raw->%s) = 'object'""",
        (STOOD_IN_KEY, ids, STOOD_IN_KEY)).fetchall()
    if not rows:
        return set()
    named = {a for r in rows if isinstance(r["accts"], list)
             for a in r["accts"] if isinstance(a, str) and a}
    alive = _feed_accounts_alive(conn, named - set(dropping_accounts))
    keep_accountless = None
    out = set()
    for r in rows:
        accts = [a for a in (r["accts"] if isinstance(r["accts"], list)
                             else []) if isinstance(a, str) and a]
        if accts:
            if any(a in alive for a in accts):
                out.add(r["id"])
            continue
        # a note with no readable account is kept while any feed could
        # rely on it: keeping a row too many is recoverable, losing one
        # is not
        if keep_accountless is None:
            keep_accountless = (not release_accountless
                                and _any_feed_account(
                                    conn, tuple(dropping_accounts)))
        if keep_accountless:
            out.add(r["id"])
    return out


def kept_without_account(conn, account_ids: list[str],
                         dropping: tuple[str, ...]) -> tuple[int, int]:
    """For a delete of these accounts: how many of their rows it would
    keep for a feed, and how many of those are kept only by a note that
    names no account — the rows an owner may choose to delete anyway."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE account_id = ANY(%s)",
        (list(account_ids),)).fetchall()]
    keep = relied_on_by_feed(conn, ids, dropping)
    if not keep:
        return 0, 0
    named = relied_on_by_feed(conn, list(keep), dropping,
                              release_accountless=True)
    return len(keep), len(keep - named)


def _feed_accounts_alive(conn, account_ids: set[str]) -> set[str]:
    """The ids, of these, whose account is still here in some form: by the
    same id, adopted into a re-linked one, or restored under the comma-free
    name an archive's id is stored as. One look each, no chain to walk —
    the question is only whether the feed still exists."""
    if not account_ids:
        return set()
    ids = list(account_ids)
    have = {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id = ANY(%s)", (ids,)).fetchall()}
    have |= {r["old_account_id"] for r in conn.execute(
        "SELECT DISTINCT old_account_id FROM account_adoptions"
        " WHERE old_account_id = ANY(%s)", (ids,)).fetchall()}
    rest = [a for a in ids if a not in have and "," in a]
    if rest:
        from .adopt import account_id_without_comma
        alias = {account_id_without_comma(a): a for a in rest}
        have |= {alias[r["id"]] for r in conn.execute(
            "SELECT id FROM accounts WHERE id = ANY(%s)",
            (list(alias),)).fetchall()}
    return have


def kept_rows_note(n: int) -> str:
    """What an undo or a delete says about the rows it kept."""
    return (f"{n} row{'s were' if n != 1 else ' was'} kept: the bank feed "
            "already relies on "
            f"{'them' if n != 1 else 'it'} (the feed set its own copy aside "
            f"because {'these rows' if n != 1 else 'this row'} already "
            "represented it, and will not send it again).")


def _feed_sources(conn, account_ids: set[str]) -> dict[str, str]:
    """account id -> aggregator of the connection it hangs off."""
    if not account_ids:
        return {}
    return {r["id"]: r["aggregator"] for r in conn.execute(
        """SELECT a.id, i.aggregator FROM accounts a
             JOIN items i ON i.id = a.item_id
            WHERE a.id = ANY(%s)""", (list(account_ids),)).fetchall()}


def filter_import_duplicates(conn, txns: list[Transaction],
                             guard: "ImportOverlapGuard | None" = None
                             ) -> tuple[list[Transaction], int]:
    """Drop incoming aggregator transactions already covered by file-import
    rows (see ImportOverlapGuard). Rows whose aggregator id ALREADY exists
    are always kept — those are updates (e.g. pending→posted), not new
    duplicates. Returns (kept, skipped_count).

    `guard`: a caller that applies a pull PAGE BY PAGE (plaid.sync) passes
    ONE guard for the whole run, so the one-import-row-covers-one-
    aggregator-row consumption spans the pages — a fresh guard per page
    would re-offer every consumed import row to the next page, and two
    genuine same-amount charges split across a page boundary would both
    be dropped."""
    if not txns:
        return txns, 0
    guard = guard or ImportOverlapGuard(conn)
    guard.load_for(txns)
    if guard.empty:
        return txns, 0
    existing = {r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE id = ANY(%s)",
        ([t.id for t in txns],)).fetchall()}
    kept, dropped = [], []
    by_file_row: dict[str, list[str]] = defaultdict(list)
    for t in txns:
        stand_in = guard.match(t) if t.id not in existing else None
        if stand_in is not None:
            dropped.append(t)
            by_file_row[stand_in].append(t.account_id)
        else:
            kept.append(t)
    if dropped:
        # The feed's cursor moves past a suppressed row, so the file row
        # standing in for it becomes the only copy of the charge; the note
        # tells an undo of that import to keep it.
        sources = _feed_sources(conn, {t.account_id for t in dropped})
        by_source: dict[str, dict[str, list[str]]] = defaultdict(dict)
        for fid, accts in by_file_row.items():
            src = next((sources[a] for a in accts if a in sources), "feed")
            by_source[src][fid] = accts
        for src, rows in by_source.items():
            mark_stood_in(conn, rows, src)
        # The match is a heuristic (amount + ±3 days + name evidence), and
        # a false positive here LOSES a real transaction: the aggregator's
        # cursor advances past it, so it is never offered again. An
        # aggregate count alone made such a drop undiagnosable — record
        # each suppressed row's identity in sync_log (admin-visible
        # activity) so a wrong drop can be found and the row re-entered.
        # ONLY the opaque aggregator id + date: sync_log renders on the
        # per-tenant admin console, whose doctrine is no tenant DATA. The
        # id lets the tenant re-find the row in their OWN ledger (where the
        # amount and merchant live); the amount and name themselves are
        # third-party PII and must not cross to an operator.
        detail = "; ".join(f"{t.id} {t.date}" for t in dropped[:25])
        if len(dropped) > 25:
            detail += f"; +{len(dropped) - 25} more"
        log_sync(conn, "import-overlap", 0,
                 error="suppressed as covered by file-imported rows: "
                       + detail)
    return kept, len(dropped)


# Aggregators whose accounts are pulled by
# the worker — a script token must never file-import into them (rows a
# pull source owns get polluted with csv: rows the source can neither
# reconcile nor restate).
LIVE_PULL_AGGREGATORS = ("plaid", "simplefin", "simplefin-org", "mx")

# same doctrine for accounts OWNED by a push script — the plan-CSV door
# and coinbase (tokened OR push-fed) restate their own rows on every push;
# foreign csv: rows landing there sit outside the source's prune scope and
# pollute the account forever. Script tokens target manual/import-fed
# accounts only.
PUSH_FED_AGGREGATORS = ("coinbase", "plan_csv")


def assert_token_importable(conn, account_id: str) -> None:
    """Refuse (PermissionError) a script-token file import into an account
    owned by a live pull aggregator or a push script. Session imports are
    not routed here — the owner may import anywhere. Unknown accounts
    pass: the importers' own account handling owns that error."""
    row = conn.execute(
        """SELECT i.aggregator, i.access_token FROM accounts a
           JOIN items i ON i.id = a.item_id WHERE a.id=%s""",
        (account_id,)).fetchone()
    if row is None:
        return
    agg = row["aggregator"] or ""
    if agg in LIVE_PULL_AGGREGATORS or agg in PUSH_FED_AGGREGATORS:
        raise PermissionError(
            f"script tokens cannot import files into an account synced by "
            f"{agg} — target a manual or import-fed account")


@contextlib.contextmanager
def categorize_lock(conn, tenant_id):
    """The sync-time categorize pass, single-flight per tenant.

    Three doors run `categorize_new` + `apply_txn_categories` after a
    pull — the hourly sweep, the webhook item sync and the ↻ button's
    background thread — and only the first two hold the tenant SYNC lock
    while they do it. `categorize_new` guards its LLM and Amazon halves
    itself, but its ledger-wide `apply()` and the bill-category apply are
    unguarded, so two passes over one ledger at once are wasted work and
    a deadlock candidate. Yields True when this caller holds the pass;
    False means skip — every step is pending-gated, so the holder or the
    next sweep picks up whatever this pass would have done."""
    key = f"oikonome:sync-categorize:{tenant_id}"
    got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                       (key,)).fetchone()["ok"]
    try:
        yield got
    finally:
        if got:
            tenancy.release_lock(conn, key)


@contextlib.contextmanager
def tenant_sync_lock(conn, tenant_id):
    """The per-tenant sync lock every scheduled door takes, for the ad-hoc
    ones (a new bank link's first sync, connect-and-pull, sync-now).
    Yields True when held; False means another sync/restore for this
    tenant is in flight and the caller should answer "already running"
    rather than race it — two pulls on one cursor discard each other's
    work, and a first sync racing a background restore interleaves with
    its uncommitted insert-only transaction."""
    key = f"oikonome:sync:{tenant_id}"
    got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                       (key,)).fetchone()["ok"]
    try:
        yield got
    finally:
        # a failed unlock drops the pooled connection rather than
        # handing the next borrower a lock nobody meant to hold
        if (got and not tenancy.release_lock(conn, key)
                and not _session_holds_lock(conn, key)):
            # A full sync that found the lock held left a nudge and went
            # away, trusting the holder to answer it. The holders of this
            # helper — disconnects, reconnects, one-bank pulls, a new
            # link's first pull, the SimpleFIN connect, and the file and
            # bulk imports — never make that pass themselves, so the sync
            # is queued now that the lock is free.
            #
            # Only once it IS free. Advisory locks nest within a session: a
            # bulk import holds the lock and each file's import takes it
            # again, and that inner unlock only lowers the count. Answering
            # there would queue a sync that finds the lock still held,
            # nudges again and bounces — once per file — so the answer
            # waits for the release that lets the lock go.
            from ..jobs import worker
            worker.answer_sync_nudge(conn, tenant_id)


class SyncRunning(RuntimeError):
    """A household write that must not overlap a sync found the tenant
    sync lock held. Nothing was changed; the caller answers "try again"."""

    def __init__(self, message: str = "a sync is running for this "
                 "household — try again in a moment"):
        super().__init__(message)


@contextlib.contextmanager
def household_delete_lock(conn):
    """The tenant sync lock for a hard delete of ledger rows, on a
    tenant-scoped connection; raises SyncRunning when it is held.

    A sync reads the household's file-import rows once, at its start, and
    filters the whole pull against that view. A delete of those rows that
    lands in between leaves the sync dropping feed rows as covered by rows
    that no longer exist, and the feed never sends them again. The lock
    nests, so a caller already holding it passes straight through."""
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    with tenant_sync_lock(conn, tid) as held:
        if not held:
            raise SyncRunning()
        yield


def _session_holds_lock(conn, key: str) -> bool:
    """True while this session still holds the advisory lock `key` — an
    outer acquisition of a nested one. pg_locks shows a held advisory lock
    once whatever its count, its bigint key split into classid (high half)
    and objid (low half)."""
    return conn.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_locks l,
                      (SELECT hashtext(%s)::bigint AS h) k
                WHERE l.locktype = 'advisory' AND l.granted
                  AND l.pid = pg_backend_pid() AND l.objsubid = 1
                  AND l.classid = ((k.h >> 32) & 4294967295)::oid
                  AND l.objid = (k.h & 4294967295)::oid) AS held""",
        (key,)).fetchone()["held"]


def mark_removed(conn, txn_ids: list[str]) -> int:
    """Flag rows a removal delta names. Returns how many actually landed.

    A miss is not noise: after a reconnect the aggregator can know a held
    charge under a NEW id (id churn), so a later removal naming that id
    matches nothing here while the charge stays on the books under its old
    id. Logging the missed ids is the only trace such a phantom leaves."""
    from ..engine.budget import FEED_REMOVED_KEY
    missed = []
    # under the pairing lock, like the pending→posted settlement: a link
    # being made on one of these rows either commits first, and its pair is
    # then here for the re-anchor below to carry, or waits and finds the
    # row retired — never lands on it after the re-anchor has looked
    from ..web.data import _REIMB_LOCK
    with conn.transaction():
        conn.execute(_REIMB_LOCK)
        for tid in txn_ids:
            # Stamped as the feed's own withdrawal. Merges, file restates
            # and full-replace pushes retire rows with removed=1 as well,
            # and only this one means "the bank took the charge back" — the
            # bills pages read the stamp to say so. A feed that re-reports
            # the row rewrites `raw`, which drops the stamp with the flag.
            # Only a row that was live: one a merge, a restate or a person
            # already retired was not withdrawn by this delta, and a row
            # the feed already withdrew keeps the stamp it has.
            cur = conn.execute(
                "UPDATE transactions SET removed=1,"
                " raw = CASE WHEN removed = 0 AND (raw IS NULL"
                "                OR jsonb_typeof(raw) = 'object')"
                "  THEN COALESCE(raw, '{}'::jsonb)"
                "       || jsonb_build_object(%s::text, now())"
                "  ELSE raw END"
                " WHERE id=%s", (FEED_REMOVED_KEY, tid))
            if cur.rowcount == 0:
                missed.append(tid)
        if missed:
            log.warning("removal delta named %d id(s) not in the ledger "
                        "(id churn after a reconnect?): %s",
                        len(missed), ", ".join(missed[:10]))
        # a retired row's notes, receipts, categories and pairings follow
        # the charge when it lives on under another id (a re-link numbers
        # the same purchases afresh); best-effort, the nightly sweep is the
        # backstop
        _reanchor_after_removal(conn, [t for t in txn_ids if t not in missed])
    return len(txn_ids) - len(missed)


# Keep in step with migration 157, which applied the same rule once to the
# ledgers that existed when the stamp was introduced.
_LEGACY_FEED_REMOVED_SQL = """
    UPDATE transactions t
       SET raw = COALESCE(t.raw, '{}'::jsonb)
                 || jsonb_build_object(%(key)s::text, now())
     WHERE t.removed = 1
       AND t.pending = 0
       AND t.retired_at IS NULL
       AND (t.raw IS NULL OR jsonb_typeof(t.raw) = 'object')
       AND NOT (COALESCE(t.raw, '{}'::jsonb) ? %(key)s)
       AND position(':' IN t.id) = 0
       AND EXISTS (SELECT 1
                     FROM accounts a
                     JOIN items i ON i.tenant_id = a.tenant_id
                                 AND i.id = a.item_id
                    WHERE a.tenant_id = t.tenant_id
                      AND a.id = t.account_id
                      AND i.aggregator = 'plaid')
       AND NOT EXISTS (SELECT 1
                         FROM transactions l
                        WHERE l.tenant_id = t.tenant_id
                          AND l.account_id = t.account_id
                          AND l.removed = 0
                          AND l.date = t.date
                          AND l.amount = t.amount)"""


def stamp_unstamped_feed_withdrawals(conn) -> int:
    """Stamp, as the feed's withdrawal, the rows a removal delta retired
    before mark_removed stamped them — for an archive written then. A row
    qualifies only when every other way of being retired is ruled out:
    posted (a retired hold is the pending-to-posted swap), no decision
    behind it (retired_at), the aggregator's own unprefixed id on the one
    connection kind that sends removal deltas, and no live copy of the
    same charge on the account (a merged twin always has one). Returns
    how many rows were stamped."""
    from ..engine.budget import FEED_REMOVED_KEY
    return conn.execute(_LEGACY_FEED_REMOVED_SQL,
                        {"key": FEED_REMOVED_KEY}).rowcount


def _reanchor_after_removal(conn, ids: list[str]) -> None:
    if not ids:
        return
    try:
        from . import reanchor
        # a savepoint: callers hold a transaction open around the retire,
        # and a failed re-anchor must not abort it
        with conn.transaction():
            # A pair still on a withdrawn row is NOT released here. Its
            # re-numbered twin can arrive on a later page of this same sync,
            # on the next sync, or through a re-link days later, and the
            # pair has to still be there for the nightly sweep to carry it.
            # The readers already treat a pair on a removed row as absent;
            # only a full pair's transfer stamp waits, and the nightly sweep
            # releases it once the twin has had the reconcile window to
            # show up (reanchor_stranded's release_after_days).
            reanchor.reanchor_stranded(conn, ids)
    except Exception:                                  # noqa: BLE001
        log.warning("re-anchor after removal failed", exc_info=True)


def reconcile_vanished_pendings(conn, account_ids: list[str],
                                pulled_ids: set[str], id_prefix: str,
                                since: dt.date) -> list[dict]:
    """Retire pending rows a full-pull source no longer carries.

    Stateless connectors (MX, SimpleFIN) re-send their whole window every
    sync and have no removal delta, so a cancelled or expired pending hold
    simply stops appearing — and without this it sat at pending=1 forever,
    double-counting spend once the real charge posted under another id.
    Only pending rows go: a posted row older than the rolling window is
    history, not a retraction. Scoped to the connector's own id prefix so
    file-imported rows on the same account are never touched."""
    if not account_ids:
        return []
    stale = [r for r in conn.execute(
        "SELECT id, account_id FROM transactions WHERE account_id = ANY(%s) "
        "AND pending = 1 AND removed = 0 AND id LIKE %s AND date >= %s",
        (account_ids, id_prefix + "%", since)).fetchall()
        if r["id"] not in pulled_ids]
    if stale:
        # under the pairing lock, as mark_removed: a link in flight on a
        # hold commits before the retire or sees the row gone
        from ..web.data import _REIMB_LOCK
        with conn.transaction():
            conn.execute(_REIMB_LOCK)
            conn.execute("UPDATE transactions SET removed=1"
                         " WHERE id = ANY(%s) AND removed = 0",
                         ([r["id"] for r in stale],))
            log.info("retired %d vanished pending row(s) for %s",
                     len(stale), id_prefix)
            # the charge usually posts under another id: what was written
            # on the hold follows it
            _reanchor_after_removal(conn, [r["id"] for r in stale])
    return stale


# Some aggregator credentials live IN the request URL — a SimpleFIN access
# token is `https://user:pass@bridge.simplefin.org/...`, and the userinfo IS
# the bearer secret. httpx transport exceptions stringify the full URL, so
# persisting raw exception text (error=f"{e}") would write that credential
# in PLAINTEXT into sync_log.error, defeating the at-rest envelope
# encryption the token is deliberately stored under. Redact userinfo at the
# sink so every syncer, present and future, is covered rather than only the
# ones anyone remembered.
_URL_CREDS = re.compile(r"(\w+://)[^\s/@]+@")


def redact_url_credentials(text: str | None) -> str | None:
    if not text:
        return text
    return _URL_CREDS.sub(r"\1***@", text)


def log_sync(conn, item_id: str, added: int, error: str | None = None,
             request_id: str | None = None, *, modified: int = 0,
             removed: int = 0) -> None:
    """request_id (Plaid) is retained because Plaid support and the
    dashboard Activity Log key on it.

    modified/removed are the sync's real delta counts, passed through from
    the caller rather than hardcoded — zeros there would blind exactly the
    diagnosis (phantom pendings, lost removal deltas) that sync_log exists
    to serve."""
    conn.execute(
        "INSERT INTO sync_log (item_id, added, modified, removed, error, "
        "request_id) VALUES (%s,%s,%s,%s,%s,%s)",
        (item_id, added, int(modified), int(removed),
         redact_url_credentials(error), request_id))
