"""Unified alerts: one structured list for the strip + a persistent log.

The strip, the log and the dismiss machinery are shared by every source.
The SOURCES folded by build() are deliberately provider-neutral: anomalies,
recurring proposals, merchant merge offers, the drift digest, awaiting
reimbursements, stale pulls,
and the budget.cash_events trio — investment funding, large transfer, bonus.
An alert that could only make sense for one institution does not belong
here; sources get added as the features behind them land.

Severities: good (green — actionable good news), info (neutral),
warn (amber), bad (red).

Dismissal semantics: dismiss hides an alert
while its condition PERSISTS; when the condition clears, dismissal resets
so a later recurrence shows again — recurrence is news.
"""

import datetime as dt
from urllib.parse import quote


def _mmdd(s) -> str:
    """MM/DD/YY from an ISO-ish string or date (module-wide — one copy)."""
    s = str(s)
    return f"{s[5:7]}/{s[8:10]}/{s[2:4]}" if len(s) >= 10 else (s or "never")


# ---- data-pull freshness watchdog ------------------------------------------
# Every pull job writes a heartbeat on SUCCESSFUL completion; a heartbeat
# older than its threshold raises an alert. Data-side evidence catches every
# failure mode: a dead scheduler, a crash, and a job that "succeeds" while
# doing nothing.

SYNC_MAX_HOURS = 6       # aggregator sync cadence tolerance


def heartbeat(conn, job: str, note: str = "", zone: str | None = None):
    """`zone` is the IANA name the run's local day was reckoned in — the
    scheduled cadences pass it so a later zone change cannot make the same
    local day look new (see worker.emails_due)."""
    conn.execute(
        """INSERT INTO job_runs (job, ran_at, note, zone)
           VALUES (%s, now(), %s, %s)
           ON CONFLICT (tenant_id, job)
           DO UPDATE SET ran_at=now(), note=EXCLUDED.note,
                         zone=EXCLUDED.zone""", (job, note, zone))


def freshness(conn, jobs: list[tuple[str, str, int]] | None = None,
              now_utc: dt.datetime | None = None) -> list[dict]:
    """Stale-pull alerts. `jobs` = [(job key, label, max age hours)] for
    heartbeat-tracked jobs; aggregator sync is judged from sync_log."""
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    out: list[dict] = []
    last_ok = conn.execute(
        """SELECT MAX(ran_at) AS t FROM sync_log WHERE error IS NULL""").fetchone()
    # file-only instances (manual/csv items) have no sync to be stale
    has_items = conn.execute(
        "SELECT 1 FROM items WHERE COALESCE(status,'') != 'archived' "
        "AND aggregator IN ('simplefin','simplefin-org','plaid','mx',"
        "'coinbase') LIMIT 1").fetchone()
    if has_items:
        if last_ok["t"] is None:
            out.append({"kind": "stale-pull", "severity": "warn",
                        "link": "/accounts",
                        "message": "Bank sync has never completed — check the "
                                   "connection on the Accounts page."})
        elif (now - last_ok["t"]).total_seconds() > SYNC_MAX_HOURS * 3600:
            out.append({"kind": "stale-pull", "severity": "warn",
                        "link": "/accounts",
                        "message": f"Bank sync last succeeded "
                                   f"{_mmdd(last_ok['t'].date().isoformat())} — "
                                   f"data may be stale."})
    # a collector whose script token was revoked after its last push (a
    # password change or reset does that to every token) is not "stale",
    # it is dead until a new token is minted — say which, on the strip
    # and in the email, not only on the Accounts page
    try:
        from ..sync import heartbeat as _hb
        for source, when in _hb.revoked_tokens(conn).items():
            out.append({"kind": "collector-token-revoked", "severity": "warn",
                        "link": "/settings/scripts",
                        "message": f"The {source} collector's script token "
                                   f"was revoked {_mmdd(when.date().isoformat())}"
                                   f" — mint a new one under Settings → "
                                   f"Scripts and update the host."})
    except Exception:                                    # noqa: BLE001
        pass
    hb = {r["job"]: r["ran_at"] for r in conn.execute(
        "SELECT job, ran_at FROM job_runs").fetchall()}
    for job, label, max_h in (jobs or []):
        t = hb.get(job)
        if t is None or (now - t).total_seconds() > max_h * 3600:
            when = _mmdd(t.date().isoformat()) if t else "never"
            out.append({"kind": "stale-pull", "severity": "warn",
                        "link": "/accounts",
                        "message": f"{label} hasn't succeeded since {when}."})
    return out


# ---- the model backend --------------------------------------------------


def llm_failing(conn) -> dict | None:
    """One alert while the last nightly LLM run failed outright — the
    model backend answering 404 (model gone), unreachable, or timing out
    with nothing produced. Amazon summaries and merchant categories stop
    the same night and nothing else says so; this is the Today strip and
    daily-email line that does. Clears on the next run that produces."""
    from . import llm_categorize as llm
    last = llm.last_run(conn)
    if not last or last.get("state") != "error":
        return None
    when = last.get("at")
    when_s = _mmdd(when.date().isoformat()) if when else "recently"
    pending = last.get("pending") or 0
    waiting = (f" — {pending} merchants and Amazon orders are waiting"
               if pending else "")
    return {"kind": "llm-backend", "severity": "warn", "link": "/doctor",
            "message": f"Smart categorization failed on its last run "
                       f"({when_s}): {last.get('error')}{waiting}. "
                       f"Check the Doctor page."}


# ---- awaiting reimbursements -------------------------------------------


def reimb_pending(conn) -> dict | None:
    """Rollup for the awaiting-reimbursement alert: {count, total, oldest}
    or None when nothing is outstanding.

    `total` is what is STILL owed, not the charges' face value —
    COALESCE(expected, amount) minus the partial receipts already recorded
    against each charge (the remaining-balance model), floored
    at 0. The link path deletes a flag once enough came back, but a charge
    mid-way through partial receipts stays flagged with money already
    received — summing the charges would keep alerting on the full amount."""
    rows = conn.execute(
        """SELECT t.date,
                  GREATEST(COALESCE(f.expected, t.amount)
                           - COALESCE((SELECT SUM(pr.amount)
                                       FROM reimbursements pr
                                       WHERE pr.expense_id = t.id
                                         AND pr.partial = 1), 0),
                           0) AS outstanding
           FROM reimburse_flags f
           JOIN transactions t ON t.id = f.txn_id AND t.removed = 0"""
        ).fetchall()
    open_ = [r for r in rows if (r["outstanding"] or 0) > 0.005]
    if not open_:
        return None
    return {"count": len(open_),
            "total": round(sum(r["outstanding"] for r in open_), 2),
            "oldest": min(r["date"] for r in open_)}


# ---- the strip ---------------------------------------------------------


def reaped_connections(conn) -> list[dict]:
    """Auto-removed bank connections pending a reconnect.
    The reaper (jobs/reaper.py) archives Plaid Items dead 30+ days with
    archived_reason 'auto-reap' (we released it) or 'plaid-gone' (it was
    already gone at Plaid). Alert until a live plaid item for the same
    institution exists again (= the user reconnected) — dismissal hides
    it meanwhile like any other alert. This is the daily-email one-liner:
    the strip renders in the email and the Today page alike."""
    try:
        rows = conn.execute(
            """SELECT i.institution_name, i.archived_reason
               FROM items i
               WHERE i.status = 'archived'
                 AND i.archived_reason IN ('auto-reap','plaid-gone','sub-ended')
                 AND NOT EXISTS (
                     SELECT 1 FROM items i2
                     WHERE i2.aggregator = 'plaid'
                       AND COALESCE(i2.status,'') != 'archived'
                       AND ((i2.institution_id IS NOT NULL
                             AND i2.institution_id = i.institution_id)
                            OR i2.institution_name = i.institution_name))
               ORDER BY i.institution_name""").fetchall()
    except Exception:                                     # noqa: BLE001
        return []
    from ..jobs.reaper import reap_days
    days = reap_days() or 30
    out = []
    for r in rows:
        name = r["institution_name"] or "A bank connection"
        if r["archived_reason"] == "plaid-gone":
            msg = (f"{name}: bank connection no longer exists at Plaid — "
                   f"reconnect it on the Accounts page to resume syncing.")
        else:
            msg = (f"{name}: bank connection removed after {days} days of "
                   f"failure — reconnect it on the Accounts page to resume "
                   f"syncing.")
        out.append({"kind": "connection-removed", "severity": "warn",
                    "message": msg})
    return out


def build(status: dict) -> list[dict]:
    """Fold the gathered status fields into one ordered alert list
    (worst first). Messages are plain text — they live in the log too."""
    out: list[dict] = []

    def add(kind, severity, message, link=None):
        # `link` is an optional in-app path the message points at, so an
        # alert that names a thing you'd want to change can be clicked
        # through to the page that changes it. Deliberately NOT part of the
        # (kind, message) identity used for logging and dismissal — retargeting
        # a link must not resurrect a dismissed alert.
        a = {"kind": kind, "severity": severity, "message": message}
        if link:
            a["link"] = link
        out.append(a)

    # investment-funding / large-transfer / bonus sources, fed by
    # budget.cash_events via status['events']. The wording names no
    # institution — any brokerage produces these rows.
    ev = status.get("events") or {}
    if ev.get("funding_mtd"):
        n = len(ev["funding_mtd"])
        add("funding", "bad",
            f"${ev['funding_mtd_total']:,.0f} pulled from investments this "
            f"month ({n} transfer{'s' if n > 1 else ''}) to cover spending — "
            f"${ev['funding_ytd_total']:,.0f} year to date (excl. major "
            f"purchases).",
            link="/networth")
    for a in status.get("anomalies") or []:
        add("anomaly", "warn", a,
            link="/transactions")
    rp = status.get("reimb_pending")
    if rp:
        oldest = rp.get("oldest") or ""
        try:
            age = (dt.date.today() - dt.date.fromisoformat(str(oldest)[:10])).days
        except ValueError:
            age = 0
        add("reimb", "warn" if age >= 45 else "info",
            f"{rp['count']} charge{'s' if rp['count'] > 1 else ''} awaiting "
            f"reimbursement (${rp['total']:,.0f}, oldest {_mmdd(oldest)}) — "
            f"match on the Reimburse page when the check arrives.",
            link="/reimburse")
    if status.get("needs_recurring_setup"):
        add("setup", "info",
            "Your transactions are in, but no recurring bills are tracked "
            "yet — run the finder on the Bills page so the budget can "
            "tell fixed bills from day-to-day spending.",
            link="/bills")
    if status.get("recurring_pending"):
        n = status["recurring_pending"]
        add("proposals", "info",
            f"{n} proposed recurring change{'s' if n > 1 else ''} awaiting "
            f"review on the Bills page.",
            link="/bills")
    mo = status.get("merge_offers")
    if mo:
        # worded from the batch, not the live queue, so working through the
        # offers does not resurrect a dismissed line (merchant_merge.notice)
        n = mo["batch"]
        add("merges", "info",
            f"{n} merchant pair{'s' if n > 1 else ''} look like one business "
            f"(found {_mmdd(mo['found'])}) — review on the Merchants page.",
            link="/merchants")
    for dr in status.get("recurring_drifts") or []:
        # amount drift is applied automatically, so this alert is the ONLY
        # notice the plan moved — link it to that bill's history, where the
        # amount can be set by hand if the new figure is wrong — which is
        # why it links there instead of only stating the change.
        add("drift", "info",
            f"{dr['payee']} plan updated ${dr.get('old', 0):,.2f} → "
            f"${dr.get('new', 0):,.2f} (tracks recent amounts).",
            link="/bills/history?payee=" + quote(str(dr["payee"])))
    if ev.get("large_mtd"):
        items = ", ".join(f"${a:,.0f} on {_mmdd(d)}" for d, a in ev["large_mtd"])
        add("transfer", "info",
            f"Major investment transfer this month: {items} — treated as a "
            f"one-time asset purchase, not counted in the shortfall metric.")
    if ev.get("bonus"):
        total = sum(a for _, a in ev["bonus"])
        add("bonus", "info",
            f"Bonus received: ${total:,.0f} — excluded from all budget "
            f"math by design (savings, not monthly income).")
    out.extend(status.get("stale_pulls") or [])
    # auto-removed connections pending reconnect (reaped_connections)
    out.extend(status.get("reaped_items") or [])
    if status.get("llm_failing"):
        out.append(status["llm_failing"])
    rank = {"bad": 0, "warn": 1, "good": 2, "info": 3}
    out.sort(key=lambda a: rank.get(a["severity"], 9))   # stable: worst first
    return out


def log(conn, alerts: list[dict], today: dt.date, *, partial: bool = False):
    """Upsert the current alerts (first_seen kept, last_seen advanced) and
    mark everything else inactive. Idempotent per day.

    partial=True: upsert-only — the caller is contributing
    SOME alerts, not asserting the whole active set. The default mode is a
    full-snapshot write, and the hourly sync's failover branch used it with
    just its own rows: every other active alert was deactivated AND
    un-dismissed, then the next Today load re-activated them with
    dismissed reset — the user's dismissals ping-ponged back forever.

    Two things keep concurrent writers from undoing each other, because a
    snapshot is a READ of the world followed by a WRITE of the whole active
    set, and the two can be minutes apart when the build behind it is slow:

    * a per-tenant lock for the duration, so snapshots apply whole — one
      cannot deactivate rows in the middle of another's upserts, and the
      "is this alert still current?" answer a writer acts on cannot be
      overtaken while it acts.
    * `last_seen` as a monotonic stamp. The lock orders the writes, but a
      SLOW writer can still take the lock LAST while carrying the OLDEST
      view of the world — it built its list before the alert cleared. Such
      a writer may not advance `last_seen`, reactivate, or reset a
      dismissal over a newer one's work, and may not deactivate a row a
      newer writer has since stamped. The user-visible failure is an alert
      they cleared coming back by itself.
    """
    current = {(a["kind"], a["message"]) for a in alerts}
    with conn.transaction():
        conn.execute(
            # keyed on the tenant, because advisory locks ignore RLS;
            # xact-scoped, so it is released by the commit below whatever
            # happens in between
            "SELECT pg_advisory_xact_lock(hashtext(COALESCE("
            "current_setting('app.tenant_id', true), '')), "
            "hashtext('alerts-log'))")
        for a in alerts:
            conn.execute(
                """INSERT INTO alerts_log (kind, severity, message, first_seen,
                                           last_seen, active)
                   VALUES (%s,%s,%s,%s,%s,1)
                   ON CONFLICT (tenant_id, kind, message) DO UPDATE SET
                       -- never walk the stamp backwards: a writer holding
                       -- an older view of the world is not news
                       last_seen=GREATEST(alerts_log.last_seen,
                                          EXCLUDED.last_seen),
                       -- a recurrence AFTER a clearance is news (dismissal
                       -- resets); an upsert of a still-active row keeps the
                       -- user's dismissal. Deciding this inside the upsert
                       -- makes the reset atomic with the reactivation — the
                       -- old shape (clear wrote dismissed=0, re-log wrote
                       -- active=1) let two concurrent full-snapshot writers
                       -- resurrect a dismissed alert between the two steps.
                       -- A STALE writer (older last_seen than the row
                       -- already carries) asserts nothing at all: it cannot
                       -- reactivate what a newer snapshot cleared, and it
                       -- cannot undo a dismissal.
                       active=CASE
                           WHEN EXCLUDED.last_seen < alerts_log.last_seen
                               THEN alerts_log.active ELSE 1 END,
                       dismissed=CASE
                           WHEN EXCLUDED.last_seen < alerts_log.last_seen
                               THEN alerts_log.dismissed
                           WHEN alerts_log.active=0 THEN 0
                           ELSE alerts_log.dismissed END""",
                (a["kind"], a["severity"], a["message"], today, today))
        if partial:
            return
        for row in conn.execute(
                "SELECT id, kind, message FROM alerts_log WHERE active=1"
                ).fetchall():
            if (row["kind"], row["message"]) not in current:
                # clearing keeps the dismissal bit — the "recurrence is
                # news" reset now happens on reactivation, inside the
                # upsert, where it is atomic. The last_seen guard is the
                # mirror of the one above: a stale snapshot does not know
                # about an alert raised after it built its list, so it must
                # not clear a row a newer writer has stamped.
                conn.execute("UPDATE alerts_log SET active=0 "
                             "WHERE id=%s AND last_seen<=%s",
                             (row["id"], today))


def history(conn, limit: int = 200) -> list:
    return conn.execute(
        """SELECT kind, severity, message, first_seen, last_seen, active, dismissed
           FROM alerts_log ORDER BY active DESC, last_seen DESC, id DESC
           LIMIT %s""", (limit,)).fetchall()


def visible(conn, alerts: list[dict]) -> list[dict]:
    """The strip's view: currently-built alerts minus dismissed ones."""
    hidden = {(r["kind"], r["message"]) for r in conn.execute(
        "SELECT kind, message FROM alerts_log WHERE dismissed=1").fetchall()}
    return [a for a in alerts if (a["kind"], a["message"]) not in hidden]


def dismiss(conn, kind: str, message: str) -> int:
    return conn.execute(
        "UPDATE alerts_log SET dismissed=1 WHERE kind=%s AND message=%s",
        (kind, message)).rowcount


def restore(conn, kind: str, message: str) -> int:
    return conn.execute(
        "UPDATE alerts_log SET dismissed=0 WHERE kind=%s AND message=%s",
        (kind, message)).rowcount
