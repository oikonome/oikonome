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
import logging
import math
from ..envnum import env_flag
import re
from collections import defaultdict
from typing import Protocol

from ..db import crypto, tenancy
from ..engine.compat import jsonb
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
    their slot; manual accounts and collector scripts never count."""
    return conn.execute(
        """SELECT COUNT(*) AS n FROM items
           WHERE COALESCE(status, 'ok') != 'archived'
             AND aggregator IN ('plaid', 'mx', 'simplefin-org')"""
    ).fetchone()["n"]


def check_institution_cap(conn) -> None:
    """Raise ValueError (friendly copy) when connecting one more
    institution would pass the instance's cap. Call before opening any
    ADD-a-connection door; update/repair flows are exempt."""
    cap = institution_cap(conn)
    if cap is None:
        return
    n = institution_count(conn)
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
                access_token: str | None, raw: dict | None = None) -> None:
    """access_token is ENCRYPTED at rest (per-tenant envelope, db/crypto);
    read it back only through get_access_token."""
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, access_token, raw)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id, id) DO UPDATE SET
               institution_name=EXCLUDED.institution_name,
               access_token=EXCLUDED.access_token, status='ok',
               raw=EXCLUDED.raw""",
        (item_id, aggregator, institution_name,
         crypto.encrypt(conn, access_token), jsonb(raw or {})))


def get_access_token(conn, item_id: str) -> str | None:
    row = conn.execute("SELECT access_token FROM items WHERE id=%s",
                       (item_id,)).fetchone()
    return crypto.decrypt(conn, row["access_token"]) if row else None


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
    # the audit ledger. Non-Plaid items archive as before.
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
    else:
        conn.execute(
            "UPDATE items SET status='archived', "
            "archived_at=COALESCE(archived_at, now()) WHERE id=%s",
            (item_id,))
    if agg == "simplefin":
        # archiving the bridge silences all its per-bank children too
        conn.execute(
            "UPDATE items SET status='archived', "
            "archived_at=COALESCE(archived_at, now()) "
            "WHERE aggregator='simplefin-org' AND id LIKE %s",
            (item_id + ":%",))
    return {"ok": True, "plaid_released": plaid_released,
            "release_failed": release_failed, "note": note,
            "aggregator": agg}


def purge_account_data(conn, account_id: str) -> dict:
    """Hard-delete one account and its local data (transactions cascade;
    snapshot tables that lack FKs are cleaned explicitly). Does NOT touch
    the parent Item or Plaid — caller handles disconnect when needed.
    Returns counts of deleted rows."""
    n_txn = conn.execute(
        "DELETE FROM transactions WHERE account_id=%s",
        (account_id,)).rowcount
    n_hold = conn.execute(
        "DELETE FROM holdings WHERE account_id=%s",
        (account_id,)).rowcount
    n_crypto = conn.execute(
        "DELETE FROM crypto_holdings WHERE account_id=%s",
        (account_id,)).rowcount
    conn.execute("DELETE FROM liabilities WHERE account_id=%s", (account_id,))
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
    return {"accounts": n_acct, "transactions": n_txn,
            "holdings": n_hold, "crypto_holdings": n_crypto}


def purge_item_accounts(conn, item_id: str) -> dict:
    """Delete every account hanging off an item (and, for a SimpleFIN
    bridge, every account under its per-bank children)."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE item_id=%s OR item_id LIKE %s",
        (item_id, item_id + ":%")).fetchall()]
    totals = {"accounts": 0, "transactions": 0, "holdings": 0,
              "crypto_holdings": 0}
    for aid in ids:
        part = purge_account_data(conn, aid)
        for k in totals:
            totals[k] += part[k]
    # drop empty child items (bridge itself stays archived for audit)
    conn.execute(
        "DELETE FROM items WHERE aggregator='simplefin-org' AND id LIKE %s "
        "AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.item_id=items.id)",
        (item_id + ":%",))
    # if the item itself has no accounts left and is archived/manual-ish,
    # leave the archived row (history of connection); only delete empty
    # non-aggregator shells when nothing references them
    return totals


def item_is_live(status: str | None) -> bool:
    """True when the connection is still syncing (not user/reaper archived)."""
    return (status or "ok") != "archived"


def upsert_accounts(conn, item_id: str, accounts: list[Account]) -> None:
    for a in accounts:
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
            (a.id, item_id, a.name, a.type, a.subtype, a.mask,
             a.balance_current, a.balance_available, a.currency, jsonb(a.raw)))
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
# money actually went to ("VENMO *TAYLOR SAMPLE 7700 EASTPORT PARKWAY
# 8558124430, NY, US"). Recover the counterparty and make it the merchant,
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


# Zelle statement lines name BOTH ends ("Zelle payment from A (Bank
# Spending Account XXXXXX1234) to B"), and banks prefix them with the
# transfer kind ("NOW Withdrawal — …"). The amount sign says which end is
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
# sizes of purchase; blended under "Costco" the ledger can answer neither
# "what does a tank cost" nor "what does a Costco run cost", and the
# recurring detector chases a payee whose amount never settles.
#
# Plaid already knows. It sends merchant_name "Costco" (the brand) beside
# name "Costco Gas" (the outlet), and the bank's own statement line says
# "COSTCO GAS #0007" vs "COSTCO WHSE #0007". What the ledger drops is the
# name: payee is COALESCE(merchant_name, name), so the brand wins and the
# specific label is discarded. Promoting `name` wholesale is not the fix —
# it is usually the shouted statement string ("H-E-B #512 000000000SPRINGFIELD",
# "STREAMFLIX *SUBSSPRINGFIELD"), which is exactly why
# merchant_name is preferred in the first place. So read the narrow signal
# out of either text instead: the BRAND followed by a fuel word, which is
# what a pump's own label looks like in both fields.
#
# What cannot do this job:
#   * merchant_entity_id — identical for the pump, the warehouse and the
#     website. Plaid's entity model says Costco is one merchant.
#   * the category — TRANSPORTATION_GAS is right on every one of these
#     rows, but fuel is also the category most often wrong elsewhere, and
#     a merchant derived from a category lets one bad row rename a payee.
#   * the MCC (5542 is the automated fuel dispenser) — mostly null.
# A chain whose fuel rows carry neither label is left alone rather than
# split on a hunch.
# Brand and text are matched on their ALPHANUMERICS only, character by
# character, so punctuation and spacing cannot hide a match: "H-E-B" finds
# "H-E-B GAS/CARWASH" and "HEB GAS STATION" alike, and "Lowe's" would find
# "LOWES FUEL". Nothing brand-specific — the pattern is built from whatever
# the merchant is called, and because only its alphanumerics survive, a
# merchant name can never inject regex syntax.
#
# The fuel word must be the NEXT token. That single requirement is what
# keeps the rule honest: "COSTCO WHSE" and "WWW COSTCO COM" fail on the
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
    """"Costco" + "COSTCO GAS #0007" (or Plaid name "Costco Gas") ->
    "Costco Gas". None when neither text proves the pump."""
    brand = (merchant or "").strip()
    if not brand:
        return None
    # already the fuel arm's own name — nothing to promote. This is also
    # what keeps a natural-gas utility ("Wisconsin Public Gas") from being
    # renamed "Wisconsin Public Gas Gas".
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


def upsert_transactions(conn, txns: list[Transaction]) -> int:
    # every aggregator (plaid per page, mx and simplefin whole-pull) and
    # every importer funnels through here — the one place a generous
    # ceiling covers a hostile provider payload (a tenant-supplied
    # SimpleFIN bridge can stream any list it likes) without any door
    # having to remember its own cap
    rowcap.check_sync_len(txns)
    n = 0
    hidden = hidden_account_ids(conn)
    cards = credit_account_ids(conn)
    for t in txns:
        if t.account_id in hidden:
            continue
        # every door funnels here, so this is where a NaN/inf amount — a
        # tenant-supplied bridge, a collector push, a hand-edited file —
        # is refused before it reaches DOUBLE PRECISION and poisons every
        # sum that touches the row
        try:
            if not math.isfinite(float(t.amount)):
                raise ValueError
        except (TypeError, ValueError):
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
        # who decided category_primary — the row can say WHY it is what it
        # is (Plaid's own PFC / an importer's map / a flow normalization /
        # the fuel-arm outlet). Machine and user rules re-stamp it later.
        cat_source = (None if cat_primary is None
                      else "plaid" if t.category_plaid else "import")
        if (t.account_id in cards and (t.amount or 0) < 0
                and flowmap.looks_like_card_payment_on_card(
                    t.merchant_name or t.name)):
            cat_primary = "LOAN_PAYMENTS"
            cat_detailed = flowmap.CC_PAYMENT_DETAILED
            cat_source = "flow"
        # No merchant from the aggregator → recover the P2P counterparty
        # from the bank's statement line. Deterministic on the same input,
        # so a re-sync (pending → posted) re-derives the same merchant.
        desc = (t.raw or {}).get("original_description")
        merchant = t.merchant_name or venmo_counterparty(desc)
        # Zelle detail can live in `name` too (file imports put the whole
        # statement line there), and an aggregator-supplied merchant that
        # is itself just the truncated Zelle descriptor is noise, not a
        # merchant — the parsed counterparty beats it.
        zelle = zelle_counterparty(desc or t.name, (t.amount or 0) < 0)
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
        outlet = fuel_arm(merchant, desc, t.name)
        # A row the bank itself labels as this brand's pump is fuel,
        # whatever the aggregator guessed. Plaid files H-E-B's
        # "GAS/CARWASH" line under groceries, which is how a tank of petrol
        # ends up inside a food budget. This is the same move the card-
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
                   name=EXCLUDED.name, merchant_name=EXCLUDED.merchant_name,
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
                   -- a row the aggregator re-reports comes back — unless a
                   -- person retired it (a stuck hold), which the feed
                   -- re-reporting it every hour must not undo
                   removed=CASE WHEN transactions.retired_at IS NOT NULL THEN 1 ELSE 0 END,
                   raw=EXCLUDED.raw""",
            (t.id, t.account_id, t.date, t.amount, t.name, merchant, outlet,
             cat_primary, cat_detailed,
             t.category_plaid, t.category_plaid_detailed,
             t.category_plaid_confidence,
             1 if t.pending else 0, jsonb(t.raw),
             cat_source, en["check_number"], en["payment_channel"],
             en["payment_processor"], en["location_city"], en["location_region"],
             en["location_address"], en["location_postal"], en["location_lat"],
             en["location_lon"], en["location_store"], en["mcc"],
             en["authorized_date"]))
        n += cur.rowcount
        # category_override deliberately untouched — sacred, never synced

    # pending→posted settlement: the posted transaction arrives as a NEW
    # row (new id) and the pending row it replaces is marked removed in
    # the same sync — so anything the user did to the pending row
    # (recategorize, owner, entity, a note) silently died with it, one to
    # two days after they did it. Plaid names the predecessor
    # (pending_transaction_id), so carry the user's state across the swap
    # — never overwriting anything already set on the posted row — and
    # persist the link itself, which existed as a column since migration
    # 043 but was never written.
    #
    # SET-BASED, not once per settling row: a morning sync delivering a
    # day's card activity settles dozens of pendings at once, and the
    # per-row version spent nine round trips on each of them inside the
    # path that delivers the money. Every statement below carries the
    # WHOLE page from one `unnest` pair, so the cost is a fixed handful of
    # statements per sync however many rows settle.
    links = [(t.id, t.raw.get("pending_transaction_id")) for t in txns
             if t.account_id not in hidden and isinstance(t.raw, dict)
             and t.raw.get("pending_transaction_id")]
    if links:
        pairs = list(dict.fromkeys(links))     # a replayed page repeats pairs
        new_ids = [p[0] for p in pairs]
        old_ids = [p[1] for p in pairs]
        # The posted row's own id is the upsert key, so it appears once per
        # page: one target row per pair, and the COALESCE below reads the
        # pending twin exactly as the per-row UPDATE did.
        conn.execute(
            """UPDATE transactions t SET
                   pending_transaction_id = v.old_id,
                   category_override = COALESCE(t.category_override,
                                                p.category_override),
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
        _carry("UPDATE business_txn_class b SET txn_id=v.new_id"
               " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
               " WHERE b.txn_id=v.old_id AND NOT EXISTS"
               "  (SELECT 1 FROM business_txn_class x WHERE x.txn_id=v.new_id)")
        _carry("UPDATE equity_movement e SET txn_id=v.new_id"
               " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
               " WHERE e.txn_id=v.old_id AND NOT EXISTS"
               "  (SELECT 1 FROM equity_movement k WHERE k.txn_id=v.new_id"
               "   AND k.kind=e.kind)")
        _carry("UPDATE manual_categories m SET transaction_id=v.new_id"
               " FROM unnest(%s::text[], %s::text[]) AS v(new_id, old_id)"
               " WHERE m.transaction_id=v.old_id AND NOT EXISTS"
               "  (SELECT 1 FROM manual_categories x"
               "   WHERE x.transaction_id=v.new_id)")
        # And retire the predecessors: waiting for the aggregator's own
        # removal delta leaves a phantom pending double-counting spend
        # whenever that delta is missed. OUTSIDE the best-effort carries
        # above, on purpose — a child move that fails must not also keep
        # the pending row live next to its posted twin (the money would
        # then count twice until a removal delta that may never come).
        # Each statement autocommits, so a failed carry cannot poison this.
        conn.execute(
            "UPDATE transactions SET removed=1"
            " WHERE id = ANY(%s) AND pending=1 AND removed=0", (old_ids,))

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


# ---- reverse cross-source dedup (aggregator vs. earlier file imports) ------
#
# The file importers dedup against aggregator rows on the SAME account
# (csvimport and its siblings: account, ±3 days, exact amount). The
# reverse case —
# user imports bank files FIRST (rows land on manual:<slug> accounts), THEN
# connects SimpleFIN/Plaid — could never match that way: the aggregator's
# account ids differ, so every overlapping charge double-counted and the
# hourly worker kept recreating the overlap. Aggregator ingest therefore
# checks tenant-WIDE for a file-import row covering the incoming charge,
# but only inside the date window the imports actually cover — genuinely
# new activity after the import coverage always inserts. Long-term account
# linking is out of scope; this window-scoped guard is the fix.

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
    genuine same-amount charges survive."""

    def __init__(self, conn):
        clauses = " OR ".join("id LIKE %s" for _ in IMPORT_ID_PREFIXES)
        rows = conn.execute(
            "SELECT date, amount, name, merchant_name FROM transactions "
            f"WHERE removed = 0 AND ({clauses})",
            tuple(p + "%" for p in IMPORT_ID_PREFIXES)).fetchall()
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
        self._by_cents: dict[int, list[dict]] = defaultdict(list)
        dates = []
        for r in rows:
            if r["date"] is None or r["amount"] is None:
                continue
            self._by_cents[round(r["amount"] * 100)].append(
                {"date": r["date"],
                 "tokens": _name_tokens(r["name"], r["merchant_name"])})
            dates.append(r["date"])
        # import coverage window (+ posting-drift slack at the edges)
        slack = dt.timedelta(days=IMPORT_DEDUP_WINDOW_DAYS)
        self.min_date = min(dates) - slack if dates else None
        self.max_date = max(dates) + slack if dates else None

    @property
    def empty(self) -> bool:
        return self.min_date is None

    def covers(self, t: Transaction) -> bool:
        """True (and the matched import row is consumed) when a file-import
        row already represents this incoming aggregator transaction."""
        if self.empty or t.date is None or t.amount is None:
            return False
        if not (self.min_date <= t.date <= self.max_date):
            return False                 # outside import coverage: always new
        cands = self._by_cents.get(round(t.amount * 100))
        if not cands:
            return False
        toks = _name_tokens(t.name, t.merchant_name)
        best = None                      # (rank, candidate); lower rank wins
        for c in cands:
            gap = abs((c["date"] - t.date).days)
            if gap > IMPORT_DEDUP_WINDOW_DAYS:
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
            return False
        cands.remove(best[1])            # one-to-one: consume the import row
        return True


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
    if guard.empty:
        return txns, 0
    existing = {r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE id = ANY(%s)",
        ([t.id for t in txns],)).fetchall()}
    kept, dropped = [], []
    for t in txns:
        if t.id not in existing and guard.covers(t):
            dropped.append(t)
        else:
            kept.append(t)
    if dropped:
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
        if got:
            # a failed unlock drops the pooled connection rather than
            # handing the next borrower a lock nobody meant to hold
            tenancy.release_lock(conn, key)


def mark_removed(conn, txn_ids: list[str]) -> int:
    """Flag rows a removal delta names. Returns how many actually landed.

    A miss is not noise: after a reconnect the aggregator can know a held
    charge under a NEW id (id churn), so a later removal naming that id
    matches nothing here while the charge stays on the books under its old
    id. Logging the missed ids is the only trace such a phantom leaves."""
    missed = []
    for tid in txn_ids:
        cur = conn.execute(
            "UPDATE transactions SET removed=1 WHERE id=%s", (tid,))
        if cur.rowcount == 0:
            missed.append(tid)
    if missed:
        log.warning("removal delta named %d id(s) not in the ledger "
                    "(id churn after a reconnect?): %s",
                    len(missed), ", ".join(missed[:10]))
    return len(txn_ids) - len(missed)


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
        conn.execute("UPDATE transactions SET removed=1 WHERE id = ANY(%s)",
                     ([r["id"] for r in stale],))
        log.info("retired %d vanished pending row(s) for %s", len(stale),
                 id_prefix)
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
