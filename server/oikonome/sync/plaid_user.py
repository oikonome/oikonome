"""The household's Plaid user — what lets a person connect several banks
in one Link session (specs/plaid-link.md, "Several banks in one session").

Plaid's multi-institution Link wants a Plaid USER on the link token: an id
Plaid mints at /user/create for one of our client_user_ids, under ONE
client_id in ONE Plaid environment (one client_id serves sandbox and
production alike; only the secret differs). A household links under the
instance's keys or under its own, so the id is kept per (household, keys)
in ``plaid_users`` — the keys being the client_id, suffixed with the
environment outside production (:func:`key`) — and an id made under one
client or environment is never sent with another's; a household that
switches keys or environment gets a new one the next time it links.

The id is not a secret, but it names the household at Plaid, so it is
never logged. It is not exported or restored (an id at Plaid tied to this
instance's keys), and it is erased with the household. Nothing here ever
removes the user at Plaid: once the household's Items are released the id
is inert — it holds no access to any bank.

Every failure here means "one bank per session this time", never a failed
link: the caller asks Plaid for an ordinary link token instead."""

from __future__ import annotations

import logging
import os
import secrets

log = logging.getLogger(__name__)

# the longest id we keep; Plaid's ids are far shorter, and anything longer
# is not an id
_MAX_ID = 256
# Flagged link tokens Plaid refused in a row with the stored user before
# several banks per sign-in are paused for the household and keys.
REFUSALS_TO_PAUSE = 2
# A pause for Plaid refusing (the flag, the user, /user/create) lasts this
# long, doubling with each pause in a row up to COOLDOWN_MAX_S; the SAME
# stored user is tried again after it, so a pause never mints another.
COOLDOWN_S = 24 * 3600
COOLDOWN_MAX_S = 7 * 24 * 3600


def enabled() -> bool:
    """The deployment switch OIKONOME_PLAID_MULTI_ITEM: on unless set to
    0 / off / false / no."""
    raw = (os.environ.get("OIKONOME_PLAID_MULTI_ITEM") or "").strip().lower()
    return raw not in ("0", "off", "false", "no")


def key(client_id: str, base_url: str) -> str:
    """What a household's Plaid user is kept under for the keys
    `client_id` at the Plaid environment `base_url`: the client_id itself
    in production (every row kept before environments were told apart was
    made there), ``<client_id>@<environment>`` anywhere else."""
    from . import plaid
    url = (base_url or "").rstrip("/")
    env = next((k for k, u in plaid.ENV_URLS.items() if u == url), url)
    return client_id if env == "production" else f"{client_id}@{env}"


def key_of(client) -> str:
    """:func:`key` for a Plaid client."""
    return key(client._auth.get("client_id") or "",
               getattr(client, "base_url", "") or "")


def _tenant(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t"
    ).fetchone()["t"] or ""


def _row(conn, client_id: str) -> dict | None:
    # `client_id`: the keys' key()
    return conn.execute(
        "SELECT user_id, client_user_id, refusals, cooldowns, off_reason, "
        "COALESCE(multi_item_off_until > now(), false) AS off "
        "FROM plaid_users WHERE client_id=%s", (client_id,)).fetchone()


def paused(conn, client_id: str) -> str | None:
    """Why several banks per sign-in are paused for this household under
    the keys `client_id` (:func:`key`), or None. Best-effort: a failed
    read is no pause."""
    try:
        r = _row(conn, client_id)
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid user not read: %s", type(e).__name__)
        return None
    return (r["off_reason"] or "paused") if r and r["off"] else None


def pause(conn, client_id: str, reason: str,
          seconds: float | None = None) -> None:
    """Pause several banks per sign-in for this household under the keys
    `client_id` (:func:`key`): for `seconds`, or — Plaid refusing — COOLDOWN_S
    doubled for each pause in a row, up to COOLDOWN_MAX_S. Persisted on
    the household's plaid_users row (kept, with its user, for after the
    pause); a household with no user yet gets a row with none."""
    try:
        conn.execute(
            "INSERT INTO plaid_users (client_id, user_id, "
            "multi_item_off_until, off_reason, cooldowns) VALUES "
            "(%s, '', now() + make_interval(secs => %s), %s, 1) "
            "ON CONFLICT (tenant_id, client_id) DO UPDATE SET "
            "multi_item_off_until = now() + make_interval(secs => "
            "  COALESCE(%s, LEAST(%s * power(2, plaid_users.cooldowns), "
            "  %s))), off_reason = EXCLUDED.off_reason, "
            "cooldowns = plaid_users.cooldowns + 1, refusals = 0",
            (client_id, float(seconds or COOLDOWN_S), reason,
             None if seconds is None else float(seconds), float(COOLDOWN_S),
             float(COOLDOWN_MAX_S)))
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid several-bank pause not recorded: %s",
                    type(e).__name__)


def _create(client, client_user_id: str):
    """(user_id, None) or (None, the PlaidError or None)."""
    from . import plaid
    try:
        made = client.post("/user/create", {"client_user_id": client_user_id})
    except plaid.PlaidError as e:
        return None, e
    uid = made.get("user_id") if isinstance(made, dict) else None
    if not isinstance(uid, str) or not uid or len(uid) > _MAX_ID:
        return None, None
    return uid, None


def ensure(conn, client) -> tuple[str, str] | None:
    """(user_id, client_user_id) of the household's Plaid user under
    `client`'s keys, created at Plaid the first time it is needed; None
    when several banks are paused, or there is no user to be had, and the
    link goes ahead with one bank.

    Two link starts at once create one user: the second waits on the
    household's lock and finds the first's row. The Plaid call is made
    under that lock — a first link start is rare, and creating two users
    for one household would leave one of them orphaned at Plaid.

    A /user/create refused for the household's own client_user_id (a user
    made earlier whose answer was lost — Plaid then holds one we cannot
    name) is asked once more under a client_user_id with a short suffix,
    which is the one stored and sent with every link token. Refused both
    times, several banks are paused (:func:`pause`): no start asks again
    until the pause ends."""
    from . import plaid
    client_id = key_of(client) if client._auth.get("client_id") else ""
    tenant = _tenant(conn)
    if not client_id or not tenant:
        return None
    try:
        r = _row(conn, client_id)
        if r and r["off"]:
            return None
        if r and r["user_id"]:
            return r["user_id"], r["client_user_id"] or tenant
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         (f"plaid-user:{tenant}",))
            r = _row(conn, client_id)
            if r and r["off"]:
                return None
            if r and r["user_id"]:
                return r["user_id"], r["client_user_id"] or tenant
            cuid = tenant
            uid, err = _create(client, cuid)
            if uid is None and isinstance(err, plaid.PlaidError) \
                    and err.status == 400:
                cuid = f"{tenant}:{secrets.token_hex(3)}"
                uid, err = _create(client, cuid)
            if uid is None:
                code = err.code if err is not None else "no user id"
                if isinstance(err, plaid.PlaidTransportError) or (
                        isinstance(err, plaid.PlaidError)
                        and err.status >= 500):
                    log.warning("plaid user not created (%s) — this link "
                                "connects one bank", code)
                else:
                    log.warning("plaid user not created (%s) — several "
                                "banks per sign-in paused", code)
                    pause(conn, client_id, "Plaid would not set this "
                                           "account up for it")
                return None
            conn.execute(
                "INSERT INTO plaid_users (client_id, user_id, client_user_id)"
                " VALUES (%s,%s,%s) ON CONFLICT (tenant_id, client_id) DO "
                "UPDATE SET user_id = EXCLUDED.user_id, "
                "client_user_id = EXCLUDED.client_user_id "
                "WHERE plaid_users.user_id = ''",
                (client_id, uid, cuid))
            r = _row(conn, client_id)
            return ((r["user_id"], r["client_user_id"] or tenant)
                    if r and r["user_id"] else (uid, cuid))
    except Exception as e:                                 # noqa: BLE001
        # the table missing on a database mid-upgrade, a lost connection:
        # the link still goes ahead, one bank at a time
        log.warning("plaid user not read or kept: %s", type(e).__name__)
        return None


def note_outcome(conn, client, accepted: bool) -> None:
    """What Plaid made of a link token carrying the stored user: accepted
    clears the counts; REFUSALS_TO_PAUSE refusals in a row pause several
    banks per sign-in (COOLDOWN_S, doubling), keeping the user to try
    again after. Best-effort."""
    client_id = key_of(client)
    try:
        if accepted:
            conn.execute("UPDATE plaid_users SET refusals=0, cooldowns=0 "
                         "WHERE client_id=%s AND (refusals<>0 "
                         "OR cooldowns<>0)", (client_id,))
            return
        n = conn.execute("UPDATE plaid_users SET refusals=refusals+1 "
                         "WHERE client_id=%s RETURNING refusals",
                         (client_id,)).fetchone()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid user outcome not recorded: %s", type(e).__name__)
        return
    if n and n["refusals"] >= REFUSALS_TO_PAUSE:
        pause(conn, client_id, "Plaid turned several banks per sign-in "
                               "down for this account")
