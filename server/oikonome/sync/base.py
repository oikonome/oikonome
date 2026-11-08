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
import hmac
import logging
import math
from ..envnum import env_flag
import re
import secrets
from collections import defaultdict
from typing import Protocol

from ..db import crypto, tenancy
from . import account_sources, card_number
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
    Item on the household's own keys is their bill, not the instance's.
    So is an Item handed back as a repeat of a login the household already
    holds (sync.plaid.REPEAT_LEDGER_REASON): the household reached for no
    new institution, and the repeats are bounded by their own limit. And so
    is an Item released to link the same bank again for a wider history
    window (RELINK_LEDGER_REASON), for the same reason."""
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM plaid_item_ledger l
           WHERE l.tenant = current_setting('app.tenant_id', true)
             AND l.credential_source = 'platform'
             AND l.removed_reason IS DISTINCT FROM 'repeat'
             AND l.removed_reason IS DISTINCT FROM 'relink'
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


def check_institution_cap(conn, spare: int = 0) -> None:
    """Raise ValueError (friendly copy) when connecting one more
    institution would pass the instance's cap. Call before opening any
    ADD-a-connection door; update/repair flows are exempt. The cap counts
    live institutions AND Items released in the last RELEASED_HOLD_DAYS,
    so a disconnected slot frees once that window has passed.

    `spare`: slots the caller is about to free — a relink of the same
    bank for a wider history window keeps the old Item live until the
    new one is in, so for that one link the old Item's place counts as
    free; the old Item is released the moment the new one is saved."""
    cap = institution_cap(conn)
    if cap is None:
        return
    n = institutions_held(conn) - max(0, spare)
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


# The Plaid ledger reason of an Item released so the same bank can be
# linked again with a wider history window (the window is fixed when an
# Item is created). Not counted under the institution cap.
RELINK_LEDGER_REASON = "relink"


def disconnect_item(conn, item_id: str, *,
                    reason: str = "disconnect") -> dict:
    """Archive a connection so it stops syncing; history is kept.

    For Plaid this ALSO releases the Item on Plaid's side (``/item/remove``)
    — a local archive alone never frees a billing slot. Returns
    ``{ok, plaid_released, release_failed, note, aggregator}``.
    Raises ``KeyError`` if the item does not exist.

    `reason` is what the Plaid ledger records for the release:
    "disconnect" (the default) holds the slot under the institution cap
    for the hold window; RELINK_LEDGER_REASON does not — the household is
    about to link the same bank again for a wider history window, and
    reaches for no new institution.
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
            plaid_mod.stamp_ledger_removed(item_id, reason)
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


def release_fronted_movements(conn, txn_ids: list[str], actor: str) -> int:
    """Remove the fronted movements (a capitalized cost or one marked
    reimbursable) on rows about to be hard-deleted. For the writers
    release_matched_receipts serves, under the same pairing lock, which a
    booking holds too (entities.live_txn_id), so none lands in between.

    Such a movement is the other half of the cost on the business's books,
    and the books stop holding that cost when the row goes. The foreign key's
    SET NULL would keep it, unlinked: capital counts money for a cost that is
    gone, and neither the one-per-charge check nor its unique index sees a
    NULL link, so booking the same row after a re-import counts it twice. A
    draw or distribution that merely names a row is an entry of its own, and
    keeps only losing the link. So does a movement on a business no longer
    active: its books are retention records only delete-forever may destroy,
    and it takes no new booking that could double the cost. FOR SHARE on the
    business orders this look against an archive committing at the same
    moment. Each removal is written to the activity log with what it was, so
    it is never silent (as equity.release_withdrawn_fronted). Returns how
    many went."""
    from ..engine import activity, equity
    ids = [i for i in dict.fromkeys(txn_ids) if i]
    if not ids:
        return 0
    rows = conn.execute(
        """SELECT m.*, e.name AS entity_name, t.name AS txn_name
             FROM equity_movement m
             JOIN transactions t ON t.id = m.txn_id
             JOIN business_entity e ON e.id = m.entity_id
            WHERE m.txn_id = ANY(%s) AND m.kind = ANY(%s)
              AND e.status = 'active'
              FOR UPDATE OF m FOR SHARE OF e""",
        (ids, list(equity.FRONTED_KINDS))).fetchall()
    for r in rows:
        conn.execute("DELETE FROM equity_movement WHERE id = %s", (r["id"],))
        activity.record(
            conn, actor, "business", "released", target=r["txn_id"],
            label=(f"{r['kind']} of {activity.money(r['amount'])} for "
                   f"{r['entity_name']} on "
                   f"{r['txn_name'] or 'a charge'} deleted from the ledger"),
            detail={"movement": equity._row(r)})
    return len(rows)


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
        doomed = lock_account_rows(conn, account_id)
        # an archived business's rows are retention records only its
        # delete-forever may remove (batches._archived_business_rows)
        from .batches import _archived_business_rows
        archived = _archived_business_rows(conn, doomed)
        keep = archived | relied_on_by_feed(
            conn, [d for d in doomed if d not in archived], dropping,
            release_accountless=force_kept)
        if keep:
            doomed = [d for d in doomed if d not in keep]
        detach_pairs(conn, doomed)
        release_matched_receipts(conn, doomed)
        release_fronted_movements(conn, doomed, "a deleted account")
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
        # names it — its links, the budget settings — stays as it was. Its
        # balance does not: the person deleted the account, and no feed
        # will refresh it again, so it is filed as closed (a NULL balance)
        # and leaves net worth, card debt and the runway.
        conn.execute("UPDATE accounts SET balance_current = NULL,"
                     " balance_available = NULL WHERE id = %s", (account_id,))
        note = _purge_kept_note(len(keep) - len(archived), len(archived))
        log_sync(conn, "account-purge", 0,
                 error=f"account {account_id}: {note}")
        return {"accounts": 0, "transactions": n_txn, "holdings": n_hold,
                "crypto_holdings": n_crypto, "kept_transactions": len(keep),
                "kept_archived": len(archived),
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
    # which sources fill it: a row left behind rides every export and
    # reads as orphaned on every restore
    conn.execute("DELETE FROM account_sources WHERE account_id=%s",
                 (account_id,))
    n_acct = conn.execute(
        "DELETE FROM accounts WHERE id=%s", (account_id,)).rowcount
    # the per-connection entity scope was stamped at checkout; an account
    # leaving a business must leave the scope too before a later read here
    tenancy.set_entity_scope(conn)
    return {"accounts": n_acct, "transactions": n_txn,
            "holdings": n_hold, "crypto_holdings": n_crypto,
            "kept_transactions": 0, "kept_archived": 0}


def lock_account_rows(conn, account_id: str) -> list[str]:
    """Lock, in id order, every row an account delete is about to write —
    the account's own rows and the rows paired with them (and the rows
    paired with those), which unlinking a pair rewrites — and return the
    ids of the account's own rows.

    For the deletes of a whole account, after the pairing lock and before
    anything looks at a business: the archived-business check and the
    fronted-cost release take the business row FOR SHARE, and
    entities.delete_forever takes the booked rows first and the business
    row last. A delete that held the share lock and only then reached for
    a row delete-forever holds would deadlock with it; taking the rows
    first keeps the one order (rows, then business) every writer keeps."""
    return [r["id"] for r in conn.execute(
        """WITH own AS (
               SELECT id FROM transactions WHERE account_id = %s),
           hop AS (
               SELECT reimburse_id AS id FROM reimbursements
                WHERE expense_id IN (SELECT id FROM own)
               UNION
               SELECT expense_id FROM reimbursements
                WHERE reimburse_id IN (SELECT id FROM own)),
           hop2 AS (
               SELECT reimburse_id AS id FROM reimbursements
                WHERE expense_id IN (SELECT id FROM hop)
               UNION
               SELECT expense_id FROM reimbursements
                WHERE reimburse_id IN (SELECT id FROM hop))
           SELECT t.id, t.account_id FROM transactions t
            WHERE t.id IN (SELECT id FROM own UNION SELECT id FROM hop
                           UNION SELECT id FROM hop2)
            ORDER BY t.id FOR UPDATE OF t""", (account_id,)).fetchall()
        if r["account_id"] == account_id]


def _purge_kept_note(fed: int, archived: int) -> str:
    """What a delete says about the rows it kept, by why each was kept."""
    parts = [kept_rows_note(fed)] if fed else []
    if archived:
        parts.append(f"{archived} row{'s' if archived != 1 else ''} of an "
                     "archived business kept: restore the business to "
                     "remove them, or they stay as its records.")
    return " ".join(parts)


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
              "crypto_holdings": 0, "kept_transactions": 0,
              "kept_archived": 0}
    for aid in ids:
        # every account of the item goes in this one delete, so a note
        # naming any of them protects nothing (see relied_on_by_feed)
        part = _purge_account_data_locked(conn, aid, tuple(ids),
                                          force_kept)
        for k in totals:
            totals[k] += part[k]
    if totals["kept_transactions"]:
        totals["note"] = (_purge_kept_note(
            totals["kept_transactions"] - totals["kept_archived"],
            totals["kept_archived"]) + " The accounts holding them stay.")
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


# The brand is matched by reading, never by a pattern built from it: a
# pattern per merchant name is a compile per distinct name (the re module
# caches a few hundred), tens of milliseconds for a long one, and every
# upsert and every restored row asks. The text's alphanumerics, lowercased,
# are searched for the brand's; where they occur with a separator (or the
# start) before the first, the fixed pattern below must follow the last.
_ALNUM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz"
                         "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_FUEL_BRAND_RE = re.compile(rf"\b{_FUEL_WORD}\b", re.IGNORECASE)
_FUEL_AFTER_RE = re.compile(
    r"[^A-Za-z0-9]+" + _FUEL_WORD + r"(?:[^A-Za-z0-9]|$)", re.IGNORECASE)


def _brand_then_fuel(letters: str, text: str):
    """The fuel-word match following `letters` (lowercased alphanumerics)
    spelled in `text` with any non-alphanumerics between them and a
    separator or the start before them; None when no such place exists."""
    if not _FUEL_AFTER_RE.search(text):
        return None
    where = [i for i, ch in enumerate(text) if ch in _ALNUM_CHARS]
    read = "".join(text[i] for i in where).lower()
    k = read.find(letters)
    while k >= 0:
        first = where[k]
        if first == 0 or text[first - 1] not in _ALNUM_CHARS:
            m = _FUEL_AFTER_RE.match(text, where[k + len(letters) - 1] + 1)
            if m:
                return m
        k = read.find(letters, k + 1)
    return None


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
    if _FUEL_BRAND_RE.search(brand):
        return None
    letters = "".join(ch for ch in brand if ch in _ALNUM_CHARS).lower()
    if len(letters) < _MIN_BRAND:
        return None
    for text in (name, orig_desc):
        if not text:
            continue
        m = _brand_then_fuel(letters, text)
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
            except (TypeError, ValueError, OverflowError):
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
# migrations 153, 178, 181 and 203 and restore in step with this list.
LONG_DIGITS_RE = card_number.LONG_RUN_RE
FILE_IMPORT_ID_PREFIXES = ("pdf:", "csv:", "ofx:", "qif:", "mint:", "ynab:",
                           "monarch:", "copilot:", "simplifi:")


# What a card or account number is, and its cut, are sync.card_number's;
# these are the names the rest of the product has always called them by.
# The hash input of a file row (FileImportIds.id_for) stays on the
# unbroken-run cut, so widening the stored-name cut never re-ids a row: a
# row whose hash input still holds a number in groups is stored under its
# keyed form from the moment it lands (FileImportIds._key_hashed_over_a_number),
# and the keying pass (key_file_import_ids) takes one stored before.
mask_long_digits = card_number.cut_for_hash
mask_account_name = card_number.cut


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
# so they count as free text too. Migrations 178, 181 and 203 apply the same
# keys to rows already stored; keep them in step. The source account's
# name (_SPLIT_SOURCE_KEYS) is cut the same way, migration 199 for the
# rows already stored.
_FREE_TEXT_KEYS = frozenset((
    "name", "memo", "note", "notes", "payee", "description",
    "original description", "original statement"))

# The raw cells a split import keeps its source account's name under (QIF
# and the vendor exports: "account"; Mint: "Account Name"), compared
# trimmed and lower-cased as headers are. Free text too: a split account
# named by its number would otherwise keep the number in every row it
# holds, after the account's own name and id lost it.
_SPLIT_SOURCE_KEYS = ("account", "account name")
_FREE_TEXT_WORDS = ("memo", "note", "description", "details", "payee",
                    "narrative", "particulars")
_OVERFLOW_KEYS = frozenset(("", "null"))


def _free_text_key(k) -> bool:
    if k is None:
        return True
    key = str(k).strip().lower()
    return (key in _FREE_TEXT_KEYS or key in _OVERFLOW_KEYS
            or key in _SPLIT_SOURCE_KEYS
            or any(w in key for w in _FREE_TEXT_WORDS))


def mask_file_raw(raw, *texts):
    """A file row's raw with card and long numbers masked
    (mask_account_name, as the stored name is) in every cell that holds the row's name or merchant, or that a statement
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
    want = {mask_account_name(x.strip()).lower() for x in texts
            if isinstance(x, str) and x.strip()}

    def cut(v, free, source):
        if isinstance(v, str) and (
                source or free
                or mask_account_name(v.strip()).lower() in want):
            return mask_account_name(v)
        return v

    out = {}
    for k, v in raw.items():
        free = _free_text_key(k)
        # a source account's name is a typed name, cut as one: a card
        # number in groups too (mask_account_name), as the account's id
        # and name are
        source = (k is not None
                  and str(k).strip().lower() in _SPLIT_SOURCE_KEYS)
        if isinstance(v, (list, tuple)):
            v = [cut(x, free, source) for x in v]
        else:
            v = cut(v, free, source)
        out[k] = v
    return out


# A file row's id is an unkeyed hash of "<account id>|date|cents|name|n",
# and every input rides the export beside the row, the name with any long
# number cut to its last four. Two kinds of row hashed a full number the
# export no longer shows. An account an earlier release named by its full
# number (a split source, see rekey_numbered_accounts) had it in its
# id, and the last four show in the moved account's id. A row imported
# before names were masked (FileImportIds.legacy_id) had it in its name,
# and the last four show in the masked name. Either way the number was a
# short search away. Those rows are re-id'd as "<prefix>:k" plus the HMAC
# of their old id under a secret each household keeps and no export
# carries (txn_id_keys, migration 202). The "k" can never begin a hex
# digest, so a keyed id is never keyed twice, and the prefix is kept, so
# every test of where a row came from still holds. A re-import recomputes
# the old id (the file spells the old account id and the full name again)
# and finds the row under its keyed form. The secret never travels, so a
# row restored from another household's archive is re-keyed on the way in
# under the id an import here mints for its line (restore.
# _plan_foreign_keyed_txns), and a keyed row no secret here explains is
# still found by its content (FileImportIds._adopt_by_content).
KEYED_ID_MARK = "k"
_PREFIX_ALTS = "|".join(p.rstrip(":") for p in FILE_IMPORT_ID_PREFIXES)
# an unkeyed file-row id, in a pattern Python and PostgreSQL both read:
# the 40-hex digest FileImportIds mints, or the same digest cut to 20,
# which a ledger migrated in from another tool may carry under the same
# prefixes and hash the same kind of line into
UNKEYED_FILE_ID_PATTERN = (
    rf"^({_PREFIX_ALTS}):[0-9a-f]{{20}}([0-9a-f]{{20}})?$")
_UNKEYED_FILE_ID_RE = re.compile(UNKEYED_FILE_ID_PATTERN)
# the mark mask_long_digits leaves where it cut a run of digits: a name
# holding it may once have held the whole run, and its id been hashed so
MASKED_RUN_PATTERN = card_number.CUT_MARK_PATTERN
# an OFX row keyed by the bank's FITID names its account in plain text
# ("ofx:<account id>:<fitid>"); one that names a numbered manual id is
# moved onto the account it is on now, the id an OFX re-import makes
NUMBERED_OFX_ID_PATTERN = r"^ofx:manual:[^:]*[0-9]{8}"
_NUMBERED_OFX_ID_RE = re.compile(r"^ofx:(manual:[^:]*):(.*)$", re.S)
# the id an account moved off that spelled a full number: in full (an
# adoption recorded before re-keys cut it) or cut (split_lineage_id)
NUMBERED_LINEAGE_PATTERN = (
    f"^manual:[^:]*({card_number.CUT_MARK_PATTERN}|[0-9]{{8}})")


def keyed_file_id(secret: bytes, tid: str) -> str:
    """The keyed form of the file-row id `tid` under a household's secret."""
    prefix = tid.split(":", 1)[0]
    return f"{prefix}:{KEYED_ID_MARK}" + hmac.new(
        secret, tid.encode("utf-8"), hashlib.sha1).hexdigest()


def txn_id_secret(conn, *, create: bool = False) -> bytes | None:
    """The household's secret for keyed file-row ids; None when it has none
    and `create` is false. Read on the household's own connection only: the
    filter is spelled out so a connection outside RLS fails rather than
    reading another household's."""
    row = conn.execute(
        "SELECT secret FROM txn_id_keys WHERE tenant_id ="
        " NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    ).fetchone()
    if row is None:
        if not create:
            return None
        conn.execute(
            "INSERT INTO txn_id_keys (secret) VALUES (%s)"
            " ON CONFLICT (tenant_id) DO NOTHING",
            (crypto.encrypt(conn, secrets.token_hex(32)),))
        row = conn.execute(
            "SELECT secret FROM txn_id_keys WHERE tenant_id ="
            " NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        ).fetchone()
    return crypto.decrypt(conn, row["secret"]).encode("utf-8")


def _held_ids(conn, ids, account_id: str | None = None) -> set[str]:
    """Which of `ids` the household holds (on `account_id`, when given),
    looked up a slice at a time."""
    ids = sorted(set(ids))
    out: set[str] = set()
    for i in range(0, len(ids), ADOPT_SLICE):
        part = ids[i:i + ADOPT_SLICE]
        if account_id is None:
            rows = conn.execute(
                "SELECT id FROM transactions WHERE id = ANY(%s)", (part,))
        else:
            rows = conn.execute(
                "SELECT id FROM transactions WHERE id = ANY(%s)"
                "   AND account_id = %s", (part, account_id))
        out.update(r["id"] for r in rows.fetchall())
    return out


def held_forms(conn, ids, account_id: str | None = None) -> dict[str, str]:
    """Each of `ids` the household holds (on `account_id`, when given) ->
    the id it is held under: itself, or its keyed form (keyed_file_id)
    when the row was re-id'd under the household's secret after the id
    was minted. Every door that recomputes the id an earlier import gave a
    line (a sign or year reading since corrected) asks through here, so a
    keyed row is found where its plain id would have been. Only an
    unkeyed file-row id has a keyed form, and only this household's secret
    is tried, so a household with none hashes nothing: a row keyed under
    another household's secret is re-keyed when a restore brings it
    (restore._plan_foreign_keyed_txns), and an import finds one that was
    not by its content (FileImportIds._adopt_by_content)."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    out = {i: i for i in _held_ids(conn, ids, account_id)}
    rest = [i for i in ids if i not in out and _UNKEYED_FILE_ID_RE.match(i)]
    if rest:
        secret = txn_id_secret(conn)
        if secret is not None:
            keyed = {keyed_file_id(secret, i): i for i in rest}
            for k in _held_ids(conn, keyed, account_id):
                out[keyed[k]] = k
    return out


# a keyed file-row id, as keyed_file_id writes it, in a pattern Python and
# PostgreSQL both read. Spelled out to the digest: an OFX row keyed by the
# bank's FITID ("ofx:<account id>:<fitid>") may begin with the same mark
# when the account id does.
KEYED_FILE_ID_PATTERN = (
    rf"^({_PREFIX_ALTS}):{KEYED_ID_MARK}[0-9a-f]{{40}}$")


def line_shape(date, amount, name) -> tuple | None:
    """What a file row's plain id hashes besides its account id and its
    occurrence — the date, the signed cents, the name with any long run of
    digits cut — read the way the row is stored (the name kept to
    MAX_DESCRIPTOR, without the NULs the upsert drops), so a line in a
    file and its stored row compare equal. None when the amount or date
    cannot be read."""
    try:
        cents = round(float(amount) * 100)
    except (TypeError, ValueError, OverflowError):
        return None
    if date is None:
        return None
    if not isinstance(date, dt.date):
        try:
            date = dt.date.fromisoformat(str(date)[:10])
        except ValueError:
            return None
    return (date, cents,
            (_descriptor(mask_account_name((name or "").replace("\x00", "")))
             or "").lower())


def file_id_pattern(prefix: str, *, plain: bool = False) -> str:
    """The ids importer `prefix` mints, in a pattern Python and PostgreSQL
    both read: its keyed ids, and with `plain` its unkeyed hashes too —
    never an OFX row keyed by the bank's FITID, which is its own line."""
    keyed = f"{KEYED_ID_MARK}[0-9a-f]{{40}}"
    body = f"({keyed}|[0-9a-f]{{20}}([0-9a-f]{{20}})?)" if plain else keyed
    return rf"^{re.escape(prefix)}:{body}$"


def rows_by_line_shape(conn, prefix: str, account_id: str, dates, *,
                       plain: bool = False, skip=()) -> dict[tuple, list]:
    """The rows importer `prefix` holds on `account_id` on any of `dates`
    under an id file_id_pattern matches, by line_shape, each shape's ids
    in id order; `skip` left out. The pool a line no id finds is matched
    against by its content (FileImportIds._adopt_by_content, restore.
    _plan_foreign_keyed_txns): each row stands for one line, so a caller
    takes from the front of a shape's list and never takes a row twice."""
    pool: dict[tuple, list[str]] = defaultdict(list)
    dates = sorted(set(dates))
    if not dates:
        return pool
    skip = set(skip)
    for r in conn.execute(
            "SELECT id, date, amount, name FROM transactions"
            " WHERE account_id = %s AND id ~ %s AND date = ANY(%s)"
            " ORDER BY id",
            (account_id, file_id_pattern(prefix, plain=plain),
             dates)).fetchall():
        shape = line_shape(r["date"], r["amount"], r["name"])
        if shape is not None and r["id"] not in skip:
            pool[shape].append(r["id"])
    return pool


def file_row_id(prefix: str, account_id: str, date, amount, name: str,
                n: int) -> str:
    """The plain id FileImportIds gives the n-th identical line on an
    account, its name hashed masked: the id an import mints for it now."""
    ids = FileImportIds(prefix, account_id)
    return ids._hash(f"{account_id}|{date.isoformat()}|"
                     f"{round(amount * 100)}|{mask_long_digits(name).lower()}",
                     n)


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

    def __init__(self, prefix: str, account_id: str, *,
                 former: tuple[str, ...] = ()):
        self.prefix = prefix
        self.account_id = account_id
        # ids the caller knows this account's rows may have been hashed
        # under that no record names in full: a split source's original
        # id, which a re-key records only with its number cut (see
        # split_lineage_id); the file being imported spells it again
        self._known_former = tuple(a for a in former if a and a != account_id)
        # one instance may serve several source groups of one file that
        # resolved to the same account (two sources whose names cut alike):
        # their occurrence numbering is then shared, so identical lines
        # from both get distinct ids instead of the second overwriting the
        # first, and each group's former ids are added as it joins

        self._occ: dict[str, int] = {}
        self._legacy_occ: dict[str, int] = {}
        self._legacy_of: dict[str, str] = {}
        # every id this import minted -> the hash inputs after the account
        # id, so the same line can be re-hashed under a former account id
        self._tails: dict[str, list[str]] = {}
        # the household's secret for keyed ids, read once on first need;
        # False until then (None is "the household has none")
        self._secret: bytes | None | bool = False
        # every stored id this instance has resolved a line to: one
        # instance may serve several source groups of a file, and a row
        # handed to one group's line is not free for another's
        self._issued: set[str] = set()
        # the ids this import minted over text still holding a card number
        # in groups, which the hash input keeps whole (see sync.card_number)
        self._over_a_number: set[str] = set()

    def also_former(self, former) -> None:
        """Add a source group's former ids (see __init__) to this shared
        instance."""
        more = tuple(a for a in former if a and a != self.account_id
                     and a not in self._known_former)
        self._known_former = self._known_former + more

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
        if mask_account_name(masked) != masked:
            self._over_a_number.add(tid)
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
        under: the pre-masking id, the keyed form of either (see
        keyed_file_id), or the id an earlier import of the same line gave
        it while the account had another id, plain or keyed. A new line
        whose id hashes a whole card number takes its keyed form at once."""
        if self._legacy_of:
            have = {x["id"] for x in conn.execute(
                "SELECT id FROM transactions WHERE id = ANY(%s)",
                (list(self._legacy_of.values()),)).fetchall()}
            have -= self._issued
            txns = [dataclasses.replace(t, id=self._legacy_of[t.id])
                    if self._legacy_of.get(t.id) in have else t
                    for t in txns]
        txns = self._adopt_keyed(conn, txns)
        txns = self._adopt_renamed(conn, txns)
        out = self._key_hashed_over_a_number(
            conn, self._adopt_by_content(conn, txns))
        self._issued.update(t.id for t in out)
        return out

    def _key_hashed_over_a_number(self, conn, txns: list[Transaction]
                                  ) -> list[Transaction]:
        """Swap in the keyed form of an id hashed over a whole card number
        for a line no stored row holds. The stored name shows the last four
        and every other hash input rides the export beside the row, so the
        plain id would leave the number a short search away from anyone
        holding an export. A row already held under the plain id stays there
        for the keying pass (key_file_import_ids), which moves it with every
        reference to it."""
        mine = [t for t in txns if t.id in self._over_a_number]
        if not mine:
            return txns
        held = _held_ids(conn, [t.id for t in mine])
        fresh = {t.id for t in mine} - held
        if not fresh:
            return txns
        if not self._secret_of(conn):
            self._secret = txn_id_secret(conn, create=True)
        return [dataclasses.replace(t, id=keyed_file_id(self._secret, t.id))
                if t.id in fresh else t for t in txns]

    def _adopt_by_content(self, conn, txns: list[Transaction]
                          ) -> list[Transaction]:
        """Swap in a keyed row of this importer on this account that holds
        the same line, when no id found one.

        A keyed id is a function of the secret of the household that
        keyed it, and an archive carries the ids but never the secret, so
        a household that restored them from another (or lost its own)
        cannot recompute them; without this a re-import adds every such
        line again beside its restored row, counting it twice, and the
        cross-source dedup deliberately skips the importer's own rows. The
        fields compared are the ones the plain id hashes besides the
        account id (line_shape), so the row taken is the one the id would
        have found; each stored row stands for one line, and identical
        lines take identical rows in file order, as their occurrence
        numbers would. Nothing is read unless the account holds a keyed
        row of this importer."""
        mine = [t for t in txns if t.id in self._tails
                and t.account_id == self.account_id]
        if not mine:
            return txns
        if not conn.execute(
                "SELECT 1 FROM transactions WHERE account_id = %s"
                "   AND id ~ %s LIMIT 1",
                (self.account_id, file_id_pattern(self.prefix))).fetchone():
            return txns
        held = _held_ids(conn, [t.id for t in mine])
        mine = [t for t in mine if t.id not in held]
        if not mine:
            return txns
        pool = rows_by_line_shape(conn, self.prefix, self.account_id,
                                  [t.date for t in mine],
                                  skip={t.id for t in txns} | self._issued)
        swap: dict[str, str] = {}
        for t in mine:
            shape = line_shape(t.date, t.amount, t.name)
            rows = pool.get(shape) if shape is not None else None
            if rows:
                swap[t.id] = rows.pop(0)
        if not swap:
            return txns
        return [dataclasses.replace(t, id=swap[t.id]) if t.id in swap else t
                for t in txns]

    def _secret_of(self, conn) -> bytes | None:
        if self._secret is False:
            self._secret = txn_id_secret(conn)
        return self._secret

    def _adopt_keyed(self, conn, txns: list[Transaction]
                     ) -> list[Transaction]:
        """Swap in the keyed form of an id this import minted, or of its
        pre-masking twin, when that is what the household holds: the row
        was re-id'd under the household's secret after it was imported.
        A household with no secret hashes nothing here; a keyed row no
        secret here explains is _adopt_by_content's."""
        mine = [t for t in txns if t.id in self._tails]
        if not mine:
            return txns
        secret = self._secret_of(conn)
        if secret is None:
            return txns
        present = _held_ids(conn, [t.id for t in mine])
        want: dict[str, list[str]] = {}
        for t in mine:
            if t.id in present:
                continue
            forms = [t.id]
            if t.id in self._legacy_of:
                forms.append(self._legacy_of[t.id])
            want[t.id] = [keyed_file_id(secret, f) for f in forms]
        if not want:
            return txns
        have = _held_ids(conn, [k for ks in want.values() for k in ks])
        # a row an earlier source group of this file took is spoken for
        taken = {t.id for t in txns} | self._issued
        swap: dict[str, str] = {}
        for tid, keyed in want.items():
            for k in keyed:
                if k in have and k not in taken:
                    swap[tid] = k
                    taken.add(k)
                    break
        if not swap:
            return txns
        return [dataclasses.replace(t, id=swap[t.id]) if t.id in swap else t
                for t in txns]

    def _former_account_ids(self, conn) -> list[str]:
        """Every id this account has stood under before: the ids adoption
        moved it off (followed back through earlier adoptions), then the
        comma spellings a comma rename could have started from.

        Recorded adoptions come first, so a capped search keeps them. An id
        the app minted itself (`manual:`, slugged to [a-z0-9-]) never held
        a comma, so its dashes are not taken for renamed commas: that would
        multiply every imported line by up to 63 spellings for nothing."""
        seen = {self.account_id, *self._known_former}
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
        return (list(self._known_former) + sorted(recorded)
                + sorted(spelled_ids))

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
        # a row re-id'd under the household's secret is found by the keyed
        # form of the same guess, so each guess counts twice then
        secret = self._secret_of(conn)
        per_former = sum(len(self._tails[t.id]) for t in missing) * (
            1 if secret is None else 2)
        keep = max(1, ADOPT_MAX_GUESSES // max(1, per_former))
        if keep < len(former):
            log.warning("file import into %s: id adoption limited to %d of "
                        "%d former account ids", self.account_id, keep,
                        len(former))
            former = former[:keep]
        budget = ADOPT_MAX_GUESSES
        # a row an earlier source group of this file took is spoken for
        taken = {t.id for t in txns} | self._issued
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
                if secret is not None:
                    gs += [keyed_file_id(secret, g) for g in gs]
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


# How far back a household asked its ledger to reach, in whole months
# (Settings and the setup wizard). Absent means everything a source has.
# The same answer shortens a bank link's backfill (sync/plaid) and trims a
# file import, so a person who asked for three months does not get three
# months from the bank and five years from the statement they dropped in.
HISTORY_MONTHS = (1, 3, 6, 12)


def history_months_of(cfg: dict | None) -> int | None:
    """The stored history window, or None for "everything". A value
    outside the offered choices reads as None: a restored or hand-edited
    config must widen the window, never silently narrow it."""
    v = (cfg or {}).get("history_months")
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n in HISTORY_MONTHS and n == v else None


def months_before(day: dt.date, months: int) -> dt.date:
    """The same day of the month `months` calendar months earlier, clamped
    to the end of a shorter month (May 31 less three months is Feb 28/29)."""
    import calendar
    y, m = divmod(day.year * 12 + (day.month - 1) - months, 12)
    m += 1
    return dt.date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def history_cutoff(conn) -> dt.date | None:
    """The first day a file import may land, or None when the household
    keeps everything. Counted from the day it is where the household lives,
    so the window does not move a day early in the evening."""
    row = conn.execute(
        "SELECT config->'history_months' AS months,"
        "       config->>'timezone' AS timezone"
        "  FROM tenant_settings").fetchone()
    if row is None:
        return None
    n = history_months_of({"history_months": row["months"]})
    if n is None:
        return None
    from .. import localtime
    today = localtime.now_local({"timezone": row["timezone"]}).date()
    return months_before(today, n)


# How far back a routine refresh of a linked bank reaches (MX's and the
# SimpleFIN worker's 30-day window). A history window never narrows it.
ROUTINE_PULL_DAYS = 30


def bound_first_pull(conn, start: dt.date | None) -> dt.date | None:
    """A bank link's deep first pull start date, moved up to the
    household's history window when it reaches further back (None, no
    limit, reaches furthest). Plaid asks for the window at link time
    (plaid.days_requested); MX and SimpleFIN ask by start date on every
    pull, so the bound is applied where that date is chosen. It never
    starts later than a routine refresh would: a short window must not
    stop late-posting rows near its edge from being refreshed."""
    cutoff = history_cutoff(conn)
    if cutoff is None:
        return start
    floor = min(cutoff,
                dt.date.today() - dt.timedelta(days=ROUTINE_PULL_DAYS))
    return floor if start is None or start < floor else start


def drop_before_history(conn, txns: list[Transaction]
                        ) -> tuple[list[Transaction], int]:
    """A file import's rows less those dated before the household's history
    window, and how many were left out. Applied just before the write, after
    the importer's own duplicate and re-import bookkeeping has run over the
    whole file: an earlier import of the same statement can still be
    recognised and corrected, and only rows that would otherwise have been
    added are counted as skipped.

    A row whose id already stands on its account is kept whatever its date.
    It is not a new old row but the household's own row coming in again,
    and the write is what carries a correction to it: the re-import
    bookkeeping only moves a row onto the id its corrected reading hashes
    to, with its old amount and date, and the write then sets the new
    ones. Dropped here, a sign or year fix made before the window was set
    would be reported as corrected, left wrong, and re-dropped by every
    later import of the file."""
    cutoff = history_cutoff(conn)
    if cutoff is None:
        return txns, 0
    from ..engine.compat import as_date

    def inside(t):
        try:
            d = as_date(t.date)
        except (TypeError, ValueError):
            return True        # the write refuses a bad date with its own words
        return d is None or d >= cutoff
    older = [t for t in txns if not inside(t)]
    if not older:
        return txns, 0
    have = {(r["id"], r["account_id"]) for r in conn.execute(
        "SELECT id, account_id FROM transactions WHERE id = ANY(%s)",
        (list({t.id for t in older}),)).fetchall()}
    kept = [t for t in txns
            if inside(t) or (t.id, t.account_id) in have]
    return kept, len(txns) - len(kept)


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
        # the stored text is cut wider than the id's hash input: see
        # sync.card_number
        line = _descriptor(mask_account_name(t.name) if filed else t.name)
        named = _descriptor(mask_account_name(t.merchant_name) if filed
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
        # Cut to a descriptor's length BEFORE any parsing: a file import's
        # raw is the whole source row, so this cell can be any size a
        # header names it, and the counterparty patterns below backtrack
        # quadratically on a long enough run of brackets or spaces.
        raw = t.raw
        desc = (raw or {}).get("original_description")
        desc = (_descriptor(mask_account_name(desc) if filed else desc)
                if isinstance(desc, str) else None)
        if filed and isinstance(raw, dict) and isinstance(
                raw.get("original_description"), str) and len(
                raw["original_description"]) > MAX_DESCRIPTOR:
            raw = {**raw, "original_description":
                   _descriptor(raw["original_description"])}
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
        merchant = _descriptor(mask_account_name(merchant) if filed
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
             jsonb(mask_file_raw(raw, t.name, t.merchant_name)
                   if filed else raw),
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
            # Both rows of every pair are locked, in id order, before any
            # child moves: a split writer (splits.set_split, a person's
            # category edit, the store matcher) takes the charge's row
            # before its parts, and the carries below touch the pending
            # row's parts before the pending row itself is retired at the
            # end — without the row lock first, a person splitting or
            # filing the hold during the settlement could deadlock it.
            conn.execute("SELECT 1 FROM transactions WHERE id = ANY(%s)"
                         " ORDER BY id FOR UPDATE",
                         (sorted(set(new_ids) | set(old_ids)),))
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

            def _carry(*sqls: str) -> None:
                # several statements carry as one: all of them or none
                try:
                    with conn.transaction():
                        for sql in sqls:
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
            # A person's split outranks a store's (its matcher's reading of
            # a receipt, recomputed every run): when the hold carries the
            # person's parts, the posted row's store parts give way first —
            # left there, they would strand the person's answer on the
            # retired row. A store's parts never displace a person's.
            _carry("DELETE FROM transaction_splits s"
                   " USING unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
                   " WHERE s.txn_id=v.new_id AND s.source <> 'user' AND EXISTS"
                   "  (SELECT 1 FROM transaction_splits x"
                   "   WHERE x.txn_id=v.old_id AND x.source = 'user')",
                   "UPDATE transaction_splits s SET txn_id=v.new_id"
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


# The most source accounts one file may split into. A household
# export (every card, loan and closed account Mint ever saw) stays far
# below it; a file naming tens of thousands is not a ledger, and each
# split is a new account every page of the app then lists.
MAX_SPLIT_ACCOUNTS = 100


def check_split_accounts(names) -> None:
    """Refuse a file that would split into more accounts than one
    household export holds — before any account is made."""
    n = len(set(names))
    if n > MAX_SPLIT_ACCOUNTS:
        raise ValueError(
            f"this file names {n} different accounts; one import can split "
            f"into at most {MAX_SPLIT_ACCOUNTS}. Export the accounts in "
            f"smaller groups, or one at a time.")


def tally_split(counts: dict, name: str, n: int) -> None:
    """Add `n` rows to a split result keyed by account name, cut as the
    account's own name is (mask_account_name: the result is shown and
    logged); two source names that mask alike share one key rather than
    one hiding the other."""
    name = mask_account_name(name)
    counts[name] = counts.get(name, 0) + n


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


def _split_source(text: str, *, grouped_whole: bool = False) -> str:
    """A source account name as its identity is compared: the full account
    number cut to its last four, typed in groups or not (mask_account_name),
    case and spacing folded. Rows store the source name masked, so both
    sides of every comparison are masked; a row an earlier build stored
    with a grouped number whole is cut here on the way in. `grouped_whole`
    cuts only unbroken runs, as those builds did, to recompute an id they
    minted."""
    cut = mask_long_digits if grouped_whole else mask_account_name
    return " ".join(cut((text or "").strip()).casefold().split())


def _holds_source(conn, aid: str, prefix: str, me: str) -> bool | None:
    """None: no account `aid`. Otherwise whether it holds the source `me`.

    Asked of the rows, because they record where each one came from: an
    earlier release could give two sources one id, and the account's name
    names only the first of them. An account with no row of this importer
    naming a source (an OFX statement keeps no source cell; every row
    deleted) is matched on its name, which a rename never touches — that is
    display_name."""
    row = conn.execute("SELECT name FROM accounts WHERE id=%s",
                       (aid,)).fetchone()
    if row is None:
        return None
    held = {_split_source(r["v"]) for r in conn.execute(
        """SELECT DISTINCT e.value #>> '{}' AS v
             FROM transactions t, jsonb_each(t.raw) e
            WHERE t.account_id = %s AND left(t.id, %s) = %s
              AND jsonb_typeof(t.raw) = 'object'
              AND jsonb_typeof(e.value) = 'string'
              AND lower(btrim(e.key)) = ANY(%s)
            LIMIT 101""",
        (aid, len(prefix) + 1, prefix + ":",
         list(_SPLIT_SOURCE_KEYS))).fetchall()}
    held.discard("")
    if held:
        return me in held
    return _split_source(row["name"] or "") == me


def _adopted_split_account(conn, starts, prefix: str, me: str,
                           lines=None) -> str | None:
    """The account an id this importer once minted was moved to, if it
    still exists and holds this source: following the adoption records
    the move left (migration 199 and restore re-key an id that spelled a
    full account number), so a re-import lands on the moved account.
    A move records the old id with its number cut (split_lineage_id), so
    each start is looked up in that form as well as in full.

    A cut form is not unique: two cards sharing their last four leave one
    key pointing at two accounts, and the source name (masked the same
    way) cannot tell them apart either. Such a key is followed only to the
    one account holding rows whose ids recompute under the full id, which
    the file spells again (`lines`, see _lineage_by_rows); with no such
    account, or several, it is not followed at all, since filing one
    card's history onto the other's account is a merge nobody asked for."""
    todo: list[str] = []
    for s in starts:
        todo.append(s)
        for cut in lineage_keys(s):
            if cut == s:
                continue
            if len(_lineage_targets(conn, cut)) > 1:
                found = _lineage_by_rows(conn, prefix, s, cut, lines)
                if (found is not None
                        and _holds_source(conn, found, prefix, me)):
                    return found
                continue
            todo.append(cut)
    todo = list(dict.fromkeys(todo))
    seen = set(todo)
    while todo and len(seen) < 32:
        cur = todo.pop(0)
        for r in conn.execute(
                "SELECT account_id FROM account_adoptions"
                " WHERE old_account_id = %s AND item_id = 'manual'"
                " ORDER BY adopted_at DESC", (cur,)).fetchall():
            nxt = r["account_id"]
            if nxt in seen:
                continue
            seen.add(nxt)
            if _holds_source(conn, nxt, prefix, me):
                return nxt
            todo.append(nxt)
    return None


def _lineage_targets(conn, cut: str) -> set[str]:
    """The accounts a re-key moved some id with this cut form onto."""
    return {r["account_id"] for r in conn.execute(
        "SELECT DISTINCT account_id FROM account_adoptions"
        " WHERE old_account_id = %s AND item_id = 'manual'",
        (cut,)).fetchall()}


# Lines of a file hashed to tell apart the accounts one cut lineage key
# points at: the first rows are enough to find an earlier import's ids,
# and a 50,000-row file is not hashed whole for it.
LINEAGE_PROBE_LINES = 5_000


def _lineage_by_rows(conn, prefix: str, legacy: str, cut: str, lines
                     ) -> str | None:
    """Of the accounts the cut key `cut` leads to, the one holding rows
    whose ids were hashed under the full old id `legacy`, recomputed from
    the file's own lines (date, amount, name) the way FileImportIds made
    them. None when no account, or more than one, holds such a row: the
    key is then no evidence of where this source lives."""
    if not lines:
        return None
    reach, todo = set(), [cut]
    while todo and len(reach) < 32:
        cur = todo.pop(0)
        for nxt in sorted(_lineage_targets(conn, cur)):
            if nxt not in reach:
                reach.add(nxt)
                todo.append(nxt)
    if not reach:
        return None
    ids = FileImportIds(prefix, legacy)
    probe: set[str] = set()
    for n, (date, amount, name) in enumerate(lines):
        if n >= LINEAGE_PROBE_LINES:
            break
        tid = ids.id_for(date, amount, name)
        probe.add(tid)
        if ids.legacy_id(tid):
            probe.add(ids.legacy_id(tid))
    if not probe:
        return None
    # rows re-id'd under the household's secret answer to the keyed form
    secret = txn_id_secret(conn)
    if secret is not None:
        probe |= {keyed_file_id(secret, p) for p in probe}
    held = [r["account_id"] for r in conn.execute(
        "SELECT DISTINCT account_id FROM transactions"
        " WHERE id = ANY(%s) AND account_id = ANY(%s)",
        (sorted(probe), sorted(reach))).fetchall()]
    return held[0] if len(held) == 1 else None


# A digest an id carries is spelled in letters: hex digits g-p stand for
# 0-9. Eight hex digits are all decimal about one time in forty-four, and
# an id ending in eight decimal digits reads as one that spells a full
# account number (NUMBERED_SPLIT_ID_RE), so every restore would move the
# account and the next import would mint the digest id beside it again.
_DIGEST_LETTERS = str.maketrans("0123456789", "ghijklmnop")


def letter_digest(hexdigest: str) -> str:
    """The first eight digits of a hex digest, with no decimal digit in
    them. Keep in step with migration 199's translate()."""
    return hexdigest[:8].translate(_DIGEST_LETTERS)


def split_legacy_id(prefix: str, src: str) -> str:
    """The id every release before the masked ids gave a split source: the
    plain slug of its name, a full account number and all."""
    return f"manual:{prefix}-{_split_slug(src.strip())}"


# The "-N" an account door appends when the slug a name gives is taken
# ("Checking!" beside "Checking?": web/api and web/bulk_import count from
# 2). The cut never takes a short group after a number into it
# (sync.card_number), so "visa-4111-1111-1111-1111-2" cuts to
# "visa-…1111-2" either way; what this decides is whether the "-2" is a
# door's mark, which a move records beside the cut last four, or the
# number's own last group. It reads as a mark only when the digits before
# it still spell a full number on their own (card_number.ends_in_number). A door counts from 2
# and cut_lineage_candidates walks no further than CUT_LINEAGE_MAX_SUFFIX,
# so a longer or zero-led group ("-0012-3456-7890-123") is the number's
# own. Shape alone cannot tell "-12" the door wrote from a number's own
# last group, so what a move STORES asks the household too (door_suffix).
_COLLISION_SUFFIX_RE = re.compile(r"^(.*\d)(-[1-9]\d?)$", re.S)


def collision_suffix(old: str) -> tuple[str, str]:
    """(`old` without its trailing "-N" collision suffix, the suffix), or
    (`old`, "") when it has none: see _COLLISION_SUFFIX_RE."""
    m = _COLLISION_SUFFIX_RE.match(old or "")
    if (m and 2 <= int(m.group(2)[1:]) <= CUT_LINEAGE_MAX_SUFFIX
            and card_number.ends_in_number(m.group(1))):
        return m.group(1), m.group(2)
    return old, ""


def door_suffix(conn, old: str) -> tuple[str, str]:
    """collision_suffix, kept only when the id without it is an account
    or a move recorded one (a door appends "-N" only because that id was
    taken). Otherwise the digits are the number's own last group, and
    keeping them beside a cut last four would store more of the number
    than four digits."""
    head, suffix = collision_suffix(old)
    if suffix and (conn.execute("SELECT 1 FROM accounts WHERE id = %s",
                                (head,)).fetchone()
                   or _has_lineage_record(conn, head)):
        return head, suffix
    return old, ""


def _cut_lineage(head: str, suffix: str = "") -> str:
    return mask_account_name(head) + suffix


def split_lineage_id(old: str) -> str:
    """What a re-key records as the id an account moved off: the old id
    with every full number cut to its last four. The record must not keep
    the number it moved the account away from (it rides /export and the
    portability archive), and no digest of it either: an account number
    whose last four the new id shows is a short search away from any
    unkeyed hash. A re-import needs the full id only to recompute its rows'
    ids, and the file it reads spells that id again (split_legacy_id).
    A run of eight digits is cut first, exactly as migration 199 cut it,
    so every record it wrote is still found under this form; a card
    number typed in groups ("visa-4111-1111-1111-1111", which holds no
    run of eight) is cut after, the way mask_account_name cuts a typed
    name. A door's "-N" collision suffix after the number is kept after its
    cut last four (collision_suffix), so each suffixed account keeps a
    lineage of its own. What a move stores is recorded_lineage_id, which
    keeps the suffix only when the household shows a door wrote it; a
    lookup tries both forms (lineage_keys)."""
    return _cut_lineage(*collision_suffix(old))


def recorded_lineage_id(conn, old: str) -> str:
    """The cut form a move records for `old`: split_lineage_id with a
    trailing "-N" kept only when door_suffix confirms it."""
    return _cut_lineage(*door_suffix(conn, old))


def lineage_keys(old: str) -> list[str]:
    """Every cut form a move may have recorded `old` under: its door's
    suffix kept (split_lineage_id) or cut with the number
    (recorded_lineage_id when no door wrote it)."""
    return sorted({split_lineage_id(old), _cut_lineage(old)})


def split_former(prefix: str, src: str, target: str) -> tuple[str, ...]:
    """The former account id a split import hands FileImportIds for one
    source: its legacy id, when ensure_split_account found the source
    anywhere else. A re-key records that id only with its number cut, so
    the file is the one place that still spells it."""
    legacy = split_legacy_id(prefix, src)
    return (legacy,) if legacy != target else ()


def split_account_id(prefix: str, src: str, *, taken: bool = False,
                     hex_digest: bool = False,
                     grouped_whole: bool = False) -> str:
    """The id a build before opaque ids gave a source account when nothing
    earlier claimed it — only for finding the accounts those builds made
    (ensure_split_account); a new split account's id is minted
    (account_sources.mint_id), never derived from the source.

    The slug of the source name with any full account number cut to its
    last four. A slug that dropped what tells two names apart — cut at 40
    characters, or a name in a script the slug cannot spell — carried a
    short digest of the source; so did one whose readable id another
    source already held (`taken`). `hex_digest` spells that digest as a
    build before letter_digest did, and `grouped_whole` cuts only unbroken
    runs as a build before grouped numbers were cut did."""
    masked = (mask_long_digits if grouped_whole else mask_account_name)(
        src.strip())
    full = re.sub(r"[^a-z0-9]+", "-", masked.lower()).strip("-")
    slug = full[:40]
    lossy = (not slug or len(full) > 40
             or any(ch.isalpha() and not ch.isascii() for ch in masked))
    if not lossy and not taken:
        return f"manual:{prefix}-{slug}"
    digest = hashlib.sha1(_split_source(
        src, grouped_whole=grouped_whole).encode("utf-8")).hexdigest()
    digest = digest[:8] if hex_digest else letter_digest(digest)
    return f"manual:{prefix}-{(slug or 'x')[:31]}-{digest}"


def ensure_split_account(conn, prefix: str, name: str, *,
                         key: str | None = None,
                         type_: str | None = None,
                         subtype: str | None = None,
                         lines=None, source: str | None = None,
                         fitids=None) -> str:
    """A manual account for one source-side account of a multi-account
    file (Mint CSV, OFX, QIF). Re-imports land on the
    same account; existing rows — including a user rename or
    reclassification — are never overwritten. Type defaults to the
    name-based inference unless the caller knows better (OFX statements
    do).

    The source is `source` when the importer has the account's own number
    (OFX's ACCTID), else `key` or the source name. Its row in
    account_sources names the account it fills. A source with none yet is
    found where a build before opaque ids put it, and the import never
    re-keys (_earlier_split_account), then remembered there. Otherwise —
    or when that account already answers to a DIFFERENT full number (two
    cards sharing their last four, which every derived id and every cut
    name made one) — a new account under an opaque id. Except that a
    source whose rows that account already holds stays on it, however many
    cards it holds: a new account's rows would be hashed under a new id,
    and the re-import would insert its whole history a second time
    (_holds_file_rows over the file's `lines` and, for OFX, its `fitids`).
    For the same reason a source no row of this household names (its rows
    were keyed under another household's secret, which a restore does not
    carry) goes to the split account that holds its rows
    (_split_account_holding) before one is minted. The importer passes
    split_legacy_id to FileImportIds whenever the answer is not the legacy
    id, so rows hashed under it are found."""
    from . import account_sources as srcs
    if type_ is None:
        type_, subtype = infer_account_type(name)
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, access_token)
           VALUES ('manual','manual','Manual',NULL)
           ON CONFLICT (tenant_id, id) DO NOTHING""")
    src = (key or name).strip()
    who = (source or src).strip()
    with conn.transaction():
        # check-then-act, serialized with every account door
        srcs.hold_lock(conn)
        found = srcs.lookup(conn, prefix, who)
        if found:
            return found
        earlier = _earlier_split_account(conn, prefix, name, src, lines)
        if earlier and (
                not srcs.claimed_by_another(conn, prefix, who, earlier)
                or _holds_file_rows(conn, prefix, earlier, src, lines,
                                    fitids)):
            srcs.remember(conn, prefix, who, earlier)
            return earlier
        held = _split_account_holding(conn, prefix, name, who, src, lines,
                                      fitids, skip=earlier)
        if held:
            srcs.remember(conn, prefix, who, held)
            return held
        aid = srcs.mint_id(conn, prefix)
        # The stored name keeps only the last four, the rule every
        # hand-made account follows.
        conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype,
                                     updated_at)
               VALUES (%s,'manual',%s,%s,%s,now())""",
            (aid, mask_account_name(name.strip()), type_, subtype))
        srcs.remember(conn, prefix, who, aid)
        return aid


def _holds_file_rows(conn, prefix: str, aid: str, src: str, lines,
                     fitids) -> bool:
    """Whether account `aid` holds a row of this file's source: a row
    whose id the file's own `lines` (date, amount, name) recompute, or an
    OFX row under one of its `fitids`, hashed under `aid`, under the id
    the source had before (split_legacy_id), or under an id a move took
    the account off — plain or keyed under the household's secret. Only
    the first LINEAGE_PROBE_LINES lines are hashed."""
    heads = {aid, split_legacy_id(prefix, src)} | {
        r["old_account_id"] for r in conn.execute(
            "SELECT old_account_id FROM account_adoptions"
            " WHERE account_id = %s", (aid,)).fetchall()}
    probe: set[str] = set()
    for head in sorted(heads):
        ids = FileImportIds(prefix, head)
        for n, (date, amount, name) in enumerate(lines or ()):
            if n >= LINEAGE_PROBE_LINES:
                break
            tid = ids.id_for(date, amount, name)
            probe.add(tid)
            if ids.legacy_id(tid):
                probe.add(ids.legacy_id(tid))
        probe |= {f"ofx:{head}:{f}" for f in (fitids or ()) if f}
    if not probe:
        return False
    secret = txn_id_secret(conn)
    if secret is not None:
        probe |= {keyed_file_id(secret, p) for p in probe}
    probe_ids = sorted(probe)
    # an OFX id a reused FITID suffixed ("~<date>…") is still that line's
    return conn.execute(
        "SELECT 1 FROM transactions WHERE account_id = %s"
        "   AND (id = ANY(%s) OR split_part(id, '~', 1) = ANY(%s)) LIMIT 1",
        (aid, probe_ids, probe_ids)).fetchone() is not None


def _split_account_holding(conn, prefix: str, name: str, who: str,
                           src: str, lines, fitids, *,
                           skip: str | None = None) -> str | None:
    """The live split account of this importer that holds this source's
    rows (_holds_source, then _holds_file_rows) and answers to no other
    card here, for a source no row of this household names: restored from
    another household's archive, its source rows are keyed under a secret
    that never travels, and its opaque id is not one any earlier build
    derived. The first by id when several do; None when none does, so a
    source with no rows anywhere gets an account of its own."""
    from . import account_sources as srcs
    me = _split_source(name)
    for r in conn.execute(
            "SELECT id FROM accounts WHERE item_id = 'manual'"
            "   AND left(id, %s) = %s AND user_removed_at IS NULL"
            "   AND id IS DISTINCT FROM %s ORDER BY id",
            (len(prefix) + 8, f"manual:{prefix}-", skip)).fetchall():
        aid = r["id"]
        if (_holds_source(conn, aid, prefix, me)
                and not srcs.claimed_by_another(conn, prefix, who, aid)
                and _holds_file_rows(conn, prefix, aid, src, lines, fitids)):
            return aid
    return None


def _earlier_split_account(conn, prefix: str, name: str, src: str,
                           lines) -> str | None:
    """The account a build before opaque ids made for this source, if one
    holds it:
      1. the id every earlier release derived (the plain slug), when it
         holds this source. A slug that mapped two sources onto one id
         stays that one account: moving one source off it here would mint
         a new id its rows were never hashed under, and the re-import
         would insert that source's whole history a second time;
      2. the account an adoption moved that id to (a re-key of an id that
         spelled a full number), when it holds this source; when the cut
         record of that move names several accounts, the one holding
         rows the file's `lines` (date, amount, name) recompute under the
         full id, else none;
      3. the readable id (split_account_id), or its taken form, when it
         holds this source; then the account an adoption moved either to;
      4. the id a build that spelled the digest in hex digits gave it.
    None when none does."""
    # rows hold the source NAME (OFX passes a key and keeps no source
    # cell, so it is matched on the account name, which is `name` too)
    me = _split_source(name)
    legacy = split_legacy_id(prefix, src)
    if _holds_source(conn, legacy, prefix, me):
        return legacy
    moved = _adopted_split_account(conn, [legacy], prefix, me, lines)
    if moved is not None:
        return moved
    aid = split_account_id(prefix, src)
    alt = split_account_id(prefix, src, taken=True)
    held = _holds_source(conn, aid, prefix, me)
    if held:
        return aid
    if held is False and _holds_source(conn, alt, prefix, me):
        return alt
    # the ids earlier builds minted for this source: a digest spelled in
    # hex, or a card number typed in groups left whole (its account moved
    # by a re-key when the id kept the number, else still where it was)
    older = [o for o in dict.fromkeys(
        split_account_id(prefix, src, taken=t, hex_digest=h,
                         grouped_whole=g)
        for g in (False, True) for t in (False, True) for h in (False, True))
        if o not in (aid, alt)]
    moved = _adopted_split_account(
        conn, [aid, alt] + [o for o in older if o != legacy], prefix, me)
    if moved is not None:
        return moved
    for hexed in older:
        if _holds_source(conn, hexed, prefix, me):
            return hexed
    return None


# The split importers whose ids were the slug of the source name, which
# kept a full account number whole when the account was named by it.
# OFX keys on the last four alone and never had one. Nothing the app mints
# matches: a masked slug holds no run of eight digits, and every digest an
# id carries is spelled in letters (letter_digest).
NUMBERED_SPLIT_ID_RE = re.compile(
    r"^manual:(?:qif|mint|monarch|copilot|simplifi)-.*\d{8,}")
# The split importers' own ids, numbered or not: rows hashed under one
# that spelled a number are keyed rather than re-hashed (see
# rekey_numbered_accounts).
SPLIT_ID_RE = re.compile(r"^manual:(?:qif|mint|monarch|copilot|simplifi)-")

# Every hand-made id that spells a full account number, as the selector of
# sync.card_number reads it: a run of eight digits, or a card layout (a
# slug turns "Visa 4111 1111 1111 1111" into "visa-4111-1111-1111-1111",
# which no eight-digit test sees). An id minted from a masked name never
# matches, and every digest an id carries is spelled in letters. In a
# pattern Python and PostgreSQL both read; it may over-select, and the
# re-key asks the lineage cut before moving anything. NUMBERED_SPLIT_ID_RE
# is the subset migration 199 moved.
NUMBERED_MANUAL_ID_PATTERN = f"^manual:[^:]*({card_number.sql_selector()})"
NUMBERED_MANUAL_ID_RE = re.compile(NUMBERED_MANUAL_ID_PATTERN)


def renumbered_manual_id(old: str) -> str:
    """The id a hand-made account whose id spells a full account number
    moves to: the one the bulk door minted, before ids were opaque, for
    the name the old id was slugged from (web/bulk_import._legacy_slug —
    the dashes read back as spaces, the name cut, slugged), so a later
    file naming the same account finds it by the slug walk. Cut to 40 like
    every split id, with a door's "-N" collision suffix put back after the
    cut (collision_suffix), as the door appends it to the cut slug. A split
    importer's id keeps renumbered_split_id."""
    return _renumbered_manual_id(old, keep_suffix=True)


def _renumbered_manual_id(old: str, *, keep_suffix: bool) -> str:
    body = old.split(":", 1)[1] if ":" in old else old
    body, suffix = collision_suffix(body) if keep_suffix else (body, "")
    body = card_number.cut_slug(body, width=40)
    return f"manual:{body or 'account'}{suffix}"


def renumbered_split_id(old: str) -> str:
    """The id migration 199 gives a split account whose id spells a full
    account number: each run cut to its last four, slugged and cut to 40
    as the id always was. Keep in step with the migration."""
    head, _, tail = old.partition("-")
    tail = re.sub(r"\d{4,}(\d{4})", r"-\1", tail)
    tail = re.sub(r"-+", "-", tail).strip("-")[:40] or "x"
    return f"{head}-{tail}"


# How many suffixes a re-key tries for one account whose masked id is
# taken before it leaves the account where it is.
RENUMBER_MAX_SUFFIXES = 32


def renumbered_split_suffix(cut: str, n: int) -> str:
    """The n-th suffix (from 1) a re-key tries for an account whose masked
    id is taken: a letter digest of its cut old id and the ordinal. The
    cut id alone is shared by every card with the same last four, so the
    ordinal is what keeps three or more of them apart; the number itself
    is never hashed. Keep in step with migration 199."""
    return letter_digest(hashlib.md5(
        f"{cut}#{n}".encode("utf-8")).hexdigest())


def _free_renumbered_id(conn, old: str) -> str | None:
    """The id a re-key moves `old` to: renumbered_split_id for a split
    importer's id (as migration 199 names it), else renumbered_manual_id;
    when that is taken, the first free suffix of renumbered_split_suffix.
    None when every suffix is taken."""
    base_id = (renumbered_split_id(old) if NUMBERED_SPLIT_ID_RE.search(old)
               else old)
    if NUMBERED_MANUAL_ID_RE.search(base_id):
        base_id = _renumbered_manual_id(
            base_id, keep_suffix=bool(door_suffix(conn, old)[1]))
    cut = recorded_lineage_id(conn, old)
    candidates = [base_id] + [
        f"{base_id}-{renumbered_split_suffix(cut, n)}"
        for n in range(1, RENUMBER_MAX_SUFFIXES + 1)]
    return next((c for c in candidates if not conn.execute(
        "SELECT 1 FROM accounts WHERE id=%s", (c,)).fetchone()), None)


def rekey_numbered_accounts(conn) -> dict[str, str]:
    """Move every hand-made account whose id spells a full account number
    (NUMBERED_MANUAL_ID_RE) onto an id that keeps only the last four, and
    cut the number from every adoption record that still names it in full
    (rekey_numbered_adoptions). `oikonome migrate` runs it for every
    household (migrate.key_numbered_file_rows) and restore runs it after
    an archive's rows land, so an archive written before the move does
    not bring the number back.

    A split importer's account (SPLIT_ID_RE) moves the way migration 199
    moves the ones already stored: its rows keep their ids, each an
    unkeyed hash over the old id, and key_file_import_ids — which every
    caller runs next — keys them; the split file names its source, so the
    importer recomputes the old id from it (split_former). Any other
    account's file never spells its id, so its rows are re-hashed under
    the new id first (rehash_file_rows), while the old one is still in
    hand: a re-import then mints exactly the ids they hold.

    Each move, the rows, the account and the settings that name it, and
    an adoption record (reconciled, so no reconnect guard arms) from the
    old id with its number cut (split_lineage_id), is one transaction.
    Accounts are taken in the order of the md5 of their old id, as the
    migration takes them, so both name the same ids; the md5 orders and
    is never stored, and ordering by the id itself would let the suffix
    tell which of two cards has the smaller number. Returns old -> new."""
    from .adopt import (_rewrite_account_id, _rewrite_account_id_in_config)
    moved: dict[str, str] = {}
    olds = sorted((r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id ~ %s",
        (NUMBERED_MANUAL_ID_PATTERN,)).fetchall()),
        key=lambda o: hashlib.md5(o.encode("utf-8")).hexdigest())
    for old in olds:
        if split_lineage_id(old) == old:
            continue            # nothing to cut: the pattern over-selected
        # the id is chosen INSIDE the held transaction, under the account
        # doors' per-household lock, so a concurrent add of the same last
        # four neither takes the id first nor aborts this move
        new = None
        # read before the move: whether a door's "-N" stands beside the
        # number depends on the account without it, which may move here
        lineage = recorded_lineage_id(conn, old)
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("oikonome:ensure-account:" + str(conn.execute(
                             "SELECT current_setting('app.tenant_id', true) AS t"
                         ).fetchone()["t"]),))
            new = _free_renumbered_id(conn, old)
            if new is None:
                continue
            if not _hold_account_for_move(conn, old):
                continue
            if not SPLIT_ID_RE.search(old):
                rehash_file_rows(conn, old, new)
            if not _rewrite_account_id(conn, old, new, "manual"):
                continue
            _rewrite_account_id_in_config(conn, old, new)
            # the names were copied across whole: an id cut of the number
            # beside a name still spelling it hides nothing
            _cut_account_names(conn, "id = %s", (new,))
            conn.execute(
                "INSERT INTO account_adoptions (account_id, old_account_id,"
                " item_id, reconciled_at) VALUES (%s, %s, 'manual', now())",
                (new, lineage))
        moved[old] = new
    rekey_numbered_adoptions(conn)
    return moved


# The name every caller before the hand-made ids were covered used.
rekey_numbered_split_accounts = rekey_numbered_accounts

# The name columns of an account a person or a file typed, which may spell
# a card number in groups; and a stored name that still does, in the
# selector sync.card_number builds from its grammar for PostgreSQL. It is
# wider than the cut, which decides exactly: a name the cut leaves
# unchanged is not written.
ACCOUNT_NAME_COLUMNS = ("name", "display_name", "official_name")
NUMBERED_NAME_PATTERN = card_number.sql_selector()

# The accounts whose stored names may still spell a number: any account's
# rename, a hand-made account's name and institution name. Each column is
# read the way the name rows read it (account_sources.FOLDED_SQL), so a
# number spaced by underscores, commas or en dashes is selected as one,
# never left whole while its name row waits for a cut that never comes.
# Run with {"p": NUMBERED_NAME_PATTERN}.
NUMBERED_NAMES_WHERE = (
    "(item_id = 'manual' AND ({name} ~ %(p)s OR {official} ~ %(p)s))"
    " OR {display} ~ %(p)s").format(
        **{k: account_sources.FOLDED_SQL.format(expr=c) for k, c in (
            ("name", "name"), ("official", "official_name"),
            ("display", "display_name"))})


def _cut_account_names(conn, where: str, args) -> int:
    """Cut every account name column (ACCOUNT_NAME_COLUMNS) of the
    accounts `where` selects the way the account doors store a name
    (account_sources.stored_account_name: cut, and cut as the slug reads
    it when the slug still spells a number), writing only what changes.
    Returns the accounts written.

    Each write is compare-and-set on the values it read: the caller's
    connection may be in autocommit, and a rename committed between the
    read and the write must not be overwritten with the old name's cut.
    A lost race re-reads the row and cuts what it holds now."""
    cols = ", ".join(ACCOUNT_NAME_COLUMNS)
    done = 0
    for r in conn.execute(
            f"SELECT id, {cols} FROM accounts WHERE {where}",
            args).fetchall():
        for _ in range(8):
            cut = {c: account_sources.stored_account_name(r[c])
                   for c in ACCOUNT_NAME_COLUMNS}
            cut = {c: v for c, v in cut.items() if v != r[c]}
            if not cut:
                break
            if conn.execute(
                    "UPDATE accounts SET "
                    + ", ".join(f"{c} = %s" for c in cut)
                    + " WHERE id = %s AND "
                    + " AND ".join(f"{c} IS NOT DISTINCT FROM %s"
                                   for c in cut),
                    [*cut.values(), r["id"], *(r[c] for c in cut)]
            ).rowcount:
                done += 1
                break
            r = conn.execute(
                f"SELECT id, {cols} FROM accounts WHERE id = %s",
                (r["id"],)).fetchone()
            if r is None:
                break
    if done:
        # a cut name's c-row is written, and the one over the name it had
        # (a digest of the whole number) dropped
        account_sources.backfill(conn)
    return done


def mask_manual_account_names(conn) -> int:
    """Cut a full number from every hand-made account's stored names —
    its name, the name a person renamed it to and the institution's name
    — typed in groups or not, or spaced by separators only its slug
    reads (NUMBERED_NAMES_WHERE, _cut_account_names), which a plain
    run match on `name` alone misses. Runs from `oikonome migrate`
    (migrate.key_numbered_file_rows) for every household holding one;
    idempotent, since a cut name holds no run to cut. Returns the
    accounts written."""
    return _cut_account_names(conn, NUMBERED_NAMES_WHERE,
                              {"p": NUMBERED_NAME_PATTERN})


def _live_end(conn, account_id: str) -> str | None:
    """`account_id` if it is an account, else the account later moves
    took it to (the first in id order where a move branched); None when
    the line ends nowhere."""
    seen = {account_id}
    cur = account_id
    for _ in range(32):
        if conn.execute("SELECT 1 FROM accounts WHERE id = %s",
                        (cur,)).fetchone():
            return cur
        nxt = sorted({r["account_id"] for r in conn.execute(
            "SELECT account_id FROM account_adoptions"
            " WHERE old_account_id = %s", (cur,)).fetchall()} - seen)
        if not nxt:
            return None
        cur = nxt[0]
        seen.add(cur)
    return None


def cut_lineage_targets(conn, full_id: str) -> list[str]:
    """The live accounts an id spelling a full number now lives on, when
    a re-key moved it (rekey_numbered_accounts) or an adoption record
    naming it was cut (rekey_numbered_adoptions): the targets of the
    record under its cut form (split_lineage_id) — and under the full
    form, on an instance not yet re-keyed — each followed through later
    moves to an account that exists, sorted. Two cards sharing their
    last four leave one cut key pointing at both, so more than one answer
    means the id alone cannot tell which account is meant; none means
    nothing moved it."""
    starts = sorted({full_id, *lineage_keys(full_id)})
    firsts = {r["account_id"] for r in conn.execute(
        "SELECT account_id FROM account_adoptions"
        " WHERE old_account_id = ANY(%s)", (starts,)).fetchall()}
    return sorted({e for e in (_live_end(conn, a) for a in firsts)
                   if e is not None})


# How far cut_lineage_candidates walks a full id's "-N" suffixes.
CUT_LINEAGE_MAX_SUFFIX = 32


def _has_lineage_record(conn, full_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM account_adoptions WHERE old_account_id = ANY(%s)"
        " LIMIT 1",
        (sorted({full_id, *lineage_keys(full_id)}),)).fetchone() is not None


def cut_lineage_candidates(conn, full_id: str, *,
                           max_suffix: int = CUT_LINEAGE_MAX_SUFFIX
                           ) -> list[tuple[str, list[str]]]:
    """Every id a door could have given a name spelling the full number
    `full_id` stands for, with the live accounts each now lives on
    (cut_lineage_targets): `full_id` itself, then `full_id`-2, -3, … — the
    suffixes the account doors append when the slug is taken — while a
    move recorded one (under its full or cut form), up to `max_suffix`. Only
    ids leading somewhere are listed, bare id first, so a door asks which
    of several accounts a typed number means exactly as it walked the
    suffixes before the re-key (bulk_import._slug_walk). Each
    suffixed id is cut with its suffix kept (split_lineage_id), so "-2"
    leads to the "-2" account only."""
    out: list[tuple[str, list[str]]] = []
    for n in range(1, max_suffix + 1):
        cand = full_id if n == 1 else f"{full_id}-{n}"
        if n > 1 and not _has_lineage_record(conn, cand):
            break
        targets = cut_lineage_targets(conn, cand)
        if targets:
            out.append((cand, targets))
    return out


def _hold_account_for_move(conn, account_id: str) -> bool:
    """Lock `account_id`'s row for the rest of the caller's transaction,
    after the pairing lock (reanchor.hold_pairing_lock, the order every
    carry keeps: pairing lock, then rows); False when it is gone.

    A re-key reads the account's rows and re-hashes them before it moves
    the account. Held first, the row lock makes any writer adding a row
    to the account — a hand-entered row, a door that does not take the
    household's sync lock — wait for the move (its foreign-key check
    needs a share of the row) and then find the account gone, instead of
    landing a row in between that the move carries across unproved."""
    from .reanchor import hold_pairing_lock
    hold_pairing_lock(conn)
    return conn.execute(
        "SELECT 1 FROM accounts WHERE id = %s FOR UPDATE",
        (account_id,)).fetchone() is not None


def rekey_numbered_adoptions(conn) -> int:
    """Cut the full number from every adoption record that names the id an
    account moved off in full (one written before re-keys cut the number:
    a numbered hand-made account later adopted by a reconnect). The
    record rides /export and the portability archive, and a re-import
    recomputes the rows' ids from it, so the rows hashed under it are
    first re-hashed under the account
    they live on now (rehash_file_rows), which is the id a re-import there
    mints, and only then is the record cut. A split importer's record is
    only cut: its rows are keyed (key_file_import_ids) and its file spells
    the full id again. A cut record that already stands beside it (an
    older archive restored the full one again) makes the full one a
    duplicate, removed once reconciled. Returns the records cut."""
    rows = conn.execute(
        "SELECT account_id, old_account_id, adopted_at, reconciled_at"
        "  FROM account_adoptions WHERE old_account_id ~ %s"
        " ORDER BY adopted_at",
        (NUMBERED_MANUAL_ID_PATTERN,)).fetchall()
    done = 0
    for r in rows:
        full = r["old_account_id"]
        if conn.execute("SELECT 1 FROM accounts WHERE id = %s",
                        (full,)).fetchone():
            continue        # an account again: rekey_numbered_accounts'
        cut = recorded_lineage_id(conn, full)
        if cut == full:
            continue            # nothing to cut: the pattern over-selected
        with conn.transaction():
            if not SPLIT_ID_RE.search(full):
                live = _live_end(conn, r["account_id"])
                if live is not None and _hold_account_for_move(conn, live):
                    rehash_file_rows(conn, full, live, on=live)
            twin = r["reconciled_at"] is not None and conn.execute(
                "SELECT 1 FROM account_adoptions WHERE account_id = %s"
                "   AND old_account_id = %s", (r["account_id"], cut)
            ).fetchone()
            if twin:
                conn.execute(
                    "DELETE FROM account_adoptions WHERE account_id = %s"
                    "   AND adopted_at = %s AND old_account_id = %s",
                    (r["account_id"], r["adopted_at"], full))
            else:
                conn.execute(
                    "UPDATE account_adoptions SET old_account_id = %s"
                    " WHERE account_id = %s AND adopted_at = %s"
                    "   AND old_account_id = %s",
                    (cut, r["account_id"], r["adopted_at"], full))
        done += 1
    return done


# The most identical lines (one importer, account, date, amount and
# name) a proof of a row's id walks. A real statement repeats a line a
# handful of times — two coffees, a run of parking meters — and every
# proof hashes each occurrence up to the count, so an uncapped count is
# the lever a crafted archive of one line repeated pulls to keep the web
# process hashing for hours. A row past it is not proved: rehash_file_rows
# keys it and counts it, as it does a row edited since.
FILE_ROW_MAX_OCCURRENCES = 64


def _line_parts(date, amount, name):
    """(iso date, cents without sign, masked name, stored name), lower-
    cased, as a file row's hash inputs read them; None when the date or
    amount cannot be read."""
    try:
        cents = abs(round(float(amount) * 100))
    except (TypeError, ValueError, OverflowError):
        return None
    if date is None:
        return None
    if not isinstance(date, dt.date):
        try:
            date = dt.date.fromisoformat(str(date)[:10])
        except ValueError:
            return None
    return (date.isoformat(), cents, mask_long_digits(name or "").lower(),
            (name or "").lower())


def _name_tails(iso: str, cents: int, name: str, occurrences: int):
    """The hash inputs after the account id for one name of a line: at
    either sign, at each occurrence up to a little past `occurrences`
    (rows deleted since may have numbered it higher), clamped to
    FILE_ROW_MAX_OCCURRENCES."""
    top = min(max(occurrences, 1), FILE_ROW_MAX_OCCURRENCES)
    for c in dict.fromkeys((cents, -cents)):
        for n in range(1, top + 3):
            yield f"|{iso}|{c}|{name}|{n}"


def file_row_shape(tid: str, date, amount, name) -> tuple:
    """What identical lines of one importer on one account share, for
    numbering them: the prefix, the date, the cents without their sign
    and the masked name."""
    try:
        cents = abs(round(float(amount) * 100))
    except (TypeError, ValueError, OverflowError):
        cents = None
    return (tid.split(":", 1)[0], str(date)[:10], cents,
            mask_long_digits(name or "").lower())


def _sha1_id(prefix: str, text: str) -> str:
    return f"{prefix}:" + hashlib.sha1(text.encode()).hexdigest()


def rehashed_file_ids(old_aid: str, new_aid: str, rows,
                      secret: bytes | None = None) -> dict[str, str]:
    """rehashed_file_id for many rows of one move: `rows` holds
    (tid, date, amount, name, occurrences) per row; returns tid -> the id
    it takes, for each row proved (an id already hashed under `new_aid`
    maps to itself).

    The candidate hashes of one name of one line are the same for every
    row of that line, so they are hashed once and each row looked up:
    rows sorted by line, only the current line's candidates held. A row
    whose id is not one an importer writes (its prefix, a 40-hex digest,
    or the keyed form when `secret` is given) is skipped before anything
    is hashed. With the occurrence clamp (FILE_ROW_MAX_OCCURRENCES) the
    work is a bounded number of hashes per row, however many identical
    lines arrive."""
    out: dict[str, str] = {}
    todo = []
    for tid, date, amount, name, occ in rows:
        tid = str(tid)
        m = _NUMBERED_OFX_ID_RE.match(tid)
        if m and m.group(1) == old_aid:
            out[tid] = f"ofx:{new_aid}:{m.group(2)}"
            continue
        prefix, _, digest = tid.partition(":")
        if prefix + ":" not in FILE_IMPORT_ID_PREFIXES:
            continue
        keyed = (len(digest) == 41 and digest.startswith(KEYED_ID_MARK))
        if keyed and secret is None:
            continue
        if not keyed and len(digest) != 40:
            continue
        parts = _line_parts(date, amount, name)
        if parts is None:
            continue
        iso, cents, masked, stored = parts
        try:
            k = min(max(int(occ), 1), FILE_ROW_MAX_OCCURRENCES)
        except (TypeError, ValueError):
            k = 1
        todo.append(((prefix, iso, cents, k, masked), stored, tid, keyed))
    todo.sort(key=lambda x: (x[0], x[1]))
    cur = None
    held: dict[str, list] = {}

    def _maps(head, nm, keyed):
        maps = held.get(nm)
        if maps is None:
            prefix, iso, cents, k, _ = head
            plain = {_sha1_id(prefix, f"{old_aid}{t}"): t
                     for t in _name_tails(iso, cents, nm, k)}
            maps = held[nm] = [plain, None]
        if keyed and maps[1] is None:
            maps[1] = {keyed_file_id(secret, p): t
                       for p, t in maps[0].items()}
        return maps[1] if keyed else maps[0]

    for head, stored, tid, keyed in todo:
        if head != cur:
            cur, held = head, {}
        masked = head[4]
        # the masked name's candidates serve the whole line; a stored
        # name that differs is kept only while rows of it run
        for nm in list(held):
            if nm not in (masked, stored):
                del held[nm]
        tail = None
        for nm in dict.fromkeys((masked, stored)):
            tail = _maps(head, nm, keyed).get(tid)
            if tail is not None:
                break
        if tail is not None:
            out[tid] = _sha1_id(head[0], f"{new_aid}{tail}")
    return out


def rehashed_file_id(tid: str, old_aid: str, new_aid: str, date, amount,
                     name, occurrences: int,
                     secret: bytes | None = None) -> str | None:
    """The id the file row `tid` takes when the account it was hashed
    under moves from `old_aid` to `new_aid`: the same line hashed under
    the new id (what FileImportIds mints for it there), or an OFX row
    keyed by the bank's FITID renamed onto the new id. Exact: a row is
    re-hashed only when one of its possible hash inputs (its masked name
    and, while it differs, its stored name, each at either sign and every
    occurrence _name_tails walks, from its own stored date, amount and
    name) hashed
    under `old_aid` gives `tid`, or its keyed form under `secret` does —
    a sha1 cannot match by chance. None when nothing proves it. For more
    than one row, rehashed_file_ids hashes each line once."""
    return rehashed_file_ids(
        old_aid, new_aid, [(tid, date, amount, name, occurrences)],
        secret).get(tid)


def rehash_file_rows(conn, old_aid: str, new_aid: str, *,
                     on: str | None = None) -> tuple[int, int, int]:
    """Re-hash the file rows on account `on` (default `old_aid`) whose id
    was hashed under the account id `old_aid` so it is hashed under
    `new_aid`: the id a file import into `new_aid` mints for the same
    line, so a re-import there finds every row by its id alone — no former
    id, no record lookup, no guess budget. Run while `old_aid` is still
    in hand (rekey_numbered_accounts, rekey_numbered_adoptions), because
    afterwards nothing spells it.

    Each move is proved (rehashed_file_id). A row of this importer family
    that no input proves under `old_aid` — an amount or date edited since,
    a name stored cut — is re-id'd under the household's secret instead
    (keyed_file_id) and counted, since its id may still hash the number;
    one already keyed stays as it is. With `on` given the account holds
    rows from other lines too, so a row whose id its own account explains
    is left alone, as is one already hashed under `new_aid` (a pass run
    again). A row whose new id is already held is a copy that
    existed before: left where it is, and logged. Every reference to a
    moved row follows it (adopt.rewrite_txn_ids, carry_txn_mentions).
    Returns (moved, keyed, left)."""
    on = on or old_aid
    ofx_prefix = f"ofx:{old_aid}:"
    rows = conn.execute(
        "SELECT id, date, amount, name FROM transactions"
        " WHERE account_id = %s"
        "   AND (id ~ %s OR id ~ %s OR left(id, %s) = %s)"
        " ORDER BY id",
        (on, UNKEYED_FILE_ID_PATTERN, KEYED_FILE_ID_PATTERN,
         len(ofx_prefix), ofx_prefix)).fetchall()
    if not rows:
        return (0, 0, 0)
    same: dict[tuple, int] = defaultdict(int)
    for r in rows:
        same[file_row_shape(r["id"], r["date"], r["amount"], r["name"])] += 1
    if max(same.values()) > FILE_ROW_MAX_OCCURRENCES:
        log.warning("a re-keyed account holds more than %d identical "
                    "lines; the ones past that are keyed, not proved",
                    FILE_ROW_MAX_OCCURRENCES)
    secret = txn_id_secret(conn)
    keyed_re = re.compile(KEYED_FILE_ID_PATTERN)
    pairs: list[tuple[str, str]] = []
    keyed: list[tuple[str, str]] = []
    lines = [(r["id"], r["date"], r["amount"], r["name"],
              same[file_row_shape(r["id"], r["date"], r["amount"],
                                  r["name"])]) for r in rows]
    proved = rehashed_file_ids(old_aid, new_aid, lines, secret)
    rest = [x for x in lines if x[0] not in proved
            and not keyed_re.match(x[0])]
    # hashed under the new id already, or under the account's own
    own: set[str] = set()
    for a in dict.fromkeys((new_aid, on)):
        if a != old_aid and rest:
            own |= {t for t, n in rehashed_file_ids(a, a, rest).items()
                    if n == t}
    for tid, *_ in lines:
        new = proved.get(tid)
        if new is not None:
            if new != tid:
                pairs.append((tid, new))
            continue
        if keyed_re.match(tid):
            continue                     # keyed already
        if tid in own:
            continue
        if secret is None:
            secret = txn_id_secret(conn, create=True)
        keyed.append((tid, keyed_file_id(secret, tid)))
    from .adopt import rewrite_txn_ids
    done = rewrite_txn_ids(conn, pairs + keyed)
    ok = [p for p, d in zip(pairs + keyed, done) if d]
    n_moved = sum(done[:len(pairs)])
    n_keyed = sum(done[len(pairs):])
    left = len(done) - len(ok)
    if n_keyed:
        log.warning("%d file row(s) on a re-keyed account could not be "
                    "proved hashed under its old id and were keyed "
                    "instead; a re-import may add those lines again", n_keyed)
    if left:
        log.warning("%d file row(s) on a re-keyed account were left as "
                    "they are: the id they move to is already held", left)
    carry_txn_mentions(conn, ok)
    return (n_moved, n_keyed, left)


# Every file row whose id may hash a full account number. One kind: the
# rows of an importer on each account reached from an id that spelled one
# (cut by a re-key, or in full on an older adoption), following later
# moves, whose id is an unkeyed file-row id or an OFX id naming such an
# account (`numbered`). The other: an unkeyed file-row id on any account
# whose name carries the mark of a cut run of digits, which may have been
# hashed while the name still held the whole run; key_file_import_ids
# leaves the ones whose id the masked name explains. Written over
# tenant_id so `oikonome migrate` can ask it of every household at once;
# on a household's own connection RLS narrows it.
NUMBERED_FILE_ROWS_SQL = """
    WITH RECURSIVE reach (tenant_id, account_id, hops) AS (
        SELECT tenant_id, account_id, 0 FROM account_adoptions
         WHERE old_account_id ~ %(lineage)s
        UNION
        SELECT a.tenant_id, a.account_id, r.hops + 1
          FROM account_adoptions a
          JOIN reach r ON a.tenant_id = r.tenant_id
                      AND a.old_account_id = r.account_id
         WHERE r.hops < 32),
    reached AS (SELECT DISTINCT tenant_id, account_id FROM reach)
    SELECT t.tenant_id, t.id, t.account_id, t.date, t.amount, t.name,
           true AS numbered
      FROM transactions t
      JOIN reached r
        ON t.tenant_id = r.tenant_id AND t.account_id = r.account_id
     WHERE t.id ~ %(unkeyed)s OR t.id ~ %(ofx)s
    UNION ALL
    SELECT t.tenant_id, t.id, t.account_id, t.date, t.amount, t.name,
           false
      FROM transactions t
     -- the name first: the id pattern costs a scan of every row
     WHERE t.name ~ %(cut)s AND t.id ~ %(unkeyed)s
       AND NOT EXISTS (SELECT 1 FROM reached r
                        WHERE r.tenant_id = t.tenant_id
                          AND r.account_id = t.account_id)"""
NUMBERED_FILE_ROWS_ARGS = {"lineage": NUMBERED_LINEAGE_PATTERN,
                           "unkeyed": UNKEYED_FILE_ID_PATTERN,
                           "ofx": NUMBERED_OFX_ID_PATTERN,
                           "cut": MASKED_RUN_PATTERN}


def _explained_by_own_account(rows, occurrences) -> set[str]:
    """The ids of `rows` (file rows, any accounts) that _hashed_over_masked_name
    holds for, `occurrences(r)` giving each row's count of identical
    lines: one rehashed_file_ids pass per account, so each line is
    hashed once."""
    by_aid: dict[str, list] = defaultdict(list)
    for r in rows:
        by_aid[r["account_id"] or ""].append(
            (r["id"], r["date"], r["amount"], r["name"], occurrences(r)))
    out: set[str] = set()
    for aid, lines in by_aid.items():
        out |= {t for t, n in rehashed_file_ids(aid, aid, lines).items()
                if n == t}
    return out


def _hashed_over_masked_name(r, occurrences: int) -> bool:
    """True when the file row `r`'s id is the one FileImportIds gives its
    stored name on the account it is on now (masked, or as stored while
    that still differs), at either sign and any occurrence up to a little
    past `occurrences`: a row imported after names were masked, or one
    re-hashed onto its account when the account moved off a numbered id
    (rehash_file_rows), whose id never held the number. A sha1 cannot
    match by chance, so a False is only ever a row keyed that needed no
    keying, never the reverse."""
    aid = r["account_id"] or ""
    return rehashed_file_id(r["id"], aid, aid, r["date"], r["amount"],
                            r["name"], occurrences) == r["id"]


def key_file_import_ids(conn) -> int:
    """Re-id every file row whose id may hash a full account number
    (NUMBERED_FILE_ROWS_SQL) onto its keyed form (keyed_file_id), under
    the household's secret, minted here when there is work and none yet.
    A row whose id its own account explains is left
    (_hashed_over_masked_name): one the SQL finds only by the mark in its
    name and hashed masked, and every row a hand-made account's re-key
    re-hashed (rehash_file_rows), so what this keys on a once-numbered
    lineage is what no re-hash could reach — a split account moved off
    its numbered id without its rows, whose full id is no longer recorded.
    An OFX row keyed
    by the bank's FITID moves instead onto the id an OFX import gives the
    line on the account it is on now. Each move carries every reference
    to the row (adopt._rewrite_txn_id), and then every record that names
    it by id with no foreign key to follow (carry_txn_mentions), with any
    such record an earlier move left behind (carry_stale_mentions). A row
    whose new id is already held is left where it is and logged: that id
    is the same line, already here. Idempotent: a keyed id is never
    matched again. Runs from `oikonome migrate`
    (migrate.key_numbered_file_rows) and from restore after
    rekey_numbered_accounts. Returns the rows moved."""
    rows = conn.execute(
        NUMBERED_FILE_ROWS_SQL + " ORDER BY id",
        NUMBERED_FILE_ROWS_ARGS).fetchall()
    # identical lines on one account share a base and are numbered
    # 1..n among themselves
    same: dict[tuple, int] = defaultdict(int)

    def _shape(r):
        try:
            cents = abs(round(float(r["amount"]) * 100))
        except (TypeError, ValueError):
            cents = None
        return (r["account_id"], r["date"], cents,
                mask_long_digits(r["name"] or "").lower())

    for r in rows:
        same[_shape(r)] += 1
    from .adopt import rewrite_txn_ids
    secret = None
    left = 0
    pairs: list[tuple[str, str]] = []
    # a row its own account explains never held the number — on a
    # once-numbered account too, where a re-key re-hashed it or an import
    # after the move minted it (_hashed_over_masked_name, a line at a time)
    explained = _explained_by_own_account(
        [r for r in rows if _UNKEYED_FILE_ID_RE.match(r["id"])],
        lambda r: same[_shape(r)])
    for r in rows:
        old = r["id"]
        if old in explained:
            continue
        m = _NUMBERED_OFX_ID_RE.match(old)
        if m and LONG_DIGITS_RE.search(m.group(1)):
            if LONG_DIGITS_RE.search(r["account_id"] or ""):
                left += 1          # its account is not moved yet
                continue
            new = f"ofx:{r['account_id']}:{m.group(2)}"
        else:
            if secret is None:
                secret = txn_id_secret(conn, create=True)
            new = keyed_file_id(secret, old)
        if new != old:
            pairs.append((old, new))
        else:
            left += 1
    # one set-based carry per slice, not one ledger scan per row: a large
    # household moves tens of thousands of rows while migrate holds the
    # startup lock
    done = rewrite_txn_ids(conn, pairs)
    moved = [p for p, ok in zip(pairs, done) if ok]
    left += len(done) - len(moved)
    if left:
        log.warning("%d file row(s) whose id may hash a full account "
                    "number were left as they are: the new id is already "
                    "held, or the account has not moved off its own", left)
    carry_txn_mentions(conn, moved)
    carry_stale_mentions(conn)
    return len(moved)


# Every id a record names a transaction by with no foreign key to carry
# it. In the activity log, the target of a one-row act and the rows a
# bulk act lists in its detail: kept for years, beside a sentence giving
# the row's date, amount and name. In an adoption's guard ledger, the
# restored row each feed row was dropped as a copy of: the reconnect
# guard reads it back to know which rows are spoken for. In a feed row's
# raw, the row an earlier guard noted it was carried from. All three ride
# /export and the portability archive, so an id left in any of them after
# the row was re-id'd keeps the exposure the re-id closed. (The other
# unforeign mentions — a receipt's refused charges, the pending pointer —
# move with the row in adopt._rewrite_txn_id; the guard ledger's other
# lists name feed rows, which are never re-id'd.) A merchant-wide undo is
# a week of scratch that never leaves the instance; it moves with its rows
# (carry_undo_payloads) and is searched here too, so one left on a dead
# id by a move that died before the carry is still found.
_MENTIONS_SQL = """
    SELECT a.tenant_id, n.v FROM activity_log a
     CROSS JOIN LATERAL (
        SELECT a.target AS v
        UNION ALL
        SELECT e FROM jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(a.detail->'ids') = 'array'
                 THEN a.detail->'ids' ELSE '[]'::jsonb END) AS e) n
    UNION ALL
    SELECT x.tenant_id, g.value FROM account_adoptions x
     CROSS JOIN LATERAL jsonb_each_text(
        CASE WHEN jsonb_typeof(x.guard_ledger->'absorbed') = 'object'
             THEN x.guard_ledger->'absorbed' ELSE '{}'::jsonb END) AS g
    UNION ALL
    SELECT t.tenant_id, t.raw->>'_carried_from' FROM transactions t
     WHERE t.raw ? '_carried_from'
    UNION ALL
    SELECT u.tenant_id, e.v->>'id' FROM category_undo u
     CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(u.payload->'affected') = 'array'
             THEN u.payload->'affected' ELSE '[]'::jsonb END) AS e(v)
     WHERE jsonb_typeof(e.v) = 'object' AND e.v ? 'id'"""
_MENTIONED_IDS_SQL = (
    "SELECT DISTINCT m.v FROM (" + _MENTIONS_SQL + ") m"
    " WHERE m.v ~ %(unkeyed)s OR m.v ~ %(ofx)s")
# The households a stale mention may stand in, over all of them at once
# for `oikonome migrate`: an unkeyed file-row id the household no longer
# holds where it keeps a secret (a keyed row may stand for it), or an OFX
# id spelling a full number it no longer holds on an account the
# household moved off that number (the account it moved to tells where
# the line is now; on one never moved, no row was moved off the id
# either). A mention of a row deleted since matches too: an unkeyed one's
# visit changes nothing, and an OFX one is carried to the id the line has
# on the moved account all the same, so it never matches again. The
# account the OFX id names is compared in full and cut (split_lineage_id
# in SQL), as an adoption records it either way.
STALE_MENTION_TENANTS_SQL = (
    "SELECT DISTINCT m.tenant_id FROM (" + _MENTIONS_SQL + ") m"
    """
     WHERE ((m.v ~ %(unkeyed)s
             AND EXISTS (SELECT 1 FROM txn_id_keys k
                          WHERE k.tenant_id = m.tenant_id))
            OR (m.v ~ %(ofx)s
                AND EXISTS (
                    SELECT 1 FROM account_adoptions x
                     WHERE x.tenant_id = m.tenant_id
                       AND x.old_account_id IN (
                           substring(m.v from %(ofx_account)s),
                           regexp_replace(
                               substring(m.v from %(ofx_account)s),
                               %(long_run)s, %(long_cut)s, 'g')))))
       AND NOT EXISTS (SELECT 1 FROM transactions t
                        WHERE t.tenant_id = m.tenant_id AND t.id = m.v)""")
MENTION_ARGS = {"unkeyed": UNKEYED_FILE_ID_PATTERN,
                "ofx": NUMBERED_OFX_ID_PATTERN,
                "ofx_account": r"^ofx:(manual:[^:]*):",
                # mask_long_digits: a run of 8+ digits keeps its last four
                "long_run": "[0-9]{4,}([0-9]{4})",
                "long_cut": "…\\1"}


def carry_txn_mentions(conn, pairs) -> None:
    """Point every record that names a transaction by id with no foreign
    key (see _MENTIONS_SQL) from each old id of `pairs` to its new one. A
    sibling of the carry adopt._rewrite_txn_id does for the rows that do
    have one; a bulk act's list keeps its order and every other entry it
    holds, and a guard ledger every feed row it maps."""
    pairs = [(o, n) for o, n in pairs if o and n and o != n]
    if not pairs:
        return
    olds = [o for o, _ in pairs]
    news = [n for _, n in pairs]
    conn.execute(
        "UPDATE activity_log a SET target = m.new_id"
        "  FROM unnest(%s::text[], %s::text[]) AS m(old_id, new_id)"
        " WHERE a.target = m.old_id", (olds, news))
    conn.execute(
        """UPDATE activity_log a
              SET detail = jsonb_set(a.detail, '{ids}', (
                  SELECT jsonb_agg(COALESCE(to_jsonb(m.new_id), e.v)
                                   ORDER BY e.i)
                    FROM jsonb_array_elements(
                         CASE WHEN jsonb_typeof(a.detail->'ids') = 'array'
                              THEN a.detail->'ids' ELSE '[]'::jsonb END)
                         WITH ORDINALITY AS e(v, i)
                    LEFT JOIN unnest(%s::text[], %s::text[])
                         AS m(old_id, new_id)
                      ON e.v = to_jsonb(m.old_id)))
            WHERE jsonb_typeof(a.detail->'ids') = 'array'
              AND a.detail->'ids' ?| %s::text[]""", (olds, news, olds))
    conn.execute(
        """UPDATE account_adoptions x
              SET guard_ledger = jsonb_set(x.guard_ledger, '{absorbed}', (
                  SELECT jsonb_object_agg(
                             e.key, COALESCE(to_jsonb(m.new_id), e.value))
                    FROM jsonb_each(x.guard_ledger->'absorbed') AS e
                    LEFT JOIN unnest(%s::text[], %s::text[])
                         AS m(old_id, new_id)
                      ON e.value = to_jsonb(m.old_id)))
            WHERE jsonb_typeof(x.guard_ledger->'absorbed') = 'object'
              AND EXISTS (
                  SELECT 1 FROM jsonb_each_text(
                         x.guard_ledger->'absorbed') AS g
                   WHERE g.value = ANY(%s::text[]))""", (olds, news, olds))
    conn.execute(
        "UPDATE transactions t SET raw = jsonb_set(t.raw, '{_carried_from}',"
        "                                          to_jsonb(m.new_id))"
        "  FROM unnest(%s::text[], %s::text[]) AS m(old_id, new_id)"
        " WHERE t.raw ? '_carried_from'"
        "   AND t.raw->>'_carried_from' = m.old_id", (olds, news))
    carry_undo_payloads(conn, olds, news)


def carry_undo_payloads(conn, olds: list[str], news: list[str]) -> int:
    """Rewrite a merchant-wide write's held undo (category_undo) from each
    old id onto its new one. The undo restores each row it moved by id,
    for a week; left on the old id it would skip the row and still answer
    that it undid the write. Each entry keeps its order and every other
    key it holds. adopt._rewrite_txn_slice runs it inside the transaction
    that moves the rows, so no undo claimed between a slice and a later
    carry finds its rows gone. Returns the undos rewritten."""
    if not olds:
        return 0
    return conn.execute(
        """UPDATE category_undo u
              SET payload = jsonb_set(u.payload, '{affected}', (
                  SELECT jsonb_agg(
                             CASE WHEN m.new_id IS NULL THEN e.v
                                  ELSE jsonb_set(e.v, '{id}',
                                                 to_jsonb(m.new_id)) END
                             ORDER BY e.i)
                    FROM jsonb_array_elements(
                             CASE WHEN jsonb_typeof(u.payload->'affected')
                                       = 'array'
                                  THEN u.payload->'affected'
                                  ELSE '[]'::jsonb END)
                         WITH ORDINALITY AS e(v, i)
                    LEFT JOIN unnest(%s::text[], %s::text[])
                         AS m(old_id, new_id)
                      ON jsonb_typeof(e.v) = 'object'
                     AND e.v->>'id' = m.old_id))
            WHERE jsonb_typeof(u.payload->'affected') = 'array'
              AND jsonb_array_length(
                      CASE WHEN jsonb_typeof(u.payload->'affected') = 'array'
                           THEN u.payload->'affected'
                           ELSE '[]'::jsonb END) > 0
              AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements(
                             CASE WHEN jsonb_typeof(u.payload->'affected')
                                       = 'array'
                                  THEN u.payload->'affected'
                                  ELSE '[]'::jsonb END) AS a(v)
                   WHERE jsonb_typeof(a.v) = 'object'
                     AND a.v->>'id' = ANY(%s::text[]))""",
        (olds, news, olds)).rowcount


def _moved_on(conn, account_id: str) -> str | None:
    """The account the numbered id `account_id` (in full; an adoption may
    record it cut) moved to, followed through every later move: the last
    one reached, the first in id order where a move branched. None when
    nothing moved off it."""
    at = sorted({account_id, *lineage_keys(account_id)})
    seen = set(at)
    last = None
    for _ in range(32):
        nxt = sorted({r["account_id"] for r in conn.execute(
            "SELECT account_id FROM account_adoptions"
            " WHERE old_account_id = ANY(%s)", (at,)).fetchall()} - seen)
        if not nxt:
            break
        last = nxt[0]
        seen.update(nxt)
        at = [last]
    return last


def carry_stale_mentions(conn) -> int:
    """Carry the mentions an earlier move left on an id the household no
    longer holds: an unkeyed file-row id whose keyed form it holds, and an
    OFX id spelling a full number, onto the id an OFX import gives that
    FITID on the account the number moved to (the one key_file_import_ids
    moved its row to) — held or not, and never a row of another account
    repeating the FITID, as banks do. A mention of an unkeyed id whose row
    is simply gone, or of an OFX id on an account nothing moved off, is
    left as it is: the row may come back under that plain id (a re-import
    on an account with no number), and a mention rewritten to a keyed form
    nothing will ever hold is lost for good. Returns the ids carried."""
    named = {r["v"] for r in conn.execute(
        _MENTIONED_IDS_SQL, MENTION_ARGS).fetchall()}
    if not named:
        return 0
    named -= _held_ids(conn, named)
    pairs: dict[str, str] = {}
    unkeyed = [i for i in named if _UNKEYED_FILE_ID_RE.match(i)]
    secret = txn_id_secret(conn) if unkeyed else None
    if secret is not None:
        keyed = {keyed_file_id(secret, i): i for i in unkeyed}
        for k in _held_ids(conn, keyed):
            pairs[keyed[k]] = k
    moved_to: dict[str, str | None] = {}
    for i in sorted(named):
        m = _NUMBERED_OFX_ID_RE.match(i)
        if not (m and LONG_DIGITS_RE.search(m.group(1))):
            continue
        if m.group(1) not in moved_to:
            moved_to[m.group(1)] = _moved_on(conn, m.group(1))
        if moved_to[m.group(1)] is not None:
            pairs[i] = f"ofx:{moved_to[m.group(1)]}:{m.group(2)}"
    carry_txn_mentions(conn, pairs.items())
    return len(pairs)


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
# A match may start only where a word run begins: `\w+://` restarting at
# every position inside a long run of word characters with no `://` is
# quadratic, and this runs over error text a third-party server wrote. Both
# runs are possessive — neither can give a character back that the next
# part could use (`:` is not a word character, `@` is outside the class) —
# so a failed attempt never backtracks into them. And the text is held to
# its head before it is scanned: it only ever reaches a log line or an
# error cell, and a reply of megabytes should cost what a short one does.
REDACT_SCAN_CHARS = 4096
_URL_CREDS = re.compile(r"(?<!\w)(\w++://)[^\s/@]++@")


def redact_url_credentials(text: str | None) -> str | None:
    if not text:
        return text
    if len(text) <= REDACT_SCAN_CHARS:
        return _URL_CREDS.sub(r"\1***@", text)
    head = text[:REDACT_SCAN_CHARS]
    # The cut may fall inside a URL before its `@`, where the pattern can
    # no longer see the credential as one — so the last unbroken run of
    # the head is dropped whole rather than half-kept. A credential never
    # holds whitespace, so whatever stays before it is complete.
    if not head[-1].isspace() and not text[REDACT_SCAN_CHARS].isspace():
        last = head.rsplit(None, 1)
        head = head[:len(head) - len(last[-1])] if last else ""
    return (_URL_CREDS.sub(r"\1***@", head)
            + f"… [{len(text) - len(head):,} more characters not shown]")


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
