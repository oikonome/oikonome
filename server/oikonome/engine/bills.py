"""Ledger-driven recurring bills: detection, proposals, manual management.

The SQL follows compat.py's sqlite→Postgres pattern. The schema lives in
db/schema.sql; `ensure_schema` is a no-op kept for call compatibility.

Row format: `raw` carries exactly what budget._recurring_bills consumes —
{"dueOn", "lastDueOn", "recurrence": {...}} plus {"source", "bill_type"}.
Envelope bills have no occurrences/due dates: `amount` IS the monthly pool.

Everything detection wants to change goes through `bill_proposals`
(status=pending) until approved — EXCEPT amount drift, which auto-applies
and leaves an `auto` audit row.
"""

import datetime as dt
import hashlib
import re
import statistics

from .. import localtime
from . import budget, merchant_dedup
from .compat import as_date, as_dict, jsonb

PER_YEAR = {"DAILY": 365.0, "WEEKLY": 52.0, "MONTHLY": 12.0, "YEARLY": 1.0}

# supported detection cycles: (days, frequency, interval)
CYCLES = [(7, "WEEKLY", 1), (14, "WEEKLY", 2), (30, "MONTHLY", 1),
          (61, "MONTHLY", 2), (91, "MONTHLY", 3), (182, "MONTHLY", 6),
          (365, "YEARLY", 1)]

LOOKBACK_MONTHS = 48          # ledger window for detection


def DETECT_TOL(amount) -> float:
    """Detection's amount band — how far apart charges may sit and still
    read as one recurring series. Wider than budget.bill_tolerance (the
    band a KNOWN bill accepts a payment inside) on purpose: the flat $30
    floor is what lets a varied food series read as one merchant's
    refills rather than a bill, and detection asks "is this a bill?",
    not "is this the bill's payment?"."""
    return max(30.0, abs(float(amount or 0)) * 0.25)
MIN_HITS = 3                  # occurrences before proposing (short cycles)
MIN_HITS_LONG = 2             # cycles >= LONG_CYCLE days
LONG_CYCLE = 150
ENV_MIN_MONTHLY = 20.0        # ignore sub-$20/mo envelope noise
ENV_MAD_FRAC = 0.30           # monthly-total stability bound
DRIFT_MIN = 1.0               # $ change that triggers an amount update
PHASE_TOL = 2                 # days the due-date anchor may sit off the real
                              # draft phase before health reads 'misdated'
GRACE_DAYS = 60               # hand-edited amounts are drift-immune this long


def ensure_schema(conn) -> None:
    """No-op: DDL lives in db/schema.sql (applied by migrate)."""


def _slug(payee: str) -> str:
    return "rec:" + re.sub(r"[^a-z0-9]+", "-", (payee or "").lower()).strip("-")


def _sig(*parts) -> str:
    return "prop:" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _alias(frequency: str | None, interval: int) -> str | None:
    if not frequency:
        return None
    f = frequency.upper()
    if f == "ENVELOPE":
        return "ENVELOPE"
    base = {"DAILY": "DAY", "WEEKLY": "WEEK", "MONTHLY": "MONTH", "YEARLY": "YEAR"}[f]
    return f"EVERY_{base}" if interval == 1 else f"EVERY_X_{base}S"


def _monthly(amount: float, frequency: str | None, interval: int) -> float:
    if not frequency or frequency.upper() == "ENVELOPE":
        return amount
    return amount * PER_YEAR[frequency.upper()] / (interval or 1) / 12.0


# ---- canonical merchant ---------------------------------------------------

def _derive_merchant(conn, amount: float, *, match_tokens: set | None = None,
                     alias: str | None = None, min_hits: int = 2,
                     today: dt.date | None = None) -> str | None:
    """Derive a bill's canonical merchant match-key from the feed: the token
    INTERSECTION across the (amount-matched) recent transactions the bill
    currently matches — specific enough that a collider missing one token
    ('Oakmere' lacks 'water') is rejected, loose enough to absorb
    variants ('Oakmere Water' vs 'Oakmere Water District')."""
    today = today or localtime.household_day(conn)
    since = today - dt.timedelta(days=550)
    tol = DETECT_TOL(abs(amount))
    hits = []
    for r in conn.execute(
            # Raw descriptor on purpose, not the display name: the match key
            # this derives is compared against the bank's own text at match
            # time, so a canonical rename here would produce a key that
            # nothing in the feed contains.
            """SELECT COALESCE(merchant_name,'') m, COALESCE(name,'') n, amount
               FROM transactions WHERE removed=0 AND amount>0 AND date>=%s""",
            (since,)).fetchall():
        if alias is not None:
            ok = alias in f"{r['m']} {r['n']}".lower()
        elif match_tokens:
            ok = match_tokens <= budget._tokens(f"{r['m']} {r['n']}")
        else:
            ok = False
        if ok and abs(r["amount"] - abs(amount)) <= tol:
            hits.append(budget._tokens(r["m"] or r["n"]))    # clean merchant tokens
    hits = [h for h in hits if h]
    if len(hits) < min_hits:
        return None
    common = set.intersection(*hits)
    return " ".join(sorted(common)) or None


def backfill_merchants(conn, aliases: dict | None = None,
                       today: dt.date | None = None) -> int:
    """Populate/refresh recurring.merchant for every active bill from the
    feed. Each bill's stored merchant is its own finder, so it self-updates.
    Returns rows updated."""
    aliases = {k.lower(): v.lower() for k, v in (aliases or {}).items()}
    n = 0
    for r in conn.execute(
            "SELECT id, payee, amount, merchant, raw FROM bills "
            "WHERE active=1 AND amount<0").fetchall():
        payee = r["payee"] or ""
        # Envelope merchants are a deliberate user config (a monthly pool),
        # not an occurrence-amount finder — the derivation (which filters to
        # charges near the bill AMOUNT) can't reproduce them. Leave them.
        if as_dict(r["raw"]).get("bill_type") == "envelope":
            continue
        # Phrase merchants ('city water') and alternatives ('acme|acme co')
        # are deliberate manual pins the derivation can't reproduce — skip.
        if budget._merchant_phrase(r["merchant"]) or "|" in (r["merchant"] or ""):
            continue
        existing = budget._tokens(r["merchant"]) if r["merchant"] else None
        alias = aliases.get(payee.lower())
        # precise finders (an existing merchant key, or an alias) can seed off
        # a single charge; the loose nickname key needs >=2
        if existing:
            m = _derive_merchant(conn, r["amount"], match_tokens=existing,
                                 min_hits=1, today=today)
        elif alias:
            m = _derive_merchant(conn, r["amount"], alias=alias,
                                 min_hits=1, today=today)
        else:
            key = budget._key_token(payee)
            m = _derive_merchant(conn, r["amount"],
                                 match_tokens={key} if key else None, today=today)
        if m and m != r["merchant"]:
            conn.execute("UPDATE bills SET merchant=%s WHERE id=%s", (m, r["id"]))
            n += 1
    return n


# ---- manual management (dashboard + proposal apply) ----------------------


# Fee-shaped descriptor words: a processor's convenience fee, a landlord
# portal's service charge, a card surcharge — the small charge that rides
# with a payment and, unattached, reads as a $1 bill of its own.
FEE_WORDS = frozenset({"fee", "fees", "convenience", "conv", "process",
                       "processing", "service", "svc", "chg", "charge",
                       "surcharge", "portal"})
COMPANION_WINDOW_DAYS = 3
COMPANION_MAX_AMOUNT = 25.0      # a fee is small; above this it is its own bill


def companions_of(raw: dict | None) -> list[dict]:
    """The bill's companion charges — the fees that ride with a payment —
    normalised from `raw.companions`: each {"tokens": set, "label": str,
    "amount": float, "window_days": int}. Malformed entries are dropped,
    never raised on: the bill must still render."""
    out = []
    for c in (raw or {}).get("companions") or []:
        if not isinstance(c, dict):
            continue
        label = str(c.get("tokens") or c.get("label") or "").strip()
        toks = budget._tokens(label)
        try:
            amt = abs(float(c.get("amount") or 0))
        except (TypeError, ValueError):
            continue
        if not toks or not amt > 0:
            continue
        try:
            win = int(c.get("window_days", COMPANION_WINDOW_DAYS))
        except (TypeError, ValueError):
            win = COMPANION_WINDOW_DAYS
        out.append({"tokens": toks, "label": label, "amount": round(amt, 2),
                    "window_days": max(0, min(14, win))})
    return out


def fee_total(raw: dict | None) -> float:
    return round(sum(c["amount"] for c in companions_of(raw)), 2)


def companion_matches(c: dict, text_tokens: set, amount: float) -> bool:
    """Does a ledger row look like this companion — its words present, its
    amount within a fee-sized tolerance (50¢ or 25%)."""
    return (c["tokens"] <= text_tokens
            and abs(abs(amount) - c["amount"]) <= max(0.5, c["amount"] * 0.25))


def in_manual_grace(raw: dict, today: dt.date | None = None) -> bool:
    """True while a hand-set amount is protected from auto-drift (the user's
    number wins for GRACE_DAYS; after that the ledger median resumes)."""
    ts = raw.get("manual_edited_at")
    if not ts:
        return False
    # A raw dict names no household, so there is no zone to ask here: every
    # caller inside the engine passes the day its pass is running for, which
    # is the household's. The fallback is the last resort for a caller that
    # has neither.
    today = today or dt.date.today()
    try:
        return (today - dt.date.fromisoformat(str(ts)[:10])).days < GRACE_DAYS
    except ValueError:
        return False


_in_manual_grace = in_manual_grace   # alias kept for older call sites


def save_bill(conn, *, payee: str, amount: float, bill_type: str = "occurrence",
              frequency: str | None = None, interval: int = 1,
              next_due: str | dt.date | None = None, category: str | None = None,
              source: str = "manual", last_seen: str | dt.date | None = None,
              by_month_day: int | None = None, income: bool = False,
              merchant: str | None = None,
              manual_touch: bool | None = None,
              show_today: bool | None = None,
              match_category: bool | None = None,
              txn_category: str | None = None,
              companions: list[dict] | None = None,
              merchants: list[str] | None = None) -> str:
    """Insert or replace one bill. `amount` in positive dollars (sign applied
    here: bills negative, income positive).

    Raises ValueError on a non-positive amount (a $0 bill is invisible to
    every amount<0/amount>0 filter and gets corrupted by drift) and on a slug
    collision with a DIFFERENT payee (the upsert would silently delete the
    other bill).

    `show_today` pins the bill as a card on the Today page and in the daily
    email; `match_category` (envelope bills) also counts rows whose effective
    category equals the bill's `category` toward the pool — for pools like a
    hobby budget whose spend arrives from many merchants. Both are
    tri-state: None keeps whatever the bill already has (machine paths and
    partial edits must not strip a hand-set pin).

    `txn_category` is the TRANSACTION category the bill stamps on the rows
    it matches (see apply_txn_categories) — for a merchant that is not one
    thing: a store that sells both a membership and one-off purchases has
    dues and shopping under one name, and a merchant rule cannot say both.
    None keeps the current value; "" clears it; a name sets it.

    `companions` are the fees that ride with this bill's payment (see
    companions_of): a list replaces the set, None keeps it, [] clears it.
    The bill's `amount` stays the main charge; cap, health, Today and the
    email add the fees on top ("$91.60 incl. $1 fee")."""
    if not float(amount) > 0:
        raise ValueError("bill amount must be positive dollars")
    with conn.transaction():
        # Read-modify-write under the row lock: the new raw carries values
        # forward from the old one (lock_amount, anchors, companions), so two
        # concurrent saves without the lock would let the later writer revert
        # the earlier one's fields with no conflict signalled to either.
        other = conn.execute("SELECT payee, merchant, raw, amount FROM bills "
                             "WHERE id=%s FOR UPDATE",
                             (_slug(payee),)).fetchone()
        if other is not None and (other["payee"] or "") != payee:
            raise ValueError(f"name collides with existing bill '{other['payee']}'")
        prev_merchant = other["merchant"] if other is not None else None
        if merchant is not None:
            prev_merchant = merchant.strip().lower() or None
        # the upsert would wipe manual raw flags — carry lock_amount forward
        prev_raw = as_dict(other["raw"]) if other is not None else {}
        # both stamps below are CALENDAR days the household reads back on
        # its own pages (the grace window, the drift anchor), so they are
        # dated where the household lives, not where the container runs
        today = localtime.household_day(conn)
        amount = abs(float(amount)) * (1 if income else -1)
        interval = max(1, int(interval or 1))
        next_due_d = as_date(next_due)
        # No due date supplied → the bill KEEPS the one it has. An amount-only
        # edit (the attention queue's Accept) must not null due_on: that drops
        # the bill out of the monthly plan and the Today card, and nothing
        # ever recomputes a null due date.
        if next_due_d is None and prev_raw.get("dueOn"):
            next_due_d = as_date(prev_raw.get("dueOn"))
        if bill_type == "envelope":
            # `interval` is the pool's length in months — 1 (the classic
            # monthly pool) or 12 (an annual pool for lumpy yearly costs). `amount`
            # is the pool for that PERIOD, so the plan/forecast share is the pool
            # spread over its months; at interval=1 that is the amount itself.
            env_months = 12 if int(interval or 1) >= 12 else 1
            frequency, next_due_d = "ENVELOPE", None
            monthly = amount / env_months
            recurrence: dict = {}
        elif not frequency or frequency.upper() in ("", "ONE_TIME"):
            frequency, monthly = None, amount
            recurrence = {}
        else:
            monthly = _monthly(amount, frequency, interval)
            recurrence = {"frequency": (frequency or "MONTHLY").upper(),
                          "interval": interval}
            if recurrence["frequency"] == "MONTHLY":
                # no new due date → keep the anchor the bill already has (an
                # amount-only edit must not re-anchor a bill to the 1st and
                # flip it to misdated)
                bmd = (prev_raw.get("recurrence") or {}).get("byMonthDay")
                # raw can arrive verbatim from a restore ZIP — a malformed
                # anchor is treated as absent, never a crash
                prev_day = (bmd[0] if isinstance(bmd, list) and bmd
                            and isinstance(bmd[0], int) else None)
                day = by_month_day or (next_due_d.day if next_due_d
                                       else prev_day or 1)
                recurrence["byMonthDay"] = [day]
            elif recurrence["frequency"] == "YEARLY" and next_due_d:
                recurrence["byMonth"] = [next_due_d.month]
        raw = {"source": source, "bill_type": bill_type,
               "dueOn": next_due_d.isoformat() if next_due_d else None,
               **({"envelope_months": env_months} if bill_type == "envelope" else {}),
               # lastDueOn=None floors a brand-new series at its first due date;
               # detected series pass their last observed hit
               "lastDueOn": (as_date(last_seen).isoformat() if last_seen else None),
               "recurrence": recurrence}
        if prev_raw.get("lock_amount"):
            raw["lock_amount"] = True
        # when this amount became the bill's amount — the drift check only
        # counts occurrences after it (see _maintain_bills). For a detected
        # series that is the last hit the amount was read from (last_seen);
        # for a hand-entered bill, today.
        prev_amt = abs(other["amount"] or 0) if other is not None else None
        if prev_amt is None or abs(prev_amt - abs(amount or 0)) > 0.005 \
                or not prev_raw.get("amount_set_on"):
            raw["amount_set_on"] = (as_date(last_seen).isoformat() if last_seen
                                    else today.isoformat())
        else:
            raw["amount_set_on"] = prev_raw["amount_set_on"]
        for flag, val in (("show_today", show_today),
                          ("match_category", match_category)):
            if val if val is not None else prev_raw.get(flag):
                raw[flag] = True
        tc = (prev_raw.get("txn_category") if txn_category is None
              else txn_category.strip())
        if tc:
            raw["txn_category"] = tc
        comps = (prev_raw.get("companions") if companions is None else companions)
        comps = [{"tokens": c["label"], "amount": c["amount"],
                  "window_days": c["window_days"]}
                 for c in companions_of({"companions": comps})]
        if comps:
            raw["companions"] = comps
        # hand edits win over drift for GRACE_DAYS; machine paths carry the
        # previous stamp forward untouched
        if manual_touch or (manual_touch is None and source == "manual"):
            raw["manual_edited_at"] = today.isoformat()
        elif prev_raw.get("manual_edited_at"):
            raw["manual_edited_at"] = prev_raw["manual_edited_at"]
        # the bill's identity — the merchants it pays, by display name.
        # Given: the person's list. Absent: carried forward; and a bill
        # that has none yet is bootstrapped from the ledger, so a bill
        # made from a merchant page matches by identity from its first
        # day and text is the rule only while the ledger shows nothing.
        if merchants is not None:
            ident = _identity(conn, merchants)
            # the person's own list, empty included: the nightly pass may
            # offer additions but never silently refills it from the ledger
            raw["merchant_pinned"] = True
        elif "merchant_refs" in prev_raw or "merchant_names" in prev_raw:
            ident = _identity(conn, budget.bill_refs(prev_raw))
            if prev_raw.get("merchant_pinned"):
                raw["merchant_pinned"] = True
        else:
            found = budget.bill_displays(
                conn, [{"payee": payee, "merchant": prev_merchant}]
            ).get(payee, frozenset())
            ident = _identity(conn, attachable(conn, payee, prev_merchant, found),
                              backed_only=True)
        if (merchants is None and not ident["merchant_refs"]
                and prev_raw.get("merchant_offered")):
            # the nightly is holding this bill at an empty identity while an
            # offer waits on the person (reconcile_bill_merchants). An edit
            # that settles neither must not drop the mark — the upsert
            # rebuilds raw, and losing it would put the bill back on text
            raw["merchant_offered"] = True
        raw.update(ident)
        conn.execute(
            """INSERT INTO bills (id, type, payee, amount, frequency,
                   monthly_amount, due_on, category, account_id, is_completed,
                   active, synced_at, raw, merchant)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,0,1,now(),%s,%s)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   type=EXCLUDED.type, payee=EXCLUDED.payee,
                   amount=EXCLUDED.amount, frequency=EXCLUDED.frequency,
                   monthly_amount=EXCLUDED.monthly_amount, due_on=EXCLUDED.due_on,
                   category=EXCLUDED.category, is_completed=0, active=1,
                   synced_at=now(), raw=EXCLUDED.raw, merchant=EXCLUDED.merchant""",
            (_slug(payee), "INCOME" if income else "BILL", payee, amount,
             _alias(frequency, interval) if bill_type != "envelope" else "ENVELOPE",
             monthly, next_due_d, category, jsonb(raw), prev_merchant))
        return _slug(payee)


def rename_bill(conn, old: str, new: str) -> int:
    """Rename in place (id is the payee slug). Matching is driven by
    `merchant` (preserved), so a rename never changes what a bill matches.
    Returns 0 if the target name/slug is taken.

    Everything that points at the bill by NAME moves with it, in one
    transaction: a rename either takes all of them or none. Renaming is a
    display change, so nothing that was actionable before it may stop being
    actionable after it."""
    if conn.execute("SELECT 1 FROM bills WHERE (payee=%s OR id=%s) AND payee!=%s",
                    (new, _slug(new), old)).fetchone():
        return 0
    with conn.transaction():
        # Lock order matches apply_proposal — its proposal row first, then
        # the bill (the attach branch locks its host): a rename taking the
        # bill first and then touching the same pending proposals could
        # deadlock against an approval running the other way round.
        conn.execute(
            "SELECT id FROM bill_proposals WHERE status='pending' AND "
            "(payee=%s OR (kind IN ('attach','merchant') "
            "AND evidence->>'bill_payee'=%s)) FOR UPDATE", (old, old))
        n = conn.execute(
            "UPDATE bills SET id=%s, payee=%s, synced_at=now() WHERE payee=%s",
            (_slug(new), new, old)).rowcount
        if n:
            # the bill's category marks follow the bill (its id is the slug)
            conn.execute(
                "UPDATE manual_categories SET bill_id=%s WHERE bill_id=%s",
                (_slug(new), _slug(old)))
            # Proposals name their bill by PAYEE STRING — there is no
            # bill_id FK — so a rename that leaves them behind strands
            # them: _apply_approved looks the old name up, finds nothing,
            # and every Approve returns "bill '<old>' no longer exists"
            # forever, with no way to act on a legitimate drift or
            # cadence correction that happened to be pending. Only rows
            # still awaiting a decision move; a decided proposal is
            # history and keeps the name it was decided under.
            conn.execute("UPDATE bill_proposals SET payee=%s "
                         "WHERE payee=%s AND status='pending'", (new, old))
            # kind='attach' (a fee) and kind='merchant' (a merchant to
            # join the identity) are the shapes whose `payee` is the
            # thing's OWN label: the bill they would join is named in the
            # evidence, and only that moves.
            conn.execute(
                "UPDATE bill_proposals SET evidence = evidence || %s::jsonb "
                "WHERE kind IN ('attach','merchant') AND status='pending' "
                "AND evidence->>'bill_payee' = %s",
                (jsonb({"bill_payee": new}), old))
            # The budget config keys two things by PAYEE STRING — the
            # budget-disabled list and the per-occurrence cap — so a rename
            # that commits before they move leaves a window in which the
            # bill's name and the config's name disagree. Everything that
            # reads that pair during the window reads it wrong: the nightly
            # pass watches a paused bill (drift corrections, a standing
            # "remove?" proposal on a bill one tap disabled), and the budget
            # counts a bill the person excluded from it. Moving them under
            # the rename's own transaction means no reader can ever see the
            # halves apart. The settings lock is taken LAST, after the
            # bill's own rows, so the order is the same on every path that
            # holds both.
            with budget.config_txn(conn) as cfg:
                dis = cfg.get("disabled_bills") or []
                if old in dis:
                    cfg["disabled_bills"] = [new if p == old else p
                                             for p in dis]
                caps = cfg.get("occurrence_caps") or {}
                if old in caps:
                    caps[new] = caps.pop(old)
                    cfg["occurrence_caps"] = caps
    return n


def _bill_row_matcher(payee, merchant, amount, raw, ids=None):
    """(matches(text, amt, merchant_id=None) -> bool) for one bill — the
    ledger's own rule, the ⟳ pill's and the history page's ✓: the row's
    merchant is one of the bill's (`ids`, see budget.bill_merchant_ids;
    its text tokens only for a bill with no identity) AND the amount is
    inside the bill's tolerance (an envelope takes any spend row on its
    merchant; an income series takes inflows)."""
    key = budget._key_token(payee)
    btype = raw.get("bill_type", "occurrence")
    income = (amount or 0) > 0
    ref = abs(amount or 0)
    tol = budget.bill_tolerance(ref)
    match = budget.merchant_matcher(merchant, key, ids)

    def ok(text: str, amt: float, merchant_id: str | None = None) -> bool:
        if not match(text, None, merchant_id):
            return False
        if btype == "envelope":
            return amt > 0
        return ((amt < 0 if income else amt > 0)
                and abs(abs(amt) - ref) <= tol)
    return ok


def apply_txn_categories(conn, *, bill_id: str | None = None,
                         days: int | None = None) -> dict:
    """Stamp each bill's transaction category on the rows it matches, and
    take it back off rows it no longer should be on.

    The write is a category_override recorded in manual_categories WITH the
    bill's id, so: a person's own correction (bill_id NULL) is never
    touched — enforced IN THE WRITE, not only in the read that precedes it,
    because a correction that lands between this pass's scan and its write
    must still win (the scan is a snapshot; the write is the truth); when
    the bill's category changes, is cleared, or the bill is archived, its
    marks are reverted (override → NULL, the row falls back to the merchant
    rule / aggregator); and re-running is idempotent. Runs on save (that
    bill), after every sync (recent rows — `days`) and in the nightly pass
    (everything), so a new charge is categorized within the hour like
    everything else.

    Only ever the bill's own HOUSEHOLD rows: a merchant that is several
    things is the whole reason this exists, so the merchant rule is
    deliberately NOT taught, and business-entity money — which bills do
    not budget — is never restamped into a Schedule C bucket. When two
    bills both match a charge, the closer amount wins, deterministically,
    so a row's category cannot flip between nightly runs on SQL row order.
    `bill_id` narrows the pass to one bill; None does them all."""
    stats = {"applied": 0, "reverted": 0, "bills": 0}
    q = "SELECT id, payee, merchant, amount, active, raw FROM bills"
    args: list = []
    if bill_id:
        q += " WHERE id=%s"; args.append(bill_id)
    q += " ORDER BY id"
    bills = conn.execute(q, args).fetchall()
    keep: dict[str, str] = {}
    for b in bills:
        raw = as_dict(b["raw"])
        tc = (raw.get("txn_category") or "").strip()
        if b["active"] and tc:
            keep[b["id"]] = tc
    with conn.transaction():
        # 1. revert marks that no longer belong: bill gone/archived/cleared,
        #    or the bill's category changed (the mark carries the old one)
        if bill_id:
            stale = conn.execute(
                "SELECT transaction_id, category FROM manual_categories "
                "WHERE bill_id=%s", (bill_id,)).fetchall()
        else:
            stale = conn.execute(
                "SELECT transaction_id, category, bill_id FROM manual_categories "
                "WHERE bill_id IS NOT NULL").fetchall()
        for m in stale:
            bid = m["bill_id"] if not bill_id else bill_id
            if keep.get(bid) == m["category"]:
                continue
            # re-check ownership in the write: a no-op if a person claimed
            # the row in the meantime (their write nulls bill_id) or another
            # bill's pass restamped it (bill_id moved) — only THIS bill's
            # mark, as scanned, is reverted
            n = conn.execute(
                "DELETE FROM manual_categories WHERE transaction_id=%s "
                "AND bill_id=%s", (m["transaction_id"], bid)).rowcount
            if n:
                # only the BILL's own override is taken back: an item match
                # or a person's pin that landed on the row since stays
                conn.execute("UPDATE transactions SET category_override=NULL, "
                             "override_source=NULL WHERE id=%s "
                             "AND override_source IS NOT DISTINCT FROM 'bill'",
                             (m["transaction_id"],))
                stats["reverted"] += 1
        if not keep:
            return stats
        # 2. stamp the rows each kept bill matches. One ledger scan per
        #    pass, all bills tested against each row (there are tens of
        #    bills and tens of thousands of rows, not the other way round).
        matchers = []
        ids = budget.bill_merchant_ids(
            conn, [b for b in bills if b["id"] in keep])
        for b in bills:
            if b["id"] in keep:
                braw = as_dict(b["raw"])
                matchers.append((b["id"], keep[b["id"]], abs(b["amount"] or 0),
                                 _bill_row_matcher(b["payee"], b["merchant"],
                                                   b["amount"], braw,
                                                   ids.get(b["payee"] or ""))))
                # the fees that ride with this bill's payment are the
                # bill's too — the $1 convenience charge beside the water
                # bill is water, not "general services"
                for c in companions_of(braw):
                    matchers.append((
                        b["id"], keep[b["id"]], c["amount"],
                        (lambda c: lambda text, amt, merchant_id=None:
                            amt > 0 and companion_matches(
                                c, budget._tokens(text), amt))(c)))
        stats["bills"] = len(matchers)
        since_sql, since_args = "", []
        if days is not None:
            since_sql = " AND t.date >= %s"
            since_args = [localtime.household_day(conn)
                          - dt.timedelta(days=days)]
        rows = conn.execute(
            f"""SELECT t.id, t.amount, t.category_override, t.override_source,
                      {budget.MATCH_PAYEE_SQL} AS raw_payee, t.name,
                      t.merchant_id::text AS merchant_id,
                      m.bill_id AS mark_bill, m.category AS mark_cat,
                      (m.transaction_id IS NOT NULL AND m.bill_id IS NULL) AS user_set
                 FROM transactions t
                 LEFT JOIN manual_categories m ON m.transaction_id = t.id
                WHERE t.removed = 0{budget.PERSONAL_ONLY_SQL}{since_sql}""",
            since_args).fetchall()
        from . import categories as _cats
        for r in rows:
            if r["user_set"]:
                continue                       # a person's answer wins
            # nor does a bill's stamp overwrite an item match — the store's
            # own record of what was bought outranks a bill's blanket
            # category (its "Unmatched" is no record and yields)
            if not _cats.override_outranks(r["override_source"], "bill",
                                           r["category_override"]):
                continue
            text = budget.match_text(r["raw_payee"], r["name"])
            amt = r["amount"] or 0
            hits = [(abs(abs(amt) - ref), bid, cat)
                    for bid, cat, ref, ok in matchers
                    if ok(text, amt, r["merchant_id"])]
            if not hits:
                continue
            _, bid, cat = min(hits)            # closest amount, then id
            if r["mark_bill"] == bid and r["mark_cat"] == cat \
                    and r["category_override"] == cat:
                continue                       # already right
            # the write re-checks ownership: a row a person claimed since
            # the scan (bill_id NULL) is left exactly as they set it
            n = conn.execute(
                """INSERT INTO manual_categories
                       (transaction_id, category, set_at, bill_id)
                   VALUES (%s,%s,now(),%s)
                   ON CONFLICT (tenant_id, transaction_id) DO UPDATE SET
                       category=EXCLUDED.category, set_at=now(),
                       bill_id=EXCLUDED.bill_id
                   WHERE manual_categories.bill_id IS NOT NULL""",
                (r["id"], cat, bid)).rowcount
            if n:
                conn.execute(
                    """UPDATE transactions SET category_override=%s,
                                               override_source='bill'
                        WHERE id=%s AND EXISTS (SELECT 1 FROM manual_categories m
                                                WHERE m.transaction_id=%s
                                                  AND m.bill_id=%s)""",
                    (cat, r["id"], r["id"], bid))
                stats["applied"] += 1
    return stats


def archive_bill(conn, payee: str) -> int:
    """Soft-remove: the row survives with active=0 (history), restorable."""
    return conn.execute(
        "UPDATE bills SET active=0, synced_at=now() WHERE payee=%s",
        (payee,)).rowcount


def restore_bill(conn, payee: str) -> int:
    return conn.execute(
        "UPDATE bills SET active=1, synced_at=now() WHERE payee=%s",
        (payee,)).rowcount


def delete_bill(conn, payee: str) -> int:
    """Hard delete — only reachable from the archived list."""
    return conn.execute("DELETE FROM bills WHERE payee = %s",
                        (payee,)).rowcount


def _merchant_names(names) -> list[str]:
    """A bill's attached merchant names, normalised: strings only, trimmed,
    deduplicated, bounded, sorted — the same list whichever door wrote it."""
    out: list[str] = []
    seen: set[str] = set()
    for n in names or []:
        if not isinstance(n, str):
            continue
        n = n.strip()[:200]
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return sorted(out[:20])


def _identity(conn, items, *, backed_only: bool = False) -> dict:
    """The two raw keys a bill's identity is stored under, from names or
    refs: `merchant_refs` (id + name — the id is the key that follows a
    rename or merge, the name the fallback across instances) and
    `merchant_names` (the names alone, what the clients read and edit).
    Names are resolved to live merchants here (budget.resolve_refs)."""
    refs = []
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("name"), str):
            refs.append({"id": (str(it["id"]) if it.get("id") else None),
                         "name": it["name"].strip()[:200]})
        elif isinstance(it, str) and it.strip():
            refs.append({"id": None, "name": it.strip()[:200]})
    refs = [r for r in refs if r["name"]][:20]
    refs = budget.resolve_refs(conn, refs)
    if backed_only:
        # discovery names a display; only a display that IS a merchant row
        # can be an identity (a raw descriptor no merchant claims is not)
        refs = [r for r in refs if r["id"]]
    return {"merchant_refs": refs,
            "merchant_names": _merchant_names(r["name"] for r in refs)}


def _words(text: str | None) -> set[str]:
    """Every alphanumeric run of a name, lowercased — the words a person
    reads, short ones included."""
    return set(re.findall(r"[a-z0-9]+",
                          budget.join_apostrophes((text or "").lower())))


def attachable(conn, payee: str, merchant: str | None, names,
               *, offer: bool = False) -> list[str]:
    """Which of the display names the ledger offered a bill (budget.bill_displays)
    may become its identity. Discovery claims a name when the bill's words
    appear in a merchant key under it, which is also how a brand bill is
    offered its fuel arm and a one-word bill its longer siblings ("Google"
    → Google Store, "Oakmere" → Oakmere Health). Once attached, identity
    takes EVERY row of that merchant, so the offer must be tighter than
    the claim:

    * a name that is a CHILD (an outlet, `parent_id`) is the bill's only
      when the bill names what makes it a child (the child's tokens beyond
      its parent's — gas, fuel, carwash);
    * when the ledger offers exactly ONE name the bill's words are not
      ambiguous and it stands (a rename, a merge, Plaid's canonical name:
      "Northwind Insurance" for a "Northwind" bill) — UNLESS a merchant
      named by the bill's own words exists at all, however long ago it was
      last seen. Discovery looks back a fixed window, so a "Google" bill
      whose plain Google rows aged out is offered Google Store alone, and
      a lone sibling is not a rename: it is the shortening's absence. Such
      a name is never taken silently; with `offer=True` it is returned so
      the nightly can put it to the person instead;
    * when it offers several, the bill's words fit more than one merchant,
      and only a name whose own tokens are all among the bill's stands
      ("Google" among Google, Google Store, Google One) — a shortening,
      never a longer sibling.

    Names that are no merchant row at all are dropped: a raw descriptor
    no merchant claims cannot be an identity."""
    names = sorted({n for n in names if n})
    if not names:
        return []
    # whole words of any length, not the matcher's four-letter tokens: the
    # word that makes "Costco Gas" a child ("gas") or "Google One" a
    # sibling ("one") is exactly the short one the matcher ignores
    bill_toks = _words(merchant) | _words(payee)
    rows = {r["name"]: r for r in conn.execute(
        """SELECT m.name, p.name AS parent
             FROM merchants m LEFT JOIN merchants p ON p.id = m.parent_id
            WHERE m.merged_into IS NULL AND m.name = ANY(%s)""",
        (names,)).fetchall()}
    keep = []
    for n in names:
        r = rows.get(n)
        if r is None:
            continue
        if r["parent"]:
            own = _words(n) - _words(r["parent"])
            if own and not (own & bill_toks):
                continue
        keep.append(n)
    # a bill that names the child is the child's, not also its parent's:
    # a "Costco Gas" bill takes the pump, never the warehouse beside it
    parents = {rows[n]["parent"] for n in keep if rows[n]["parent"]}
    keep = [n for n in keep if n not in parents]
    shortenings = [n for n in keep if _words(n) and _words(n) <= bill_toks]
    if len(keep) != 1:
        return shortenings
    if shortenings:
        return shortenings
    # the lone name is longer than the bill's words: a rename, unless the
    # ledger knows a merchant the bill's words name exactly (any age)
    named = conn.execute(
        f"""SELECT 1 AS one FROM merchants
            WHERE merged_into IS NULL AND name <> ALL(%s)
              AND regexp_split_to_array(
                      {budget.apostrophes_joined_sql("lower(name)")},
                      '[^a-z0-9]+') <@ %s::text[]
              AND lower(name) ~ '[a-z0-9]'
            LIMIT 1""", (keep, sorted(bill_toks))).fetchone()
    if named is None:
        return keep
    return keep if offer else []


# The bootstrap decides on a snapshot read without a lock, and a person
# may pin a list (save_bill: FOR UPDATE, `merchant_pinned`) while the loop
# is still working through the other bills. So the two facts it decided on
# — nobody has pinned this bill, it still has no identity — are re-checked
# by the UPDATE itself; a stale snapshot writes nothing rather than
# refilling a list its owner had just emptied. jsonb comparison, not a
# boolean cast: `raw` can arrive verbatim from a restore ZIP, and a
# non-boolean there must skip the write, not raise.
_UNCLAIMED = (" WHERE id=%s"
              " AND COALESCE(raw->'merchant_pinned', 'false'::jsonb) = 'false'::jsonb"
              " AND COALESCE(raw->'merchant_refs', '[]'::jsonb) = '[]'::jsonb"
              " AND COALESCE(raw->'merchant_names', '[]'::jsonb) = '[]'::jsonb")


def reconcile_bill_merchants(conn, today: dt.date | None = None) -> dict:
    """Nightly: every active bill has an identity, and gains none silently.

    A bill with no identity — no `merchant_names` key (made before bills
    carried one), or an empty one (made before its charges existed) — is
    bootstrapped from the ledger: the merchants its words name on the
    ledger's own authority (budget.bill_displays). Not a list a PERSON
    wrote (`merchant_pinned`, set by the bill form), empty included: that
    is their answer, and the ledger may only offer additions to it. A bill
    that has its identity is only OFFERED a name the ledger newly files
    its charges under (the aggregator renamed the payee, the person merged
    two spellings): an attach proposal, approve-gated like every other
    change to a bill, so a rename never silently breaks the bill and
    never silently widens it. A rejected offer is not made again.

    When the ledger's only offer is one the bill may not take silently (a
    lone longer sibling, see attachable), the bill is not bootstrapped at
    all: it is OFFERED that name and marked `merchant_offered`, which
    gives it an EMPTY identity meanwhile (budget.bill_merchant_ids) rather
    than the text fallback. Text is the rule only for a bill the ledger
    shows nothing for; here it showed something, and matching by letters
    would hand the bill exactly the sibling's rows the offer is asking
    about — a "Google" bill paid by every Google Store charge, which is
    the outcome the identity is for. The mark is dropped the moment the
    bill is bootstrapped or a list is pinned."""
    today = today or localtime.household_day(conn)
    stats = {"bootstrapped": 0, "proposed_merchant": 0}
    rows = conn.execute(
        "SELECT id, payee, merchant, amount, raw FROM bills WHERE active = 1"
    ).fetchall()
    if not rows:
        return stats
    found = budget.bill_displays(conn, rows, today)
    stats["refreshed"] = 0
    for r in rows:
        raw = as_dict(r["raw"])
        payee = r["payee"] or ""
        offered = found.get(payee, frozenset())
        names = set(attachable(conn, payee, r["merchant"], offered))
        refs = budget.bill_refs(raw)
        # no identity yet — made before bills carried one, or made before
        # its charges existed and bootstrapped to nothing — takes the
        # ledger's word silently; an identity is OFFERED additions only
        # once it has one, or a bill matching by text would be offered the
        # very merchant its text already claims
        if not refs and not raw.get("merchant_pinned"):
            # nothing may be taken, but something was offered: put it to the
            # person and hold the bill at an empty identity until they
            # answer (see the docstring) — never text, which would take the
            # offered rows without asking
            wider = set() if names else set(
                attachable(conn, payee, r["merchant"], offered, offer=True))
            if wider:
                if not raw.get("merchant_offered"):
                    conn.execute(
                        "UPDATE bills SET raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb" + _UNCLAIMED,
                        (jsonb({"merchant_offered": True}), r["id"]))
                for name in sorted(wider):
                    if _insert_proposal(
                            conn, pid=_sig("merchant", payee, name), kind="merchant",
                            payee=name, amount=abs(r["amount"] or 0),
                            evidence={"bill_payee": payee, "merchant_name": name}):
                        stats["proposed_merchant"] += 1
                continue
            if names or ("merchant_refs" not in raw and "merchant_names" not in raw):
                # the guard, not the snapshot, decides whether this counts
                stats["bootstrapped"] += conn.execute(
                    "UPDATE bills SET raw = "
                    "(COALESCE(raw, '{}'::jsonb) - 'merchant_offered') "
                    "|| %s::jsonb"
                    + _UNCLAIMED,
                    (jsonb(_identity(conn, sorted(names), backed_only=True)),
                     r["id"])).rowcount
            continue
        # bookkeeping, not a change of identity: an id whose merchant was
        # renamed or merged shows its current name; a name that arrived
        # without an id (mirror, restore) gains the id of the merchant it
        # names here
        ident = _identity(conn, refs)
        if ident["merchant_refs"] != refs:
            # same unlocked snapshot as the bootstrap: write only while the
            # refs are still the ones that were resolved, so a save landing
            # mid-loop is not reverted to the list it replaced
            stats["refreshed"] += conn.execute(
                "UPDATE bills SET raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb WHERE id=%s"
                " AND raw->'merchant_refs' IS NOT DISTINCT FROM %s::jsonb",
                (jsonb(ident), r["id"],
                 jsonb(raw["merchant_refs"]) if "merchant_refs" in raw
                 else None)).rowcount
        have = set(ident["merchant_names"])
        # a lone longer sibling is never taken silently, but it may be
        # the right answer — the person decides
        wider = set(attachable(conn, payee, r["merchant"], offered, offer=True))
        for name in sorted((names | wider) - have):
            if _insert_proposal(
                    conn, pid=_sig("merchant", payee, name), kind="merchant",
                    payee=name, amount=abs(r["amount"] or 0),
                    evidence={"bill_payee": payee, "merchant_name": name}):
                stats["proposed_merchant"] += 1
    return stats


def get_bill(conn, payee: str):
    return conn.execute("SELECT * FROM bills WHERE payee = %s",
                        (payee,)).fetchone()


# ---- proposals ----------------------------------------------------------


def pending_proposals(conn) -> list[dict]:
    return conn.execute(
        """SELECT * FROM bill_proposals WHERE status = 'pending'
           ORDER BY CASE kind WHEN 'add' THEN 0 WHEN 'remove' THEN 1 ELSE 2 END,
                    ABS(amount) DESC""").fetchall()


def merchant_offer_summary(conn, p) -> str:
    """What approving a merchant proposal would change, in numbers.

    The row reads "<merchant> — add to bill <bill>", and a bill is often
    named like the merchant it pays, so the names alone cannot say what
    is at stake: how many charges would start counting toward the bill,
    and which merchants it counts today."""
    ev = as_dict(p["evidence"])
    name = ev.get("merchant_name") or p["payee"]
    row = conn.execute(
        "SELECT count(*) AS n, max(t.date) AS last FROM transactions t "
        "JOIN merchants m ON m.id = t.merchant_id "
        "WHERE m.name = %s AND t.amount > 0", (name,)).fetchone()
    n = row["n"] if row else 0
    bits = [f"{n} charge{'' if n == 1 else 's'} under {name}"
            + (f", last {as_date(row['last']):%m/%d/%y}" if n and row["last"] else "")]
    bill = conn.execute("SELECT raw FROM bills WHERE payee = %s",
                        (ev.get("bill_payee") or "",)).fetchone()
    have = (as_dict(bill["raw"]).get("merchant_names") or []) if bill else []
    if have:
        bits.append("the bill counts " + ", ".join(have) + " today")
    return " · ".join(bits)


def recent_auto_changes(conn, days: int = 1) -> list[dict]:
    """Auto-applied amount drifts for the email digest and the Today alert.

    CONDITION-AWARE. Amount drift is applied without asking, so this
    notice is the only word the user gets that their plan moved — and it
    must not keep repeating after they have dealt with it, merely because
    the window has not aged out.

    So a drift stops being news once the user has been to the bill and
    decided, by either of the two marks a decision leaves:

    1. The amount no longer matches what the drift wrote — they changed it
       to something else. Comparing the amount rather than `synced_at` is
       deliberate: the auto-apply writes `synced_at` itself, so "changed
       since decided_at" cannot tell the drift's own write apart from a
       human's, while the amount can.
    2. `raw.manual_edited_at` is on or after the drift's day — they hand
       edited it, even if they landed on exactly the drifted figure, which
       is the common case when the alert is what sent them there. Only
       hand edits set that stamp; machine paths carry the old one forward.

    (2) cannot false-positive on a fresh drift: a hand-edited amount is
    drift-immune for GRACE_DAYS, so a drift can never be applied to a bill
    that was edited the same day.

    A bill since deleted or deactivated also drops out: an even more
    definitive way of having dealt with it.
    """
    return conn.execute(
        """SELECT p.* FROM bill_proposals p
           WHERE p.status = 'auto'
             AND p.decided_at >= now() - make_interval(days => %s)
             AND EXISTS (SELECT 1 FROM bills b
                          WHERE b.payee = p.payee AND b.active = 1
                            AND round(abs(b.amount)::numeric, 2)
                                = round(p.amount::numeric, 2)
                            AND COALESCE(b.raw ->> 'manual_edited_at', '')
                                < p.decided_at::date::text)
           ORDER BY p.decided_at DESC""", (days,)).fetchall()


def _insert_proposal(conn, *, pid, kind, payee, amount, bill_type="occurrence",
                     frequency=None, interval=1, next_due=None, evidence=None,
                     status="pending") -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    cur = conn.execute(
        """INSERT INTO bill_proposals
               (id, kind, bill_type, payee, amount, frequency, interval, next_due,
                evidence, status, created_at, decided_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (pid, kind, bill_type, payee, amount, frequency, interval,
         as_date(next_due), jsonb(evidence or {}), status, now,
         now if status == "auto" else None))
    return cur.rowcount > 0


# Plaid stream frequency → our (frequency, interval). SEMI_MONTHLY has no
# shape here (twice a month is not "every N months") and UNKNOWN says nothing
_PLAID_FREQ = {"WEEKLY": ("WEEKLY", 1), "BIWEEKLY": ("WEEKLY", 2),
               "MONTHLY": ("MONTHLY", 1), "ANNUALLY": ("YEARLY", 1)}


def propose_from_plaid_streams(conn, streams: list[dict],
                               today: dt.date | None = None) -> dict:
    """Plaid's recurring streams as a CROSS-CHECK for detection: a mature,
    active stream whose merchant no active bill covers — and no pending
    proposal of ours already names — becomes a pending proposal with the
    evidence saying it came from Plaid (stream id, status, dates, how many
    charges Plaid saw). Approve/reject like any other; never auto-applied,
    because Plaid's amount is an average and its merchant grouping is its
    own. Idempotent per stream (the proposal id is the stream id)."""
    today = today or localtime.household_day(conn)
    stats = {"streams": len(streams), "proposed": 0, "covered": 0, "skipped": 0}
    bills = conn.execute(
        "SELECT payee, merchant, amount, raw FROM bills WHERE active = 1").fetchall()
    matchers = [(budget.merchant_matcher(b["merchant"], budget._key_token(b["payee"] or "")),
                 (b["amount"] or 0) > 0) for b in bills]
    # a stream whose merchant is one a bill already pays is that bill's,
    # whatever its words: by identity first, the words as the fallback
    ids_by_bill = budget.bill_merchant_ids(conn, bills)
    covered_ids = {(mid, (b["amount"] or 0) > 0)
                   for b in bills for mid in (ids_by_bill.get(b["payee"] or "") or ())}
    stream_names = sorted({(st.get("merchant") or "").strip()
                           for st in streams if (st.get("merchant") or "").strip()})
    stream_ids = {r["lname"]: r["id"] for r in conn.execute(
        """SELECT DISTINCT ON (lower(name)) lower(name) AS lname, id::text AS id
             FROM merchants WHERE merged_into IS NULL AND lower(name) = ANY(%s)
            ORDER BY lower(name), (plaid_entity_id IS NULL), created_at""",
        ([n.lower() for n in stream_names],)).fetchall()} if stream_names else {}
    # Keyed by (payee token, DIRECTION). Payee alone collides across the
    # two directions: a household that both pays and is paid by the same
    # counterparty (a rent payment and a roommate's reimbursement, a card
    # bill and its refund stream) would get one proposal for whichever
    # direction was seen first and silently lose the other. `income` is absent on
    # bill proposals and True on income ones, which is what the matcher
    # loop above compares against too.
    pending_keys = {(budget._key_token(r["payee"] or ""),
                     bool(as_dict(r["evidence"]).get("income")))
                    for r in conn.execute(
        "SELECT payee, evidence FROM bill_proposals "
        "WHERE status='pending' AND kind='add'").fetchall()}
    for st in streams:
        if not st.get("is_active") or st.get("status") not in ("MATURE", "EARLY_DETECTION"):
            stats["skipped"] += 1
            continue
        fq = _PLAID_FREQ.get((st.get("frequency") or "").upper())
        if not fq or not st.get("amount"):
            stats["skipped"] += 1
            continue
        text = f"{st.get('merchant','')} {st.get('description','')}".lower()
        income = st.get("direction") == "inflow"
        smid = stream_ids.get((st.get("merchant") or "").strip().lower())
        if (smid and (smid, income) in covered_ids) or any(
                m(text) and inc == income for m, inc in matchers):
            stats["covered"] += 1
            continue
        payee = merchant_dedup.canonical_merchant(st.get("merchant") or st.get("description") or "") \
            or (st.get("merchant") or "?")
        key = budget._key_token(payee)
        if key and (key, income) in pending_keys:
            stats["covered"] += 1
            continue
        pid = "prop:plaid:" + str(st.get("stream_id") or _sig("plaid", payee, st["amount"]))[:32]
        ok = _insert_proposal(
            conn, pid=pid, kind="add", payee=payee, amount=float(st["amount"]),
            frequency=fq[0], interval=fq[1],
            next_due=st.get("predicted_next_date"),
            evidence={"source": "plaid_recurring", "stream_id": st.get("stream_id"),
                      "status": st.get("status"), "first_date": st.get("first_date"),
                      "last_date": st.get("last_date"), "last_seen": st.get("last_date"),
                      "n": len(st.get("transaction_ids") or []),
                      "description": st.get("description"),
                      "category": st.get("category"), "income": income,
                      "item_id": st.get("item_id")})
        if ok:
            stats["proposed"] += 1
            if key:
                pending_keys.add((key, income))
    return stats


def apply_proposal(conn, pid: str, action: str) -> dict:
    """action = approve | reject. Approving mutates the recurring table.

    The status read and the act on it are one transaction over a LOCKED
    row. As a plain read followed by a write, two clicks on Approve (a
    double-tap, or the same proposal open in two tabs) would both see
    'pending' and both run `_apply_approved`, creating an added bill twice
    or applying an amount change twice."""
    with conn.transaction():
        p = conn.execute(
            "SELECT * FROM bill_proposals WHERE id = %s FOR UPDATE",
            (pid,)).fetchone()
        if not p or p["status"] not in ("pending",):
            return {"error": "unknown or already-decided proposal"}
        if action == "reject":
            conn.execute("UPDATE bill_proposals SET status='rejected', "
                         "decided_at=now() WHERE id=%s", (pid,))
            return {"rejected": p["payee"]}
        if action != "approve":
            return {"error": f"unknown action {action!r}"}
        ev = as_dict(p["evidence"])
        try:
            return _apply_approved(conn, p, ev)
        except ValueError as e:  # slug collision / bad amount — stay pending
            # Deliberately swallowed, not raised: the proposal must survive
            # as pending so it can be fixed and re-approved. Nothing else in
            # this block has written yet when _apply_approved raises.
            return {"error": str(e)}


def _apply_approved(conn, p, ev) -> dict:
    if p["kind"] == "add":
        save_bill(conn, payee=p["payee"], amount=p["amount"],
                  bill_type=p["bill_type"], frequency=p["frequency"],
                  interval=p["interval"] or 1, next_due=p["next_due"],
                  category=ev.get("category"), source="detected",
                  last_seen=ev.get("last_seen"),
                  by_month_day=ev.get("by_month_day"),
                  income=bool(ev.get("income")), manual_touch=False)
    elif p["kind"] == "remove":
        n = conn.execute("UPDATE bills SET active=0, synced_at=now() "
                         "WHERE payee=%s", (p["payee"],)).rowcount
        if n == 0:      # bill renamed/deleted since — don't mark this approved
            return {"error": f"bill '{p['payee']}' no longer exists"}
    elif p["kind"] == "attach":
        # a fee joins the bill it rides with: the proposal's payee is the
        # fee's own label, the evidence names the bill. The host row is
        # LOCKED for the read-modify-write of its companions list: two
        # attach proposals for the same bill are two proposal rows, so the
        # proposal lock above never contends, and without this lock the
        # later write would replace the whole companions key with a list
        # computed before the first landed — one fee silently lost.
        host = conn.execute("SELECT * FROM bills WHERE payee = %s FOR UPDATE",
                            (ev.get("bill_payee") or "",)).fetchone()
        if host is None or not host["active"]:
            return {"error": f"bill '{ev.get('bill_payee')}' no longer exists"}
        raw = as_dict(host["raw"])
        have = companions_of(raw)
        new = {"tokens": str(ev.get("tokens") or p["payee"]),
               "amount": abs(float(p["amount"] or 0)),
               "window_days": int(ev.get("window_days")
                                  or COMPANION_WINDOW_DAYS)}
        if not any(c["tokens"] == budget._tokens(new["tokens"]) for c in have):
            companions = ([{"tokens": c["label"], "amount": c["amount"],
                            "window_days": c["window_days"]}
                           for c in have] + [new])
            # merge just the companions key — a whole-raw overwrite from an
            # unlocked read would race a concurrent save of the host
            conn.execute("UPDATE bills SET raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb, "
                         "synced_at=now() WHERE id=%s",
                         (jsonb({"companions": companions}), host["id"]))
            apply_txn_categories(conn, bill_id=host["id"])
    elif p["kind"] == "merchant":
        # a merchant joins the bill's identity: the proposal's payee is the
        # merchant's display name, the evidence names the bill. Locked
        # read-modify-write of the names list, as for a fee.
        host = conn.execute("SELECT * FROM bills WHERE payee = %s FOR UPDATE",
                            (ev.get("bill_payee") or "",)).fetchone()
        if host is None or not host["active"]:
            return {"error": f"bill '{ev.get('bill_payee')}' no longer exists"}
        raw = as_dict(host["raw"])
        ident = _identity(conn, budget.bill_refs(raw) + [p["payee"]])
        # they accepted an identity change: the list is theirs from here,
        # so the nightly's "waiting on the person" mark is answered
        ident["merchant_pinned"] = True
        conn.execute("UPDATE bills SET raw = "
                     "(COALESCE(raw, '{}'::jsonb) - 'merchant_offered') "
                     "|| %s::jsonb, synced_at=now() WHERE id=%s",
                     (jsonb(ident), host["id"]))
        apply_txn_categories(conn, bill_id=host["id"])
    elif p["kind"] in ("frequency_change", "type_change"):
        bill = get_bill(conn, p["payee"])
        if bill is None:
            return {"error": f"bill '{p['payee']}' no longer exists"}
        old = as_dict(bill["raw"])
        # manual_touch=False: approving a machine proposal is not a hand-edit
        # of the AMOUNT — it must not (re)start the drift grace window
        save_bill(conn, payee=p["payee"], amount=p["amount"],
                  bill_type=p["bill_type"], frequency=p["frequency"],
                  interval=p["interval"] or 1, next_due=p["next_due"],
                  category=bill["category"], source=old.get("source", "detected"),
                  last_seen=ev.get("last_seen") or old.get("lastDueOn"),
                  by_month_day=ev.get("by_month_day"),
                  income=(bill["amount"] or 0) > 0, manual_touch=False)
    conn.execute("UPDATE bill_proposals SET status='approved', "
                 "decided_at=now() WHERE id=%s", (p["id"],))
    return {"approved": p["payee"], "kind": p["kind"]}


# ---- ledger scan --------------------------------------------------------


def _ledger_rows(conn, today: dt.date, *, include_pending: bool = False) -> list[dict]:
    """Candidate spend rows: same universe as the budget's spend definition,
    limited to depository/credit accounts."""
    from . import links
    since = today - dt.timedelta(days=LOOKBACK_MONTHS * 31)
    shadows = links.shadow_ids(conn)              # linked non-primaries
    out = []
    for r in conn.execute(
            # Raw descriptor, deliberately: detection matches on the tokens
            # the bank sends, and the payee it lands on becomes the bill's
            # match key (budget._key_token(payee)) — a display rename here
            # would create bills whose key matches no transaction text.
            f"""SELECT t.date, t.amount, {budget.MATCH_PAYEE_SQL} AS payee,
                       t.name, t.account_id,
                       COALESCE(t.category_override, t.category_primary, '') AS category,
                       {budget.FOOD_SQL} AS is_food,
                       -- and the merchant the ledger files it under: a
                       -- bill pays merchants, not letters
                       t.merchant_id::text AS merchant_id
                FROM transactions t JOIN accounts a ON a.id = t.account_id
                WHERE t.removed = 0 AND (t.pending = 0 OR %s) AND t.amount > 0
                  AND a.type IN ('depository', 'credit')
                  AND COALESCE(t.category_detailed,'') != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
                  AND COALESCE(t.category_override, t.category_primary, '') NOT IN
                      ('TRANSFER_OUT','TRANSFER_IN','INCOME','BANK_FEES')
                  -- exclude linked shadow accounts, else a
                  -- multi-source account's duplicated charges look like a
                  -- denser series → phantom bills / wrong cadence
                  AND NOT (t.account_id = ANY(%s))
                  -- business spend must not become a PERSONAL bill.
                  -- Entity money is its own section; without this an
                  -- entity's recurring charges would read as personal bills.
                  {budget.PERSONAL_ONLY_SQL}
                  AND t.date >= %s AND t.date <= %s""",
            (bool(include_pending), shadows, since, today)).fetchall():
        text = budget.match_text(r["payee"], r["name"])
        out.append({"date": as_date(r["date"]), "amount": r["amount"],
                    "payee": r["payee"] or "?", "category": r["category"],
                    "account_id": r["account_id"],
                    "merchant_id": r["merchant_id"],
                    "is_food": bool(r["is_food"]), "text": text,
                    "tokens": budget._tokens(text),
                    "key": budget._key_token(r["payee"] or "")})
    return out


def _events(rows: list[dict]) -> list[dict]:
    """Merge same-day rows for one merchant into one event."""
    by_day: dict[dt.date, dict] = {}
    for r in rows:
        e = by_day.setdefault(r["date"], {"date": r["date"], "amount": 0.0,
                                          "payee": r["payee"],
                                          "category": r["category"],
                                          "is_food": r["is_food"]})
        e["amount"] += r["amount"]
    return sorted(by_day.values(), key=lambda e: e["date"])


def _run_from_end(dates: list[dt.date], cycle: int) -> list[dt.date]:
    """Longest consecutive on-cycle run ending at the latest event. Events
    closer than a cycle are skipped (extra charges); a gap beyond tolerance
    ends the run."""
    tol = max(4, round(cycle * 0.22))
    run = [dates[-1]]
    for d in reversed(dates[:-1]):
        gap = (run[-1] - d).days
        if abs(gap - cycle) <= tol:
            run.append(d)
        elif gap < cycle - tol:
            continue
        else:
            break
    return list(reversed(run))


def _fit_cycle(dates: list[dt.date]) -> tuple[tuple, list[dt.date]] | None:
    """Best (cycle_def, run) across supported cycles, longest run wins;
    shorter cycle breaks ties.

    Density guard: a run is only valid if the merchant's TOTAL events inside
    the run's span roughly equal the run length — otherwise the skip-close
    logic can thread a 6-month 'cycle' through dense shopping noise."""
    best = None
    for cyc in CYCLES:
        if len(dates) < 2:
            break
        run = _run_from_end(dates, cyc[0])
        if len(run) < 2:
            continue
        span_events = sum(1 for d in dates if run[0] <= d <= run[-1])
        if span_events > len(run) + max(1, len(run) // 3):
            continue
        if best is None or len(run) > len(best[1]):
            best = (cyc, run)
    return best


def _amount_core(events: list[dict]) -> tuple[list[dict], float]:
    """Events within bill-matching tolerance of the median amount — outliers
    (a bonus paycheck, a one-off big charge) don't break the chain."""
    med = statistics.median(e["amount"] for e in events)
    tol = DETECT_TOL(med)
    return [e for e in events if abs(e["amount"] - med) <= tol], med


def _month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _complete_months(today: dt.date, n: int) -> list[str]:
    """The last n COMPLETE calendar months, oldest first."""
    first = today.replace(day=1)
    out = []
    for _ in range(n):
        first = (first - dt.timedelta(days=1)).replace(day=1)
        out.append(_month_key(first))
    return list(reversed(out))


def _monthly_totals(events, months: list[str]) -> tuple[list[float], list[int]]:
    tot = {m: 0.0 for m in months}
    cnt = {m: 0 for m in months}
    for e in events:
        m = _month_key(e["date"])
        if m in tot:
            tot[m] += e["amount"]
            cnt[m] += 1
    return [tot[m] for m in months], [cnt[m] for m in months]


def _stable(values: list[float], frac: float = ENV_MAD_FRAC) -> bool:
    """Max deviation from the median within frac. (MAD is useless at n=3.)"""
    med = statistics.median(values)
    if med <= 0:
        return False
    return max(abs(v - med) for v in values) <= frac * med


# ---- detection ----------------------------------------------------------


def _cycle_to_proposal(cyc, run, med, events):
    days, freq, iv = cyc
    last = run[-1]
    by_month_day = None
    if freq == "MONTHLY":
        by_month_day = int(statistics.median(d.day for d in run))
        nxt = (last.replace(day=1) + dt.timedelta(days=32 * iv)).replace(day=1)
        nxt = nxt.replace(day=min(by_month_day, 28))
    else:
        nxt = last + dt.timedelta(days=days)
    return {"cycle_days": days, "frequency": freq, "interval": iv,
            "next_due": nxt.isoformat(), "amount": round(med, 2),
            "by_month_day": by_month_day, "last_seen": last.isoformat(),
            "dates": [d.isoformat() for d in run[-6:]],
            "hits": len(run)}


def run(conn, today: dt.date | None = None) -> dict:
    """Nightly detection pass. Returns counters."""
    cfg = budget.load_config(conn)
    today = today or localtime.now_local(cfg).date()
    stats = {"proposed_add": 0, "proposed_remove": 0, "proposed_change": 0,
             "proposed_income": 0,
             "drift_applied": 0, "superseded": 0, "due_advanced": 0, "groups": 0}

    rows = _ledger_rows(conn, today)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["key"]:
            groups.setdefault(r["key"], []).append(r)
    stats["groups"] = len(groups)

    # keep each bill's canonical merchant fresh from the feed, give every
    # bill without an identity yet one, and offer the names a payee gained
    backfill_merchants(conn, today=today)
    stats.update(reconcile_bill_merchants(conn, today))
    # The disabled list is read WITH the rows, in one statement and so one
    # snapshot: read separately, a rename committing between the two reads
    # hands the pass a row under its old name and a config under the new
    # one, and the bill is watched for a night as though it were enabled.
    bills = conn.execute(
        """SELECT b.*,
                  COALESCE((SELECT jsonb_exists(s.config->'disabled_bills',
                                                b.payee)
                              FROM tenant_settings s
                             WHERE jsonb_typeof(s.config->'disabled_bills')
                                   = 'array'), false) AS budget_disabled
             FROM bills b WHERE b.active = 1""").fetchall()
    active_keys = set()
    for b in bills:
        k = budget._key_token(b["payee"] or "")
        if k:
            active_keys.add(k)
        # a bill's merchant tokens mark its ledger groups as covered
        active_keys |= budget._tokens(b["merchant"] or "")

    with conn.transaction():
        # supersede pending adds that an active bill now covers
        for p in conn.execute("SELECT id, payee FROM bill_proposals "
                              "WHERE status='pending' AND kind='add'").fetchall():
            if budget._key_token(p["payee"] or "") in active_keys:
                conn.execute("UPDATE bill_proposals SET status='superseded', "
                             "decided_at=now() WHERE id=%s", (p["id"],))
                stats["superseded"] += 1

        stats["income_rolled"] = advance_received_income(conn, today)
        _advance_due_dates(conn, today, stats)
        _maintain_bills(conn, bills, rows, today, stats)
        _propose_new(conn, groups, active_keys, today, stats)
        _propose_income(conn, today, stats)
    # bills that carry a transaction category stamp the rows they match
    # (and let go of rows they no longer should) — after the maintenance
    # above so a drifted amount's new tolerance is what gets applied
    tc = apply_txn_categories(conn)
    stats["txn_category_applied"] = tc["applied"]
    stats["txn_category_reverted"] = tc["reverted"]
    return stats


def _scan_months(rec: dict) -> int:
    """How many months ahead a due-date roll must look to find the NEXT
    occurrence. 14 months covers a yearly bill; a bill on a longer cycle
    (a YEARLY interval of 2, or a MONTHLY interval of 18) has its next
    occurrence 12·N months out, and a fixed 14-month scan would find
    nothing, leaving `due_on` in the past and the Bills page calling a bill
    paid on its day "overdue" every day after."""
    return max(14, budget._cycle_days(rec) // 30 + 3)


def _advance_due_dates(conn, today, stats):
    """Roll each bill's due_on forward to the next projected occurrence once
    it passes (the page's 'Next due' column and the email's 'due within 7
    days' section read due_on directly)."""
    for b in conn.execute(
            "SELECT id, due_on, raw FROM bills WHERE active = 1").fetchall():
        raw = as_dict(b["raw"])
        rec = raw.get("recurrence") or {}
        due = as_date(b["due_on"])
        if (raw.get("bill_type") == "envelope" or not due
                or due >= today or not rec.get("frequency")):
            continue  # envelope/one-time/current bills keep their anchor
        bill = {"recurrence": rec, "anchor": due}
        nxt, (y, m) = None, (today.year, today.month)
        for _ in range(_scan_months(rec)):
            for d in budget._occurrences_in_month(bill, y, m):
                if d >= today:
                    nxt = d
                    break
            if nxt:
                break
            y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        if not nxt:
            continue
        delta = {"dueOn": nxt.isoformat()}
        if not raw.get("lastDueOn"):
            # the series' first due date has passed — it has history now, so
            # the new-series floor must not travel forward with the anchor
            delta["lastDueOn"] = due.isoformat()
        # merge only the keys this pass owns — a whole-raw overwrite from
        # the loop's snapshot read would race a user's concurrent save and
        # lose whatever they had just written (lock_amount, companions, …)
        conn.execute("UPDATE bills SET due_on=%s, raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb, "
                     "synced_at=now() WHERE id=%s",
                     (nxt, jsonb(delta), b["id"]))
        stats["due_advanced"] += 1


def advance_received_income(conn, today: dt.date | None = None) -> int:
    """Roll an income series' due_on to the next occurrence the moment its
    deposit posts. Without it, on payday the Next column sits on today's
    date until the nightly pass — a received paycheck due today is stale
    chrome, unlike an unpaid bill due today, which is actionable.

    Evidence bar mirrors match_occurrences: a posted non-transfer inflow
    on a depository account (budget._inflow_rows), amount within
    max($30, 25%), merchant/key-matched, dated inside the occurrence's
    own window and not before the last received occurrence. Called from
    the Bills page load (cheap: income rows only, idempotent) and the
    nightly detect; the nightly _advance_due_dates still rolls unpaid
    past-due rows regardless."""
    today = today or localtime.household_day(conn)
    n = 0
    due_rows = conn.execute(
        "SELECT id, payee, amount, due_on, merchant, raw FROM bills "
        "WHERE active = 1 AND amount > 0 AND due_on IS NOT NULL "
        "AND due_on <= %s", (today,)).fetchall()
    ids = budget.bill_merchant_ids(conn, due_rows)
    for b in due_rows:
        raw = as_dict(b["raw"])
        rec = raw.get("recurrence") or {}
        due = as_date(b["due_on"])
        if raw.get("bill_type") == "envelope" or not rec.get("frequency"):
            continue
        window = max(3, min(budget.PREPAY_MATCH_DAYS,
                            budget._cycle_days(rec) // 2))
        match = budget.merchant_matcher(
            b["merchant"], budget._key_token(b["payee"] or ""),
            ids.get(b["payee"] or ""))
        tol = budget.bill_tolerance(float(b["amount"]))
        floor = as_date(raw.get("lastDueOn")) if raw.get("lastDueOn") else None
        received = False
        for r in budget._inflow_rows(conn, due - dt.timedelta(days=window),
                                     min(today, due + dt.timedelta(days=window))
                                     + dt.timedelta(days=1)):
            if floor is not None and as_date(r["date"]) <= floor:
                continue    # that deposit belongs to the previous occurrence
            if abs(float(r["amount"]) - float(b["amount"])) > tol:
                continue
            if match(budget.match_text(r["payee"], r["name"]), None,
                     budget.row_merchant_id(r)):
                received = True
                break
        if not received:
            continue
        bill = {"recurrence": rec, "anchor": due}
        # roll ONE cycle at a time — the first occurrence
        # strictly after the received due, even if that is still in the
        # past. A row multiple cycles behind (missed nightly runs) must not
        # let one deposit vault the schedule to the first future due; the
        # next call rolls again only if THAT occurrence's own deposit
        # evidence exists.
        nxt, (y, m) = None, (due.year, due.month)
        for _ in range(_scan_months(rec)):
            for d in budget._occurrences_in_month(bill, y, m):
                if d > due:
                    nxt = d
                    break
            if nxt:
                break
            y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        if not nxt:
            continue
        # merge only this pass's keys — see _advance_due_dates: a whole-raw
        # overwrite from the loop's snapshot would race a concurrent save
        conn.execute("UPDATE bills SET due_on=%s, raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb, "
                     "synced_at=now() WHERE id=%s",
                     (nxt, jsonb({"lastDueOn": due.isoformat(),
                                  "dueOn": nxt.isoformat()}), b["id"]))
        n += 1
    return n


def _refill_like(events: list[dict]) -> bool:
    """Prepaid-card fingerprint: charges cluster on a few discrete amounts
    (gift-card $30/$50 top-ups). Restaurant/grocery totals almost never
    repeat exactly."""
    amts = [round(e["amount"], 2) for e in events]
    if len(amts) < 4:
        return False
    top2 = sorted((amts.count(v) for v in set(amts)), reverse=True)[:2]
    return sum(top2) >= 0.6 * len(amts)


def _companion_context(conn, groups):
    """What the auto-suggest needs: every active occurrence bill's already
    attached companions (a covered fee is never proposed as its own bill)
    and, per bill, the dates+accounts of the ledger rows its own group
    holds — the charges a fee would ride beside."""
    covered, hosts = [], []
    for b in conn.execute(
            "SELECT payee, amount, raw FROM bills WHERE active=1 AND amount<0"
    ).fetchall():
        raw = as_dict(b["raw"])
        if raw.get("bill_type") == "envelope":
            continue
        covered.extend(c["tokens"] for c in companions_of(raw))
        key = budget._key_token(b["payee"] or "")
        rows = groups.get(key) if key else None
        if rows:
            hosts.append({"payee": b["payee"],
                          "events": [(r["date"], r.get("account_id"))
                                     for r in rows]})
    return covered, hosts


def _suggest_attach(conn, key, rws, events, med, hosts, stats):
    """A small, fee-shaped group whose charges land within a few days of
    one bill's charges, on the same account, at least three cycles running
    → propose attaching it to that bill instead of proposing it as a bill.
    Returns True when a proposal was made (or already stands)."""
    group_tokens = set().union(*(r["tokens"] for r in rws))
    if not (group_tokens & FEE_WORDS) or med > COMPANION_MAX_AMOUNT:
        return False
    accounts = {r.get("account_id") for r in rws}
    best, best_hits = None, 0
    for h in hosts:
        hits = 0
        for e in events:
            if any(abs((e["date"] - hd).days) <= COMPANION_WINDOW_DAYS
                   and (ha is None or ha in accounts)
                   for hd, ha in h["events"]):
                hits += 1
        if hits > best_hits:
            best, best_hits = h, hits
    if best is None or best_hits < MIN_HITS:
        return False
    label = max((r["payee"] for r in rws),
                key=lambda p: sum(1 for r in rws if r["payee"] == p))
    pid = _sig("attach", key, best["payee"])
    if _insert_proposal(conn, pid=pid, kind="attach", payee=label,
                        amount=round(med, 2), frequency=None, interval=1,
                        evidence={"bill_payee": best["payee"],
                                  "tokens": " ".join(sorted(
                                      group_tokens & budget._tokens(label))
                                      or group_tokens),
                                  "window_days": COMPANION_WINDOW_DAYS,
                                  "hits": best_hits,
                                  "last_seen": events[-1]["date"].isoformat()}):
        stats["proposed_attach"] = stats.get("proposed_attach", 0) + 1
    return True


def _propose_new(conn, groups, active_keys, today, stats):
    months4 = _complete_months(today, 4)
    covered, hosts = _companion_context(conn, groups)
    for key, rws in groups.items():
        if key in active_keys or "amazon" in key:
            continue
        # an active bill's key anywhere in this merchant's descriptors means
        # the bill matcher already claims these rows — not a new series
        group_tokens = set().union(*(r["tokens"] for r in rws))
        if group_tokens & active_keys:
            continue
        # a fee already attached to a bill rides with that bill — it is
        # never a bill of its own
        if any(ct <= group_tokens for ct in covered):
            continue
        events = _events(rws)
        if len(events) < 2:
            continue
        payee = max((e["payee"] for e in events),
                    key=lambda p: sum(1 for e in events if e["payee"] == p))
        cats = [e["category"] for e in events if e["category"]]
        category = max(set(cats), key=cats.count) if cats else None
        food = any(e["is_food"] for e in events)

        # --- occurrence fingerprint (food excluded by policy) ---
        core, med = _amount_core(events)
        # A key is a lead token, and a city-named merchant ("SPRINGFIELD
        # REC CENTER") shares its key with every café in that city. Judging
        # "food" over the whole group would let one lunch veto a steady,
        # exact-amount monthly membership; the policy is about the SERIES,
        # so it is judged on the amount core that would become the bill.
        food_core = (sum(1 for e in core if e["is_food"]) * 2 > len(core)
                     if core else food)
        amt_by_date = {e["date"]: e["amount"] for e in core}
        fit = _fit_cycle([e["date"] for e in core]) if len(core) >= 2 else None
        # A recency-aware second try: when the whole-history core's run
        # went stale (a provider renamed the series, or the price moved out
        # of the old cluster), the current series lives in the recent events.
        if fit and (today - fit[1][-1]).days > fit[0][0] * 1.5 \
                and len(events) >= 6:
            rcore, rmed = _amount_core(events[-8:])
            rfit = (_fit_cycle([e["date"] for e in rcore])
                    if len(rcore) >= 3 else None)
            if rfit and (today - rfit[1][-1]).days <= rfit[0][0] * 1.5:
                core, med, fit = rcore, rmed, rfit
                food_core = sum(1 for e in core if e["is_food"]) * 2 > len(core)
                amt_by_date = {e["date"]: e["amount"] for e in core}
        occ_ok = False
        # a fee that rides with a bill's charges is offered as a companion
        # of that bill, not as a $1 bill of its own
        if _suggest_attach(conn, key, rws, events, med, hosts, stats):
            continue
        if fit and not food_core and med >= 5:
            cyc, run_dates = fit
            need = MIN_HITS_LONG if cyc[0] >= LONG_CYCLE else MIN_HITS
            fresh = (today - run_dates[-1]).days <= cyc[0] * 1.5
            # a long-cycle run at the 2-hit minimum is thin evidence — only a
            # near-exact repeated amount (how real annual bills bill) counts;
            # with 3+ on-cycle hits the cadence itself is the evidence
            exact = True
            if cyc[0] >= LONG_CYCLE:
                # a long cycle is thin evidence at any run length — real
                # semi-annual/annual bills bill near-exact, and two
                # unrelated charges six months apart do not
                tol = max(5.0, med * 0.05) if cyc[0] >= 300 else max(10.0, med * 0.10)
                exact = (med >= 15
                         and all(abs(amt_by_date[d] - med) <= tol for d in run_dates))
                if exact and len(run_dates) == 2:
                    # …and at the 2-hit minimum, the same merchant string
                    # both times — the group key alone is one lead token
                    payees = {e["payee"] for e in core if e["date"] in run_dates}
                    exact = len(payees) == 1
            if len(run_dates) >= need and fresh and exact:
                # The proposal carries the CURRENT price — the median of
                # the last three on-cycle hits — which is exactly what the
                # drift check will compute once the bill is tracked. With
                # the whole-history median, a bill whose price rose over
                # two years would be approved at the old figure and
                # immediately "drift" to the new one on the next pass — an
                # amount change on a ledger that had not changed.
                # Same idea income uses.
                # …read from the proposal's own merchant string when the
                # core also holds a look-alike neighbour (same key, other
                # shop): its dates can sit on-cycle too
                by_date_payee = {e["date"]: e["payee"] for e in core}
                own = [d for d in run_dates if by_date_payee.get(d) == payee]
                recent_dates = (own or run_dates)[-3:]
                recent_amts = [amt_by_date[d] for d in recent_dates
                               if d in amt_by_date]
                cur_amt = (statistics.median(recent_amts)
                           if recent_amts else med)
                ev = _cycle_to_proposal(cyc, run_dates, cur_amt, core)
                ev["category"] = category
                pid = _sig("add", key, "occurrence", cyc[0], round(cur_amt, -1))
                if _insert_proposal(conn, pid=pid, kind="add", payee=payee,
                                    amount=round(cur_amt, 2), frequency=cyc[1],
                                    interval=cyc[2], next_due=ev["next_due"],
                                    evidence=ev):
                    stats["proposed_add"] += 1
                occ_ok = True

        # --- envelope fingerprint (food only with refill-like amounts) ---
        if occ_ok:
            continue
        recent_events = [e for e in events
                         if _month_key(e["date"]) in months4]
        if food and not _refill_like(recent_events):
            continue
        totals, counts = _monthly_totals(events, months4)
        t3, c3 = totals[-3:], counts[-3:]
        # A utility or medical series bills every month at an amount that
        # moves with the season or the visit — an electric bill, a clinic
        # co-pay — which is exactly what an envelope is for, and never a
        # steady occurrence. Those categories get a wider stability band
        # (the pool is a cap; the month's real number lands in it), and the
        # pool is the trailing average rather than the median so a cold
        # month is covered: a monthly utility with a year of hits and no
        # two alike must propose an envelope, never an occurrence.
        variable_kind = (category or "") in ("RENT_AND_UTILITIES", "MEDICAL")
        if variable_kind and not food:
            # judged over six months, because a utility's billing date
            # slips across month ends (two bills in one month, none the
            # next): present in at least four of the last six months, five
            # or more charges, and the pool is the six-month average — a
            # cold month is covered, a mild one leaves headroom
            months6 = _complete_months(today, 6)
            t6, c6 = _monthly_totals(events, months6)
            present = sum(1 for cc in c6 if cc >= 1)
            avg6 = sum(t6) / 6
            if present >= 4 and sum(c6) >= 5 and avg6 >= ENV_MIN_MONTHLY:
                monthly = round(avg6, 2)
                ev = {"monthly_totals": dict(zip(months6, [round(t, 2) for t in t6])),
                      "charges_per_month": dict(zip(months6, c6)),
                      "category": category,
                      "last_seen": events[-1]["date"].isoformat()}
                pid = _sig("add", key, "envelope", "env6", round(monthly, -1))
                if _insert_proposal(conn, pid=pid, kind="add", payee=payee,
                                    amount=monthly, bill_type="envelope",
                                    evidence=ev):
                    stats["proposed_add"] += 1
                continue
        if (all(c >= 1 for c in c3) and _stable(t3)
                and statistics.median(t3) >= ENV_MIN_MONTHLY
                and (sum(c3) / 3 >= 2 or fit is None or len(fit[1]) < MIN_HITS)):
            monthly = round(statistics.median(t3), 2)
            ev = {"monthly_totals": dict(zip(months4, [round(t, 2) for t in totals])),
                  "charges_per_month": dict(zip(months4, counts)),
                  "category": category,
                  "last_seen": events[-1]["date"].isoformat()}
            pid = _sig("add", key, "envelope", "env", round(monthly, -1))
            if _insert_proposal(conn, pid=pid, kind="add", payee=payee,
                                amount=monthly, bill_type="envelope",
                                evidence=ev):
                stats["proposed_add"] += 1


# ACH/payroll descriptors that bury the employer name as the second token
# ("DIR DEP NORTHWIND…", "ACH PAYROLL ACME"). Stripped only for grouping —
# the proposal still keeps the raw payee label.
_INCOME_NOISE = re.compile(
    r"\b(dir|dd|ach|direct|deposit|dep|payroll|salary|wages|ppd|ccd|"
    r"xfer|transfer|dirdep|dirdepst|web|pos|trace)\b", re.I)


def _income_key(payee: str) -> str | None:
    """Distinctive employer token after stripping ACH/payroll noise words."""
    cleaned = _INCOME_NOISE.sub(" ", payee or "")
    return budget._key_token(cleaned) or budget._key_token(payee)


def _propose_income(conn, today, stats):
    """Paycheck-series detection: recurring deposits into bank accounts.
    Bonus-sized outliers fall out via the amount-core filter."""
    from . import links
    since = today - dt.timedelta(days=730)
    shadows = links.shadow_ids(conn)              # see _income_rows
    rows = [{"date": as_date(r["date"]), "amount": -r["amount"],
             "payee": r["payee"] or "?", "category": "INCOME", "is_food": False}
            for r in conn.execute(
                # Raw descriptor: paycheck grouping keys off the ACH text
                # (_income_key strips its noise words), and the merchant map
                # is a retail-display tool that never sees payroll strings.
                f"""SELECT t.date, t.amount, {budget.MATCH_PAYEE_SQL} AS payee
                   FROM transactions t
                   JOIN accounts a ON a.id = t.account_id
                   WHERE t.removed = 0 AND t.pending = 0 AND t.amount <= -200
                     AND a.type = 'depository'
                     -- Household paychecks only: an LLC's client deposits
                     -- into an entity checking account would otherwise be
                     -- proposed (and approved) as personal income.
                     {budget.PERSONAL_ONLY_SQL}
                     -- '' too: sparse sources (SimpleFIN) carry NO category,
                     -- and the LLM flow-guard never assigns INCOME — with
                     -- INCOME-only, their paychecks would be undetectable. The
                     -- cadence/amount-core fit + approve gate screen the
                     -- uncategorized noise this lets in.
                     AND COALESCE(t.category_override, t.category_primary, '')
                         IN ('INCOME', '')
                     -- A dual-source account would propose a doubled
                     -- paycheck the user could approve as real
                     AND NOT (t.account_id = ANY(%s))
                     AND t.date >= %s""", (shadows, since)).fetchall()]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        k = _income_key(r["payee"])
        if k:
            groups.setdefault(k, []).append(r)
    active_income = {_income_key(r["payee"] or "")
                     for r in conn.execute(
                         "SELECT payee FROM bills WHERE active=1 AND amount > 0"
                     ).fetchall()}
    for key, rws in groups.items():
        if key in active_income:
            continue
        events = _events(rws)
        if len(events) < MIN_HITS:
            continue
        # Amount core from RECENT deposits first: a multi-year raise ladder
        # (or sparse bonuses) pulls the all-history median off the current
        # paycheck, then _amount_core keeps too many off-amount hits and
        # density kills the biweekly fit: a paycheck well above its own
        # two-year median would otherwise produce no proposal at all.
        # Anchor on the last ~6mo (or full history if thinner),
        # then re-select every event near that median so the cadence run
        # can still reach back.
        recent_cut = today - dt.timedelta(days=180)
        recent = [e for e in events if e["date"] >= recent_cut] or events
        _, med = _amount_core(recent)
        tol = DETECT_TOL(med)
        core = [e for e in events if abs(e["amount"] - med) <= tol]
        if len(core) < MIN_HITS:
            continue
        fit = _fit_cycle([e["date"] for e in core]) if len(core) >= 2 else None
        if not fit:
            continue
        cyc, run_dates = fit
        # Recency: allow ~2.5 cycles of silence so biweekly payroll holidays
        # and mid-cycle wizard runs still propose (1.5 cycles would drop a
        # biweekly series after one late payday). Floor 35d keeps short
        # cycles from dying a few days past one missed payday. ledger_fit uses ×2 for
        # the same idea on the bills side.
        max_age = max(35, int(cyc[0] * 2.5))
        if len(run_dates) < MIN_HITS or (today - run_dates[-1]).days > max_age:
            continue
        # Prefer a clean employer label — Plaid often leaves ACH memo tails
        # (a "future amount" clause, a "~ Tran: DDIR" suffix) as the modal
        # string.
        labels = [e["payee"] for e in events]
        clean = [p for p in labels
                 if "future amount" not in p.lower() and "~" not in p]
        pool = clean or labels
        payee = max(pool, key=lambda p: sum(1 for x in labels if x == p))
        if len(payee) > 48:          # still noisy → first 3 words
            payee = " ".join(payee.split()[:3])
        # Proposal amount = median of the recent on-cycle core hits, not the
        # full-history core (raises land as the current paycheck size).
        recent_run_amts = [
            e["amount"] for e in core
            if e["date"] in set(run_dates[-6:]) and e["date"] >= recent_cut]
        prop_amt = (statistics.median(recent_run_amts)
                    if recent_run_amts else med)
        ev = _cycle_to_proposal(cyc, run_dates, prop_amt, core)
        ev["income"] = True
        pid = _sig("add-income", key, cyc[0], round(prop_amt, -1))
        if _insert_proposal(conn, pid=pid, kind="add", payee=payee,
                            amount=round(prop_amt, 2), frequency=cyc[1],
                            interval=cyc[2], next_due=ev["next_due"], evidence=ev):
            stats["proposed_add"] += 1
            stats["proposed_income"] = stats.get("proposed_income", 0) + 1


def _maintain_bills(conn, bills, rows, today, stats):
    months3 = _complete_months(today, 3)
    months2 = _complete_months(today, 2)
    # a budget-disabled bill is paused, not watched: no drift corrections
    # and no removal proposals — one tap disabled it, so a standing
    # "remove?" proposal would put the destructive path one tap further.
    # The flag rides on the row (run reads it in the row's own statement),
    # not on a config re-read that a rename can have moved out from under.
    ids = budget.bill_merchant_ids(conn, bills)
    for b in bills:
        payee = b["payee"] or ""
        if b["budget_disabled"]:
            continue
        raw = as_dict(b["raw"])
        bill_type = raw.get("bill_type", "occurrence")
        key = budget._key_token(payee)
        match = budget.merchant_matcher(b["merchant"], key, ids.get(payee))
        matched = [r for r in rows
                   if match(r["text"], r["tokens"], r.get("merchant_id"))]
        if not b["merchant"] and not key:
            continue
        events = _events(matched)
        bill_amt = abs(b["amount"] or 0)
        is_income = (b["amount"] or 0) > 0

        if bill_type == "envelope":
            # the pool covers envelope_months — 1 (the month) or 12
            # (the calendar year). Both rules below are PERIOD-relative,
            # because monthly-pool rules are wrong in both directions on an
            # annual pool: a lumpy yearly cost landing twice in a 3-month
            # window would drift the whole year pool down to one charge, and
            # two quiet months — which is 10 months out of 12 for an annual
            # cost, by definition — would propose deleting the bill.
            pool_months = budget.envelope_months(raw)
            this_month = any(_month_key(e["date"]) == _month_key(today)
                             for e in events)

            if pool_months >= 12:
                months12 = _complete_months(today, 12)
                totals12, counts12 = _monthly_totals(events, months12)
                # removal: quiet for a WHOLE pool period, not two months
                if events and all(c == 0 for c in counts12) and not this_month:
                    pid = _sig("remove", key, "env12",
                               _month_key(events[-1]["date"]))
                    if _insert_proposal(
                            conn, pid=pid, kind="remove", payee=payee,
                            amount=bill_amt, bill_type="envelope",
                            evidence={"last_seen": events[-1]["date"].isoformat(),
                                      "pool_months": 12}):
                        stats["proposed_remove"] += 1
                    continue
                # drift: an annual pool is a YEAR's spend, so it tracks the
                # trailing 12-month TOTAL — and only once a full year of this
                # merchant is actually observable, or a half-year of history
                # would ratchet the pool down to half its real size.
                year_covered = (events
                                and (today - events[0]["date"]).days >= 365)
                new_amt = round(sum(totals12), 2)
                if (year_covered and not raw.get("lock_amount")
                        and not in_manual_grace(raw, today)
                        and new_amt >= ENV_MIN_MONTHLY
                        and abs(new_amt - bill_amt) > DRIFT_MIN):
                    _apply_drift(conn, b, new_amt,
                                 {"monthly_totals": dict(zip(months12, totals12)),
                                  "pool_months": 12}, stats, today)
                continue

            totals, counts = _monthly_totals(events, months3)
            # removal: two consecutive complete months with zero charges AND
            # nothing this month either (a merchant that resumed charging
            # yesterday must not get a removal proposal)
            _, c2 = _monthly_totals(events, months2)
            if events and all(c == 0 for c in c2) and not this_month:
                pid = _sig("remove", key, "env",
                           _month_key(events[-1]["date"]))
                if _insert_proposal(conn, pid=pid, kind="remove", payee=payee,
                                    amount=bill_amt, bill_type="envelope",
                                    evidence={"last_seen": events[-1]["date"].isoformat()}):
                    stats["proposed_remove"] += 1
                continue
            # monthly amount auto-tracks the trailing 3-month median, UNLESS
            # amount-locked or hand-edited within the grace window
            new_amt = round(statistics.median(totals), 2) if totals else 0.0
            if (not raw.get("lock_amount") and not in_manual_grace(raw, today)
                    and new_amt >= ENV_MIN_MONTHLY
                    and abs(new_amt - bill_amt) > DRIFT_MIN):
                _apply_drift(conn, b, new_amt, {"monthly_totals":
                                                dict(zip(months3, totals))},
                             stats, today)
            continue

        if is_income:
            continue  # income series: no drift/removal automation

        # occurrence bills ------------------------------------------------
        tol = budget.bill_tolerance(bill_amt)
        amt_matched = [e for e in events if abs(e["amount"] - bill_amt) <= tol]
        rec = raw.get("recurrence") or {}
        cycle = budget._cycle_days(rec) if rec.get("frequency") else None

        if not rec.get("frequency"):
            # one-time bill: propose removal 60 days after its due date passes
            due = as_date(raw.get("dueOn"))
            if due and (today - due).days > 60:
                pid = _sig("remove", key, "onetime", due.isoformat())
                if _insert_proposal(conn, pid=pid, kind="remove", payee=payee,
                                    amount=bill_amt,
                                    evidence={"due_on": due.isoformat()}):
                    stats["proposed_remove"] += 1
            continue

        # removal: 2 fully missed cycles (only for bills the ledger has seen),
        # AND the merchant itself must be gone — a merchant still charging at
        # a different price is an amount problem, not a dead bill
        if (amt_matched and (today - amt_matched[-1]["date"]).days > 2 * cycle
                and (not events or (today - events[-1]["date"]).days > 2 * cycle)):
            pid = _sig("remove", key, cycle, _month_key(amt_matched[-1]["date"]))
            if _insert_proposal(conn, pid=pid, kind="remove", payee=payee,
                                amount=bill_amt,
                                evidence={"last_seen": amt_matched[-1]["date"].isoformat(),
                                          "cycle_days": cycle}):
                stats["proposed_remove"] += 1
            continue

        # amount drift: median of the last 3 matched occurrences (auto-apply,
        # unless amount-locked or hand-edited within the grace window).
        # Only once at least one occurrence has landed AFTER the amount was
        # set: on a just-imported ledger there is nothing to drift FROM —
        # every hit the median sees predates the bill, and the amount the
        # person approved a minute ago is the amount.
        # `amount_set_on` is stamped by save_bill; a row without it is not
        # gated.
        # raw is JSON that a restore ZIP can hand us verbatim: a malformed
        # stamp is treated as a missing one (no gate), never a crash of the
        # nightly pass
        try:
            set_on = (as_date(raw.get("amount_set_on"))
                      if raw.get("amount_set_on") else None)
        except (ValueError, TypeError):
            set_on = None
        since_set = ([e for e in amt_matched if e["date"] > set_on]
                     if set_on else amt_matched)
        if (len(amt_matched) >= 3 and since_set and not raw.get("lock_amount")
                and not in_manual_grace(raw, today)):
            new_amt = round(statistics.median(e["amount"] for e in amt_matched[-3:]), 2)
            if abs(new_amt - bill_amt) > DRIFT_MIN:
                _apply_drift(conn, b, new_amt,
                             {"recent": [(e["date"].isoformat(), round(e["amount"], 2))
                                         for e in amt_matched[-3:]]}, stats, today)

        # frequency change: recent matched events fit a different cycle
        # (≥4 hits — one skipped week shouldn't reshape a bill's schedule)
        if len(amt_matched) >= 4 and cycle and cycle <= 120:
            recent = [e["date"] for e in amt_matched[-6:]]
            fit = _fit_cycle(recent)
            if fit:
                cyc, run_dates = fit
                if (len(run_dates) >= 4
                        and abs(cyc[0] - cycle) > 0.25 * cycle
                        and (today - run_dates[-1]).days <= cyc[0] * 1.5):
                    med = statistics.median(e["amount"] for e in amt_matched[-3:])
                    ev = _cycle_to_proposal(cyc, run_dates, med, amt_matched)
                    ev["old_cycle_days"] = cycle
                    pid = _sig("freq", key, cyc[0], cycle)
                    if _insert_proposal(conn, pid=pid, kind="frequency_change",
                                        payee=payee, amount=round(med, 2),
                                        frequency=cyc[1], interval=cyc[2],
                                        next_due=ev["next_due"], evidence=ev):
                        stats["proposed_change"] += 1

        # type change → envelope: many variable-amount charges, stable total
        totals, counts = _monthly_totals(events, months3)
        if (all(c >= 2 for c in counts) and _stable(totals)
                and len(events) >= 6
                and not _stable([e["amount"] for e in events[-6:]], 0.25)):
            monthly = round(statistics.median(totals), 2)
            ev = {"monthly_totals": dict(zip(months3, [round(t, 2) for t in totals])),
                  "charges_per_month": dict(zip(months3, counts)),
                  "last_seen": events[-1]["date"].isoformat()}
            pid = _sig("type", key, "env", round(monthly, -1))
            if _insert_proposal(conn, pid=pid, kind="type_change", payee=payee,
                                amount=monthly, bill_type="envelope", evidence=ev):
                stats["proposed_change"] += 1


# ---- validation & investigation ------------------------------------------


def _income_rows(conn, today: dt.date) -> list[dict]:
    """Inflow universe for matching income series (amounts flipped positive).

    Shadow-excluded exactly like `_ledger_rows`: without it, two linked
    checking sources feeding one real account turn a $3,000 paycheck into a
    same-day $6,000 event — `_events` sums same-day amounts. That would
    drive income proposals the user could approve ("biweekly $6k" that is
    really $3k×2), income bill-health, and the forecast's paycheck median.
    """
    from . import links
    since = today - dt.timedelta(days=730)
    shadows = links.shadow_ids(conn)              # linked non-primaries
    out = []
    for r in conn.execute(
            # Raw descriptor: this is the universe an income bill's key is
            # matched against (same rule as _ledger_rows) — matching text,
            # not a display name.
            f"""SELECT t.date, t.amount, {budget.MATCH_PAYEE_SQL} AS payee,
                      t.name, COALESCE(t.category_primary,'') AS category,
                      t.merchant_id::text AS merchant_id
               FROM transactions t
               JOIN accounts a ON a.id = t.account_id
               WHERE t.removed = 0 AND t.pending = 0 AND t.amount <= -200
                 AND a.type = 'depository'
                 AND NOT (t.account_id = ANY(%s))
                 AND t.date >= %s"""
            # an entity's deposits are business income, not the
            # owner's paycheck series
            + budget.PERSONAL_ONLY_SQL,
            (shadows, since)).fetchall():
        text = budget.match_text(r["payee"], r["name"])
        out.append({"date": as_date(r["date"]), "amount": -r["amount"],
                    "payee": r["payee"] or "?", "category": r["category"],
                    "merchant_id": r["merchant_id"],
                    "is_food": False, "text": text,
                    "tokens": budget._tokens(text),
                    "key": budget._key_token(r["payee"] or "")})
    return out


def ledger_fit(events: list[dict], today: dt.date) -> dict | None:
    """Best description of what the ledger actually shows for a merchant:
    an occurrence fit (cycle + median amount) or an envelope fit (median
    monthly total)."""
    if len(events) < 3:
        return None
    core, med = _amount_core(events)
    fit = _fit_cycle([e["date"] for e in core]) if len(core) >= 2 else None
    if fit and len(fit[1]) >= MIN_HITS and (today - fit[1][-1]).days <= fit[0][0] * 2:
        cyc, run = fit
        p = _cycle_to_proposal(cyc, run, med, core)
        # Amount = the RECENT charges (median of the last 3 occurrences),
        # NOT the full-history median — a bill whose price changed shows its
        # CURRENT amount, not a stale years-old average
        amt_by_date = {}
        for e in events:
            amt_by_date.setdefault(e["date"], e["amount"])
        recent = [amt_by_date[d] for d in run[-3:] if d in amt_by_date]
        amt = round(statistics.median(recent), 2) if recent else p["amount"]
        # how far apart the recent charges sit — the attention queue may
        # only offer "accept this amount" when they agree (a median of
        # 120/150/400 is not a price, it is noise)
        spread = round(max(recent) - min(recent), 2) if len(recent) > 1 else 0.0
        return {"kind": "occurrence", "amount": amt,
                "cycle_days": cyc[0], "hits": len(run),
                "recent_spread": spread,
                "cadence": f"{cyc[1]}:{cyc[2]}", "next_due": p["next_due"],
                "short": f"~${amt:,.0f}/{cyc[0]}d",
                "label": f"~${amt:,.2f} about every {cyc[0]} days "
                         f"({len(run)} hits, last "
                         f"{dt.date.fromisoformat(p['last_seen']).strftime('%m/%d/%y')})"}
    months3 = _complete_months(today, 3)
    totals, counts = _monthly_totals(events, months3)
    if all(c >= 1 for c in counts):
        monthly = round(statistics.median(totals), 2)
        return {"kind": "envelope", "amount": monthly, "cadence": "ENVELOPE:1",
                "next_due": "", "short": f"~${monthly:,.0f}/mo",
                "label": f"~${monthly:,.2f}/month across "
                f"{min(counts)}–{max(counts)} charges (no clean cycle → envelope)"}
    return None


def _phase_off_days(raw: dict, fit: dict | None, cycle: int) -> int | None:
    """How far the bill's due-date ANCHOR sits from the ledger's real payment
    phase, folded into [0, cycle/2]. Only DAILY/WEEKLY (day-exact) cadences
    are judged — MONTHLY/YEARLY are anchored by calendar day, so a raw
    day-difference is noisy."""
    rec = raw.get("recurrence") or {}
    if (rec.get("frequency") or "").upper() not in ("DAILY", "WEEKLY"):
        return None
    if not fit or fit.get("kind") != "occurrence" or not fit.get("next_due"):
        return None
    stored = raw.get("dueOn")
    if not stored or cycle <= 1:
        return None
    try:
        s = dt.date.fromisoformat(stored)
        f = dt.date.fromisoformat(fit["next_due"])
    except ValueError:
        return None
    d = abs((f - s).days) % cycle
    return min(d, cycle - d)


def _fit_suggests_change(raw: dict, fit: dict | None, bill_amt: float) -> bool:
    """True when the ledger fit points at a config change worth surfacing on
    the main Bills page — amount beyond drift tolerance, or a genuinely
    different cadence (fuzzy real-world schedules shouldn't nag)."""
    if not fit or fit.get("kind") != "occurrence":
        return False
    if abs((fit.get("amount") or 0) - bill_amt) > max(DRIFT_MIN, bill_amt * 0.02):
        return True
    rec = raw.get("recurrence") or {}
    cfg_cycle = budget._cycle_days(rec)
    fc = fit.get("cycle_days")
    if fc and cfg_cycle and abs(fc - cfg_cycle) > max(10, cfg_cycle * 0.25):
        return True
    return False


def fit_matches_dismissal(fit: dict | None, stored: dict | None) -> bool:
    """True when a stored 💡-hint dismissal still covers the current ledger
    fit — same kind + cadence, amount within the surface tolerance. A
    materially different fit re-raises the hint."""
    if not fit or not stored:
        return False
    if fit.get("kind") != stored.get("kind"):
        return False
    if (fit.get("cadence") or "") != (stored.get("cadence") or ""):
        return False
    a = float(fit.get("amount") or 0)
    b = float(stored.get("amount") or 0)
    return abs(a - b) <= max(DRIFT_MIN, 0.02 * max(a, b))


def _suggest(health: str, fit: dict | None, cycle: int | None,
             last_seen_date, today: dt.date) -> dict | None:
    """The ONE action the attention queue may offer for this bill, chosen
    by the same evidence that raised the flag — never a guess.

    Thresholds gate the tap: a confident action appears only when the
    ledger is unambiguous (a stable new amount over several charges, or a
    merchant silent past the removal-proposal threshold). Anything weaker
    degrades to "review", which opens the options instead of acting. Both
    clients render this verbatim so the queue can never disagree with the
    server about what one tap does."""
    if health == "stale":
        if (cycle and last_seen_date
                and (today - last_seen_date).days > 2 * cycle + 5):
            return {"action": "disable", "reason": "silent_two_cycles"}
        return {"action": "review", "reason": "missed_one_cycle"}
    if health == "mismatch":
        # the fit's amount is the median of the last three occurrences —
        # offered only when those three AGREE (same tolerance the drift
        # logic uses), so a stable new price qualifies and 120/150/400
        # does not. A fit already implies MIN_HITS occurrences.
        fc = (fit or {}).get("cycle_days")
        # one tap can only change the AMOUNT — if the ledger's cadence
        # moved too (monthly $50 became weekly $18), accepting $18 on a
        # monthly bill would book a fifth of the real cost; that needs the
        # editor, so it degrades to review
        cadence_ok = not (fc and cycle
                          and abs(fc - cycle) > max(10, cycle * 0.25))
        if (fit and fit.get("kind") == "occurrence"
                and fit.get("hits", 0) >= MIN_HITS and cadence_ok
                and fit.get("recent_spread", 0.0)
                <= max(DRIFT_MIN, 0.02 * abs(fit.get("amount") or 0))):
            return {"action": "accept_amount", "amount": fit["amount"],
                    "reason": "amount_moved"}
        return {"action": "review",
                "reason": "cadence_moved" if not cadence_ok
                else "amount_scattered"}
    if health == "drifting":
        return {"action": "review", "reason": "auto_corrects_tonight"}
    if health == "misdated":
        return {"action": "review", "reason": "anchor_off"}
    return None


def _bill_health(b, raw: dict, events: list[dict], today: dt.date) -> dict:
    """Health verdict for one active bill vs the ledger. Stale = 1 missed
    cycle (+ posting grace) — a full cycle of warning before the 2-cycle
    removal proposal fires."""
    bill_amt = abs(b["amount"] or 0)
    bill_type = raw.get("bill_type", "occurrence")
    rec = raw.get("recurrence") or {}
    fit = ledger_fit(events, today)

    if bill_type == "envelope":
        last = events[-1]["date"] if events else None
        if not events:
            health = "off-ledger"
        else:
            live = {_month_key(today), _complete_months(today, 1)[0]}
            health = "ok" if any(_month_key(e["date"]) in live for e in events) else "stale"
        return {"health": health, "last_match": last, "fit": fit,
                "matched": len(events), "fit_diverges": False,
                "cycle_days": None,
                "suggestion": _suggest(health, fit, None,
                                       as_date(last), today)}

    is_income = (b["amount"] or 0) > 0
    if is_income:
        # paychecks drift with raises/bonus-splits and have no auto-correct —
        # a wide band beats nagging 'stale' on a grown check
        amt_matched = [e for e in events
                       if 0.5 * bill_amt <= e["amount"] <= 2 * bill_amt]
    else:
        tol = budget.bill_tolerance(bill_amt)
        amt_matched = [e for e in events if abs(e["amount"] - bill_amt) <= tol]
    last = amt_matched[-1]["date"] if amt_matched else None

    if not rec.get("frequency"):          # one-time scheduled bill
        due = as_date(raw.get("dueOn"))
        if amt_matched or (due and due >= today):
            health = "ok"
        elif due and (today - due).days > 30:
            health = "stale"
        else:
            health = "off-ledger" if not events else "ok"
        return {"health": health, "last_match": last, "fit": fit,
                "matched": len(amt_matched),
                "cycle_days": None,
                "suggestion": _suggest(health, fit, None,
                                       as_date(events[-1]["date"])
                                       if events else None, today),
                "fit_diverges": (not is_income
                                 and _fit_suggests_change(raw, fit, bill_amt))}

    cycle = budget._cycle_days(rec)
    if not amt_matched:
        # merchant IS in the ledger but never near the configured amount —
        # the case the 'ledger says' prefill exists for. With too few
        # charges for a fit there is no prefill; `last_seen` below still
        # tells the person what the ledger DID show.
        health = "mismatch" if events else "off-ledger"
    else:
        # Stale = the MERCHANT went quiet for a full cycle, judged on ANY
        # matched charge — a usage-varying bill is still active even when the
        # latest charge is off-amount; that's drift, not stale.
        last_seen = events[-1]["date"] if events else last
        if (today - last_seen).days > cycle + 5:
            health = "stale"
        elif not is_income and (today - last).days > 2 * cycle + 5:
            # merchant still charging but nothing near the configured amount
            # for 2+ cycles — a permanent price jump beyond the tolerance
            health = "mismatch"
        elif (not is_income and len(amt_matched) >= 3 and abs(
                statistics.median(e["amount"] for e in amt_matched[-3:]) - bill_amt)
                > DRIFT_MIN):
            health = "drifting"           # self-corrects at the nightly pass
        elif not is_income and (
                (off := _phase_off_days(raw, fit, cycle)) is not None
                and off >= PHASE_TOL):
            health = "misdated"           # anchor day drifted off ledger phase
        else:
            health = "ok"
    return {"health": health, "last_match": last, "fit": fit,
            "matched": len(amt_matched),
            # the newest charge the ledger has for this merchant, whatever
            # its amount — so a "mismatch" can say what it saw
            "last_seen": ({"date": events[-1]["date"],
                           "amount": round(events[-1]["amount"], 2)}
                          if events else None),
            "cycle_days": cycle,
            "suggestion": _suggest(health, fit, cycle,
                                   as_date(events[-1]["date"])
                                   if events else None, today),
            "fit_diverges": (not is_income
                             and _fit_suggests_change(raw, fit, bill_amt))}


def analyze_bills(conn, today: dt.date | None = None, *,
                  payee: str | None = None) -> dict[str, dict]:
    """Live per-bill validation for the Bills page health pills.
    `payee` restricts the verdict to that one bill (the history page shows
    a single bill and must not pay for the whole table's matching); the
    ledger universe a bill never looks at is not loaded at all."""
    today = today or localtime.household_day(conn)
    rows = (conn.execute("SELECT * FROM bills WHERE active = 1 AND payee = %s",
                         (payee,)).fetchall() if payee is not None
            else conn.execute("SELECT * FROM bills WHERE active = 1").fetchall())
    # health sees PENDING charges too: a charge that is settling inside the
    # bill's tolerance is evidence the bill is right, not a mismatch to
    # nag about until it posts. Detection (bills.run) stays posted-only —
    # a pending row can still vanish or change amount.
    spend = income = None
    out = {}
    # each bill's merchant identity (the names its rows display under), so
    # a payee the bank renamed keeps matching its own charges
    ids = budget.bill_merchant_ids(conn, rows)
    for b in rows:
        payee_ = b["payee"] or ""
        key = budget._key_token(payee_)
        raw = as_dict(b["raw"])
        if not b["merchant"] and not key:
            continue
        if (b["amount"] or 0) > 0:
            if income is None:
                income = _income_rows(conn, today)
            universe = income
        else:
            if spend is None:
                spend = _ledger_rows(conn, today, include_pending=True)
            universe = spend
        match = budget.merchant_matcher(b["merchant"], key, ids.get(payee_))
        # the rows carry their tokens already — hand them over rather than
        # re-splitting every descriptor once per bill
        matched = [r for r in universe
                   if match(r["text"], r["tokens"], r["merchant_id"])]
        out[payee_] = _bill_health(b, raw, _events(matched), today)
    return out


def _canonical_for(conn, payee: str) -> str | None:
    """The canonical merchant a page-payee refers to: the map's answer for
    that exact raw string when there is one (so a reviewed merge is
    honored), else the deterministic layer-1 form. None when neither
    yields anything usable."""
    try:
        row = conn.execute(
            "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s",
            (payee,)).fetchone()
    except Exception:                                     # noqa: BLE001
        return None
    if row and row["canonical"]:
        return row["canonical"]
    return merchant_dedup.canonical_merchant(
        payee, merchant_dedup.cities_for(conn)) or None


def _merchant_row_matches(row, text: str, want_display: str,
                          display_bucket: bool, want_canon: str | None,
                          arb_toks: set) -> bool:
    """Does this transaction belong to the merchant page for `want_display`?

    One predicate, used by both the row list and the lifetime aggregate, so
    the total can never disagree with the rows it is totalling. `text` is the
    bank's raw descriptor (lowered); `row["payee"]` is the display name.
    """
    if display_bucket:
        return row["payee"] == want_display
    return bool((want_canon and row["canon"] == want_canon)
                or (arb_toks and arb_toks <= budget._tokens(text)))


def _history_match_sql(is_bill: bool, bill_merchant: str | None,
                       key: str | None, want_cat: str | None,
                       want_display: str, display_bucket: bool,
                       want_canon: str | None,
                       arb_toks: set,
                       bill_ids: frozenset | None = None) -> tuple[str, list]:
    """The page's row predicate, in the part of it SQL can decide.

    A SUPERSET of the Python predicate, never a replacement — everything
    this admits is still put to `_merchant_row_matches` (or the bill's own
    matcher) row by row, so the page's answer is unchanged. Its only job is
    to keep the whole ledger out of the worker: without it both scans below
    fetch every non-removed transaction in the tenant and run the tokenizer
    on each one, which is a full table read and hundreds of milliseconds of
    CPU per request on a large ledger.

    The category-pool branch is the one deliberately loose clause: it drops
    the "no other bill already claims this row" half, which subtracts rows
    and so can only ever widen the candidate set.
    """
    cat_frag = cat_params = None
    if want_cat:
        # _cat_norm collapses every non-alnum run, so a row that normalizes
        # to the wanted category must contain its words, in order, separated
        # by something. Matching that as a LIKE is looser than the Python
        # test and cannot exclude a row the Python test would keep.
        cat_frag = ("lower(COALESCE(t.category_override, t.category_primary, "
                    "'?')) LIKE %s")
        cat_params = ["%" + "%".join(want_cat.split()) + "%"]

    if is_bill and bill_ids is not None:
        # a bill with an identity IS its merchants' rows — the SQL side of
        # merchant_matcher's `ids`; an identity resolving to nothing here
        # is no rows at all
        frag, params = (("t.merchant_id::text = ANY(%s)", [sorted(bill_ids)])
                        if bill_ids else ("FALSE", []))
    elif is_bill:
        frag, params = budget.merchant_match_sql(bill_merchant, key)
    elif display_bucket:
        frag, params = f"{merchant_dedup.DISPLAY_MERCHANT} = %s", [want_display]
    else:
        frags, params = [], []
        if want_canon:
            frags.append("mc.canonical = %s")
            params.append(want_canon)
        if arb_toks:
            f, p = budget.tokens_match_sql(arb_toks)
            frags.append(f)
            params += p
        frag = "(" + " OR ".join(frags) + ")" if frags else "FALSE"

    if cat_frag:
        frag = f"({frag} OR {cat_frag})"
        params = list(params) + cat_params
    return frag, list(params)


# the site-wide timeframe vocabulary, as the history page's transaction
# list bounds it: months back from today for the open windows, None for
# all. "cur" (this month so far) and "1m" (the last complete month) are
# calendar months, cut in merchant_history itself.
HISTORY_WINDOWS = {"cur": 1, "1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36,
                   "5y": 60, "all": None}


def merchant_history(conn, payee: str, today: dt.date | None = None,
                     months: int | None = 24, *,
                     window: str | None = None) -> dict:
    """Everything a merchant-history page needs: matched transactions,
    monthly totals, lifetime aggregate, and the ledger fit.

    The transaction list is bounded by `months` back from today (None =
    the whole ledger), or, when `window` names one of HISTORY_WINDOWS, by
    that window: "cur" is this month from the 1st, "1m" the last complete
    month closed on its last day, the rest months back as before. The
    lifetime aggregate is lifetime whatever the window."""
    today = today or localtime.household_day(conn)
    until: dt.date | None = None
    if window is not None:
        months = HISTORY_WINDOWS.get(window)      # unknown reads as all
    if window == "cur":
        since = today.replace(day=1)
    elif window == "1m":
        until = today.replace(day=1)
        since = (until - dt.timedelta(days=1)).replace(day=1)
    else:
        # months=None is the whole ledger: the page's "All" window
        since = (dt.date(1900, 1, 1) if months is None
                 else today - dt.timedelta(days=months * 31))
    row = conn.execute(
        "SELECT merchant, amount, category, raw FROM bills "
        "WHERE payee=%s LIMIT 1", (payee,)).fetchone()
    key = budget._key_token(payee)
    is_bill = row is not None
    bill_merchant = row["merchant"] if is_bill else None
    row_with_payee = {"payee": payee, "raw": row["raw"]} if is_bill else None
    # category-pool envelope (match_category): rows of the bill's effective
    # category belong to it even when their merchants share nothing —
    # that is the whole point of the pool (cash withdrawals, payment-app
    # counterparties). Mirror what actually claims a row ahead of the
    # category tier: another ENVELOPE's merchant match always wins
    # (_claim_envelope), while an occurrence bill only takes the charge it
    # is actually the payment for — inside its amount tolerance. Without
    # that amount gate a $600 one-off to a $200/mo payee would vanish from
    # this page while the engine still spent it out of the pool.
    want_cat = None
    others = []
    if is_bill:
        raw = as_dict(row["raw"])
        if (raw.get("bill_type") == "envelope" and raw.get("match_category")
                and row["category"]):
            want_cat = budget._cat_norm(row["category"])
            for b in conn.execute(
                    "SELECT payee, merchant, amount, raw FROM bills "
                    "WHERE active=1 AND amount<0 AND payee!=%s",
                    (payee,)).fetchall():
                amt = abs(b["amount"] or 0)
                others.append((
                    budget.merchant_matcher(b["merchant"],
                                            budget._key_token(b["payee"] or "")),
                    as_dict(b["raw"]).get("bill_type") == "envelope",
                    amt, budget.bill_tolerance(amt)))
    # arbitrary merchant clicked from the Transactions page — `payee` IS a
    # real merchant name, so match on its full token set
    arb_toks = budget._tokens(payee) or ({key} if key else set())
    # one compiled matcher for the bill, not one per ledger row — carrying
    # the bill's merchant identity, so the page lists the charge the bank
    # renamed exactly as the Bills page counts it
    bill_ids = (budget.bill_merchant_ids(conn, [row_with_payee]).get(payee)
                if is_bill else None)
    bill_match = (budget.merchant_matcher(bill_merchant, key, bill_ids)
                  if is_bill else None)
    # Group by the CANONICAL merchant, not the raw descriptor. One payee
    # reaches us under several strings — a feed that sends no merchant at all
    # (the raw bank descriptor, street address and all), a feed that truncates
    # it ('Example Merchant Na'), and a clean one — and the dedup map
    # already knows they are the same business. Matching raw text alone
    # would ignore those merges and list the same merchant as three
    # different histories. Token matching stays as the fallback for
    # anything with no canonical row.
    #
    # The first test is the DISPLAY key itself (merchant_dedup.DISPLAY_MERCHANT
    # over MC_JOIN — the same fragments the ledger, the Merchants catalog and
    # the bulk-recategorize preview render). Both clients link here with
    # exactly the string the ledger printed, and two kinds of merchant cannot
    # be looked up by raw text alone: an OUTLET row (a chain's fuel arm,
    # which the map cannot key because "Costco" is the pump on one row and the
    # warehouse on the next), and a rename whose words are not a token subset
    # of the bank's raw text ("The UPS Store" over "UPS STORE #1234"). Both
    # would return an empty page for a name the ledger had just displayed.
    #
    # And when the name IS a live display bucket, that bucket is the whole
    # answer: the page must show exactly the rows the Merchants catalog counts
    # under it, no more. The token fallback alone cannot say that — asked for
    # "Costco" it also sweeps in every "Costco Gas" pump row, so the history
    # and the catalog would disagree about the same merchant in the other
    # direction. The fallback stays for names that display nowhere: bill
    # payees, older links, and raw descriptors typed or stored before a merge.
    want_display = payee
    # a BILL page is matched by the bill's own matcher alone (below), so the
    # display-bucket probe and the canonical lookup — each a pass over the
    # whole ledger — are only asked for a merchant page
    display_bucket = (False if is_bill
                      else merchant_dedup.display_in_use(conn, payee))
    want_canon = (None if is_bill or display_bucket
                  else _canonical_for(conn, payee))
    # Narrow in SQL what SQL can decide. The Python predicate below still has
    # the last word — this only decides which rows are worth carrying into the
    # worker and tokenizing, and it is a superset of that predicate, so the
    # fuzzy matching that IS the product behaviour is untouched.
    match_frag, match_params = _history_match_sql(
        is_bill, bill_merchant, key, want_cat, want_display, display_bucket,
        want_canon, arb_toks, bill_ids)
    txns = []
    for r in conn.execute(
            f"""SELECT t.id AS txn_id, t.date, t.amount, t.pending,
                      t.merchant_id::text AS merchant_id,
                      {merchant_dedup.DISPLAY_MERCHANT} AS payee,
                      -- what the bank literally sent, kept beside the display
                      -- name: bill matching is defined on the raw descriptor's
                      -- tokens, so it must not start reading a renamed label
                      {budget.MATCH_PAYEE_SQL} AS raw_payee, t.name,
                      mc.canonical AS canon, mm.logo_url AS merchant_logo,
                      REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ') AS category,
                      tn.note AS note,
                      TRIM(COALESCE(i.institution_name,'') || ' ' ||
                           COALESCE(a.display_name, a.name, '')) ||
                        CASE WHEN COALESCE(a.mask,'') != '' THEN ' ' || a.mask ELSE '' END
                        AS account
               FROM transactions t JOIN accounts a ON a.id = t.account_id
                    LEFT JOIN items i ON i.id = a.item_id
                    {merchant_dedup.MC_JOIN}
                    -- the note must not be a CORRELATED subquery: Postgres
                    -- runs one seq scan of transaction_notes PER
                    -- TRANSACTION ROW for that shape, which looks free
                    -- while the table is nearly empty and is
                    -- O(rows x notes) once a ledger is annotated.
                    -- A LEFT JOIN scans it once.
                    LEFT JOIN transaction_notes tn ON tn.txn_id = t.id
               WHERE t.removed = 0 AND t.date >= %s AND t.date < %s
                 AND {match_frag}
               -- id breaks the date tie: without it two charges on one day
               -- come back in whatever order the plan happened to produce,
               -- so the same page reorders itself between requests
               ORDER BY t.date DESC, t.id DESC""",
            (since, until or dt.date(9999, 1, 1), *match_params)).fetchall():
        text = budget.match_text(r["raw_payee"], r["name"])
        ok = (bill_match(text, None, r["merchant_id"]) if is_bill
              else _merchant_row_matches(r, text, want_display, display_bucket,
                                         want_canon, arb_toks))
        if not ok and want_cat:
            ok = (budget._cat_norm(r["category"]) == want_cat
                  and not any(m(text) and (env or abs(r["amount"] - amt) <= tol)
                              for m, env, amt, tol in others))
        if ok:
            txns.append(r)
    events = _events([{"date": as_date(r["date"]),
                       "amount": r["amount"], "payee": r["payee"] or "?",
                       "category": r["category"], "is_food": False}
                      for r in txns if r["amount"] > 0 and not r["pending"]])

    # direction: an income series (or an inflow-dominant merchant) aggregates
    # its INFLOWS — but transfers/CC-credits are the user's own money and
    # must not count in the majority vote or the displayed events
    def _real_inflow(r):
        cat = (r["category"] or "").replace("_", " ")
        return (r["amount"] < 0 and not r["pending"]
                and "TRANSFER" not in cat and "LOAN PAYMENTS" not in cat)

    inflow = ((is_bill and (row["amount"] or 0) > 0)
              or (not is_bill
                  and sum(1 for r in txns if _real_inflow(r))
                  > sum(1 for r in txns if r["amount"] > 0 and not r["pending"])))
    disp_events = (_events([{"date": as_date(r["date"]),
                             "amount": -r["amount"], "payee": r["payee"] or "?",
                             "category": r["category"], "is_food": False}
                            for r in txns if _real_inflow(r)])
                   if inflow else events)
    # The CURRENT month belongs in the table too — otherwise a bill already
    # paid this month is invisible in the monthly totals while the txn list
    # and lifetime total both show the charge
    months12 = _complete_months(today, 12) + [_month_key(today.replace(day=1))]
    totals, counts = _monthly_totals(disp_events, months12)
    monthly = [{"month": m, "total": round(t, 2), "count": c}
               for m, t, c in zip(months12, totals, counts) if c or t]
    # lifetime aggregate — ALL history, project spend definition (without it,
    # a transfer/card-payoff payee reads the user's own money movement as
    # hundreds of thousands 'spent all-time')
    life_total = 0.0
    life_count = 0
    life_first = life_last = None
    life_months: set = set()
    amount_side = "amount < 0" if inflow else "amount > 0"
    # a BILL is matched on the raw descriptor and the display name (its
    # merchant identity); a merchant page needs the display name and the
    # canonical for the display-first rule. Both join the merchant.
    if is_bill:
        life_cols = f"{merchant_dedup.DISPLAY_MERCHANT} AS payee, NULL AS canon"
    else:
        life_cols = (f"{merchant_dedup.DISPLAY_MERCHANT} AS payee, "
                     "mc.canonical AS canon")
    life_join = merchant_dedup.MC_JOIN
    for r in conn.execute(
            f"""SELECT t.date, t.amount, t.pending, {life_cols},
                      t.merchant_id::text AS merchant_id,
                      {budget.MATCH_PAYEE_SQL} AS raw_payee, t.name,
                      REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ') AS category
               FROM transactions t
                    {life_join}
               WHERE t.removed = 0 AND t.{amount_side} AND t.pending = 0
                 AND COALESCE(t.category_detailed,'') != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
                 AND COALESCE(t.category_override, t.category_primary, '')
                     NOT IN ('TRANSFER_IN','TRANSFER_OUT')
                 -- the lifetime aggregate is lifetime BY DEFINITION, so it
                 -- cannot take a date bound; the merchant predicate is what
                 -- keeps it off the rest of the ledger
                 AND {match_frag}""", match_params or None).fetchall():
        text = budget.match_text(r["raw_payee"], r["name"])
        # same display-first rule as the txn list above: the lifetime
        # aggregate sits beside it, so a different matcher here would print
        # a total that disagrees with the rows it is totalling
        ok = (bill_match(text, None, r["merchant_id"]) if is_bill
              else _merchant_row_matches(r, text, want_display, display_bucket,
                                         want_canon, arb_toks))
        if not ok and want_cat:
            ok = (budget._cat_norm(r["category"]) == want_cat
                  and not any(m(text) and (env or abs(r["amount"] - amt) <= tol)
                              for m, env, amt, tol in others))
        if ok:
            life_total += abs(r["amount"])
            life_count += 1
            life_months.add((r["date"].year, r["date"].month))
            if life_first is None or r["date"] < life_first:
                life_first = r["date"]
            if life_last is None or r["date"] > life_last:
                life_last = r["date"]
    # mean over months WITH activity, not the whole span — a
    # domain that renews twice a year must not read as "$1.50/mo" because
    # ten empty months watered it down
    lifetime = {"total": round(life_total, 2), "count": life_count,
                "first": life_first, "last": life_last,
                "active_months": len(life_months),
                "avg_monthly_active": (round(life_total / len(life_months), 2)
                                       if life_months else 0.0)}
    # the ledger fit stays SPEND-only: an income page must never grow a
    # "make this an envelope" hint from paycheck deposits
    logo = next((r["merchant_logo"] for r in txns if r["merchant_logo"]), None)
    return {"txns": txns, "monthly": list(reversed(monthly)), "inflow": inflow,
            "lifetime": lifetime, "fit": ledger_fit(events, today),
            "logo": logo}


def _apply_drift(conn, bill_row, new_amt: float, evidence: dict, stats,
                 today: dt.date | None = None):
    """Auto-update a bill's amount (no approval needed) and
    leave an `auto` audit row for the email digest."""
    today = today or localtime.household_day(conn)
    old = abs(bill_row["amount"] or 0)
    # a $0 row must not flip to +amount (the income convention), which
    # would make it invisible everywhere; bills are negative unless the row
    # is genuinely income (strictly positive)
    sign = 1 if (bill_row["amount"] or 0) > 0 else -1
    if old:
        monthly = (bill_row["monthly_amount"] or 0) * (new_amt / old)
    else:
        # $0 legacy row: recompute from the recurrence instead of ratio
        rec = as_dict(bill_row["raw"]).get("recurrence") or {}
        monthly = sign * _monthly(new_amt, rec.get("frequency"),
                                  rec.get("interval") or 1)
    # the drift IS an amount-set: the next drift needs an occurrence after
    # today, not a re-read of the same three
    conn.execute(
        "UPDATE bills SET amount=%s, monthly_amount=%s, synced_at=now(), "
        "raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb WHERE id=%s",
        (sign * new_amt, monthly,
         jsonb({"amount_set_on": today.isoformat()}),
         bill_row["id"]))
    evidence = dict(evidence, old=round(old, 2), new=round(new_amt, 2))
    pid = _sig("drift", bill_row["id"], round(new_amt, 2),
               today.isoformat())
    if _insert_proposal(conn, pid=pid, kind="amount_drift", payee=bill_row["payee"],
                        amount=new_amt, evidence=evidence, status="auto"):
        stats["drift_applied"] += 1
