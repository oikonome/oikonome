"""Reconnecting a bank ONTO the accounts a restore brought back.

A restored item is a shell: the export carries no access token (it never
should), so there is nothing for Plaid's update mode to repair and the
only way forward is a fresh link. But Plaid's ids are item-scoped, so a
fresh link mints a new account id for the same real account and a new
transaction id for every charge it backfills. Left alone the tenant ends
up with two of each account, and — across the ~24 months Plaid replays on
link — two of most of their recent transactions, because the upsert keys
on the id and these ids have never been seen before.

So the link ADOPTS instead of duplicating:

  * an incoming account is matched to a restored one by institution, mask
    and type, and the restored row's id is REWRITTEN to the incoming
    Plaid id. Afterwards the tenant looks exactly like a normal Plaid
    tenant — the sync path carries no permanent indirection, and the
    history, the bills pointing at it, the holdings and the entity
    assignment all still point at the same row;
  * the backfill that follows is filtered against what the restore already
    holds: anything well older than the account's last restored transaction
    is dropped outright, and anything in the overlapping days goes through
    the same one-to-one, name-aware matcher the file importers use. The
    overlap has a FAR edge as well as a near one — the restore holds
    nothing dated after that last transaction, so a charge further past it
    than the matcher's reach happened after the backup and is never
    examined at all;
  * a row dropped by that matcher hands its incoming Plaid id to the
    restored copy it duplicated — the same rename, one table down. Plaid's
    later `removed` and `modified` deltas name the id the new link minted,
    so without the hand-over a reversal or a correction arrives for an id
    this tenant has never stored and quietly does nothing. That hand-over
    only happens when the receiving row is PROVABLY the one the matcher
    consumed, because moving a live charge's identity onto the wrong row is
    worse than never moving it.

Both halves are deliberately conservative. Two candidate accounts with the
same mask means no adoption at all — merging somebody's chequing into
their savings is far worse than an extra account they can link by hand.
And the drop-what-we-have rule is a one-shot: it ends the moment the first
sync completes, because leaving it on would start silently discarding
Plaid's later CORRECTIONS to old rows.
"""

from __future__ import annotations

import datetime as dt
import logging

log = logging.getLogger(__name__)

# How long the guard stays open after an adoption. NOT "until the first
# sync finishes": Plaid keeps delivering for days after a link, and a charge
# that was PENDING when the backup was taken comes back later as a
# different transaction entirely. One sync is not the width of the
# overlap; the pending window is.
RECONCILE_DAYS = 14

# Below this, a backfilled row is certainly already held by the restore
# and is dropped without asking. Above it, the row is close enough to the
# seam that it gets matched properly instead.
DEEP_BACKFILL_DAYS = 30

# Every column that points at accounts.id, from information_schema rather
# than memory. A missed one would orphan the rows it holds — the bills
# still pointing at a renamed account are the difference between a
# recurring charge that keeps matching and one that silently stops.
ACCOUNT_REFS = (
    ("transactions", "account_id"),
    ("account_adoptions", "account_id"),
    ("liabilities", "account_id"),
    ("account_links", "account_id"),
    ("bills", "account_id"),
    ("holdings", "account_id"),
    ("crypto_holdings", "account_id"),
    ("import_batches", "account_id"),
    ("business_entity", "tax_reserve_account_id"),
)


def _candidates(conn, institution_id, institution_name, new_item_id):
    """Accounts on a TOKENLESS item of the same institution.

    Keyed on having no access token rather than on `status = 'restored'`,
    which would adopt almost nothing in practice: a restored item only
    wears that status until the next hourly sync tries it and stamps
    `error:NO_TOKEN`. Tokenlessness is the property
    that actually matters, and it is also true of an item the reaper
    disconnected after 30 days of failure, which is the same predicament
    from the other direction: history kept, credentials gone, reconnect
    means a fresh link.

    An item that still holds a token is syncing (or fixable in place), and
    stealing its accounts would break a working connection to fix nothing.
    """
    return conn.execute(
        """SELECT a.id, a.mask, a.type, a.subtype, a.name, a.item_id
             FROM accounts a JOIN items i ON i.id = a.item_id
            WHERE COALESCE(i.access_token, '') = ''
              AND i.id <> %s
              -- institution_id FIRST and on its own when we have one:
              -- names drift between what a restore carries and what Plaid
              -- returns for the same bank ('Acme' vs 'Acme Bank'), so a
              -- name comparison quietly half-works. The name is only a
              -- fallback for a restored item old enough to have no id
              -- recorded.
              AND (CASE WHEN %s::text IS NOT NULL
                             AND i.institution_id IS NOT NULL
                        THEN i.institution_id = %s::text
                        ELSE lower(i.institution_name)
                             = lower(%s::text) END)
              AND a.user_removed_at IS NULL""",
        (new_item_id, institution_id, institution_id,
         institution_name)).fetchall()


def _rewrite_account_id_in_config(conn, old_id: str, new_id: str) -> list[str]:
    """Carry the rename into the settings document.

    `ACCOUNT_REFS` covers every column that DECLARES a foreign key to
    accounts.id. The settings blob (`tenant_settings.config`, JSONB) names
    accounts too, by plain string, and no constraint connects the two — so
    the rename leaves those pointers naming an id that no longer exists,
    and every one of them fails OPEN and SILENTLY:

      * `excluded_accounts` is the Accounts page's 'excl' toggle, read as
        `t.account_id = ANY(...)` by the budget engine. A stale id excludes
        nothing, so an account the household deliberately kept out of the
        budget starts counting toward spend again — in Today, in the
        verdict, in every chart — with no notice that anything changed;
      * `checking_account_id` is the primary-checking pin the forecast and
        the cash views resolve first. A stale id falls back to "whichever
        depository account we find", which is a different account's balance
        presented as the same number;
      * `savings_goals[].account_id` pins a goal to the account it
        accumulates in (`t.account_id = %s`). A stale id matches zero rows
        for ever, so a goal that was tracking real progress reports nothing
        and never errors;
      * `link_dismissed` remembers "these two accounts are NOT the same
        one" as a `"idA|idB"` pair. A stale half stops suppressing the
        suggestion, so a pairing the household already rejected starts
        nagging again — and the reconnect is exactly when it would, since
        the adopted account is now on a different item from the one it was
        compared against.

    `budget_snapshots.config` holds a frozen copy of this document per
    closed month and is deliberately NOT rewritten: a snapshot is the
    budget the month was judged under, and rewriting history to name an
    account that did not exist yet would make it a different record. Ids
    in it are read only to reproduce that month's verdict, which is
    already fixed.

    Enumerated rather than a blind walk of the document replacing every
    string equal to `old_id`: account ids are opaque strings, and a walk
    would happily rewrite a bucket name, a dismissed-hint key or a merchant
    token that happened to collide. The keys that hold an account id are a
    short, knowable list — the same argument that makes ACCOUNT_REFS a list
    rather than a scan for TEXT columns.

    Returns the keys it changed, for the log line.
    """
    from ..engine import budget
    touched: list[str] = []
    with budget.config_txn(conn) as cfg:
        excl = cfg.get("excluded_accounts")
        if isinstance(excl, list) and old_id in excl:
            # de-dup on the way through: an id can only be excluded once,
            # and the new id may already be listed if the account was
            # linked, excluded and restored around the same reconnect
            seen, out = set(), []
            for a in excl:
                a = new_id if a == old_id else a
                if a not in seen:
                    seen.add(a)
                    out.append(a)
            cfg["excluded_accounts"] = out
            touched.append("excluded_accounts")
        if cfg.get("checking_account_id") == old_id:
            cfg["checking_account_id"] = new_id
            touched.append("checking_account_id")
        goals = cfg.get("savings_goals")
        if isinstance(goals, list):
            for g in goals:
                if isinstance(g, dict) and g.get("account_id") == old_id:
                    g["account_id"] = new_id
                    if "savings_goals" not in touched:
                        touched.append("savings_goals")
        dis = cfg.get("link_dismissed")
        if isinstance(dis, list):
            # each entry is the two account ids sorted and joined — rebuild
            # the key the same way the suggester does, or the rewritten one
            # would not be the key it looks up
            out = {"|".join(sorted(new_id if p == old_id else p
                                   for p in str(k).split("|")))
                   for k in dis}
            if out != set(dis):
                cfg["link_dismissed"] = sorted(out)
                touched.append("link_dismissed")
    return touched


def _rewrite_account_id(conn, old_id: str, new_id: str,
                        new_item_id: str) -> bool:
    """Move an account row, and everything pointing at it, to the new id.

    Copy-repoint-delete rather than a plain UPDATE of accounts.id: the
    children reference the account with a plain foreign key (ON DELETE
    CASCADE, no ON UPDATE), so renaming the parent while they still point
    at the old key is refused, and re-pointing them first is refused too
    because the new key does not exist yet. Making the new row first
    satisfies both ends, and dropping the old one at the finish leaves
    exactly one account where there was one before.

    All of it in ONE transaction, because the connection is autocommit and
    the statements below are a single move, not a dozen: a crash partway
    (deploy, OOM, dropped connection) between the insert and the delete
    leaves the tenant holding BOTH accounts with the children split across
    them, which double-counts or drops the account in every money aggregate
    with nothing raised. `_rewrite_txn_id` follows the same rule.

    The settings document points at accounts too, by plain string with no
    foreign key to keep the two honest, and it is rewritten by the CALLER
    at the end of the same transaction — `_rewrite_account_id_in_config`,
    see the comment at that call site for why it goes last rather than
    here. A caller that renames an account without it leaves the budget
    pointing at an id the tenant no longer has.

    False when the old row is gone by the time the lock is taken — there is
    then no account to adopt and nothing to record.
    """
    with conn.transaction():
        # LOCK the row being renamed before reading it — the same window the
        # transaction half of this rename closes, one table up. The insert
        # below is a SNAPSHOT copy and the delete at the end drops whatever
        # the row holds at that moment, so a web edit committing in between
        # — a display-name rename, an owner reassignment, a type/subtype
        # classification — is copied by neither and destroyed by the delete.
        # This runs on the synchronous link-exchange path while the
        # household is using the app, so "in between" is a real window, and
        # the loss is silent: the API already answered 200.
        #
        # FOR UPDATE makes that writer wait for the rename and then find no
        # such id. What it is TOLD then still varies by door — the owner
        # door re-checks its rowcount and answers 404, but /accounts/rename
        # and /accounts/classify answer 200 for an id that no longer exists.
        # Making those two honest is a web-layer fix; the lock is what stops
        # the edit landing on a doomed row in the first place.
        if not conn.execute(
                "SELECT 1 FROM accounts WHERE id = %s FOR UPDATE",
                (old_id,)).fetchone():
            return False
        conn.execute(
            """INSERT INTO accounts (id, item_id, name, display_name,
                   official_name, type, subtype, type_user_set, mask,
                   balance_current, balance_available, balance_limit,
                   currency, updated_at, raw, entity_id, user_removed_at,
                   owner)
               SELECT %s, %s, name, display_name, official_name, type,
                      subtype, type_user_set, mask, balance_current,
                      balance_available, balance_limit, currency, now(),
                      raw, entity_id, user_removed_at, owner
                 FROM accounts WHERE id = %s""",
            (new_id, new_item_id, old_id))
        for table, col in ACCOUNT_REFS:
            conn.execute(
                f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
                (new_id, old_id))
        conn.execute("DELETE FROM accounts WHERE id = %s", (old_id,))
    return True


# The columns to copy and every column pointing at transactions.id, read
# from the catalog rather than a hand-list, and cached for the process
# because the schema does not move under a running server. A missed child
# column would be CASCADE-DELETED along with the row it points at, taking
# a person's note, receipt or reimbursement pairing with it — the same
# hazard ACCOUNT_REFS exists to close, one table down.
_TXN_SHAPE: tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None = None


def _txn_shape(conn):
    global _TXN_SHAPE
    if _TXN_SHAPE is None:
        cols = tuple(
            r["column_name"] for r in conn.execute(
                """SELECT column_name FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'transactions'
                      AND is_generated = 'NEVER'
                      AND column_name <> 'id'
                    ORDER BY ordinal_position""").fetchall())
        refs = tuple(
            (r["tbl"], r["col"]) for r in conn.execute(
                """SELECT cl.relname AS tbl, src.attname AS col
                     FROM pg_constraint c
                     JOIN pg_class cl ON cl.oid = c.conrelid
                     CROSS JOIN LATERAL
                          unnest(c.conkey, c.confkey) AS k(s, f)
                     JOIN pg_attribute src ON src.attrelid = c.conrelid
                                          AND src.attnum = k.s
                     JOIN pg_attribute tgt ON tgt.attrelid = c.confrelid
                                          AND tgt.attnum = k.f
                    WHERE c.contype = 'f'
                      AND c.confrelid = 'transactions'::regclass
                      AND tgt.attname = 'id'""").fetchall())
        _TXN_SHAPE = (cols, refs)
    return _TXN_SHAPE


def _rewrite_txn_id(conn, old_id: str, new_id: str) -> bool:
    """Move a transaction row, and everything pointing at it, to the id
    the aggregator uses for the same charge.

    The account half of adoption renames the restored row into the
    incoming Plaid id so the sync path afterwards carries no indirection.
    Transactions need the same thing for the same reason: Plaid's later
    `removed` and `modified` deltas name the id the new link minted, and
    a restored row still wearing its old id is not there to be found —
    a reversed charge silently stays on the books.

    Copy-repoint-delete for the same reason `_rewrite_account_id` uses it:
    the children reference the row with a plain foreign key (no ON UPDATE),
    so neither end can move first.

    False when the new id is already held — then there is nothing to carry
    and renaming would collide with a row we have — and False when the old
    row is gone by the time the lock is taken.
    """
    if conn.execute("SELECT 1 FROM transactions WHERE id = %s",
                    (new_id,)).fetchone():
        return False
    cols, refs = _txn_shape(conn)
    collist = ", ".join(cols)
    with conn.transaction():
        # LOCK the row being renamed before reading it. The insert below is
        # a snapshot copy and the delete at the end drops whatever the row
        # holds at that moment, so a web edit committing in between (a
        # category override, an owner, a note) is copied by neither and
        # destroyed by the delete — silently, with a 200 already returned
        # for the write that vanished. This runs in the worker while the
        # owner is using the app, so "in between" is a real window.
        #
        # FOR UPDATE makes that writer wait for the rename and then see the
        # old id is gone. Every API door that mutates a transaction by id
        # re-checks existence and answers 404 for an id it cannot find, so
        # the person is told their edit did not land instead of being told
        # it did.
        if not conn.execute(
                "SELECT 1 FROM transactions WHERE id = %s FOR UPDATE",
                (old_id,)).fetchone():
            return False
        conn.execute(
            f"INSERT INTO transactions (id, {collist}) "
            f"SELECT %s, {collist} FROM transactions WHERE id = %s",
            (new_id, old_id))
        for table, col in refs:
            conn.execute(f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
                         (new_id, old_id))
        # transactions.pending_transaction_id is a plain TEXT self-reference,
        # NOT a declared foreign key, so the catalog scan above cannot see
        # it — and restore.py round-trips the column, so two restored rows
        # in one ledger can point at each other through it. Without this the
        # sibling's pointer would keep naming an id this rename just deleted.
        conn.execute(
            "UPDATE transactions SET pending_transaction_id = %s "
            "WHERE pending_transaction_id = %s", (new_id, old_id))
        # manual_categories.transaction_id is the same FK-less class: the
        # pin that records "a person decided this category" — left behind,
        # the renamed row would lose its protection against re-stamping
        conn.execute(
            "UPDATE manual_categories SET transaction_id = %s "
            "WHERE transaction_id = %s", (new_id, old_id))
        conn.execute("DELETE FROM transactions WHERE id = %s", (old_id,))
    return True


def adopt_accounts(conn, *, item_id: str, institution_id: str | None,
                   institution_name: str | None,
                   incoming: list[dict]) -> list[dict]:
    """Adopt restored accounts onto a freshly linked item.

    `incoming` is Plaid's account list — dicts with at least
    `account_id`, `mask`, `type`. Returns one record per adoption.
    Call BEFORE the accounts are upserted: the rewrite renames the
    existing row into the incoming id, so the ordinary upsert then lands
    on that same row instead of inserting a second one.
    """
    pool = _candidates(conn, institution_id, institution_name, item_id)
    if not pool:
        # Nothing to adopt still leaves the successor question open: a
        # shell with no accounts left on it holds no history, and the live
        # link is what replaced it.
        retire_replaced_shells(conn, live_item_id=item_id, incoming=incoming)
        return []
    taken: set[str] = set()
    donors: set[str] = set()
    done: list[dict] = []
    for acct in incoming:
        new_id = str(acct.get("account_id") or "")
        mask = (acct.get("mask") or "").strip()
        typ = (acct.get("type") or "").strip().lower()
        if not new_id or not mask:
            continue                      # no mask, no safe identity
        if conn.execute("SELECT 1 FROM accounts WHERE id = %s",
                        (new_id,)).fetchone():
            continue                      # already ours; nothing to adopt
        hits = [c for c in pool
                if c["id"] not in taken
                and (c["mask"] or "").strip() == mask
                and (c["type"] or "").strip().lower() == typ]
        if len(hits) != 1:
            # zero: nothing to adopt. more than one: two accounts at this
            # institution share a mask and a type, and picking either could
            # weld the wrong history onto a live feed — refuse and let the
            # ordinary path create a fresh account.
            if len(hits) > 1:
                log.info("reconnect: %s candidates share mask %s — not "
                         "adopting", len(hits), mask)
            continue
        old = hits[0]
        # The rename and the row that RECORDS it are one move, not two.
        # The connection is autocommit, so with the bookkeeping outside the
        # rename's transaction a crash in the gap (deploy restart, OOM,
        # dropped connection) would leave the account wearing the incoming
        # Plaid id with no `account_adoptions` row behind it — and that
        # state is unrecoverable, not merely untidy: `_candidates` only
        # looks at TOKENLESS items, so the renamed account would sit on the
        # live item and never be revisited, while `filter_incoming`
        # keys entirely off `pending()` and short-circuits when there is no
        # adoption row. The very next page of the same backfill would then
        # insert two years of Plaid history alongside the restored copy of
        # it, under ids that collide with nothing. Committing them together
        # means the retry sees either both or neither.
        touched: list[str] = []
        with conn.transaction():
            cutoff = conn.execute(
                "SELECT max(date) AS d FROM transactions "
                "WHERE account_id = %s AND removed = 0", (old["id"],)
            ).fetchone()["d"]
            moved = _rewrite_account_id(conn, old["id"], new_id, item_id)
            if moved:
                _retire_orphaned_pending(conn, new_id)
                conn.execute(
                    """INSERT INTO account_adoptions
                           (account_id, old_account_id, item_id, cutoff_date)
                       VALUES (%s,%s,%s,%s)""",
                    (new_id, old["id"], item_id, cutoff))
                # The FK-less half of the rename, in the same transaction
                # for the same reason as the rest: a settings document
                # still naming the deleted id is a half-done rename that
                # nothing ever revisits.
                #
                # LAST, and deliberately so. `config_txn` takes the
                # tenant_settings ROW lock, and a lock taken inside a
                # transaction is held until that transaction commits — so
                # taken any earlier it also spans the accounts DELETE, the
                # pending sweep and the bookkeeping INSERT above. That is
                # every settings writer in the household (the settings
                # page, the wizard, the budget seed) queued behind row work
                # that has nothing to do with the document, once per
                # adopted account, on the synchronous link-exchange path.
                # Going last keeps the atomicity the lock is here for and
                # narrows the wait to the read-modify-write it protects.
                #
                # Also never before the rename: the row may be gone by the
                # time we get here (`moved` False), and rewriting the
                # config for a rename that did not happen would point the
                # budget at an account this tenant does not have.
                touched = _rewrite_account_id_in_config(
                    conn, old["id"], new_id)
        if touched:
            log.info("reconnect: carried %s onto the new account id in %s",
                     old["id"], ", ".join(touched))
        if not moved:
            # The candidate was read on an autocommit connection, so it can
            # be removed between the scan and the lock. Nothing moved, so
            # record no adoption: an adoption row for an account that does
            # not exist would keep the one-shot backfill guard open on it.
            continue
        taken.add(old["id"])
        donors.add(old["item_id"])
        done.append({"account_id": new_id, "old_account_id": old["id"],
                     "name": old["name"], "mask": mask,
                     "cutoff_date": cutoff})
        log.info("reconnect: adopted %s onto %s (cutoff %s)",
                 old["id"], new_id, cutoff)
    _retire_donors(conn, donors)
    # and any other shell this link replaced: adopting nothing from a
    # connection does not mean the new link is not its successor — only
    # that the accounts arrived under ids we already held.
    retire_replaced_shells(conn, live_item_id=item_id, incoming=incoming)
    return done


def _opaque(tok: str) -> bool:
    """A bank-descriptor token rather than a merchant word: carries digits
    or is a long alphanumeric code ('a1b2c3d4e5f6g7h', 'xx1234')."""
    return any(ch.isdigit() for ch in tok) or len(tok) > 14


def dedupe_twins(conn, account_id: str, *, apply: bool = True) -> list[dict]:
    """Retire the second lineage of a transaction that reached one account
    twice under two ids — the seam a restore + reconnect leaves when the
    restored history and the fresh link's backfill both land on the same
    account without passing through the incoming guard (a link synced
    before the restore was adopted onto it).

    A twin is: same account, same date, same amount, different id, both
    live, and names that are NOT positively distinct (shared token, or an
    opaque descriptor on one side — the importers' own rule). Two real
    same-day charges with different merchants stay; two $4.50 coffees at
    the same shop on one day do NOT — the rule cannot tell a re-described
    charge from a repeated one. So this never runs on its own: a
    tenant-wide sweep proposes mostly real repeats (same-day bookings,
    recurring payroll and contribution lines land on one date at one
    amount all the time). It is an OPERATOR tool for one account whose
    seam is known, dry-run first, and the report is what says whether the
    pairs are twins.

    The survivor is the row a person would keep: posted over pending, the
    cleaner description over the raw descriptor, the one carrying user
    state (override / owner / entity) over the bare one, then a stable
    tie-break on id. The loser is flagged removed (never deleted), and any
    user state it alone carried moves to the survivor."""
    from .dedup import _tokens
    rows = conn.execute(
        """SELECT id, date, amount, name, merchant_name, pending,
                  category_override, owner_override, entity_id, merchant_id
             FROM transactions
            WHERE account_id = %s AND removed = 0
            ORDER BY date, amount, id""", (account_id,)).fetchall()
    cells: dict[tuple, list] = {}
    for r in rows:
        cells.setdefault((r["date"], round(float(r["amount"]) * 100)), []).append(dict(r))
    out: list[dict] = []
    from .dedup import _distinct_names as _dn  # local alias for the loop
    for group in cells.values():
        if len(group) < 2:
            continue
        live = list(group)
        # Find one non-distinct pair, retire the loser, then RESTART the
        # scan of what is left. Mutating the list mid-walk could compare a
        # survivor against itself — three rows [A, B, C] with A/C twins would
        # retire A and then match C against its own dict, deleting a genuine
        # transaction.
        changed = True
        while changed and len(live) > 1:
            changed = False
            for i in range(len(live)):
                for j in range(i + 1, len(live)):
                    a, b = live[i], live[j]
                    ta = _tokens(a["name"], a["merchant_name"])
                    tb = _tokens(b["name"], b["merchant_name"])
                    if _dn(ta, tb):
                        continue
                    keep, lose = _pick_survivor(a, ta, b, tb)
                    out.append({"account_id": account_id, "date": str(a["date"]),
                                "amount": a["amount"], "kept": keep["id"],
                                "kept_name": keep["name"], "removed": lose["id"],
                                "removed_name": lose["name"]})
                    if apply:
                        _retire_twin(conn, keep["id"], lose["id"])
                    live.remove(lose)
                    changed = True
                    break
                if changed:
                    break
    if out and apply:
        log.info("twins: retired %s duplicate lineage rows on %s", len(out), account_id)
    return out


def _retire_twin(conn, keep_id: str, lose_id: str) -> None:
    """Retire `lose_id` in favour of `keep_id`, losing nothing a person
    put on either row.

    User state (category / owner / entity) merges from the CURRENT row in
    the database, not from whatever was read when the scan started — an
    edit landing between the scan and the retirement must survive. And
    everything attached to the retired row by its id — notes, receipts,
    reimbursement and business flags — moves to the survivor, because
    every read path filters removed = 0 and would otherwise hide them."""
    conn.execute(
        """UPDATE transactions k SET
               category_override = COALESCE(k.category_override, l.category_override),
               owner_override    = COALESCE(k.owner_override,    l.owner_override),
               entity_id         = COALESCE(k.entity_id,         l.entity_id)
           FROM transactions l
          WHERE k.id = %s AND l.id = %s""", (keep_id, lose_id))
    conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s", (lose_id,))
    # notes: one row per transaction, so they cannot both keep theirs. If
    # the survivor has no note, the retired row's moves over. If BOTH have
    # a note, the retired one's text is appended to the survivor's rather
    # than silently stranded on a removed=1 row that every read hides — a
    # user's written words must survive a dedup. Either way the losing
    # row's note is then deleted, like the flags below.
    conn.execute(
        """UPDATE transaction_notes k
              SET note = k.note || E'\\n' || l.note, updated_at = now()
             FROM transaction_notes l
            WHERE k.txn_id = %s AND l.txn_id = %s""",
        (keep_id, lose_id))
    conn.execute(
        """UPDATE transaction_notes SET txn_id = %s
            WHERE txn_id = %s AND NOT EXISTS
                  (SELECT 1 FROM transaction_notes WHERE txn_id = %s)""",
        (keep_id, lose_id, keep_id))
    conn.execute("DELETE FROM transaction_notes WHERE txn_id = %s",
                 (lose_id,))
    conn.execute("UPDATE receipts SET txn_id = %s WHERE txn_id = %s",
                 (keep_id, lose_id))
    for table in ("reimburse_flags", "business_flags"):
        conn.execute(
            f"""UPDATE {table} SET txn_id = %s
                 WHERE txn_id = %s AND NOT EXISTS
                       (SELECT 1 FROM {table} WHERE txn_id = %s)""",
            (keep_id, lose_id, keep_id))
        conn.execute(f"DELETE FROM {table} WHERE txn_id = %s", (lose_id,))


def _pick_survivor(a: dict, ta: set, b: dict, tb: set) -> tuple[dict, dict]:
    def score(r, toks):
        opaque = sum(1 for t in toks if _opaque(t))
        state = int(bool(r["category_override"] or r["owner_override"] or r["entity_id"]))
        return (0 if r["pending"] else 1,          # posted beats pending
                -opaque,                           # cleaner name wins
                state,                             # user state wins
                1 if r["merchant_id"] else 0,      # resolved merchant wins
                r["id"] < (b["id"] if r is a else a["id"]))   # stable tie-break
    sa, sb = score(a, ta), score(b, tb)
    return (a, b) if sa >= sb else (b, a)


def _retire_donors(conn, item_ids: set) -> None:
    """Archive a donor item once a reconnect has adopted from it.

    Retiring only a donor left completely EMPTY sounds careful and is
    wrong in practice. A reconnect frequently leaves something behind — a
    closed account from years ago, a lineage the new link no longer
    returns — and that shell then sits in the app saying "sync failing"
    forever, because it has no token and never will again.

    A donor is by definition the connection the new link REPLACED: its
    credentials are gone, nothing on it can ever sync, and the accounts it
    still holds are history. Archiving says exactly that — the Accounts
    page renders it quietly in the closed-lineage section, the connections
    query hides it from the health list, and it stops holding a slot
    against the institution cap.

    Archiving does NOT put its accounts out of reach: adoption keys on the
    absence of a token, not on status, so re-linking that bank later can
    still claim them.
    """
    for iid in item_ids:
        conn.execute(
            """UPDATE items SET status = 'archived',
                   archived_reason = 'reconnected — accounts adopted by a '
                                     'new link',
                   archived_at = now()
               WHERE id = %s AND COALESCE(status,'') != 'archived'""",
            (iid,))
        log.info("reconnect: retired donor item %s", iid)


def _retire_orphaned_pending(conn, account_id: str) -> int:
    """Drop the account's PENDING rows as the reconnect lands.

    A pending transaction is a promise that its own item will post it
    later. After a restore that item is gone, so the promise can never be
    kept: the row will sit there for ever, and when the bank posts the
    real charge the fresh link delivers it under a NEW id that collides
    with nothing. That is the duplicate — a recurring payee appearing
    twice, once for each lineage.

    Retiring them here is honest: they are unresolvable, and the incoming
    backfill is about to supply the posted version of each.
    """
    cur = conn.execute(
        "UPDATE transactions SET removed = 1 "
        "WHERE account_id = %s AND pending = 1 AND removed = 0",
        (account_id,))
    if cur.rowcount:
        log.info("reconnect: retired %s unresolvable pending rows on %s",
                 cur.rowcount, account_id)
    return cur.rowcount


def pending(conn, account_id: str) -> dict | None:
    """The live adoption for an account, if its backfill is still being
    reconciled."""
    return conn.execute(
        """SELECT account_id, cutoff_date FROM account_adoptions
            WHERE account_id = %s AND reconciled_at IS NULL
              AND adopted_at > now() - make_interval(days => %s)
            ORDER BY adopted_at DESC LIMIT 1""",
        (account_id, RECONCILE_DAYS)).fetchone()


class _Absorbed:
    """WHICH restored row absorbed an incoming one — when that is PROVABLE.

    The seam matcher answers whether an incoming charge is one the restore
    already holds; it does not say which row it matched, and the row is
    what the incoming Plaid id has to be carried onto. This re-identifies
    it, and the whole design question is what makes the answer safe to act
    on, because acting on a wrong one is not a missed repair — it moves a
    LIVE charge's identity onto a different charge, and every later
    `removed`/`modified` delta for both then lands on the wrong row or on
    nothing at all.

    Three conditions, all of which must hold:

      * the candidate is provably part of what the RESTORE brought back.
        The cutoff is `max(date)` over the account's live rows at the
        moment of adoption, so no restored row is dated after it — which
        makes `date <= cutoff` a proof, not a heuristic, that a candidate
        predates the reconnect. Without it the candidate set is every live
        row in the window, so a charge that arrives days AFTER the
        reconnect (still inside the 14-day guard) could be picked as "the
        row the matcher consumed" and have its Plaid id overwritten;
      * no other candidate could have been the match. Two rows a person
        could not tell apart are exactly where re-identification is a
        guess;
      * the one candidate shares a name TOKEN with the incoming row. The
        sibling `Deduper` will also claim on same-day amount alone when the
        names merely fail to contradict each other (an opaque bank
        descriptor is no evidence either way) — that is the right rule for
        DROPPING a duplicate, and the wrong one for deciding whose identity
        to move, because it rests on the absence of evidence rather than
        the presence of any.

    Anything short of all three carries nothing and the guard only drops the
    duplicate: a missed correction beats a corrupted row.
    """

    def __init__(self, conn, account_id: str, lo: dt.date, hi: dt.date,
                 cutoff: dt.date, *, skip_ids: set | None = None):
        from .dedup import MAX_CANDIDATES, _tokens
        self._cells: dict[tuple[int, dt.date], list[dict]] = {}
        # limit + 1 so the cap is DETECTED rather than silently applied —
        # the sibling `dedup.Deduper` fetches the same way. It raises;
        # this one cannot, because it runs inside a sync rather than a
        # re-runnable file import, and the carry is best-effort by design.
        # So it degrades explicitly: carry NOTHING and say so, instead of
        # matching against an arbitrary subset (LIMIT with no ORDER BY
        # returns whichever rows Postgres reached first) and renaming a
        # row on evidence that was never complete.
        rows = conn.execute(
            """SELECT id, date, amount, name, merchant_name
                 FROM transactions
                WHERE account_id = %s AND removed = 0
                  AND date <= %s
                  AND date BETWEEN %s - INTERVAL '3 days'
                                AND %s + INTERVAL '3 days'
                LIMIT %s""",
            (account_id, cutoff, lo, hi, MAX_CANDIDATES + 1)).fetchall()
        if len(rows) > MAX_CANDIDATES:
            log.warning("reconnect: account %s holds more than %s rows in "
                        "the adoption window — carrying no Plaid ids for "
                        "it (a missed correction beats a wrong rename)",
                        account_id, MAX_CANDIDATES)
            return
        for r in rows:
            # A row wearing an id this very backfill is delivering is a row
            # an earlier page already carried an id onto. Renaming it again
            # would strip the id Plaid has ALREADY started addressing it by.
            if skip_ids and r["id"] in skip_ids:
                continue
            cell = (round(float(r["amount"]) * 100), r["date"])
            self._cells.setdefault(cell, []).append(
                {"id": r["id"], "used": False,
                 "tokens": _tokens(r["name"], r["merchant_name"])})

    def take(self, date: dt.date, amount: float, tokens: set) -> str | None:
        from .dedup import WINDOW_DAYS, _distinct_names
        cents = round(amount * 100)
        hits: list[dict] = []
        for delta in range(WINDOW_DAYS + 1):
            days = ((date,) if delta == 0
                    else (date - dt.timedelta(days=delta),
                          date + dt.timedelta(days=delta)))
            for d in days:
                for c in self._cells.get((cents, d), ()):
                    if c["used"] or _distinct_names(tokens, c["tokens"]):
                        continue
                    hits.append(c)
                    if len(hits) > 1:
                        return None            # never rename a guess
        if len(hits) != 1 or not (tokens & hits[0]["tokens"]):
            return None
        hits[0]["used"] = True
        return hits[0]["id"]


def filter_incoming(conn, account_id: str, txns: list) -> tuple[list, dict]:
    """Drop the part of a backfill the restore already holds.

    `txns` are objects (or dicts) with .date/.amount/.name for one
    account. Returns (kept, stats). A no-op — same list, zero stats —
    when the account has no adoption awaiting reconciliation, so this is
    safe to call on every sync.

    A row the guard drops has its incoming id carried onto the restored
    copy it duplicated, so the two identities meet permanently: Plaid's
    later `removed` and `modified` deltas name that id, and without the
    carry they land on nothing at all.
    """
    adopt = pending(conn, account_id)
    if not adopt or not adopt["cutoff_date"]:
        return txns, {}
    cutoff = adopt["cutoff_date"]
    if isinstance(cutoff, dt.datetime):
        cutoff = cutoff.date()

    def _f(t, name):
        return t.get(name) if isinstance(t, dict) else getattr(t, name, None)

    def _day(t):
        d = _f(t, "date")
        if isinstance(d, dt.datetime):
            return d.date()
        if isinstance(d, str):
            return dt.date.fromisoformat(d[:10])
        return d

    # An id we ALREADY hold is a correction to that row, never a second
    # copy of it: `modified` re-delivers a transaction under the id it was
    # first given. Dropping one — on the date rule or through the matcher,
    # which happily matches a row against itself — would discard the
    # correction silently, so the guard never looks at them.
    ids = [i for i in (_f(t, "id") for t in txns) if i]
    held = {r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE id = ANY(%s)", (ids,)).fetchall()
        } if ids else set()
    passthrough = [t for t in txns if _f(t, "id") in held]
    txns = [t for t in txns if _f(t, "id") not in held]

    # Only the DEEP backfill is dropped on the date alone. Rows near the
    # seam are matched instead: blind-dropping everything below the cutoff
    # would let a row that Plaid re-delivered just under the line pass
    # unexamined on a later sync.
    #
    # And the seam has a FAR edge too. The
    # cutoff is the newest date the restore holds on this account, and the
    # matcher's reach is WINDOW_DAYS, so an incoming row dated further past
    # the cutoff than that cannot possibly be a duplicate of anything the
    # restore brought back — it is a charge that happened after the backup
    # was taken. The guard stays open for RECONCILE_DAYS, so without the
    # far edge every ordinary charge of the next fortnight would be
    # run through a duplicate matcher against the tenant's own live
    # history: two same-price coffees a few days apart, and the second is
    # claimed as a copy of the first.
    from .dedup import Deduper, WINDOW_DAYS, _tokens
    deep_before = cutoff - dt.timedelta(days=DEEP_BACKFILL_DAYS)
    seam_ends = cutoff + dt.timedelta(days=WINDOW_DAYS)
    older, overlap, buckets = [], [], []
    for t in txns:
        d = _day(t)
        if d is not None and d < deep_before:
            older.append(t)
            buckets.append((t, None, "older"))
        elif d is not None and d > seam_ends:
            buckets.append((t, d, "after"))     # past the seam: untouched
        else:
            overlap.append(t)
            buckets.append((t, d, "overlap"))

    # The overlapping days are the only place a genuine new charge and a
    # restored one can be confused, so they get the real matcher rather
    # than a date rule: same amount, within three days, and a shared name
    # token, each existing row absorbing at most one incoming.
    dates = [d for t, d, b in buckets if b == "overlap" and d is not None]
    kept, deduped, carried = [], 0, 0
    absorbed = None                 # built on the first claim, not before
    m = None
    if overlap:
        # The seam is a bounded window (DEEP_BACKFILL_DAYS before the
        # cutoff, WINDOW_DAYS after it), but a busy account still holds
        # thousands of rows in
        # it — well past the file-importer default, which exists to stop a
        # hand-picked CSV scanning a decade.
        # max_date=cutoff: the matcher exists to recognize re-deliveries
        # of what the RESTORE holds, and every restored row is dated at or
        # before the cutoff by construction. Without the bound, rows this
        # same backfill inserted moments ago (dated past the cutoff)
        # would become candidates, and an earlier page's genuine charge could
        # absorb a later page's distinct same-amount charge as a
        # "duplicate" — a silent drop inside the sync's own stream.
        m = Deduper(conn, account_id, "__no_source_prefix__", dates,
                    max_candidates=50_000, max_date=cutoff)
    for t, d, bucket in buckets:
        if bucket == "older":
            continue
        amt = _f(t, "amount")
        if bucket == "after" or d is None or amt is None:
            kept.append(t)
            continue
        if m.claim(d, float(amt), _f(t, "name"), _f(t, "merchant_name")):
            deduped += 1
            new_id = _f(t, "id")
            if new_id and dates:
                if absorbed is None:
                    absorbed = _Absorbed(conn, account_id, min(dates),
                                         max(dates), cutoff,
                                         skip_ids=set(ids))
                old_id = absorbed.take(
                    d, float(amt),
                    _tokens(_f(t, "name"), _f(t, "merchant_name")))
                # Carrying the id is a repair, not the job: if it
                # fails the tenant still gets one copy of the charge,
                # which is what the guard is for.
                try:
                    if old_id and _rewrite_txn_id(conn, old_id, new_id):
                        carried += 1
                except Exception:                    # noqa: BLE001
                    log.warning("reconnect: could not carry %s onto %s",
                                new_id, old_id, exc_info=True)
        else:
            kept.append(t)
    stats = {"dropped_before_cutoff": len(older), "deduped_overlap": deduped,
             "carried_plaid_ids": carried, "cutoff": str(cutoff)}
    if older or deduped:
        log.info("reconnect: account %s — dropped %s pre-cutoff, %s "
                 "overlapping duplicates, carried %s ids",
                 account_id, len(older), deduped, carried)
    return passthrough + kept, stats


def mark_reconciled(conn, item_id: str) -> int:
    """End the one-shot guard for every account this item adopted.

    Called once the first sync for the item has landed. Leaving it on
    would start discarding Plaid's later corrections to old rows, which is
    the same silent data loss this exists to prevent, pointed the other
    way.
    """
    cur = conn.execute(
        "UPDATE account_adoptions SET reconciled_at = now() "
        "WHERE item_id = %s AND reconciled_at IS NULL", (item_id,))
    return cur.rowcount


def mark_reconciled_if_settled(conn, item_id: str, *,
                               min_age_hours: int = 48) -> int:
    """Close the guard early once the seam has demonstrably settled.

    Called from a successful sync whose backfill is complete and whose
    seam matcher claimed nothing. The age floor must COVER the re-delivery
    window it protects against: the aggregator keeps re-delivering for a
    day or two after HISTORICAL_UPDATE_COMPLETE, which is exactly the
    window the guard exists for — hence 48h, not 24. Without it every
    adoption would run the duplicate matcher against live data for the
    full 14-day clock."""
    cur = conn.execute(
        "UPDATE account_adoptions SET reconciled_at = now() "
        "WHERE item_id = %s AND reconciled_at IS NULL "
        "AND adopted_at < now() - make_interval(hours => %s)",
        (item_id, min_age_hours))
    if cur.rowcount:
        log.info("reconnect: guard closed on settled seam for item %s "
                 "(%d adoption(s))", item_id, cur.rowcount)
    return cur.rowcount


def retire_replaced_shells(conn, *, live_item_id: str,
                           incoming: list[dict]) -> int:
    """Archive the tokenless connections THIS link actually replaced.

    Adoption retires the shell it took accounts FROM, which covers the
    ordinary case. It does not cover a shell that was left behind for some
    other reason — no account matched, the link returned a different set,
    an older adoption. Those sit in the app saying
    "sync failing" on credentials that are gone, and hold a slot against
    the institution cap while they do it.

    A shell counts as replaced only when the new link SERVES everything it
    still holds: every account left on it comes back in the incoming list
    under the same mask and type (a shell with nothing left qualifies
    trivially — there is no account it could still be the home of). The
    weaker rule of "any tokenless item at this institution, once any live
    one exists" archives the wrong connection for a household with two
    logins at one bank: reconnecting the first would retire the second,
    whose accounts the new link never returned, hiding the very prompt
    telling them to re-link it. An account the new link does not return is
    a connection genuinely waiting for its own re-link.

    Institution is matched on institution_id rather than name: a restore
    carries whatever the bank was called when it was linked, and Plaid may
    return something else now ('Acme' / 'Acme Bank'), which is exactly how
    a shell survives a name-based sweep.
    """
    live = conn.execute(
        "SELECT institution_id, COALESCE(access_token, '') AS token "
        "FROM items WHERE id = %s", (live_item_id,)).fetchone()
    if not live or not live["institution_id"] or not live["token"]:
        return 0                    # nothing live here replaced anything
    # An account with no mask has no identity to match on, so it can never
    # be shown to be served — the shell holding it is left alone.
    served = {((a.get("mask") or "").strip(),
               (a.get("type") or "").strip().lower())
              for a in incoming if (a.get("mask") or "").strip()}
    shells = conn.execute(
        """SELECT id FROM items
            WHERE aggregator = 'plaid'
              AND COALESCE(access_token, '') = ''
              AND COALESCE(status, '') != 'archived'
              AND institution_id = %s
              AND id <> %s""",
        (live["institution_id"], live_item_id)).fetchall()
    n = 0
    for shell in shells:
        left = conn.execute(
            "SELECT mask, type FROM accounts "
            "WHERE item_id = %s AND user_removed_at IS NULL",
            (shell["id"],)).fetchall()
        if any(((r["mask"] or "").strip(),
                (r["type"] or "").strip().lower()) not in served
               for r in left):
            continue                # a separate login, still waiting
        conn.execute(
            """UPDATE items SET status = 'archived',
                   archived_reason = 'reconnected — a new link now serves '
                                     'this institution',
                   archived_at = now()
               WHERE id = %s""", (shell["id"],))
        n += 1
        log.info("reconnect: retired replaced shell %s", shell["id"])
    return n


def list_adoptions(conn, item_id: str | None = None) -> list[dict]:
    sql = ("SELECT account_id, old_account_id, item_id, cutoff_date, "
           "adopted_at, reconciled_at FROM account_adoptions")
    args: tuple = ()
    if item_id:
        sql += " WHERE item_id = %s"
        args = (item_id,)
    return [dict(r) for r in
            conn.execute(sql + " ORDER BY adopted_at DESC", args).fetchall()]
