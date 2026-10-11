"""Staged imports awaiting a second request.

Three flows park an upload between requests: the bulk plan (analyze →
submit, when the files move into a background job), a CSV that needs
manual column mapping, and taxdoc review. The
rows live in `import_staging`: RLS makes tenant binding the database's
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
    it — an explicit refusal, never a silent eviction)."""
    _sweep(conn)
    files = files or [("", b"")]
    incoming = sum(len(d or b"") for _, d in files)
    token = secrets.token_urlsafe(16)
    # One transaction with a per-tenant advisory lock around the SUM and
    # the INSERTs: without it two concurrent uploads read the same `held`
    # before either writes, and both fit under a budget only one of them
    # actually leaves room for — the exact "park gigabytes on a shared
    # node" the budget exists to stop, just needing parallel requests.
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext("
            "'oikonome:staging:' || current_setting('app.tenant_id')))")
        # A folder import moves its bytes out of the stage into its job
        # (bulk_import_files) and keeps them until each file has run; they
        # count here too, or moving an upload into a job would free its
        # room and the household could park a budget's worth in jobs, stage
        # another, and repeat.
        held = conn.execute(
            "SELECT (SELECT COALESCE(SUM(octet_length(data)), 0) "
            "          FROM import_staging)"
            "     + (SELECT COALESCE(SUM(octet_length(data)), 0) "
            "          FROM bulk_import_files) AS n").fetchone()["n"]
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


def sweep_all(admin=None) -> int:
    """Drop every household's expired stages; returns how many rows went.

    The sweep inside put/pop/peek only ever reaches the household making
    the call, so an upload that is analyzed and then abandoned stays until
    that same household stages again, which may be never. A staged upload
    is the file as it arrived, before the importers cut account and routing
    numbers down to their last four, so it must not outlive its hour, nor
    ride along in every backup meanwhile. This one runs on the owner
    connection (no tenant scope, so every household's rows) and is meant
    for a periodic job; the per-household sweep stays as the backstop."""
    from . import tenancy
    conn = admin if admin is not None else tenancy.admin_connect()
    try:
        n = conn.execute(
            "DELETE FROM import_staging WHERE created_at < "
            "now() - make_interval(mins => %s)", (TTL_MINUTES,)).rowcount
        # A folder import holds its files as uploaded until each has run.
        # Settle the jobs far past any run they could still be having,
        # clear the bytes of every settled one, and forget week-old jobs.
        # Best effort: a failure here must not cost the stage sweep above.
        try:
            from ..jobs import bulk_import_job
            bulk_import_job.expire(conn)
        except Exception:                                # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "folder import sweep failed", exc_info=True)
        return n
    finally:
        if admin is None:
            conn.close()
