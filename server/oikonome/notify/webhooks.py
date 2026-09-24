"""Outbound webhooks: the instance telling another program what just
happened, so a household running its own box can wire the books into
whatever else it runs — a Home Assistant automation, a Matrix bot, a
spreadsheet, a script of their own.

The shape is the ordinary one, so any receiver written for any other
webhook source works unchanged:

  * POST, JSON body ``{"id", "event", "created_at", "attempt", "data"}``.
  * ``X-Oikonome-Event`` names the event, ``X-Oikonome-Delivery`` the row,
    and ``X-Oikonome-Signature: t=<unix>,v1=<hex>`` is HMAC-SHA256 over
    ``"<t>.<body>"`` with the webhook's secret — the receiver recomputes
    it and refuses anything else, and the timestamp lets it refuse a
    replay. The secret is shown once at creation, like a script token.
  * A 2xx is delivered. Anything else, a timeout, a refused connection,
    is retried on a backoff (a minute, five, thirty, two hours, twelve)
    and then given up; thirty failed attempts in a row switch the webhook
    off and say so in Settings, because a receiver that has been gone for
    days is not coming back by itself and the outbox should not grow
    forever behind it.

Events are written to an OUTBOX (`webhook_deliveries`) by the code that
observed them, and sent by the worker: a delivery must never run inside
the request or the sync that produced it (a slow receiver would make a
category change take ten seconds), and a row in the outbox survives a
restart where an in-memory queue would not. `emit` is therefore cheap —
one SELECT and a few INSERTs — and never raises: the thing that happened
is more important than telling someone about it.

Two things a receiver never gets: a secret (an aggregator credential, a
key) and another household's rows (every query here runs on the
tenant-scoped connection, under RLS). The URL itself goes through the
same address policy as every other tenant-supplied URL (`netguard`), so
on a hosted instance a webhook cannot point at the box's own network.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid

from ..db import crypto
from ..engine.compat import jsonb

log = logging.getLogger("oikonome.webhooks")

# The event catalogue: name → what it means, in the words the Settings
# card shows beside each checkbox. The order is the order of the list.
EVENTS: dict[str, str] = {
    "transactions.new":
        "New transactions landed in the ledger (per sync, up to 50 listed)",
    "sync.completed":
        "A bank sync finished — which connections, how many rows, any errors",
    "alert.raised":
        "An alert appeared on the Today page (anomaly, funding, stale sync…)",
    "connection.changed":
        "A bank connection broke or recovered",
    "report.daily":
        "The daily verdict — left today, month pace, bills due, at the "
        "household's report hour",
    "networth.snapshot":
        "The nightly net-worth snapshot was recorded",
    "activity.logged":
        "Someone in the household changed a category, bill, note, rule…",
    "test.ping":
        "The test button in Settings",
}
WILDCARD = "*"

# the retry ladder, seconds after the attempt that failed: a minute,
# five, thirty, two hours, twelve hours
_MIN, _HOUR = 60, 3600
BACKOFF = (_MIN, 5 * _MIN, 30 * _MIN, 2 * _HOUR, 12 * _HOUR)
MAX_ATTEMPTS = len(BACKOFF) + 1
# consecutive failed ATTEMPTS (across deliveries) before the app switches
# the webhook off — roughly five deliveries lost end to end
DISABLE_AFTER = 30
# how long a settled delivery stays for the Settings card
RETENTION_DAYS = 7
# what one drain pass sends per tenant
BATCH = 50
# the budget for any one network operation of an attempt (connect, one
# read, one write)
TIMEOUT = 10.0
# ...and for the whole attempt, end to end. A receiver that answers a byte
# at a time never trips a per-read timeout, so without a wall-clock limit
# it holds the worker thread, or the request thread and pooled connection
# behind the test button, for as long as it likes.
DEADLINE = 10.0
# how much of a refusal's body is read: only its first ERROR_MAX
# characters are kept, and a receiver may answer with gigabytes
READ_MAX = 4096
# the time one drain pass spends on one household before moving on; what
# is left stays due for the next minute's pass
TENANT_BUDGET = 30.0
# how far a claimed row's next_attempt_at moves while a pass holds it —
# far past TENANT_BUDGET + DEADLINE, so a pass that is still sending never
# sees its rows come due under it; a pass that died mid-send has its rows
# retried once the lease lapses
LEASE = dt.timedelta(minutes=10)
# how many rows a transactions.new payload lists (the count is exact)
LIST_MAX = 50
ERROR_MAX = 200
URL_MAX = 2000
NAME_MAX = 80
SECRET_PREFIX = "oikwh_"  # noqa: S105 — a prefix, not a secret

USER_AGENT = "Oikonome-Webhooks/" + (os.environ.get("OIKONOME_VERSION")
                                     or "dev")

# tests swap the transport; production leaves it None and the sender
# picks netguard's pinned transport (hosted) or httpx's default
_transport_override = None


class WebhookError(ValueError):
    """A request the API should refuse with the message as the reason."""


# ---- signing ---------------------------------------------------------------

def sign(secret: str, ts: int, body: bytes) -> str:
    """HMAC-SHA256 hex over ``"<ts>.<body>"``."""
    mac = hmac.new(secret.encode(), str(ts).encode() + b"." + body,
                   hashlib.sha256)
    return mac.hexdigest()


def signature_header(secret: str, ts: int, body: bytes) -> str:
    return f"t={ts},v1={sign(secret, ts, body)}"


def verify(secret: str, header: str, body: bytes,
           *, tolerance: int = 300, now: int | None = None) -> bool:
    """What a receiver does — here so the docs' example and the tests
    exercise the same arithmetic the sender uses."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs((now if now is not None else int(dt.datetime.now(
            dt.timezone.utc).timestamp())) - ts) > tolerance:
        return False
    return hmac.compare_digest(parts.get("v1", ""), sign(secret, ts, body))


# ---- the catalogue ----------------------------------------------------------

def normalize_events(events) -> list[str]:
    """The stored list: known names only, deduplicated, catalogue order;
    ``*`` alone means everything, now and later."""
    if not isinstance(events, (list, tuple)):
        raise WebhookError("events must be a list")
    names = {str(e).strip() for e in events}
    if WILDCARD in names:
        return [WILDCARD]
    unknown = sorted(n for n in names if n not in EVENTS)
    if unknown:
        raise WebhookError("unknown event: " + ", ".join(unknown[:3]))
    out = [e for e in EVENTS if e in names]
    if not out:
        raise WebhookError("pick at least one event")
    return out


def subscribed(row_events, event: str) -> bool:
    ev = list(row_events or [])
    return WILDCARD in ev or event in ev


# ---- managing hooks ---------------------------------------------------------

def _check_url(url: str) -> str:
    from ..envnum import env_flag
    from ..web.netguard import BlockedURL, check_url
    url = (url or "").strip()
    if not url or len(url) > URL_MAX:
        raise WebhookError("a URL is required")
    try:
        check_url(url, what="the webhook URL")
    except BlockedURL as e:
        raise WebhookError(str(e))
    if env_flag("OIKONOME_HOSTED") and not url.startswith("https://"):
        # the body carries the household's balances across the public
        # internet; a hosted instance only sends it encrypted
        raise WebhookError("the webhook URL must be https")
    return url


def _new_secret() -> str:
    return SECRET_PREFIX + secrets.token_hex(24)


def _public(r) -> dict:
    d = {k: v for k, v in dict(r).items() if k not in ("secret", "tenant_id")}
    for k, v in list(d.items()):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            d[k] = str(v)
    d["events"] = list(d.get("events") or [])
    return d


def create(conn, *, url: str, events, name: str = "",
           created_by: str = "") -> tuple[dict, str]:
    """→ (public row, plaintext secret). The secret is returned exactly
    once; only its encrypted form is stored."""
    url = _check_url(url)
    ev = normalize_events(events)
    secret = _new_secret()
    row = conn.execute(
        """INSERT INTO webhooks (url, name, secret, events, created_by)
           VALUES (%s, %s, %s, %s, %s)
           RETURNING *""",
        (url, (name or "").strip()[:NAME_MAX], crypto.encrypt(conn, secret),
         ev, (created_by or "")[:120])).fetchone()
    return _public(row), secret


def update(conn, webhook_id: str, *, url: str | None = None, events=None,
           name: str | None = None, enabled: bool | None = None) -> dict:
    sets, args = [], []
    if url is not None:
        sets.append("url = %s")
        args.append(_check_url(url))
    if events is not None:
        sets.append("events = %s")
        args.append(normalize_events(events))
    if name is not None:
        sets.append("name = %s")
        args.append(name.strip()[:NAME_MAX])
    if enabled is not None:
        sets.append("enabled = %s")
        args.append(bool(enabled))
        if enabled:
            # a person switching it back on is the one signal that the
            # receiver is worth trying again
            sets.append("failures = 0")
            sets.append("disabled_reason = NULL")
    if not sets:
        raise WebhookError("nothing to change")
    args.append(webhook_id)
    row = conn.execute(
        f"UPDATE webhooks SET {', '.join(sets)} WHERE id = %s RETURNING *",
        args).fetchone()
    if row is None:
        raise WebhookError("no such webhook")
    return _public(row)


def rotate_secret(conn, webhook_id: str) -> tuple[dict, str]:
    secret = _new_secret()
    row = conn.execute(
        "UPDATE webhooks SET secret = %s WHERE id = %s RETURNING *",
        (crypto.encrypt(conn, secret), webhook_id)).fetchone()
    if row is None:
        raise WebhookError("no such webhook")
    return _public(row), secret


def delete(conn, webhook_id: str) -> bool:
    row = conn.execute("DELETE FROM webhooks WHERE id = %s RETURNING id",
                       (webhook_id,)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM webhook_deliveries WHERE webhook_id = %s",
                 (webhook_id,))
    return True


def list_hooks(conn) -> list[dict]:
    return [_public(r) for r in conn.execute(
        "SELECT * FROM webhooks ORDER BY created_at").fetchall()]


def deliveries(conn, webhook_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT id, event, state, attempts, next_attempt_at, created_at,
                  delivered_at, response_status, last_error
             FROM webhook_deliveries
            WHERE webhook_id = %s
            ORDER BY created_at DESC LIMIT %s""",
        (webhook_id, max(1, min(int(limit), 100)))).fetchall()
    return [_public(r) for r in rows]


# ---- the outbox -------------------------------------------------------------

def wanted(conn, event: str) -> bool:
    """Is anyone listening? Cheap, so a producer can skip building a
    payload nobody will read."""
    try:
        return conn.execute(
            """SELECT 1 FROM webhooks
                WHERE enabled AND (%s = ANY(events) OR %s = ANY(events))
                LIMIT 1""", (event, WILDCARD)).fetchone() is not None
    except Exception as e:                                   # noqa: BLE001
        log.warning("webhooks: wanted(%s) failed: %s", event, e)
        return False


def emit(conn, event: str, data: dict) -> int:
    """Queue `event` for every enabled webhook subscribed to it. Returns
    the number of deliveries queued. Never raises."""
    if event not in EVENTS:
        log.warning("webhooks: unknown event %s not emitted", event)
        return 0
    try:
        hooks = conn.execute(
            """SELECT id FROM webhooks
                WHERE enabled AND (%s = ANY(events) OR %s = ANY(events))""",
            (event, WILDCARD)).fetchall()
        if not hooks:
            return 0
        body = jsonb(_jsonable(data))
        for h in hooks:
            conn.execute(
                """INSERT INTO webhook_deliveries (webhook_id, event, payload)
                   VALUES (%s, %s, %s)""", (h["id"], event, body))
        return len(hooks)
    except Exception as e:                                   # noqa: BLE001
        log.warning("webhooks: emit(%s) failed: %s", event, e)
        return 0


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "quantize"):                    # Decimal
        return float(v)
    return v


def _envelope(row) -> bytes:
    body = {"id": str(row["id"]), "event": row["event"],
            "created_at": row["created_at"].isoformat(),
            "attempt": int(row["attempts"]) + 1,
            "data": row["payload"]}
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


@functools.cache
def _deadline_backend():
    """An httpcore network backend whose every connect, TLS handshake,
    read and write gets the smaller of its own timeout and what is left
    of the attempt's deadline — which is what turns httpx's per-operation
    timeouts into a limit on the whole attempt."""
    import httpcore

    def left(deadline: float, timeout, exc):
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise exc("the attempt ran past its deadline")
        return rem if timeout is None else min(timeout, rem)

    class Stream(httpcore.NetworkStream):
        def __init__(self, inner, deadline: float):
            self._inner, self._deadline = inner, deadline

        def read(self, max_bytes, timeout=None):
            return self._inner.read(max_bytes, left(
                self._deadline, timeout, httpcore.ReadTimeout))

        def write(self, buffer, timeout=None):
            self._inner.write(buffer, left(
                self._deadline, timeout, httpcore.WriteTimeout))

        def close(self):
            self._inner.close()

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            return Stream(self._inner.start_tls(
                ssl_context, server_hostname,
                left(self._deadline, timeout, httpcore.ConnectTimeout)),
                self._deadline)

        def get_extra_info(self, info):
            return self._inner.get_extra_info(info)

    class Backend(httpcore.NetworkBackend):
        def __init__(self, inner, deadline: float):
            self._inner, self._deadline = inner, deadline

        def connect_tcp(self, host, port, timeout=None, local_address=None,
                        socket_options=None):
            return Stream(self._inner.connect_tcp(
                host, port,
                left(self._deadline, timeout, httpcore.ConnectTimeout),
                local_address, socket_options), self._deadline)

        def connect_unix_socket(self, path, timeout=None,
                                socket_options=None):
            return Stream(self._inner.connect_unix_socket(
                path, left(self._deadline, timeout, httpcore.ConnectTimeout),
                socket_options), self._deadline)

        def sleep(self, seconds):
            self._inner.sleep(seconds)

    return Backend


def _with_deadline(client, deadline: float):
    """`client` with the attempt's deadline under every socket operation of
    every transport it may route through: its own (netguard's pinned one on
    hosted, httpx's default on self-host) and each proxy mount httpx built
    from HTTP(S)_PROXY / ALL_PROXY. The client is made with no transport on
    self-host precisely so those mounts exist — an explicit transport turns
    httpx's environment proxy handling off, and an instance whose only way
    out is an egress proxy would fail every delivery until the hook
    disabled itself. httpx has no public knob for the network backend, so
    it is set on each transport's pool; the trickling-receiver tests fail
    if an upgrade moves it."""
    wrap = _deadline_backend()
    for t in (client._transport, *client._mounts.values()):
        if t is None:
            continue
        pool = t._pool
        pool._network_backend = wrap(pool._network_backend, deadline)
    return client


def _first_words(r, deadline: float) -> str:
    """The start of a refusal's body — at most READ_MAX raw bytes, and
    only while the attempt's deadline lasts. Raw, never decoded: a
    compressed body could inflate a few KB into gigabytes."""
    import httpx
    got = b""
    try:
        for chunk in r.iter_raw():
            got += chunk
            if len(got) >= READ_MAX or time.monotonic() >= deadline:
                break
    except httpx.StreamConsumed:
        # a response built already read (a test transport) has its body
        # in memory, so there is nothing left to bound
        got = r.content
    return " ".join(got[:READ_MAX].decode("utf-8", "replace").split())


def _send(url: str, secret: str, event: str, delivery_id: str,
          body: bytes) -> tuple[int | None, str | None]:
    """One HTTP attempt → (status or None, error or None). Never raises,
    and never takes longer than DEADLINE."""
    import httpx

    from ..web.netguard import BlockedURL, check_url, pinned_transport
    try:
        # re-checked at send time: the policy may have changed since the
        # URL was saved, and a restore can plant any URL at all
        check_url(url, what="the webhook URL")
    except BlockedURL as e:
        return None, str(e)[:ERROR_MAX]
    ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        # the body is read raw and only its first bytes; a compressed one
        # would be both unreadable and a way to make a few bytes large
        "Accept-Encoding": "identity",
        "X-Oikonome-Event": event,
        "X-Oikonome-Delivery": delivery_id,
        "X-Oikonome-Signature": signature_header(secret, ts, body),
    }
    deadline = time.monotonic() + DEADLINE
    try:
        # None on self-host, so httpx mounts the environment's proxies
        client = httpx.Client(timeout=TIMEOUT, follow_redirects=False,
                              transport=_transport_override
                              or pinned_transport())
        with client:
            if _transport_override is None:
                _with_deadline(client, deadline)
            # streamed: a 2xx is settled on its status line and nothing
            # more is read; a refusal is read only as far as its first words
            with client.stream("POST", url, content=body,
                               headers=headers) as r:
                if 200 <= r.status_code < 300:
                    return r.status_code, None
                text = _first_words(r, deadline)[:ERROR_MAX]
        return r.status_code, f"HTTP {r.status_code}" + (f": {text}"
                                                          if text else "")
    except BlockedURL as e:
        return None, str(e)[:ERROR_MAX]
    except Exception as e:                                   # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"[:ERROR_MAX]


def _settle(conn, row, ok: bool, status, error, now) -> bool:
    """Write the attempt's outcome to the delivery and the webhook.

    Only when the row is still the attempt that was read — pending, with
    the attempt count this pass saw. Otherwise another pass settled it
    first, and a late failure must neither reopen a delivered row nor
    count against the hook. Returns whether the outcome was written."""
    attempts = int(row["attempts"]) + 1
    if ok:
        cur = conn.execute(
            """UPDATE webhook_deliveries
                  SET state = 'delivered', attempts = %s, delivered_at = %s,
                      response_status = %s, last_error = NULL
                WHERE id = %s AND state = 'pending' AND attempts = %s""",
            (attempts, now, status, row["id"], attempts - 1))
        if not cur.rowcount:
            return False
        conn.execute(
            """UPDATE webhooks
                  SET failures = 0, last_attempt_at = %s, last_status = %s,
                      last_error = NULL
                WHERE id = %s""", (now, status, row["webhook_id"]))
        return True
    final = attempts >= MAX_ATTEMPTS
    nxt = None if final else now + dt.timedelta(
        seconds=BACKOFF[min(attempts - 1, len(BACKOFF) - 1)])
    cur = conn.execute(
        """UPDATE webhook_deliveries
              SET state = %s, attempts = %s, next_attempt_at = %s,
                  response_status = %s, last_error = %s
            WHERE id = %s AND state = 'pending' AND attempts = %s""",
        ("failed" if final else "pending", attempts,
         nxt or row["next_attempt_at"], status, error, row["id"],
         attempts - 1))
    if not cur.rowcount:
        return False
    hook = conn.execute(
        """UPDATE webhooks
              SET failures = failures + 1, last_attempt_at = %s,
                  last_status = %s, last_error = %s
            WHERE id = %s
            RETURNING failures, enabled""",
        (now, status, error, row["webhook_id"])).fetchone()
    if hook and hook["enabled"] and hook["failures"] >= DISABLE_AFTER:
        conn.execute(
            """UPDATE webhooks
                  SET enabled = FALSE,
                      disabled_reason = %s
                WHERE id = %s""",
            (f"switched off after {hook['failures']} failed attempts in "
             f"a row (last: {error or status})"[:ERROR_MAX + 80],
             row["webhook_id"]))
        # nothing further will be tried until a person turns it back on
        conn.execute(
            """UPDATE webhook_deliveries SET state = 'failed'
                WHERE webhook_id = %s AND state = 'pending'""",
            (row["webhook_id"],))
    return True


def _claim(conn, now: dt.datetime, limit: int) -> list:
    """Take up to `limit` due rows for this pass, oldest first. The same
    statement that picks a row moves its next_attempt_at a LEASE ahead, so
    a pass that starts while this one is still sending (the next minute's
    cron, a second worker) finds none of them due; SKIP LOCKED keeps two
    claims racing inside one instant off the same row. `due_at` is when
    the row was due, for handing back the ones this pass does not try."""
    rows = conn.execute(
        """WITH due AS (
               SELECT d.id, d.next_attempt_at AS due_at
                 FROM webhook_deliveries d
                 JOIN webhooks w ON w.id = d.webhook_id
                WHERE d.state = 'pending' AND d.next_attempt_at <= %s
                  AND w.enabled
                ORDER BY d.next_attempt_at, d.created_at
                LIMIT %s
                  FOR UPDATE OF d SKIP LOCKED)
           UPDATE webhook_deliveries d
              SET next_attempt_at = %s
             FROM due
            WHERE d.id = due.id
        RETURNING d.*, due.due_at""", (now, limit, now + LEASE)).fetchall()
    return sorted(rows, key=lambda r: (r["due_at"], r["created_at"]))


def _release(conn, rows) -> None:
    """Hand claimed rows back untried: due again when they were due, no
    attempt counted."""
    for r in rows:
        conn.execute(
            """UPDATE webhook_deliveries SET next_attempt_at = %s
                WHERE id = %s AND state = 'pending' AND attempts = %s""",
            (r["due_at"], r["id"], r["attempts"]))


def deliver_pending(conn, *, now: dt.datetime | None = None,
                    limit: int = BATCH) -> dict:
    """Send every due delivery for the connection's tenant, within
    TENANT_BUDGET. Returns {sent, failed}. One attempt per row; rows are
    claimed before anything is sent, and each row's hook is read again
    just before its send, so a hook switched off mid-batch — by its owner,
    or by the failure that crossed DISABLE_AFTER — gets nothing more."""
    now = now or dt.datetime.now(dt.timezone.utc)
    started = time.monotonic()
    # a hook the owner switched off keeps its queue (switching it back
    # on resumes it) but must not fill the batch with rows nobody will
    # send — so the filter is in the claim, not only in the loop
    rows = _claim(conn, now, limit)
    out = {"sent": 0, "failed": 0}
    secrets_by_hook: dict[tuple[str, str], str] = {}
    for i, row in enumerate(rows):
        if time.monotonic() - started >= TENANT_BUDGET:
            # one slow receiver must not keep the pass on this household;
            # the rest go to the next pass, due as they were
            _release(conn, rows[i:])
            break
        hook = conn.execute(
            "SELECT url, secret, enabled FROM webhooks WHERE id = %s",
            (row["webhook_id"],)).fetchone()
        if hook is None or not hook["enabled"]:
            _release(conn, [row])
            continue
        key = (str(row["webhook_id"]), hook["secret"])
        if key not in secrets_by_hook:
            try:
                secrets_by_hook[key] = crypto.decrypt(conn, hook["secret"])
            except Exception as e:                           # noqa: BLE001
                # an undecryptable secret (a restore under another key)
                # is a permanent failure for this hook
                _settle(conn, row, False, None,
                        f"secret unreadable: {type(e).__name__}", now)
                out["failed"] += 1
                continue
        status, error = _send(hook["url"], secrets_by_hook[key],
                              row["event"], str(row["id"]), _envelope(row))
        ok = error is None
        _settle(conn, row, ok, status, error, now)
        out["sent" if ok else "failed"] += 1
    return out


def ping_prepare(conn, webhook_id: str,
                 *, now: dt.datetime | None = None) -> dict:
    """First half of the Settings button: queue a test.ping for this hook
    and read what the send needs. The row is written already leased, so
    the minute drain cannot send the same ping while this attempt is
    open; if the request dies mid-attempt, the drain retries it once the
    lease lapses. Returns the attempt for `ping_send` / `ping_settle`, or,
    when the secret cannot be read, the settled result (`"done"`)."""
    hook = conn.execute("SELECT * FROM webhooks WHERE id = %s",
                        (webhook_id,)).fetchone()
    if hook is None:
        raise WebhookError("no such webhook")
    now = now or dt.datetime.now(dt.timezone.utc)
    data = {"message": "Hello from Oikonome — this webhook is wired up.",
            "webhook": {"id": str(hook["id"]), "name": hook["name"]},
            "at": now}
    row = conn.execute(
        """INSERT INTO webhook_deliveries (webhook_id, event, payload,
                                           next_attempt_at)
           VALUES (%s, 'test.ping', %s, %s) RETURNING *""",
        (hook["id"], jsonb(_jsonable(data)), now + LEASE)).fetchone()
    try:
        secret = crypto.decrypt(conn, hook["secret"])
    except Exception as e:                                   # noqa: BLE001
        error = f"secret unreadable: {type(e).__name__}"
        _settle(conn, row, False, None, error, now)
        return {"done": {"ok": False, "status": None, "error": error,
                         "delivery_id": str(row["id"])}}
    return {"row": row, "url": hook["url"], "secret": secret, "now": now}


def ping_send(attempt: dict) -> tuple[int | None, str | None]:
    """The network half: needs no connection, so the caller holds none
    while a receiver takes up to DEADLINE to answer."""
    row = attempt["row"]
    return _send(attempt["url"], attempt["secret"], "test.ping",
                 str(row["id"]), _envelope(row))


def ping_settle(conn, attempt: dict, status, error) -> dict:
    """Last half: a failure is settled like any other (it counts toward
    the ladder), and the row stays in the list."""
    row = attempt["row"]
    _settle(conn, row, error is None, status, error, attempt["now"])
    return {"ok": error is None, "status": status, "error": error,
            "delivery_id": str(row["id"])}


def prune(conn, days: int = RETENTION_DAYS) -> int:
    """Nightly: drop settled deliveries older than the retention."""
    cur = conn.execute(
        """DELETE FROM webhook_deliveries
            WHERE state <> 'pending'
              AND created_at < now() - make_interval(days => %s)""",
        (int(days),))
    return cur.rowcount or 0
