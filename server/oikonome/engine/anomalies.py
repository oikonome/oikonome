"""Transaction anomaly watch for the daily email / Today page.

The budget engine answers "too much?"; bill health answers "on
schedule?"; drift answers "price creep?". This answers "is something
WRONG?":

- possible double charge: same merchant + same amount + same statement
  descriptor twice within DUP_WINDOW_DAYS (botched retry / double-swipe /
  fraud), posted rows only. $10 floor.
- first-ever merchant with a large charge: absent from the entire ledger
  history charging >= NEW_MERCHANT_MIN — the classic fraud signature.

Scans the budget's own spend rows, so transfers and card payments can't
false-positive. Freshness rule: only anomalies whose newest transaction is
from yesterday/today are reported — the same pair must not nag every
morning as the window slides.
"""

import datetime as dt

from . import budget, merchant_sql
from .compat import as_date

DUP_WINDOW_DAYS = 2
DUP_MIN = 10.0
NEW_MERCHANT_MIN = 200.0
COST_MIN = 1.00            # a fee or interest charge below this is not worth a line


def _money(v: float) -> str:
    s = f"${v:,.2f}"
    return s[:-3] if s.endswith(".00") else s   # $19.90 stays $19.90


def detect(conn, today: dt.date) -> list[str]:
    fresh = today - dt.timedelta(days=1)          # newest txn must be >= this
    since = today - dt.timedelta(days=DUP_WINDOW_DAYS + 1)
    # honor the user's excluded_accounts (same as month_status /
    # today paths), else an excluded high-churn card fires phantom
    # double-charge / big-purchase alerts.
    cfg = budget.load_config(conn)
    # parts=False: the questions here (charged twice? unusually large?)
    # are about the CHARGE the bank sent, so a hand-split row stays whole
    rows = budget._spend_rows(conn, since, today + dt.timedelta(days=1),
                              excluded=budget.excluded_account_ids(cfg),
                              parts=False)
    msgs: list[str] = []

    # possible double charges. Keyed on the raw statement descriptor as well
    # as merchant + amount: a merchant that stamps an order reference into
    # each charge ("AMAZON MKTPL*ORDER1") gives separate orders at a
    # common price point distinct descriptors, while a
    # re-run or double swipe repeats the descriptor verbatim. Pending rows
    # are skipped — their descriptors are generic, and a duplicate that
    # really happened is still caught once it posts.
    groups: dict[tuple, list] = {}
    for r in rows:
        if r.get("pending"):
            continue
        groups.setdefault(((r["payee"] or "").lower(), round(r["amount"], 2),
                           (r["name"] or "").strip().lower()),
                          []).append(r)
    for (_, amt, _), grp in sorted(groups.items(), key=lambda kv: -kv[0][1]):
        if amt < DUP_MIN or len(grp) < 2:
            continue
        dates = sorted(as_date(r["date"]) for r in grp)
        # The pair that matters is the one ending at the NEWEST charge. Judging
        # the whole group's first-to-last span instead lets an older twin —
        # inside the scan window but outside the duplicate window — hide a
        # fresh pair: three $40 rows on days 1, 3 and 4 span three days, yet
        # days 3 + 4 are a double charge and must be reported.
        recent = [d for d in dates
                  if d >= dates[-1] - dt.timedelta(days=DUP_WINDOW_DAYS)]
        if len(recent) >= 2 and dates[-1] >= fresh:
            days = " + ".join(f"{d:%m/%d}" for d in recent)
            msgs.append(f"Possible double charge: {grp[0]['payee']} "
                        f"{_money(amt)} ×{len(recent)} ({days})")

    # first-ever merchant, large amount
    seen: set[str] = set()
    for r in rows:
        merchant = (r["payee"] or "").strip()
        m = merchant.lower()
        # one alert per merchant per scan, counted by identity: a payee whose
        # rows key on an outlet and on a bare descriptor is still one payee
        ident = r.get("merchant_id") or m
        if (r["amount"] < NEW_MERCHANT_MIN or not m or ident in seen
                or as_date(r["date"]) < fresh):
            continue
        seen.add(ident)
        # prior = ANY charge strictly before this one (including 1-3 days ago
        # inside the scan window) — bounding it at `since` would leave a blind
        # window where a merchant's second charge fires a false "first-ever"
        # alert.
        #
        # "The same merchant" is the row's OWN identity, and it has to be
        # asked for in the same spelling the payee above is: the merchant the
        # row is filed under, else the identity key. Probing a different
        # expression — the aggregator's merchant name, where the payee is the
        # outlet or the bank's line — answers "no prior" for every row whose
        # two spellings differ, so a fuel pump, a renamed payee and a line
        # the aggregator named one-off all read as first-ever on a payee with
        # a hundred charges behind it. That is the one shape this alert must
        # never wear: it asks the person to go and check a charge that is
        # ordinary.
        prior = conn.execute(
            f"""SELECT 1 FROM transactions
               WHERE removed = 0 AND date < %s
                 AND (LOWER(BTRIM({merchant_sql.RAW_KEY_UNALIASED})) = %s
                      OR merchant_id = %s::uuid) LIMIT 1""",
            (r["date"], m, r.get("merchant_id"))).fetchone()
        if prior is None:
            # named as the ledger names it: an identity key is sometimes a
            # descriptor nobody has ever read, and an alert asking the person
            # to recognize a charge has to say what the charge looks like
            shown = (r.get("display_payee") or "").strip() or merchant
            msgs.append(f"First-ever charge from {shown}: {_money(r['amount'])} "
                        f"— verify you recognize it")

    # a fee or interest charge that just posted. Money lost to holding money
    # is the one spend a person can nearly always do something about — a
    # fee is often waived for the asking, interest stops when the statement
    # is paid — but only if they hear about it the day it lands, not in a
    # yearly total. Refunds are silent. A card's first interest in a while
    # names the gap, because "first since March" is the whole story.
    from . import reporting
    for r in reporting.cash_cost_rows(conn, since=fresh):
        if r["amount"] < COST_MIN or r["date"] < fresh:
            continue
        when = f"{r['date']:%m/%d}"
        if r["kind"] == "interest":
            prior = conn.execute(
                """SELECT MAX(date) AS d FROM transactions
                    WHERE removed = 0 AND account_id = %s AND amount > 0
                      AND date < %s
                      AND (COALESCE(category_detailed,'') = 'BANK_FEES_INTEREST_CHARGE'
                           OR LOWER(name) LIKE '%% interest charge%%'
                           OR LOWER(name) LIKE '%%purchase interest%%'
                           OR LOWER(name) LIKE '%%finance charge%%')""",
                (r["account_id"], r["date"])).fetchone()["d"]
            gap = (f"first since {as_date(prior):%b %Y}" if prior
                   else "first ever on this account")
            msgs.append(f"Interest charged: {r['account']} "
                        f"{_money(r['amount'])} on {when} — {gap}")
        else:
            msgs.append(f"Fee charged: {r['name']} {_money(r['amount'])} "
                        f"on {when} ({r['account']})")
    return msgs
