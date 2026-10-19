"""Plaid Hosted Link sessions on record, and finishing them server-side.

A new bank link ends with a public token Plaid hands back when the person
finishes in the bank's window. Exchanging it is what creates the Item in
this instance, so finishing an ADD session must not depend on a client
poll being present at the moment the person finishes: a closed or
reloaded tab, an app dropped during an OAuth hop, a web restart, or a
client that gives up before the Hosted Link URL expires would otherwise
leave an Item at Plaid — billed — that no stored access token can remove.

So every session is written to ``plaid_link_sessions`` when it starts, and
three doors can finish an ADD session, all through :func:`complete_add`
under the household's sync lock, so exactly one of them exchanges each
public token:

* the client's status poll (``web/pages.plaid_link_status``);
* the Plaid LINK webhook (SESSION_FINISHED / ITEM_ADD_RESULT), which
  queues :func:`finish_session` on the worker;
* :func:`sweep`, every few minutes, over sessions still open — the floor
  for an instance with no webhook, or a webhook that never arrived.

Update-mode (re-auth) sessions are only ever finished by their poll: a
re-auth Plaid completes without us still repairs the Item, and the next
sync clears its error, so nothing is lost when nobody is watching.

Rows are read and written on the admin connection with the tenant named in
every WHERE clause (see migration 194 for why there is no RLS here)."""

from __future__ import annotations

import hashlib
import json
import logging
import time

from ..db import tenancy
from ..sync import plaid

log = logging.getLogger("oikonome.plaid")

# A session younger than this is left to its client's poll.
SWEEP_AFTER_S = 60
# The Hosted Link URL is good for 30 minutes (create_hosted_link asks for
# url_lifetime_seconds=1800) and a person already inside Link can take a
# while longer; past this an open session is taken as abandoned.
OPEN_FOR_S = 2 * 3600
# Finished and abandoned rows are deleted after this.
KEEP_S = 24 * 3600
# Plaid stamps finished_at when a person leaves Link without a bank, and the
# results can lag the stamp by a beat: an add session is called "exited"
# only once the stamp is this old with still nothing added.
EXIT_GRACE_S = 6


def token_key(public_token: str) -> str:
    """What a public token is recorded under: its hash, never the token."""
    return hashlib.sha256(public_token.encode()).hexdigest()


def record(tenant_id, link_token: str, kind: str, item_id: str | None,
           manage_accounts: bool, institution: str | None) -> bool:
    """Put a newly started session on record. Best-effort: a session that
    is not recorded still completes through its poll, as it always did."""
    try:
        admin = tenancy.admin_connect()
        try:
            n = admin.execute(
                "INSERT INTO plaid_link_sessions (link_token, tenant, kind, "
                "item_id, manage_accounts, institution) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (link_token) "
                # Plaid mints a fresh token per session, so a start naming
                # one already on record is that session starting over
                "DO UPDATE SET tenant=EXCLUDED.tenant, kind=EXCLUDED.kind, "
                "item_id=EXCLUDED.item_id, "
                "manage_accounts=EXCLUDED.manage_accounts, "
                "institution=EXCLUDED.institution, created_at=now(), "
                "exchanged='{}'::jsonb, done_at=NULL, reply=NULL "
                # …of the same household: a start can never move a session
                # on record to another one, whose finisher would then
                # exchange the first household's bank into it
                "WHERE plaid_link_sessions.tenant = EXCLUDED.tenant",
                (link_token, str(tenant_id), kind, item_id or None,
                 bool(manage_accounts), institution)).rowcount
        finally:
            admin.close()
        if not n:
            log.warning("plaid link session not recorded: its link token "
                        "is on record for another household")
            return False
        return True
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session not recorded: %s", e)
        return False


def forget(link_token: str, tenant_id) -> None:
    """Take a session off the record — one given up to make room under the
    institution cap, which must not be finished by any door afterwards."""
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_link_sessions "
                          "WHERE link_token=%s AND tenant=%s",
                          (link_token, str(tenant_id)))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session not forgotten: %s", e)


def set_aside(link_token: str, tenant_id) -> bool:
    """The person said they closed the bank window: the session stops
    holding a place under the institution cap and is no longer offered to
    a returning client, but stays on record, so if they did finish at
    Plaid after all the webhook or the sweep still exchanges the token —
    forgetting it would leave that Item billing with nothing here to
    remove it. True when an open add session was set aside."""
    try:
        admin = tenancy.admin_connect()
        try:
            n = admin.execute(
                "UPDATE plaid_link_sessions SET reply=%s "
                "WHERE link_token=%s AND tenant=%s AND kind='add' "
                "AND done_at IS NULL",
                (json.dumps({"cancelled": True}), link_token,
                 str(tenant_id))).rowcount
        finally:
            admin.close()
        return bool(n)
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session not set aside: %s", e)
        return False


def refuse_open_adds(admin, tenant_id, why: str) -> int:
    """Mark the household's add sessions still open as ones it may no
    longer keep a bank from: they stay on record — a person may still
    finish at the bank, and the session is the only handle on the Item
    that makes — and whichever door finishes one (the LINK webhook, the
    sweep, a poll) exchanges the token and hands the Item straight back,
    saying `why`. They are no longer offered to a returning client nor
    counted under the institution cap. Returns how many were marked."""
    return admin.execute(
        "UPDATE plaid_link_sessions SET reply = COALESCE(reply, "
        "'{}'::jsonb) || jsonb_build_object('refuse', %s::text) "
        "WHERE tenant=%s AND kind='add' AND done_at IS NULL",
        (why, str(tenant_id))).rowcount


def recent_open(tenant_id, within_s: float) -> set[str]:
    """This household's add sessions on record, still open and not set
    aside, started within `within_s` — what a web process that restarted
    since they opened must still count under the institution cap until
    their clients' polls bring them back into its memory. Empty on any
    failure: the exchange re-checks the cap either way."""
    try:
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                "SELECT link_token FROM plaid_link_sessions WHERE tenant=%s "
                "AND kind='add' AND done_at IS NULL AND reply IS NULL "
                "AND created_at > now() - make_interval(secs => %s)",
                (str(tenant_id), within_s)).fetchall()
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link sessions not read: %s", e)
        return set()
    return {r["link_token"] for r in rows}


def _row(admin, link_token: str, tenant_id) -> dict | None:
    return admin.execute(
        "SELECT link_token, tenant, kind, item_id, manage_accounts, "
        "institution, extract(epoch FROM created_at) AS at, exchanged, "
        "done_at, reply FROM plaid_link_sessions "
        "WHERE link_token=%s AND tenant=%s",
        (link_token, str(tenant_id))).fetchone()


def _as_session(row: dict) -> dict:
    """A recorded row in the shape of the in-memory session pages.py
    keeps (`_PLAID_LINK`)."""
    reply = row["reply"] or {}
    return {"tenant_id": str(row["tenant"]),
            "item_id": row["item_id"], "kind": reply.get("kind")
            or row["kind"], "manage_accounts": bool(row["manage_accounts"]),
            "institution": reply.get("institution") or row["institution"],
            "at": float(row["at"]), "url": None,
            "done": row["done_at"] is not None,
            "cancelled": row["done_at"] is None
            and bool(reply.get("cancelled")),
            # an open session the household may no longer finish into
            # itself (refuse_open_adds): whoever finishes it hands back
            "refuse": reply.get("refuse") if row["done_at"] is None
            else None,
            "reply": row["reply"],
            "exchanged": dict(row["exchanged"] or {})}


def load(link_token: str, tenant_id) -> dict | None:
    """The recorded session, as a session dict, or None — unknown, or not
    this tenant's."""
    try:
        admin = tenancy.admin_connect()
        try:
            row = _row(admin, link_token, tenant_id)
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session not read: %s", e)
        return None
    return _as_session(row) if row else None


def refresh(sess: dict, link_token: str) -> None:
    """Bring an in-memory session up to what is recorded: another door
    (the webhook, the sweep, a poll served before a restart) may have
    exchanged its tokens or finished it. Call under the sync lock, before
    acting on the session. A session with no record is left as it is."""
    try:
        admin = tenancy.admin_connect()
        try:
            row = _row(admin, link_token, sess["tenant_id"])
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session not read: %s", e)
        return
    if not row:
        return
    sess.setdefault("exchanged", {}).update(row["exchanged"] or {})
    if row["done_at"] is None and (row["reply"] or {}).get("refuse"):
        sess["refuse"] = row["reply"]["refuse"]
    if row["done_at"] is not None and not sess.get("done"):
        reply = row["reply"] or {}
        sess["done"] = True
        sess["reply"] = reply
        sess["kind"] = reply.get("kind") or sess.get("kind")
        sess["institution"] = reply.get("institution") \
            or sess.get("institution")


def _save_exchange(link_token: str, tenant_id, key: str,
                   value: dict) -> None:
    """Record one public token's outcome the moment it is known, so no
    other door exchanges it again even if this one dies before finishing
    the session."""
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE plaid_link_sessions SET exchanged = exchanged || "
                "jsonb_build_object(%s::text, %s::jsonb) "
                "WHERE link_token=%s AND tenant=%s",
                (key, json.dumps(value), link_token, str(tenant_id)))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link exchange not recorded: %s", e)


def mark_done(sess: dict, link_token: str, reply: dict) -> dict:
    """Finish the session in memory and on record with `reply`, the answer
    every later poll gets. Returns `reply`."""
    sess["done"] = True
    sess["reply"] = reply
    sess["kind"] = reply.get("kind") or sess.get("kind")
    sess["institution"] = reply.get("institution") or sess.get("institution")
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE plaid_link_sessions SET done_at=now(), reply=%s "
                "WHERE link_token=%s AND tenant=%s AND done_at IS NULL",
                (json.dumps(reply), link_token, sess["tenant_id"]))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session finish not recorded: %s", e)
    return reply


def done_reply(sess: dict) -> dict:
    """What a finished session answers."""
    reply = dict(sess.get("reply") or {})
    reply.setdefault("kind", sess.get("kind"))
    reply.setdefault("institution", sess.get("institution"))
    reply["done"] = True
    return reply


def open_sessions(tenant_id) -> list[dict]:
    """This household's add sessions still able to finish, newest first —
    what a client that comes back resumes polling. A session the person
    set aside (:func:`set_aside`) is not offered again; the server still
    finishes it if Plaid reports a result."""
    admin = tenancy.admin_connect()
    try:
        rows = admin.execute(
            "SELECT s.link_token, s.kind, s.institution, s.created_at, "
            # the Item a relink session replaces, so a client that comes
            # back shows the row as the relink it is and holds that
            # bank's door
            "(SELECT i.id FROM items i WHERE i.tenant_id = s.tenant "
            " AND i.raw->>'relink_session' = s.link_token LIMIT 1) "
            " AS relink_of "
            "FROM plaid_link_sessions s WHERE s.tenant=%s "
            "AND s.done_at IS NULL AND s.reply IS NULL "
            "AND s.kind='add' AND s.created_at > now() - make_interval("
            "secs => %s) ORDER BY s.created_at DESC LIMIT 5",
            (str(tenant_id), OPEN_FOR_S)).fetchall()
    finally:
        admin.close()
    return [{"link_token": r["link_token"], "kind": r["kind"],
             "institution": r["institution"],
             "relink_of": r["relink_of"],
             "started_at": r["created_at"].isoformat()} for r in rows]


def _held_by_session(conn, lsid: str | None, settled: dict) -> str | None:
    """An Item this household holds that Plaid session `lsid` created and
    no settled token already accounts for — the answer for a token whose
    exchange committed its row but whose outcome was never recorded (the
    finisher died before it could)."""
    if not lsid:
        return None
    claimed = {v.get("item_id") for v in settled.values()
               if isinstance(v, dict)}
    for r in conn.execute(
            "SELECT id FROM items WHERE link_session_id=%s "
            "AND aggregator='plaid' ORDER BY id", (lsid,)).fetchall():
        if r["id"] not in claimed:
            return r["id"]
    return None


def _hand_back(conn, client, tenant_id, public_token: str,
               why: str) -> dict:
    """Exchange a public token for a household that may not keep a new
    Item (no longer active, or held read-only) and remove the Item at once.
    Left unexchanged, the Item the person authorised would bill at Plaid
    with no access token anywhere able to remove it. A removal that fails
    is queued for the nightly retry. Returns the token's outcome."""
    try:
        ex = client.exchange_public_token(public_token)
    except plaid.PlaidTransportError:
        raise
    except plaid.PlaidError as e:
        return e.public()
    item_id = ex["item_id"]
    try:
        # the Item was created all the same, and takes its plan slot
        plaid._record_slot(conn, item_id, client=client)
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid slot ledger skipped item=%s: %s", item_id, e)
    try:
        client.remove_item(ex["access_token"])
    except Exception as e:                                 # noqa: BLE001
        log.warning("item/remove after a refused link failed item=%s: %s",
                    item_id, e)
        plaid.queue_release(tenant_id, item_id, ex["access_token"], client,
                            "link-refused",
                            getattr(e, "code", None) or type(e).__name__)
    else:
        plaid.stamp_ledger_removed(item_id, "link refused")
    return {"error": why}


def complete_add(conn, sess: dict, link_token: str,
                 sessions: list, refuse: str | None = None,
                 client=None, defer_sync: list | None = None) -> dict | None:
    """Exchange every public token Plaid reports for this session, and
    finish the session with the answer. None when Plaid reports no added
    Item yet. Call under the household's sync lock, after :func:`refresh`.

    A public token is single-use, so each one's outcome is recorded the
    moment it is known and reused after that — a second exchange could
    only fail, and that failure would hide the first answer. The one
    exception is a transport failure AT the exchange: Plaid's answer was
    lost, so whether the token was spent is unknown, and the session stays
    open for the next look to try again (a spent token then answers
    INVALID_PUBLIC_TOKEN). Once a token has
    created an Item, nothing that follows can turn the answer into a
    failure: the outcome is recorded the moment the items row commits,
    before the first sync, and a spent token whose outcome was lost is
    answered by the Item its Plaid session created, when there is one.

    `refuse` (with the household's `client`) is the household that may not
    keep a new Item: each token is exchanged and its Item handed straight
    back, and the session finishes as a failed link saying why.

    `defer_sync`, a list, leaves each new Item's first pull to the caller:
    the Item is saved and its id appended, and the answer says the first
    sync is pending. The caller queues the pull once it has let go of the
    sync lock (the web poll; see plaid.link_item)."""
    add_results = [
        (s.get("link_session_id"), r) for s in sessions
        for r in ((s.get("results") or {}).get("item_add_results") or [])
        if isinstance(r, dict) and r.get("public_token")]
    if not add_results:
        return None
    if refuse is None and sess.get("refuse"):
        # the session was marked while open (refuse_open_adds): every door
        # that finishes it hands its Items back, the poll included
        refuse = sess["refuse"]
        if client is None:
            client = plaid.Client.for_tenant(conn)
    settled = sess.setdefault("exchanged", {})
    names, err, retry, pending = [], None, False, False
    # a relink for a wider history window: the Item this session
    # replaces, bound in memory at the start and on the old row itself
    # (items.raw.relink_session), so a finisher after a restart — the
    # webhook, the sweep — settles it the same way
    relink_of = sess.get("relink_of") or _relink_of(conn, link_token)
    for lsid, r in add_results:
        key = token_key(r["public_token"])
        got = settled.get(key)
        if got is None and refuse is not None:
            try:
                got = _hand_back(conn, client, sess["tenant_id"],
                                 r["public_token"], refuse)
            except plaid.PlaidTransportError as e:
                log.warning("plaid link exchange got no answer: %s", e)
                retry = True
                continue
            settled[key] = got
            _save_exchange(link_token, sess["tenant_id"], key, got)
        if got is None:
            outcome: dict = {}

            def saved(iid, key=key):
                # the settle point: the row is committed, so from here no
                # door may exchange this token again, whatever happens to
                # this one during the first sync
                rec = {"item_id": iid, "first_sync": "pending"}
                settled[key] = rec
                _save_exchange(link_token, sess["tenant_id"], key, rec)
            try:
                # link_session_id rides along: it is the identifier Plaid
                # support asks for when a link is investigated
                iid = plaid.link_item(conn, r["public_token"],
                                      link_session_id=lsid, outcome=outcome,
                                      on_saved=saved,
                                      first_sync=defer_sync is None,
                                      relink_of=relink_of)
                if defer_sync is not None:
                    defer_sync.append(iid)
                got = {"item_id": iid,
                       "first_sync": outcome.get("first_sync", "ok")}
            except plaid.PlaidTransportError as e:
                # no answer to the exchange itself: unknown whether the
                # token was spent, so it is not settled
                log.warning("plaid link exchange got no answer: %s", e)
                retry = True
                continue
            except plaid.PlaidError as e:
                held = (_held_by_session(conn, lsid, settled)
                        if e.code == "INVALID_PUBLIC_TOKEN" else None)
                got = ({"item_id": held, "first_sync": "pending"} if held
                       else e.public())
            except plaid.LinkRefused as e:
                # refused after the exchange (institution cap, key swap,
                # bank-link hold, the row not saving) — link_item has
                # already handed the Item back to Plaid; the reason is
                # the typed sentence it chose, not the exception's text
                got = {"error": e.reason, "error_code": "LINK_REFUSED"}
            settled[key] = got
            _save_exchange(link_token, sess["tenant_id"], key, got)
        if got.get("item_id"):
            row = conn.execute(
                "SELECT institution_name FROM items WHERE id=%s",
                (got["item_id"],)).fetchone()
            names.append(row["institution_name"] if row else None)
            pending = pending or got.get("first_sync") == "pending"
        else:
            # the typed fields of the refusal (error, error_code and,
            # from Plaid, request_id) ride into the failed answer whole
            err = {k: v for k, v in got.items()
                   if k in ("error", "error_code", "request_id") and v}
    if retry and not names:
        return {"done": False, "kind": "busy"}
    institution = (", ".join(n for n in names if n)
                   or sess.get("institution"))
    if not names and err:
        return mark_done(sess, link_token,
                         {"done": True, "kind": "add_failed",
                          "institution": institution, **err})
    reply = {"done": True, "kind": "add", "institution": institution}
    if pending:
        reply["first_sync"] = "pending"
    return mark_done(sess, link_token, reply)


def _relink_of(conn, link_token: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM items WHERE aggregator='plaid' "
        "AND raw->>'relink_session' = %s "
        "AND COALESCE(status,'') <> 'archived'", (link_token,)).fetchone()
    return row["id"] if row else None


def exited(sess: dict, link_token: str, sessions: list,
           now: float | None = None) -> dict | None:
    """An ADD session Plaid stamped finished with nothing added — the
    person left Link without connecting a bank. Results can lag the stamp
    by a beat, so it takes a second look EXIT_GRACE_S later to call it;
    returns the finished reply then, else None."""
    if sess.get("item_id") or not any(s.get("finished_at")
                                       for s in sessions):
        return None
    now = now if now is not None else time.time()
    first = sess.get("finished_empty_at")
    if first is None:
        sess["finished_empty_at"] = now
        return None
    if now - first <= EXIT_GRACE_S:
        return None
    return mark_done(sess, link_token,
                     {"done": True, "kind": "exited",
                      "institution": sess.get("institution")})


ERASED = "this account is being deleted"


def hand_back_open_sessions(tenant_id, transport=None) -> dict:
    """Before a household's rows are erased: finish each of its add
    sessions still open, handing back every Item the person made at Plaid.

    The erasure deletes the session rows with the household, and a person
    who finished at the bank moments before would otherwise leave an Item
    that nothing on the instance can ever exchange or remove — billing at
    Plaid for good. Each public token Plaid reports is exchanged and its
    Item removed at once (a removal that fails is queued for the nightly
    retry, like any refused link); a token already exchanged is left to
    the release of the household's Items that follows.

    Called with the household's writer locks held by the erasure, which
    already exclude every other finisher (they try the sync lock and
    answer "busy"), so this does not take the sync lock itself. Never
    raises; returns {link_token: word} for the log."""
    out: dict[str, str] = {}
    try:
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                "SELECT link_token FROM plaid_link_sessions WHERE tenant=%s "
                "AND kind='add' AND done_at IS NULL ORDER BY created_at",
                (str(tenant_id),)).fetchall()
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link hand-back: sessions not read: %s", e)
        return out
    if not rows:
        return out
    try:
        conn = tenancy.tenant_connect(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link hand-back: no tenant connection: %s", e)
        return {r["link_token"]: "error" for r in rows}
    try:
        try:
            client = plaid.Client.for_tenant(conn, transport=transport)
        except plaid.PlaidError as e:
            log.warning("plaid link hand-back: no Plaid keys: %s", e)
            return {r["link_token"]: "error" for r in rows}
        for r in rows:
            lt = r["link_token"]
            try:
                sess = load(lt, tenant_id)
                if sess is None or sess["done"] or sess["item_id"]:
                    continue
                data = client.get_link_session(lt)
                reply = complete_add(conn, sess, lt,
                                     data.get("link_sessions") or [],
                                     refuse=ERASED, client=client)
                out[lt] = ("open" if reply is None
                           else "handed-back" if reply.get("done")
                           else "busy")
            except Exception as e:                         # noqa: BLE001
                log.warning("plaid link hand-back failed for a session of "
                            "%s: %s", tenant_id, e)
                out[lt] = "error"
    finally:
        conn.close()
    return out


def finish_session(tenant_id, link_token: str, transport=None) -> str:
    """Finish one recorded ADD session server-side, if Plaid says it is
    done — the worker's half of the LINK webhook and of the sweep. Returns
    a word for the log: done | open | busy | gone | blocked | error
    (blocked: the household may not keep a new Item, and any the session
    made was handed back)."""
    from .. import ext
    from ..sync.base import tenant_sync_lock
    sess = load(link_token, tenant_id)
    if sess is None or sess["kind"] != "add" or sess["item_id"]:
        return "gone"
    if sess["done"]:
        return "done"
    # the same standing rules as the poll: a household no longer active,
    # or held read-only by its billing, gets no new Item from a session
    # it opened earlier
    admin = tenancy.admin_connect()
    try:
        t = admin.execute("SELECT status FROM tenants WHERE id=%s",
                          (str(tenant_id),)).fetchone()
    finally:
        admin.close()
    if not t:
        return "gone"
    # A person who finished at Plaid created the Item whatever we decide,
    # so a refused household's tokens are still exchanged — and the Item
    # handed straight back — rather than left to expire with it billing
    refuse = sess.get("refuse")
    if refuse:
        pass
    elif t["status"] != "active":
        refuse = "this account is closed to new bank links right now"
    elif ext.gate.write_blocked(str(tenant_id)):
        refuse = "this account is read-only right now"
    # The new Items' first pulls run after the lock is let go, the way the
    # poll defers them: run inside complete_add, a multi-year pull kept the
    # session unfinished — and every poll answering "busy" — for minutes.
    first_syncs: list[str] = []
    conn = tenancy.tenant_connect(tenant_id)
    try:
        return _finish_locked(conn, tenant_id, sess, link_token, refuse,
                              transport, first_syncs, tenant_sync_lock)
    except plaid.PlaidError as e:
        log.warning("plaid link finish failed: %s", e)
        return "error"
    finally:
        conn.close()
        if first_syncs:
            # queued, not run here: the sweep finishes several sessions in
            # a row, and the queued job takes its turn among the first
            # pulls the worker runs at once (worker.sync_item)
            from . import worker
            for iid in first_syncs:
                worker.queue_first_sync(tenant_id, iid)


def _finish_locked(conn, tenant_id, sess, link_token, refuse, transport,
                   first_syncs, tenant_sync_lock) -> str:
    """finish_session's work under the household's sync lock; each new
    Item's id goes on `first_syncs` for the pull queued after the lock."""
    with tenant_sync_lock(conn, tenant_id) as held:
        if not held:
            return "busy"
        refresh(sess, link_token)
        if sess["done"]:
            return "done"
        client = plaid.Client.for_tenant(conn, transport=transport)
        data = client.get_link_session(link_token)
        sessions = data.get("link_sessions") or []
        reply = complete_add(conn, sess, link_token, sessions,
                             refuse=refuse, client=client,
                             defer_sync=first_syncs)
        if reply is not None:
            if not reply.get("done"):
                return "busy"
            return "blocked" if refuse else "done"
        if any(s.get("finished_at") for s in sessions):
            # nobody is polling, so no second look is coming: the
            # finished stamp has to be old enough on its own
            age = _finished_age(sessions)
            if age is not None and age > EXIT_GRACE_S:
                mark_done(sess, link_token,
                          {"done": True, "kind": "exited",
                           "institution": sess.get("institution")})
                return "done"
        return "blocked" if refuse else "open"


def _finished_age(sessions: list) -> float | None:
    import datetime as dt
    ages = []
    for s in sessions:
        f = s.get("finished_at")
        if not isinstance(f, str):
            continue
        try:
            at = dt.datetime.fromisoformat(f.replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        ages.append((dt.datetime.now(dt.timezone.utc) - at).total_seconds())
    return min(ages) if ages else None


def sweep(transport=None) -> dict:
    """Every few minutes: try to finish each open ADD session older than
    SWEEP_AFTER_S, close the ones past OPEN_FOR_S as abandoned, and delete
    rows older than KEEP_S. Never raises; returns {link_token: word}."""
    out: dict[str, str] = {}
    try:
        admin = tenancy.admin_connect()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link sweep: no admin connection: %s", e)
        return out
    try:
        admin.execute(
            "DELETE FROM plaid_link_sessions WHERE created_at < now() - "
            "make_interval(secs => %s)", (KEEP_S,))
        admin.execute(
            "UPDATE plaid_link_sessions SET done_at=now(), reply=%s "
            "WHERE done_at IS NULL AND created_at < now() - "
            "make_interval(secs => %s)",
            (json.dumps({"done": True, "kind": "exited"}), OPEN_FOR_S))
        # every household still on record, not only active ones: one that
        # was frozen or lapsed mid-link still has its finished tokens
        # exchanged and handed back (finish_session)
        rows = admin.execute(
            "SELECT s.link_token, s.tenant FROM plaid_link_sessions s "
            "JOIN tenants t ON t.id = s.tenant "
            "WHERE s.done_at IS NULL AND s.kind='add' "
            "AND s.created_at < now() - make_interval(secs => %s) "
            "ORDER BY s.created_at", (SWEEP_AFTER_S,)).fetchall()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link sweep: %s", e)
        return out
    finally:
        admin.close()
    for r in rows:
        try:
            out[r["link_token"]] = finish_session(str(r["tenant"]),
                                                  r["link_token"],
                                                  transport=transport)
        except Exception as e:                             # noqa: BLE001
            log.warning("plaid link sweep failed for a session: %s", e)
            out[r["link_token"]] = "error"
    return out


def tenant_for_link(link_token: str) -> str | None:
    """The household an open recorded session belongs to — the webhook
    arrives naming only the link token. Any household still on record:
    finish_session decides whether it may keep what the session made."""
    admin = tenancy.admin_connect()
    try:
        row = admin.execute(
            "SELECT s.tenant FROM plaid_link_sessions s "
            "JOIN tenants t ON t.id = s.tenant "
            "WHERE s.link_token=%s AND s.done_at IS NULL AND s.kind='add'",
            (link_token,)).fetchone()
    finally:
        admin.close()
    return str(row["tenant"]) if row else None
