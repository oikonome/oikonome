"""Management/data layer behind the web routes — thin wrappers over the same
engine modules the email uses; no duplicate business logic.

Covers the parts that are product features: transactions browse/search,
recategorize, reimbursements, account detail, config knobs. Single-machine
plumbing (systemd job triggers, watched file mtimes, a connection-source
table) is deliberately absent — hosted jobs go through arq, see jobs/.
"""

from __future__ import annotations

import datetime as dt

from ..engine import budget, merchant_dedup, reporting
from ..engine.compat import as_date

_ACCT_FULL = budget.ACCT_LABEL_SQL   # single definition, shared with _spend_rows

# The one shadow predicate (reporting._NOT_SHADOW, the same fragment books.py
# and selfemploy.py import) rather than a fifth hand-rolled copy: a real-world
# account reached through two aggregators is two account rows carrying one
# history, and a ledger that lists both shows every transaction twice and
# reports double the total. Reads the app.shadow_ids session var, set per
# tenant_connect and refreshed by links.set_shadow_scope after a link change.
_NOT_SHADOW = reporting._NOT_SHADOW.format(col="t.account_id")

# Merchant naming is ONE fact: the canonical map keys on what a row DISPLAYS
# (outlet first — see merchant_dedup.MC_JOIN's rationale), and every surface
# here resolves through the same imported fragments. Keyed differently, a
# user rename showed the new name on the Merchants page while the ledger and
# the bulk-category write still used the old one.
_MC_JOIN = merchant_dedup.MC_JOIN
_DISPLAY_MERCHANT = merchant_dedup.DISPLAY_MERCHANT


def _as_array(expr: str) -> str:
    """SQL for reading a JSONB value that is SUPPOSED to be an array.

    `jsonb_array_elements` does not skip a row whose value is an object or
    a number — it raises, and the raise aborts the whole statement. A
    ledger search runs over every row in the household, so one order whose
    items landed as `{"note":1}` would take out the search for all of them,
    identically on every retry, with nothing the reader could do about it.
    Guarded this way a row with the wrong shape simply contributes no items
    and stays findable by everything else it says about itself.
    """
    return (f"CASE WHEN jsonb_typeof({expr}) = 'array' "
            f"THEN {expr} ELSE '[]'::jsonb END")


# The business a row belongs to — its own entity_id (an equity movement
# assigned from a personal account) or its ACCOUNT's (a business-entity
# account). Rendered as a named pill on the ledger so a business account's
# rows read as the business's money, distinct from the Schedule C `biz`
# flag on a personal card. NULL for household rows, which is the common
# case and costs nothing. `t`/`a` are the transactions/accounts aliases.
_ENTITY_NAME = """(SELECT e.name FROM business_entity e
                   WHERE e.id = COALESCE(t.entity_id, a.entity_id))"""

# The scope of a personal-ledger listing: 'all' lists household AND
# business-entity rows (each business row carries its `entity`); 'personal'
# is the household-only view every aggregate uses (budget.PERSONAL_ONLY_SQL,
# so the combined toggle is honoured); 'business' is its complement. The
# ledger used to be personal-only with no way to widen it, which made a
# business account's own transactions unreachable from the Accounts page.
def _scope_sql(scope: str) -> str:
    if scope == "personal":
        return budget.PERSONAL_ONLY_SQL
    if scope == "business":
        # the plain complement of the personal predicate — spelled out
        # rather than negated, because the combined toggle turns the
        # personal one into a no-op and its negation would list nothing
        return """
  AND (t.entity_id IS NOT NULL
       OR (NULLIF(current_setting('app.entity_account_ids', true), '') IS NOT NULL
           AND t.account_id = ANY(string_to_array(
                   current_setting('app.entity_account_ids', true), ','))))"""
    return ""


# the Transactions-page row shape — ONE field list so every surface that
# renders ledger rows (the page, the Today recent pane) carries identical
# pills/flags and cannot drift. Every query that formats this MUST also
# carry _MC_JOIN: payee resolves through the canonical map so a user
# rename is visible on the ledger, not only on the Merchants page.
_TXN_FIELDS = f"""t.id, t.date, t.amount, {_DISPLAY_MERCHANT} AS payee,
                  {merchant_dedup.MERCHANT_LOGO} AS merchant_logo,
                  {merchant_dedup.MERCHANT_ID} AS merchant_id,
                  -- whether the verdict counts this row as spending. The
                  -- client renders day subtotals from this flag, so it is
                  -- budget.SPEND_ONLY_SQL verbatim (imported, not restated)
                  -- and can never disagree with the verdict's own math.
                  -- (and business-entity money is never HOUSEHOLD spend
                  -- — the verdict carries PERSONAL_ONLY_SQL, so a day
                  -- subtotal must too, or listing a business account's
                  -- rows would inflate the day they sit in)
                  (TRUE{budget.SPEND_ONLY_SQL}{budget.PERSONAL_ONLY_SQL}) AS counts_as_spend,
                  -- DISPLAY form: the override is prettified like the primary
                  -- (a picked standard category is stored as its raw key), so
                  -- no consumer — page, email, mobile — sees FOOD_AND_DRINK
                  REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ') AS category,
                  {_ENTITY_NAME} AS entity,
                  EXISTS(SELECT 1 FROM reimbursements r
                         WHERE r.expense_id=t.id OR r.reimburse_id=t.id) AS reimb,
                  EXISTS(SELECT 1 FROM reimburse_flags f
                         WHERE f.txn_id=t.id) AS reimb_flag,
                  EXISTS(SELECT 1 FROM business_flags b
                         WHERE b.txn_id=t.id) AS biz_flag,
                  EXISTS(SELECT 1 FROM receipts rc
                         WHERE rc.txn_id=t.id) AS has_receipt,
                  (SELECT note FROM transaction_notes tn
                   WHERE tn.txn_id=t.id) AS note,
                  COALESCE(t.owner_override, a.owner) AS owner,
                  -- What was actually bought, for the one merchant whose name
                  -- never says: every Amazon charge posts as "AMAZON.COM", so
                  -- the order's summary rides here. NULL for everything else,
                  -- which keeps this a display nicety rather than a schema
                  -- assumption: no order data, no change.
                  COALESCE(
                    (SELECT s.summary FROM amazon_matches am
                      JOIN amazon_summaries s ON s.dedup_key = am.dedup_key
                      WHERE am.transaction_id = t.id),
                    -- a Costco receipt's breakdown by what was bought
                    -- ("groceries $180 · household $60"), the same slot
                    (SELECT NULLIF(cr.summary, '') FROM costco_matches cm
                      JOIN costco_receipts cr ON cr.dedup_key = cm.dedup_key
                      WHERE cm.transaction_id = t.id)) AS item_summary,
                  t.pending, {{acct}} AS account, a.mask,
                  -- what the aggregator knows (migration 098) — the row's
                  -- chips and its detail panel; NULL where a source has none
                  t.check_number, t.payment_channel, t.payment_processor,
                  t.location_city, t.location_region, t.location_address,
                  t.location_lat, t.location_lon, t.location_store,
                  t.mcc, t.authorized_date,
                  t.raw->>'original_description' AS bank_text,
                  -- the category's provenance: who set the primary, and
                  -- whether an override (a person / a bill / Amazon) sits
                  -- on top — the detail panel says WHY in words from these
                  t.category_source, t.category_override,
                  t.category_primary AS category_key,
                  t.category_detailed, t.category_plaid,
                  t.category_plaid_detailed, t.category_plaid_confidence,
                  mcat.bill_id AS override_bill_id,
                  (SELECT b.payee FROM bills b WHERE b.id = mcat.bill_id) AS override_bill,
                  -- a companion charge (the fee that rides with a bill's
                  -- payment) is stamped by the bill too; the ledger says so
                  EXISTS (SELECT 1 FROM bills b,
                                 jsonb_array_elements(""" + _as_array("b.raw->'companions'") + """) c
                           WHERE b.id = mcat.bill_id
                             AND abs(abs(t.amount) - (c->>'amount')::numeric)
                                 <= GREATEST(0.5, (c->>'amount')::numeric * 0.25))
                    AS override_is_fee,
                  -- a person's answer AND a category to show for it: a
                  -- CLEARED row carries their kind over a NULL override
                  -- (they answered "no category"), and "you set it" would
                  -- be a lie about a category nobody set
                  (t.category_override IS NOT NULL
                   AND (t.override_source = 'user'
                        OR (mcat.transaction_id IS NOT NULL
                            AND mcat.bill_id IS NULL))) AS override_manual"""
# the manual_categories join every ledger query needs for provenance
_TXN_JOINS = "LEFT JOIN manual_categories mcat ON mcat.transaction_id = t.id"


def transactions_by_ids(conn, ids: list[str]):
    """Ledger rows (the Transactions-page shape) for specific ids — the
    Today recent pane renders the same table component as the page."""
    if not ids:
        return []
    q = f"""SELECT {_TXN_FIELDS.format(acct=_ACCT_FULL)}
           FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
                LEFT JOIN items i ON i.id = a.item_id
                {_MC_JOIN} {_TXN_JOINS}
           WHERE t.removed = 0 AND t.id = ANY(%s)
           ORDER BY t.date DESC, ABS(t.amount) DESC"""
    return conn.execute(q, [ids]).fetchall()


# reimbursement-related rows: a linked pair (either side) or an
# awaiting-reimbursement flag — the Transactions page's quick filter
_REIMB_ONLY = """ AND (EXISTS(SELECT 1 FROM reimbursements pr
                        WHERE pr.expense_id=t.id OR pr.reimburse_id=t.id)
                  OR EXISTS(SELECT 1 FROM reimburse_flags pf
                            WHERE pf.txn_id=t.id))"""


def _like(term: str) -> str:
    """`%term%` with the user's own `%`, `_` and `\\` neutralised — a search
    for "100%" or "A_B" must match those characters, not act as wildcards."""
    t = (term or "").lower().replace("\\", "\\\\")
    t = t.replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"


def transactions(conn, year: int, month: int, *, search: str = "",
                 account_id: str | None = None, pending_only: bool = False,
                 reimb_only: bool = False, owner: str | None = None,
                 scope: str = "all"):
    start = dt.date(year, month, 1)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    # Business-entity rows are LISTED (labelled by `entity`) unless the
    # caller narrows the scope; they never count as household spend
    # (counts_as_spend), so the day and month figures stay personal. Same
    # rule in both modes of this page — the two drifted once.
    q = f"""SELECT {_TXN_FIELDS.format(acct=_ACCT_FULL)}
           FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
                LEFT JOIN items i ON i.id = a.item_id
                {_MC_JOIN} {_TXN_JOINS}
           WHERE t.removed = 0{_scope_sql(scope)}
             AND t.date >= %s AND t.date < %s"""
    args: list = [start, end]
    if account_id:
        q += " AND t.account_id = %s"
        args.append(account_id)
    else:
        # hidden accounts count toward nothing, and the ledger is part of
        # nothing — but asking for one by id still shows its history
        q += (" AND t.account_id NOT IN (SELECT id FROM accounts"
              " WHERE user_removed_at IS NOT NULL)")
        # …and neither does a linked non-primary: its rows are the SAME
        # real transactions the primary already carries. The session var
        # covers hidden accounts too, but only tenant_connect sets it, so
        # the unconditional subquery above stays as the floor.
        q += _NOT_SHADOW
    if owner:
        # filter by the EFFECTIVE owner (per-txn override, else the
        # account's owner). A display filter only — never touches the budget.
        q += " AND COALESCE(t.owner_override, a.owner) = %s"
        args.append(owner)
    if pending_only:
        q += " AND t.pending = 1"
    if reimb_only:
        # reimbursement quick filter: linked pairs
        # (either side) + awaiting flags
        q += _REIMB_ONLY
    if search:
        # categories are DISPLAYED underscore-replaced — match both forms;
        # receipt line items match too ("shampoo" finds the run); the
        # canonical is what a RENAMED row displays as its payee, so the new
        # name has to find it
        q += """ AND (t.search_text LIKE %s
                 OR COALESCE(t.merchant_outlet, t.merchant_name, t.name) IN (
                        SELECT mcs.raw_merchant FROM merchant_canonical mcs
                         WHERE LOWER(mcs.canonical) LIKE %s)
                 OR t.merchant_id IN (SELECT mmm.id FROM merchants mmm
                                      WHERE LOWER(mmm.name) LIKE %s)
                 OR t.id IN (SELECT rc2.txn_id FROM receipts rc2
                             JOIN receipt_items ri ON ri.receipt_id = rc2.id
                            WHERE LOWER(ri.description) LIKE %s))"""
        args += [_like(search)] * 4
    q += " ORDER BY t.date DESC, ABS(t.amount) DESC"
    return conn.execute(q, args).fetchall()


# household ownership attribution — an account's default owner, a
# per-transaction override, and the distinct owner labels for the filter axis.
# Pure attribution/display; the verdict + spend-exclusion math never read it.

def set_account_owner(conn, account_id: str, owner: str | None) -> int:
    """Set (or clear, owner=None/'') an account's default owner label."""
    cur = conn.execute("UPDATE accounts SET owner=%s WHERE id=%s",
                       (owner or None, account_id))
    return cur.rowcount


def set_txn_owner(conn, txn_id: str, owner: str | None) -> int:
    """Per-transaction owner override (owner=None/'' clears → inherit account)."""
    cur = conn.execute(
        "UPDATE transactions SET owner_override=%s WHERE id=%s AND removed=0",
        (owner or None, txn_id))
    return cur.rowcount


def list_owners(conn) -> list[str]:
    """Distinct owner labels in use — from account defaults and per-txn
    overrides — for the filter dropdown."""
    rows = conn.execute("""
        SELECT DISTINCT owner FROM (
            SELECT owner FROM accounts WHERE owner IS NOT NULL AND owner <> ''
            UNION
            SELECT owner_override FROM transactions
            WHERE owner_override IS NOT NULL AND owner_override <> ''
        ) o ORDER BY owner
    """).fetchall()
    return [r["owner"] for r in rows]


UNCATEGORIZED = "?"          # the category filter's name for "has none"


def search_transactions(conn, search: str, *, account_id: str | None = None,
                        date_from: str | None = None, date_to: str | None = None,
                        category: str | None = None, reimb_only: bool = False,
                        page: int = 1, per_page: int = 100,
                        scope: str = "all", channel: str | None = None,
                        checks_only: bool = False,
                        ids: list[str] | None = None):
    """Universal search: EVERY account, the FULL history, newest first,
    PAGINATED. Returns (rows, total_count, amount_sum, amazon_items) —
    amount_sum is the Plaid-signed sum over the WHOLE result set;
    amazon_items is {count, sum} of matching Amazon order items (per-item
    prices, not transaction totals), or None when the search matched no
    Amazon item; spend is {count, sum} over the whole result set counting
    only what the verdict counts as spending, so the caller can state a
    per-transaction average. A sixth element lists the accounts the term
    matched by name (either the user's rename or the bank's own name) —
    those accounts' rows are part of the result.

    `ids` restricts the result to exactly those transaction ids, in place
    of the account visibility rules (see below). It is how a caller that
    already KNOWS its row set — the budget engine, handing over the rows
    behind one of its bucket tiles — gets that set back in the shape the
    search surface renders."""
    search = (search or "").strip()
    account_hits: list[dict] = []
    # Business-entity rows are listed with their `entity` name unless the
    # scope narrows them out; the spend aggregates below stay household-only
    # regardless (the verdict's rule), so a business account's rows can be
    # FOUND here without moving the personal figures. The Business page has
    # its own predicate (books._BIZ_TXN) and is unaffected.
    where = ["t.removed = 0" + _scope_sql(scope)]
    args: list = []
    # An explicit id list is the caller saying WHICH rows these are — the
    # budget engine handing over the rows behind one of its own numbers.
    # It is the whole predicate: the hidden/shadow filters below would
    # silently drop rows the number already counted, and the listing would
    # no longer add up to the tile it was opened from.
    if ids is not None:
        where.append("t.id = ANY(%s)")
        args.append(list(ids))
    elif account_id:
        where.append("t.account_id = %s"); args.append(account_id)
    else:
        # A HIDDEN account contributes to nothing (see links.shadow_ids), and
        # the ledger is part of "nothing". Asking for that account explicitly
        # still works — that is how its history stays reachable.
        where.append(
            "t.account_id NOT IN (SELECT id FROM accounts "
            "WHERE user_removed_at IS NOT NULL)")
        # Same for a linked non-primary — one real account reached through
        # two aggregators. Without this, `total`, `amount_sum` and the
        # spend average the page renders as trusted figures all read
        # double, and every matching row is listed twice.
        where.append("TRUE" + _NOT_SHADOW)
    if category == UNCATEGORIZED:
        # "?" is what every category surface DISPLAYS for a row with no
        # category (reporting.EFF_CAT coalesces to it), so it is also what
        # comes back as a filter — and no row stores it, so equality would
        # match nothing and the link would look broken.
        where.append("COALESCE(t.category_override, t.category_primary, '') = ''")
    elif category:
        where.append("COALESCE(t.category_override, t.category_primary, '') = %s")
        args.append(category)
    if date_from:
        where.append("t.date >= %s"); args.append(as_date(date_from))
    if date_to:
        where.append("t.date <= %s"); args.append(as_date(date_to))
    if reimb_only:
        where.append(_REIMB_ONLY.replace(" AND ", "", 1))
    if channel:
        # in store | online | other — Plaid's payment_channel
        where.append("t.payment_channel = %s"); args.append(channel)
    if checks_only:
        where.append("t.check_number IS NOT NULL")
    if search:
        like = _like(search)
        # One precomputed column for everything the row itself says (name,
        # merchant, outlet, category both ways, the bank's own statement
        # line, check number, city, payment processor — migration 111), and
        # set-membership tests for what other tables say about the row: the
        # canonical map ("doordash" finds "Doordasan"), the merchant row's
        # name, receipt line items, Amazon order items and summaries. Each
        # of those is evaluated ONCE per query as a hashed sub-select, never
        # once per ledger row, which on a large ledger costs seconds. '%q%'
        # on search_text rides the trigram index where pg_trgm exists.
        clauses = ["t.search_text LIKE %s",
                   """COALESCE(t.merchant_outlet, t.merchant_name, t.name) IN (
                          SELECT mcs.raw_merchant FROM merchant_canonical mcs
                           WHERE LOWER(mcs.canonical) LIKE %s)""",
                   """t.merchant_id IN (SELECT mmm.id FROM merchants mmm
                                        WHERE LOWER(mmm.name) LIKE %s)""",
                   """t.id IN (SELECT rc2.txn_id FROM receipts rc2
                               JOIN receipt_items ri ON ri.receipt_id = rc2.id
                              WHERE LOWER(ri.description) LIKE %s)""",
                   """t.id IN (SELECT am.transaction_id FROM amazon_matches am
                               JOIN amazon_orders ao ON ao.dedup_key = am.dedup_key
                              WHERE EXISTS (SELECT 1
                                       FROM jsonb_array_elements(
                                            """ + _as_array('ao.items_json') + """) it
                                       WHERE LOWER(it->>'title') LIKE %s)
                                 OR EXISTS (SELECT 1 FROM amazon_summaries s
                                        WHERE s.dedup_key = am.dedup_key
                                          AND LOWER(s.summary) LIKE %s))""",
                   """t.id IN (SELECT cm.transaction_id FROM costco_matches cm
                               JOIN costco_receipts cr ON cr.dedup_key = cm.dedup_key
                              WHERE EXISTS (SELECT 1
                                       FROM jsonb_array_elements(
                                            """ + _as_array('cr.items_json') + """) it
                                       WHERE LOWER(it->>'title') LIKE %s
                                          OR LOWER(it->>'description') LIKE %s))"""]
        args += [like] * 8
        # An account is findable by EITHER of its names — the user's rename
        # ("Everyday card") or the bank's own ("CREDIT CARD"). search_text is
        # a generated column and cannot reference the accounts table, so the
        # name match is resolved once on the (tiny) accounts table and the
        # ids joined in — migration 111's cross-table form, never a per-row
        # sub-select. The hits ride back to the caller so a row matched only
        # via the bank's name can explain itself on screen.
        account_hits = [dict(r) for r in conn.execute(
            f"""SELECT a.id, {_ACCT_FULL} AS label, a.name AS bank_name,
                       (a.display_name IS NOT NULL
                        AND LOWER(COALESCE(a.name,'')) LIKE %s) AS via_bank
                  FROM accounts a JOIN items i ON i.id = a.item_id
                 WHERE a.user_removed_at IS NULL
                   AND (LOWER(COALESCE(a.display_name,'')) LIKE %s
                        OR LOWER(COALESCE(a.name,'')) LIKE %s)""",
            (like, like, like)).fetchall()]
        if account_hits:
            clauses.append("t.account_id = ANY(%s)")
            args.append([r["id"] for r in account_hits])
        cleaned = search.replace(",", "").replace("$", "")
        try:
            float(cleaned)
            # float() accepts digit separators, so "1_0" reaches here and
            # its underscore would match any character in the rendered
            # amount — the typed digits must stay literal
            clauses.append("to_char(ABS(t.amount), 'FM999999990.00')"
                           " LIKE %s ESCAPE '\\'")
            args.append(_like(cleaned))
        except ValueError:
            pass
        where.append("(" + " OR ".join(clauses) + ")")
    where_sql = " AND ".join(where)
    # spend_n / spend_sum ride along so the caller can state an AVERAGE. They
    # are computed over the WHOLE result set, like the count and sum beside
    # them — averaging the 100 rows of page one would answer a question nobody
    # asked, and would change as you paged. "Spending" is the verdict's own
    # rule (budget.SPEND_ONLY_SQL), not every matched row: a search for a
    # merchant you also transfer money to would otherwise average a payment
    # against a purchase.
    # The headline total is HOUSEHOLD money like every other figure on the
    # page: a business entity's rows are listed for reference and counted
    # (biz_count) but never summed into the personal total — unless the
    # caller asked for the business scope, where they are the whole set.
    money_scope = "" if scope == "business" else budget.PERSONAL_ONLY_SQL
    # The predicate is evaluated ONCE: the ordered id list of every matching
    # row. The aggregates, the Amazon item stats and the page then read by
    # primary key from that list instead of each re-running the whole match
    # (three full passes would cost three times the predicate). The page is a
    # slice of the ordered ids, so ORDER BY below only re-sorts 100 rows.
    ids = [r["id"] for r in conn.execute(
        f"SELECT t.id FROM transactions t WHERE {where_sql} "
        f"ORDER BY t.date DESC, ABS(t.amount) DESC", args).fetchall()]
    row = conn.execute(
        f"SELECT COUNT(*) AS n, "
        f"       COALESCE(SUM(t.amount) FILTER (WHERE TRUE {money_scope}), 0) AS s, "
        f"       COUNT(*) FILTER (WHERE NOT (TRUE {budget.PERSONAL_ONLY_SQL})) AS biz_n, "
        f"       COUNT(*) FILTER (WHERE TRUE {budget.SPEND_ONLY_SQL}"
        f"                        {budget.PERSONAL_ONLY_SQL}) AS spend_n, "
        f"       COALESCE(SUM(t.amount) FILTER "
        f"                (WHERE TRUE {budget.SPEND_ONLY_SQL}"
        f"                       {budget.PERSONAL_ONLY_SQL}), 0) AS spend_sum "
        f"FROM transactions t WHERE t.id = ANY(%s)", (ids,)).fetchone()
    total, amount_sum = row["n"], row["s"]
    spend_n = row["spend_n"]
    spend_sum = round(float(row["spend_sum"]), 2)
    biz_n = row["biz_n"]
    # When the search term names an Amazon ITEM, the transaction sum
    # overstates the answer — a $90 box with $40 of honey in it counts all
    # $90. So alongside it: the true per-item total, summed from the order's
    # item prices, over the whole result set. None when nothing matches, so
    # non-Amazon searches carry no extra chrome.
    amazon_items = None
    if search and ids:
        # `unpriced` = matched items whose price is absent or unparsable;
        # they are IN the count but cannot be in the sum, and the UI must
        # say so — "$40 on 2 items" with one priceless item reads as if
        # the $40 covered both
        arow = conn.execute(
            """SELECT COUNT(*) AS n,
                       COUNT(*) FILTER (WHERE NOT
                           it->>'price' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                           OR it->>'price' IS NULL) AS unpriced,
                       COALESCE(SUM(CASE WHEN it->>'price' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                                         THEN (it->>'price')::numeric END), 0) AS s
                  FROM amazon_matches am
                  JOIN amazon_orders ao ON ao.dedup_key = am.dedup_key
                  CROSS JOIN LATERAL jsonb_array_elements(
                           """ + _as_array("ao.items_json") + """) it
                 WHERE am.transaction_id = ANY(%s) AND LOWER(it->>'title') LIKE %s""",
            (ids, _like(search))).fetchone()
        if arow["n"]:
            amazon_items = {"count": arow["n"],
                            "sum": round(float(arow["s"]), 2),
                            "unpriced": arow["unpriced"]}
    offset = max(0, (page - 1) * per_page)
    page_ids = ids[offset:offset + per_page]
    rows = conn.execute(
        f"""SELECT {_TXN_FIELDS.format(acct=_ACCT_FULL)}
            FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
                 LEFT JOIN items i ON i.id = a.item_id
                 {_MC_JOIN} {_TXN_JOINS}
            WHERE t.id = ANY(%s)
            ORDER BY t.date DESC, ABS(t.amount) DESC""",
        (page_ids,)).fetchall() if page_ids else []
    return (rows, total, round(amount_sum, 2), amazon_items,
            {"count": spend_n, "sum": spend_sum, "biz_count": biz_n},
            account_hits)


def all_categories(conn) -> list[str]:
    """Every category in use on LIVE rows — the filter dropdown. A retired
    row (removed=1) must not keep a name in the list."""
    cats = {r["category_primary"] for r in conn.execute(
        "SELECT DISTINCT category_primary FROM transactions "
        "WHERE removed = 0 AND category_primary IS NOT NULL").fetchall()}
    cats |= {r["category_override"] for r in conn.execute(
        "SELECT DISTINCT category_override FROM transactions "
        "WHERE removed = 0 AND category_override IS NOT NULL").fetchall()}
    return sorted(cats)


def all_accounts(conn):
    """Every account for the search dropdown: de-doubled full label +
    institution, live accounts first."""
    return conn.execute(
        f"""SELECT a.id, a.item_id, {_ACCT_FULL} AS label,
                   i.institution_name AS inst, a.balance_current AS bal
            FROM accounts a JOIN items i ON i.id=a.item_id
            ORDER BY (a.balance_current IS NULL), i.institution_name,
                     COALESCE(a.display_name, a.name)""").fetchall()


def accounts_detail(conn):
    return conn.execute(
        """SELECT a.id, COALESCE(a.display_name, a.name) AS name, a.mask,
                  -- the aggregator's own name, kept under any rename so the
                  -- editor can show it and offer a revert
                  a.name AS bank_name,
                  COALESCE(a.type,'') || '/' || COALESCE(a.subtype,'') AS kind,
                  a.balance_current, a.balance_available, a.item_id, a.owner,
                  a.entity_id, a.user_removed_at, i.institution_name, i.status,
                  i.aggregator,
                  -- institution branding (Plaid optional metadata): a base64
                  -- PNG the client renders inline, brand colour for the monogram
                  CASE WHEN i.logo IS NOT NULL
                       THEN '/api/institutions/' || i.id || '/logo' END
                    AS inst_logo_url,
                  i.brand_color AS inst_color, i.url AS inst_url,
                  (SELECT COUNT(*) FROM transactions t
                   WHERE t.account_id=a.id AND t.removed=0) AS txns,
                  -- a card's next due date + statement balance, straight
                  -- from the liabilities pull — shown beside the card row
                  l.raw ->> 'next_payment_due_date' AS card_due,
                  -- cast only what is a number: a restored ZIP can carry
                  -- "N/A" here, and a bare cast on it failed the whole
                  -- Accounts page (same guard as forecast.build)
                  CASE WHEN l.raw ->> 'last_statement_balance'
                            ~ '^-?[0-9]+(\\.[0-9]+)?$'
                       THEN (l.raw ->> 'last_statement_balance')::double precision
                  END AS card_statement
           FROM accounts a JOIN items i ON i.id=a.item_id
           LEFT JOIN liabilities l ON l.account_id = a.id
                                      AND a.type = 'credit'
           ORDER BY i.institution_name""").fetchall()


def items_health(conn):
    return conn.execute(
        """SELECT i.id, i.institution_name, i.status,
                  (SELECT MAX(ran_at) FROM sync_log s
                   WHERE s.item_id=i.id AND s.error IS NULL) AS last_ok,
                  (SELECT error FROM sync_log s WHERE s.item_id=i.id
                   ORDER BY ran_at DESC LIMIT 1) AS last_error
           FROM items i ORDER BY i.institution_name""").fetchall()


def set_account_display_name(conn, account_id: str, name: str) -> int:
    """User rename — a display_name override that sync never touches.
    Empty clears it (revert to the aggregator name).

    Returns rows written so the door can tell a rename from a write that
    landed on nothing: a reconnect adoption renames account ids, so an
    edit racing one can address an id that no longer exists, and
    answering ok for it reports success for a change nobody will ever
    see."""
    return conn.execute("UPDATE accounts SET display_name=%s WHERE id=%s",
                        (name.strip() or None, account_id)).rowcount


NOTE_MAX = 2000


def set_note(conn, txn_id: str, note: str) -> str:
    """Set (or clear) a free-text note on a transaction. Empty clears it.
    Returns the stored note (''). A per-transaction annotation like the
    business flag — sync never touches it."""
    note = (note or "").strip()[:NOTE_MAX]
    if not note:
        conn.execute("DELETE FROM transaction_notes WHERE txn_id=%s", (txn_id,))
        return ""
    conn.execute(
        "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,%s) "
        "ON CONFLICT (tenant_id, txn_id) DO UPDATE SET note=EXCLUDED.note, "
        "updated_at=now()", (txn_id, note))
    return note


# ---- abandoned pending authorizations -------------------------------------

# How long a hold may sit at pending=1 before a person may retire it by
# hand. Card authorizations expire in days, so a row still pending after
# two weeks is one the bank abandoned and will never post — but nothing in
# the ledger can PROVE that, which is why this is a person's call rather
# than a sweep. The window is what keeps the button off a row that is
# merely slow: a hold released tomorrow would otherwise be deleted today
# and then re-arrive as a duplicate when it posted.
PENDING_STUCK_DAYS = 14

PENDING_TOO_YOUNG = ("still settling — a pending charge is only retired "
                     "after 14 days")
NOT_PENDING = ("that charge has already posted — only a pending "
               "authorization can be retired")


def retire_pending(conn, txn_id: str, today: dt.date | None = None) -> dict:
    """Retire a pending hold the bank left behind, so it stops counting.

    A pending row is real spending until it posts or vanishes, and the
    connectors retire the ones their source stops carrying
    (sync.base.reconcile_vanished_pendings). A hold from a source with no
    removal delta — a file import, a dead connection, an issuer that simply
    forgets — has nobody to retire it, and it counts against the verdict
    every day forever.

    Retiring is `removed=1` plus a `retired_at` stamp: the same soft
    retirement every connector uses, marked as the person's so the next
    sync's upsert (which un-removes a row the feed still reports) leaves
    it retired. It
    the row leaves every read (they all filter `removed = 0`) but keeps its
    id, so a hold that does eventually post is recognised as the same
    transaction instead of arriving twice, and the retirement is reversible
    by flipping one column back.

    Raises LookupError when the id is not a live transaction, and
    ValueError when the row is posted or still inside the window — the
    caller turns those into 404 and 400.
    """
    row = conn.execute(
        "SELECT id, date, pending FROM transactions WHERE id=%s AND removed=0",
        (txn_id,)).fetchone()
    if row is None:
        raise LookupError("transaction not found")
    if not row["pending"]:
        raise ValueError(NOT_PENDING)
    from ..localtime import household_day
    # the household's day, like the engine: west of UTC in the evening the
    # process clock is already tomorrow
    age = (today or household_day(conn)) - as_date(row["date"])
    if age.days < PENDING_STUCK_DAYS:
        raise ValueError(PENDING_TOO_YOUNG)
    conn.execute("UPDATE transactions SET removed=1, retired_at=now() WHERE id=%s AND removed=0",
                 (txn_id,))
    return {"id": txn_id}


# ---- categorization overrides ---------------------------------------------


def rename_category(conn, old: str, new: str) -> dict:
    """Rename a **user/custom** category everywhere it appears.

    Custom categories are not a registry — they are free-form strings on
    ``category_override``, merchant rules, and budget carve-outs (from
    *✎ custom category…*). This rewrites that string in one shot:

      * transactions.category_override and category_primary (exact match)
      * manual_categories.category
      * merchant_categories.category_primary
      * config custom_buckets name + categories[] matchers

    **Standard Plaid primaries are not renamable** (FOOD_AND_DRINK,
    TRANSFER_OUT, … — the full ``PLAID_PRIMARIES`` set). Renaming those
    would scramble bank-sourced labels and spend-exclusion flow guards.
    The *new* name may be a Plaid SPEND primary (e.g. fold a custom label
    into FOOD_AND_DRINK) but never a flow one: this rewrites merchant rules
    too, and a rule is never allowed to name a transfer / income / loan
    payment — the categorizer's user pass writes a rule's category over
    whatever the aggregator said, so a rule naming TRANSFER_OUT would turn a
    merchant's spend into transfers on the next sync. Per-row overrides may
    still be flow (set_category); only the rule-writing doors refuse.
    Returns counts so the UI can confirm the blast radius.
    """
    from ..engine import budget, categories as cat_mod

    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new:
        raise ValueError("both the current and new names are required")
    if len(new) > 120:
        raise ValueError("new name is too long (max 120 characters)")
    if new in cat_mod.FLOW:
        raise ValueError(
            f"'{new}' is a transfer / income category — a custom category "
            "can only be folded into a spend category (mark individual "
            "transactions as transfers instead)")
    if old == new:
        return {"old": old, "new": new, "overrides": 0, "primaries": 0,
                "manual": 0, "rules": 0, "buckets": 0}
    # Exact match against the canonical Plaid primary set (case-sensitive —
    # custom names like "food and drink" stay free; FOOD_AND_DRINK does not).
    if old in cat_mod.PLAID_PRIMARIES:
        raise ValueError(
            f"'{old}' is a standard Plaid category — only custom names "
            "you created can be renamed")

    with conn.transaction():
        overrides = conn.execute(
            "UPDATE transactions SET category_override=%s "
            "WHERE category_override=%s", (new, old)).rowcount
        primaries = conn.execute(
            "UPDATE transactions SET category_primary=%s "
            "WHERE category_primary=%s", (new, old)).rowcount
        manual = conn.execute(
            "UPDATE manual_categories SET category=%s WHERE category=%s",
            (new, old)).rowcount
        # classified_at=now() marks the labeled set as moved: the nightly
        # overlay retrain's staleness check reads only COUNT(*) and
        # MAX(classified_at), and a rename changes neither — without the
        # refresh the categorizer would keep serving the old name forever
        rules = conn.execute(
            "UPDATE merchant_categories SET category_primary=%s, "
            "classified_at=now() "
            "WHERE category_primary=%s", (new, old)).rowcount
        # a bill that stamps this category on its charges follows the
        # rename too — else its next pass reverts every mark (name no
        # longer equal) and restamps the OLD name, undoing the rename
        conn.execute(
            """UPDATE bills SET raw = COALESCE(raw, '{}'::jsonb) || jsonb_build_object('txn_category', %s::text)
                WHERE raw->>'txn_category' = %s""", (new, old))

        buckets = 0
        with budget.config_txn(conn) as cfg:
            lst = list(cfg.get("custom_buckets") or [])
            if lst:
                out = []
                for b in lst:
                    if not isinstance(b, dict):
                        out.append(b)
                        continue
                    bb = dict(b)
                    if (bb.get("name") or "") == old:
                        bb["name"] = new
                        buckets += 1
                    cats = bb.get("categories")
                    if isinstance(cats, list) and old in cats:
                        bb["categories"] = [
                            new if c == old else c for c in cats]
                        buckets += 1
                    out.append(bb)
                if buckets:
                    cfg["custom_buckets"] = out

    return {"old": old, "new": new, "overrides": overrides,
            "primaries": primaries, "manual": manual, "rules": rules,
            "buckets": buckets}


def set_category(conn, txn_id: str, category: str,
                 scope: str = "one") -> dict | None:
    """Permanent single-transaction override; survives every sync.

    `scope` decides whether the correction also teaches the MERCHANT:

      "one" — this row only. No rule is written, so nothing appears on the
              Rules page. The right answer for a payee that is not one
              thing: "Check Paid" is every cheque you write, "<name>
              Transfer" is every transfer you make.
      "all" — write/refresh the user rule and re-apply it across the
              merchant (the previous behaviour, now opt-in).

    Unconditional teaching means a single correction on ONE row silently
    claims every row sharing that payee: correcting one "Check Paid" would
    pin every cheque ever written to the same category, from one edit.
    Default is "one": the narrow reading is recoverable, the broad one
    rewrites history.

    Returns None for "one" (and for an "all" that writes no rule — a flow
    category, or a row with no merchant); otherwise the same shape the
    history page's bulk write returns: the canonical taught and an `undo`
    snapshot undo_merchant_category accepts. The teach deletes
    duplicate-spelling user rules (_write_user_rule), so without a snapshot
    that delete was silent and unrecoverable from this door.
    """
    if scope not in ("one", "all"):
        raise ValueError(f"unknown category scope: {scope!r}")
    with conn.transaction():
        conn.execute(
            # bill_id NULL: this is a PERSON's answer now, even if a bill's
            # transaction category had stamped the row before — the bill
            # pass leaves it alone from here on
            """INSERT INTO manual_categories (transaction_id, category, set_at,
                                              bill_id)
               VALUES (%s,%s,now(),NULL)
               ON CONFLICT (tenant_id, transaction_id)
               DO UPDATE SET category=EXCLUDED.category, set_at=now(),
                             bill_id=NULL""",
            (txn_id, category))
        conn.execute("UPDATE transactions SET category_override=%s, "
                     "override_source='user' WHERE id=%s", (category, txn_id))
    # a correction teaches the merchant map, merchant-WIDE — every
    # past row of this merchant updates right now (apply() below) and every
    # future row lands pre-categorized, even where the aggregator had its
    # own sharp idea. Custom category names propagate the same way. The
    # only exclusions are flow categories (transfers/income/loan payments —
    # the spend-exclusion machinery's domain) and rows carrying their own
    # per-transaction override, which stay sacred.
    from ..engine import categories as _cats, llm_categorize as _llm
    if scope == "all" and category and category not in _cats.FLOW:
        row = conn.execute(
            "SELECT COALESCE(merchant_name, name) AS m FROM transactions "
            "WHERE id=%s", (txn_id,)).fetchone()
        if row and row["m"]:
            canon = _canonical_of(conn, row["m"])
            with conn.transaction():
                # Snapshot BEFORE the write, exactly as set_merchant_category
                # does for the history page: the rows this teach will move
                # and every rule resolving to the canonical (including the
                # duplicate spellings _write_user_rule is about to delete),
                # so the caller gets a real undo instead of a silent,
                # unrecoverable rule delete. The corrected row itself is
                # not in `affected` — it carries a per-row override now, so
                # undoing the teach leaves the single-row correction alone.
                affected = [{"id": r["id"], "prev": r["category_primary"]}
                            for r in _merchant_targets(conn, canon)
                            if r["category_primary"] != category]
                prior = conn.execute(
                    "SELECT category_primary, source, disabled "
                    "FROM merchant_categories WHERE merchant=%s",
                    (canon,)).fetchone()
                rules_snap = [{"merchant": pr["merchant"], "prior": dict(pr)}
                              for pr in conn.execute(
                                  f"""SELECT merchant, category_primary,
                                             source, disabled
                                        FROM merchant_categories AS m
                                       WHERE {_llm._RULE_CANON} = %s""",
                                  (canon,)).fetchall()]
                _write_user_rule(conn, canon, category)
                # scoped: the full-table sweep runs minutes on a big ledger
                # and this HTTP request waits on it (the SPA sat on a silent,
                # stale row until timeout)
                _llm.apply(conn, canon=canon)
            return {"merchant": canon,
                    "undo": {"canons": [{"canon": canon,
                                         "prior_rule": (dict(prior)
                                                        if prior else None)}],
                             "rules": rules_snap, "affected": affected}}
    return None


# bulk category override from a merchant's summary page — set a
# category once and every past + future transaction for THAT merchant (its
# canonical, per) follows. Scoped to the history page's token-subset
# view — every canonical whose raw names the page's charge list matches —
# because the write must cover exactly what the page shows (truncated
# descriptors split one store across canonicals, and a canonical-only write
# skips charges sitting right on the page); the affected COUNT is shown
# before the write so the user consents to the true, merchant-wide scope.
# Rows with their own per-transaction override are never touched;
# flow-labelled rows ARE — a user rule outranks the aggregator's transfer
# label, see llm_categorize.apply. Highest-blast-radius write in the app → owner-only
# + undoable. _merchant_targets() drives the preview COUNT and the undo
# snapshot, but the actual write is apply()'s user pass: the two predicates
# must select the same rows, or apply() rewrites rows the preview never
# counted and the undo cannot restore.
from ..engine.categories import FLOW as _BULK_FLOW  # noqa: E402 — ONE flow list
# derived, never written twice: the preview's refusal count, the bulk path's
# consequence count and this guard must name the same categories, and the
# comment above exists because they once did not
_BULK_FLOW_SQL = "(" + ",".join(f"'{c}'" for c in _BULK_FLOW) + ")"


def _canonical_of(conn, payee: str) -> str:
    r = conn.execute("SELECT canonical FROM merchant_canonical "
                     "WHERE raw_merchant=%s", (payee,)).fetchone()
    return r["canonical"] if r else payee


def _merchant_rows(conn, canon: str) -> list[dict]:
    """Every live transaction of the canonical merchant, with its category
    and per-row override. Grouped by the DISPLAY key (outlet first, via the
    imported fragments), because the write must never match differently
    than the page: keyed on merchant_name, an outlet row (a fuel arm of a
    parent brand) or a rename keyed on the outlet string resolves a
    different canonical here than the page displayed, so the preview counts
    and the undo snapshots a different set of rows than the user is looking
    at.

    The ONE merchant-scan predicate: the preview's count, its flow /
    override disclosure and the undo snapshot all derive from this list,
    so they cannot drift apart (two hand-written copies once disagreed on
    exactly which rows were "the merchant's")."""
    return conn.execute(f"""
        SELECT t.id, t.category_primary, t.category_override
        FROM transactions t
        {_MC_JOIN}
        WHERE t.removed = 0 AND {_DISPLAY_MERCHANT} = %s
    """, (canon,)).fetchall()


def _merchant_targets(conn, canon: str) -> list[dict]:
    """Non-overridden transactions of the canonical merchant — the exact
    rows a bulk category change (via the user rule + apply) will move."""
    # Flow-labelled rows ARE targets: a user's merchant rule reaches them
    # in apply()'s user pass — a Venmo/Zelle payment to a person
    # is TRANSFER_OUT to the aggregator and whatever the household says it
    # is. Only per-row overrides are left alone. Preview, undo snapshot and
    # write must count the same rows, so this mirrors that pass exactly.
    return [r for r in _merchant_rows(conn, canon)
            if r["category_override"] is None]


def _write_user_rule(conn, canon: str, category: str) -> list[dict]:
    """The user's merchant-wide answer, keyed on the canonical, and NO
    other live user rule saying the same merchant under another spelling.

    Every door that teaches a merchant (the single-row "apply to whole
    merchant" correction, the history page's bulk set) goes through here:
    a raw-keyed survivor ("Venmo — Casey Example" beside "Venmo Casey
    Example") made the Rules page list one merchant as two, and when its
    category differed it broke apply()'s unanimity gate so the user's new
    rule silently did nothing. The canonical-keyed rule covers every
    variant, so the duplicates go. Disabled rules are left alone — a
    disable is the user's own call and does not participate in the gate.

    Returns the rows it deleted, so a caller building an undo snapshot can
    prove its pre-write snapshot covered them (both teach doors snapshot
    every rule resolving to the canonical before calling here)."""
    from ..engine import llm_categorize as _llm
    _llm.upsert_user_rule(conn, canon, category)
    return [dict(r) for r in conn.execute(
        f"""DELETE FROM merchant_categories AS m
             WHERE source='user' AND NOT disabled
               AND m.merchant <> %s
               AND {_llm._RULE_CANON} = %s
         RETURNING m.merchant, m.category_primary, m.source, m.disabled""",
        (canon, canon)).fetchall()]


# Merchant matching in SQL is ONE definition, and it lives next to the Python
# matchers it has to agree with (budget.merchant_matcher / _tokens): the
# history page narrows its ledger scans with the same fragments this module
# writes with, and a second spelling here is a second thing to keep in step.
_MATCH_TEXT = budget.MATCH_TEXT_SQL
_MATCH_NORM = budget.MATCH_NORM_SQL
_tokens_sql = budget.tokens_match_sql
_merchant_match_sql = budget.merchant_match_sql


def _page_match_sql(conn, payee: str) -> tuple[str, list] | None:
    """The EXACT predicate bills.merchant_history uses for this payee, so
    the bulk write can never match looser than what the page shows.

    Three modes, in the page's own order. A name that is a live DISPLAY
    bucket — a merchant the catalog lists, which is what the ledger's
    payee now resolves to — is exactly its own raw strings and nothing
    else; without this a fuel arm's rows would be swept into the parent
    brand's write, and a renamed merchant would match by words it no
    longer has. Otherwise a BILL payee matches via its merchant matcher
    (phrase / alternatives / token forms — 'Nova Mobile' is a
    word-boundary phrase, so its lone ≥4-letter token 'mobile' must NOT
    sweep T-Mobile); an arbitrary merchant matches by token subset.
    Returns None when nothing can match (the page would list no rows).

    A SQL fragment rather than a Python predicate because the caller's job
    is to ask the ledger which descriptors match: as a callable it had to
    pull every distinct descriptor across the whole ledger into Python and
    tokenize each one."""
    from ..engine import budget, merchant_dedup
    raws = merchant_dedup.raw_strings_for(conn, payee)
    if raws:
        # A live display bucket is EXACTLY its own raw strings — the page
        # compares the row's display name, so the write compares the row's
        # raw merchant key, whole, never as a substring of the descriptor.
        # A substring form would read "care" inside longer names
        # ("healthcare", "caretech", "daycare"), so one short merchant
        # name would preview thousands of unrelated rows.
        return (f"btrim({_MATCH_TEXT}) = ANY(%s)",
                [sorted({r.lower() for r in raws})])
    row = conn.execute("SELECT merchant, raw FROM bills WHERE payee=%s LIMIT 1",
                       (payee,)).fetchone()
    key = budget._key_token(payee)
    if row is not None:
        # a bill with an identity IS its merchants' rows (the page's own
        # rule, bills.merchant_history); one without matches by its text
        ids = budget.bill_merchant_ids(
            conn, [{"payee": payee, "raw": row["raw"]}]).get(payee)
        if ids is not None:
            if not ids:
                return None
            return "t.merchant_id::text = ANY(%s)", [sorted(ids)]
        return _merchant_match_sql(row["merchant"], key)
    toks = budget._tokens(payee) or ({key} if key else set())
    if not toks:
        return None
    return _tokens_sql(toks)


def _token_family(conn, payee: str) -> list[str]:
    """The distinct raw merchant names the history page counts as this
    payee — the page's own match (see _page_match_sql). The bulk write must
    cover exactly what the page shows: truncated descriptors register one
    real store under several names/canonicals (the same shop reaching the
    ledger with its words in a different order), and a canonical-only write
    skips charges sitting right on the page."""
    from ..engine import merchant_dedup
    # a live display bucket resolves in SQL: the raw strings displaying
    # under the name, no ledger scan
    if merchant_dedup.display_in_use(conn, payee):
        fam = [r["m"] for r in conn.execute(
            f"""SELECT DISTINCT COALESCE(t.merchant_name, t.name) AS m
                  FROM transactions t {_MC_JOIN}
                 WHERE t.removed = 0 AND {_DISPLAY_MERCHANT} = %s""",
            (payee,)).fetchall() if r["m"]]
        return sorted(set(fam)) or [payee]
    # and so does everything else: the match is a predicate over the row's
    # own text, so the database can answer which descriptors satisfy it.
    # Walking the DISTINCT list in Python instead shipped every descriptor
    # in the ledger to the app and re-tokenized it there, once per bulk
    # preview — bounded by DISTINCT, but seconds of it on a big ledger.
    match = _page_match_sql(conn, payee)
    if match is None:
        return [payee]
    where, params = match
    fam = [r["m"] for r in conn.execute(
        f"""SELECT DISTINCT COALESCE(t.merchant_name, t.name) AS m
              FROM transactions t {_MC_JOIN}
             WHERE t.removed = 0 AND {where}""", params).fetchall() if r["m"]]
    return sorted(set(fam)) or [payee]


def _family_canons(conn, payee: str) -> list[str]:
    return sorted({_canonical_of(conn, raw)
                   for raw in _token_family(conn, payee)}
                  | {_canonical_of(conn, payee)})


def merchant_category_preview(conn, payee: str, category: str,
                              family: bool = True) -> dict:
    """How many transactions a bulk set would recategorize (rows of every
    canonical the page's token match covers, whose category differs), plus
    the raw name variants so the confirm can state its scope.

    `family` picks which WRITE is being previewed, because the two teach
    doors have different scopes and the confirm's count must match the
    write behind it: True (the default) is the history page's bulk set
    (set_merchant_category — token-family scoped); False is the ledger
    row's "apply to whole merchant" (set_category scope="all" — the row's
    single canonical only)."""
    canons = (_family_canons(conn, payee) if family
              else [_canonical_of(conn, payee)])
    seen: set[str] = set()
    n = 0
    for c in canons:
        for r in _merchant_targets(conn, c):
            if r["id"] not in seen and r["category_primary"] != category:
                seen.add(r["id"])
                n += 1
    # The write is canonical-granular (rules key on the canonical), so it can
    # move sibling descriptors of a covered canonical that the page's own
    # match never displayed (a wallet-prefixed descriptor pulling in the
    # store's plain one via their shared canonical). The confirm must NAME that full
    # scope, not just the token-matched names — the count already includes it.
    variants = set(_token_family(conn, payee)) if family else {payee}
    for r in conn.execute(
            f"""SELECT DISTINCT COALESCE(t.merchant_name, t.name) AS m
               FROM transactions t
               {_MC_JOIN}
               WHERE t.removed = 0
                 AND {_DISPLAY_MERCHANT} = ANY(%s)""", (canons,)).fetchall():
        if r["m"]:
            variants.add(r["m"])
    # What the write will NOT touch (a per-row override is a decision
    # someone already made by hand), and what it WILL touch that deserves a
    # word: rows the aggregator labelled as a transfer / income / loan
    # payment. A user's merchant rule reaches those — a Venmo payment to the
    # babysitter is TRANSFER_OUT to Plaid and childcare to the household,
    # and refusing them leaves every new payment landing as a transfer — but
    # moving a transfer into spend changes the budget math, so the confirm
    # names the count instead of doing it quietly. Refusing SILENTLY is what
    # makes "apply to all" look broken.
    flow_rows = skipped_override = 0
    counted: set[str] = set()
    for c in canons:
        for r in _merchant_rows(conn, c):
            if r["id"] in counted:
                continue
            counted.add(r["id"])
            if r["category_override"] is not None:
                if r["category_override"] != category:
                    skipped_override += 1
            elif r["id"] in seen and (r["category_primary"] or "") in _BULK_FLOW:
                flow_rows += 1
    return {"merchant": _canonical_of(conn, payee), "count": n,
            "variants": sorted(variants),
            "flow_rows": flow_rows,
            "skipped_override": skipped_override}


def bulk_apply(conn, ids: list[str], action: str,
               category: str = "") -> dict:
    """Apply one action to an EXPLICIT list of transactions.

    The merchant-wide write refuses flow rows (transfers, income, loan
    payments) because a rule inferred from one correction must not silently
    reclassify money movement as spending — that guard stands. This path is
    different in kind: the user selected these rows individually, which is
    consent to exactly them, and no rule is written, so nothing propagates to
    future transactions or to the Rules page. Selecting a transfer and saying
    "this is rent" is a statement about that transfer.

    Returns per-row results so the caller can report honestly rather than
    claim a blanket success.
    """
    if action not in ("category", "biz_on", "biz_off",
                      "reimb_flag", "reimb_unflag"):
        raise ValueError(f"unknown bulk action: {action!r}")
    if action == "category" and not category:
        raise ValueError("a category is required")
    ids = [i for i in dict.fromkeys(ids) if i]      # de-dup, keep order
    if not ids:
        return {"applied": 0, "missing": 0, "flow": 0}
    live = {r["id"]: r for r in conn.execute(
        "SELECT id, COALESCE(category_primary,'') AS prim FROM transactions "
        "WHERE id = ANY(%s) AND removed = 0", (ids,)).fetchall()}
    applied = flow = 0
    with conn.transaction():
        for tid in ids:
            row = live.get(tid)
            if row is None:
                continue
            if action == "category":
                if row["prim"] in _BULK_FLOW:
                    flow += 1          # allowed, but worth telling them
                conn.execute(
                    """INSERT INTO manual_categories
                           (transaction_id, category, set_at, bill_id)
                       VALUES (%s,%s,now(),NULL)
                       ON CONFLICT (tenant_id, transaction_id)
                       DO UPDATE SET category=EXCLUDED.category, set_at=now(),
                                     bill_id=NULL""",
                    (tid, category))
                conn.execute(
                    "UPDATE transactions SET category_override=%s, "
                    "override_source='user' WHERE id=%s", (category, tid))
            elif action == "biz_on":
                flag_business(conn, tid)
            elif action == "biz_off":
                unflag_business(conn, tid)
            elif action == "reimb_flag":
                flag_reimbursement(conn, tid)
            else:
                unflag_reimbursement(conn, tid)
            applied += 1
    return {"applied": applied, "missing": len(ids) - len(live), "flow": flow}


def set_merchant_category(conn, payee: str, category: str) -> dict:
    """Apply `category` to every canonical the page's token match covers,
    past + future. Returns the affected count and an undo snapshot (prior
    categories + each canonical's prior rule) for a one-click undo."""
    from ..engine import categories as _cats, llm_categorize as _llm
    if not category or category in _cats.FLOW:
        raise ValueError("a bulk category must be a spend category")
    with conn.transaction():
        canon = _canonical_of(conn, payee)
        canons = _family_canons(conn, payee)
        seen: set[str] = set()
        affected = []
        for c in canons:
            for r in _merchant_targets(conn, c):
                if r["id"] not in seen and r["category_primary"] != category:
                    seen.add(r["id"])
                    affected.append({"id": r["id"],
                                     "prev": r["category_primary"]})
        priors = []
        rules_snap = []
        for c in canons:
            prior = conn.execute(
                "SELECT category_primary, source, disabled "
                "FROM merchant_categories WHERE merchant=%s", (c,)).fetchone()
            priors.append({"canon": c,
                           "prior_rule": (dict(prior) if prior else None)})
            # "Resolve on write": the user's merchant-wide
            # choice is authoritative, so snapshot EVERY user rule that resolves
            # to this canonical and force the live ones to agree — otherwise a
            # raw-keyed split survivor with a different category makes
            # apply()'s user-pass unanimity gate (COUNT(DISTINCT)=1) silently
            # veto the write while the API still claims success. Disabled rules
            # don't participate in the gate, so they're snapshotted but never
            # re-enabled here (that would reverse the user's own disable).
            for pr in conn.execute(
                    f"""SELECT merchant, category_primary, source, disabled
                        FROM merchant_categories AS m
                        WHERE {_llm._RULE_CANON} = %s""", (c,)).fetchall():
                rules_snap.append({"merchant": pr["merchant"],
                                   "prior": dict(pr)})
            # canonical rule → target; duplicate spellings go (the undo
            # snapshot above already holds them)
            _write_user_rule(conn, c, category)
            _llm.apply(conn, canon=c)                   # past rows now
        # Honest count: rows that ACTUALLY landed on the new category, not a
        # pre-write estimate — success is never reported when nothing moved.
        ids = [a["id"] for a in affected]
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions "
            "WHERE id = ANY(%s) AND category_primary=%s "
            "AND category_override IS NULL", (ids, category)
        ).fetchone()["n"] if ids else 0
    return {"merchant": canon, "count": count,
            "undo": {"canons": priors, "rules": rules_snap,
                     "affected": affected}}


# every source merchant_categories.source can hold — the Rules page shows
# them all (model-written rules were invisible for a while: written by
# model_pass, counted in `total`, absent from every group)
RULE_SOURCES = ("user", "llm", "seed", "model")
RULES_PER_PAGE = 50


def list_rules(conn, source: str | None = None, q: str = "",
               page: int = 1, per_page: int = RULES_PER_PAGE) -> dict:
    """The categorization rules the system is applying, with provenance
    (user / llm / seed) and each canonical merchant's current transaction
    count. The page shows three provenance groups loaded lazily, so this
    filters by `source`, searches merchant+category (`q`), and paginates
    (~50/page — a mature ledger has thousands of rules and nothing may render
    them all at once). `counts` carries the q-filtered per-source totals for
    every group, so collapsed headers can show counts without loading
    rows."""
    where, params = "TRUE", []
    if (q or "").strip():
        # The search box takes literal text, not a pattern: percent,
        # underscore and backslash must match themselves. Unescaped, a
        # lone percent returned every rule and made the collapsed
        # per-source headers report unfiltered counts, and a category
        # label carrying an underscore matched anything. ESCAPE pins the
        # escape character instead of trusting the server default.
        where += (" AND (mc.merchant ILIKE %s ESCAPE '\\'"
                  " OR mc.category_primary ILIKE %s ESCAPE '\\')")
        params += [_like(q.strip())] * 2
    counts = {s: 0 for s in RULE_SOURCES}
    for r in conn.execute(
            f"""SELECT mc.source, COUNT(*) AS n FROM merchant_categories mc
                WHERE {where} GROUP BY mc.source""", params).fetchall():
        if r["source"] in counts:
            counts[r["source"]] = r["n"]
    if source:
        where += " AND mc.source = %s"
        params = params + [source]
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM merchant_categories mc WHERE {where}",
        params).fetchone()["n"]
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or RULES_PER_PAGE), 200))
    rows = conn.execute(f"""
        SELECT mc.merchant, mc.category_primary, mc.source, mc.disabled,
               COALESCE(c.n, 0) AS count
        FROM merchant_categories mc
        LEFT JOIN (
            SELECT COALESCE(m2.canonical, COALESCE(t.merchant_name, t.name))
                       AS canon, COUNT(*) AS n
            FROM transactions t
            LEFT JOIN merchant_canonical m2
                   ON m2.raw_merchant = COALESCE(t.merchant_name, t.name)
            WHERE t.removed = 0
            GROUP BY 1
        ) c ON c.canon = mc.merchant
        WHERE {where}
        ORDER BY mc.source = 'user' DESC, mc.disabled, mc.merchant
        LIMIT %s OFFSET %s""",
        params + [per_page, (page - 1) * per_page]).fetchall()
    return {"rules": [dict(r) for r in rows], "total": total,
            "page": page, "per_page": per_page, "counts": counts}


def set_rule_disabled(conn, merchant: str, disabled: bool) -> None:
    """Toggle a rule on/off. Disabling stops it categorizing going forward
    (past rows it already set keep their category until re-synced), and it
    PROMOTES the rule to source='user' — the person made a call about it,
    so it moves to "Your rules" and the Model-learned group stays purely
    untouched inferences. Re-enabling keeps source='user'."""
    with conn.transaction():
        if disabled:
            conn.execute("UPDATE merchant_categories "
                         "SET disabled=true, source='user' "
                         "WHERE merchant=%s", (merchant,))
        else:
            conn.execute("UPDATE merchant_categories SET disabled=false "
                         "WHERE merchant=%s", (merchant,))
        if not disabled:
            from ..engine import llm_categorize as _llm
            # re-enabling re-sweeps past rows (scoped to this rule's merchant)
            _llm.apply(conn, canon=_canonical_of(conn, merchant))


def delete_rule(conn, merchant: str) -> int:
    cur = conn.execute("DELETE FROM merchant_categories WHERE merchant=%s",
                       (merchant,))
    return cur.rowcount


# An undo snapshot is client-held state: the apply response hands it to the
# SPA, which posts it back. Nothing ties it to a prior apply, so it is
# validated like any other body — a wrong shape is a 400, not a 500, and
# every value it writes is bounded to what a genuine snapshot can hold.
UNDO_MAX_AFFECTED = 5000
UNDO_MAX_RULES = 200
UNDO_MAX_CANONS = 200
_NAME_MAX = 200


def _undo_name(v, what: str, max_len: int = _NAME_MAX) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"undo: {what} must be a non-empty string")
    if len(v) > max_len:
        raise ValueError(f"undo: {what} too long ({max_len} chars max)")
    if any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in v):
        raise ValueError(f"undo: {what} contains control characters")
    return v


def _undo_rule_prior(p, what: str, default_source: str = "user") -> dict:
    """A snapshotted rule row: a bounded category name, provenance from the
    known set (a forged `source` would dress a user's rule up as the
    aggregator's or the model's), and a boolean disabled flag.

    The category gets SHAPE checks only (length, control characters):
    restoring what the rule table held before the write is always
    legitimate, and refusing by content made genuine snapshots
    un-undoable forever — a legacy flow-named rule, or a custom label
    over the old 120-char cap, sat in the snapshot because it sat in the
    table. A restored flow-named rule is inert anyway: apply() refuses to
    act on flow-named rules outright (llm_categorize's rules CTE), so
    putting one back cannot turn spend into transfers."""
    if not isinstance(p, dict):
        raise ValueError(f"undo: {what} must be an object")
    cat_name = _undo_name(p.get("category_primary"),
                          f"{what}.category_primary")
    source = p.get("source", default_source)
    if source not in RULE_SOURCES:
        raise ValueError(f"undo: {what}.source must be one of "
                         f"{', '.join(RULE_SOURCES)}")
    return {"category_primary": cat_name, "source": source,
            "disabled": bool(p.get("disabled"))}


def _validate_undo(conn, undo) -> tuple[list[dict], list[dict],
                                        list[dict] | None]:
    """Normalise an undo body into (canons, affected, rules) — every entry
    a dict of the expected keys with bounded, known-good values — or raise
    ValueError. Accepts the multi-canonical shape ({"canons": [...]}, the
    token-family write) and the legacy single-canon shape."""
    if not isinstance(undo, dict):
        raise ValueError("undo must be an object")
    canons_in = undo.get("canons")
    if canons_in is None and undo.get("canon"):
        canons_in = [{"canon": undo.get("canon"),
                      "prior_rule": undo.get("prior_rule")}]
    canons_in = canons_in or []
    affected_in = undo.get("affected") or []
    rules_in = undo.get("rules")
    for name, lst, cap in (("canons", canons_in, UNDO_MAX_CANONS),
                           ("affected", affected_in, UNDO_MAX_AFFECTED),
                           ("rules", rules_in if rules_in is not None else [],
                            UNDO_MAX_RULES)):
        if not isinstance(lst, list):
            raise ValueError(f"undo: {name} must be a list")
        if len(lst) > cap:
            raise ValueError(f"undo: too many {name} ({cap} max)")

    canons: list[dict] = []
    for i, entry in enumerate(canons_in):
        if not isinstance(entry, dict):
            raise ValueError(f"undo: canons[{i}] must be an object")
        c = _undo_name(entry.get("canon"), f"canons[{i}].canon")
        prior = entry.get("prior_rule")
        if prior is not None:
            prior = _undo_rule_prior(prior, f"canons[{i}].prior_rule", "llm")
        canons.append({"canon": c, "prior_rule": prior})
    canon_set = {c["canon"] for c in canons}

    rules: list[dict] | None = None
    if rules_in is not None:
        rules = []
        for i, r in enumerate(rules_in):
            if not isinstance(r, dict):
                raise ValueError(f"undo: rules[{i}] must be an object")
            merchant = _undo_name(r.get("merchant"), f"rules[{i}].merchant")
            # a snapshot only ever holds rules that resolve to a touched
            # canonical; anything else is not this undo's to restore
            if _canonical_of(conn, merchant) not in canon_set:
                raise ValueError(
                    f"undo: rules[{i}] does not belong to a touched merchant")
            rules.append({"merchant": merchant,
                          "prior": _undo_rule_prior(r.get("prior"),
                                                    f"rules[{i}].prior")})

    # A prior category is RESTORED, not authored: the write displaced it,
    # so putting it back is always legitimate, flow categories included
    # (restoring TRANSFER_OUT to a row the write moved into spend is the
    # point of undo). Shape checks only — a membership test against
    # "known" categories rejected genuine snapshots: a custom label that
    # lived only on the moved rows exists nowhere else once the write
    # lands, so its own undo 400'd forever.
    affected: list[dict] = []
    for i, a in enumerate(affected_in):
        if not isinstance(a, dict):
            raise ValueError(f"undo: affected[{i}] must be an object")
        tid = _undo_name(a.get("id"), f"affected[{i}].id")
        prev = a.get("prev")
        if prev is not None:
            prev = _undo_name(prev, f"affected[{i}].prev")
        affected.append({"id": tid, "prev": prev})
    return canons, affected, rules


def undo_merchant_category(conn, undo: dict) -> int:
    """Reverse a set_merchant_category: restore each row's prior category and
    every touched canonical's prior rule (or remove a rule that didn't
    exist). Accepts the multi-canonical shape ({"canons": [...]}, the
    token-family write) and the legacy single-canon shape. A malformed
    body raises ValueError (the API's 400), never a 500."""
    from ..engine import llm_categorize as _llm
    canons, affected, rules = _validate_undo(conn, undo)
    if not canons:
        return 0
    with conn.transaction():
        for a in affected:
            conn.execute(
                # the prior category comes back; its provenance was not
                # snapshotted, so it reads as a rule (which is what a
                # merchant-wide write always displaces)
                "UPDATE transactions SET category_primary=%s, "
                "category_source=COALESCE(category_source, 'rule') "
                "WHERE id=%s AND category_override IS NULL",
                (a["prev"], a["id"]))
        # The write snapshots EVERY user rule resolving to a touched canonical
        # (`rules`), so undo restores the exact prior rule set: wipe every
        # current user rule under each canonical, then re-insert the snapshot.
        # A rule the write CREATED is absent from the snapshot, so it stays
        # gone; overwritten/reset rules return to their prior category and
        # disabled state. Old payloads carry no `rules` → the legacy
        # canonical-only restore below still applies.
        if rules is not None:
            for entry in canons:
                conn.execute(
                    f"""DELETE FROM merchant_categories AS m
                        WHERE source='user' AND {_llm._RULE_CANON} = %s""",
                    (entry["canon"],))
            for r in rules:
                p = r["prior"]
                conn.execute(
                    """INSERT INTO merchant_categories
                           (merchant, category_primary, source, disabled)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, merchant) DO UPDATE SET
                           category_primary=EXCLUDED.category_primary,
                           source=EXCLUDED.source, classified_at=now(),
                           disabled=EXCLUDED.disabled""",
                    (r["merchant"], p["category_primary"], p["source"],
                     p["disabled"]))
            # No apply() here: the affected rows were already restored to their
            # exact prior categories above; re-running categorization would
            # re-clobber them from the just-restored (possibly un-applied)
            # rules. Restoring the rule table to its prior state is enough.
            return len(affected)
        for entry in canons:
            canon = entry["canon"]
            prior = entry["prior_rule"]
            if prior:
                # Restore `disabled` too: set_merchant_category re-enables the
                # rule (upsert_user_rule forces disabled=false), so an undo
                # that omitted it left a previously-disabled rule live —
                # silently reversing the user's disable and re-clobbering the
                # categories on the next apply.
                conn.execute(
                    """INSERT INTO merchant_categories
                           (merchant, category_primary, source, disabled)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, merchant) DO UPDATE SET
                           category_primary=EXCLUDED.category_primary,
                           source=EXCLUDED.source, classified_at=now(),
                           disabled=EXCLUDED.disabled""",
                    (canon, prior["category_primary"], prior["source"],
                     prior["disabled"]))
            else:
                conn.execute("DELETE FROM merchant_categories "
                             "WHERE merchant=%s AND source='user'", (canon,))
    return len(affected)


# A person's answer of "no category at all". manual_categories.category is
# NOT NULL, so the empty string is the only way to spell it — and it is the
# spelling the door already uses (POST .../category with an empty category IS
# the clear). What the marker buys is the row itself: an unstamped
# manual_categories row with bill_id NULL says "a person decided this",
# which is exactly the condition every automatic stamper skips. Without a
# row there is nothing to distinguish "the user cleared this" from "nobody
# ever categorized it", and the bill and store passes restamp the second one.
_CLEARED = ""


def clear_category(conn, txn_id: str) -> None:
    """Undo an override — the row falls back to the aggregator's native
    category.

    Undoing YOUR OWN pin is a true reset: the pin goes, the kind goes, and
    the automatic layers have the row back. Undoing an override an
    automatic layer wrote — a bill's stamp, an Amazon/Costco item match,
    or a store's "Unmatched" placeholder — cannot be, because that layer
    re-runs within the hour and writes the same override again. So there
    the reset is recorded as the person's own answer of "no category": the
    empty pin (`_CLEARED`, bill_id NULL) over a NULL override carrying
    their kind, which outranks every automatic writer. The row then reads
    as the aggregator's own category — read through the NULL override, so
    a later merchant rule still improves it, where an override frozen at
    today's primary would have hidden every improvement — and the reset
    stays reset.

    Clearing an already-cleared row is a no-op for the same reason:
    deleting the empty pin would hand the row straight back."""
    with conn.transaction():
        row = conn.execute(
            """SELECT m.transaction_id IS NOT NULL AS pinned, m.bill_id,
                      m.category AS pin, t.override_source
                 FROM transactions t
                 LEFT JOIN manual_categories m ON m.transaction_id = t.id
                WHERE t.id=%s""", (txn_id,)).fetchone()
        if row is None:
            return
        mine = row["pinned"] and not row["bill_id"]
        if mine and row["pin"] == _CLEARED:
            return
        if not mine and (row["bill_id"]
                         or row["override_source"] not in (None, "user")):
            conn.execute(
                """INSERT INTO manual_categories (transaction_id, category,
                                                  set_at, bill_id)
                   VALUES (%s,%s,now(),NULL)
                   ON CONFLICT (tenant_id, transaction_id)
                   DO UPDATE SET category=EXCLUDED.category, set_at=now(),
                                 bill_id=NULL""",
                (txn_id, _CLEARED))
            conn.execute("UPDATE transactions SET category_override=NULL, "
                         "override_source='user' WHERE id=%s", (txn_id,))
            return
        conn.execute("DELETE FROM manual_categories WHERE transaction_id=%s",
                     (txn_id,))
        conn.execute("UPDATE transactions SET category_override=NULL, "
                     "override_source=NULL WHERE id=%s", (txn_id,))


# ---- reimbursement pairing: EXPLICIT only — the user picks the expense,
# then the reimbursing deposit; no pattern rules


def link_reimbursement(conn, txn_id: str, other_id: str,
                       partial: bool = False) -> dict:
    """Pair an expense with the deposit that reimburses it. Direction is
    auto-oriented by sign (positive = money out = the expense).

    FULL pair: both sides get pass-through semantics — expense →
    TRANSFER_OUT, deposit → TRANSFER_IN — so spend/income math treat the
    pair as the user's own money moving.

    PARTIAL pair: only a fraction came back (co-pay + insurance check).
    The deposit still becomes TRANSFER_IN, but the expense KEEPS its
    category — spend math nets the received amount off the row
    (reporting.NET_AMOUNT) and the remainder stays real spend. Several
    partial deposits can pair to one charge, and one deposit can split
    across several charges: each link records min(charge's remaining need,
    deposit's remaining balance), so link amounts never sum past either
    side (both caps are enforced below). The awaiting flag clears only once
    the received total covers the flag's expected value (or the whole
    charge when no expectation was recorded).

    Every link — full or partial — runs as ONE transaction under ONE
    per-tenant lock, so concurrent links serialize and a half-written pair
    is never visible.

    The partial caps below are check-then-act (read a SUM, compare,
    INSERT): two concurrent links sharing either side would each read the
    pre-insert total, both pass, and both insert — a deposit credited past
    its balance or a charge netted below zero, exactly what the caps exist
    to refuse.

    The lock used to be taken per transaction ROW, in sorted id order, and
    only on the partial path. That left two doors open. The full path took
    no lock and no transaction at all, so its INSERT committed before
    either category write and spend math could see a pair whose expense was
    still ordinary spend — the pair counted twice for as long as the gap
    lasted, and a failure inside the gap left it that way. And holding one
    row's lock while waiting for another's IS a lock order: correct here,
    but one call site away from a cycle, and a cycle under host load
    surfaces as a deadlock rather than as a wrong number. A single
    per-tenant lock has no order to get wrong. Linking is a hand-driven
    action a household performs a few times a month, so serializing it
    costs nothing worth measuring.

    The explicit transaction block is load-bearing: these are autocommit
    connections (see budget.config_txn), so outside it an xact-scoped lock
    would release the instant the statement returned and guard nothing.
    The lock is keyed on the tenant because advisory locks ignore RLS."""
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(COALESCE("
            "current_setting('app.tenant_id', true), '')), "
            "hashtext('reimbursement-link'))")
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, amount FROM transactions "
            "WHERE id IN (%s,%s) AND removed=0",
            (txn_id, other_id)).fetchall()}
        if len(rows) != 2:
            return {"error": "transaction not found"}
        a, b = rows[txn_id], rows[other_id]
        if (a["amount"] > 0) == (b["amount"] > 0):
            return {"error": "pick one charge and one deposit — both rows "
                             "flow the same direction"}
        expense = a if a["amount"] > 0 else b
        reimb = b if expense is a else a
        if partial:
            dep_amt = round(abs(reimb["amount"]), 2)
            # A partial pair nets the received money off the charge, so
            # over-linking deposits drove the row NEGATIVE — spend
            # understated, a false UNDER BUDGET. Cumulative received may not
            # exceed the charge. (reporting/budget also floor the net at 0
            # for rows linked before this guard existed.)
            got = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS got FROM reimbursements "
                "WHERE expense_id=%s AND partial=1",
                (expense["id"],)).fetchone()["got"]
            room = round(float(expense["amount"]) - float(got), 2)
            if room <= 0.005:
                return {"error": (
                    f"that charge is already fully reimbursed — "
                    f"${float(got):,.2f} of ${float(expense['amount']):,.2f} "
                    f"already came back")}
            # Cap the DEPOSIT side too. Without this, one $100 deposit
            # partial-linked to N different $100 charges recorded the FULL
            # deposit N times, netting them ALL to $0 (true economics: $100
            # out). Remaining-balance semantics: each link records
            # min(charge need, deposit remaining) — Σ amounts per
            # reimburse_id can never exceed abs(deposit.amount).
            applied = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS got FROM reimbursements "
                "WHERE reimburse_id=%s AND partial=1",
                (reimb["id"],)).fetchone()["got"]
            dep_left = round(dep_amt - float(applied), 2)
            if dep_left <= 0.005:
                return {"error": (
                    f"that deposit is already fully applied — all "
                    f"${dep_amt:,.2f} of it reimburses other charges")}
            received = round(min(room, dep_left), 2)
            conn.execute(
                "INSERT INTO reimbursements (expense_id, reimburse_id, "
                "partial, amount) VALUES (%s,%s,1,%s) ON CONFLICT DO NOTHING",
                (expense["id"], reimb["id"], received))
            set_category(conn, reimb["id"], "TRANSFER_IN")
            conn.execute("DELETE FROM reimburse_flags WHERE txn_id=%s",
                         (reimb["id"],))
            # the charge stays awaiting until enough came back
            flag = conn.execute(
                "SELECT expected FROM reimburse_flags WHERE txn_id=%s",
                (expense["id"],)).fetchone()
            total = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS got FROM reimbursements "
                "WHERE expense_id=%s AND partial=1",
                (expense["id"],)).fetchone()["got"]
            target = (flag["expected"] if flag and flag["expected"]
                      else expense["amount"])
            if flag is not None and total >= float(target) - 0.005:
                conn.execute("DELETE FROM reimburse_flags WHERE txn_id=%s",
                             (expense["id"],))
            return {"expense": expense["id"], "reimburse": reimb["id"],
                    "partial": True, "received": received}
        conn.execute("INSERT INTO reimbursements (expense_id, reimburse_id) "
                     "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                     (expense["id"], reimb["id"]))
        set_category(conn, expense["id"], "TRANSFER_OUT")
        set_category(conn, reimb["id"], "TRANSFER_IN")
        # a matched charge is no longer awaiting — clear any flags
        conn.execute("DELETE FROM reimburse_flags WHERE txn_id IN (%s,%s)",
                     (expense["id"], reimb["id"]))
        return {"expense": expense["id"], "reimburse": reimb["id"]}


def link_reimbursements(conn, txn_id: str, other_ids: list[str],
                        partial: bool = False) -> dict:
    """Link several counterparts to one anchor in one shot — one check often
    covers many charges. Failures are reported, not fatal."""
    linked, errors = [], []
    for oid in other_ids:
        r = link_reimbursement(conn, txn_id, oid, partial=partial)
        (errors if "error" in r else linked).append(r.get("error", oid))
    return {"linked": len(linked), "errors": errors}


def unlink_reimbursement(conn, expense_id: str, reimburse_id: str) -> None:
    """Remove a pair; each side falls back to the native category unless
    another pair still references it. A partial pair never touched the
    expense's category, so only the deposit side falls back."""
    row = conn.execute(
        "DELETE FROM reimbursements WHERE expense_id=%s AND reimburse_id=%s "
        "RETURNING partial", (expense_id, reimburse_id)).fetchone()
    was_partial = bool(row and row["partial"])
    sides = (reimburse_id,) if was_partial else (expense_id, reimburse_id)
    for tid in sides:
        still = conn.execute(
            "SELECT 1 FROM reimbursements WHERE expense_id=%s OR reimburse_id=%s LIMIT 1",
            (tid, tid)).fetchone()
        if still is None:
            clear_category(conn, tid)


def flag_reimbursement(conn, txn_id: str, partial: bool = False,
                       expected: float | None = None) -> None:
    """Mark a charge 'reimbursement expected later' — the awaiting list.
    Deliberately does NOT touch the category: the charge stays real spend
    until the deposit actually lands and is matched. `partial` marks a
    fraction-back expectation; `expected` (optional) is the value the
    awaiting badge waits for."""
    row = conn.execute("SELECT 1 FROM transactions WHERE id=%s AND removed=0",
                       (txn_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO reimburse_flags (txn_id, partial, expected) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, txn_id) DO UPDATE SET "
        "partial=EXCLUDED.partial, expected=EXCLUDED.expected",
        (txn_id, 1 if partial else 0, expected))


def unflag_reimbursement(conn, txn_id: str) -> None:
    conn.execute("DELETE FROM reimburse_flags WHERE txn_id=%s", (txn_id,))


def pending_reimbursements(conn) -> list:
    """Charges flagged as awaiting reimbursement, oldest first."""
    return conn.execute(
        f"""SELECT t.id, t.date, t.amount, t.pending,
                   {_DISPLAY_MERCHANT} AS payee,
                   REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ') AS category,
                   {_ACCT_FULL} AS account, f.flagged_at,
                   f.partial, f.expected,
                   COALESCE((SELECT SUM(pr.amount) FROM reimbursements pr
                             WHERE pr.expense_id = t.id AND pr.partial = 1),
                            0) AS received
            FROM reimburse_flags f
            JOIN transactions t ON t.id = f.txn_id AND t.removed = 0
            LEFT JOIN accounts a ON a.id = t.account_id
            LEFT JOIN items i ON i.id = a.item_id
            {_MC_JOIN}
            ORDER BY t.date ASC""").fetchall()


def recent_reimbursement_pairs(conn, limit: int = 50) -> list:
    """The tenant's linked pairs, newest first — the UNDO surface.

    A wrong match stamps both sides TRANSFER_* and the charge leaves the
    pending list, so without this the mistake is invisible everywhere the
    person would look; the matched list puts every pair one click from
    unlink_reimbursement."""
    return conn.execute(
        f"""SELECT r.expense_id, r.reimburse_id, r.partial,
                  r.amount AS received, r.created_at,
                  t.date AS expense_date, t.amount AS expense_amount,
                  {_DISPLAY_MERCHANT} AS expense_payee,
                  d.date AS deposit_date, d.amount AS deposit_amount,
                  COALESCE(NULLIF(d.merchant_name, ''), d.name)
                      AS deposit_payee
           FROM reimbursements r
           JOIN transactions t ON t.id = r.expense_id
           JOIN transactions d ON d.id = r.reimburse_id
           {_MC_JOIN}
           ORDER BY r.created_at DESC, t.date DESC
           LIMIT %s""", (int(limit),)).fetchall()


def reimbursement_pairs(conn, txn_id: str) -> list:
    """Pairs this transaction participates in, with counterpart details."""
    return conn.execute(
        f"""SELECT r.expense_id, r.reimburse_id, r.partial, r.amount AS received,
                  CASE WHEN r.expense_id=%(t)s THEN r.reimburse_id
                       ELSE r.expense_id END AS other_id,
                  t.date, t.amount, {_DISPLAY_MERCHANT} AS payee
           FROM reimbursements r
           JOIN transactions t ON t.id = CASE WHEN r.expense_id=%(t)s
                                              THEN r.reimburse_id ELSE r.expense_id END
           {_MC_JOIN}
           WHERE r.expense_id=%(t)s OR r.reimburse_id=%(t)s
           ORDER BY t.date DESC""", {"t": txn_id}).fetchall()


def reimbursement_candidates(conn, txn_id: str, q: str = "", limit: int = 40):
    """Opposite-direction transactions near the anchor, best matches first
    (amount closeness, then date proximity, ±180 days).

    Deposit-side candidates (anchor = a charge) come from the tenant's
    primary checking account — config `checking_account_id`, else any
    depository checking — AND from the charge's own account, because a
    merchant refund is credited back to the card that was charged, never
    to checking; a card payment on that account is not a refund and is
    kept out by its category. Charge-side candidates
    exclude investment/crypto internals and card settlements but KEEP
    already-TRANSFER_OUT charges selectable."""
    anchor = conn.execute(
        "SELECT id, date, amount, account_id FROM transactions "
        "WHERE id=%s AND removed=0", (txn_id,)).fetchone()
    if anchor is None:
        return None, []
    side = "t.amount < 0" if anchor["amount"] > 0 else "t.amount > 0"
    args: list = []
    if anchor["amount"] > 0:   # looking for the reimbursing deposit
        cfg = budget.load_config(conn)
        acct = cfg.get("checking_account_id")
        if acct:
            checking = "t.account_id = %s"
            args.append(acct)
        else:
            checking = ("t.account_id IN (SELECT a2.id FROM accounts a2"
                        " WHERE a2.type = 'depository'"
                        " AND a2.subtype = 'checking')")
        # the charge's own account carries refunds; its settlements
        # (card payments) are credits too, and are not reimbursements
        side += (f" AND ({checking} OR (t.account_id = %s"
                 " AND COALESCE(t.category_override, t.category_primary, '')"
                 " NOT LIKE 'LOAN_PAYMENTS%%'))")
        args.append(anchor["account_id"])
    else:   # the charges a check covers: real spend surfaces only
        side += (" AND t.account_id IN (SELECT a2.id FROM accounts a2"
                 " WHERE a2.type IN ('credit','depository'))"
                 " AND COALESCE(t.category_override, t.category_primary, '')"
                 " NOT LIKE 'LOAN_PAYMENTS%%'")
    args += [anchor["date"], anchor["date"]]
    extra = ""
    if q.strip():
        # the typed filter is literal text — a `%` here narrowed nothing
        # and a `_` widened the candidate list silently
        extra = (" AND LOWER(COALESCE(t.merchant_name, t.name))"
                 " LIKE %s ESCAPE '\\'")
        args.append(_like(q.strip()))
    args += [abs(anchor["amount"]), anchor["date"]]   # ORDER BY placeholders
    rows = conn.execute(
        f"""SELECT t.id, t.date, t.amount, t.pending,
                   {_DISPLAY_MERCHANT} AS payee,
                   REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ') AS category,
                   {_ACCT_FULL} AS account
            FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
                 LEFT JOIN items i ON i.id = a.item_id
                 {_MC_JOIN}
            WHERE t.removed = 0 AND {side}
              AND t.date >= %s - INTERVAL '180 days'
              AND t.date <= %s + INTERVAL '180 days'{extra}
            ORDER BY ABS(ABS(t.amount) - %s) ASC,
                     ABS(t.date - %s) ASC
            LIMIT {int(limit)}""", args).fetchall()
    return anchor, rows


def flag_business(conn, txn_id: str) -> None:
    """Tag a transaction as a business expense — a Schedule C marker,
    deliberately separate from categories; verdict math is unchanged
    (the money still left the account)."""
    row = conn.execute("SELECT 1 FROM transactions WHERE id=%s AND removed=0",
                       (txn_id,)).fetchone()
    if row is None:
        return
    conn.execute("INSERT INTO business_flags (txn_id) VALUES (%s) "
                 "ON CONFLICT DO NOTHING", (txn_id,))


def unflag_business(conn, txn_id: str) -> None:
    conn.execute("DELETE FROM business_flags WHERE txn_id=%s", (txn_id,))


def business_expenses(conn, year: int | None = None,
                      limit: int | None = None, offset: int = 0) -> dict:
    """Flagged transactions (newest first) + totals; year=None -> all years.
    Sign convention preserved: positive amount = money out = an expense;
    negative flagged rows (refunds of a business purchase) net against
    the total, which is exactly what a Schedule C wants.

    `limit`/`offset` page the rows (the on-screen worksheet); `total`,
    `count` and `years` always cover the WHOLE scope, so the headline
    numbers stay truthful on any page. limit=None returns everything —
    the CSV export is the full worksheet by definition."""
    where = "WHERE t.removed = 0"
    args: list = []
    if year is not None:
        where += " AND t.date >= %s AND t.date < %s"
        args += [dt.date(year, 1, 1), dt.date(year + 1, 1, 1)]
    page = ""
    if limit is not None:
        page = " LIMIT %s OFFSET %s"
        args = args + [int(limit), max(0, int(offset))]
    rows = conn.execute(
        f"""SELECT t.id, t.date, t.amount, t.pending,
                   {_DISPLAY_MERCHANT} AS payee,
                   REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ') AS category,
                   (SELECT note FROM transaction_notes tn
                    WHERE tn.txn_id=t.id) AS note,
                   bf.flagged_at, {_ACCT_FULL} AS account
            FROM business_flags bf
            JOIN transactions t ON t.id = bf.txn_id
            LEFT JOIN accounts a ON a.id = t.account_id
            LEFT JOIN items i ON i.id = a.item_id
            {_MC_JOIN}
            {where}
            ORDER BY t.date DESC, t.id{page}""", args).fetchall()
    agg_args = args[:2] if year is not None else []
    agg = conn.execute(
        f"""SELECT COALESCE(SUM(t.amount), 0) AS total, COUNT(*) AS n
            FROM business_flags bf
            JOIN transactions t ON t.id = bf.txn_id
            {where}""", agg_args).fetchone()
    years = [r["y"] for r in conn.execute(
        """SELECT DISTINCT EXTRACT(YEAR FROM t.date)::int AS y
           FROM business_flags bf JOIN transactions t ON t.id = bf.txn_id
           WHERE t.removed = 0 ORDER BY y DESC""").fetchall()]
    return {"rows": rows,
            "total": round(float(agg["total"]), 2),
            "count": int(agg["n"]), "years": years}
