"""the Coinbase collector's push door — a host-side script
pulls from the Coinbase API (CDP keys never leave the host) and POSTs
the normalized payload here with a script token, exactly like the
plan-CSV endpoint. This module is the product-side half: upsert items /
accounts / transactions and retire rows the source no longer reports
(window-replace inside the pushed prefix).

Payload: {items: [{id, institution_name}],
          accounts: [{id, item_id, name, type, subtype?, mask?,
                      balance_current?, balance_available?, currency?,
                      raw?}],
          transactions: [{id, account_id, date, amount, name,
                          merchant_name?, pending?, category_primary?,
                          category_detailed?, raw?}],
          full_replace: bool (optional),
          prefix: "coinbase:"}

Stale-removal scope: absence is only
treated as deletion where the push actually restated. With
``full_replace: true`` (the community collector sends it — it always
pushes its complete current view) every ``coinbase:`` row on the pushed
accounts that is missing from the payload is retired. Without it, only
rows on the DATES each account actually restated are pruned
so a partial, sparse (anchors years apart), or accidentally EMPTY
``transactions`` list can never wipe an account's history.
"""

from __future__ import annotations

import datetime as dt
import math

from . import base

MAX_ROWS = 20_000

# A script token pushing here may ONLY create /
# touch rows in Coinbase's own namespace. The door used to trust the
# request's account/item ids and a free-form `prefix`, so a leaked
# collector token could re-home a SimpleFIN account onto the coinbase
# item, rewrite its balance, and mass soft-delete another source's rows
# (`prefix: "sfin:"`). Everything is now derived server-side and refused
# unless it carries these namespaces.
ITEM_PREFIX = "coinbase-"       # items/accounts: coinbase-<label>
TXN_PREFIX = "coinbase:"        # transactions: coinbase:<uuid>
# an existing account with a coinbase-shaped id may still belong to a
# live aggregator (a stray plaid/simplefin row) — only coinbase/csv/manual
# rows are safe for this door to overwrite (enforced in SQL below).


def _money(value, what: str) -> float:
    """Coerce a JSON money field, or say which field was wrong.

    The payload comes from a host-side script, so a field can be a
    number, a string, null, or anything else JSON allows. Coercing here
    — before any row is written — is what keeps a bad value a 400 that
    names the field: `transactions.amount` is NUMERIC NOT NULL, and an
    uncoerced null used to travel all the way to Postgres, whose
    complaint is a psycopg error rather than the ValueError this door's
    caller translates into a bad-payload answer. Same helper shape as
    plan_csv's _num() and amazon_orders' float(), which is where the other
    two push doors already do this.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{what}: {value!r} is not a number")
    try:
        num = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what}: {value!r} is not a number")
    # NaN and infinity survive float() and are storable in NUMERIC, where
    # they would poison every sum the account appears in
    if not math.isfinite(num):
        raise ValueError(f"{what}: {value!r} is not a finite number")
    return num


def _money_or_none(value, what: str) -> float | None:
    """A balance is optional — the collector sends an explicit null for
    the ones Coinbase does not report. Absent stays absent; present must
    still be a number."""
    return None if value is None else _money(value, what)


def import_payload(conn, payload: dict) -> dict:
    items = payload.get("items") or []
    accounts = payload.get("accounts") or []
    txns_in = payload.get("transactions") or []
    if not accounts:
        raise ValueError("payload needs accounts")
    if len(txns_in) > MAX_ROWS:
        raise ValueError(f"too many transactions (max {MAX_ROWS})")

    # --- namespace enforcement, before a single row is written ---------
    # The prefix is DERIVED here, never read from the request.
    prefix = TXN_PREFIX
    acct_ids = [str(a["id"]) for a in accounts]
    for iid in (str(it["id"]) for it in items):
        if not iid.startswith(ITEM_PREFIX):
            raise ValueError(f"item id {iid!r} outside the coinbase namespace")
    for a in accounts:
        aid, a_item = str(a["id"]), str(a["item_id"])
        if not aid.startswith(ITEM_PREFIX):
            raise ValueError(
                f"account id {aid!r} outside the coinbase namespace")
        if not a_item.startswith(ITEM_PREFIX):
            raise ValueError(
                f"account {aid!r} item {a_item!r} outside the coinbase "
                "namespace")
    for t in txns_in:
        tid, t_acct = str(t["id"]), str(t["account_id"])
        if not tid.startswith(TXN_PREFIX):
            raise ValueError(
                f"transaction id {tid!r} outside the coinbase namespace")
        if t_acct not in acct_ids:
            raise ValueError(
                f"transaction {tid!r} names account {t_acct!r} not in "
                "this push")
    # An id can LOOK namespaced yet already belong to a live aggregator
    # (a stray plaid/simplefin account whose id starts with coinbase-).
    # Refuse to overwrite those — only coinbase/csv/manual are ours.
    foreign = conn.execute(
        """SELECT a.id, i.aggregator FROM accounts a
           JOIN items i ON i.id = a.item_id
           WHERE a.id = ANY(%s) AND i.aggregator NOT IN ('coinbase','csv',
                                                         'manual')""",
        (acct_ids,)).fetchall()
    if foreign:
        raise ValueError(
            "refusing to overwrite account "
            f"{foreign[0]['id']!r} owned by aggregator "
            f"{foreign[0]['aggregator']!r}")

    def counts():
        return {r["account_id"]: r["n"] for r in conn.execute(
            "SELECT account_id, count(*) n FROM transactions "
            "WHERE account_id = ANY(%s) AND removed=0 "
            "GROUP BY account_id", (acct_ids,)).fetchall()}

    # --- coerce every value the payload supplies, still before a write --
    # Building the rows up here means a bad amount, balance or date is a
    # ValueError with nothing written, rather than a database error
    # somewhere in the middle of the batch.
    acct_rows = [(str(a["item_id"]), base.Account(
        id=a["id"], name=a["name"], type=a["type"],
        subtype=a.get("subtype"), mask=a.get("mask"),
        balance_current=_money_or_none(
            a.get("balance_current"), f"account {a['id']} balance_current"),
        balance_available=_money_or_none(
            a.get("balance_available"),
            f"account {a['id']} balance_available"),
        currency=a.get("currency"), raw=a.get("raw") or {})) for a in accounts]
    txns = [base.Transaction(
        id=t["id"], account_id=t["account_id"],
        date=dt.date.fromisoformat(str(t["date"])[:10]),
        amount=_money(t.get("amount"), f"transaction {t['id']} amount"),
        name=t["name"], merchant_name=t.get("merchant_name"),
        pending=bool(t.get("pending")),
        category_primary=t.get("category_primary"),
        category_detailed=t.get("category_detailed"),
        raw=t.get("raw") or {}) for t in txns_in]

    before = counts()
    # One transaction for the whole push. The connection is autocommit,
    # so without this each row committed as it was written: anything
    # raising mid-batch (a value the checks above cannot see, a
    # constraint, a lost connection) left the account holding half a
    # restatement — some rows updated, the retirement pass never run —
    # while the caller got an error saying nothing was applied.
    with conn.transaction():
        for it in items:
            # Stamp the honest source. aggregator='coinbase' (not
            # 'csv') so the item's origin is visible everywhere the value is
            # load-bearing (namespace refusal, token-import guard).
            # status='archived' keeps a push-fed item out of the worker's
            # hourly pull loop and the stale-connection checks — freshness is
            # the script heartbeat's job (same doctrine as plan_csv). The
            # native CDP connector's items differ by carrying an access_token.
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, status,"
                " raw) VALUES (%s,'coinbase',%s,'archived','{}'::jsonb)"
                " ON CONFLICT (tenant_id, id) DO NOTHING",
                (it["id"], it.get("institution_name") or "Coinbase"))
        for item_id, account in acct_rows:
            base.upsert_accounts(conn, item_id, [account])

        base.upsert_transactions(conn, txns)

        # rows inside the pushed prefix that the source stopped reporting
        # are retired. The scope of "stopped reporting" depends on the
        # push —
        #   * full_replace: the payload IS the account's complete view;
        #     anything absent is retired (the community collector always
        #     sends this).
        # * otherwise: prune only the DATES each account actually
        #     restates — a sparse push (two anchors years apart) must not
        #     retire every row between them, which the old min..max window
        #     did. A date the payload reports is restated in full; dates it
        #     says nothing about keep their rows. An account with no pushed
        #     transactions — including the empty-transactions[] case that used
        #     to soft-delete ALL history — loses nothing.
        pushed = {t.id for t in txns}
        if payload.get("full_replace"):
            candidates = conn.execute(
                "SELECT id FROM transactions WHERE account_id = ANY(%s)"
                " AND id LIKE %s AND removed=0",
                (acct_ids, prefix + "%")).fetchall()
        else:
            date_sets: dict[str, set[dt.date]] = {}
            for t in txns:
                date_sets.setdefault(t.account_id, set()).add(t.date)
            candidates = []
            for aid, dates in date_sets.items():
                candidates += conn.execute(
                    "SELECT id FROM transactions WHERE account_id = %s"
                    " AND id LIKE %s AND removed=0 AND date = ANY(%s)",
                    (aid, prefix + "%", sorted(dates))).fetchall()
        stale = [r["id"] for r in candidates if r["id"] not in pushed]
        if stale:
            conn.execute(
                "UPDATE transactions SET removed=1 WHERE id = ANY(%s)",
                (stale,))

    # the collector ran and was understood — balances alone are a real
    # push too. Without this the Doctor never had a Coinbase heartbeat to
    # judge, so a collector whose keys had expired looked fine forever.
    from . import heartbeat
    heartbeat.stamp(conn, "coinbase", rows=len(txns), label="Coinbase")
    after = counts()
    bal = {r["id"]: r["balance_current"] for r in conn.execute(
        "SELECT id, balance_current FROM accounts WHERE id = ANY(%s)",
        (acct_ids,)).fetchall()}
    return {"ok": True, "pushed": len(txns), "stale_removed": len(stale),
            "accounts": {aid: {"before": before.get(aid, 0),
                               "after": after.get(aid, 0),
                               "balance": bal.get(aid)}
                         for aid in acct_ids}}
