"""Staged imports awaiting a second request.

Three flows park an upload between requests: the bulk plan (analyze →
run), a CSV that needs manual column mapping, and taxdoc review. They
each had a per-process dict — invisible to other workers (the follow-up
request 404'd if a different process answered it), silently evicted at
a fixed count, and the taxdoc one wasn't tenant-bound at all. The rows
live in `import_staging` now: RLS makes tenant binding the database's
job, any worker can finish what another started, and eviction is
explicit — a TTL (stale stages are junk, not treasure) plus a
per-tenant byte budget (without one, an authenticated owner could
park gigabytes of "pending" uploads on a shared node).

Every call takes a TENANT-SCOPED connection; nothing here ever names a
tenant_id.
"""

from __future__ import annotations

import secrets

from ..engine.compat import jsonb

TTL_MINUTES = 60
TENANT_BUDGET = 200 * 1024 * 1024      # staged bytes per tenant, all stages


def put(conn, kind: str, files: list[tuple[str, bytes]] | None = None,
        meta: dict | None = None) -> str:
    """Stage files/metadata; returns the claim token. Raises ValueError
    when the tenant's staging budget is exhausted (the caller surfaces
    it — an explicit refusal beats the old silent eviction)."""
    _sweep(conn)
    files = files or [("", b"")]
    incoming = sum(len(d or b"") for _, d in files)
    token = secrets.token_urlsafe(16)
    # One transaction with a per-tenant advisory lock around the SUM and
    # the INSERTs: two concurrent uploads used to read the same `held`
    # before either wrote, and both fit under a budget only one of them
    # actually left room for — the exact "park gigabytes on a shared
    # node" the budget exists to stop, just needing parallel requests.
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext("
            "'oikonome:staging:' || current_setting('app.tenant_id')))")
        held = conn.execute(
            "SELECT COALESCE(SUM(octet_length(data)), 0) AS n "
            "FROM import_staging").fetchone()["n"]
        if held + incoming > TENANT_BUDGET:
            raise ValueError(
                f"staging area is full "
                f"({(held + incoming) // (1024*1024)} MB "
                f"pending, budget {TENANT_BUDGET // (1024*1024)} MB) — "
                f"finish or abandon the imports in flight, or upload "
                f"smaller batches")
        for i, (name, data) in enumerate(files):
            conn.execute(
                """INSERT INTO import_staging (token, idx, kind, filename,
                                               data, meta)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (token, i, kind, name or None, data or b"",
                 jsonb(meta) if meta and i == 0 else None))
    return token


def pop(conn, kind: str, token: str) -> dict | None:
    """Consume a stage: delete + return {files: [(name, bytes)], meta} —
    or None (unknown/expired token, wrong kind, or another tenant's:
    RLS makes those indistinguishable, which is the point)."""
    _sweep(conn)
    rows = conn.execute(
        """DELETE FROM import_staging WHERE token = %s AND kind = %s
           RETURNING idx, filename, data, meta""",
        (token, kind)).fetchall()
    if not rows:
        return None
    rows.sort(key=lambda r: r["idx"])
    return {"files": [(r["filename"] or "", bytes(r["data"] or b""))
                      for r in rows],
            "meta": next((r["meta"] for r in rows if r["meta"]), None)}


def peek(conn, kind: str, token: str) -> dict | None:
    """Read a stage WITHOUT consuming it — same shape as `pop`, same
    None for a token that is unknown, expired, the wrong kind or another
    tenant's. For the checks that must run before the upload is spent:
    a refusal that consumes nothing must leave the stage where the
    client's token still points, or the client holds a dead token and
    the person re-uploads a file the server still had."""
    _sweep(conn)
    rows = conn.execute(
        """SELECT idx, filename, data, meta FROM import_staging
            WHERE token = %s AND kind = %s ORDER BY idx""",
        (token, kind)).fetchall()
    if not rows:
        return None
    return {"files": [(r["filename"] or "", bytes(r["data"] or b""))
                      for r in rows],
            "meta": next((r["meta"] for r in rows if r["meta"]), None)}


def _sweep(conn) -> None:
    conn.execute(
        f"DELETE FROM import_staging WHERE created_at < "
        f"now() - interval '{TTL_MINUTES} minutes'")
