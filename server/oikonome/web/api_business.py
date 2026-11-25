"""Business tagging, entities and the self-employed tax pack.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import contextlib
import datetime as dt
from fastapi import Depends, HTTPException, Query, Request

from . import data, demoguard
from ..engine import activity as _activity
from .api_common import (_ck_year, _conn, _csv_safe, _csv_writer,
                         _filename_safe, _may_edit, _owner_only, router,
                         _same_site_download, _ser, _today, _user)
from .fields import Fields, json_fields
from .schemas import documented, Ok
from .schemas.business import (AccountEntitySet, BalanceSheet, BizData,
                               BizTxns, BusinessSummary, Combine, Combined,
                               Created, Entities, Entity, EntityAssign,
                               EntityCreate, EntityDeleteForever,
                               EntityDeleteImpact, EntityDeleted,
                               EntityInput, EntityMember, EntityStatus,
                               EquityInput, EquityMovement, EquityPage,
                               EquityReimburse, EstimatedTax, FlagsImported,
                               LineSuggestions, MemberInput, Mileage,
                               ObligationInput, Obligations,
                               OwnerTransferInput, Pnl, TripInput,
                               TxnClass, TxnEntitySet, UnlinkedMovements,
                               VendorMark, Vendors)


# ---- business expenses (Schedule C tagging) ----------------------------------

def _require_business(conn):
    """Business / expense tracking is not gated; this no-op is the one
    place a feature gate would go, so adding one is a change to a single
    function rather than to every route below."""
    return None


def _require_entity_id(entity_id: str) -> str:
    """404 a path segment that is not a uuid.

    business_entity.id is a Postgres uuid column, so a malformed id fails in
    the cast — before any of our own checks — and surfaces as a bare 500 from
    the global handler. One definition rather than the inline copy each of
    these ~15 routes would otherwise carry: an inline copy is missed every
    time a route is added, which is the whole failure mode."""
    from ..engine import entities as _ent
    if not _ent._valid_uuid(entity_id):
        raise HTTPException(404, "no such entity")
    return entity_id


def _require_record_id(record_id: str, what: str) -> str:
    """The same rule for a nested record id (equity movement, mileage trip,
    compliance obligation). Those columns are uuid too, and the DELETE goes
    straight to Postgres with whatever the path carried."""
    from ..engine import entities as _ent
    if not _ent._valid_uuid(record_id):
        raise HTTPException(404, f"no such {what}")
    return record_id


@router.post("/business/{txn_id:path}/flag")
@documented(response=Ok)
def business_flag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        _require_business(conn)
        # under the pairing lock, onto the live row (live_edit_ids)
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
            data.flag_business(conn, txn_id)
        _activity.record(conn, user, "business", "flagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/business/{txn_id:path}/unflag")
@documented(response=Ok)
def business_unflag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        _require_business(conn)
        # under the pairing lock, onto the live row (live_edit_ids)
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
            data.unflag_business(conn, txn_id)
        _activity.record(conn, user, "business", "unflagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id))
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business")
@documented(response=BizData)
def business_list(user: dict = Depends(_user()), year: int | None = None,
                  limit: int = 500, offset: int = 0):
    """Flagged-transaction worksheet, paged. The default cap is generous
    (one fetch covers almost every ledger) but bounded — an all-years
    request on a decade-old ledger must not ship the entire history to
    paint one screen. `count` covers the whole scope, so a client can
    page with limit/offset when rows hits the cap — and must SAY when it
    is showing fewer rows than `count`, or the drop is invisible. The CSV
    export stays unpaged: a worksheet download is the full worksheet."""
    _ck_year(year, "year")
    limit = max(1, min(int(limit), 1000))
    # `offset` is capped as well as `limit`: an offset past bigint would
    # reach OFFSET and die inside the driver as
    # NumericValueOutOfRange -> 500. Same ceiling the paged ledger and the
    # rules list clamp their page number to: far past any real worksheet,
    # and small enough that offset * anything still binds.
    offset = min(max(0, int(offset)), 10_000_000)
    conn = _conn(user)
    try:
        _require_business(conn)
        r = data.business_expenses(conn, year, limit=limit, offset=offset)
    finally:
        conn.close()
    return {"rows": [_ser(x) for x in r["rows"]], "total": r["total"],
            "count": r["count"], "years": r["years"]}


@router.get("/business/export.csv")
def business_export(request: Request, user: dict = Depends(_user()),
                    year: int | None = None):
    """The Schedule C worksheet: flagged transactions as CSV. Owner-only,
    as the ledger download is."""
    _same_site_download(request)
    _owner_only(user)
    import io as _io

    from fastapi.responses import Response
    _ck_year(year, "year")
    conn = _conn(user)
    try:
        _require_business(conn)
        r = data.business_expenses(conn, year)
    finally:
        conn.close()
    buf = _io.StringIO()
    w = _csv_writer(buf)
    w.writerow(["date", "payee", "category", "account", "amount"])
    for row in r["rows"]:
        w.writerow([row["date"].isoformat(), _csv_safe(row["payee"]),
                    _csv_safe(row["category"]),
                    _csv_safe(row["account"] or ""), f'{row["amount"]:.2f}'])
    w.writerow([])
    w.writerow(["total", "", "", "", f'{r["total"]:.2f}'])
    name = f"oikonome-business-{year or 'all'}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{name}"'})


# ---- business entities ----------------------------------------------

@router.get("/business/entities")
@documented(response=Entities)
def entities_list(user: dict = Depends(_user())):
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"entities": entities.list_entities(conn)}
    finally:
        conn.close()


@router.post("/business/entities")
@documented(response=Entity, body=EntityCreate)
def entities_create(user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            ent = entities.create_entity_once(
                conn, name=body.text("name"),
                structure=body.text("structure"),
                state=body.text("state") or None,
                formation_date=body.text("formation_date") or None,
                business_start_date=body.text(
                    "business_start_date") or None,
                ein=body.text("ein") or None,
                registered_agent=body.text("registered_agent") or None,
                fiscal_year_end=body.text("fiscal_year_end") or None,
                # whose W-2 wages close its wage base; left out: not said
                owner=body.text("owner") or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return ent
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}")
@documented(response=Entity)
def entities_get(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        from ..engine import equity
        ent["members"] = entities.list_members(conn, entity_id)
        ent["capital"] = equity.capital_summary(conn, entity_id)
        return ent
    finally:
        conn.close()


@router.patch("/business/entities/{entity_id}")
@documented(response=Entity, body=EntityInput)
def entities_update(entity_id: str, user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    # One typed reader per editable key, and only the keys the body names
    # are passed on: a key left out keeps its value, null clears it, and a
    # stray "entity_id"/"conn" never reaches the engine's keyword args. The
    # engine still owns each field's own rule (range, MM-DD, structure).
    # The clients send the size and the rate as numbers (Number(text), so
    # a fraction can arrive) or null; a digits string is taken as well.
    readers = {
        "name": lambda: body.text("name") or None,
        "structure": lambda: body.text("structure", strip=False) or None,
        "state": lambda: body.text("state") or None,
        "registered_agent": lambda: body.text("registered_agent") or None,
        "fiscal_year_end": lambda: body.text("fiscal_year_end") or None,
        "formation_date": lambda: body.text("formation_date") or None,
        "business_start_date":
            lambda: body.text("business_start_date") or None,
        "home_office_sqft": lambda: body.int_in(
            "home_office_sqft", text_ok=True, truncate=True,
            msg="home_office_sqft must be a whole number"),
        "income_tax_rate": lambda: body.money(
            "income_tax_rate", msg="income_tax_rate must be a number"),
        "tax_reserve_account_id":
            lambda: body.text("tax_reserve_account_id") or None,
        "ein": lambda: body.text("ein") or None,
        # whose business it is; null is "not said". A body without the key
        # (an older client) leaves it as it is.
        "owner": lambda: body.text("owner") or "unknown",
    }
    conn = _conn(user)
    try:
        _require_business(conn)
        patch = {k: read() for k, read in readers.items() if body.present(k)}
        try:
            ent = entities.update_entity(conn, entity_id, **patch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ent:
            raise HTTPException(404, "no such entity")
        return ent
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/status")
@documented(response=Entity, body=EntityStatus)
def entities_status(entity_id: str, user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            ent = entities.set_status(
                conn, entity_id, body.text("status", strip=False))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ent:
            raise HTTPException(404, "no such entity")
        return ent
    finally:
        conn.close()


def _entity_lifecycle(entity_id: str, user: dict, status: str) -> dict:
    """Shared archive/restore body — editors only, demo-denied, 404 on a
    missing or malformed id."""
    from ..engine import entities
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.set_status(conn, entity_id, status)
        if not ent:
            raise HTTPException(404, "no such entity")
        return ent
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}")
@documented(response=Entity)
def entities_delete(entity_id: str, user: dict = Depends(_user())):
    """'Delete' ARCHIVES — the safe default. The entity's equity ledger,
    mileage log, 1099 vendors, and compliance calendar are tax records with
    retention obligations, so a plain REST delete (including any stale
    client that still issues one) must never destroy them. Actual
    destruction is the separate delete-forever call, which demands the
    entity's typed name."""
    return _entity_lifecycle(entity_id, user, "archived")


@router.post("/business/entities/{entity_id}/archive")
@documented(response=Entity)
def entities_archive(entity_id: str, user: dict = Depends(_user())):
    """Hide the entity from active use; every record stays readable."""
    return _entity_lifecycle(entity_id, user, "archived")


@router.post("/business/entities/{entity_id}/restore")
@documented(response=Entity)
def entities_restore(entity_id: str, user: dict = Depends(_user())):
    """Reopen an archived entity — the round trip out of archive."""
    return _entity_lifecycle(entity_id, user, "active")


@router.get("/business/entities/{entity_id}/delete-impact")
@documented(response=EntityDeleteImpact)
def entities_delete_impact(entity_id: str, user: dict = Depends(_user())):
    """What delete-forever would destroy (cascaded rows, counted per table)
    and detach (accounts/transactions returned to personal) — so the
    confirmation dialog states exactly what is at stake."""
    from ..engine import entities
    _require_entity_id(entity_id)
    conn = _conn(user)
    try:
        _require_business(conn)
        impact = entities.delete_impact(conn, entity_id)
        if not impact:
            raise HTTPException(404, "no such entity")
        return impact
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/delete-forever")
@documented(response=EntityDeleted, body=EntityDeleteForever)
def entities_delete_forever(entity_id: str, user: dict = Depends(_user()),
                            body: Fields = Depends(json_fields)):
    """Permanent destruction — the made-this-by-mistake case. The body must
    carry the entity's EXACT name; a mismatch is a 400 and nothing is
    touched. Enforced server-side so the typed-name confirmation cannot be
    bypassed by calling the API directly.

    Owner-only. Archiving (the safe default a member may do) keeps every
    record; this call irrecoverably DELETEs the entity and cascades to its
    equity ledger, compliance calendar, mileage log and 1099 vendors —
    retention-obligated tax records. Destroying them is exactly the
    irreversible, household-control-losing act the role design reserves for
    the owner, and the typed-name string is a mistake-guard, not an
    authentication control (every member can read the name)."""
    from ..engine import entities
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _owner_only(user)
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            impact = entities.delete_forever(
                conn, entity_id, body.text("name", strip=False))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not impact:
            raise HTTPException(404, "no such entity")
        return {"ok": True, **impact}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/members")
@documented(response=EntityMember, body=MemberInput)
def entities_add_member(entity_id: str, user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
    """Add one member. The owner's door, like correcting or removing one:
    a new member's ownership share changes how a multi-member estimate
    splits the profit just as an edited share does."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            with _entity_write(conn, entity_id):
                return entities.add_member(
                    conn, entity_id,
                    member_name=body.text("member_name"),
                    ownership_pct=body.money(
                        "ownership_pct",
                        msg="ownership_pct must be a number"),
                    is_manager=bool(body.flag("is_manager")),
                    is_household=bool(body.flag("is_household")))
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@contextlib.contextmanager
def _entity_write(conn, entity_id: str):
    """The transaction a write into an entity's own records runs in, opened
    by the active check: the business row is held FOR SHARE until the write
    commits, so an archive landing meanwhile waits for it rather than
    closing the books under it. A door's own check before this, outside any
    transaction, is only the early answer. For writes that lock no
    transaction or account row; those take their row first and check the
    entity after it (entities.require_active)."""
    from ..engine import entities
    with conn.transaction():
        try:
            entities.require_active(conn, entity_id)
        except ValueError as e:
            raise HTTPException(409 if "archived" in str(e) else 404, str(e))
        yield


def _member_write(user: dict, entity_id: str, write):
    """Run one member edit or removal, refusing an archived business. Both
    doors are the owner's (checked in each handler), as adding is:
    ownership shares
    decide how a multi-member estimate splits the profit. The
    business row is held FOR SHARE while the write runs, so an archive
    landing meanwhile waits for it rather than closing the books under it."""
    _require_entity_id(entity_id)
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            with conn.transaction():
                row = conn.execute(
                    "SELECT status FROM business_entity WHERE id = %s "
                    "FOR SHARE", (entity_id,)).fetchone()
                if row is None:
                    raise HTTPException(404, "no such entity")
                if row["status"] != "active":
                    raise HTTPException(409, "entity is archived (read-only)")
                out = write(conn)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not out:
            raise HTTPException(404, "no such member")
        return out
    finally:
        conn.close()


@router.patch("/business/entities/{entity_id}/members/{member_id}")
@documented(response=EntityMember, body=MemberInput)
def entities_update_member(entity_id: str, member_id: str,
                           user: dict = Depends(_user()),
                           body: Fields = Depends(json_fields)):
    """Correct one member: any of member_name, ownership_pct (0–100, or
    null for none) and is_manager (true/false). Keys left out keep their
    value. Answers the member as list_members shows it."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import entities
    # a flag sent as null reads as None, which the engine refuses (a flag
    # is true or false, never cleared)
    readers = {
        "member_name": lambda: body.text("member_name") or None,
        "ownership_pct": lambda: body.money(
            "ownership_pct", msg="ownership_pct must be a number"),
        "is_manager": lambda: body.flag("is_manager"),
        "is_household": lambda: body.flag("is_household"),
    }
    fields = {k: read() for k, read in readers.items() if body.present(k)}
    return _member_write(user, entity_id, lambda conn: entities.update_member(
        conn, entity_id, member_id, **fields))


@router.delete("/business/entities/{entity_id}/members/{member_id}")
@documented(response=Ok)
def entities_delete_member(entity_id: str, member_id: str,
                           user: dict = Depends(_user())):
    """Remove one member. A capital movement that named them keeps its
    amount and stops naming them."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import entities
    _member_write(user, entity_id, lambda conn: entities.delete_member(
        conn, entity_id, member_id))
    return {"ok": True}


@router.get("/business/entities/{entity_id}/equity")
@documented(response=EquityPage)
def equity_list(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import books, equity
    conn = _conn(user)
    try:
        _require_business(conn)
        lifetime = books.entity_transactions(conn, entity_id, None)
        return {"movements": equity.list_movements(conn, entity_id),
                "capital": equity.capital_summary(conn, entity_id,
                                                  lifetime=lifetime),
                "owner_transfers": books.owner_transfers(lifetime)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/equity")
@documented(response=EquityMovement, body=EquityInput)
def equity_record(entity_id: str, user: dict = Depends(_user()),
                  body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, equity
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            with _entity_write(conn, entity_id):
                # an amount missing or blank reaches the engine as None,
                # which it refuses as "amount must be a number"
                return equity.record_movement(
                    conn, entity_id, kind=body.text("kind", strip=False),
                    amount=body.money("amount"),
                    date=body.text("date") or None,
                    member_id=body.text("member_id") or None,
                    form=body.text("form") or None,
                    note=body.text("note") or None)
        except equity.FrontedExpenseConflict as e:
            raise HTTPException(409, str(e))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}/equity/{movement_id}")
@documented(response=Ok)
def equity_delete(entity_id: str, movement_id: str,
                  user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, equity
    conn = _conn(user)
    try:
        _require_business(conn)
        # archived entities are read-only, and these are the retention
        # records archiving exists to keep — only delete-forever may go
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        with _entity_write(conn, entity_id):
            if not equity.delete_movement(
                    conn, _require_record_id(movement_id, "movement"),
                    entity_id):
                raise HTTPException(404, "no such movement")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/doctor/unlinked-movements")
@documented(response=UnlinkedMovements)
def doctor_unlinked_movements(user: dict = Depends(_user())):
    """Capital contributions and reimbursements left behind by a charge
    deleted before deletes took them along — still counted, explaining
    nothing. Listed for the owner to remove by hand; none goes on its own."""
    from ..engine import equity
    conn = _conn(user)
    try:
        return {"movements": equity.unlinked_fronted(conn)}
    finally:
        conn.close()


@router.delete("/doctor/unlinked-movements/{movement_id}")
@documented(response=Ok)
def doctor_unlinked_movement_delete(movement_id: str,
                                    user: dict = Depends(_user())):
    """Remove one listed movement. Owner-only, as the other Doctor writes
    are. A movement still linked to its charge, or entered by hand, is not
    this door's to remove: the business's own equity list removes those."""
    demoguard.deny(user)
    _may_edit(user)
    if user.get("role") != "owner":
        raise HTTPException(403, "only the household owner can do this")
    from ..engine import equity
    conn = _conn(user)
    try:
        gone = equity.delete_unlinked_fronted(
            conn, _require_record_id(movement_id, "movement"))
        if gone is None:
            raise HTTPException(404, "no such unlinked movement")
        _activity.record(
            conn, user, "business", "removed", target=None,
            label=(f"{gone['kind']} of {_activity.money(gone['amount'])} "
                   f"for {gone['entity_name'] or 'a business'} whose "
                   "charge was deleted"),
            detail={"movement": {k: v for k, v in gone.items()
                                 if k != "entity_name"}})
        return {"ok": True}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/reimburse")
@documented(response=EquityMovement, body=EquityReimburse)
def equity_reimburse(entity_id: str, user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    """A business cost paid on a personal card → capitalize it (contribution)
    or mark it reimbursable, assigning the transaction to the entity either way."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, equity
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        txn_id = body.text("txn_id")
        mode = body.text("mode", strip=False)
        if not txn_id:
            raise HTTPException(400, "txn_id required")
        try:
            fn = (equity.contribute_expense if mode == "contribute"
                  else equity.reimburse_expense if mode == "reimburse"
                  else None)
            if fn is None:
                raise HTTPException(400, "mode must be contribute or reimburse")
            return fn(conn, entity_id, txn_id,
                      member_id=body.text("member_id") or None)
        except equity.FrontedExpenseConflict as e:
            # the expense is already explained for another business, or the
            # other way: a conflict with what is recorded, not a bad request
            raise HTTPException(409, str(e))
        except ValueError as e:
            # an archive that landed after the early check above
            raise HTTPException(409 if "archived" in str(e) else 400, str(e))
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/owner-transfer")
@documented(response=EquityMovement, body=OwnerTransferInput)
def equity_owner_transfer(entity_id: str, user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    """An owner transfer on a business account → recorded as a contribution
    (money in) or a draw (money out), at the row's amount and date."""
    demoguard.deny(user)
    _may_edit(user)
    _require_entity_id(entity_id)
    from ..engine import equity
    conn = _conn(user)
    try:
        _require_business(conn)
        txn_id = body.text("txn_id")
        if not txn_id:
            raise HTTPException(400, "txn_id required")
        try:
            with _entity_write(conn, entity_id):
                return equity.record_owner_transfer(conn, entity_id, txn_id)
        except equity.FrontedExpenseConflict as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            msg = str(e)
            raise HTTPException(409 if "archived" in msg
                                else 404 if msg.startswith("no such")
                                else 400, msg)
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/suggestions")
@documented(response=LineSuggestions)
def business_suggestions(entity_id: str, user: dict = Depends(_user())):
    """Proposed Schedule C lines for unclassified business transactions.

    SUGGEST-ONLY by design. The personal side
    auto-applies a learned merchant category because a wrong one is
    cosmetic; a wrong Schedule C line is a wrong number on a filed return,
    so nothing is written until a human confirms. `source` tells the caller
    where each came from: "merchant" is this entity's own prior decision,
    "kind" the vendor's business kind (books.suggest_lines); the bank's
    category is never one.
    """
    _require_entity_id(entity_id)
    from ..engine import books
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"lines": books.SCHED_C_LINES,
                "suggestions": books.suggest_lines(conn, entity_id),
                # the true total — `suggestions` is capped, so counting it
                # under-reports a large backlog
                "unclassified": books.unclassified_count(conn, entity_id)}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/transactions")
@documented(response=BizTxns)
def business_txns(entity_id: str, user: dict = Depends(_user()),
                  year: int | None = None,
                  limit: int | None = Query(None, ge=1, le=1000),
                  offset: int = Query(0, ge=0, le=10_000_000)):
    """The entity's worksheet rows, newest first. With no `limit` the reply
    is every row, the shape older clients read. With `limit` (and
    `offset`) it is one page plus `total`, every row in scope — the
    effective bucket of a refund on the page is still decided against the
    entity's whole history, so paging never changes a row's bucket."""
    _require_entity_id(entity_id)
    _ck_year(year, "year")
    from ..engine import books
    conn = _conn(user)
    try:
        _require_business(conn)
        rows = books.entity_transactions(conn, entity_id, year)
        if limit is None and not offset:
            return {"transactions": rows}
        page = rows[offset:offset + limit] if limit else rows[offset:]
        return {"transactions": page, "total": len(rows),
                "limit": limit, "offset": offset}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/transactions/{txn_id:path}/class")
@documented(response=Ok, body=TxnClass)
def business_classify(entity_id: str, txn_id: str,
                      user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import books, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)   # archived = read-only
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        # A body that names `sched_c_line` sets it, and null or "" clears
        # it; a body without the key (the Books tab's bucket change) keeps
        # whatever line is stored.
        names_line = body.present("sched_c_line")
        line = body.text("sched_c_line") or None
        # free text bound straight into the row: anything but a string is
        # the client's mistake (400), never a driver error (500)
        note = body.text("note") or None
        # the request is judged as sent, even when the stored bucket wins
        bucket = body.one_of("bucket", books.BUCKETS, required=True,
                             msg=f"bucket must be one of {books.BUCKETS}")
        try:
            with conn.transaction():
                # The review queue lists pending holds, and the sync may
                # post one while this request is in flight: the row that
                # stands for it now is found, and locked, under the lock
                # the settlement holds from its carry to its retire. The
                # class then lands on the row every reader counts, never
                # on a retired hold after its one-time carry has run.
                live = entities.live_txn_id(conn, txn_id)
                # The class row is keyed by txn alone, so the URL's entity
                # must own the transaction — otherwise entity A's endpoint
                # could classify (and rewrite) entity B's rows, archived
                # ones included. Checked on the live row, under its lock.
                if live is None or not books.txn_belongs_to_entity(
                        conn, entity_id, live):
                    raise HTTPException(
                        404, "no such transaction for this entity")
                txn_id = live
                # and the entity checked again under this transaction, after
                # the row lock (entities.require_active gives the order), so
                # an archive cannot land between the check and the class
                try:
                    entities.require_active(conn, entity_id)
                except ValueError as _e:
                    raise HTTPException(
                        409 if "archived" in str(_e) else 404, str(_e))
                # A save that carries a Schedule C line is the review
                # queue's confirm, and its bucket is the queue's date-derived
                # default for a row that had none. A bucket chosen since (on
                # the Books tab, which sends a bucket alone, or on another
                # device) is the person's decision and stays. Both saves
                # hold the transaction row's lock (taken by live_txn_id
                # above), so a queue confirm and a bucket change cannot
                # interleave and the later write cannot undo the earlier one.
                if line is not None:
                    have = conn.execute(
                        "SELECT bucket FROM business_txn_class "
                        "WHERE txn_id=%s", (txn_id,)).fetchone()
                    if have is not None and have["bucket"]:
                        bucket = have["bucket"]
                books.classify(
                    conn, txn_id, bucket,
                    sched_c_line=line if names_line else books.KEEP,
                    note=note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/pnl")
@documented(response=Pnl)
def business_pnl(entity_id: str, user: dict = Depends(_user()),
                 year: int | None = None):
    _ck_year(year, "year")
    from ..engine import books, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        lifetime = books.entity_transactions(conn, entity_id, None)
        out = books.pnl(conn, entity_id, year, lifetime=lifetime)
        # the Business overview's tiles: this month's profit under the
        # year's, by the same rules, and the cash in the business
        month = _today(conn).strftime("%Y-%m")
        out["this_month"] = books.period_profit(conn, entity_id, month,
                                                lifetime=lifetime)
        out["cash"] = books.entity_cash(conn, entity_id)
        return out
    finally:
        conn.close()


def _year_end_sheet(conn, entity_id: str, year: int | None,
                    today: dt.date, lifetime: list[dict]) -> dict:
    """The balance sheet as of December 31 of `year`, for the year-end
    package. With no year, or a year still in progress, it is today's
    sheet and says so.

    No balance history is recorded, so a past year-end balance is rebuilt:
    today's balance with every posted transaction dated after the year end
    undone. That only holds when the account's ledger reaches back to the
    year end, and only for cash and debt — an investment account's value
    moved with the market, which no transaction records. Any other account
    is listed without an amount and left out of the totals, with the
    reason, rather than passed off as a year-end figure.

    The capital account counts the movements dated on or before the year
    end and the net income of every year up to and including it."""
    from ..engine import books, reporting, selfemploy
    if not year or year >= today.year:
        sheet = selfemploy.balance_sheet(conn, entity_id)
        sheet["as_of"] = today.isoformat()
        sheet["as_of_note"] = ("balances as of the download date — the "
                               "year is not over")
        sheet["unknown"] = []
        return sheet
    as_of = dt.date(year, 12, 31)
    rows = conn.execute(
        f"""SELECT COALESCE(a.display_name, a.name) AS name, a.type,
                   a.balance_current AS bal,
                   (SELECT MIN(t.date) FROM transactions t
                     WHERE t.account_id = a.id AND t.removed = 0) AS first,
                   (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
                     WHERE t.account_id = a.id AND t.removed = 0
                       AND COALESCE(t.pending, 0) = 0
                       AND t.date > %s) AS after
            FROM accounts a WHERE a.entity_id = %s
              AND a.balance_current IS NOT NULL
              {reporting._NOT_SHADOW.format(col="a.id")}""",
        (as_of, entity_id)).fetchall()
    assets, liabilities, unknown = [], [], []
    ta = tl = 0.0
    for r in rows:
        if r["type"] == "investment":
            unknown.append({"name": r["name"], "reason":
                            "market value at year end is not recorded"})
            continue
        if r["first"] is None or r["first"] > as_of:
            unknown.append({"name": r["name"], "reason":
                            "transaction history starts after year end"})
            continue
        # amounts are positive for money out: undoing a later charge puts
        # it back into a cash balance and takes it off a debt
        if r["type"] in ("credit", "loan"):
            bal = float(r["bal"]) - float(r["after"])
            liabilities.append({"name": r["name"], "amount": round(bal, 2)})
            tl += bal
        else:
            bal = float(r["bal"]) + float(r["after"])
            assets.append({"name": r["name"], "amount": round(bal, 2)})
            ta += bal
    from ..engine import equity
    # the same movements the live capital account counts: one fronted for
    # a charge the bank feed withdrew explains a cost the books skip
    by = {m["kind"]: float(m["s"]) for m in conn.execute(
        f"""SELECT m.kind, COALESCE(SUM(m.amount), 0) AS s
              FROM equity_movement m
             WHERE m.entity_id = %s AND m.date <= %s
               AND {equity.COUNTS_IN_CAPITAL}
             GROUP BY m.kind""",
        (entity_id, as_of)).fetchall()}
    upto = [t for t in lifetime
            if (books._row_date(t) or as_of) <= as_of]
    # the same retained earnings the live capital account counts, start-up
    # allowance deducted through this year included
    net_income = books.net_income_through(conn, entity_id, year, upto,
                                         lifetime)
    # costs paid personally by the year end count as contributions, as
    # they do in the live capital account (books.personally_paid)
    capital = (by.get("contribution", 0.0)
               + books.personally_paid(lifetime, through=as_of) + net_income
               - by.get("draw", 0.0) - by.get("distribution", 0.0))
    return {"assets": assets, "liabilities": liabilities,
            "total_assets": round(ta, 2), "total_liabilities": round(tl, 2),
            "net": round(ta - tl, 2), "capital_account": round(capital, 2),
            "as_of": as_of.isoformat(),
            "as_of_note": ("year-end balances, rebuilt from today's balance "
                           "and the transactions since"),
            "unknown": unknown}


@router.get("/business/entities/{entity_id}/package.zip")
def business_year_package(entity_id: str, request: Request,
                          user: dict = Depends(_user()),
                          year: int | None = None):
    """Everything a CPA asks for, in one file.

    One button producing P&L, balance sheet, 1099s, mileage log and the
    categorized ledger. Every part exists as its own endpoint — the work is
    the BUNDLING, because the failure mode is not a missing report, it is a
    person exporting four things separately in March and missing the
    fifth.

    Deliberately a plain zip of CSVs, not a rendered document: a CPA wants
    numbers they can open in a spreadsheet and re-foot, not a PDF they have
    to retype. A README states what the figures are and, importantly, what
    they are not.

    Owner-only: it carries the entity's categorized ledger, so it is the
    P&L CSV's door at a larger size.
    """
    _same_site_download(request)
    _owner_only(user)
    import io as _io
    import zipfile as _z

    from fastapi.responses import Response
    from ..engine import books, entities, selfemploy
    _ck_year(year, "year")
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        today = _today(conn)
        # the package is ONE tax year's, every file in it: with no year
        # asked for, the household's current one (not the instance
        # clock's). The 1099 and mileage figures only exist per year, so an
        # all-years ledger or trip log beside them would not add up.
        year = year or today.year
        txns = books.entity_transactions(conn, entity_id, year)
        pnl = books.pnl(conn, entity_id, year, txns=txns)
        # its balance sheet and capital account stand at that year's end,
        # not at the day it was downloaded
        sheet = _year_end_sheet(
            conn, entity_id, year, today,
            books.entity_transactions(conn, entity_id, None))
        vendors = selfemploy.vendor_totals(conn, entity_id, year)
        trips = selfemploy.list_trips(conn, entity_id, year)
        miles = selfemploy.mileage_deduction(conn, entity_id, year)
    finally:
        conn.close()

    def _csv(rows) -> str:
        buf = _io.StringIO()
        w = _csv_writer(buf)
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    period = str(year)
    name = _csv_safe(ent["name"])
    buf = _io.BytesIO()
    with _z.ZipFile(buf, "w", _z.ZIP_DEFLATED) as z:
        z.writestr("README.txt",
                   f"Oikonome year-end package\n"
                   f"Business: {name} ({ent.get('structure', '')})\n"
                   f"Period:   {period}\n\n"
                   "Contents\n"
                   "  profit-and-loss.csv  revenue, operating expenses, net,\n"
                   "                       and the startup/organizational split\n"
                   "  ledger.csv           every business transaction with its\n"
                   "                       bucket and Schedule C line; Net\n"
                   "                       amount (what was not paid back)\n"
                   "                       is what the P&L adds up\n"
                   "  balance-sheet.csv    assets, liabilities, capital account\n"
                   f"                       as of {sheet['as_of']} "
                   f"({sheet['as_of_note']})\n"
                   "  vendors-1099.csv     paid vendors: total paid, the part\n"
                   "                       paid by card (reported on a 1099-K,\n"
                   "                       not a 1099-NEC) and the 1099-NEC\n"
                   "                       amount the threshold is judged on\n"
                   "  mileage.csv          trip log and the standard-rate\n"
                   "                       deduction it implies\n\n"
                   "These figures are bookkeeping output, NOT tax advice or a\n"
                   "filed return. Deductibility, capitalization, meal limits,\n"
                   "vehicle and home-office method are the preparer's call.\n"
                   "Every ledger row says what it is in its Schedule C\n"
                   "line column: the line of an operating cost; the\n"
                   "organizational (248) or start-up (195) bucket of a\n"
                   "capitalized cost, which is not also deducted on a line;\n"
                   "an owner contribution or draw; a transfer between the\n"
                   "business's accounts. An operating cost with no line yet\n"
                   "reads \"Uncategorized — needs a line\" and needs a\n"
                   "decision before filing. Business costs paid on a\n"
                   "personal card count in the capital account as money\n"
                   "the owner put in.\n")
        z.writestr("profit-and-loss.csv", _csv([
            ["Business", name], ["Period", period], [],
            ["Line", "Amount"],
            ["Revenue", f'{pnl["revenue"]:.2f}'],
            ["Operating expenses", f'{pnl["operating_expenses"]:.2f}'],
            ["Net operating profit", f'{pnl["net_operating"]:.2f}'],
            ["Organizational costs (248)", f'{pnl["organizational"]:.2f}'],
            ["Start-up costs (195)", f'{pnl["startup_195"]:.2f}'],
            [],
            ["Schedule C line", "Amount"],
            # The keys are Schedule C lines, or books.UNLINED for the
            # operating costs that have none yet — never a raw category.
            # Still through _csv_safe, like every cell of a file built to
            # be opened by an accountant's Excel.
        ] + [[_csv_safe(str(k)), f"{v:.2f}"] for k, v in
             sorted((pnl.get("operating_by_line") or {}).items())]))
        z.writestr("ledger.csv", _csv([
            ["Date", "Payee", "Amount", "Net amount", "Bucket",
             "Schedule C line", "Category"]] + [
            # every free-text cell goes through _csv_safe — `category` carries
            # the user's own category_override verbatim, and this file is built
            # to be handed to an accountant, so the person who opens it in Excel
            # is not the person who typed it. "Net amount" is what the P&L
            # counts (what was not paid back); "Amount" is what the bank
            # recorded — without the net column the rows under a Schedule C
            # line would not add up to that line.
            [str(x.get("date") or ""), _csv_safe(x.get("payee") or ""),
             f'{float(x.get("amount") or 0):.2f}',
             f'{float(books._net(x) or 0):.2f}',
             _csv_safe(x.get("effective_bucket") or ""),
             _csv_safe(books.ledger_line(x)),
             _csv_safe(x.get("category") or "")] for x in txns]))
        z.writestr("balance-sheet.csv", _csv(
            [["As of", sheet["as_of"]], ["Basis", sheet["as_of_note"]], []]
            + [["Assets", "Amount"]]
            + [[_csv_safe(a["name"]), f'{a["amount"]:.2f}']
               for a in sheet.get("assets", [])]
            + [["Total assets", f'{sheet.get("total_assets", 0):.2f}'], []]
            + [["Liabilities", "Amount"]]
            + [[_csv_safe(l["name"]), f'{l["amount"]:.2f}']
               for l in sheet.get("liabilities", [])]
            + [["Total liabilities", f'{sheet.get("total_liabilities", 0):.2f}'],
               [], ["Net", f'{sheet.get("net", 0):.2f}'],
               ["Capital account", f'{sheet.get("capital_account", 0):.2f}']]
            # why capital and net differ, in the page's own sentences
            + [[line] for line in sheet.get("reconcile") or []]
            # an account whose year-end balance cannot be rebuilt is named,
            # with why, instead of being counted at today's figure
            + ([[], ["Not included", "Why"]]
               + [[_csv_safe(u["name"]), u["reason"]]
                  for u in sheet["unknown"]] if sheet["unknown"] else [])))
        # No column may be headed as the whole when it holds a part: a
        # preparer reads "Total paid" as everything and subtracts the card
        # column from it. So the total is the true total, and the form
        # amount is its own column.
        z.writestr("vendors-1099.csv", _csv(
            [["Vendor", "Total paid", "Paid by card (not 1099-NEC)",
              "1099-NEC amount", "Marked reportable"]]
            + [[_csv_safe(v.get("merchant") or ""),
                f'{float(v.get("paid") or 0) + float(v.get("paid_by_card") or 0):.2f}',
                f'{float(v.get("paid_by_card") or 0):.2f}',
                f'{float(v.get("paid") or 0):.2f}',
                "yes" if v.get("reportable") else ""] for v in vendors]))
        z.writestr("mileage.csv", _csv(
            [["Date", "Miles", "Purpose", "Note"]]
            + [[str(x.get("date") or ""), x.get("miles"),
                _csv_safe(x.get("purpose") or ""),
                _csv_safe(x.get("note") or "")] for x in trips]
            + [[], ["Total miles", miles.get("miles")]]
            # a year can carry two IRS rates; list each with its miles
            + [["Rate", r.get("rate"), r.get("miles")]
               for r in (miles.get("rates") or [{"rate": miles.get("rate")}])]
            + [["Deduction", f'{float(miles.get("deduction") or 0):.2f}']]))
    fn = f"oikonome-{period}-{entity_id[:8]}.zip"
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@router.get("/business/entities/{entity_id}/pnl.csv")
def business_pnl_csv(entity_id: str, request: Request,
                     user: dict = Depends(_user()),
                     year: int | None = None):
    """A CPA-consumable export: entity, P&L summary, and every classified
    line. Owner-only, as the ledger download is."""
    _same_site_download(request)
    _owner_only(user)
    import io as _io

    from fastapi.responses import Response
    from ..engine import books, entities
    _ck_year(year, "year")
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        txns = books.entity_transactions(conn, entity_id, year)
        p = books.pnl(conn, entity_id, year, txns=txns)   # reuse, don't re-query
        # a past year's export states the capital account at that year's
        # end, like the year-end package — not today's running balance
        capital = (p["capital"]["capital_balance"] if year is None else
                   _year_end_sheet(conn, entity_id, year, _today(conn),
                                   books.entity_transactions(
                                       conn, entity_id, None))
                   ["capital_account"])
    finally:
        conn.close()
    buf = _io.StringIO()
    w = _csv_writer(buf)
    w.writerow(["Oikonome business books export", _csv_safe(ent["name"]),
                _csv_safe(ent.get("structure", "")),
                f"EIN •••••{ent.get('ein_last4') or ''}"])
    w.writerow(["Period", year or "all years"])
    w.writerow([])
    w.writerow(["Summary", "Amount"])
    w.writerow(["Revenue", f'{p["revenue"]:.2f}'])
    w.writerow(["Operating expenses", f'{p["operating_expenses"]:.2f}'])
    w.writerow(["Net operating profit", f'{p["net_operating"]:.2f}'])
    w.writerow(["Organizational costs (§248)", f'{p["organizational"]:.2f}'])
    w.writerow(["Start-up costs (§195)", f'{p["startup_195"]:.2f}'])
    w.writerow(["  §195 first-year deduction (est.)",
                f'{p["startup_deduction"]["immediate"]:.2f}'])
    w.writerow(["  §195 amortizable over 180 mo (est.)",
                f'{p["startup_deduction"]["amortizable"]:.2f}'])
    w.writerow(["Capital account balance", f'{capital:.2f}'])
    w.writerow([])
    # "Net amount" is what the summary above counts (a charge less what was
    # paid back); "Amount" is what the bank recorded. Both, so the rows
    # re-foot to the summary and still match the statement.
    w.writerow(["Date", "Payee", "Amount", "Net amount", "Bucket",
                "Schedule C line", "Category"])
    for t in txns:
        # bucket goes through _csv_safe like every other free-text cell (and
        # like the same field in the year-end package): the column is plain
        # TEXT with the three-value taxonomy enforced in application code
        # only, and the full-ledger restore writes it straight from an
        # imported CSV — so what arrives here is not guaranteed to be one of
        # the three, and this file is meant to be opened in a spreadsheet.
        w.writerow([t["date"], _csv_safe(t["payee"]), f'{t["amount"]:.2f}',
                    f'{books._net(t):.2f}',
                    _csv_safe(t.get("effective_bucket") or ""),
                    _csv_safe(books.ledger_line(t)),
                    _csv_safe(t["category"] or "")])
    name = f"oikonome-books-{_filename_safe(ent['name'], 'entity')}-{year or 'all'}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{name}"'})


@router.get("/business/entities/{entity_id}/compliance")
@documented(response=Obligations)
def compliance_list(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        # the household's own day: on a UTC instance a deadline due today
        # would drop off the list hours early, in the evening of its due date
        return {"obligations": compliance.obligations(
            conn, ent, today=_today(conn))}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/compliance")
@documented(response=Created, body=ObligationInput)
def compliance_add(entity_id: str, user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            with _entity_write(conn, entity_id):
                # recurrence left out is yearly; sent as null or blank it is
                # refused by the engine, as it always was
                return compliance.add_obligation(
                    conn, entity_id, title=body.text("title"),
                    due_date=body.text("due_date") or None,
                    recurrence=(body.text("recurrence", strip=False)
                                if body.present("recurrence") else "yearly"),
                    fee=body.money("fee", msg="fee must be a number"),
                    url=body.text("url") or None,
                    note=body.text("note") or None)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}/compliance/{obligation_id}")
@documented(response=Ok)
def compliance_delete(entity_id: str, obligation_id: str,
                      user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        # archived entities are read-only, and these are the retention
        # records archiving exists to keep — only delete-forever may go
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        with _entity_write(conn, entity_id):
            if not compliance.delete_obligation(
                    conn, _require_record_id(obligation_id, "obligation"),
                    entity_id):
                raise HTTPException(404, "no such obligation")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/compliance.ics")
def compliance_ics(entity_id: str, request: Request,
                   user: dict = Depends(_user())):
    _same_site_download(request)
    _require_entity_id(entity_id)
    from fastapi.responses import Response
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        body = compliance.ics(conn, ent, today=_today(conn))
    finally:
        conn.close()
    return Response(body, media_type="text/calendar",
                    headers={"Content-Disposition":
                             'attachment; filename="oikonome-compliance.ics"'})


# ---- self-employed tax pack -----------------------------------------

@router.get("/business/entities/{entity_id}/estimated-tax")
@documented(response=EstimatedTax)
def se_estimated_tax(entity_id: str, user: dict = Depends(_user()),
                     year: int | None = None):
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        _ck_year(year, "year")
        # the household's day decides which tax year is in progress and how
        # much of it has elapsed; the instance clock rolls the year early
        est = selfemploy.estimated_tax(conn, entity_id, year,
                                       today=_today(conn))
        est["set_aside"] = selfemploy.tax_set_aside(conn, entity_id, est)
        return est
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/balance-sheet")
@documented(response=BalanceSheet)
def se_balance_sheet(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        return selfemploy.balance_sheet(conn, entity_id)
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/mileage")
@documented(response=Mileage)
def se_mileage_list(entity_id: str, user: dict = Depends(_user()),
                    year: int | None = None):
    _ck_year(year, "year")
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        return {"trips": selfemploy.list_trips(conn, entity_id, year),
                "summary": selfemploy.mileage_deduction(
                    conn, entity_id, year or _today(conn).year)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/mileage")
@documented(response=Created, body=TripInput)
def se_mileage_add(entity_id: str, user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            with _entity_write(conn, entity_id):
                # miles missing or blank reach the engine as None, which it
                # refuses as "miles must be a number"
                return selfemploy.add_trip(
                    conn, entity_id, date=body.text("date") or None,
                    miles=body.money("miles", msg="miles must be a number"),
                    purpose=body.text("purpose") or None,
                    note=body.text("note") or None)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}/mileage/{trip_id}")
@documented(response=Ok)
def se_mileage_delete(entity_id: str, trip_id: str,
                      user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        # archived entities are read-only, and these are the retention
        # records archiving exists to keep — only delete-forever may go
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        with _entity_write(conn, entity_id):
            if not selfemploy.delete_trip(
                    conn, _require_record_id(trip_id, "trip"), entity_id):
                raise HTTPException(404, "no such trip")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/vendors")
@documented(response=Vendors)
def se_vendors(entity_id: str, user: dict = Depends(_user()),
               year: int | None = None):
    _ck_year(year, "year")
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        # the year's 1099-NEC line, so the panel states the figure its
        # flags were measured against instead of a hard-coded one
        yr = year or _today(conn).year
        return {"vendors": selfemploy.vendor_totals(conn, entity_id, yr),
                "threshold": selfemploy.reportable_1099_threshold(yr)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/vendors")
@documented(response=Ok, body=VendorMark)
def se_vendor_mark(entity_id: str, user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        merchant = body.text("merchant")
        if not merchant:
            raise HTTPException(400, "merchant required")
        keep_tin = not body.present("tin_last4")
        # a TIN sent as a bare JSON number has always been taken (only its
        # digits are kept); a bool, a list or an object never was
        tin = body.text_or_int("tin_last4", strip=False,
                               msg="tin_last4 must be a string")
        # left out, the payee is marked reportable; sent as null it has
        # always read as false
        reportable = (bool(body.flag("reportable"))
                      if body.present("reportable") else True)
        try:
            with _entity_write(conn, entity_id):
                if keep_tin:
                    # a toggle that does not speak of the TIN keeps the one
                    # on file, read under the same write lock as the save;
                    # only an explicit "" or null clears it
                    tin = selfemploy.vendor_tin(conn, entity_id, merchant)
                selfemploy.mark_vendor(
                    conn, entity_id, merchant,
                    reportable=reportable, tin_last4=tin)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/summary")
@documented(response=BusinessSummary)
def business_summary(user: dict = Depends(_user())):
    """SPA header state: entity count, unassigned legacy-flag count, combined
    toggle — so the Business section can render its empty/reconcile/combined UI."""
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ents = entities.list_entities(conn)
        return {"entities": ents,
                "flagged_unassigned": entities.count_flagged(conn),
                "combined": entities.is_combined(conn)}
    finally:
        conn.close()


@router.post("/business/combine")
@documented(response=Combined, body=Combine)
def business_combine(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    """Toggle the combined view (personal aggregates include business)."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"combined": entities.set_combined(
            conn, bool(body.flag("on")))}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/import-flags")
@documented(response=FlagsImported)
def entities_import_flags(entity_id: str, user: dict = Depends(_user())):
    """Reconcile the legacy business_flags tags into this entity."""
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        with _entity_write(conn, entity_id):
            moved = entities.import_flagged_transactions(conn, entity_id)
        return {"ok": True, "moved": moved}
    finally:
        conn.close()


@router.post("/accounts/{account_id}/entity")
@documented(response=AccountEntitySet, body=EntityAssign)
def account_assign_entity(account_id: str, user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    """Assign a whole account to a business entity (or null → personal)."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        eid = body.text("entity_id") or None
        if eid:                          # assigning INTO an entity is a write —
            try:                         # an archived entity is read-only
                entities.require_active(conn, eid)
            except ValueError as _e:
                raise HTTPException(409 if "archived" in str(_e) else 404,
                                    str(_e))
        try:                             # …and so is moving OUT of one
            if not entities.assign_account(conn, account_id, eid):
                raise HTTPException(404, "no such account")
        except ValueError as _e:
            raise HTTPException(409, str(_e))
        return {"ok": True, "account_id": account_id, "entity_id": eid}
    finally:
        conn.close()


def _refuse_moving_a_fronted_expense(conn, txn_id: str,
                                     entity_id: str | None) -> None:
    """A capitalized or reimbursable expense belongs to the one entity its
    movement names. Moving the row elsewhere (or back to personal) would
    leave that entity counting a contribution or a debt for a cost its books
    no longer carry, so the move is refused until the movement is deleted —
    the same rule the capitalize/reimburse doors apply. Takes the row lock
    those doors take, so the check and the move cannot interleave with a
    concurrent pick. Raises equity.FrontedExpenseConflict (a ValueError)."""
    from ..engine import equity
    row = conn.execute(
        "SELECT a.entity_id AS acct FROM transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id "
        "WHERE t.id = %s AND t.removed = 0 FOR UPDATE OF t",
        (txn_id,)).fetchone()
    if row is None:                   # gone, or off the ledger with no posted
        return                        # row: assign_transaction answers the 404
    after = entity_id or row["acct"]
    after = str(after) if after else None
    p = conn.execute(
        """SELECT m.kind, m.entity_id, e.name AS entity_name
             FROM equity_movement m
             LEFT JOIN business_entity e ON e.id = m.entity_id
            WHERE m.txn_id = %s AND m.kind = ANY(%s)
              AND m.entity_id::text IS DISTINCT FROM %s
            ORDER BY m.created_at, m.id LIMIT 1""",
        (txn_id, list(equity.FRONTED_KINDS), after)).fetchone()
    if p is not None:
        how = ("capitalized" if p["kind"] == "contribution"
               else "marked reimbursable")
        raise equity.FrontedExpenseConflict(
            f"that expense is {how} for "
            f"{p['entity_name'] or 'another business'}: delete that "
            "movement first")


@router.post("/transactions/{txn_id:path}/entity")
@documented(response=TxnEntitySet, body=EntityAssign)
def txn_assign_entity(txn_id: str, user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    """Per-transaction business override (or null → clear)."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        eid = body.text("entity_id") or None
        if eid:                          # assigning INTO an entity is a write —
            try:                         # an archived entity is read-only
                entities.require_active(conn, eid)
            except ValueError as _e:
                raise HTTPException(409 if "archived" in str(_e) else 404,
                                    str(_e))
        try:                             # …and so is moving OUT of one
            with conn.transaction():
                # the pairing lock before the row lock, the order the
                # settlement takes them in; and the check reads the row the
                # move will land on, which for a hold that has since posted
                # is the posted charge
                txn_id = entities.live_txn_id(conn, txn_id) or txn_id
                _refuse_moving_a_fronted_expense(conn, txn_id, eid)
                if not entities.assign_transaction(conn, txn_id, eid):
                    raise HTTPException(404, "no such transaction")
        except ValueError as _e:
            raise HTTPException(409, str(_e))
        return {"ok": True, "txn_id": txn_id, "entity_id": eid}
    finally:
        conn.close()
