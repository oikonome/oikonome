"""Business entities — CRUD + account/transaction assignment.

A tenant declares one or more business entities (their LLC / sole prop). Whole
accounts and/or individual transactions get assigned to an entity; the
hard-separation filter (entity money excluded from personal budgets) reads
these assignments — see the `entity_id IS NULL` predicate in engine/budget.py
and engine/reporting.py. This module only records the model; it never touches
money math directly.

EIN is encrypted at rest under the tenant envelope (db/crypto). The plaintext
EIN is NEVER returned by list/get — only `ein_last4` for display. Reveal is a
separate, explicit call.
"""
from __future__ import annotations

import re

from ..db import crypto

STRUCTURES = ("single_member_llc", "multi_member_llc", "sole_prop", "s_corp")

# columns safe to return to the client (no ein ciphertext)
_PUBLIC = ("id", "name", "structure", "state", "formation_date",
           "business_start_date", "ein_last4", "registered_agent",
           "fiscal_year_end", "status", "home_office_sqft", "income_tax_rate",
           "filing_status", "tax_reserve_account_id", "archived_at",
           "created_at", "updated_at")


def _ein_digits(ein: str | None) -> str | None:
    """Normalize an EIN to 9 digits, or None. Accepts 'XX-XXXXXXX' etc."""
    if not ein:
        return None
    digits = re.sub(r"\D", "", ein)
    if len(digits) != 9:
        raise ValueError("EIN must be 9 digits (formatted XX-XXXXXXX)")
    return digits


def _row(r: dict) -> dict:
    """Serialize a business_entity row for the API — dates to ISO, no EIN."""
    out = {k: r.get(k) for k in _PUBLIC}
    for k in ("formation_date", "business_start_date"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    for k in ("archived_at", "created_at", "updated_at"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    if out.get("income_tax_rate") is not None:
        out["income_tax_rate"] = float(out["income_tax_rate"])
    out["id"] = str(out["id"])
    return out


def create_entity(conn, *, name: str, structure: str, state: str | None = None,
                  formation_date=None, business_start_date=None,
                  ein: str | None = None, registered_agent: str | None = None,
                  fiscal_year_end: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    if structure not in STRUCTURES:
        raise ValueError(f"structure must be one of {STRUCTURES}")
    from .compat import as_date        # parse/validate optional dates (400 not 500)
    formation_date = as_date(formation_date)
    business_start_date = as_date(business_start_date)
    digits = _ein_digits(ein)
    ein_enc = crypto.encrypt(conn, digits) if digits else None
    ein_last4 = digits[-4:] if digits else None
    row = conn.execute(
        """INSERT INTO business_entity
             (name, structure, state, formation_date, business_start_date,
              ein_enc, ein_last4, registered_agent, fiscal_year_end)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING *""",
        (name, structure, state, formation_date, business_start_date,
         ein_enc, ein_last4, registered_agent, fiscal_year_end)).fetchone()
    return _row(row)


def list_entities(conn, *, include_archived: bool = True) -> list[dict]:
    q = "SELECT * FROM business_entity"
    if not include_archived:
        q += " WHERE status = 'active'"
    q += " ORDER BY created_at"
    return [_row(r) for r in conn.execute(q).fetchall()]


def _valid_uuid(s) -> bool:
    import uuid as _uuid
    try:
        _uuid.UUID(str(s))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def get_entity(conn, entity_id: str) -> dict | None:
    if not _valid_uuid(entity_id):   # a malformed id is a 404, not a DB 500
        return None
    r = conn.execute("SELECT * FROM business_entity WHERE id = %s",
                     (entity_id,)).fetchone()
    return _row(r) if r else None


def reveal_ein(conn, entity_id: str) -> str | None:
    """Decrypt the full EIN — an explicit, separate call (never in list/get)."""
    r = conn.execute("SELECT ein_enc FROM business_entity WHERE id = %s",
                     (entity_id,)).fetchone()
    if not r or not r["ein_enc"]:
        return None
    return crypto.decrypt(conn, r["ein_enc"])


_EDITABLE = {"name", "state", "formation_date", "business_start_date",
             "registered_agent", "fiscal_year_end", "structure",
             "home_office_sqft", "income_tax_rate", "filing_status",
             "tax_reserve_account_id"}


def update_entity(conn, entity_id: str, **fields) -> dict | None:
    """Patch editable fields. `ein` (if present) is re-encrypted; `structure`
    is validated. Refuses to edit an archived (read-only) entity."""
    cur = conn.execute("SELECT status FROM business_entity WHERE id = %s",
                       (entity_id,)).fetchone()
    if not cur:
        return None
    if cur["status"] != "active":
        raise ValueError("entity is archived (read-only)")
    from .compat import as_date
    if "structure" in fields and fields["structure"] not in STRUCTURES:
        raise ValueError(f"structure must be one of {STRUCTURES}")
    sets, params = [], []
    for k in _EDITABLE:
        if k not in fields:
            continue
        v = fields[k]
        if k in ("formation_date", "business_start_date"):
            v = as_date(v)                        # parse/validate → 400 not 500
        elif k == "income_tax_rate" and v is not None and v != "":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError("income_tax_rate must be a number")
            if not 0 <= v <= 100:
                raise ValueError("income_tax_rate must be 0–100")
        elif k == "tax_reserve_account_id":
            # "" clears the nomination — the picker's own empty option, and
            # the difference between "no reserve account" and "the account
            # whose id is the empty string" is not one the DB should have to
            # keep straight.
            v = (str(v).strip() or None) if v is not None else None
            if v is not None and not conn.execute(
                    "SELECT 1 FROM accounts WHERE id = %s AND entity_id = %s",
                    (v, entity_id)).fetchone():
                # RLS scopes this to the tenant, so a foreign id fails here
                # too. The entity check is the real point: nominating
                # somebody else's account — or a personal one — as this
                # business's tax reserve would silently misreport the
                # set-aside for both.
                raise ValueError(
                    "the tax reserve must be an account assigned to this "
                    "business")
        elif k == "home_office_sqft" and v is not None and v != "":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError("home_office_sqft must be a whole number")
            # upper bound too: the column is fixed-precision, and an
            # out-of-range value becomes a psycopg NumericValueOutOfRange
            # 500 rather than the 400 the lower bound produces.
            # 4.3 billion sq ft is ~154 sq miles of home office.
            if not 0 <= v <= 1_000_000_000:
                raise ValueError("home_office_sqft must be between 0 and 1e9")
        sets.append(f"{k} = %s")
        params.append(v)
    if "ein" in fields:
        digits = _ein_digits(fields["ein"])
        sets.append("ein_enc = %s")
        params.append(crypto.encrypt(conn, digits) if digits else None)
        sets.append("ein_last4 = %s")
        params.append(digits[-4:] if digits else None)
    if not sets:
        return get_entity(conn, entity_id)
    sets.append("updated_at = now()")
    params.append(entity_id)
    r = conn.execute(
        f"UPDATE business_entity SET {', '.join(sets)} WHERE id = %s "
        "RETURNING *", tuple(params)).fetchone()
    return _row(r)


def set_status(conn, entity_id: str, status: str) -> dict | None:
    """active | archived. Archiving is the non-destructive 'delete' — the
    entity and its assignments stay, but it goes read-only. archived_at
    tracks when the business was closed and clears on restore, so a
    restored-then-archived-again entity carries the latest closing date."""
    if status not in ("active", "archived"):
        raise ValueError("status must be active or archived")
    r = conn.execute(
        "UPDATE business_entity SET status = %s, "
        "archived_at = CASE WHEN %s = 'archived' THEN now() END, "
        "updated_at = now() WHERE id = %s RETURNING *",
        (status, status, entity_id)).fetchone()
    return _row(r) if r else None


def archive_entity(conn, entity_id: str) -> dict | None:
    """The everyday 'remove': hide the entity from active use, keep every
    record. Its books, equity ledger, mileage log, 1099 vendors, compliance
    rows, and transaction assignments all stay readable — tax records carry
    retention obligations, so nothing is destroyed here."""
    return set_status(conn, entity_id, "archived")


def restore_entity(conn, entity_id: str) -> dict | None:
    """Undo an archive: the entity returns to active use, writes reopen."""
    return set_status(conn, entity_id, "active")


# tables whose rows die with the entity (ON DELETE CASCADE) — the counts a
# delete-forever confirmation must put in front of the user, keyed by the
# name the API reports
_CASCADE_TABLES = (("members", "entity_membership"),
                   ("equity_movements", "equity_movement"),
                   ("compliance_obligations", "compliance_obligation"),
                   ("mileage_trips", "mileage_log"),
                   ("vendors_1099", "vendor_1099"))


def delete_impact(conn, entity_id: str) -> dict | None:
    """What delete-forever would destroy (cascaded rows, per table) and what
    it would detach back to personal (accounts/transactions — their FKs are
    ON DELETE SET NULL, so the rows survive with the assignment cleared)."""
    ent = get_entity(conn, entity_id)
    if not ent:
        return None
    out = {"id": ent["id"], "name": ent["name"], "destroyed": {},
           "detached": {}}
    for label, table in _CASCADE_TABLES:
        out["destroyed"][label] = conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE entity_id = %s",
            (entity_id,)).fetchone()["n"]
    for label, table in (("accounts", "accounts"),
                         ("transactions", "transactions")):
        out["detached"][label] = conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE entity_id = %s",
            (entity_id,)).fetchone()["n"]
    return out


def delete_forever(conn, entity_id: str, confirm_name: str) -> dict | None:
    """Permanently destroy an entity and everything the CASCADE FKs hang off
    it — the made-this-by-mistake case only; archiving is the normal way to
    remove a business. The caller must present the entity's EXACT name:
    that is enforced HERE, server-side, so no client shortcut can skip the
    typed-name confirmation. Returns the pre-delete impact (what was
    destroyed/detached), or None if the entity doesn't exist."""
    impact = delete_impact(conn, entity_id)
    if impact is None:
        return None
    if (confirm_name or "").strip() != impact["name"]:
        raise ValueError(
            "type the business's exact name to delete it forever")
    with conn.transaction():
        conn.execute("DELETE FROM business_entity WHERE id = %s",
                     (entity_id,))
    return impact


def require_active(conn, entity_id: str) -> dict:
    """Existence + not-archived guard for every business WRITE path (equity,
    mileage, members, classification, obligations…). Archived entities are
    read-only — an archived entity must reject writes, not just entity-update.
    Returns the entity row; raises ValueError (→ 404/409) otherwise."""
    e = get_entity(conn, entity_id)
    if not e:
        raise ValueError("no such entity")
    if e["status"] != "active":
        raise ValueError("entity is archived (read-only)")
    return e


# ---- assignment ------------------------------------------------------------

def assign_account(conn, account_id: str, entity_id: str | None) -> bool:
    """Assign a whole account to an entity (all its transactions become
    business), or None to return it to personal. entity_id (when set) must
    exist — the FK enforces it."""
    n = conn.execute(
        "UPDATE accounts SET entity_id = %s WHERE id = %s",
        (entity_id, account_id)).rowcount
    return n > 0


def assign_transaction(conn, txn_id: str, entity_id: str | None) -> bool:
    """Per-transaction override — a business cost paid on a personal card, or
    None to clear the override (falls back to the account's assignment)."""
    n = conn.execute(
        "UPDATE transactions SET entity_id = %s WHERE id = %s",
        (entity_id, txn_id)).rowcount
    return n > 0


# ---- members ---------------------------------------------------------------

def add_member(conn, entity_id: str, *, member_name: str,
               ownership_pct=None, is_manager: bool = False) -> dict:
    member_name = (member_name or "").strip()
    if not member_name:
        raise ValueError("member_name is required")
    if ownership_pct is not None and ownership_pct != "":
        try:
            ownership_pct = float(ownership_pct)
        except (TypeError, ValueError):
            raise ValueError("ownership_pct must be a number")
        if not 0 <= ownership_pct <= 100:
            raise ValueError("ownership_pct must be 0–100")
    else:
        ownership_pct = None
    r = conn.execute(
        """INSERT INTO entity_membership
             (entity_id, member_name, ownership_pct, is_manager)
           VALUES (%s,%s,%s,%s) RETURNING id, member_name, ownership_pct,
                   is_manager""",
        (entity_id, member_name, ownership_pct, is_manager)).fetchone()
    return {"id": str(r["id"]), "member_name": r["member_name"],
            "ownership_pct": (float(r["ownership_pct"])
                              if r["ownership_pct"] is not None else None),
            "is_manager": r["is_manager"]}


# ---- reconcile the legacy business_flags tags + combined toggle -------------

def count_flagged(conn) -> int:
    """How many transactions carry the legacy business_flags tag — which
    entity assignment supersedes — and are not yet assigned to an
    entity."""
    r = conn.execute(
        "SELECT count(*) AS n FROM business_flags f JOIN transactions t "
        "ON t.id = f.txn_id WHERE t.entity_id IS NULL").fetchone()
    return r["n"]


def import_flagged_transactions(conn, entity_id: str) -> int:
    """Assign every business_flagged transaction (not already on an entity) to
    this entity — the one-time reconcile from the legacy Schedule-C tags.
    Leaves the flags in place (non-destructive); the entity assignment now
    drives the money math. Returns the count moved."""
    return conn.execute(
        "UPDATE transactions SET entity_id = %s WHERE entity_id IS NULL "
        "AND id IN (SELECT txn_id FROM business_flags)",
        (entity_id,)).rowcount


def is_combined(conn) -> bool:
    from . import budget
    return bool(budget.load_config(conn).get("combine_entities"))


def set_combined(conn, on: bool) -> bool:
    """Toggle the combined view (config combine_entities). Takes effect on the
    NEXT connection (the session var is set at tenant_connect)."""
    from . import budget
    with budget.config_txn(conn) as cfg:
        cfg["combine_entities"] = bool(on)
    return bool(on)


def list_members(conn, entity_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, member_name, ownership_pct, is_manager FROM "
        "entity_membership WHERE entity_id = %s ORDER BY created_at",
        (entity_id,)).fetchall()
    return [{"id": str(r["id"]), "member_name": r["member_name"],
             "ownership_pct": (float(r["ownership_pct"])
                               if r["ownership_pct"] is not None else None),
             "is_manager": r["is_manager"]} for r in rows]
