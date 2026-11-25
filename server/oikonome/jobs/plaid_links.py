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
import threading
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
# A session whose token allows several banks is answered as finished when
# Plaid says the person left Link (finished_at), or once it has brought no
# new bank — and Link reported nothing of the person — for this long. A
# person who closes the window without leaving Link never gets a stamp,
# and every client waiting on "busy" would otherwise spin until its own
# deadline and report a timeout for a bank that is connected.
QUIET_S = 90
# …for a session only clients that do not say they keep their window
# open (no `v=2` on the poll) have looked at: such a phone closes its
# in-app browser on the answer, and a bank's own sign-in page can be
# silent for minutes. Still well inside every client's 35-minute wait.
OLD_CLIENT_QUIET_S = 300
# A session that brought more than this many banks the household could
# not keep (the cap, a repeat) pauses several banks per sign-in for a day.
SESSION_REFUSALS_TO_PAUSE = 2
SESSION_PAUSE_S = 24 * 3600
# How long Plaid can still report a bank for a link token: the token's own
# lifetime plus the hours /link/token/get keeps a session's results. A
# finished several-bank session keeps taking banks inside this window (a
# result that lagged the finished stamp, a bank added after the quiet
# answer), so none is left billing at Plaid unexchanged.
RESULTS_KEPT_S = 6 * 3600
# …when Plaid's answer names no expiration: a bound longer than any
# lifetime Plaid gives a link token by default.
RESULTS_WINDOW_DEFAULT_S = 10 * 3600
# …and never longer than this, whatever the answer says.
RESULTS_WINDOW_MAX_S = 30 * 3600
# What a bank saved but not yet pulled says (the clients' FIRST_SYNC_RUNNING,
# word for word).
FIRST_SYNC_RUNNING = "first sync running — accounts appear in a minute"


def token_key(public_token: str) -> str:
    """What a public token is recorded under: its hash, never the token."""
    return hashlib.sha256(public_token.encode()).hexdigest()


def record(tenant_id, link_token: str, kind: str, item_id: str | None,
           manage_accounts: bool, institution: str | None,
           days_requested: int | None = None,
           multi_item: bool = False,
           expiration: str | None = None,
           client_id: str | None = None) -> bool:
    """Put a newly started session on record. Best-effort: a session that
    is not recorded still completes through its poll, as it always did.

    `multi_item`: its link token lets the person connect several banks
    (create_hosted_link's answer), so it is finished only once Plaid says
    the person left Link or it goes quiet — see complete_add — and it
    keeps taking banks until `results_until`: Plaid's `expiration` for the
    token plus RESULTS_KEPT_S (RESULTS_WINDOW_DEFAULT_S from now when the
    answer names none; never past RESULTS_WINDOW_MAX_S). `client_id`: the
    keys its link token was made under (session_client).

    `days_requested` is the backfill its link token asked Plaid for
    (create_hosted_link's answer). Plaid fixes the backfill then, so every
    door that finishes the session records that window on the new Item,
    not the household's choice at the exchange (window_of)."""
    forget_cached_open(tenant_id)
    try:
        admin = tenancy.admin_connect()
        try:
            n = admin.execute(
                "INSERT INTO plaid_link_sessions (link_token, tenant, kind, "
                "item_id, manage_accounts, institution, days_requested, "
                "multi_item, results_until, client_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
                "now() + make_interval(secs => %s), %s) "
                "ON CONFLICT (link_token) "
                # Plaid mints a fresh token per session, so a start naming
                # one already on record is that session starting over
                "DO UPDATE SET tenant=EXCLUDED.tenant, kind=EXCLUDED.kind, "
                "item_id=EXCLUDED.item_id, "
                "manage_accounts=EXCLUDED.manage_accounts, "
                "institution=EXCLUDED.institution, "
                "days_requested=EXCLUDED.days_requested, "
                "multi_item=EXCLUDED.multi_item, "
                "results_until=EXCLUDED.results_until, created_at=now(), "
                "client_id=EXCLUDED.client_id, client_v=NULL, "
                "last_result_at=NULL, looked_at=NULL, "
                "exchanged='{}'::jsonb, done_at=NULL, reply=NULL "
                # …of the same household: a start can never move a session
                # on record to another one, whose finisher would then
                # exchange the first household's bank into it
                "WHERE plaid_link_sessions.tenant = EXCLUDED.tenant",
                (link_token, str(tenant_id), kind, item_id or None,
                 bool(manage_accounts), institution,
                 days_requested if plaid._is_days(days_requested)
                 else None, bool(multi_item),
                 _window_s(expiration) if multi_item else 0,
                 client_id or None)).rowcount
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


def _window_s(expiration) -> float:
    """Seconds from now until Plaid can no longer report a bank for a link
    token whose /link/token/create answer said `expiration`."""
    import datetime as dt
    left = None
    if isinstance(expiration, str):
        try:
            at = dt.datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=dt.timezone.utc)
            left = (at - dt.datetime.now(dt.timezone.utc)).total_seconds() \
                + RESULTS_KEPT_S
        except ValueError:
            left = None
    if left is None or left <= 0:
        left = RESULTS_WINDOW_DEFAULT_S
    return min(left, RESULTS_WINDOW_MAX_S)


def forget(link_token: str, tenant_id) -> None:
    """Take a session off the record — one given up to make room under the
    institution cap, which must not be finished by any door afterwards.

    A several-bank session is set aside instead (it no longer holds a place
    and is not offered again): the person may still be adding banks in its
    window, and the record is the only handle on them — whoever finishes it
    checks each bank against the cap, and one past it is handed back."""
    forget_cached_open(tenant_id)
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE plaid_link_sessions SET reply = COALESCE(reply, "
                "'{}'::jsonb) || '{\"cancelled\": true}'::jsonb "
                "WHERE link_token=%s AND tenant=%s AND multi_item",
                (link_token, str(tenant_id)))
            admin.execute("DELETE FROM plaid_link_sessions "
                          "WHERE link_token=%s AND tenant=%s "
                          "AND NOT multi_item",
                          (link_token, str(tenant_id)))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session not forgotten: %s", e)


ERASED = "this account is being deleted"


def _reply_sql(new: str) -> str:
    """The value every door that rewrites a session's `reply` writes, as
    SQL over the row's own reply (`s.reply`) and the new answer `new` (a
    jsonb expression). Decided here, against the row on record — never
    from a copy in some process's memory — for every rewrite:

    * the row's marks survive: `refuse` (every bank the session brings
      from now on is handed back: a data reset, an erasure, the day's
      ceiling) and `cancelled` (set aside by its client);
    * an erased household's row keeps only what the hand-back needs: no
      bank names, results or sentences about them — just whether it is
      done, how it ended and the refusal."""
    erased = ERASED.replace("'", "''")         # a SQL string literal
    return (
        f"CASE WHEN s.reply->>'refuse' = '{erased}' THEN "
        "jsonb_strip_nulls(jsonb_build_object("
        f"'done', {new}->'done', 'kind', {new}->'kind', "
        f"'error', {new}->'error', 'error_code', {new}->'error_code', "
        "'cancelled', s.reply->'cancelled', 'refuse', s.reply->'refuse')) "
        f"ELSE {new} || jsonb_strip_nulls(jsonb_build_object("
        "'cancelled', s.reply->'cancelled', 'refuse', s.reply->'refuse')) "
        "END")


def _write_reply(link_token: str, tenant_id, reply: dict, *,
                 finish: bool = False, open_only: bool = False,
                 where: str = "") -> dict | None:
    """Rewrite one session's `reply` with `reply` (through _reply_sql),
    stamping it finished when `finish`, touching only an unfinished row
    when `open_only`; `where` narrows further. The row's reply as stored,
    or None when no row matched. Raises on a database failure."""
    forget_cached_open(tenant_id)
    admin = tenancy.admin_connect()
    try:
        row = admin.execute(
            "UPDATE plaid_link_sessions s SET "
            + ("done_at=now(), " if finish else "")
            + "reply=" + _reply_sql("n.v")
            + " FROM (SELECT %s::jsonb AS v) n "
            "WHERE s.link_token=%s AND s.tenant=%s"
            + (" AND s.done_at IS NULL" if open_only else "")
            + where + " RETURNING s.reply",
            (json.dumps(reply), link_token, str(tenant_id))).fetchone()
    finally:
        admin.close()
    return row["reply"] if row else None


def _mark_refused(sess: dict, link_token: str, why: str) -> None:
    """Mark one session as the household's to keep no more banks from:
    every door that finishes it from now on hands each bank back, saying
    `why` (as refuse_open_adds does for a household's open sessions)."""
    sess["refuse"] = why
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE plaid_link_sessions SET reply = COALESCE(reply, "
                "'{}'::jsonb) || jsonb_build_object('refuse', %s::text) "
                "WHERE link_token=%s AND tenant=%s "
                "AND NOT COALESCE(reply ? 'refuse', false)",
                (why, link_token, str(sess["tenant_id"])))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session refusal not recorded: %s", e)


def set_aside(link_token: str, tenant_id) -> bool:
    """The person said they closed the bank window: the session stops
    holding a place under the institution cap and is no longer offered to
    a returning client, but stays on record, so if they did finish at
    Plaid after all the webhook or the sweep still exchanges the token —
    forgetting it would leave that Item billing with nothing here to
    remove it. True when an open add session was set aside."""
    try:
        n = _write_reply(link_token, tenant_id, {"cancelled": True},
                         open_only=True, where=" AND s.kind='add'")
        return n is not None
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
        "WHERE tenant=%s AND kind='add' AND (done_at IS NULL "
        # a finished several-bank session still taking banks: every one
        # it brings from now on is handed back
        f"OR ({ACCEPTING_SQL}))",
        (why, str(tenant_id))).rowcount


# A row still able to bring a bank: open, or a finished several-bank
# session inside its window. (SQL over plaid_link_sessions' own columns.)
ACCEPTING_SQL = ("multi_item AND results_until > now() "
                 "AND item_id IS NULL")


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
    got = {r["link_token"] for r in rows}
    with _OPEN_SEEN_LOCK:
        if len(_OPEN_SEEN) >= OPEN_SEEN_MAX:
            _OPEN_SEEN.clear()
        _OPEN_SEEN[(str(tenant_id), within_s)] = (time.monotonic(), got)
    return got


# What recent_open last read per household, for the account answer every
# page load asks (pages.link_offer): one owner connection per household
# per OPEN_SEEN_S at most. Stale by that much at worst — a session that
# opened in THIS process is counted from its memory anyway, and the start
# door reads the record fresh before it binds.
OPEN_SEEN_S = 30
OPEN_SEEN_MAX = 4096
_OPEN_SEEN: dict = {}
_OPEN_SEEN_LOCK = threading.Lock()


def recent_open_cached(tenant_id, within_s: float) -> set[str]:
    """recent_open, answered from what it read for this household in the
    last OPEN_SEEN_S."""
    with _OPEN_SEEN_LOCK:
        hit = _OPEN_SEEN.get((str(tenant_id), within_s))
    if hit and time.monotonic() - hit[0] < OPEN_SEEN_S:
        return set(hit[1])
    return recent_open(tenant_id, within_s)


def forget_cached_open(tenant_id=None) -> None:
    """Drop the cached recent_open reads of one household (every one when
    None): a session of it started, finished or was set aside here."""
    with _OPEN_SEEN_LOCK:
        if tenant_id is None:
            _OPEN_SEEN.clear()
            return
        for k in [k for k in _OPEN_SEEN if k[0] == str(tenant_id)]:
            _OPEN_SEEN.pop(k, None)


def _row(admin, link_token: str, tenant_id) -> dict | None:
    return admin.execute(
        "SELECT link_token, tenant, kind, item_id, manage_accounts, "
        "institution, extract(epoch FROM created_at) AS at, exchanged, "
        "done_at, reply, days_requested, multi_item, "
        "extract(epoch FROM results_until) AS results_until, "
        "extract(epoch FROM last_result_at) AS last_result_at, "
        "client_id, client_v "
        "FROM plaid_link_sessions "
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
            # itself (refuse_open_adds): whoever finishes it hands back —
            # and so does a finished one still taking banks
            "refuse": reply.get("refuse"),
            "reply": row["reply"],
            "days_requested": row["days_requested"],
            "multi_item": bool(row["multi_item"]),
            "results_until": (float(row["results_until"])
                              if row["results_until"] is not None else None),
            "last_result_at": (float(row["last_result_at"])
                               if row["last_result_at"] is not None
                               else None),
            "client_id": row["client_id"], "client_v": row["client_v"],
            # the kind it was STARTED as: `kind` above is how it ended
            # (add_failed, exited), and such a session still takes banks
            "started_as": row["kind"],
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
    if sess.get("days_requested") is None:
        sess["days_requested"] = row["days_requested"]
    if row["multi_item"]:
        sess["multi_item"] = True
    if row["results_until"] is not None:
        sess["results_until"] = float(row["results_until"])
    if row["last_result_at"] is not None:
        sess["last_result_at"] = max(float(row["last_result_at"]),
                                     sess.get("last_result_at") or 0)
    if (row["reply"] or {}).get("refuse"):
        sess["refuse"] = row["reply"]["refuse"]
    if row["client_id"] and not sess.get("client_id"):
        sess["client_id"] = row["client_id"]
    if row["client_v"]:
        sess["client_v"] = max(row["client_v"], sess.get("client_v") or 0)
    if row["done_at"] is None and (row["reply"] or {}).get("refuse"):
        sess["refuse"] = row["reply"]["refuse"]
    if row["done_at"] is not None and not sess.get("done"):
        reply = row["reply"] or {}
        sess["done"] = True
        sess["reply"] = reply
        sess["kind"] = reply.get("kind") or sess.get("kind")
        sess["institution"] = reply.get("institution") \
            or sess.get("institution")


def note_client(link_token: str, tenant_id, v: int) -> None:
    """A poll said which client protocol it speaks (`v=2`: keeps its window
    open on a quiet answer); the newest is kept with the session."""
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE plaid_link_sessions SET client_v=%s "
                "WHERE link_token=%s AND tenant=%s "
                "AND COALESCE(client_v, 0) < %s",
                (int(v), link_token, str(tenant_id), int(v)))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link client protocol not recorded: %s", e)


def session_client(conn, sess: dict, link_token: str = "", transport=None):
    """The Plaid client for everything about a session: the keys its link
    token was made under, for as long as the instance still has them.
    A public token can only be exchanged — and its Item removed — under
    the client that made it; the household's keys may have changed since
    (or been erased with it). The instance's own keys are always at hand;
    a household's own keys only while they are still its keys. Raises
    PlaidError("KEYS_GONE") when they are not, logged by session."""
    want = sess.get("client_id")
    try:
        cur = plaid.Client.for_tenant(conn, transport=transport)
    except plaid.PlaidError:
        cur = None
    if cur is not None and (not want or cur._auth["client_id"] == want):
        return cur
    platform = plaid.Client.for_platform(transport) if want else None
    if platform is not None and platform._auth["client_id"] == want:
        return platform
    if not want and cur is None:
        raise plaid.PlaidError({"error_code": "NO_CREDENTIALS",
                                "error_type": "CONFIG",
                                "error_message": "no Plaid credentials "
                                                 "configured"}, 0)
    log.warning("plaid link session %s: the keys its link token was made "
                "under are gone — its banks cannot be exchanged here",
                token_key(link_token)[:12] if link_token else "?")
    raise plaid.PlaidError({"error_code": "KEYS_GONE",
                            "error_type": "CONFIG",
                            "error_message": "the keys this bank link was "
                                             "made with are gone"}, 0)


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
        _keep_marks(sess, _write_reply(link_token, sess["tenant_id"], reply,
                                       finish=True, open_only=True))
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session finish not recorded: %s", e)
    return reply


def _keep_marks(sess: dict, stored: dict | None) -> None:
    """The marks the row on record carries (_reply_sql kept them), back
    into the session in memory."""
    if stored and stored.get("refuse"):
        sess["refuse"] = stored["refuse"]


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


def _held_by_session(conn, key: str, lsid: str | None,
                     settled: dict, since: float | None = None) -> str | None:
    """An Item this household holds that the public token hashed as `key`
    created — the answer for a token whose exchange committed its row but
    whose outcome was never recorded (the finisher died before it could).
    Found by the token's hash on the row; a row saved before the hash was
    kept is found by its Plaid session only when that names exactly one
    Item no settled token accounts for (several Items of one session could
    not be told apart). `since`: when the session started — an Item
    linked before it was not made by its token."""
    row = conn.execute(
        "SELECT id FROM items WHERE aggregator='plaid' "
        "AND raw->>'link_token_key' = %s "
        "AND linked_at >= to_timestamp(%s)",
        (key, float(since or 0))).fetchone()
    if row:
        return row["id"]
    if not lsid:
        return None
    claimed = {v.get("item_id") for v in settled.values()
               if isinstance(v, dict)}
    left = [r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE link_session_id=%s "
        "AND aggregator='plaid' AND raw->>'link_token_key' IS NULL "
        "ORDER BY id", (lsid,)).fetchall() if r["id"] not in claimed]
    return left[0] if len(left) == 1 else None


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


def apply_account_selection(conn, client, token, item_id: str) -> None:
    """After a shared-accounts (account-selection) update completes, hide
    the item's LIVE accounts that Plaid no longer returns: the user just
    deselected them, so Plaid stops serving (and billing) them, and their
    local rows would otherwise linger looking connected with a
    forever-stale balance.

    Live-served only — balance_current IS NOT NULL — because accounts
    that carry no live balance, such as history imported from files, may
    share the item, and hiding those would pull their history out of net
    worth and cash flow.

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


def _finish_checklist_session(conn, sess: dict, client) -> None:
    """A shared-accounts session Plaid answered with a public token: the same
    finish the update path gives it. The session could have shared the
    waiting accounts, so the new-accounts prompt is done, and whatever was
    deselected stops looking connected. Plaid does not always report such a
    session as an update, and the prompt would otherwise stay up for good
    after the person answered it."""
    item_id = sess["item_id"]
    conn.execute(
        "UPDATE items SET webhook_status = NULL, webhook_status_at = NULL "
        "WHERE id=%s AND webhook_status='new_accounts' "
        "AND COALESCE(status,'') != 'archived'", (item_id,))
    apply_account_selection(conn, client or plaid.Client.for_tenant(conn),
                            plaid.get_access_token(conn, item_id), item_id)


def window_of(sess: dict) -> int:
    """The backfill window a session's new Item is recorded with: the one
    its link token asked Plaid for, kept with the session. A session with
    none kept (started by an older build, or never put on record) gets the
    widest window — the household's choice read now may be narrower than
    the backfill the token asked for, and an Item recorded narrower than
    its backfill is offered a relink, a new billed Item, it does not need."""
    asked = sess.get("days_requested")
    return asked if plaid._is_days(asked) else plaid.DAYS_REQUESTED


def accepts_results(sess: dict, now: float | None = None) -> bool:
    """A several-bank add session inside its window: finished or not, a bank
    Plaid reports for it is still exchanged (and kept or handed back)."""
    until = sess.get("results_until")
    return (bool(sess.get("multi_item")) and not sess.get("item_id")
            and until is not None
            and float(until) > (now if now is not None else time.time()))


def _touch(link_token: str, tenant_id, field: str) -> None:
    """Stamp one of the session's clocks (last_result_at / looked_at)."""
    if field not in ("last_result_at", "looked_at"):
        raise ValueError(field)
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                f"UPDATE plaid_link_sessions SET {field}=now() "
                "WHERE link_token=%s AND tenant=%s",
                (link_token, str(tenant_id)))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session clock not recorded: %s", e)


def _last_event_at(sessions: list) -> float | None:
    """The newest timestamp of any event Link reported for the session
    (Hosted Link sessions may carry `events`), as epoch seconds."""
    import datetime as dt
    newest = None
    for s in sessions:
        for ev in (s.get("events") or []) if isinstance(s, dict) else []:
            ts = ev.get("timestamp") if isinstance(ev, dict) else None
            if not isinstance(ts, str):
                continue
            try:
                at = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=dt.timezone.utc)
            t = at.timestamp()
            newest = t if newest is None else max(newest, t)
    return newest


# A bank whose exchange Plaid refused, in words (the code stays in
# error_code; Plaid's own message is no sentence for a person).
NOT_COMPLETED = "its sign-in could not be completed — link it again"


def complete_add(conn, sess: dict, link_token: str,
                 sessions: list, refuse: str | None = None,
                 client=None, defer_sync: list | None = None,
                 tokens: list | None = None) -> dict | None:
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
    answered by the Item it created (found by the token's hash).

    `refuse` (with the household's `client`) is the household that may not
    keep a new Item: each token is exchanged and its Item handed straight
    back, and the session finishes as a failed link saying why.

    `defer_sync`, a list, leaves each new Item's first pull to the caller:
    the Item is saved and its id appended, and the answer says the first
    sync is pending. The caller queues the pull once it has let go of the
    sync lock (the web poll; see plaid.link_item).

    `tokens`: public tokens a LINK webhook named (ITEM_ADD_RESULT's one,
    SESSION_FINISHED's list) — Plaid's word that they belong to this
    session, so one not yet in its results is settled all the same.

    A session whose token allows several banks (``multi_item``) reports
    each bank as the person adds it. Each one is exchanged as it arrives,
    and the session is answered as finished once Plaid stamps it finished
    or once it has gone QUIET_S without a new bank (or a Link event);
    until then the answer is a busy one carrying the banks so far
    (``results``, ``summary``, ``progress``). A finished several-bank
    session keeps taking banks for its window (:func:`accepts_results`):
    a late one is exchanged — kept, or handed back when the household may
    not keep it — and the finished answer is rewritten to include it.
    A token whose exchange got no answer keeps an unfinished session open.

    When more than one bank came back, the finished answer lists each in
    ``results`` ({institution, connected, error?}) and words them all in
    ``summary``; ``institution`` stays the connected banks' names joined,
    which is what a client that reads only it shows."""
    add_results = [
        (s.get("link_session_id"), r) for s in sessions
        for r in ((s.get("results") or {}).get("item_add_results") or [])
        if isinstance(r, dict) and r.get("public_token")]
    seen = {token_key(r["public_token"]) for _, r in add_results}
    for t in tokens or []:
        if isinstance(t, str) and 0 < len(t) <= 512 \
                and token_key(t) not in seen:
            seen.add(token_key(t))
            add_results.append((None, {"public_token": t}))
    if not add_results:
        return None
    if refuse is None and sess.get("refuse"):
        # the session was marked while open (refuse_open_adds): every door
        # that finishes it hands its Items back, the poll included
        refuse = sess["refuse"]
    if client is None:
        client = session_client(conn, sess, link_token)
    multi = bool(sess.get("multi_item"))
    settled = sess.setdefault("exchanged", {})
    before = set(settled)
    names, err, retry, pending = [], None, False, False
    results: list[dict] = []
    # a relink for a wider history window: the Item this session
    # replaces, bound in memory at the start and on the old row itself
    # (items.raw.relink_session), so a finisher after a restart — the
    # webhook, the sweep — settles it the same way
    relink_of = sess.get("relink_of") or _relink_of(conn, link_token)
    for lsid, r in add_results:
        key = token_key(r["public_token"])
        got = settled.get(key)
        if got is None and refuse is None and multi \
                and plaid.handed_back_today(conn, sess["tenant_id"]) \
                >= plaid.HANDED_BACK_PER_DAY:
            # the day's ceiling on banks handed back holds inside a
            # sitting, not only at its start: Plaid's "add another bank"
            # cannot be stopped from here, so from this bank on the session
            # keeps none, and several banks per sign-in pause
            refuse = plaid.TOO_MANY_HANDED_BACK
            _mark_refused(sess, link_token, refuse)
            _pause_several(conn, client, refuse)
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
                                      window_asked=window_of(sess),
                                      relink_of=relink_of, several=multi,
                                      client=client)
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
                held = (_held_by_session(conn, key, lsid, settled,
                                         sess.get("at"))
                        if e.code == "INVALID_PUBLIC_TOKEN" else None)
                got = ({"item_id": held, "first_sync": "pending"} if held
                       else e.public())
            except plaid.LinkRefused as e:
                # refused after the exchange (institution cap, key swap,
                # bank-link hold, a repeat, the row not saving) — link_item
                # has already handed the Item back to Plaid; the reason is
                # the typed sentence it chose, not the exception's text
                got = {"error": e.reason, "error_code": "LINK_REFUSED"}
                if e.kind:
                    got["refusal"] = e.kind
            settled[key] = got
            _save_exchange(link_token, sess["tenant_id"], key, got)
        inst = r.get("institution")
        plaid_name = inst.get("name") if isinstance(inst, dict) else None
        if not isinstance(plaid_name, str) or not plaid_name.strip():
            plaid_name = None
        if got.get("item_id"):
            row = conn.execute(
                "SELECT institution_name FROM items WHERE id=%s",
                (got["item_id"],)).fetchone()
            names.append(row["institution_name"] if row else None)
            pending = pending or got.get("first_sync") == "pending"
            results.append({"institution": names[-1] or plaid_name,
                            "connected": True, "_key": key})
        else:
            # the typed fields of the refusal (error, error_code and,
            # from Plaid, request_id) ride into the failed answer whole
            err = {k: v for k, v in got.items()
                   if k in ("error", "error_code", "request_id") and v}
            why = (got.get("error") if got.get("error_code")
                   in ("LINK_REFUSED", None) else NOT_COMPLETED)
            results.append({"institution": plaid_name, "connected": False,
                            "error": why or NOT_COMPLETED, "_key": key})
    now = time.time()
    fresh = set(settled) - before
    if fresh:
        # a new bank: the quiet clock starts again
        sess["last_result_at"] = now
        _touch(link_token, sess["tenant_id"], "last_result_at")
        if multi and refuse is None:
            _pause_if_overreached(conn, client, settled)
    tagged = results
    results = [{k: v for k, v in r.items() if k != "_key"} for r in tagged]
    several = ({"results": results, "summary": summary(results, pending)}
               if len(add_results) > 1 else {})
    if sess.get("done"):
        # a finished session that kept taking banks: its answer is
        # rewritten only when something new came of this look
        if not fresh:
            return done_reply(sess)
        # a bank that came after the answer: the household hears of it
        # where it reads what happened, kept or not
        if refuse is None:
            _note_late(conn, [r for r in tagged if r.get("_key") in fresh])
        return _rewrite_done(sess, link_token, _finished(
            sess, names, err, several, pending, add_results, settled,
            refuse, client, conn))
    if retry:
        # a token whose exchange got no answer is tried again next look
        return _busy(results, pending)
    if multi and not any(s.get("finished_at") for s in sessions):
        last = max(sess.get("last_result_at") or now,
                   _last_event_at(sessions) or 0)
        quiet_s = (QUIET_S if (sess.get("client_v") or 0) >= 2
                   else OLD_CLIENT_QUIET_S)
        if now - last <= quiet_s:
            # the person may still add another bank in this session
            return _busy(results, pending)
        # gone quiet: answered with the banks so far — the window may
        # still be open, and a bank added there is taken later
        several = {**several, "quiet": True}
    return mark_done(sess, link_token, _finished(
        sess, names, err, several, pending, add_results, settled, refuse,
        client, conn))


def _pause_if_overreached(conn, client, settled: dict) -> None:
    """A session that brought more than SESSION_REFUSALS_TO_PAUSE banks
    the household could not keep (the cap, a repeat) pauses several banks
    per sign-in for a day — one bank per sign-in still works — and says so
    in the activity log. Each such bank was a real Item at Plaid until it
    was removed, and Plaid's "add another bank" cannot be stopped here."""
    n = sum(1 for v in settled.values() if isinstance(v, dict)
            and v.get("refusal") in ("cap", "repeat"))
    if n <= SESSION_REFUSALS_TO_PAUSE:
        return
    _pause_several(conn, client, "a sign-in brought more banks than this "
                                 "account could keep")


def _pause_several(conn, client, why: str) -> None:
    """Pause several banks per sign-in for the household under `client`'s
    keys for SESSION_PAUSE_S, saying `why` in the activity log — once,
    while a pause already stands nothing more is written."""
    from ..sync import plaid_user
    key = plaid_user.key_of(client)
    if plaid_user.paused(conn, key):
        return
    plaid_user.pause(conn, key, why, seconds=SESSION_PAUSE_S)
    from ..engine import activity
    activity.record(conn, "plaid", "account", "several_banks_paused",
                    label="", detail={"reason": why})


def _note_late(conn, results: list) -> None:
    """Each bank a finished session brought after its answer, in the
    activity log: connected, or not and why."""
    from ..engine import activity
    for r in results:
        name = r.get("institution") or "a bank"
        if r.get("connected"):
            activity.record(conn, "plaid", "account", "late_connected",
                            label=name)
        else:
            activity.record(conn, "plaid", "account", "late_refused",
                            label=name,
                            detail={"reason": r.get("error") or NOT_COMPLETED})


def _busy(results: list, pending: bool) -> dict:
    busy: dict = {"done": False, "kind": "busy"}
    if results:
        busy["results"] = results
        busy["summary"] = summary(results, pending)
        ok = [r.get("institution") or "a bank" for r in results
              if r.get("connected")]
        if ok:
            busy["progress"] = (f"Connected {_named(ok)} — add another in "
                                "the bank window, or you're done.")
    return busy


def _finished(sess, names, err, several, pending, add_results, settled,
              refuse, client, conn) -> dict:
    """The finished answer of an add session from what each token came to."""
    institution = (", ".join(n for n in names if n)
                   or sess.get("institution"))
    if not names and err:
        return {"done": True, "kind": "add_failed",
                "institution": institution, **err, **several}
    reply = {"done": True, "kind": "add", "institution": institution,
             **several}
    # the session was opened on an Item and its token led back to that Item:
    # a reconnect, whichever way Plaid reported it
    own = sess.get("item_id") and any(
        (settled.get(token_key(r["public_token"])) or {}).get("item_id")
        == sess["item_id"] for _, r in add_results)
    if own:
        reply["kind"] = "update"
        if sess.get("manage_accounts") and refuse is None:
            _finish_checklist_session(conn, sess, client)
    if pending:
        reply["first_sync"] = "pending"
    return reply


def _rewrite_done(sess: dict, link_token: str, reply: dict) -> dict:
    """A finished session's answer, rewritten with a bank that came late."""
    keep = {k: v for k, v in (sess.get("reply") or {}).items()
            if k in ("refuse", "cancelled")}
    reply = {**reply, **keep}
    sess["reply"] = reply
    sess["institution"] = reply.get("institution") or sess.get("institution")
    try:
        _keep_marks(sess, _write_reply(link_token, sess["tenant_id"], reply))
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session answer not rewritten: %s", e)
    return done_reply(sess)


def _named(names: list[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def summary(results: list[dict], first_sync_pending: bool = False) -> str:
    """The sentence a session that came back with several banks earns:
    every bank connected, then the banks that were not, grouped by why (a
    bank named twice is named once). Both clients print it as it is."""
    ok = list(dict.fromkeys(r.get("institution") or "a bank"
                            for r in results if r.get("connected")))
    parts = []
    if ok:
        line = "Connected " + _named(ok)
        if first_sync_pending:
            line += " — " + FIRST_SYNC_RUNNING
        parts.append(line + ".")
    by_why: dict[str, list[str]] = {}
    for r in results:
        if r.get("connected"):
            continue
        why = str(r.get("error") or NOT_COMPLETED).rstrip(".")
        who = by_why.setdefault(why, [])
        name = r.get("institution") or ("a bank" if who else "A bank")
        if name not in who:
            who.append(name)
    for why, who in by_why.items():
        verb = "were" if len(who) > 1 else "was"
        named = _named(who)
        if len(who) > 1:
            why = plaid.PLURAL_REASONS.get(why, why)
        parts.append(f"{named[:1].upper()}{named[1:]} {verb} not "
                     f"connected: {why}.")
    return " ".join(parts)


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
                "AND kind='add' AND (done_at IS NULL "
                f"OR ({ACCEPTING_SQL})) ORDER BY created_at",
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
        for r in rows:
            lt = r["link_token"]
            try:
                sess = load(lt, tenant_id)
                if sess is None or sess["item_id"] or (
                        sess["done"] and not accepts_results(sess)):
                    continue
                # the keys the session was made under
                client = session_client(conn, sess, lt, transport)
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


def finish_session(tenant_id, link_token: str, transport=None,
                   tokens: list | None = None) -> str:
    """Finish one recorded ADD session server-side, if Plaid says it is
    done — the worker's half of the LINK webhook and of the sweep — and
    take any bank a finished several-bank session still brings inside its
    window. `tokens`: the public tokens the webhook named. Returns a word
    for the log: done | open | busy | gone | blocked | error (blocked: the
    household may not keep a new Item, and any the session made was
    handed back).

    A household erased while its several-bank session could still bring a
    bank leaves the row behind as a tombstone marked ERASED: every bank it
    brings is exchanged and handed straight back."""
    from .. import ext
    from ..sync.base import tenant_sync_lock
    sess = load(link_token, tenant_id)
    if sess is None or sess["started_as"] != "add" or sess["item_id"]:
        return "gone"
    if sess["done"] and not accepts_results(sess):
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
    # A person who finished at Plaid created the Item whatever we decide,
    # so a refused household's tokens are still exchanged — and the Item
    # handed straight back — rather than left to expire with it billing.
    # A row whose household no longer exists is an erased one's, whatever
    # its reply says: its banks go back too
    refuse = sess.get("refuse") or (None if t else ERASED)
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
                              transport, first_syncs, tenant_sync_lock,
                              tokens)
    except plaid.PlaidError as e:
        if not t and e.code == "KEYS_GONE":
            # an erased household's session made under its own Plaid keys:
            # they went with it, so no bank it brings can be exchanged — or
            # removed — here (an Item there bills that household's own
            # Plaid account, which can remove it). Closed, so neither the
            # sweep nor the webhook keeps asking for the rest of its window
            _close(link_token, tenant_id)
            return "gone"
        log.warning("plaid link finish failed: %s", e)
        return "error"
    finally:
        conn.close()
        if first_syncs and t:
            # queued, not run here: the sweep finishes several sessions in
            # a row, and the queued job takes its turn among the first
            # pulls the worker runs at once (worker.sync_item)
            from . import worker
            for iid in first_syncs:
                worker.queue_first_sync(tenant_id, iid)


def _finish_locked(conn, tenant_id, sess, link_token, refuse, transport,
                   first_syncs, tenant_sync_lock, tokens=None) -> str:
    """finish_session's work under the household's sync lock; each new
    Item's id goes on `first_syncs` for the pull queued after the lock."""
    with tenant_sync_lock(conn, tenant_id) as held:
        if not held:
            return "busy"
        refresh(sess, link_token)
        if sess["done"] and not accepts_results(sess):
            return "done"
        if sess.get("refuse") and not refuse:
            refuse = sess["refuse"]
        client = session_client(conn, sess, link_token, transport)
        data = client.get_link_session(link_token)
        if sess.get("multi_item"):
            _touch(link_token, tenant_id, "looked_at")
        sessions = data.get("link_sessions") or []
        reply = complete_add(conn, sess, link_token, sessions,
                             refuse=refuse, client=client,
                             defer_sync=first_syncs, tokens=tokens)
        if reply is not None:
            if not reply.get("done"):
                return "busy"
            return "blocked" if refuse else "done"
        if sess["done"]:
            return "done"
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


def _close(link_token: str, tenant_id) -> None:
    """Stop a session taking banks: finished, and its window shut now. The
    sweep deletes the row once it is KEEP_S old."""
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE plaid_link_sessions SET done_at = COALESCE(done_at, "
                "now()), results_until = LEAST(results_until, now()) "
                "WHERE link_token=%s AND tenant=%s",
                (link_token, str(tenant_id)))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link session not closed: %s", e)


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
    SWEEP_AFTER_S, close the ones past OPEN_FOR_S as abandoned, look again
    at each finished several-bank session still inside its window (every
    sweep in its first hour, every 15 minutes to its fourth, hourly after),
    and delete rows older than KEEP_S whose window has closed. Never
    raises; returns {link_token: word}."""
    out: dict[str, str] = {}
    try:
        admin = tenancy.admin_connect()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid link sweep: no admin connection: %s", e)
        return out
    try:
        admin.execute(
            "DELETE FROM plaid_link_sessions WHERE created_at < now() - "
            "make_interval(secs => %s) "
            "AND (results_until IS NULL OR results_until < now())",
            (KEEP_S,))
        admin.execute(
            "UPDATE plaid_link_sessions s SET done_at=now(), reply="
            + _reply_sql("n.v") + " FROM (SELECT %s::jsonb AS v) n "
            "WHERE s.done_at IS NULL AND s.created_at < now() - "
            "make_interval(secs => %s) "
            # a several-bank session with banks in is answered by the
            # banks, not called abandoned
            "AND NOT (s.multi_item AND s.exchanged <> '{}'::jsonb)",
            (json.dumps({"done": True, "kind": "exited"}), OPEN_FOR_S))
        # every household on record, not only active ones: one that was
        # frozen or lapsed mid-link still has its finished tokens exchanged
        # and handed back (finish_session); a row whose household is gone
        # (an erased one's tombstone) too
        rows = admin.execute(
            "SELECT s.link_token, s.tenant FROM plaid_link_sessions s "
            "WHERE s.kind='add' AND (("
            " s.done_at IS NULL AND s.created_at < now() - "
            " make_interval(secs => %s))"
            f" OR (s.done_at IS NOT NULL AND {ACCEPTING_SQL}"
            "     AND (s.looked_at IS NULL OR s.looked_at < now() - CASE"
            "       WHEN s.done_at > now() - interval '1 hour'"
            "         THEN interval '1 minute'"
            "       WHEN s.done_at > now() - interval '4 hours'"
            "         THEN interval '15 minutes'"
            "       ELSE interval '1 hour' END)))"
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
    """The household a recorded session belongs to — the webhook arrives
    naming only the link token. An open add session, or a finished
    several-bank one inside its window (an erased household's tombstone
    included): finish_session decides what may be kept."""
    admin = tenancy.admin_connect()
    try:
        row = admin.execute(
            "SELECT s.tenant FROM plaid_link_sessions s "
            "WHERE s.link_token=%s AND s.kind='add' AND (s.done_at IS NULL "
            f"OR ({ACCEPTING_SQL}))",
            (link_token,)).fetchone()
    finally:
        admin.close()
    return str(row["tenant"]) if row else None
