"""Merchant-name consolidation for imported (non-aggregator) ledger rows.

Mint/Simplifi/bank-PDF rows carry the raw bank descriptor as their merchant
name (aggregator rows arrive pre-cleaned), so one real payee shows up under
many strings — 'ACME PROPERTY MGMT WEB PAYMENT REF 1234' vs 'ACME
PROPERTY MGMT WEB PMT', or 'Acme Lending' vs 'Acme Lending Student'.

Three layers, all writing to the `merchant_canonical` map (raw -> canonical),
never touching `name`/`merchant_name`/`raw`. Display surfaces read the
merchant row's name, else this map, else the row's own identity key — the
DISPLAY_MERCHANT fragment below, joined per MC_JOIN, both spelled once in
merchant_sql — so the fix is reversible (drop the rows) and clobber-proof
(sync only writes name/merchant_name):

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

import datetime as dt
import logging
import re

from . import budget, merchant_sql
from .compat import jsonb

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

# An apostrophe is a LETTER-JOINER here, never a word break: it is DELETED
# before the scrub rather than turned into a space. Which spelling reaches
# us is an accident of the source — one feed resolves a payee as "Juniper's
# Market" while the card line for the same purchase prints "JUNIPERS
# MARKET" — and the canonical form has to come out the same string either
# way, or one business becomes two merchants with the money split between
# them until somebody merges them by hand. Broken on, "Juniper's" yields
# the pieces "Juniper" and "s", the one-letter piece goes as noise, and
# this payee is "Juniper Market" while its own bank line is "Junipers
# Market".
#
# Deleting the punctuation is also exactly what a bank does to a name it
# prints, so the joined form is the one spelling BOTH sources can produce,
# and it is the reading engine/merchant_identity compares a descriptor
# under (its `_joined_words`). The two modules have to agree: a name this
# function cleaned is matched against the lines it came from.
#
# The same one rule runs over an elided prefix — "L'Anfora" -> "Lanfora" —
# which agrees with a line printing it closed up ("LANFORA") and not with
# one that spaces it out ("L ANFORA", whose lone letter is dropped as a
# bank's letter prefix, the way "N BROADWAY MARKET" loses its "N"). One
# rule in one direction is the point: a possessive that joins beside a
# prefix that splits is two rules for a reader to hold, and the comparison
# that genuinely needs both spellings takes the union of the two readings
# rather than asking this function to pick differently per name.
#
# Only an apostrophe that FOLLOWS a letter or digit joins. A leading one is
# a quote mark standing in front of a word, and the word break it sits on
# is real. All three characters a feed may write it with count: ASCII, the
# typographic right single quote, and the modifier letter a few sources use.
_APOSTROPHE = re.compile(r"(?<=[A-Za-z0-9])['’ʼ]")


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
    # the same scrub the label got, apostrophe rule included, or a town
    # spelled with one ("Anse d'Or") would be generated as two words the
    # cleaned label no longer contains
    scrubbed = " ".join(
        t for t in re.sub(r"[^A-Za-z ]+", " ",
                          _APOSTROPHE.sub("", city)).split()
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


# a run of hyphen-joined alphanumeric pieces ("1-800-Blooms", "XFER-OUT")
_MAX_LABEL = 256
_HYPHEN_RUN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")
# short pieces that are NOT a brand's short half — the hyphen beside them
# is punctuation ("ATM-WEB", "PMT-ACH", a state or street suffix)
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
      'QB Fish Market', 'ZR Licensing Office'. A blanket three-letter floor would
      delete the brand and keep the generic half ('Bank', 'Outfitters').
    * MID-DESCRIPTOR, they are bank shorthand — 'Withdrawal Kiosk QX
      Springfield', 'Payment Dir DB Ref', 'Bike Repair QT Sal'.
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
    # punctuation ("XFER-OUT", "WIRE-TRANSFER", "CHARGE-SERVICE") and
    # still splits. Digits survive ONLY inside such a hyphenated word that
    # also carries letters — a bare phone number or date is still noise.
    s = _HYPHEN_RUN.sub(_bind_hyphens, s)
    # an apostrophe joins the letters it sits between instead of breaking
    # them apart — see _APOSTROPHE
    s = _APOSTROPHE.sub("", s)
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
# and "Northwind Club" resolves to the pump on one row and the warehouse on the
# next, so that decision is per transaction.
#
# The join therefore keys on the OUTLET when there is one, not on
# merchant_name. Keyed the other way, a merge row that maps a brand to
# itself ("Northwind Club" -> "Northwind Club", which the reviewed pass writes routinely)
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
    from . import merchant_identity
    with conn.transaction():
        # identity before the alias rows, written one at a time in the
        # map's order: see apply()
        merchant_identity._lock_identity(conn)
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
    merchant_identity.reconcile_raws(conn, [r for r in mapping if r and mapping[r]])
    return len(mapping)


def apply(conn) -> int:
    """Idempotent, no-LLM. Gives every distinct raw merchant its layer1
    canonical WITHOUT overwriting any method='llm'/'manual' row. Cheap
    enough to run every sync. Returns rows touched."""
    # the identity key — the same key the join uses, so an outlet string
    # gets its layer1 row minted and refreshed like any other, and a row
    # whose aggregator name was set aside mints one for its bank line
    # rather than for the name it no longer answers to
    rows = conn.execute(
        f"SELECT DISTINCT {merchant_sql.RAW_KEY_UNALIASED} m "
        "FROM transactions WHERE removed=0 "
        f"AND {merchant_sql.RAW_KEY_UNALIASED} IS NOT NULL").fetchall()
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
    # the merchant each machine-written alias names NOW, so a key whose
    # canonical changes below can say which merchant its rows are leaving
    held = {r["raw_merchant"]: r["merchant_id"] for r in conn.execute(
        "SELECT raw_merchant, merchant_id FROM merchant_canonical "
        " WHERE method = 'layer1'").fetchall()}
    from . import merchant_identity
    with conn.transaction():
        # The identity lock before the first alias row, as every other
        # writer of alias rows takes it. This loop locks the rows one at a
        # time in its query's order; a rename locks the same rows in one
        # statement in its scan's order, and two writers taking one set of
        # rows in two orders can each hold a row the other is waiting for.
        merchant_identity._lock_identity(conn)
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
    # A layer1 canonical that CHANGED — the string cleaning improved, or a
    # newly-harvested city took effect — has to move its rows to the
    # merchant it now names, or the household keeps reading the old name
    # forever: the merchant row is what every surface displays, and the
    # resolve below only visits rows that have none. This is how a fix to
    # canonical_merchant reaches data that is already in the ledger.
    #
    # The merchant those rows LEAVE is retired into the one they went to
    # rather than dropped, and what was stored under its name follows: see
    # merchant_identity.re_resolve_recleaned and _rekey_rules.
    if recomputed:
        for pair in merchant_identity.re_resolve_recleaned(
                conn, {m: held.get(m) for m in recomputed}):
            _rekey_rules(conn, pair["from"], pair["into"])
    # every row gets its merchant row (new strings, un-resolved backlog);
    # the full pass also prunes any merchant the re-resolve above emptied
    merchant_identity.resolve(conn)
    return touched


def _rekey_rules(conn, old: str, new: str) -> int:
    """Move the category rules stored under a retired merchant's name to the
    name of the merchant its rows went to.

    A rule is kept under a STRING. It reaches its rows through that
    string's alias row, or else through a live merchant of that exact name
    — so a rule written under a display name stops matching anything the
    night the string clean renames the merchant, silently: the household's
    correction is still on the Rules page and no longer does anything. A
    rule keyed on a raw spelling is left alone (its alias row moved with
    the rows), and so is one whose name another live merchant still
    carries.

    Where the new name already has a rule, a person's outranks a machine's
    and otherwise the one already there stays: it is the rule the surviving
    merchant's rows have been living under."""
    if not old or not new or old.lower() == new.lower():
        return 0
    moved = 0
    with conn.transaction():
        rules = conn.execute(
            """SELECT m.merchant, m.source FROM merchant_categories m
                WHERE lower(m.merchant) = lower(%s)
                  AND NOT EXISTS (SELECT 1 FROM merchant_canonical a
                                   WHERE a.raw_merchant = m.merchant)
                  AND NOT EXISTS (SELECT 1 FROM merchants l
                                   WHERE l.merged_into IS NULL
                                     AND lower(l.name) = lower(m.merchant))
                ORDER BY (m.source <> 'user'), m.classified_at DESC
                  FOR UPDATE OF m""", (old,)).fetchall()
        for i, r in enumerate(rules):
            there = conn.execute(
                "SELECT source FROM merchant_categories WHERE merchant = %s "
                "FOR UPDATE", (new,)).fetchone()
            wins = i == 0 and (there is None or (
                r["source"] == "user" and there["source"] != "user"))
            if wins and there is not None:
                conn.execute("DELETE FROM merchant_categories "
                             "WHERE merchant = %s", (new,))
            if wins:
                moved += conn.execute(
                    "UPDATE merchant_categories SET merchant = %s "
                    "WHERE merchant = %s", (new, r["merchant"])).rowcount
            else:
                conn.execute("DELETE FROM merchant_categories "
                             "WHERE merchant = %s", (r["merchant"],))
    return moved


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
        f"SELECT DISTINCT {merchant_sql.RAW_KEY} AS raw "
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
    # way a person means it: typing "northwind" when "Northwind" exists is a merge
    # into that bucket, not a second bucket differing only in case. Reuse
    # the existing spelling when it is unambiguous — the exact string typed
    # still wins outright, and when several distinct casings already exist
    # we keep what was typed rather than guess between them. The merchant
    # being renamed is excluded from the candidates: re-casing a name
    # ("Northwind" -> "NORTHWIND") must not snap back to the old spelling.
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
    # the journal rows' ids, so the rule note below lands on exactly these
    journal_ids = [r["id"] for r in conn.execute(
        "INSERT INTO merchant_renames (raw_merchant, from_canonical, to_canonical) "
        "SELECT r, p, %s FROM unnest(%s::text[], %s::text[]) AS j(r, p) "
        "RETURNING id", (to, raws, [prior.get(raw) for raw in raws])).fetchall()]
    with conn.cursor() as cur:
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
        f"{merchant_sql.RAW_KEY} = ANY(%s)",
        (raws,)).fetchone()["n"]
    # the merchant ROW follows: whole-merchant rename keeps the merchant
    # (and its logo); a rename onto an existing name is a merge
    # the merchants that displayed the name, read before the rows move:
    # what a bill holding them pays
    old_ids = [r["mid"] for r in conn.execute(
        f"SELECT DISTINCT t.merchant_id::text AS mid FROM transactions t "
        f" WHERE t.removed = 0 AND t.merchant_id IS NOT NULL "
        f"   AND {merchant_sql.RAW_KEY} = ANY(%s)", (raws,)).fetchall()]
    # the offers and bills the rename rewrites are locked before any row
    # moves: an approval holds the same pair while it stamps those rows,
    # and reaching for them after the rows would close a cycle with it
    from . import bills
    bills.lock_merchant_rename(conn, display, old_ids)
    from . import merchant_identity
    merchant_identity.reconcile_raws(conn, raws)
    # a bill paying the old name pays the new one from this transaction on,
    # not from whenever the nightly next reads its refs
    bills.follow_merchant_rename(conn, display, old_ids, raws)
    # and a 1099 vendor mark: the year-end list must not drop a contractor
    # because their payee was renamed or merged
    from . import selfemploy
    selfemploy.follow_merchant_rename(conn, display, old_ids, raws)
    # and so does the category the household taught the old name
    note = follow_rules(conn, display, to, raws, old_ids)
    if note is not None:
        _journal_note(conn, note, journal_ids)
    return {"raws": len(raws), "rows": rows}


def _journal_note(conn, note: dict, ids: list, *,
                  only_unnoted: bool = False) -> None:
    """Write a rename's rule note onto its journal rows.

    Every row carries the note, so undoing any one string can put the rules
    back, but not the rename's list of strings: a merchant shown for
    thousands of bank lines would copy that whole list onto each of its
    thousands of rows. The rows share a group token instead, and the list
    is read back from the rows that carry it (`_note_raws`). The token is
    random, not a row id, so groups brought in by a restore can never be
    mistaken for one made here."""
    import uuid
    slim = {k: v for k, v in note.items() if k != "raws"}
    slim["group"] = uuid.uuid4().hex
    conn.execute(
        "UPDATE merchant_renames SET rules = %s WHERE id = ANY(%s)"
        + (" AND rules IS NULL" if only_unnoted else ""),
        (jsonb(slim), list(ids)))


def _note_raws(conn, note: dict) -> list[str]:
    """The raw strings of the rename a note belongs to: the list itself on
    a note written before the group token, else the journal rows still
    carrying its token (an undone string has left the journal, and with it
    the list)."""
    if note.get("raws") is not None:
        return list(note.get("raws") or [])
    group = note.get("group")
    if not group:
        return []
    return [r["raw_merchant"] for r in conn.execute(
        "SELECT raw_merchant FROM merchant_renames "
        "WHERE rules->>'group' = %s", (str(group),)).fetchall()]


# ---- the category rules a rename carries -----------------------------------
# A rule is stored under a STRING and reaches its rows through that string's
# alias row, or else through a live merchant of that exact name (see the
# rules CTE in llm_categorize.apply). A rename moves every alias of a name
# to the merchant it now names, so a rule under the old DISPLAY name is left
# reaching nothing: the household's correction still shows on the Rules
# page, files no charge, and a machine guess fills the gap. A rule under one
# of the moved raw strings has the opposite problem: it follows its alias to
# the survivor and, beside a survivor rule of another category, the two
# cancel out (the CTE applies a merchant's rules only when they agree).
#
# So the rename settles it once, in its own transaction: of the old name's
# rules and the survivor's, a person's outranks a machine's and otherwise
# the survivor's stays — it is the rule the survivor's own rows have been
# living under, and a rename must never overwrite what the person told the
# survivor. Everything set aside is written into the rename's journal rows,
# so undo puts it back.

def _rule_dict(r) -> dict:
    return {"merchant": r["merchant"], "category_primary": r["category_primary"],
            "source": r["source"], "disabled": bool(r["disabled"]),
            "classified_at": r["classified_at"].isoformat()
            if r["classified_at"] is not None else None}


_RULE_COLS = "m.merchant, m.category_primary, m.source, m.disabled, m.classified_at"
# a person's live rule first, then the newest
_RULE_ORDER = "ORDER BY m.disabled, (m.source <> 'user'), m.classified_at DESC"


def rules_reaching(conn, name: str, mid, *, exclude=(), lock: bool = False) -> list:
    """The category rules that reach one merchant: those under its name
    (any case) and those under a raw string whose alias names it. Best
    first. `exclude` leaves out rules under these strings."""
    return conn.execute(
        f"""SELECT {_RULE_COLS} FROM merchant_categories m
             WHERE NOT (m.merchant = ANY(%s))
               AND (lower(m.merchant) = lower(%s)
                    OR EXISTS (SELECT 1 FROM merchant_canonical a
                                WHERE a.raw_merchant = m.merchant
                                  AND a.merchant_id = %s))
             {_RULE_ORDER}{" FOR UPDATE OF m" if lock else ""}""",
        (list(exclude), name, mid)).fetchall()


def _put_rule(conn, rule: dict, key: str | None = None) -> bool:
    """Write a journalled rule back under `key` (its own string by default)
    unless something already lives there: a rule written since is the
    household's newer word and is never overwritten."""
    cur = conn.execute(
        "INSERT INTO merchant_categories (merchant, category_primary, source, "
        "    disabled, classified_at) VALUES (%s,%s,%s,%s,COALESCE(%s::timestamptz, now())) "
        "ON CONFLICT (tenant_id, merchant) DO NOTHING",
        (key or rule["merchant"], rule["category_primary"], rule["source"],
         rule.get("disabled", False), rule.get("classified_at")))
    return cur.rowcount > 0


def _outranks(rule: dict, held: dict | None) -> bool:
    """Whether `rule` may take a key `held` already occupies. A person's
    live rule beats a machine's (or a disabled one); nothing else ever
    displaces what is there — a machine rule never overwrites a rule of
    any source, and of two people's rules the one already there stays."""
    if held is None:
        return True
    return (not rule["disabled"] and rule["source"] == "user"
            and (held["disabled"] or held["source"] != "user"))


def _rule_rank(d: dict) -> tuple:
    """_RULE_ORDER, for rules held in memory: live first, a person's first,
    then the newest."""
    at = d.get("classified_at") or ""
    return (d["disabled"], d["source"] != "user",
            -dt.datetime.fromisoformat(at).timestamp() if at else 0.0)


def _plan_follow(conn, display: str, into: str, raws: list[str], old_ids=(), *,
                 lock: bool = False, gone=frozenset(), placed=()) -> dict | None:
    """Decide what follow_rules would do, reading only. `gone` and `placed`
    let a dry run over several renames see what the earlier ones in the same
    run would have removed and carried, without writing either."""
    if not display or not into or display.lower() == into.lower() or not raws:
        return None
    from . import merchant_identity
    row = conn.execute(
        "SELECT merchant_id FROM merchant_canonical WHERE raw_merchant=%s",
        (raws[0],)).fetchone()
    mid = merchant_identity._survivor(conn, row["merchant_id"]) \
        if row and row["merchant_id"] else None
    if mid is None:
        return None
    old_ids = [str(i) for i in old_ids if i]
    # the old name's rules: under the name itself, unless another live
    # merchant still carries that name and so still answers to them, and
    # under the strings that moved. A rule under the survivor's own name is
    # the survivor's even when that string moved too: it is the key the old
    # name's rule would be written to, so it competes rather than travels.
    mine = [_rule_dict(r) for r in conn.execute(
        f"""SELECT {_RULE_COLS} FROM merchant_categories m
             WHERE lower(m.merchant) <> lower(%s)
               AND (m.merchant = ANY(%s)
                OR (lower(m.merchant) = lower(%s)
                    AND NOT EXISTS (SELECT 1 FROM merchant_canonical a
                                     WHERE a.raw_merchant = m.merchant)
                    AND NOT EXISTS (SELECT 1 FROM merchants l
                                     WHERE l.merged_into IS NULL
                                       AND lower(l.name) = lower(m.merchant)
                                       AND l.id <> %s
                                       AND NOT (l.id::text = ANY(%s)))))
             {_RULE_ORDER}{" FOR UPDATE OF m" if lock else ""}""",
        (into, raws, display, mid, old_ids)).fetchall()]
    mine = [d for d in mine if d["merchant"] not in gone]
    if not mine:
        return None
    there = [_rule_dict(r) for r in rules_reaching(
        conn, into, mid, exclude=[d["merchant"] for d in mine], lock=lock)]
    there = sorted([d for d in there if d["merchant"] not in gone]
                   + [d for d in placed if d["merchant"] not in gone],
                   key=_rule_rank)
    best = mine[0]
    wins = _outranks(best, there[0] if there else None)
    rules, displaced = [], [dict(d) for d in there] if wins else []
    for i, d in enumerate(mine):
        d = dict(d)
        if i == 0 and wins and d["merchant"] in raws:
            d["fate"] = "kept"           # its alias already carries it over
        elif i == 0 and wins:
            d["fate"] = "moved"
        else:
            d["fate"] = "dropped"
        rules.append(d)
    if all(d["fate"] == "kept" for d in rules) and not displaced:
        return None
    return {"from": display, "into": into, "raws": sorted(raws),
            "rules": rules, "displaced": displaced}


def _write_follow(conn, note: dict) -> dict:
    """Carry out a planned follow. The losers go first, so the key the
    winner moves to is free; the move itself is still an upsert that only
    lands where the winner outranks what it finds, so a rule that appeared
    under the key since the plan was read is kept and the move is recorded
    as dropped instead of failing the whole rename."""
    into = note["into"]
    for d in note["displaced"]:
        conn.execute("DELETE FROM merchant_categories WHERE merchant = %s",
                     (d["merchant"],))
    for d in note["rules"]:
        if d["fate"] == "dropped":
            conn.execute("DELETE FROM merchant_categories WHERE merchant = %s",
                         (d["merchant"],))
    for d in note["rules"]:
        if d["fate"] != "moved":
            continue
        landed = conn.execute(
            """INSERT INTO merchant_categories (merchant, category_primary,
                   source, disabled, classified_at)
               VALUES (%s,%s,%s,%s,COALESCE(%s::timestamptz, now()))
               ON CONFLICT (tenant_id, merchant) DO UPDATE SET
                   category_primary = EXCLUDED.category_primary,
                   source = EXCLUDED.source, disabled = EXCLUDED.disabled,
                   classified_at = EXCLUDED.classified_at
               WHERE EXCLUDED.source = 'user' AND NOT EXCLUDED.disabled
                 AND (merchant_categories.source <> 'user'
                      OR merchant_categories.disabled)""",
            (into, d["category_primary"], d["source"], d["disabled"],
             d["classified_at"])).rowcount
        conn.execute("DELETE FROM merchant_categories WHERE merchant = %s",
                     (d["merchant"],))
        if not landed:
            d["fate"] = "dropped"
    return note


def follow_rules(conn, display: str, into: str, raws: list[str], old_ids=()) -> dict | None:
    """Carry the category rules of the name `display` to the merchant its
    raw strings `raws` were just filed under as `into`. Called by rename
    after the rows moved, inside its transaction; also by the repair of
    renames made before this existed. `old_ids` are the merchants that
    displayed the name before the move (a twin the caller retires after
    the rename is still live under the old name at this point).

    Returns the journal note for undo, or None when no rule moved."""
    note = _plan_follow(conn, display, into, raws, old_ids, lock=True)
    return _write_follow(conn, note) if note is not None else None


def unfollow_rules(conn, note: dict | None, raw_key: str) -> None:
    """The undo of follow_rules for one raw string whose rename was just
    reversed. The old name's rules come back under their own strings at
    once, so the undone string's rows are filed as before; the survivor
    gives back what it was handed, and gets back what was set aside, only
    when the last string of that rename has gone back. Nothing written
    since — a rule the person edited or added — is overwritten."""
    if not isinstance(note, dict):
        return
    for d in note.get("rules") or []:
        if d.get("fate") in ("moved", "dropped"):
            _put_rule(conn, d)
    into = str(note.get("into") or "")
    left = conn.execute(
        "SELECT 1 FROM merchant_canonical WHERE raw_merchant = ANY(%s) "
        "AND raw_merchant <> %s AND lower(canonical) = lower(%s) LIMIT 1",
        (_note_raws(conn, note), raw_key, into)).fetchone()
    if left:
        return
    for d in note.get("rules") or []:
        if d.get("fate") == "moved":
            conn.execute(
                "DELETE FROM merchant_categories WHERE merchant = %s "
                "AND category_primary = %s AND source = %s",
                (into, d["category_primary"], d["source"]))
    for d in note.get("displaced") or []:
        _put_rule(conn, d)


def _regroup_locked(conn, ids: list, old: str, mid: str):
    """One repair group as it stands now, read under the identity lock:
    (survivor's live name, journal ids, raw strings) of the rows that still
    carry no note and still leave `old` for merchant `mid`, or None when
    nothing of the group is left to settle."""
    from . import merchant_identity
    keep_ids, raws = [], []
    for r in conn.execute(
            """SELECT j.id, j.raw_merchant, j.from_canonical, a.merchant_id
                 FROM merchant_renames j
                 JOIN merchant_canonical a ON a.raw_merchant = j.raw_merchant
                WHERE j.id = ANY(%s) AND j.rules IS NULL
                ORDER BY j.id FOR UPDATE OF j""", (list(ids),)).fetchall():
        sid = merchant_identity._survivor(conn, r["merchant_id"]) \
            if r["merchant_id"] else None
        if (r["from_canonical"] or r["raw_merchant"]) != old \
                or sid is None or str(sid) != mid:
            continue
        keep_ids.append(r["id"])
        if r["raw_merchant"] not in raws:
            raws.append(r["raw_merchant"])
    if not keep_ids:
        return None
    live = conn.execute("SELECT name FROM merchants WHERE id = %s",
                        (mid,)).fetchone()
    if live is None or live["name"].lower() == old.lower():
        return None
    return live["name"], keep_ids, raws


def rekey_orphaned_rules(conn, *, apply: bool = False) -> list[dict]:
    """Carry the rules of renames made before rename() carried them.

    Walks the journal for renames that recorded no rule note, groups each
    one's strings by the name they left and the merchant they now belong
    to, and settles each group as the rename would have.

    A dry run (apply=False) only reads: it plans every group against what
    the groups before it in the same run would have removed and carried, so
    it reports what applying would do without writing anything — not even
    inside a transaction it means to roll back, where one bad group would
    end the whole report. With apply=True each group is its own
    transaction, its note written to its journal rows alongside the rule
    changes, so undoing such a rename puts the rules back too and a group
    that fails leaves every other one whole. Idempotent, and safe to re-run
    after a partial failure: a group already carried has a note on its
    rows and is not walked again, and one with nothing left to carry plans
    to nothing."""
    from . import merchant_identity
    groups: dict = {}
    for r in conn.execute(
            """SELECT j.id, j.raw_merchant, j.from_canonical, a.merchant_id
                 FROM merchant_renames j
                 JOIN merchant_canonical a ON a.raw_merchant = j.raw_merchant
                WHERE j.rules IS NULL
                ORDER BY j.id""").fetchall():
        old = r["from_canonical"] or r["raw_merchant"]
        mid = merchant_identity._survivor(conn, r["merchant_id"]) \
            if r["merchant_id"] else None
        if mid is None:
            continue
        g = groups.setdefault((old, str(mid)), {"ids": [], "raws": []})
        g["ids"].append(r["id"])
        if r["raw_merchant"] not in g["raws"]:
            g["raws"].append(r["raw_merchant"])
    out = []
    gone: set[str] = set()        # dry run: keys an earlier group cleared
    placed: dict[str, list] = {}  # dry run: rules an earlier group carried
    for (old, mid), g in groups.items():
        live = conn.execute("SELECT name FROM merchants WHERE id = %s",
                            (mid,)).fetchone()
        if live is None or live["name"].lower() == old.lower():
            continue
        into = live["name"]
        if apply:
            with conn.transaction():
                merchant_identity._lock_identity(conn)
                # The groups were read before the lock: an undo or a rename
                # since may have taken a string back, retired the journal
                # row or renamed the survivor. Re-read the group under the
                # lock and settle only what still stands, or a stale plan
                # moves a restored merchant's rule onto another with no
                # note for undo to put it back.
                cur = _regroup_locked(conn, g["ids"], old, mid)
                if cur is None:
                    continue
                into, ids, raws = cur
                note = follow_rules(conn, old, into, raws)
                if note is not None:
                    _journal_note(conn, note, ids, only_unnoted=True)
        else:
            note = _plan_follow(conn, old, into, g["raws"], gone=frozenset(gone),
                                placed=placed.get(into.lower(), ()))
            if note is not None:
                gone.update(d["merchant"] for d in note["displaced"])
                gone.update(d["merchant"] for d in note["rules"]
                            if d["fate"] != "kept")
                carried = placed.setdefault(into.lower(), [])
                carried[:] = [d for d in carried if d["merchant"] not in gone]
                carried.extend({**d, "merchant": into} for d in note["rules"]
                               if d["fate"] == "moved")
                gone.discard(into)
        if note is not None:
            out.append({"from": old, "into": into, "raws": len(g["raws"]),
                        "rules": [{k: d[k] for k in ("merchant", "category_primary",
                                                     "source", "fate")}
                                  for d in note["rules"]],
                        "displaced": [{k: d[k] for k in ("merchant",
                                                         "category_primary", "source")}
                                      for d in note["displaced"]]})
    return out


def undo(conn, change_id: int) -> dict:
    """Replay one journalled change backwards.

    A change that was the FIRST mapping for its raw string is undone by
    deleting the row outright, not by writing an empty one — layer1 owns
    the unmapped case and recomputes it on the next sync."""
    # Same lock, same order as rename() — see there.
    from . import merchant_identity
    merchant_identity._lock_identity(conn)
    row = conn.execute(
        "SELECT raw_merchant, from_canonical, to_canonical, rules "
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
    # The bills the rename re-pointed are locked before any row moves
    # back, in id order, as rename() locks them: an approval holds its bill
    # while it stamps that bill's charges, and an undo that moved a charge
    # first and reached for the bill after would close a cycle with it.
    # These are the bills unfollow_merchant_rename below rewrites; taking
    # them again there costs nothing.
    conn.execute("SELECT 1 FROM bills WHERE raw ? 'merchant_merges' "
                 "ORDER BY id FOR UPDATE")
    from . import merchant_identity
    merchant_identity.reconcile_raws(conn, [row["raw_merchant"]])
    # and the bills the rename re-pointed go back with the string
    from . import bills
    bills.unfollow_merchant_rename(conn, row["raw_merchant"],
                                   row["to_canonical"])
    from . import selfemploy
    selfemploy.unfollow_merchant_rename(conn, row["raw_merchant"],
                                        row["to_canonical"])
    # and the category rules it carried or set aside
    from .compat import as_dict
    unfollow_rules(conn, as_dict(row["rules"]) if row["rules"] else None,
                   row["raw_merchant"])
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
    # A linked shadow (the second feed of a dual-sourced card) or a hidden
    # account carries the same purchases the primary does, or money the
    # user asked to leave out — the Spending screen drops both, so the
    # merchant's count, total, category and places do too. Identity stays
    # unscoped: the name still lists, the variants still show.
    from . import links
    shadows = links.shadow_ids(conn)
    personal = (f"COALESCE(true {budget.PERSONAL_ONLY_SQL}, false) "
                "AND NOT (t.account_id = ANY(%(shadows)s::text[]))")
    head = conn.execute(
        f"""SELECT {DISPLAY_MERCHANT} AS display,
                   count(*) FILTER (WHERE {personal}) AS rows,
                   round(COALESCE(sum(t.amount)
                         FILTER (WHERE {personal}), 0)::numeric, 2) AS total,
                   min(t.date) AS first, max(t.date) AS last,
                   array_agg(DISTINCT {merchant_sql.RAW_KEY}) AS variants,
                   -- the keys a rule reaches these rows by (see the rule
                   -- lookup below): canonical strings and merchant rows
                   array_agg(DISTINCT COALESCE(mc.canonical,
                             {merchant_sql.RAW_KEY})) AS canons,
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
             WHERE t.removed = 0 AND {DISPLAY_MERCHANT} = %(display)s
             GROUP BY 1""", {"display": display, "shadows": shadows}).fetchone()
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
                       AND NOT (t.account_id = ANY(%s::text[]))
                       {budget.PERSONAL_ONLY_SQL}
                     GROUP BY 1, 2, 3) l
             ORDER BY l.n DESC, l.last DESC LIMIT 5""",
        (display, shadows)).fetchall()]
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


# The catalog's sorts and filters. A filter or sort cut on the client from
# a capped page would silently lose every match past the cap, so both run
# here, before the LIMIT.
LISTING_SORTS = ("count", "alpha", "spend", "recent")
LISTING_FILTERS = ("all", "oneoff", "unnamed", "month")
# a name the aggregator never resolved: a run of three digits, a terminal's
# * or #, or an all-caps string of four letters or more that no one would
# write as a name — the same test the merchant screens apply
_UNNAMED_SQL = (f"({DISPLAY_MERCHANT} ~ '[0-9]{{3,}}|[*#]' OR "
                f"({DISPLAY_MERCHANT} ~ '[A-Z]{{4,}}' AND "
                f"{DISPLAY_MERCHANT} = upper({DISPLAY_MERCHANT})))")
_LISTING_ORDER = {
    # ordinals: 1 = display, 2 = the personal row count, 3 = the personal
    # total, 6 = last seen; the name breaks every tie so paging with an
    # offset never repeats or skips a merchant
    "count": "2 DESC, 1 ASC",
    "alpha": "1 ASC",
    "spend": "3 DESC, 1 ASC",
    "recent": "6 DESC NULLS LAST, 1 ASC",
}
_MAX_OFFSET = 10_000_000


def listing(conn, q: str = "", limit: int = 200,
            order: str = "count", *, sort: str | None = None,
            filter: str = "all", offset: int = 0,
            today: dt.date | None = None) -> list[dict]:
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
    walk the tail. `sort` supersedes it when given and also takes "spend"
    (personal total, largest first) and "recent" (last seen, newest
    first). `filter`: "all", "oneoff" (one personal row), "unnamed" (a
    bank string for a name, see _UNNAMED_SQL) or "month" (seen on or after
    the first of the household's current month; `today` overrides the
    household's day). `offset` pages past the cap. The search (`q`), the
    filter and the sort all apply BEFORE the limit, so matches are never
    starved by the cap."""
    key = sort if sort is not None else order
    if key not in LISTING_SORTS:
        raise ValueError("sort must be one of " + ", ".join(LISTING_SORTS))
    if filter not in LISTING_FILTERS:
        raise ValueError("filter must be one of " + ", ".join(LISTING_FILTERS))
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
    # A linked shadow (the second feed of a dual-sourced card) or a hidden
    # account counts as neither personal nor business money: its purchases
    # are the primary's again, or money the user asked to leave out, and the
    # Spending screen drops both. Without this a dual-sourced card's
    # merchants show every visit twice and sort=spend / "oneoff" read the
    # doubled figures. The row still feeds the identity columns (variants,
    # seen-range), so a name only a shadow carries still lists.
    from . import links
    args: list = [links.shadow_ids(conn)]
    conds: list[str] = []
    having = ""
    if q.strip():
        # The search text is literal text, not a pattern: escape LIKE's
        # wildcards so searching "%" finds merchants containing a percent
        # sign instead of returning everything. Backslash is Postgres's
        # default LIKE escape character.
        esc = (q.strip().replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
        conds.append(f"{DISPLAY_MERCHANT} ILIKE %s")
        args.append(f"%{esc}%")
    if filter == "unnamed":
        conds.append(_UNNAMED_SQL)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    # the aggregate filters read the grouped figures, after the WHERE
    if filter == "oneoff":
        having = "HAVING sum(t.n_personal) = 1"
    elif filter == "month":
        if today is None:
            from .. import localtime
            today = localtime.now_local(budget.load_config(conn)).date()
        having = "HAVING max(t.last_seen) >= %s"
        args.append(today.replace(day=1))
    args.append(max(1, min(int(limit), 500)))
    args.append(max(0, min(int(offset), _MAX_OFFSET)))
    # Ordinals, because the sort keys are the aliased aggregates and `rows`
    # is a keyword in Postgres (see _LISTING_ORDER).
    order_sql = _LISTING_ORDER[key]
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
    # DISPLAY_MERCHANT apply verbatim — the one definition, not a copy —
    # which means it has to carry every column the identity key is built
    # from.
    # The personal/business test is decided once per row in the innermost
    # subquery; `OFFSET 0` keeps the planner from inlining that subquery and
    # re-evaluating the predicate for each of the three FILTERs that read it.
    return conn.execute(
        f"""WITH t AS (
                SELECT r.merchant_id, r.merchant_outlet, r.merchant_name,
                       r.name,
                       count(*) FILTER (WHERE r.personal AND r.counted)
                           AS n_personal,
                       sum(r.amount) FILTER (WHERE r.personal AND r.counted)
                           AS amt_personal,
                       count(*) FILTER (WHERE NOT r.personal AND r.counted)
                           AS n_business,
                       min(r.date) AS first_seen, max(r.date) AS last_seen
                  FROM (SELECT t.merchant_id, t.merchant_outlet, t.merchant_name,
                               t.name, t.amount, t.date, {personal} AS personal,
                               NOT (t.account_id = ANY(%s::text[])) AS counted
                          FROM transactions t
                         WHERE t.removed = 0 OFFSET 0) r
                 GROUP BY 1, 2, 3, 4)
            SELECT {DISPLAY_MERCHANT} AS display,
                   sum(t.n_personal)::bigint AS rows,
                   round(COALESCE(sum(t.amt_personal), 0)::numeric, 2) AS total,
                   sum(t.n_business)::bigint AS business_rows,
                   min(t.first_seen) AS first, max(t.last_seen) AS last,
                   (array_agg(DISTINCT {merchant_sql.RAW_KEY}))[1:50] AS variants,
                   count(DISTINCT {merchant_sql.RAW_KEY}) AS variant_count,
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
             GROUP BY 1 {having}
             ORDER BY {order_sql} LIMIT %s OFFSET %s""", args).fetchall()
