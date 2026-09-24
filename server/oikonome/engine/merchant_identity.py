"""The ONE owner of merchant identity: which merchant a transaction belongs to.

A merchant is a row in `merchants` (migration 097) — Plaid's stable entity
id, a display name, a logo, a website, a kind — and every transaction
carries `merchant_id`. Identity is RESOLVED ONCE, here, and READ by joining
the id; no consumer keys on a raw string. Re-deriving the display name from
the descriptor at read time — in a dozen places, each with its own idea of
the key — is what makes one row show different names on different
screens.

Precedence, highest first:

  1. a person's rename/merge — merchant_canonical rows with method='manual'
     (their canonical is the name they chose; it outranks everything and
     survives every sync);
  2. Plaid's own resolution — the identity counterparty (highest-confidence
     counterparty that HAS an entity id and is a merchant / marketplace /
     delivery service / income source), else `merchant_entity_id`. A
     payment app or terminal in front of the merchant is context ("via
     PayPal"), not identity; a lone financial institution IS the identity
     (transfers);
  3. an LLM merge (method='llm') — a reviewed raw → name map;
  4. the layer-1 string clean (merchant_dedup.canonical_merchant) — the
     fallback for history that no aggregator resolved.

A non-Plaid row whose layer-1 name equals an existing merchant's name
(case-insensitively, exactly — never fuzzy) attaches to that merchant, so
years of imported "Costco" rows inherit the Plaid Costco logo. Fuzzy
matching stays with the LLM layer and with people.

`merchant_canonical` is the alias table under all this: (raw key → merchant
id), with `canonical` kept as a mirror of merchants.name so readers that
read the string keep working. The raw key is spelled once, in
merchant_sql.raw_key, and read the same way by every module: the outlet,
else the aggregator's merchant name, else the bank's descriptor.

Raw key → merchant is a FUNCTION, and everything downstream relies on it:
a rename collects a merchant's raw keys and maps them, reconcile_raws
moves rows by key, the split repair reasons about keys. A row filed
against a key it does not carry would be dragged about by renames of a
payee it has nothing to do with — which is why a bank line outranking an
aggregator's name (see `_descriptor_overrules_enrichment`) MOVES that name
to transactions.merchant_name_set_aside rather than leaving a special case
behind. The row then simply has no aggregator name, which every reader
already understands. See specs/merchant-identity.md.

Shadow accounts (the non-primary side of a multi-source link, and hidden
accounts — see engine/links.py) are deliberately NOT excluded anywhere in
this module, which is the opposite of the rule every money aggregate
follows. Nothing here sums or displays a count: identity is a property of
a ROW, and a shadow row is a real row that must carry a correct
merchant_id — its source becomes the primary the moment the current
primary's aggregator fails, and the ledger renders it with no further
resolve pass. Excluding shadows would leave those rows unresolved (blank
merchant, no logo) exactly when they start being shown, and would make the
liveness tests below delete a merchant that shadow rows still point at.
The per-site reasoning is repeated at each query.
"""
from __future__ import annotations

import logging

from . import merchant_dedup, merchant_sql

log = logging.getLogger(__name__)

# Advisory-lock namespace for merchant identity (arbitrary constant; it is
# the first half of a (space, household) pair, and the two-argument form is
# a lock space of its own, so it can collide with nothing else — neither
# with the per-name locks _create takes in the one-argument space, nor with
# the other two-argument spaces this codebase uses).
_IDENTITY_LOCK_SPACE = 74210094


def _lock_identity(conn) -> None:
    """Serialize merchant-identity writes for one household.

    Two passes over one ledger — the ingest hook for a freshly synced row
    beside the nightly sweep, or two connections syncing one institution —
    run in their own transactions, and under READ COMMITTED neither can see
    a merchant_id the other has written and not yet committed. Every "has
    anything already decided this?" question below then answers "no" in
    both passes, and both mint: two merchants for one payee, permanently,
    because a resolved row is never revisited.

    A lock per decision cannot close that. The question one pass asks is
    about rows the OTHER pass keyed differently — a tidy enrichment string
    against a raw bank descriptor — so there is no single key both would
    take; and a backfill over a whole ledger has tens of thousands of
    distinct keys, which as transaction-scoped locks would exhaust the
    shared lock table. One lock for the household is bounded and covers
    every pair of passes. It is taken before the rows are read, so the
    loser also re-reads, and finds the winner's rows already resolved
    rather than writing over them.

    It is always the FIRST lock this module takes — before any per-name
    lock in _create, and before the renames in reconcile_raws take theirs.
    That fixed order is what keeps the two kinds of lock from deadlocking:
    a transaction waiting for this one holds no name lock.
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext("
        "    coalesce(current_setting('app.tenant_id', true), '')))",
        (_IDENTITY_LOCK_SPACE,))


# counterparty types that ARE the merchant vs. those that only sit in front
IDENTITY_KINDS = ("merchant", "marketplace", "delivery_service", "income_source")
CONTEXT_KINDS = ("payment_app", "payment_terminal")
_CONF_RANK = {"VERY_HIGH": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}


def plaid_identity(raw: dict | None) -> dict | None:
    """Pick the identity out of a Plaid transaction's raw record.

    Returns {entity_id, name, kind, logo_url, website, processor} or None
    when Plaid resolved nothing (no entity id anywhere)."""
    if not isinstance(raw, dict):
        return None
    cps = raw.get("counterparties") or []
    best = None
    processor = None
    fin = None
    for c in cps:
        if not isinstance(c, dict):
            continue
        kind = c.get("type") or "other"
        if kind in CONTEXT_KINDS:
            processor = processor or c.get("name")
            continue
        if kind == "financial_institution":
            fin = fin or c
            continue
        if not c.get("entity_id"):
            continue
        if kind not in IDENTITY_KINDS:
            continue
        rank = _CONF_RANK.get(c.get("confidence_level") or "UNKNOWN", 0)
        if best is None or rank > best[0]:
            best = (rank, c)
    if best is not None:
        c = best[1]
        return {"entity_id": c["entity_id"], "name": c.get("name"),
                "kind": c.get("type") or "merchant",
                "logo_url": c.get("logo_url") or raw.get("logo_url"),
                "website": c.get("website") or raw.get("website"),
                "processor": processor}
    if raw.get("merchant_entity_id"):
        return {"entity_id": raw["merchant_entity_id"],
                "name": raw.get("merchant_name") or raw.get("name"),
                "kind": "merchant", "logo_url": raw.get("logo_url"),
                "website": raw.get("website"), "processor": processor}
    # a lone financial institution is the identity only when Plaid gave no
    # merchant name at all (a card payment, a bank transfer) — a P2P row
    # ("Zelle — Casey Example") names its counterparty and must keep it
    if fin is not None and fin.get("entity_id") and not raw.get("merchant_name"):
        return {"entity_id": fin["entity_id"], "name": fin.get("name"),
                "kind": "financial_institution",
                "logo_url": fin.get("logo_url"), "website": fin.get("website"),
                "processor": processor}
    return None


def _find_by_name(conn, name: str):
    row = conn.execute(
        "SELECT id FROM merchants WHERE lower(name) = lower(%s) "
        "AND merged_into IS NULL ORDER BY (plaid_entity_id IS NULL), created_at "
        "LIMIT 1", (name,)).fetchone()
    return row["id"] if row else None


# A merged-away merchant points at its survivor, and a survivor can itself
# be merged later — so `merged_into` is a chain, not a single hop. Follow it
# to the end, bounded, and stop on a repeat: a restored archive can carry a
# cycle (A→B→A), and an unbounded walk would hang the nightly pass.
_MERGE_HOPS = 16


def _survivor(conn, mid):
    """The live merchant at the end of a merged-into chain.

    Returns None when the id names no merchant at all — an alias restored
    from an archive that did not carry its merchant, say. Callers treat
    that as "no id" and resolve by name instead; reading the name off a
    row that isn't there would crash the whole pass."""
    seen: set = set()
    cur = mid
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = conn.execute(
            "SELECT merged_into FROM merchants WHERE id=%s", (cur,)).fetchone()
        if row is None:
            return None
        nxt = row["merged_into"]
        if nxt is None or nxt in seen or len(seen) >= _MERGE_HOPS:
            return cur
        cur = nxt
    return cur


def _create(conn, name: str, source: str, *, entity=None, kind="merchant",
            logo=None, website=None):
    if entity is None:
        # No unique index guards a name-only merchant, and two importers
        # (a statement upload beside the hourly sync, a restore beside a
        # webhook) can both find nothing and both insert — two rows for one
        # name, the very thing the resolver exists to prevent. Serialise
        # on (tenant, name) and look again under the lock; the connection
        # is autocommit, so the lock needs its own transaction.
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("oikonome:merchant:" + name.lower(),))
            again = _find_by_name(conn, name)
            if again is not None:
                return again
            row = conn.execute(
                """INSERT INTO merchants (name, name_source, kind, logo_url,
                                          website)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                (name, source, kind, logo, website)).fetchone()
            return row["id"]
    # Two syncs resolving the same new Plaid entity at once both find no
    # merchant and both insert; the unique index on (tenant_id,
    # plaid_entity_id) makes one of them the loser. The loser adopts the
    # winner's row rather than raising and aborting the whole pass.
    row = conn.execute(
        """INSERT INTO merchants (plaid_entity_id, name, name_source, kind,
                                  logo_url, website)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id, plaid_entity_id)
               WHERE plaid_entity_id IS NOT NULL DO NOTHING
           RETURNING id""",
        (entity, name, source, kind, logo, website)).fetchone()
    if row is not None:
        return row["id"]
    won = conn.execute("SELECT id FROM merchants WHERE plaid_entity_id=%s",
                       (entity,)).fetchone()
    return _survivor(conn, won["id"])


def _upsert_plaid_merchant(conn, ident: dict, cache: dict):
    """The merchant for a Plaid entity id — created on first sight with
    Plaid's name/logo/website/kind. The NAME is set only on create: a person's
    rename (name_source manual) must survive every later sync; logo/website
    are refreshed when Plaid has them and we don't."""
    eid = ident["entity_id"]
    if eid in cache:
        return cache[eid]
    row = conn.execute(
        "SELECT id, merged_into FROM merchants WHERE plaid_entity_id=%s",
        (eid,)).fetchone()
    if row:
        # to the END of the merge chain: B merged into C after A merged
        # into B leaves A pointing two hops from the live merchant
        mid = _survivor(conn, row["id"]) or row["id"]
        conn.execute(
            """UPDATE merchants SET logo_url = COALESCE(logo_url, %s),
                                    website = COALESCE(website, %s),
                                    updated_at = now()
                WHERE id=%s AND (logo_url IS NULL OR website IS NULL)""",
            (ident.get("logo_url"), ident.get("website"), mid))
    else:
        name = (ident.get("name") or "").strip() or "?"
        # a merchant of the same name that has no entity yet becomes THIS
        # merchant: adopt it rather than create a twin, so the history
        # unifies under Plaid's identity and gets the logo. A layer-1 row
        # (older imported rows) is taken over outright; a MANUAL row — the
        # person already named a merchant exactly what Plaid now calls this
        # entity — keeps its name and its manual standing (a rename must
        # survive every sync), and only gains the entity, logo and site.
        # Without the manual case the aggregator resolving a payee AFTER a
        # person merged its spellings would mint a second merchant with the
        # same name, and every surface that groups by name would hide the
        # split while everything keyed on the row saw two merchants.
        existing = conn.execute(
            "SELECT id, name_source FROM merchants WHERE lower(name)=lower(%s) "
            "AND plaid_entity_id IS NULL AND merged_into IS NULL "
            "ORDER BY (name_source = 'manual') DESC, created_at LIMIT 1",
            (name,)).fetchone()
        if existing:
            mid = existing["id"]
            conn.execute(
                """UPDATE merchants SET plaid_entity_id=%s,
                          name_source=CASE WHEN name_source='manual'
                                           THEN 'manual' ELSE 'plaid' END,
                          kind=%s, logo_url=COALESCE(logo_url,%s),
                          website=COALESCE(website,%s), updated_at=now()
                    WHERE id=%s""",
                (eid, ident.get("kind") or "merchant", ident.get("logo_url"),
                 ident.get("website"), mid))
        else:
            mid = _create(conn, name, "plaid", entity=eid,
                          kind=ident.get("kind") or "merchant",
                          logo=ident.get("logo_url"), website=ident.get("website"))
    cache[eid] = mid
    return mid


def _abbreviates_plaid_merchant(conn, name: str):
    """The Plaid-named merchant a bank descriptor abbreviates, or None.

    Two feeds can carry the same payee in two shapes: Plaid resolves
    "NORTHWIND *INSURANCE" to its entity "Northwind Insurance"; a
    statement import carries "NORTHWIND INS SPRINGFIELD USA", which the
    layer-1 clean leaves as "Northwind Ins Springfield Usa" (the city is
    not in the harvested list). Exact-name matching alone would mint a
    second merchant, and the household would see two names, one with a
    logo, for one insurer. The rule: the descriptor's first token equals
    the Plaid name's first token, and every further Plaid-name token is
    begun by the
    descriptor's next token ("Ins" → "Insurance", "Am" → "America"), the
    descriptor may run on past the name (a location tail), and at least
    two tokens must match. Only against Plaid-named merchants — they carry
    the authority (entity id, logo) that makes adopting them safe.

    Two letters are enough for that first token: a name can BEGIN with an
    initialism ('QX Bank Springfield' beside Plaid's 'QX Bank'), and the
    string clean keeps those, so a three-letter floor here would leave exactly
    those descriptors minting a second merchant next to the Plaid one. A
    lone letter is still too little to match on."""
    toks = [t for t in name.lower().replace("*", " ").split() if t.isalnum()]
    if len(toks) < 2 or not toks[0].isalpha() or len(toks[0]) < 2:
        return None
    cands = conn.execute(
        """SELECT id, name FROM merchants
            WHERE name_source = 'plaid' AND merged_into IS NULL
              AND lower(split_part(name, ' ', 1)) = %s""",
        (toks[0],)).fetchall()
    best, best_n = None, 1
    for c in cands:
        ptoks = c["name"].lower().split()
        if len(ptoks) < 2 or len(toks) < len(ptoks):
            continue
        if all(toks[i] == ptoks[i] or
               (len(toks[i]) >= 2 and ptoks[i].startswith(toks[i]))
               for i in range(len(ptoks))) and len(ptoks) > best_n:
            best, best_n = c["id"], len(ptoks)
    return best


# Where a label ends, for every word comparison below — the same cut the
# string clean makes, because the two read the same labels and a name they
# disagreed about the length of would clean to one thing and match as
# another. A bank descriptor is a few dozen characters; the strings here can
# also come from a file import mapping two arbitrary text columns onto
# `name` and `merchant_name`, or from a restored archive, and every question
# below compares each word of one string against every word of the other —
# quadratic, inside the household's identity lock, so an uncut pair of long
# labels would hold that lock for as long as it liked. Nothing a real payee
# name or bank line carries lives past the cut.
_MAX_LABEL = merchant_dedup._MAX_LABEL


def _words(s: str) -> list[str]:
    """The alphanumeric words of a string, lowercased: "SQ *EXAMPLE CO #12"
    becomes ["sq", "example", "co", "12"]. An apostrophe breaks a word, so
    this is the SPLIT reading of a possessive — "Kohl's" becomes
    ["kohl", "s"]. `_joined_words` is the other reading."""
    s = (s or "")[:_MAX_LABEL]
    return "".join(c if c.isalnum() else " " for c in s.lower()).split()


# the three characters a feed may write an apostrophe with: ASCII, the
# typographic right single quote, and the modifier letter a few sources use
_APOSTROPHES = "'’ʼ"


def _joined_words(s: str) -> list[str]:
    """The words of a string with apostrophes DELETED rather than broken on:
    "Kohl's" becomes ["kohls"], "O'Reilly Auto" becomes ["oreilly", "auto"]."""
    s = (s or "")[:_MAX_LABEL]
    return _words("".join(c for c in s if c not in _APOSTROPHES))


def _descriptor_words(descriptor: str) -> set[str]:
    """Every word a bank line offers, under BOTH readings of an apostrophe.

    A line is printed either way and we cannot tell which: one bank writes
    "KOHLS", the next "KOHL S", a third "O REILLY AUTO"
    for a name the aggregator spells "O'Reilly". Taking the union means a
    name matches whichever way its line happened to be printed."""
    return set(_words(descriptor)) | set(_joined_words(descriptor))


def _name_readings(merchant: str) -> list[list[str]]:
    """The payee name as words, joined reading first, then split.

    Both are needed, and neither alone will do. Deleting the apostrophe
    reads "Kohl's" as one word, which is what a line printing "KOHLS" says;
    breaking on it reads "O'Reilly" as ["o", "reilly"], which is what a line
    printing "O REILLY" — or one that elides the prefix altogether — says.
    A name matches when EITHER reading of it does."""
    joined, split = _joined_words(merchant), _words(merchant)
    return [joined] if joined == split else [joined, split]


def _descriptor_names(descriptor: str, merchant: str) -> bool:
    """Does this bank descriptor actually NAME this payee?

    A descriptor that is not payee-distinctive must never be allowed to
    name a payee. Refusing one that has been seen pointing at two merchants
    only fires after the harm is done: the FIRST unenriched row to arrive
    under a bank's constant — a terminal id, a processor prefix with
    nothing after it, "purchase authorized on" — would adopt whichever
    payee happened to be there already, and adoption writes an alias, which
    every later pass consults before anything else. A wrong answer of that
    shape makes itself authoritative and there is nothing left in the data
    to unwind it by.

    A list of known-meaningless strings would rot: every bank writes its
    own, and the one that matters is always the one nobody listed. The
    evidence is in the two strings themselves instead. A descriptor names
    the payee only when the payee's name is IN it: every word of the
    merchant name is a word of the descriptor, either side allowed to be
    the other's abbreviation ("ins" for "insurance", "amer" for
    "america"), and at least one real word — three letters, not a store
    number — carrying the match. The descriptor may run on past the name;
    that tail is the city, the terminal, the country.

    A possessive is read both ways on both sides (_name_readings,
    _descriptor_words). Read only as a break, "Kohl's" wants an "s" from a
    line reading "KOHLS" and nothing in a bank descriptor can ever supply
    it; read only as a deletion, "O'Reilly" becomes "oreilly" and no longer
    matches the line that prints the prefix as a word of its own, nor the
    name a household wrote without it. Either reading matching is enough:
    the two describe one payee, and which one a feed used is not evidence
    about anything.
    """
    dwords = _descriptor_words(descriptor)
    for mwords in _name_readings(merchant):
        if not mwords:
            continue
        strong = 0
        for m in mwords:
            if not any(d == m or (len(d) >= 3 and m.startswith(d))
                       or (len(m) >= 3 and d.startswith(m)) for d in dwords):
                break
            if len(m) >= 3 and m.isalpha():
                strong += 1
        else:
            if strong >= 1:
                return True
    return False


def _merchant_by_bank_descriptor(conn, bank_name: str, asking=()):
    """The merchant other rows carrying this exact bank descriptor already got.

    `asking` is the group being decided, and it is excluded from every
    question below — OTHER rows are the evidence, and a group asked about
    twice must not be able to cite its own last answer. It can: a re-resolve
    revisits rows that already carry a merchant, and counting them makes
    the descriptor confirm whatever was decided before, for good. That is
    how a row would sit under a name from an older, worse string clean
    forever, when moving it to the name the clean now makes is the whole
    point of the nightly recompute.

    The decision key is the row's identity key (merchant_sql.raw_key), so the
    same purchase keys differently depending on whether the aggregator
    enriched that particular row: an enriched charge keys on the tidy
    merchant string, while its unenriched twin keys on the raw descriptor,
    location tail and all. Two keys mean two independent decisions and two
    merchants for one payee, and the alias memory cannot bridge them because
    it is stored under the key that differs. Refunds and late-arriving rows
    are where enrichment is missing most often, which is where a household
    sees the split.

    The bank's own descriptor is the one string both rows do share, and two
    payees never share a full descriptor, so it is a safe second key. It is
    consulted only when nothing stronger has spoken — no Plaid entity, no
    manual answer, no reviewed alias — so it can never overrule authority it
    should be taking from."""
    if not (bank_name or "").strip():
        return None
    # The aggregator's own opinion first, where it gave one: two DIFFERENT
    # enrichment strings under one descriptor is the feed saying, in its
    # own words, that the descriptor does not identify a payee. This sees
    # evidence the merchant test below cannot — an enriched sibling still
    # unresolved in this very pass — so it can refuse before a first wrong
    # pairing rather than only after a second one.
    #
    # A row whose name was SET ASIDE is not such a second opinion: the
    # ledger has already judged that name a misreading of this very line.
    # Counting it would let one stray guess switch the descriptor off for
    # every later row that carries no name at all — and it cannot be
    # counted, because the name is no longer in merchant_name; the row is
    # excluded by the same test that excludes a row nobody named.
    others = list(asking)
    named = conn.execute(
        """SELECT DISTINCT lower(t.merchant_name) AS n
             FROM transactions t
            WHERE t.name = %s AND t.merchant_name IS NOT NULL
              AND t.removed = 0 AND t.id <> ALL(%s)
            LIMIT 3""",
        (bank_name, others)).fetchall()
    if len(named) > 1:
        return None
    # Only when the descriptor has resolved to ONE payee so far. Some banks
    # write a constant into this field — "POS DEBIT PURCHASE", "Withdrawal" —
    # and carry the real payee only in the enrichment; adopting the majority
    # answer there would fold every unenriched row of the account into
    # whichever merchant happened to be ahead. A descriptor already pointing
    # at two merchants is evidence it does not name a payee, so it is not
    # allowed to name one now. Two rows whose merchants have since been
    # merged still count as one, which is why the survivors are compared
    # rather than the stored ids.
    rows = conn.execute(
        """SELECT DISTINCT t.merchant_id AS mid
             FROM transactions t
            WHERE t.name = %s AND t.merchant_id IS NOT NULL AND t.removed = 0
              AND t.id <> ALL(%s)
            LIMIT 5""",
        (bank_name, others)).fetchall()
    if not rows:
        return None
    survivors = {_survivor(conn, r["mid"]) for r in rows}
    survivors.discard(None)
    if len(survivors) != 1:
        return None
    mid = next(iter(survivors))
    # and only when the descriptor names that payee — see _descriptor_names
    got = conn.execute("SELECT name FROM merchants WHERE id=%s",
                       (mid,)).fetchone()
    if got is None or not _descriptor_names(bank_name, got["name"]):
        return None
    return mid


# how many times a bank line and the aggregator must already have AGREED on
# one payee before the line may overrule the aggregator — a habit, not a
# coincidence
ESTABLISHED_ROWS = 5


def _descriptor_overrules_enrichment(conn, bank_name: str, enriched: str):
    """The merchant a bank descriptor and the aggregator have agreed on,
    when the aggregator has just called that same line something else.

    An aggregator's name with no entity id behind it is a guess, and it can
    guess differently for the same terminal on different days, so one charge
    in a hundred under an unchanged line can come back under an unrelated
    name. Taken at its word, that row mints a second merchant and
    the household is asked to merge it — for a line it has seen a hundred
    times. The descriptor is the better witness there, but only on strict
    terms, each one a reason to refuse:

      - the descriptor must NOT contain the new name. When it does, the
        aggregator read the line and its answer stands;
      - nor may the new name contain the descriptor. A deposit line with a
        payer's name appended to it is the aggregator ADDING detail to a
        bank's constant, not misreading a payee;
      - and the line must have a HABIT: ESTABLISHED_ROWS rows under this
        exact descriptor where the aggregator named the payee and the line
        NAMES that name — the two sources agreeing, over and over, about
        who this is — resolved to one ordinary merchant (survivors
        compared, so a merge does not read as disagreement).

    Agreement, rather than the mere presence of rows, is what makes the
    answer both trustworthy and stable. A bank's constant ("POS DEBIT
    PURCHASE") mints a merchant named after itself out of its own unnamed
    rows, and a rule asking only "does the line name the merchant it has
    carried so far" is satisfied by that trivially — every payee under the
    constant could then be folded into the first one. And rows the
    aggregator never named settle LATER in a pass than this question is
    asked, so a rule that counted them would answer differently for a whole
    ledger than for one row at a time. Agreeing rows are never doubted
    themselves — their own line names their own name — so they are settled
    before any verdict is reached, and both passes see the same evidence.

    The caller writes no alias for what this answers: the alias key would
    be the aggregator's name, which says nothing about the descriptor, and
    a real business of that name arriving later under its own descriptor
    must not inherit the answer. It takes the name off the moved rows
    instead, which is what ends their claim on it for good, and aliases the
    DESCRIPTOR — the key they carry from then on."""
    bank, enriched = (bank_name or "").strip(), (enriched or "").strip()
    if not bank or not enriched or bank == enriched:
        return None
    if _descriptor_names(bank, enriched) or _descriptor_names(enriched, bank):
        return None
    # The newcomer's own spelling is left out: it is the question, not the
    # evidence. So is an outlet (a fuel arm is its own merchant by design),
    # and so is a row whose name was already set aside — a decision is not
    # evidence for itself, and counting one would let a single stray name
    # recruit the next. That last one needs no test of its own: such a row
    # has no merchant_name left to agree with anything.
    rows = conn.execute(
        """SELECT t.merchant_id AS mid, lower(t.merchant_name) AS n, count(*) AS c
             FROM transactions t
            WHERE t.name = %s AND t.merchant_id IS NOT NULL AND t.removed = 0
              AND t.merchant_outlet IS NULL
              AND t.merchant_name IS NOT NULL
              AND lower(t.merchant_name) <> lower(%s)
            GROUP BY 1, 2""", (bank_name, enriched)).fetchall()
    agreed = [r for r in rows if _descriptor_names(bank, r["n"])]
    if sum(r["c"] for r in agreed) < ESTABLISHED_ROWS:
        return None
    survivors = {_survivor(conn, r["mid"]) for r in agreed}
    survivors.discard(None)
    if len(survivors) != 1:
        return None
    mid = next(iter(survivors))
    got = conn.execute("SELECT kind FROM merchants WHERE id=%s",
                       (mid,)).fetchone()
    # a payment app or an institution fronts many payees under one line
    if got is None or (got["kind"] or "merchant") != "merchant":
        return None
    return mid


def _line_merchant(conn, bank_name: str):
    """The merchant the bank line's own key already answers to, or None.

    A row the line outranks keys on the line from then on, so the line's
    mapping is the answer for that row too. A person who renamed the line —
    or split it off a merchant from the Merchants page — has said where its
    charges go, and filing the moved rows under the habit's merchant
    instead would leave one key pointing at two merchants, which is the
    exact failure the move exists to prevent (reconcile_raws would move
    them by key at the next rename anyway, so the disagreement would not
    even last).

    The chain is followed for the same reason every other reader follows
    it: the id on an alias may name a merchant merged away since, or (from
    a restored archive) one that never arrived."""
    row = conn.execute(
        "SELECT merchant_id FROM merchant_canonical WHERE raw_merchant = %s",
        (bank_name,)).fetchone()
    if row is None or row["merchant_id"] is None:
        return None
    return _survivor(conn, row["merchant_id"])


def _merchant_for_name(conn, name: str, source: str, cache: dict):
    key = name.lower()
    if key in cache:
        return cache[key]
    mid = _find_by_name(conn, name)
    if mid is None and source == "layer1":
        mid = _abbreviates_plaid_merchant(conn, name)
    if mid is None:
        mid = _create(conn, name, source)
    cache[key] = mid
    return mid


def resolve(conn, *, txn_ids: list[str] | None = None,
            only_unresolved: bool = True) -> dict:
    """Give transactions their merchant_id.

    `txn_ids` limits the pass to those rows (the ingest hook); None means
    the tenant's whole ledger (nightly / backfill). `only_unresolved` skips
    rows that already have a merchant_id (the normal case); the backfill and
    a re-resolve after a rename pass False for the affected rows.

    Rows are grouped by their identity key (merchant_sql.raw_key), so a row
    whose aggregator name was overruled groups under its bank descriptor
    with that line's other unnamed rows — in this pass and in every later
    one. Design rationale: specs/merchant-identity.md.

    Returns counters. Idempotent."""
    stats = {"rows": 0, "plaid": 0, "named": 0, "created": 0}
    before = conn.execute("SELECT count(*) AS n FROM merchants").fetchone()["n"]
    where = ["t.removed = 0"]
    args: list = []
    if txn_ids is not None:
        if not txn_ids:
            return stats
        where.append("t.id = ANY(%s)"); args.append(list(txn_ids))
    if only_unresolved:
        where.append("t.merchant_id IS NULL")
    where_sql = " AND ".join(where)
    cities = merchant_dedup.cities_for(conn)
    plaid_cache: dict = {}
    name_cache: dict = {}
    with conn.transaction():
        # Before the rows are read, so a pass that loses the race re-reads
        # under the lock and finds the winner's rows already resolved
        _lock_identity(conn)
        # No shadow filter on purpose: this assigns identity to rows, it
        # does not count or sum them. A linked non-primary's rows must be
        # resolved too — its source serves the ledger the moment the
        # primary's aggregator fails, and nothing re-resolves on failover.
        row_sql = (
            f"""SELECT t.id, {merchant_sql.RAW_KEY} AS raw_key, t.raw,
                       t.name AS bank_name,
                       (t.merchant_outlet IS NOT NULL) AS is_outlet,
                       t.merchant_name_set_aside AS set_aside
                  FROM transactions t WHERE """)
        rows = conn.execute(row_sql + where_sql, args).fetchall()
        # A row whose name was set aside, for which the aggregator has SINCE
        # supplied an entity id, is not a guess any more — the feed has
        # resolved it, and Plaid's answer outranks any reading of a bank
        # line. Give the name back before anything is grouped, so the row
        # keys on it again. Left set aside it would sit in the descriptor's
        # group and lend that entity — and its logo and its identity — to
        # every unnamed row under the line.
        regained = [r["id"] for r in rows
                    if r["set_aside"] is not None and plaid_identity(r["raw"])]
        if regained:
            conn.execute(
                """UPDATE transactions
                      SET merchant_name = COALESCE(merchant_name,
                                                   merchant_name_set_aside),
                          merchant_name_set_aside = NULL
                    WHERE id = ANY(%s)""", (regained,))
            fresh = {r["id"]: r for r in conn.execute(
                row_sql + "t.id = ANY(%s)", (regained,)).fetchall()}
            rows = [fresh.get(r["id"], r) for r in rows]
        # group by raw key so a large ledger costs one decision per string
        by_key: dict[str, list] = {}
        for r in rows:
            by_key.setdefault(r["raw_key"] or "?", []).append(r)
        # How many bank lines carry a key, WHICH line, and whether the key
        # is an outlet are facts about the LEDGER, not about this batch.
        # Read off the batch they made an incremental pass disagree with a
        # whole-ledger one over the same data: a name the aggregator has
        # used under two different lines is a payee it knows rather than a
        # slip of either line, but a sync delivering one of those rows on
        # its own sees a single line and lets the descriptor overrule it —
        # and only_unresolved never brings the row back to find out
        # otherwise, so the nightly pass cannot correct it either. One
        # grouped query over the live rows carrying these keys answers
        # both passes the same way.
        #
        # A row whose name was already set aside is absent from its old
        # key by construction — it carries its bank line now — so a later
        # row arriving under another line with the same name does not
        # re-open that judgement: it is judged on its own evidence, and a
        # whole-ledger re-resolve, reading the same two keys, reaches the
        # same two answers rather than flipping either.
        facts = {r["raw_key"]: r for r in conn.execute(
            f"""SELECT {merchant_sql.RAW_KEY} AS raw_key,
                       count(DISTINCT t.name) FILTER (
                           WHERE btrim(coalesce(t.name, '')) <> '') AS lines,
                       min(t.name) FILTER (
                           WHERE btrim(coalesce(t.name, '')) <> '') AS line,
                       bool_or(t.merchant_outlet IS NOT NULL) AS outlet
                  FROM transactions t
                 WHERE t.removed = 0
                   AND {merchant_sql.RAW_KEY} = ANY(%s::text[])
                 GROUP BY 1""",
            ([k for k in by_key if k != "?"],)).fetchall()}
        # manual + llm aliases first: a person's answer, then a reviewed one.
        # Scoped to the keys in hand when the caller named transactions —
        # an incremental sync of a dozen rows has no business reading a
        # household's whole alias table.
        alias_sql = ("SELECT a.raw_merchant, a.canonical, a.method, m.id AS merchant_id, "
                     "       m.merged_into "
                     "  FROM merchant_canonical a "
                     # LEFT, not INNER: an alias whose merchant_id names no
                     # merchant (an archive restored without its merchants)
                     # must still supply its canonical string. Reading the
                     # name off the missing row would abort the nightly pass
                     # for that household, every night.
                     "  LEFT JOIN merchants m ON m.id = a.merchant_id")
        alias_args: list = []
        if txn_ids is not None:
            alias_sql += " WHERE a.raw_merchant = ANY(%s)"
            alias_args.append(list(by_key))
        aliases = {}
        for r in conn.execute(alias_sql, alias_args).fetchall():
            amid = r["merchant_id"]
            if amid is not None and r["merged_into"] is not None:
                amid = _survivor(conn, amid)
            aliases[r["raw_merchant"]] = (r["canonical"], r["method"], amid)
        # A person's rename binds the strings that existed when they made
        # it. A payee whose descriptor embeds the amount ("… Future Amount:
        # 1234.56 ~ Tran: DDIR") arrives as a NEW string every payday, so
        # the next one would mint a fresh merchant beside the one the person
        # named. Their answer is read here by the string's CLEANED form:
        # a new string that cleans to the same name as strings they mapped
        # follows their merchant — unless they mapped that family to more
        # than one name, which is a split to respect, not a family to join.
        manual_family: dict[str, set] = {}
        for r in conn.execute(
                "SELECT a.raw_merchant, a.canonical, m.id AS merchant_id, m.merged_into "
                "  FROM merchant_canonical a LEFT JOIN merchants m ON m.id = a.merchant_id "
                " WHERE a.method = 'manual'").fetchall():
            clean = merchant_dedup.canonical_merchant(r["raw_merchant"], cities)
            if clean:
                fmid = r["merchant_id"]
                if fmid is not None and r["merged_into"] is not None:
                    fmid = _survivor(conn, fmid)
                manual_family.setdefault(clean, set()).add((r["canonical"], fmid))
        # One group's decision can depend on ANOTHER group's: an unenriched
        # group, keyed on the bank's own descriptor, adopts the merchant
        # the enriched rows of that same descriptor were given. The scan
        # above has no ORDER BY, so leaving that to iteration order makes
        # the outcome depend on which row Postgres happened to hand back
        # first — and the unenriched row coming first is the ordinary case
        # (a refund, a late row), not the exotic one. It is also
        # unrecoverable: only_unresolved skips a resolved row forever.
        #
        # So decide in phases, and the answer becomes a function of
        # the ledger rather than of the scan (a group whose aggregator name
        # its descriptor may overrule is settled between 1 and 2 — see
        # `overrulable` below):
        #   1. every group that decides on its own evidence, walked in a
        #      stable key order;
        #   2. every descriptor lookup, run together against the state
        #      phase 1 left — so no adopter can see another adopter, and
        #      the answers cannot depend on the adopters' order either;
        #   3. the adopters' writes.
        prepared = []
        for raw_key in sorted(by_key):
            group = by_key[raw_key]
            alias = aliases.get(raw_key)
            # Plaid identity from the FIRST row of the group that has one.
            # NOT for a fuel-arm outlet ("Costco Gas") — its rows carry the
            # BRAND's Plaid identity, and the outlet is a merchant of its own
            # (the brand becomes its parent); nor for an app-made counterparty
            # name ("Zelle — Casey Example"), which IS the identity already.
            ident = None
            # the ledger's answer where it has one; a key of its own
            # (a row with no bank line at all) falls back to the batch
            f = facts.get(raw_key)
            outlet = (bool(f["outlet"]) if f
                      else any(r["is_outlet"] for r in group))
            if not outlet and " — " not in raw_key:
                for r in group:
                    ident = plaid_identity(r["raw"])
                    if ident:
                        break
            # A group adopts by descriptor only when nothing authoritative
            # has spoken for its string AND the aggregator left it
            # unenriched — which is exactly the case where raw_key fell
            # through to the bank's own descriptor. A group that HAS an
            # enrichment string was keyed on that string, and its payee is
            # the one the aggregator named; consulting the descriptor there
            # would let two different payees that share a meaningless
            # descriptor collapse into one.
            #
            # "Nothing authoritative" is a person's rename (manual) or a
            # reviewed merge (llm) — NOT the string clean's own layer-1
            # row. That row is a machine mirror: the nightly pass mints one
            # for every key it sees and rewrites it from the raw string
            # whenever the cleaning changes, so treating it as an answer
            # switched the descriptor off for every line the household has
            # had for more than a night. The rows then fall back on the
            # cleaned line as a NAME — which mints a merchant named after
            # the bank's own descriptor, beside the payee it belongs to,
            # and re-points the line's alias at it.
            machine = alias is None or alias[1] == "layer1"
            bank = (f["line"] if f
                    else next((r["bank_name"] for r in group
                               if (r["bank_name"] or "").strip()), None))
            adopts = (machine and ident is None and not outlet
                      and " — " not in raw_key
                      and bank is not None and bank == raw_key)
            # The mirror case: the aggregator DID name the group, with no
            # entity behind the name, and every row of it sits under one
            # bank descriptor. Whether that descriptor overrules the name
            # is asked in phase 2 with the adopters, for the same reason —
            # the rows it would follow may be settled in this very pass. A
            # layer-1 alias with no merchant is the nightly string clean
            # having seen the key, not anyone having answered for it.
            unspoken = alias is None or (alias[1] == "layer1" and alias[2] is None)
            # ONE bank line, counted over the LEDGER: a name the aggregator
            # has also used under another line is a payee it knows, not a
            # misreading of either.
            lines = (f["lines"] if f
                     else len({(r["bank_name"] or "").strip() for r in group
                               if (r["bank_name"] or "").strip()}))
            overrulable = (unspoken and ident is None and not outlet
                           and " — " not in raw_key and not adopts
                           and lines == 1 and bank is not None
                           and bank != raw_key
                           # the pure half of the test, asked here so a name
                           # the descriptor does contain settles in phase 1,
                           # where the adopters of that descriptor can see it.
                           # The CLEANED name counts as contained too: "The
                           # Juniper Market", "Juniper Market #12" and
                           # "Juniper Market Inc" are all the line's own
                           # payee wearing the noise the string clean exists
                           # to remove, not a stray guess about it.
                           and not _descriptor_names(bank, raw_key)
                           and not _descriptor_names(
                               bank, merchant_dedup.canonical_merchant(
                                   raw_key, cities) or ""))
            prepared.append({"raw_key": raw_key, "group": group, "alias": alias,
                             "ident": ident, "outlet": outlet, "bank": bank,
                             "adopts": adopts, "overrulable": overrulable})

        def _settle(g, adopted, overruled=False):
            """Give one group its merchant, and record the decision.

            `adopted` is what the bank-descriptor lookup answered for this
            group, or None when it did not run or refused. `overruled`: the
            answer came from the descriptor AGAINST the group's own key, so
            the rows move, their own key is left unmapped, and the
            aggregator's name comes off them."""
            raw_key, group = g["raw_key"], g["group"]
            alias, ident, outlet = g["alias"], g["ident"], g["outlet"]
            mid = None
            if alias and alias[1] == "manual":
                # a person's answer outranks everything, Plaid included
                canon = alias[0]
                mid = alias[2] or _merchant_for_name(conn, canon, "manual", name_cache)
                conn.execute(
                    "UPDATE merchants SET name=%s, name_source='manual', "
                    "updated_at=now() WHERE id=%s AND name<>%s",
                    (canon, mid, canon))
            elif ident:
                mid = _upsert_plaid_merchant(conn, ident, plaid_cache)
                stats["plaid"] += len(group)
            elif alias and alias[1] == "llm" and not outlet:
                # a reviewed merge, for rows Plaid did not resolve. Never for
                # an outlet: a merge that folded "Costco Gas" into "Costco"
                # can predate the outlet split and would undo it
                mid = alias[2] or _merchant_for_name(conn, alias[0], "llm", name_cache)
            else:
                # an app-made counterparty name ("Venmo — Casey Example",
                # "Zelle — …") is already the display form; the string
                # clean would strip the dash it was built with
                # the layer1 alias (merchant_dedup.apply owns it — it knows
                # the harvested per-merchant cities; this pass never writes
                # a canonical, so reading it back cannot poison a later pass)
                canon = (alias[0] if alias and alias[1] == "layer1" else None) or (
                    raw_key if (" — " in raw_key or outlet)
                    else merchant_dedup.canonical_merchant(raw_key, cities)) \
                    or raw_key
                # nothing authoritative has spoken for this string; if other
                # rows carry the same bank descriptor, take the merchant they
                # were already given rather than minting its twin
                mid = adopted
                fam = manual_family.get(canon) if mid is None else None
                if fam and len({c for c, _ in fam}) == 1:
                    # the person already named this spelling family
                    fcanon, fmid = next(iter(fam))
                    mid = fmid or _merchant_for_name(conn, fcanon, "manual", name_cache)
                    stats["manual_family"] = stats.get("manual_family", 0) + len(group)
                if mid is None:
                    mid = _merchant_for_name(conn, canon, "layer1", name_cache)
                stats["named"] += len(group)
                if outlet:
                    # the outlet's brand, when Plaid resolved it on the row
                    for r in group:
                        bi = plaid_identity(r["raw"])
                        if bi:
                            pid = _upsert_plaid_merchant(conn, bi, plaid_cache)
                            if pid != mid:
                                conn.execute(
                                    "UPDATE merchants SET parent_id=%s "
                                    "WHERE id=%s AND parent_id IS NULL",
                                    (pid, mid))
                            break
            # the alias row: raw key → merchant. This pass writes ONLY the
            # merchant_id — the canonical string belongs to whoever wrote it
            # (apply's layer1, a reviewer's llm, a person's manual) and a new
            # row for a string nobody mapped yet gets the merchant's name.
            mrow = conn.execute("SELECT name FROM merchants WHERE id=%s",
                                (mid,)).fetchone() if mid is not None else None
            if mrow is None:
                # belt to the alias LEFT JOIN above: whatever branch we took
                # handed back an id nothing answers to, so mint a merchant
                # from the string rather than write the dangling id onto
                # every row in the group
                mid = _merchant_for_name(conn, raw_key, "layer1", name_cache)
                mrow = conn.execute("SELECT name FROM merchants WHERE id=%s",
                                    (mid,)).fetchone()
            mname = mrow["name"]
            moved = overruled and adopted is not None and mid == adopted
            if not moved:
                conn.execute(
                    """INSERT INTO merchant_canonical (raw_merchant, canonical, method,
                                                       as_of, merchant_id)
                       VALUES (%s,%s,'layer1',now(),%s)
                       ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET
                           merchant_id = EXCLUDED.merchant_id""",
                    (raw_key, mname, mid))
            else:
                stats["overruled"] = stats.get("overruled", 0) + len(group)
                # The key the rows DO carry from here on is the bank line,
                # so that is where the alias belongs — the same row an
                # unnamed charge on this line writes for itself.
                #
                # An existing row is re-pointed only when the string clean
                # wrote it: a machine mirror can carry no merchant at all
                # (the nightly pass mints one for a key before anything has
                # resolved it) or one that has since been pruned or
                # restored without its merchants, and leaving the key
                # mapping to nothing while rows carry it is the very gap
                # this write exists to close. Where it already names a live
                # merchant the rows were filed under THAT one
                # (see `_line_merchant`), so the update is a no-op; a
                # person's rename and a reviewed merge are never touched,
                # here as everywhere else in this module.
                conn.execute(
                    """INSERT INTO merchant_canonical (raw_merchant, canonical,
                                                       method, as_of, merchant_id)
                       VALUES (%s,%s,'layer1',now(),%s)
                       ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET
                           merchant_id = EXCLUDED.merchant_id
                        WHERE merchant_canonical.method = 'layer1'""",
                    (g["bank"], mname, mid))
            ids = [r["id"] for r in group]
            if moved:
                # The aggregator's name comes OFF the rows. Writing the
                # merchant alone would file a row under one key while it
                # still ANSWERED to another: a rename of this merchant
                # would map the stray name to it, a real business of that
                # name would be dragged in, and renaming that business
                # would pull these rows back out. Moving the name aside
                # makes the row key on its bank line instead — here, and in
                # every reader, since a row with no aggregator name is a
                # shape they all already handle — so key → merchant stays
                # one answer. COALESCE so a second pass over an already
                # moved row cannot overwrite the name with nothing.
                conn.execute(
                    """UPDATE transactions
                          SET merchant_id = %s,
                              merchant_name_set_aside = COALESCE(
                                  merchant_name, merchant_name_set_aside),
                              merchant_name = NULL
                        WHERE id = ANY(%s)""",
                    (mid, ids))
            else:
                conn.execute(
                    "UPDATE transactions SET merchant_id=%s WHERE id = ANY(%s)",
                    (mid, ids))
            stats["rows"] += len(ids)

        for g in prepared:
            if not (g["adopts"] or g["overrulable"]):
                _settle(g, None)
        # The overrulable groups next, and before the adopters: one that is
        # refused mints a merchant under its descriptor, which an adopter of
        # that descriptor has to see. Lookups together, then writes, as below.
        doubted = [g for g in prepared if g["overrulable"]]
        verdicts = [_descriptor_overrules_enrichment(conn, g["bank"], g["raw_key"])
                    for g in doubted]
        for g, verdict in zip(doubted, verdicts):
            if verdict is not None:
                # The verdict decides THAT the rows move; where they land
                # is then a question about the key they will carry. Read
                # here rather than with the verdicts above because two
                # doubted groups on one line can only read the same answer
                # — the first writes the line's alias with the merchant the
                # second would have found anyway.
                verdict = _line_merchant(conn, g["bank"]) or verdict
            _settle(g, verdict, overruled=verdict is not None)
        adopters = [g for g in prepared if g["adopts"]]
        # every lookup before every adopter write, so the answers describe
        # the same state and none of them is an artefact of going second
        answers = [_merchant_by_bank_descriptor(
                       conn, g["bank"], [r["id"] for r in g["group"]])
                   for g in adopters]
        for g, answer in zip(adopters, answers):
            _settle(g, answer)
    if txn_ids is None:
        stats["pruned"] = prune_empty(conn)
    after = conn.execute("SELECT count(*) AS n FROM merchants").fetchone()["n"]
    stats["created"] = after - before + stats.get("pruned", 0)
    return stats


def fold_twins(conn, limit: int = 50) -> dict:
    """Fold live merchants that share one name into one row.

    Two live rows with the same name are one merchant the ledger shows
    twice under one label: every surface that groups by name already
    treats them as one, and every surface keyed on the row (a bill's
    identity, a rule) sees two. A restore can leave such a pair behind.
    Nightly,
    bounded, idempotent.

    The survivor is the Plaid-backed row, else the manually named one,
    else the oldest. The loser keeps its row and points at the survivor
    (`merged_into`), so everything holding its id — a bill's identity, a
    merge chain — follows it; its rows, aliases and children move; the
    survivor gains a logo or site it lacked. Two rows that BOTH carry a
    Plaid entity are two businesses that happen to share a name and are
    left alone; so is any group with an outlet (a fuel arm is its own
    merchant by design, and its name already differs from the brand's)."""
    stats = {"groups": 0, "folded": 0, "skipped_entities": 0, "skipped_moved": 0}
    groups = conn.execute(
        """SELECT lower(name) AS lname, array_agg(id ORDER BY
                      (plaid_entity_id IS NULL), (name_source <> 'manual'),
                      created_at) AS ids,
                  count(*) FILTER (WHERE plaid_entity_id IS NOT NULL) AS entities
             FROM merchants
            WHERE merged_into IS NULL AND parent_id IS NULL
            GROUP BY lower(name) HAVING count(*) > 1
            ORDER BY lower(name) LIMIT %s""", (limit,)).fetchall()
    for g in groups:
        stats["groups"] += 1
        if g["entities"] > 1:
            stats["skipped_entities"] += 1
            continue
        survivor, losers = g["ids"][0], list(g["ids"][1:])
        with conn.transaction():
            _lock_identity(conn)
            # The groups above were read with no lock held. A merge or a
            # rename from the Merchants page (POST /merchants/merge,
            # /merchants/rename) can land in the gap, and it moves rows and
            # writes merged_into itself — folding on the stale reading
            # would overwrite that pointer and drag the rows back. So ask
            # again, now that the lock says nobody else is writing, and
            # keep only members that are still live and still share the
            # survivor's name. The group re-forms on the next run.
            live = {r["id"]: r for r in conn.execute(
                "SELECT id, lower(name) AS lname, plaid_entity_id FROM merchants "
                "WHERE id = ANY(%s) AND merged_into IS NULL AND parent_id IS NULL",
                ([survivor] + losers,)).fetchall()}
            if survivor not in live:
                stats["skipped_moved"] += 1
                continue
            lname = live[survivor]["lname"]
            losers = [x for x in losers
                      if x in live and live[x]["lname"] == lname]
            # and a loser that has since gained a Plaid entity makes this
            # the two-businesses-one-name group the pass leaves alone
            if not losers or sum(1 for x in [survivor] + losers
                                 if live[x]["plaid_entity_id"]) > 1:
                stats["skipped_moved"] += 1
                continue
            conn.execute("UPDATE transactions SET merchant_id=%s "
                         "WHERE merchant_id = ANY(%s)", (survivor, losers))
            conn.execute("UPDATE merchant_canonical SET merchant_id=%s "
                         "WHERE merchant_id = ANY(%s)", (survivor, losers))
            conn.execute("UPDATE merchants SET parent_id=%s "
                         "WHERE parent_id = ANY(%s)", (survivor, losers))
            conn.execute(
                """UPDATE merchants s
                      SET logo_url = COALESCE(s.logo_url, l.logo_url),
                          website = COALESCE(s.website, l.website),
                          phone = COALESCE(s.phone, l.phone),
                          updated_at = now()
                     FROM merchants l
                    WHERE s.id = %s AND l.id = ANY(%s)
                      AND (s.logo_url IS NULL OR s.website IS NULL
                           OR s.phone IS NULL)""", (survivor, losers))
            # a loser's entity would violate the one-entity-per-row index
            # on the survivor; it can only be a loser when the survivor has
            # none (else the group was skipped), so it moves across
            conn.execute(
                """UPDATE merchants s SET plaid_entity_id = l.plaid_entity_id,
                          updated_at = now()
                     FROM merchants l
                    WHERE s.id = %s AND l.id = ANY(%s)
                      AND s.plaid_entity_id IS NULL
                      AND l.plaid_entity_id IS NOT NULL""", (survivor, losers))
            # merged_into IS NULL: belt to the re-read above, so a pointer
            # somebody else wrote is never overwritten by this one
            conn.execute("UPDATE merchants SET plaid_entity_id = NULL, "
                         "merged_into=%s, updated_at=now() "
                         "WHERE id = ANY(%s) AND merged_into IS NULL",
                         (survivor, losers))
        stats["folded"] += len(losers)
    return stats


def prune_empty(conn) -> int:
    """Delete merchants nothing points at — no row, no alias, no child, no
    merged-away merchant naming them as survivor. A full pass can leave
    such rows behind when identity moved (a rename that merged, a re-resolve
    with better rules); they would otherwise clutter name matching.

    REMOVED transactions are cleared first. They do not hold a merchant
    alive — nothing displays them — but they do carry its id, and deleting
    the merchant out from under them would leave the only pointers in the
    schema that name nothing at all.

    Rows on shadow (linked non-primary / hidden) accounts DO hold a
    merchant alive. They are excluded from money aggregates, not from the
    schema: they keep a merchant_id, and pruning past them would delete a
    merchant the surviving pointers still name.

    A merged-away merchant that a BILL still names is held too. A bill's
    identity is a merchant id kept in its own JSON, with no foreign key, and
    it follows `merged_into` to the survivor — but only once the nightly
    bill pass has rewritten it, and that pass runs before this one. Pruned
    in between, the pointer is gone before anything read it: the bill is
    left holding an id nothing answers to and a fallback name no live
    merchant carries. The hold lasts one night; the row goes on the pass
    after the bill let go of it.

    Under the household's identity lock, and in one transaction, because
    "nothing points here" is only true for as long as nobody else is
    resolving. A sync's resolve runs in its own single-flight domain from
    the nightly sweep, so the two really do overlap; under READ COMMITTED
    the other pass can have chosen a merchant this one is about to delete
    and not yet written the row or the alias that would have held it alive,
    and then writes a merchant_id naming nothing. The lock is the same one
    resolve() takes, and this takes no other, so there is no pair of locks
    to deadlock on."""
    with conn.transaction():
        _lock_identity(conn)
        return _prune_empty_locked(conn)


# A merged-away merchant whose id an identity outside the merchant tables
# still carries — a bill's `merchant_refs`. `{m}` is the merchant's id.
_HELD_BY_A_BILL = """(SELECT 1 FROM merchants h
                       WHERE h.id = {m} AND h.merged_into IS NOT NULL
                         AND EXISTS (SELECT 1 FROM bills b
                                      WHERE b.raw -> 'merchant_refs' @> jsonb_build_array(
                                                jsonb_build_object('id', h.id::text))))"""


def _prune_empty_locked(conn) -> int:
    """prune_empty's statements, for a caller already holding the lock."""
    conn.execute(
        f"""UPDATE transactions t SET merchant_id = NULL
            WHERE t.merchant_id IS NOT NULL AND t.removed <> 0
              AND NOT EXISTS (SELECT 1 FROM transactions o
                               WHERE o.merchant_id = t.merchant_id
                                 AND o.removed = 0)
              AND NOT EXISTS (SELECT 1 FROM merchant_canonical a
                               WHERE a.merchant_id = t.merchant_id)
              AND NOT EXISTS (SELECT 1 FROM merchants c
                               WHERE c.parent_id = t.merchant_id
                                  OR c.merged_into = t.merchant_id)
              AND NOT EXISTS {_HELD_BY_A_BILL.format(m="t.merchant_id")}""")
    return conn.execute(
        f"""DELETE FROM merchants m
            WHERE NOT EXISTS (SELECT 1 FROM transactions t
                               WHERE t.merchant_id = m.id AND t.removed = 0)
              AND NOT EXISTS (SELECT 1 FROM merchant_canonical a
                               WHERE a.merchant_id = m.id)
              AND NOT EXISTS (SELECT 1 FROM merchants c
                               WHERE c.parent_id = m.id OR c.merged_into = m.id)
              AND NOT EXISTS {_HELD_BY_A_BILL.format(m="m.id")}""").rowcount


def re_resolve_recleaned(conn, was: dict) -> list[dict]:
    """Move the rows of raw keys whose layer-1 canonical just changed to the
    merchant that string now names, and leave a pointer behind.

    `was` maps each such key to the merchant id its alias carried BEFORE
    the change (None when it had none). A plain re-resolve moves the rows
    and lets the next prune delete the merchant they left, which is right
    for the ledger and wrong for everything holding that merchant's id
    where the prune cannot see it: a bill's identity is orphaned, and so is
    anything stored under the old NAME, since no live merchant answers to
    it any more. So an emptied merchant is marked merged into the one its
    rows went to, exactly as a person's merge leaves it, and takes across
    the logo, site and phone the survivor lacks.

    One locked transaction: the emptiness test reads and then writes on
    the answer, like prune_empty, and a full pass pruning in between would
    delete the row before it could be pointed anywhere.

    Returns [{"from": old name, "into": survivor's name}] per merchant
    retired, for callers with name-keyed state of their own to move."""
    retired: list[dict] = []
    if not was:
        return retired
    raws = list(was)
    with conn.transaction():
        _lock_identity(conn)
        ids = [r["id"] for r in conn.execute(
            f"SELECT t.id FROM transactions t "
            f" WHERE {merchant_sql.RAW_KEY} = ANY(%s) "
            f"   AND t.removed = 0", (raws,)).fetchall()]
        if not ids:
            return retired
        resolve(conn, txn_ids=ids, only_unresolved=False)
        now = {r["raw_merchant"]: r["merchant_id"] for r in conn.execute(
            "SELECT raw_merchant, merchant_id FROM merchant_canonical "
            " WHERE raw_merchant = ANY(%s)", (raws,)).fetchall()}
        done: set = set()
        for raw_key in sorted(raws):
            old_mid, new_mid = was[raw_key], now.get(raw_key)
            if old_mid is None or new_mid is None or old_mid in done:
                continue
            new_mid = _survivor(conn, new_mid)
            if new_mid is None or new_mid == old_mid:
                continue
            done.add(old_mid)
            _retire_if_empty(conn, old_mid, new_mid)
            pair = conn.execute(
                "SELECT o.name AS old, s.name AS new "
                "  FROM merchants o JOIN merchants s ON s.id = o.merged_into "
                " WHERE o.id = %s AND s.id = %s", (old_mid, new_mid)).fetchone()
            if pair is None:
                continue                 # still has rows or aliases: not retired
            conn.execute(
                """UPDATE merchants s
                      SET logo_url = COALESCE(s.logo_url, l.logo_url),
                          website = COALESCE(s.website, l.website),
                          phone = COALESCE(s.phone, l.phone),
                          updated_at = now()
                     FROM merchants l
                    WHERE s.id = %s AND l.id = %s
                      AND (s.logo_url IS NULL OR s.website IS NULL
                           OR s.phone IS NULL)""", (new_mid, old_mid))
            retired.append({"from": pair["old"], "into": pair["new"]})
    return retired


def reconcile_raws(conn, raws: list[str]) -> dict:
    """After a rename/merge/undo touched these raw keys, re-point their rows
    at the merchant their (new) canonical names.

    A whole-merchant rename keeps the merchant (and its logo): when every
    alias of the rows' current merchant now carries the same new name and no
    other merchant has that name, the merchant itself is renamed. Otherwise
    the raws move to the merchant with that name (a merge — the source
    merchant is left pointing at the survivor once it has no rows) or to a
    fresh one (a split)."""
    stats = {"raws": 0, "renamed": 0, "moved": 0}
    if not raws:
        return stats
    with conn.transaction():
        # The same household lock resolve() takes, and taken FIRST — this
        # function mints merchants (a per-name lock) and then re-resolves
        # the orphans, so acquiring it in the other order is the one way
        # the two locks could form a cycle.
        _lock_identity(conn)
        # LEFT JOIN and chain-follow for the same reason resolve() does: the
        # id on an alias may name a merchant that was merged away, or (from
        # a restored archive) one that never arrived
        cur = {}
        for r in conn.execute(
                "SELECT a.raw_merchant, a.canonical, m.id AS merchant_id, "
                "       m.merged_into "
                "  FROM merchant_canonical a "
                "  LEFT JOIN merchants m ON m.id = a.merchant_id "
                " WHERE a.raw_merchant = ANY(%s)", (raws,)).fetchall():
            omid = r["merchant_id"]
            if omid is not None and r["merged_into"] is not None:
                omid = _survivor(conn, omid)
            cur[r["raw_merchant"]] = (r["canonical"], omid)
        # rows whose alias row was deleted (undo of a first mapping) fall back
        # to a fresh resolve
        orphan = [r for r in raws if r not in cur]
        for raw_key, (canon, old_mid) in cur.items():
            target = _find_by_name(conn, canon)
            if target is None and old_mid is not None:
                # does the whole old merchant now go by this name?
                others = conn.execute(
                    "SELECT count(*) AS n FROM merchant_canonical "
                    "WHERE merchant_id=%s AND lower(canonical) <> lower(%s)",
                    (old_mid, canon)).fetchone()["n"]
                if others == 0:
                    conn.execute(
                        "UPDATE merchants SET name=%s, name_source='manual', "
                        "updated_at=now() WHERE id=%s", (canon, old_mid))
                    target = old_mid
                    stats["renamed"] += 1
            if target is None:
                target = _create(conn, canon, "manual")
            if target != old_mid:
                stats["moved"] += 1
            conn.execute("UPDATE merchant_canonical SET merchant_id=%s "
                         "WHERE raw_merchant=%s", (target, raw_key))
            conn.execute(
                f"""UPDATE transactions t SET merchant_id=%s
                     WHERE {merchant_sql.RAW_KEY} = %s AND t.removed = 0""",
                (target, raw_key))
            stats["raws"] += 1
            if old_mid is not None and old_mid != target:
                _retire_if_empty(conn, old_mid, target)
        if orphan:
            # every row with that raw key, shadow accounts included — a
            # rename must reach both copies of a dual-linked account or the
            # shadow keeps the old name and shows it after a failover
            ids = [r["id"] for r in conn.execute(
                f"SELECT t.id FROM transactions t "
                f"WHERE {merchant_sql.RAW_KEY} = ANY(%s) "
                "AND t.removed = 0", (orphan,)).fetchall()]
            resolve(conn, txn_ids=ids, only_unresolved=False)
            stats["raws"] += len(orphan)
    return stats


def _retire_if_empty(conn, mid, survivor) -> None:
    """A merchant that no row and no alias points at any more is marked
    merged into the survivor (kept for undo/history, hidden from lists).

    The survivor recorded is the END of its own merge chain, so repeated
    merges never build A→B→C: every reader would have to walk it, and a
    restore could reimport it as a cycle.

    Called only from inside a locked transaction (reconcile_raws,
    re_resolve_recleaned), and it must stay that way: like prune_empty it reads "does anything still point
    here" and then writes on the answer, which a concurrent resolve can
    invalidate between the two."""
    survivor = _survivor(conn, survivor) or survivor
    if survivor == mid:
        return                       # a merchant is never merged into itself
    # Counts every row, shadow accounts included — this is a liveness test
    # ("does anything still point here"), not a figure anyone reads. A
    # shadow row is a pointer.
    n = conn.execute(
        "SELECT (SELECT count(*) FROM transactions WHERE merchant_id=%s AND removed=0) "
        "+ (SELECT count(*) FROM merchant_canonical WHERE merchant_id=%s) AS n",
        (mid, mid)).fetchone()["n"]
    if n == 0:
        conn.execute("UPDATE merchants SET merged_into=%s, updated_at=now() "
                     "WHERE id=%s AND merged_into IS NULL", (survivor, mid))


def stats(conn) -> dict:
    """How resolved the ledger is — for the doctor page and the rehearsal.

    Deliberately counts the WHOLE ledger, shadow rows included: this
    answers "did the resolver reach every row it has to write", and the
    rows it has to write include the ones on linked non-primaries. It is
    a coverage ratio, never a money figure and never a per-merchant count
    shown beside an amount."""
    r = conn.execute(
        """SELECT count(*) AS rows,
                  count(*) FILTER (WHERE t.merchant_id IS NOT NULL) AS resolved,
                  count(*) FILTER (WHERE m.logo_url IS NOT NULL) AS with_logo,
                  count(DISTINCT t.merchant_id) AS merchants,
                  count(DISTINCT t.merchant_id) FILTER (WHERE m.plaid_entity_id IS NOT NULL) AS plaid_merchants
             FROM transactions t LEFT JOIN merchants m ON m.id = t.merchant_id
            WHERE t.removed = 0""").fetchone()
    return dict(r)
