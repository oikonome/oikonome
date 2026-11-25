"""Nightly retention for the logs that only machines read.

Each table here is pruned to what its readers can still ask for, and no
further. A table whose rows a person reads as history is pruned only to a
window the product chose (alerts_log below): whether to forget part of a
household's history is a product decision, not a performance one.

  sync_log       one row per pull. Readers: each connection's newest
                 success (status chips, the reaper's quiet-item check),
                 the failures after it (the reaper's failure streak, the
                 last error shown in diagnostics), whether a connection
                 was ever pulled, the last few seconds of attempts (the
                 onboarding backfill poll), the console's newest twenty
                 rows, and the last day of errors. A row older than
                 SYNC_LOG_KEEP_DAYS is dropped only when a NEWER success
                 of the same connection exists, so each connection's
                 newest success and every failure after it stay, as does
                 every row of a connection that never succeeded (the
                 reaper then reads all of them) and every bookkeeping row
                 (an import undone, an account purged: those are written
                 as errors, so they never have a newer success).
  login_unlocks  emailed sign-in unlock links. Every reader asks only
                 about a live link or a live exemption, or counts the
                 links minted in the last hour, so a row whose link and
                 window both closed UNLOCK_KEEP_DAYS ago is never read.

  alerts_log     the household's alert history. Kept HISTORY_KEEP_DAYS
                 past an alert's last sighting. A standing alert (active)
                 is current state, not history, and is never dropped. A
                 dismissed row is what keeps a dismissed alert hidden, but
                 the alert writer advances last_seen on every sighting,
                 dismissed or not, so a dismissed alert that still fires
                 always looks young here; only one that has not been seen
                 for the whole window goes, and if it ever returns it is
                 news again.
  admin_audit    the operator's audit trail (control plane; it survives a
                 household's erasure, and still does — this trims by age,
                 never by tenant). Kept HISTORY_KEEP_DAYS. Its machine
                 readers look back at most ninety days or ask "was this
                 already recorded once", where a re-record after the window
                 is one more line, not a wrong decision.

Already pruned elsewhere: the activity log (engine/activity.prune, its
own retention window), the logo cache (web/logos._prune, size caps),
sessions (auth/sessions), webhook deliveries (notify/webhooks.prune).

Deletes are batched, so a first pass over a years-old log never holds one
long transaction, and every pass is idempotent: a second run finds
nothing. No household lock is taken: a row this pass drops has a newer
success above it, and nothing a sync writes concurrently can make an
older row visible to a reader again.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SYNC_LOG_KEEP_DAYS = 180
UNLOCK_KEEP_DAYS = 30
# A year plus the quarter that reads it: a yearly review (or a tax-season
# look back at last year's alerts and operator actions) still finds the
# whole previous year a quarter after it closed.
HISTORY_KEEP_DAYS = 400
BATCH = 5000

_SWEEP_LOCK = "oikonome:retention"


def prune_sync_log(conn, keep_days: int = SYNC_LOG_KEEP_DAYS,
                   batch: int = BATCH) -> int:
    """Drop the sync_log rows no reader can still see, `batch` at a time.
    On a tenant-scoped connection this prunes that household; on the owner
    connection, every household (the newer-success test is per household).
    Returns the number of rows deleted."""
    total = 0
    while True:
        n = conn.execute(
            """DELETE FROM sync_log d
                USING (SELECT s.tenant_id, s.id FROM sync_log s
                        WHERE s.ran_at < now() - make_interval(days => %s)
                          AND EXISTS (
                              SELECT 1 FROM sync_log n
                               WHERE n.tenant_id = s.tenant_id
                                 AND n.item_id = s.item_id
                                 AND n.error IS NULL
                                 AND n.ran_at > s.ran_at)
                        LIMIT %s) doomed
               WHERE d.tenant_id = doomed.tenant_id AND d.id = doomed.id""",
            (int(keep_days), int(batch))).rowcount
        total += n
        if n < batch:
            return total


def prune_login_unlocks(admin, keep_days: int = UNLOCK_KEEP_DAYS) -> int:
    """Drop unlock links whose link and exemption window both closed more
    than `keep_days` ago. Control-plane table: the owner connection."""
    return admin.execute(
        """DELETE FROM login_unlocks
            WHERE created_at < now() - make_interval(days => %s)
              AND expires_at < now() - make_interval(days => %s)
              AND (unlocked_until IS NULL
                   OR unlocked_until < now() - make_interval(days => %s))""",
        (int(keep_days),) * 3).rowcount


def prune_alerts_log(conn, keep_days: int = HISTORY_KEEP_DAYS,
                     batch: int = BATCH) -> int:
    """Drop alert rows that stopped standing and were last seen more than
    `keep_days` ago, `batch` at a time. On a tenant-scoped connection this
    prunes that household; on the owner connection, every household (the
    cutoff is the same for all, so one pass is the per-household pass)."""
    total = 0
    while True:
        n = conn.execute(
            """DELETE FROM alerts_log d
                USING (SELECT tenant_id, id FROM alerts_log
                        WHERE active = 0
                          AND last_seen < current_date - %s
                        LIMIT %s) doomed
               WHERE d.tenant_id = doomed.tenant_id AND d.id = doomed.id
                 -- said again on the row being deleted: a sync that sights
                 -- the alert again between the pick and the delete makes it
                 -- wait on the row, and the wait re-reads only these tests
                 AND d.active = 0
                 AND d.last_seen < current_date - %s""",
            (int(keep_days), int(batch), int(keep_days))).rowcount
        total += n
        if n < batch:
            return total


def prune_admin_audit(admin, keep_days: int = HISTORY_KEEP_DAYS,
                      batch: int = BATCH) -> int:
    """Drop audit rows older than `keep_days`, `batch` at a time.
    Control-plane table: the owner connection."""
    total = 0
    while True:
        n = admin.execute(
            """DELETE FROM admin_audit
                WHERE id IN (SELECT id FROM admin_audit
                              WHERE at < now() - make_interval(days => %s)
                              LIMIT %s)""",
            (int(keep_days), int(batch))).rowcount
        total += n
        if n < batch:
            return total


def sweep() -> dict:
    """The nightly pass over every table above, on the owner connection.
    Single-flight: a console run-now overlapping the cron's run skips."""
    from ..db import tenancy
    admin = tenancy.admin_connect()
    try:
        if not admin.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (_SWEEP_LOCK,)).fetchone()["ok"]:
            log.info("retention sweep already running — skipping")
            return {"skipped": "in-flight"}
        try:
            out = {"sync_log": prune_sync_log(admin),
                   "login_unlocks": prune_login_unlocks(admin),
                   "alerts_log": prune_alerts_log(admin),
                   "admin_audit": prune_admin_audit(admin)}
        finally:
            admin.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (_SWEEP_LOCK,))
        if any(out.values()):
            log.info("retention: %s", out)
        return out
    finally:
        admin.close()
