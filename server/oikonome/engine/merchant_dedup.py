"""Merchant-name consolidation for imported (non-aggregator) ledger rows.

Mint/Simplifi/bank-PDF rows carry the raw bank descriptor as their merchant
name (aggregator rows arrive pre-cleaned), so one real payee shows up under
many strings — 'THE ORCHARD APTS ONLINE payment~ Future Amount: 1875 ~
Tran:' vs 'THE ORCHARD APTS ONLINE PMT~', or 'Acme Lending' vs 'Acme Lending
Student'.

Three layers, all writing to the `merchant_canonical` map (raw -> canonical),
never touching `name`/`merchant_name`/`raw`. Display surfaces read
COALESCE(mc.canonical, t.merchant_outlet, t.merchant_name, t.name) — the
DISPLAY_MERCHANT fragment below, joined per MC_JOIN, which keys the map on
the outlet when one was identified — so the fix is reversible (drop the
rows) and clobber-proof (sync only writes name/merchant_name):

  Layer 1 — `canonical_merchant()`: deterministic. Strips Mint '~…~ Tran'
     volatile clauses (they embed the dollar amount, exploding one payee
     into dozens of strings), ACH markers, payment-processor wrappers
     (GglPay/SQ*/TST*/PP*), and trailing bank id/hex/phone junk; keeps the
     real name tokens. Recomputable, runs each sync, tagged method='layer1'.
  Layer 2 — reviewed merges (an LLM or manual review) with method='llm'
     or 'manual'; those rows win over layer1 and are never recomputed.
     They arrive via restore, or `load_llm_map`.
  Layer 3 — the user's own renames/merges (`rename`), method='manual',
     each journalled to merchant_renames so `undo` can replay it backwards.
     Known limitation: the journal records the prior canonical but not the
     prior METHOD, so a mapping restored by undo comes back method='manual'
     — the act of undoing is read as the user endorsing that prior name.
     The cost is that a layer1 mapping restored this way stops being
     recomputed on future syncs; fixing that needs a from_method column in
     the journal.

Reviewed-merge rows travel inside export ZIPs (restore); `load_llm_map` is
the programmatic door for the same map.

`apply()` (cheap SQL, no LLM) runs each sync only to give any brand-new raw
string its layer1 canonical.
"""
from __future__ import annotations

import logging
import re

from . import budget, merchant_sql

log = logging.getLogger("oikonome.merchant_dedup")

_STOP = budget.STOP_TOKENS | {"std"}
# leading payment-processor / gateway wrappers (so 'GglPay BRIGHTMART' -> 'Brightmart')
_PROC = re.compile(
    r"^\s*(?:gglpay|sq ?\*|tst\*|pp ?\*|paypal ?\*|cko\*|google ?\*|sp | tst|"
    r"chkcard |pos debit |pos |ext |dbt crd |visa dda |ppd )+", re.I)
# trailing id / hex / phone / url junk
_TAIL = re.compile(
    r"(?:\bx{4,}[\dx]*|\bnt_[a-z0-9]+|g\.co/\S+|squareup\.com|\+?\d[\d\- ]{6,}|"
    r"\b[a-z0-9]{6,}jg[a-z0-9]+|info@\S+|#\S+).*$", re.I)
# Mint '~…' clause + ACH markers (word-boundary anchored so 'Transportation'
# survives while the 'Tran:' marker is cut)
_NOISE = re.compile(
    r"~.*$|\b(?:des|id|ppd|ref|conf|tran|indn|co)\b\s*:.*$|\bweb\s*id\b.*$", re.I)
# Street address / locality tail. Aggregators sometimes send NO merchant
# name at all, and the raw descriptor then carries the store's full address:
#   'ACME AUTO SPA 100 MAIN ST SPRINGFIELD, IL, USA'
# Kept, those tokens split one payee into 'Acme Auto Spa' and
# 'Acme Auto Spa Main Springfield'.
# Two conservative cuts, in order:
#   _GEO  — a trailing ', ST, US' / ', ST US' country-state tail.
#   _ADDR — from a house number (2-6 digits, optional letter) onward, but
#           ONLY when something survives in front of it, so '9 Lantern 34567'
#           keeps its name and a label that is nothing but an address is
#           left alone rather than emptied.
# Deliberately NOT stripped: a bare trailing city with no street number
# ("PRAIRIE CU LAC DU SABLE") — removing trailing words on suspicion of being a
# place name would eat real merchants (Springfield Co-op, Riverton Tea Co).
_GEO = re.compile(r",\s*[A-Za-z]{2}\s*,?\s*(?:us|usa)\s*$", re.I)
_ADDR = re.compile(r"(?<=\S)\s+\d{2,6}[A-Za-z]?\s+(?:[NSEW]\.?|"
                   r"north|south|east|west|[A-Za-z]{3,})\b.*$", re.I)


def cities_for(conn) -> tuple[str, ...]:
    """The tenant's configured city names (`merchant_strip_cities`).

    EXPLICIT, never guessed. Banks truncate a store's city into the merchant
    string at different lengths — 'Quillo Springfie' and 'Quillo Springfiel'
    are one restaurant — but nothing in the ledger reliably identifies which
    trailing word is a place. Deriving it is unreliable: how many spending
    categories a trailing word appears under does not tell a place from a
    business word. So the list is supplied by whoever knows — default
    empty, which is a no-op."""
    try:
        from . import budget
        cfg = budget.load_config(conn)
    except Exception:                                     # noqa: BLE001
        return ()
    raw = cfg.get("merchant_strip_cities") or []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[,;\n]", raw)]
    # NOTE: the aggregator-reported cities are deliberately NOT folded in
    # here. They are applied PER MERCHANT (harvest_city_map) because real
    # town names collide with ordinary business words — a global 'Center'
    # rewrites 'Brightline Auto Center'. merchant_cities_seen is kept in
    # config for display only, so the Settings card can show what is
    # already handled automatically.
    names: set[str] = set()
    for x in list(raw):
        for v in _city_variants(str(x)):
            names.add(v)
    return tuple(sorted(names, key=len, reverse=True))    # longest first


def _city_variants(city: str) -> set[str]:
    """A city as it can appear at the end of a merchant label: the name
    itself, its scrubbed form (canonical_merchant has already dropped
    punctuation and short words by the time we strip — "Lac du Sable"
    reaches us as "Lac Sable"), and the truncations banks produce by
    cutting the descriptor short ("Springfield" -> "Springfiel", "Springfie").
    Prefixes stop at 70% of the city (min 6 chars): the truncations banks
    actually produce are near-full ('Springfiel', 'Springfie'), while a
    short fragment would start eating real words — 'Spring' would turn
    'Mineral Spring' into 'Mineral'."""
    city = (city or "").strip()
    if len(city) < 4:
        return set()
    out = {city}
    scrubbed = " ".join(t for t in re.sub(r"[^A-Za-z ]+", " ", city).split()
                        if len(t) >= 3)
    if scrubbed:
        out.add(scrubbed)
        floor = max(6, -(-len(scrubbed) * 7 // 10))       # ceil(70%)
        for n in range(floor, len(scrubbed)):
            if scrubbed[n - 1] != " ":
                out.add(scrubbed[:n])
    return {x for x in out if len(x) >= 4}


def harvest_city_map(conn) -> dict[str, list[str]]:
    """raw merchant label -> the cities the aggregator reported FOR THAT
    MERCHANT'S OWN transactions.

    Per-merchant, not a global list, and that distinction is load-bearing.
    A global list is unsafe because real towns collide with ordinary
    business words: 'Center' is a town in Texas, and stripping it wherever
    it trails a name turns 'Brightline Auto Center' into 'Brightline Auto'
    and 'The Oil Change Center' into 'The Oil Change'. Scoped per merchant,
    'Center' is only ever removed from a business the feed actually places
    in Center, TX."""
    out: dict[str, list[str]] = {}
    try:
        rows = conn.execute(
            "SELECT DISTINCT COALESCE(merchant_name, name) AS m, "
            "       raw->'location'->>'city' AS city "
            "FROM transactions WHERE removed = 0 "
            "AND raw->'location'->>'city' IS NOT NULL "
            "AND COALESCE(merchant_name, name) IS NOT NULL").fetchall()
    except Exception:                                     # noqa: BLE001
        log.warning("city harvest failed", exc_info=True)
        return {}
    for r in rows:
        city = (r["city"] or "").strip()
        if len(city) >= 4:
            out.setdefault(r["m"], [])
            if city not in out[r["m"]]:
                out[r["m"]].append(city)
    return out


def harvest_cities(conn) -> list[str]:
    """Cities the aggregator reported on this tenant's own transactions.

    Plaid-style payloads carry `location.city` for a store-present charge —
    a minority of rows, but enough to cover the cities that would otherwise
    have to be configured by hand, bar those predating aggregator sync.
    Harvested at sync time and stored in config so read paths stay cheap."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT raw->'location'->>'city' AS city "
            "FROM transactions WHERE removed = 0 "
            "AND raw->'location'->>'city' IS NOT NULL").fetchall()
    except Exception:                                     # noqa: BLE001
        log.warning("city harvest failed", exc_info=True)
        return []
    return sorted({(r["city"] or "").strip() for r in rows
                   if (r["city"] or "").strip()
                   and len((r["city"] or "").strip()) >= 4})


# Words that must never be left standing ALONE by a city strip: they name a
# category, not a business, so collapsing to one of them would fuse every
# such merchant in town into a single row ('Bank Of Springfield' -> 'Bank').
# A distinctive single word ('Quillo') is fine and is the common case.
_GENERIC_ALONE = {
    "bank", "store", "shop", "shoppe", "market", "mart", "cafe", "coffee",
    "grill", "pizza", "restaurant", "deli", "diner", "bar", "tavern", "pub",
    "inc", "llc", "company", "center", "centre", "station", "service",
    "services", "motel", "hotel", "inn", "pharmacy", "clinic", "hospital",
    "salon", "parking", "withdrawal", "deposit", "payment", "transfer",
    "atm", "credit", "union", "the", "and", "food", "foods", "liquor",
    "wine", "spirits", "gas", "fuel", "auto", "care", "health", "supply",
}


def _keepable(name: str) -> bool:
    """Is what survives a city strip still a usable merchant name?"""
    toks = name.split()
    if len(toks) >= 2:
        return True
    # 3 chars, not 4: 'Kfc', 'Cvs', 'Bmw' are real brands. A two-letter
    # survivor ('BP') is deliberately NOT keepable: a city strip that
    # leaves two letters standing alone has cut too much.
    return bool(toks) and len(toks[0]) >= 3 \
        and toks[0].lower() not in _GENERIC_ALONE


def _strip_cities(s: str, cities) -> str:
    """Drop a trailing configured city, repeatedly ('Acme Auto Spa Lac Sable
    Springfie' -> 'Acme Auto Spa'), while what survives is still a real
    name: 'Quillo Springfie' -> 'Quillo', but 'Bank Of Springfield' is left
    alone rather than collapsing to the shared word 'Bank'."""
    if not cities or not s:
        return s
    pat = re.compile(r"\s+(?:" + "|".join(re.escape(c) for c in cities)
                     + r")\s*$", re.I)
    prev = None
    while prev != s:
        prev = s
        cand = pat.sub("", s).strip()
        if cand != s and _keepable(cand):
            s = cand
    return s


# a run of hyphen-joined alphanumeric pieces ("1-800-Blooms", "WITHDRAWAL-CASH")
_MAX_LABEL = 256
_HYPHEN_RUN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")
# short pieces that are NOT a brand's short half — the hyphen beside them
# is punctuation ("CD-ONLINE", "PMT-ACH", a state or street suffix)
_NOBIND = _STOP | {"cd", "st", "rd", "dr", "ln", "wy", "hwy", "ave", "blvd",
                   "us", "usa", "onlin", "onl", "pos", "atm", "cash", "epay",
                   "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
                   "hi", "ia", "il", "in", "ks", "ky", "la", "ma", "md", "me",
                   "mi", "mn", "mo", "ms", "mt", "nc", "nd", "ne", "nh", "nj",
                   "nm", "nv", "ny", "oh", "ok", "or", "pa", "ri", "sc", "sd",
                   "tn", "tx", "ut", "va", "vt", "wa", "wi", "wv", "wy", "dc"}

# Two letters that are a place, a street suffix or an English function word
# rather than a name: what a bank appends ("Sample Diner Springfield IL",
# "Blue Kettle 5th St", "Prairie Brewing Co"). Consulted only for a TRAILING
# token, because leading, the same two letters are the business itself
# ("NW Bank", "NV Dmv Renewal") — position decides, not the word.
_SHORT_TAIL_NOISE = _NOBIND | {
    "id", "dd", "ck", "wd", "tr", "py", "of", "on", "to", "by", "as", "is",
    "it", "an", "my", "up", "we", "he", "me", "be", "do", "go", "if", "no",
    "le", "el", "il", "du", "di", "da", "am", "pm", "jr", "sr", "ll",
}
# Two letters that are never a name, wherever they sit: a card-processor or
# bank shorthand ('SQ Coffee House', 'CK Lakeside Lofts', 'CC Arena Tix',
# 'Acme Store Xx'). The processor wrappers _PROC removes are the same codes
# with their asterisk still attached.
_SHORT_CODE_NOISE = {"sq", "cc", "ck", "cd", "dd", "wd", "tr", "py", "po",
                     "xx", "zz"}
# A leading two-letter token is an initialism far more often than a word,
# so it keeps its capitals ('NW Bank', 'QX Fuel', 'JT Outfitters') — except
# for the words that really are words at the front of a name ('At Home',
# 'La Palma', 'In N Grill'). 'us' is deliberately NOT one of them: no
# merchant is called "Us".
_LEADING_WORD = {"at", "in", "on", "to", "by", "of", "la", "le", "el", "il",
                 "du", "de", "di", "da", "my", "no", "so", "up", "we", "he",
                 "me", "it", "is", "as", "an", "be", "do", "go", "if", "or",
                 "am", "pm", "st"}


def _bind_hyphens(m: re.Match) -> str:
    """Keep a hyphenated brand whole (the hyphen becomes a placeholder the
    scrub leaves alone) when some adjacent pair binds a piece of ≤2 chars
    that is not punctuation-noise; otherwise leave the hyphens to be
    scrubbed to spaces."""
    parts = m.group(0).split("-")
    for a, b in zip(parts, parts[1:]):
        short = min(len(a), len(b)) <= 2
        if short and a.lower() not in _NOBIND and b.lower() not in _NOBIND:
            return "\x00".join(parts)
    return m.group(0)


def _short_tokens_kept(toks: list[tuple[str, bool]]) -> list[str]:
    """Which two-letter words survive, by POSITION — because across the
    descriptor shapes banks produce, position is what predicts meaning:

    * LEADING, two letters are an initialism and usually the whole point of
      the name — 'NW Bank', 'JT Outfitters', 'QX Appliances', 'La Palma',
      'St Paul Fish', 'NV Dmv Renewal'. A blanket three-letter floor would
      delete the brand and keep the generic half ('Bank', 'Outfitters').
    * MID-DESCRIPTOR, they are bank shorthand — 'Withdrawal Kiosk QX
      Springfield', 'Payment Dir DB Ref', 'Pet Grooming QT Sal'.
    * TRAILING, they are usually a word the bank truncated ('Example
      Market Gr', 'Sample Optical Pr', 'Maple Fitness Cl') — except on a
      two-word name, where the second word IS the product: 'Nova Fi'.

    So: keep a leading one; keep a trailing one only on a two-token name and
    only when it is not a known locality/street/shorthand word; drop the
    rest. Under-naming is the safe failure here — a dropped word merges two
    spellings of one payee, an invented one splits a payee in two."""
    out: list[str] = []
    for i, (tok, short) in enumerate(toks):
        if not short:
            out.append(tok)
        elif tok.lower() in _SHORT_CODE_NOISE:
            continue                        # processor/bank shorthand
        elif not out:                       # leading initialism
            out.append(tok)
        elif (i == len(toks) - 1 and len(out) == 1
                and tok.lower() not in _SHORT_TAIL_NOISE):
            out.append(tok)                 # 'Nova Fi'
    return out


def canonical_merchant(name: str, cities=()) -> str:
    """Deterministic Layer-1 canonical form. Safe: strips known noise, never
    reduces a name to a lone shared word, so it under-merges rather than
    fusing different businesses. `cities` is the tenant's explicit
    strip-list (see cities_for) — empty by default, so behavior is
    unchanged for anyone who hasn't configured one."""
    # A bank descriptor is a few dozen characters; a label that arrives
    # through a file import can be anything. The scrub below is a chain of
    # regexes whose cost grows faster than linearly on long runs of one
    # character, so the label is cut first — nothing a real merchant name
    # carries lives past this point.
    s = (name or "")[:_MAX_LABEL]
    s = _NOISE.sub("", s)
    s = _PROC.sub("", s)
    s = _TAIL.sub("", s)
    geo = _GEO.sub("", s)
    if geo != s:
        # ONLY strip a street address on a label that demonstrably carries a
        # postal tail. Unconditionally cutting at "<number> <word>" would eat
        # real names — 'Route 66 Diner' -> 'Route', 'Highway 55 Burgers' ->
        # 'Highway'. The ', ST, US' tail is the evidence that the trailing
        # run is an address and not part of the business's name.
        stripped = _ADDR.sub("", geo)
        # never let the cut empty the name — under-merge, don't destroy
        s = stripped if stripped.strip(" ,.-") else geo
    # A hyphen BETWEEN letters or digits is part of the word when it binds a
    # SHORT piece — "Q-Pack", "K-O-A", "A-1 Auto", "9-Lantern", "1-800-Blooms"
    # are one token each. Cutting on it and then dropping digits and
    # sub-3-letter pieces would leave "Pack", "Auto", "Lantern", "Blooms":
    # the brand's own name stripped
    # as noise. A hyphen between two long words is a bank's
    # punctuation ("WITHDRAWAL-CASH", "Grand-Prairie", "CREDIT-INTEREST") and
    # still splits. Digits survive ONLY inside such a hyphenated word that
    # also carries letters — a bare phone number or date is still noise.
    s = _HYPHEN_RUN.sub(_bind_hyphens, s)
    s = re.sub(r"[^A-Za-z0-9 \x00]+", " ", s)
    toks = []
    for t in s.split():
        if "\x00" in t and re.search(r"[A-Za-z]", t):
            # a hyphenated word with a letter in it is a name, however
            # short its halves ("A-1", "7-11") — the hyphen counts
            t = t.replace("\x00", "-")
            if len(t) >= 3 and t.lower() not in _STOP:
                toks.append((t, False))
            continue
        # A whole two-letter WORD can be the name ("NW Bank", "JT Outfitters",
        # "Nova Fi"); a two-letter FRAGMENT never is. They
        # are told apart by where they came from: digits are noise that
        # SPLITS the token ("2K3JD9" is not a word "KJD", "29TH" is not
        # "Th"), so the letter runs a split leaves behind still need three
        # letters, while a token that was nothing but letters counts from
        # two. A lone letter is not name evidence unless a hyphen binds it
        # (Q-Pack) — banks prefix bare letters to descriptors.
        whole_word = t.isalpha()
        for piece in re.split(r"[^A-Za-z]+", t):
            if piece.lower() in _STOP:
                continue
            if len(piece) >= 3:
                toks.append((piece, False))
            elif len(piece) == 2 and whole_word:
                toks.append((piece, True))
    out = " ".join(_short_tokens_kept(toks)).strip().title()
    if out and len(first := out.split(" ", 1)[0]) == 2 and first.isalpha() \
            and first.lower() not in _LEADING_WORD:
        out = first.upper() + out[2:]
    if len(out) <= 2 and " " not in out:
        # two letters standing ALONE are not a name (the same judgement
        # _keepable makes about a city strip): a descriptor that shrank to
        # 'CC' has lost the payee, so keep the raw string instead of minting
        # a merchant out of an abbreviation
        out = ""
    out = out or (name or "").strip()
    return _strip_cities(out, cities)


# SQL fragments for display/grouping surfaces. Callers alias transactions as
# `t` and LEFT JOIN via MC_JOIN; group/label by DISPLAY_MERCHANT.
#
# merchant_outlet sits between the canonical map and the source's own words:
# a reviewed merge still wins (it is the user's answer), then the outlet a
# chain's fuel arm was identified as, then what the aggregator called it.
# The map cannot express an outlet itself — it is keyed by merchant STRING,
# and "Costco" resolves to the pump on one row and the warehouse on the
# next, so that decision is per transaction.
#
# The join therefore keys on the OUTLET when there is one, not on
# merchant_name. Keyed the other way, a merge row that maps a brand to
# itself ("Costco" -> "Costco", which the reviewed pass writes routinely)
# would outrank the outlet and silently collapse the pump back into the
# brand — the ledger showing the split while no reporting surface does.
# Keying on what is displayed also means a user can merge an outlet later,
# the same way they can merge anything else.
# (engine/reporting.py imports these same fragments.)
# The merchant is a ROW (merchants, joined as `mm` on
# transactions.merchant_id — engine/merchant_identity.py resolves it once
# per row). mm.name is the display name; the canonical string is a mirror
# kept for rows not yet resolved. MERCHANT_LOGO/MERCHANT_ID ride along for
# every surface that shows a merchant.
# (defined once in merchant_sql — import-free, so budget.py can share it)
MC_JOIN = merchant_sql.MC_JOIN
DISPLAY_MERCHANT = merchant_sql.DISPLAY_MERCHANT
MERCHANT_LOGO = merchant_sql.MERCHANT_LOGO
MERCHANT_ID = merchant_sql.MERCHANT_ID


def load_llm_map(conn, mapping: dict[str, str]) -> int:
    """Upsert reviewed llm merges (raw -> canonical); these win over layer1."""
    with conn.transaction():
        for raw, canon in mapping.items():
            if not raw or not canon:
                continue
            conn.execute(
                "INSERT INTO merchant_canonical (raw_merchant, canonical, method, as_of) "
                "VALUES (%s,%s,'llm',now()) "
                "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET "
                "canonical=EXCLUDED.canonical, method='llm', as_of=EXCLUDED.as_of",
                (raw, canon))
    # the merchant ROW follows the reviewed name (a raw string that got its
    # own layer1 merchant re-points at the one the merge names)
    from . import merchant_identity
    merchant_identity.reconcile_raws(conn, [r for r in mapping if r and mapping[r]])
    return len(mapping)


def apply(conn) -> int:
    """Idempotent, no-LLM. Gives every distinct raw merchant its layer1
    canonical WITHOUT overwriting any method='llm'/'manual' row. Cheap
    enough to run every sync. Returns rows touched."""
    # the OUTLET-first key — the same key the join uses, so an outlet
    # string gets its layer1 row minted and refreshed like any other
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(merchant_outlet, merchant_name, name) m "
        "FROM transactions WHERE removed=0 "
        "AND COALESCE(merchant_outlet, merchant_name, name) IS NOT NULL").fetchall()
    # refresh the harvested list first, so a newly-synced city takes effect
    # in this same pass rather than the next one
    try:
        from . import budget
        seen = harvest_cities(conn)
        # The unlocked read is only a cheap "is there anything to do" test —
        # the write itself takes the row lock. This runs in the NIGHTLY
        # worker, which is precisely the collision config_txn's docstring
        # names: the settings page saving while the worker writes back.
        if seen != (budget.load_config(conn).get("merchant_cities_seen") or []):
            with budget.config_txn(conn) as cfg:
                cfg["merchant_cities_seen"] = seen
    except Exception:                                     # noqa: BLE001
        log.warning("city harvest/persist skipped", exc_info=True)
    manual = cities_for(conn)
    own = harvest_city_map(conn)
    touched = 0
    recomputed: list[str] = []
    with conn.transaction():
        for r in rows:
            m = r["m"]
            here = manual
            if m in own:
                extra: set[str] = set()
                for city in own[m]:
                    extra |= _city_variants(city)
                here = tuple(sorted(set(manual) | extra, key=len, reverse=True))
            c = canonical_merchant(m, here)
            cur = conn.execute(
                "INSERT INTO merchant_canonical (raw_merchant, canonical, method, as_of) "
                "VALUES (%s,%s,'layer1',now()) "
                "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET "
                "canonical=EXCLUDED.canonical, as_of=EXCLUDED.as_of "
                # only recompute layer1 rows; 'llm' (reviewed) and 'manual'
                # (hand-renamed) canonicals are preserved across syncs
                "WHERE merchant_canonical.method='layer1' "
                "AND merchant_canonical.canonical!=EXCLUDED.canonical",
                (m, c))
            if cur.rowcount:
                recomputed.append(m)
            touched += cur.rowcount
    from . import merchant_identity
    # A layer1 canonical that CHANGED — the string cleaning improved, or a
    # newly-harvested city took effect — has to move its rows to the
    # merchant it now names, or the household keeps reading the old name
    # forever: the merchant row is what every surface displays, and the
    # resolve below only visits rows that have none. This is how a fix to
    # canonical_merchant reaches data that is already in the ledger.
    if recomputed:
        ids = [r["id"] for r in conn.execute(
            f"SELECT t.id FROM transactions t "
            f" WHERE {merchant_identity.RAW_KEY_SQL} = ANY(%s) "
            f"   AND t.removed = 0", (recomputed,)).fetchall()]
        if ids:
            merchant_identity.resolve(conn, txn_ids=ids, only_unresolved=False)
    # every row gets its merchant row (new strings, un-resolved backlog);
    # the full pass also prunes any merchant the re-resolve above emptied
    merchant_identity.resolve(conn)
    return touched


# ---- user-made naming, the layer that outranks every automatic one -------
# A person renaming or merging a merchant is the only ground truth about
# merchant identity this app gets: the automatic layers work from strings,
# and the user worked from having been there. So these write method='manual'
# rows, which layer1 never recomputes over, and each change is journalled to
# merchant_renames so it can be undone — the correction has to be at least
# as reversible as the thing it corrects.
#
# Rename and merge are ONE operation. "Rename X to Y" and "merge X into Y"
# differ only in whether a merchant called Y already exists, which is a
# question about the rest of the ledger, not about what to write. Keeping
# them one function is what stops them drifting into two behaviours.

def raw_strings_for(conn, display: str) -> list[str]:
    """Every raw merchant string currently displaying under this name.

    Deliberately GLOBAL — no personal/business scoping. A merchant's identity
    is one fact about the world, so renaming one moves every row that carries
    its name, including rows assigned to a business entity. Scoping this to
    the personal ledger would leave business rows stranded under the old name
    and split one payee in two. What must not cross the personal/business
    line is MONEY, and that is scoped where money is computed (`listing`)."""
    rows = conn.execute(
        f"SELECT DISTINCT COALESCE(t.merchant_outlet, t.merchant_name, "
        f"                         t.name) AS raw "
        f"  FROM transactions t {MC_JOIN} "
        f" WHERE t.removed = 0 AND {DISPLAY_MERCHANT} = %s", (display,)
    ).fetchall()
    return [r["raw"] for r in rows if r["raw"]]


def display_in_use(conn, display: str) -> bool:
    """Does ANY ledger row currently display under this name? The yes/no
    form of raw_strings_for — same global scope, same display rule — for
    callers that only branch on it: an existence probe stops at the first
    row instead of DISTINCT-ing every raw string across the whole ledger."""
    row = conn.execute(
        f"SELECT 1 AS one FROM transactions t {MC_JOIN} "
        f" WHERE t.removed = 0 AND {DISPLAY_MERCHANT} = %s LIMIT 1",
        (display,)).fetchone()
    return row is not None


def _existing_casings(conn, name: str) -> set[str]:
    """Every exact spelling already in use that equals `name` ignoring case —
    across what the ledger currently displays AND canonical targets already
    written (a merge target keeps existing even while its transactions are
    momentarily filtered out).

    Global for the same reason as raw_strings_for: the set of names in use is
    a fact about the whole ledger. A business-only merchant still occupies
    its spelling, so a personal rename typed with different casing must merge
    into it rather than fork a second bucket."""
    names = {r["d"] for r in conn.execute(
        f"SELECT DISTINCT {DISPLAY_MERCHANT} AS d "
        f"  FROM transactions t {MC_JOIN} "
        f" WHERE t.removed = 0 AND lower({DISPLAY_MERCHANT}) = lower(%s)",
        (name,)).fetchall()}
    names |= {r["canonical"] for r in conn.execute(
        "SELECT DISTINCT canonical FROM merchant_canonical "
        "WHERE lower(canonical) = lower(%s)", (name,)).fetchall()}
    names.discard(None)
    return names


def rename(conn, display: str, to: str) -> dict:
    """Point every raw string now shown as `display` at the name `to`.

    Returns {"raws": n, "rows": n} — how many distinct source strings moved
    and how many transactions they cover."""
    to = (to or "").strip()
    if not to:
        raise ValueError("the new name cannot be empty")
    raws = raw_strings_for(conn, display)
    if not raws:
        return {"raws": 0, "rows": 0}
    # The household identity lock comes BEFORE the row locks below. The
    # nightly split repair holds it while it calls this function, and
    # reconcile_raws takes it after the rows are locked; a rename from the
    # API that locked its rows first and then waited for the household
    # lock would face the repair waiting the other way round — a cycle
    # Postgres resolves by killing one of them, and when that is the
    # request the person sees a 500. Same lock, same order, everywhere.
    from . import merchant_identity
    merchant_identity._lock_identity(conn)
    # Rename and merge being one act means the target must be matched the
    # way a person means it: typing "costco" when "Costco" exists is a merge
    # into that bucket, not a second bucket differing only in case. Reuse
    # the existing spelling when it is unambiguous — the exact string typed
    # still wins outright, and when several distinct casings already exist
    # we keep what was typed rather than guess between them. The merchant
    # being renamed is excluded from the candidates: re-casing a name
    # ("Costco" -> "COSTCO") must not snap back to the old spelling.
    existing = _existing_casings(conn, to) - {display}
    if to not in existing and len(existing) == 1:
        to = next(iter(existing))
    # One locked read of every prior mapping. FOR UPDATE matters: two
    # concurrent renames touching the same raws would otherwise each read
    # the same "prior" and journal a fork of history. The lock is effective
    # because the API wraps this call in conn.transaction() — rows that do
    # not exist yet cannot be locked, but the unique index makes the
    # concurrent first-insert conflict instead of silently interleaving.
    #
    # Then LOOK AGAIN, because the member set was read before any lock
    # existed and no lock can cover a row nobody has inserted yet. The
    # nightly layer-1 pass folds newly-synced spellings under this display
    # name; one landing between the two reads would keep the old name and
    # quietly fork the merchant in two — half the rows renamed, half not,
    # and nothing to say so. Re-reading under the lock catches it; a
    # spelling that arrives after THAT belongs to the next pass.
    # Three reads, not two: the loop appends what it found and must lock
    # THAT batch too before it stops, or the last arrivals go in
    # unlocked and unjournalled (undo reads the journal).
    for _ in range(3):
        prior = {r["raw_merchant"]: r["canonical"] for r in conn.execute(
            "SELECT raw_merchant, canonical FROM merchant_canonical "
            "WHERE raw_merchant = ANY(%s) FOR UPDATE", (raws,)).fetchall()}
        joined = [r for r in raw_strings_for(conn, display) if r not in raws]
        if not joined:
            break
        raws = raws + joined
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical, "
            "                              to_canonical) VALUES (%s,%s,%s)",
            [(raw, prior.get(raw), to) for raw in raws])
        cur.executemany(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "                                as_of) VALUES (%s,%s,'manual',now()) "
            "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE "
            "SET canonical=EXCLUDED.canonical, method='manual', as_of=now()",
            [(raw, to) for raw in raws])
    # Counted over every row the rename actually moved — business rows
    # included — because that is what the act did. The catalog's own count is
    # personal-only (it is a personal-ledger view of the money), so the two
    # numbers can legitimately differ; the catalog reports the business
    # rows alongside so the difference is visible rather than mysterious.
    rows = conn.execute(
        "SELECT count(*) AS n FROM transactions t WHERE t.removed=0 AND "
        "COALESCE(t.merchant_outlet, t.merchant_name, t.name) = ANY(%s)",
        (raws,)).fetchone()["n"]
    # the merchant ROW follows: whole-merchant rename keeps the merchant
    # (and its logo); a rename onto an existing name is a merge
    from . import merchant_identity
    merchant_identity.reconcile_raws(conn, raws)
    return {"raws": len(raws), "rows": rows}


def undo(conn, change_id: int) -> dict:
    """Replay one journalled change backwards.

    A change that was the FIRST mapping for its raw string is undone by
    deleting the row outright, not by writing an empty one — layer1 owns
    the unmapped case and recomputes it on the next sync."""
    # Same lock, same order as rename() — see there.
    from . import merchant_identity
    merchant_identity._lock_identity(conn)
    row = conn.execute(
        "SELECT raw_merchant, from_canonical, to_canonical "
        "FROM merchant_renames WHERE id=%s", (change_id,)).fetchone()
    if not row:
        raise LookupError("no such change")
    # A journal entry is only reversible while it is still the live mapping.
    # Applied blindly, undoing an OLD entry would trample whatever a later
    # rename wrote — history replayed out of order. The same check guards
    # the delete branch: a first-mapping undo may only remove the row it
    # created, never a later one. FOR UPDATE pins the mapping between check
    # and write; effective because the API wraps this in conn.transaction().
    cur = conn.execute(
        "SELECT canonical FROM merchant_canonical WHERE raw_merchant=%s "
        "FOR UPDATE", (row["raw_merchant"],)).fetchone()
    if not cur or cur["canonical"] != row["to_canonical"]:
        raise LookupError("this change was superseded by a later rename")
    if row["from_canonical"] is None:
        conn.execute("DELETE FROM merchant_canonical WHERE raw_merchant=%s",
                     (row["raw_merchant"],))
    else:
        # Restored as method='manual' even when the prior mapping was
        # layer1's: the journal does not record the prior method (see the
        # module docstring), and 'manual' is the safe default — the person
        # undoing is asserting this is the right name, so freezing it
        # against recomputation honors that, at the cost of layer1 never
        # revisiting the string.
        conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "                                as_of) VALUES (%s,%s,'manual',now()) "
            "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE "
            "SET canonical=EXCLUDED.canonical, method='manual', as_of=now()",
            (row["raw_merchant"], row["from_canonical"]))
    conn.execute("DELETE FROM merchant_renames WHERE id=%s", (change_id,))
    from . import merchant_identity
    merchant_identity.reconcile_raws(conn, [row["raw_merchant"]])
    return {"raw_merchant": row["raw_merchant"],
            "restored": row["from_canonical"]}


def detail(conn, display: str) -> dict | None:
    """One merchant, all of it — for the expandable row on the merchants
    screen. The catalog row carries counts and variants; this adds what a
    person wants when they open one: the merchant row's own facts (logo,
    site, phone, kind, parent), the category its rule gives it, and WHERE
    it is — the places the ledger has seen it, most-visited first, with
    the latest coordinates so the client can offer a map. Locations come
    from the aggregator's per-transaction location (street address when
    given); a chain has several, an online merchant none. Personal money
    only, same as the catalog: the counts, the total, the category and the
    places are all personal rows; identity (variants, seen-range, the
    merchant row) is unscoped, like the catalog's. None when no row
    displays as `display`."""
    personal = f"COALESCE(true {budget.PERSONAL_ONLY_SQL}, false)"
    head = conn.execute(
        f"""SELECT {DISPLAY_MERCHANT} AS display,
                   count(*) FILTER (WHERE {personal}) AS rows,
                   round(COALESCE(sum(t.amount)
                         FILTER (WHERE {personal}), 0)::numeric, 2) AS total,
                   min(t.date) AS first, max(t.date) AS last,
                   array_agg(DISTINCT COALESCE(t.merchant_outlet,
                             t.merchant_name, t.name)) AS variants,
                   -- the keys a rule reaches these rows by (see the rule
                   -- lookup below): canonical strings and merchant rows
                   array_agg(DISTINCT COALESCE(mc.canonical, t.merchant_outlet,
                             t.merchant_name, t.name)) AS canons,
                   array_agg(DISTINCT mm.id::text)
                       FILTER (WHERE mm.id IS NOT NULL) AS mids,
                   max(mm.logo_url) AS logo, max(mm.website) AS website,
                   max(mm.phone) AS phone, max(mm.kind) AS kind,
                   max(mm.mcc) AS mcc,
                   (max(mm.plaid_entity_id) IS NOT NULL) AS plaid,
                   max(pm.name) AS parent,
                   -- the category the ledger shows for this merchant,
                   -- by majority of its PERSONAL rows (override, else
                   -- working) — a business account's visits to the same
                   -- store are its own books' concern
                   mode() WITHIN GROUP (ORDER BY COALESCE(
                       t.category_override, t.category_primary))
                       FILTER (WHERE {personal}) AS category
              FROM transactions t {MC_JOIN}
              LEFT JOIN merchants pm ON pm.id = mm.parent_id
             WHERE t.removed = 0 AND {DISPLAY_MERCHANT} = %s
             GROUP BY 1""", (display,)).fetchone()
    if head is None:
        return None
    out = dict(head)
    canons = out.pop("canons") or []
    mids = out.pop("mids") or []
    # A place is an ADDRESS when the aggregator gives one, else the city —
    # so two outlets in one city are two places, and the address, postal
    # code and pin all come from the SAME row (the latest visit there):
    # picked independently, max(address) and the newest coordinates could
    # name one outlet and pin another. Personal rows only, as the head.
    out["locations"] = [dict(r) for r in conn.execute(
        f"""SELECT l.city, l.region, l.address, l.postal, l.lat, l.lon,
                   l.n, l.last
              FROM (SELECT t.location_city AS city, t.location_region AS region,
                           t.location_address AS address,
                           (array_agg(t.location_postal ORDER BY t.date DESC)
                              FILTER (WHERE t.location_postal IS NOT NULL))[1]
                               AS postal,
                           (array_agg(t.location_lat ORDER BY t.date DESC, t.id)
                              FILTER (WHERE t.location_lat IS NOT NULL
                                        AND t.location_lon IS NOT NULL))[1] AS lat,
                           (array_agg(t.location_lon ORDER BY t.date DESC, t.id)
                              FILTER (WHERE t.location_lat IS NOT NULL
                                        AND t.location_lon IS NOT NULL))[1] AS lon,
                           count(*) AS n, max(t.date) AS last
                      FROM transactions t {MC_JOIN}
                     WHERE t.removed = 0 AND {DISPLAY_MERCHANT} = %s
                       AND (t.location_city IS NOT NULL
                            OR t.location_address IS NOT NULL
                            OR t.location_lat IS NOT NULL)
                       {budget.PERSONAL_ONLY_SQL}
                     GROUP BY 1, 2, 3) l
             ORDER BY l.n DESC, l.last DESC LIMIT 5""", (display,)).fetchall()]
    # The user's own rule for it, if any (what new rows will get) — found
    # the way apply() finds it (llm_categorize._rules_cte): a rule's string
    # resolves through merchant_canonical to a canonical and, when the
    # ledger knows the merchant, to its merchant row; a rule reaches these
    # rows when either key matches. Plain string equality against the
    # display name would miss a rule stored under a spelling a rename has
    # since moved on from, and the card would say "no rule" for a merchant
    # the rule is actively categorizing.
    # …and never a flow-named rule: apply() refuses to act on one (its
    # rules CTE filters them, see llm_categorize), so reporting it here
    # would make the card claim a rule that categorizes nothing.
    # Asked from the keys' side, not the rules': the strings a matching
    # rule could be stored under are few (these canonicals, the raw
    # spellings that resolve to them or to these merchant rows, and the
    # rows' own names), so the rules table is probed by an equality on
    # its key rather than testing every rule against the merchants table.
    from .categories import FLOW_SQL as _FLOW_SQL
    rule = conn.execute(
        f"""WITH keys AS (
                SELECT unnest(%s::text[]) AS k
                UNION SELECT raw_merchant FROM merchant_canonical
                       WHERE canonical = ANY(%s) OR merchant_id::text = ANY(%s)
                UNION SELECT name FROM merchants
                       WHERE id::text = ANY(%s) AND merged_into IS NULL)
            SELECT m.category_primary, m.source
              FROM merchant_categories m
             WHERE NOT m.disabled
               AND COALESCE(m.category_primary, '') NOT IN {_FLOW_SQL}
               AND (m.merchant IN (SELECT k FROM keys)
                    OR lower(m.merchant) IN (SELECT lower(k) FROM keys))
             ORDER BY (m.source = 'user') DESC, m.classified_at DESC LIMIT 1""",
        (canons, canons, mids, mids)).fetchone()
    out["rule"] = dict(rule) if rule else None
    return out


def listing(conn, q: str = "", limit: int = 200,
            order: str = "count") -> list[dict]:
    """Merchants as the ledger shows them: name, how many rows, how much,
    and the raw strings feeding each — the variants are the whole point,
    since a merchant looking wrong is usually several strings underneath.

    Identity is global, money is personal. Every merchant appears, so the
    catalog can never hide a name `rename` can reach, but `rows`/`total`
    count only the personal ledger and `business_rows` says how many rows
    sit behind an entity — which is why a rename can report more
    transactions moved than the catalog shows. `variant_count` is the true
    number of source strings; `variants` is that list capped at 50. The
    seen-range (`first`/`last`) is unscoped, like the name itself: it says
    when this payee was seen, not what was spent.

    `order`: "count" (busiest first, the default) or "alpha" — the catalog
    is capped, and under count-order the cap cuts exactly the long-tail
    merchants most likely to need correction, so the screen needs a way to
    walk the tail. A search (`q`) filters BEFORE the limit, so matches are
    never starved by the cap."""
    if order not in ("count", "alpha"):
        raise ValueError("order must be 'count' or 'alpha'")
    args: list = []
    # Every merchant is LISTED, business-only ones included: the catalog must
    # never hide a name that `rename` can still reach, or the screen offers no
    # way to correct it and the rename's row count comes from nowhere. What
    # stays personal is the MONEY — count and total are computed with the same
    # shared predicate every personal report uses (budget.PERSONAL_ONLY_SQL,
    # which honors the combined-view toggle), applied per row via FILTER.
    #
    # PERSONAL_ONLY_SQL is a WHERE fragment ("AND (…)"); read as a value it is
    # NULL when the session vars are unset, and a WHERE drops NULL rows, so
    # COALESCE(..., false) is what makes it mean the same thing inside FILTER.
    personal = f"COALESCE(true {budget.PERSONAL_ONLY_SQL}, false)"
    where = ""
    if q.strip():
        # The search text is literal text, not a pattern: escape LIKE's
        # wildcards so searching "%" finds merchants containing a percent
        # sign instead of returning everything. Backslash is Postgres's
        # default LIKE escape character.
        esc = (q.strip().replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
        where = f"WHERE {DISPLAY_MERCHANT} ILIKE %s"
        args.append(f"%{esc}%")
    args.append(max(1, min(int(limit), 500)))
    # Ordinals, because the sort keys are the aliased aggregates and `rows`
    # is a keyword in Postgres: 1 = display, 2 = the personal row count. The
    # name breaks count ties so paging the cap is deterministic.
    order_sql = "1 ASC" if order == "alpha" else "2 DESC, 1 ASC"
    # Variants are capped at 50 per merchant: a payee with thousands of raw
    # strings (Mint amount-embedding descriptors) would otherwise bloat the
    # payload, and past 50 the list stops informing anyone. `variant_count`
    # carries the true total alongside, because the clients render the list's
    # length as a fact ("N source names") and at the cap that fact would lie.
    # Both are unscoped, matching what a rename would move (raw_strings_for).
    #
    # Two stages, one scan. The display name is a function of the row's
    # merchant_id and its raw strings, so the ledger is first collapsed to one
    # row per distinct (merchant_id, outlet, merchant name, name) — a few
    # thousand tuples instead of every transaction — carrying the counts,
    # sums and date range each tuple contributes. The merchant join, the
    # display grouping, the variant lists and the search filter then run
    # over those tuples, exactly as they ran over the rows: same joins, same
    # expressions, same figures (a sum of per-tuple sums; the DISTINCT
    # variant aggregates sort thousands of values, not tens of thousands per
    # merchant). The CTE is aliased `t` so merchant_sql's MC_JOIN and
    # DISPLAY_MERCHANT apply verbatim — the one definition, not a copy.
    # The personal/business test is decided once per row in the innermost
    # subquery; `OFFSET 0` keeps the planner from inlining that subquery and
    # re-evaluating the predicate for each of the three FILTERs that read it.
    return conn.execute(
        f"""WITH t AS (
                SELECT r.merchant_id, r.merchant_outlet, r.merchant_name, r.name,
                       count(*) FILTER (WHERE r.personal) AS n_personal,
                       sum(r.amount) FILTER (WHERE r.personal) AS amt_personal,
                       count(*) FILTER (WHERE NOT r.personal) AS n_business,
                       min(r.date) AS first_seen, max(r.date) AS last_seen
                  FROM (SELECT t.merchant_id, t.merchant_outlet, t.merchant_name,
                               t.name, t.amount, t.date, {personal} AS personal
                          FROM transactions t
                         WHERE t.removed = 0 OFFSET 0) r
                 GROUP BY 1, 2, 3, 4)
            SELECT {DISPLAY_MERCHANT} AS display,
                   sum(t.n_personal)::bigint AS rows,
                   round(COALESCE(sum(t.amt_personal), 0)::numeric, 2) AS total,
                   sum(t.n_business)::bigint AS business_rows,
                   min(t.first_seen) AS first, max(t.last_seen) AS last,
                   (array_agg(DISTINCT COALESCE(t.merchant_outlet,
                              t.merchant_name, t.name)))[1:50] AS variants,
                   count(DISTINCT COALESCE(t.merchant_outlet,
                         t.merchant_name, t.name)) AS variant_count,
                   -- the merchant ROW's facts (after the ordinals the ORDER BY
                   -- uses: 1 = display, 2 = the personal row count)
                   max(mm.logo_url) AS logo, max(mm.website) AS website,
                   max(mm.kind) AS kind,
                   (max(mm.plaid_entity_id) IS NOT NULL) AS plaid,
                   -- an outlet's brand ("Costco Gas" → Costco)
                   max(pm.name) AS parent
              FROM t {MC_JOIN}
              LEFT JOIN merchants pm ON pm.id = mm.parent_id
             {where}
             GROUP BY 1 ORDER BY {order_sql} LIMIT %s""", args).fetchall()
