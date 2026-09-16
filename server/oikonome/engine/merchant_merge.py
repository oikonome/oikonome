"""Merchant merge proposals: notice when several merchants are one business
and OFFER the merge — never make it.

A local business that Plaid has no entity for reaches the ledger under
every spelling its card terminals and banks produce: a name cut at 13, 15
or 20 characters, one with the city glued on, one without the apostrophe.
The string clean is deliberately exact (a wrong merge fuses two businesses
and everything filed under them), so each spelling is its own merchant
until a person merges them by hand. This module finds the pairs a person
would merge on sight, says why in words, and puts them to the person.

Smart means: evidence a person accepts as proof (each signal is a
sentence the page shows), vetoes that make the known false positives
impossible by construction, a precision measured before it ships
(`rehearse`), and memory (a rejected pair is never offered again).

The proposer is pure: `propose(candidates, cities)` takes plain dicts and
returns pairs with evidence. Two loaders feed it — the live merchants
(`load_live`) and the rehearsal's pseudo-merchants (`load_rehearsal`: the
ledger grouped by the string clean alone, as a fresh household with no
alias would see it, with today's aliases kept aside as the labels).
"""
from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter, defaultdict

from . import merchant_dedup
from .compat import as_date, as_dict, jsonb

# Where card terminals and bank feeds cut a merchant name. A prefix that
# ends exactly here is a truncation even when it ends on a word boundary;
# elsewhere a word-boundary prefix is a sibling ("Amazon Prime" / "Amazon
# Prime Video"), not a cut, and only a MID-word cut counts.
TERMINAL_CUTS = frozenset({13, 15, 16, 18, 20, 22, 25})
MIN_PREFIX = 8

# Leading words that name a processor, a bank mechanic or a category of
# business rather than one business: two merchants sharing one of these
# share nothing. A pair whose common leading word is here is never proposed.
GENERIC_LEAD = frozenset({
    "the", "a", "an", "of", "and", "at", "in", "on", "to", "for", "by",
    "bank", "city", "county", "state", "us", "usa", "united", "national",
    "first", "new", "old", "north", "south", "east", "west", "central",
    "pay", "payment", "payments", "transfer", "deposit", "withdrawal",
    "check", "atm", "cash", "card", "credit", "debit", "loan", "interest",
    "fee", "fees", "service", "services", "store", "market", "shop",
    "cafe", "restaurant", "pizza", "grill", "bar", "coffee", "gas", "fuel",
    "auto", "car", "home", "health", "medical", "dental", "family",
    "google", "amazon", "apple", "paypal", "venmo", "zelle", "square", "sq",
    "tst", "cash", "app", "online", "web", "www", "com", "inc", "llc",
    "mobile", "wireless", "insurance", "capital", "financial", "federal",
    "american", "america", "general", "global", "international", "express",
    # one-word names the string clean leaves behind that are not a business
    "corner", "park", "photo", "mart", "local", "shell", "main", "village",
    "plaza", "center", "centre", "square", "point", "lake", "river", "hill",
    "inst", "xfer", "transfer", "wire", "autopay", "epay", "billpay",
})

FLOW_CATEGORIES = frozenset({"TRANSFER IN", "TRANSFER OUT", "LOAN PAYMENTS",
                             "INCOME", "BANK FEES"})

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")
# what a bank feed glues in front of the merchant ("Withdrawal Pos Kroger",
# "Debit Card Purchase Frontier", "Ins Med Riverton"): not the name
_BANK_PREFIX = re.compile(
    r"^(?:(?:withdrawal|deposit|purchase|debit|credit|pos|card|ins|med|paper|"
    r"payment|to|from|check|ach|electronic|recurring|online|web|dbt|crd|"
    r"visa|mastercard|debit card|pos debit|pos purchase|"
    r"aplpay|applepay|gglpay|googlepay|sq|tst|pp|paypal|cko|sp)\s+)+")
# a trailing word that says what KIND of business, not which one: a name
# with and without it is one merchant (Brindle / Brindle Pharmacy, Quillo /
# Quillo Market) — never a fuel word, which marks an outlet
_KIND_WORDS = frozenset({
    "pharmacy", "grocery", "supermarket", "market", "foods", "food", "store",
    "stores", "shop", "restaurant", "cafe", "coffee", "bakery", "deli",
    "inc", "llc", "co", "corp", "company", "international", "ltd", "group",
    "hospital", "clinic", "center", "centre", "wholesale", "warehouse",
    "supercenter", "online", "com", "usa", "us", "ref", "refund", "grill",
    "headquarters", "hq", "kitchen", "bistro", "pizzeria", "tavern",
    "brewing", "brewery", "winery", "salon", "spa", "dental", "medical",
    "insurance", "bank", "credit", "union", "services", "service", "sub",
    "subs", "parts", "limited", "intl", "commerce", "enterprises", "holdings",
    "svc", "station", "stn"})
# what a bank feed glues on the END of a payee: how it was paid, not who
_BANK_TAIL = frozenset({
    "deposit", "pymnts", "pymnt", "pymt", "payment", "payments", "epayment",
    "epay", "autopymt", "autopay", "echeck", "ach", "ppd", "des", "web",
    "tel", "online", "bill", "billpay", "recurring", "purchase", "pos"})
# a token that is a code, not a word: digits, or letters and digits mixed
_CODE = re.compile(r"^(?=.*\d)[a-z0-9]+$")
# Plaid's generic buckets: a category that says nothing, so a disagreement
# with it is not a disagreement
_GENERIC_CATS = frozenset({"GENERAL MERCHANDISE", "GENERAL SERVICES", "OTHER", ""})


def norm(name: str | None) -> str:
    """The comparable form of a name: lower, apostrophes/periods/hyphens
    gone, "the " dropped, spaces collapsed. "Bramble's Pan" -> "brambles pan"."""
    s = (name or "").lower().replace("'", "").replace("’", "").replace(".", "")
    s = s.replace("-", " ").replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    s = _BANK_PREFIX.sub("", s)
    # a terminal code or reference is not part of the name ("443120 fastpump
    # 00009k22"), nor is how the bill was paid ("lakeshore electric echeck")
    words = [w for w in s.split() if not _CODE.match(w)]
    while len(words) > 1 and words[-1] in _BANK_TAIL:
        words.pop()
    return " ".join(words)


def _tight(s: str) -> str:
    """No spaces at all: "h e b" == "heb" == "h-e-b", "wal mart" == "walmart"."""
    return s.replace(" ", "")


def _strip_s(s: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in s.split())


def _initials(a: str, b: str) -> bool:
    """One name is the initials of the other's words (mta / metropolitan
    transportation authority): two to five letters, at least three words."""
    short, long = (a, b) if len(a) < len(b) else (b, a)
    words = long.split()
    return (" " not in short and 2 <= len(short) <= 5 and len(words) >= 3
            and short == "".join(w[0] for w in words))


def _minus_kind(s: str) -> str:
    """The name without its trailing kind-of-business words."""
    words = s.split()
    while len(words) > 1 and words[-1] in _KIND_WORDS:
        words.pop()
    return " ".join(words)


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def lead_token(name: str) -> str:
    """The leading word, with a trailing s ignored so "walgreens" and
    "walgreen" lead the same family, and with the first two one-letter
    words joined so "h e b" leads as "heb"."""
    words = norm(name).split()
    if not words:
        return ""
    if len(words) >= 2 and len(words[0]) == 1 and len(words[1]) == 1:
        return "".join(w for w in words[:3] if len(w) == 1)
    # a two-letter particle ("de", "la", "el", "st") is not the name's word
    real = [w for w in words if len(w) >= 3] or words
    w = real[0]
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _same_possessive(a: str, b: str) -> bool:
    """Equal once a trailing s / 's is ignored on every word (rivertons
    grocer / riverton grocer)."""
    return _strip_s(a) == _strip_s(b)


def _truncation(short: str, long: str) -> str | None:
    """Is `short` (normalised raw) a terminal cut of `long`? Returns the
    reason or None."""
    if len(short) < MIN_PREFIX or len(short) >= len(long):
        return None
    if not long.startswith(short):
        return None
    mid_word = long[len(short)] != " "
    if mid_word:
        # a lone word that happens to begin a longer run-together string is
        # not a cut ("riverton" / "rivertondmv"): a mid-word cut needs a
        # multi-word name, or a long one
        if " " not in short and len(short) < 10:
            return None
        return f"cut mid-word at {len(short)} characters"
    if len(short) in TERMINAL_CUTS:
        return f"cut at {len(short)} characters, a terminal's limit"
    # a three-word name cut at a word boundary is a cut, not a sibling
    # ("riverton family health" / "riverton family health care"); a
    # two-word one is a sibling ("amazon prime" / "amazon prime video")
    if len(short.split()) >= 3 and len(short) >= 14:
        return f"cut at {len(short)} characters"
    return None


def _edit_distance_le(a: str, b: str, k: int) -> bool:
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > k:
            return False
        prev = cur
    return prev[-1] <= k


def _location_tail(short: str, long: str, cities: list[str]) -> str | None:
    """`long` is `short` plus a place: a configured city (optionally with a
    two-letter state), possibly glued or mangled ("Fort Waynein")."""
    if not long.startswith(short + " "):
        return None
    tail = _letters(long[len(short):])
    if not tail:
        return None
    for c in cities:
        cl = _letters(c)
        if not cl:
            continue
        for cand in (tail, tail[:-2] if len(tail) > len(cl) else tail):
            if cand == cl or (len(cl) >= 8 and cand[:4] == cl[:4]
                              and _edit_distance_le(cand, cl, 1)):
                return f"the same name with {c} on the end"
            # the city itself cut short by the terminal ("charlottesvil")
            if len(cand) >= 6 and cl.startswith(cand) and len(cand) < len(cl):
                return f"the same name with {c} on the end, cut short"
    return None


def _minus_city(s: str, cities: list[str]) -> str:
    """The name without a known city leading or trailing it ("riverton
    ymca", "bluebird cafe riverton")."""
    for c in cities:
        cn = norm(c)
        if not cn:
            continue
        if s.startswith(cn + " ") and len(s) > len(cn) + 3:
            return s[len(cn) + 1:]
        if s.endswith(" " + cn) and len(s) > len(cn) + 3:
            return s[:-len(cn) - 1]
    return s


def _one_typo(a: str, b: str) -> bool:
    """Long names one letter apart past the first word ("lakeview
    confectionery" / "lakeview confectionary")."""
    if len(a) < 12 or len(b) < 12 or a.split()[0] != b.split()[0]:
        return False
    return a != b and _edit_distance_le(a, b, 1)


def _desc_family(a: dict, b: dict, cities: list[str] | None = None) -> str | None:
    """Rows of both share a bank-line stem (the descriptor up to its first
    digit or '#'), or the same registered line of business in one city —
    the latter only when the names still share their leading word once
    the city is taken off them. Two charities in one town are two
    businesses; the MCC says what they are, not who they are."""
    if a["stems"] & b["stems"]:
        stem = sorted(a["stems"] & b["stems"], key=len)[-1]
        if len(stem) >= MIN_PREFIX:
            return f'both carry the bank line "{stem}…"'
    if a["mcc"] and a["mcc"] == b["mcc"] and a["city"] and a["city"] == b["city"]:
        la = lead_token(_minus_city(norm(a["name"]), cities or []))
        lb = lead_token(_minus_city(norm(b["name"]), cities or []))
        if la and la == lb and la not in GENERIC_LEAD:
            return f"same registered line of business (MCC {a['mcc']}) in {a['city']}"
    return None


def _code_bearing(name: str) -> bool:
    """A one-word name that carried a code the normaliser dropped: "Tamsin:17"
    compares as "tamsin", "9-Lantern" as "lantern". Such a name must not be
    read as the bare word plus a kind of business — "Tamsin Kitchen" is not
    Tamsin:17 — though it still matches a spelling of ITSELF."""
    toks = _PUNCT.sub(" ", (name or "").lower().replace("-", " ")).split()
    return any(_CODE.match(t) for t in toks) and len(norm(name).split()) <= 1


def signals(a: dict, b: dict, cities: list[str]) -> list[str]:
    """Primary evidence that a and b are one business, as sentences."""
    out: list[str] = []
    na, nb = norm(a["name"]), norm(b["name"])
    if na and na == nb:
        out.append("the same name spelled differently")
    elif na and nb and _same_possessive(na, nb):
        out.append("the same name but for an apostrophe or an s")
    elif na and nb and _tight(na) == _tight(nb):
        out.append("the same name but for spacing or hyphens")
    elif (na and nb and len(_minus_kind(na)) >= 3
          and not _code_bearing(a["name"]) and not _code_bearing(b["name"])
          and _strip_s(_minus_kind(na)) == _strip_s(_minus_kind(nb))):
        longer = na if len(na) > len(nb) else nb
        out.append(f'"{longer}" is the same name with what kind of business it is')
    elif na and nb and _minus_city(na, cities) == _minus_city(nb, cities) \
            and len(_minus_city(na, cities)) >= 4:
        which = na if na != _minus_city(na, cities) else nb
        out.append(f'"{which}" is the same name with the city on it')
    elif na and nb and _one_typo(na, nb) and a["category"] == b["category"]:
        out.append("the same name but for one letter")
    elif (na and nb and len(_minus_kind(_minus_city(na, cities))) >= 4
          and not _code_bearing(a["name"]) and not _code_bearing(b["name"])
          and _strip_s(_minus_kind(_minus_city(na, cities)))
          == _strip_s(_minus_kind(_minus_city(nb, cities)))):
        out.append("the same name once the city and the kind of business are set aside")
    elif (na and nb and _initials(na, nb)
          # initials alone name too many things (three letters fit many
          # three-word names): only with the same category, similar
          # charges, and enough rows on both sides to mean it
          and len(supports(a, b)) == 2 and a["rows"] >= 3 and b["rows"] >= 3
          and min(na, nb, key=len) not in GENERIC_LEAD):
        out.append(f'"{min(na, nb, key=len)}" is the initials of "{max(na, nb, key=len)}"')
    raws_a = [norm(r) for r in a["raws"]] + [na]
    raws_b = [norm(r) for r in b["raws"]] + [nb]
    seen: set[str] = set()
    for ra in raws_a:
        for rb in raws_b:
            if not ra or not rb or ra == rb:
                continue
            short, long = (ra, rb) if len(ra) < len(rb) else (rb, ra)
            why = _location_tail(short, long, cities)
            if why and "tail" not in seen:
                out.append(f'"{long}" is {why}')
                seen.add("tail")
                continue
            why = _truncation(short, long)
            if why and "truncation" not in seen:
                out.append(f'"{short}" is "{long}" {why}')
                seen.add("truncation")
    fam = _desc_family(a, b, cities)
    if fam:
        out.append(fam)
    return out


def _same_name(na: str, nb: str) -> bool:
    """The two names are one name spelled differently — exact once
    apostrophes, spacing, a trailing s and bank noise are ignored. Such a
    pair is safe whatever else is true of the family or its categories."""
    return bool(na) and (na == nb or _tight(na) == _tight(nb) or _same_possessive(na, nb))


def vetoes(a: dict, b: dict, lead_counts: Counter,
           cities: list[str] | None = None) -> list[str]:
    """Any one kills the pair; the reasons are logged, never shown as an
    offer."""
    out: list[str] = []
    cities = cities or []
    if a["entity"] and b["entity"] and a["entity"] != b["entity"]:
        out.append("two different Plaid entities")
    if a["parent"] or b["parent"]:
        out.append("an outlet is its own merchant")
    la, lb = lead_token(a["name"]), lead_token(b["name"])
    ta, tb = _tight(norm(a["name"])), _tight(norm(b["name"]))
    na, nb = norm(a["name"]), norm(b["name"])
    same = _same_name(na, nb)
    if (la != lb and not (ta[:7] == tb[:7] and len(ta) >= 7) and not _initials(na, nb)
            and _minus_city(na, cities) != _minus_city(nb, cities)):
        out.append("different leading word")
    elif la in GENERIC_LEAD or len(la) < 3:
        out.append(f'"{la}" names a kind of business, not one business')
    elif lead_counts.get(la, 0) > 8 and not same:
        out.append(f'"{la}" leads {lead_counts[la]} merchants here')
    # a spend merchant and a transfer merchant are two things — unless they
    # are one name, in which case the rows keep their own categories and
    # only the label is shared ("Withdrawal Pos Kroger" is Kroger)
    if (a["category"] in FLOW_CATEGORIES or b["category"] in FLOW_CATEGORIES) and not same:
        out.append("a transfer, payment or income merchant")
    if (a["category"] not in _GENERIC_CATS and b["category"] not in _GENERIC_CATS
            and a["category"] != b["category"] and a["rows"] >= 5 and b["rows"] >= 5
            and not same):
        out.append(f"categories disagree ({a['category']} vs {b['category']})")
    # a payout merchant and a spend merchant are two things even when the
    # bank line shares a company name ("eBay Payouts" is not eBay)
    if (a.get("inflow", 0) > a["rows"]) != (b.get("inflow", 0) > b["rows"]):
        out.append("money in on one side, money out on the other")
    if a["manual"] or b["manual"]:
        out.append("a person already named one of them")
    if a["p2p"] or b["p2p"]:
        out.append("a person-to-person counterparty")
    return out


def _mmyy(d) -> str:
    return f"{d.month:02d}/{d.year % 100:02d}"


def supports(a: dict, b: dict) -> list[str]:
    out = []
    if a["category"] and a["category"] == b["category"]:
        out.append(f"both categorised {a['category']}")
    # two spellings that never overlap in time are one terminal re-labelled:
    # the old descriptor stops the month the new one starts. Spellings
    # that interleave say nothing either way — a cut varies per terminal
    # and two outlets of one chain interleave too — so only the clean
    # hand-over is cited
    fa, la, fb, lb = a.get("first"), a.get("last"), b.get("first"), b.get("last")
    if fa and la and fb and lb and (la < fb or lb < fa):
        old, new = (a, b) if la < fb else (b, a)
        out.append(f'"{old["name"]}" stops {_mmyy(old["last"])}, '
                   f'"{new["name"]}" starts {_mmyy(new["first"])}')
    if a["median"] and b["median"]:
        lo, hi = sorted((a["median"], b["median"]))
        if hi <= 2 * lo:
            out.append(f"similar charges (typically ${lo:,.0f}–${hi:,.0f})")
    return out


def _minus_place(s: str, cities: list[str]) -> str:
    """The name without a trailing place — a known city, possibly cut short
    by the terminal ("fuelco svc station oakmer")."""
    words = s.split()
    for k in (2, 1):
        if len(words) <= k:
            continue
        tail = _letters(" ".join(words[-k:]))
        for c in cities:
            cl = _letters(c)
            if not cl or len(tail) < 5:
                continue
            if tail == cl or (len(cl) >= 8 and cl.startswith(tail)) or (
                    len(cl) >= 8 and tail[:4] == cl[:4] and _edit_distance_le(tail, cl, 1)):
                return " ".join(words[:-k])
    return s


def _display_prefix(name: str, brand: str) -> str:
    """The brand as the person's own spelling wrote it: the leading words
    of `name` whose comparable form is `brand`."""
    words = name.split()
    for i in range(1, len(words) + 1):
        if norm(" ".join(words[:i])) == brand:
            return " ".join(words[:i])
    return brand.title()


def chain_brand(members: list[dict], cities: list[str]) -> str | None:
    """A family whose spellings are all one brand plus a place or a kind
    of business — four fuel stations in four towns — is offered the BRAND
    as its name, not one station's. Only when every member reduces to the
    same brand and none already wears it; the brand must be a real name,
    not a kind word or a generic lead."""
    if len(members) < 2:
        return None
    placed = [_minus_place(norm(m["name"]), cities) for m in members]
    # a family that differs only by kind words ("Oskar's Bar & Grill",
    # "Oskar Bar Restaurant") is one place, and its fullest spelling is the
    # name — the brand rule is for outlets, which is to say places
    if all(pl == norm(m["name"]) for pl, m in zip(placed, members)):
        return None
    bases = {_minus_kind(pl) for pl in placed}
    if len(bases) != 1:
        return None
    brand = bases.pop()
    if (not brand or any(norm(m["name"]) == brand for m in members)
            or all(w in _KIND_WORDS for w in brand.split())
            or lead_token(brand) in GENERIC_LEAD or len(lead_token(brand)) < 3):
        return None
    best = max(members, key=lambda m: m["rows"])
    return _display_prefix(best["name"], brand)


def _rank(m: dict, top: int = 0) -> tuple:
    """Who survives a merge: Plaid-backed, else a name the bank did not
    prefix, else a name that is more than a kind of business ("Bakery"
    alone names nothing), else one carrying a real share of the family's
    rows (`top` = the family's largest count: a one-row spelling never
    outranks the name hundreds of rows already wear, however much longer it is —
    "Taco Stand Qrt Springfie" is a junk tail, not the fuller name), else the
    fuller name, else more rows."""
    n = norm(m["name"])
    prefixed = bool(_BANK_PREFIX.match(m["name"].lower()))
    bare = all(w in _KIND_WORDS for w in n.split()) or lead_token(n) in GENERIC_LEAD
    return (m["entity"] is not None, not prefixed, not bare,
            m["rows"] * 3 > top, len(n), m["rows"])


def survivor(a: dict, b: dict) -> tuple[dict, dict]:
    """(into, from)."""
    top = max(a["rows"], b["rows"])
    return (a, b) if _rank(a, top) >= _rank(b, top) else (b, a)


# A block bigger than this is a word too common to say anything; propose
# draws no edge inside it, and neither does the neighbourhood walk.
BLOCK_MAX = 40


def _block_keys(name: str) -> list[str]:
    """The blocks a name belongs to. Two merchants are only ever compared
    when they share one, so this also answers "which merchants could this
    one possibly pair with" — `_neighbourhood` asks that of the merchants
    table alone, without aggregating the ledger."""
    out: list[str] = []
    lt = lead_token(name)
    if lt:
        out.append(lt)
    # and by the first letters of the spaceless name, so a run-together
    # spelling meets its spaced twin ("bluebirdcafe" / "bluebird cafe")
    tight = _tight(norm(name))
    if len(tight) >= 7:
        out.append("~" + tight[:7])
    # and by initials, so "mta" meets "metropolitan transportation authority"
    words = norm(name).split()
    if len(words) >= 3:
        out.append("^" + "".join(w[0] for w in words))
    elif len(words) == 1 and 2 <= len(words[0]) <= 5:
        out.append("^" + words[0])
    return out


def propose(cands: list[dict], cities: list[str],
            lead_counts: Counter | None = None) -> list[dict]:
    """Pairs a person would merge on sight, with their evidence. Candidates
    are blocked by leading word so the work is per family, not quadratic.

    `lead_counts` — how many different names each word leads — is a fact
    about the WHOLE catalog, not about the candidates handed in. A bounded
    run (`run(only_ids=…)`) loads one family and passes the catalog-wide
    counts, so its answers are the ones the full run would give; left out,
    it is counted over the candidates, which is the same thing when they
    are the whole catalog."""
    # how many DIFFERENT names a word leads: ten code-prefixed spellings of
    # one shop are one name, not ten merchants
    if lead_counts is None:
        lead_counts = Counter(lead_token(n) for n in {norm(c["name"]) for c in cands})
    blocks: dict[str, list[dict]] = defaultdict(list)
    for c in cands:
        for k in _block_keys(c["name"]):
            blocks[k].append(c)
    edges: dict[tuple, tuple] = {}     # (id, id) -> (signals, supports)
    seen_pairs: set[tuple] = set()
    for lt, group in blocks.items():
        if len(group) < 2 or len(group) > BLOCK_MAX:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                v = vetoes(a, b, lead_counts, cities)
                if v:
                    continue
                s = signals(a, b, cities)
                if not s:
                    continue
                # the shorter name, or what is left of a name once its
                # kind-of-business words are gone ("vine kitchen" → "vine"),
                # is one word: such a word must lead a small family to count
                one_word = min(len(_minus_kind(norm(a["name"])).split()),
                               len(_minus_kind(norm(b["name"])).split())) == 1
                # ...unless the two are one brand plus a place or a kind of
                # business each: outlets of a chain lead a big family by nature
                same_base = (_minus_kind(_minus_place(norm(a["name"]), cities))
                             == _minus_kind(_minus_place(norm(b["name"]), cities)))
                if one_word and lead_counts.get(lt, 0) > 3 and not same_base and not any(
                        x.startswith("the same name spelled") or "initials" in x
                        or "apostrophe" in x or "spacing" in x for x in s):
                    continue
                edges[key] = (s, supports(a, b))
    # A family is a connected set of pairs; ONE offer per member, onto the
    # family's survivor, not one per pair — three spellings of one shop are
    # two offers ("join Bramble's Pantry"), never three that also
    # offer the short spellings to each other.
    by_id = {c["id"]: c for c in cands}
    parent: dict[str, str] = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for a_id, b_id in edges:
        ra, rb = find(a_id), find(b_id)
        if ra != rb:
            parent[ra] = rb
    fams: dict[str, list] = defaultdict(list)
    for key in edges:
        for x in key:
            if x not in fams[find(x)]:
                fams[find(x)].append(x)
    out = []
    for members in fams.values():
        top = max(by_id[m]["rows"] for m in members)
        into = max((by_id[m] for m in members), key=lambda c: _rank(c, top))
        brand = chain_brand([by_id[m] for m in members], cities)
        for m in members:
            if m == into["id"]:
                continue
            frm = by_id[m]
            key = (min(m, into["id"]), max(m, into["id"]))
            if key in edges:
                s, sup = edges[key]
            else:
                # no direct evidence against the survivor: cite the link
                # that puts it in the family
                mid = next(k for k in edges if m in k)
                other = by_id[mid[0] if mid[1] == m else mid[1]]
                s = edges[mid][0] + [f'and "{other["name"]}" is one merchant with "{into["name"]}"']
                sup = edges[mid][1]
            if brand:
                s = s + [f'all are outlets of "{brand}"']
            out.append({"from": frm, "into": into, "signals": s, "supports": sup,
                        "rows": frm["rows"] + into["rows"],
                        **({"rename_to": brand} if brand else {})})
    out.sort(key=lambda p: -p["rows"])
    return out


# ---- loaders ---------------------------------------------------------------

_ROWS_SQL = """
    SELECT {key} AS key, t.merchant_id::text AS merchant_id,
           COALESCE(t.merchant_outlet, t.merchant_name, t.name) AS raw_key,
           COALESCE(t.raw->>'original_description', t.name) AS descriptor,
           t.raw->>'merchant_entity_id' AS entity,
           REPLACE(COALESCE(t.category_override, t.category_primary, ''), '_', ' ') AS category,
           t.amount, t.mcc, t.location_city AS city, t.date
      FROM transactions t
     WHERE t.removed = 0 AND t.amount > 0
       AND COALESCE(t.merchant_outlet, t.merchant_name, t.name) IS NOT NULL
"""


def _stem(desc: str | None) -> str:
    d = norm(re.split(r"[\d#*]", (desc or ""), 1)[0])
    return d


def _build(groups: dict, names: dict, meta: dict) -> list[dict]:
    """Candidate dicts from rows grouped by key."""
    out = []
    for key, rows in groups.items():
        cats = Counter(r["category"] for r in rows if r["category"])
        cat, n = (cats.most_common(1)[0] if cats else ("", 0))
        amounts = sorted(float(r["amount"]) for r in rows if r["amount"])
        m = meta.get(key, {})
        out.append({
            "id": key, "name": names[key], "rows": len(rows),
            "raws": sorted({r["raw_key"] for r in rows if r["raw_key"]})[:12],
            "samples": sorted({(r["descriptor"] or "")[:60] for r in rows})[:3],
            "stems": {s for s in (_stem(r["descriptor"]) for r in rows) if len(s) >= MIN_PREFIX},
            "entity": m.get("entity") or next((r["entity"] for r in rows if r["entity"]), None),
            "parent": m.get("parent"),
            "manual": m.get("manual", False),
            "inflow": m.get("inflow", 0),
            "category": cat if n * 2 >= len(rows) else "",
            "median": statistics.median(amounts) if amounts else None,
            "mcc": Counter(r["mcc"] for r in rows if r["mcc"]).most_common(1)[0][0]
                   if any(r["mcc"] for r in rows) else None,
            "city": Counter(r["city"] for r in rows if r["city"]).most_common(1)[0][0]
                    if any(r["city"] for r in rows) else None,
            "p2p": " — " in names[key],
            "first": min((as_date(r["date"]) for r in rows if r.get("date")), default=None),
            "last": max((as_date(r["date"]) for r in rows if r.get("date")), default=None),
            "labels": Counter(r["merchant_id"] for r in rows if r["merchant_id"]),
        })
    return out


def load_live(conn, ids: list[str] | None = None) -> list[dict]:
    """The live merchants as candidates (rows grouped by merchant_id).

    `ids` bounds every scan to those merchants. The full pass reads the
    whole ledger by design; the ingest pass must not, so it hands in one
    family's worth of ids (`_neighbourhood`)."""
    only = " AND t.merchant_id = ANY(%s::uuid[])" if ids is not None else ""
    args = (list(ids),) if ids is not None else ()
    rows = conn.execute(
        _ROWS_SQL.format(key="t.merchant_id::text") + only, args).fetchall()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["key"]:
            groups[r["key"]].append(r)
    names, meta = {}, {}
    for m in conn.execute(
            "SELECT id::text AS id, name, name_source, plaid_entity_id, parent_id "
            "FROM merchants WHERE merged_into IS NULL"
            + (" AND id = ANY(%s::uuid[])" if ids is not None else ""),
            args).fetchall():
        names[m["id"]] = m["name"]
        meta[m["id"]] = {"entity": m["plaid_entity_id"],
                         "parent": m["parent_id"] is not None,
                         "manual": m["name_source"] == "manual"}
    # money-in rows, so a payout counterparty is known for what it is
    for r in conn.execute(
            "SELECT merchant_id::text AS id, count(*) AS n FROM transactions "
            "WHERE removed = 0 AND amount < 0 AND merchant_id IS NOT NULL"
            + (" AND merchant_id = ANY(%s::uuid[])" if ids is not None else "")
            + " GROUP BY 1", args).fetchall():
        if r["id"] in meta:
            meta[r["id"]]["inflow"] = r["n"]
    groups = {k: v for k, v in groups.items() if k in names}
    return _build(groups, names, meta)


def _live_names(conn) -> list[dict]:
    """id + name of every live merchant the proposer would consider — one
    with at least one row it counts. Names only: the ledger is asked
    whether a merchant has such a row, never for the rows themselves."""
    return conn.execute(
        """SELECT m.id::text AS id, m.name
             FROM merchants m
            WHERE m.merged_into IS NULL
              AND EXISTS (SELECT 1 FROM transactions t
                           WHERE t.merchant_id = m.id AND t.removed = 0
                             AND t.amount > 0
                             AND COALESCE(t.merchant_outlet, t.merchant_name,
                                          t.name) IS NOT NULL)""").fetchall()


def _neighbourhood(names: list[dict], ids: set[str]) -> list[str]:
    """Every merchant that could land in a FAMILY with one of `ids`.

    propose draws edges only inside a block, so a family is a connected
    component of the block graph — which names alone decide. Loading the
    whole component, not just the direct neighbours, is what makes a
    bounded run answer exactly as the full run does: the survivor, the
    chain brand and the cited evidence are all family-wide facts."""
    blocks: dict[str, list[str]] = defaultdict(list)
    for r in names:
        for k in _block_keys(r["name"]):
            blocks[k].append(r["id"])
    parent: dict[str, str] = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for group in blocks.values():
        if len(group) < 2 or len(group) > BLOCK_MAX:
            continue
        for other in group[1:]:
            ra, rb = find(group[0]), find(other)
            if ra != rb:
                parent[ra] = rb
    roots = {find(i) for i in ids}
    return sorted({r["id"] for r in names if find(r["id"]) in roots} | set(ids))


def cities_known(conn) -> list[str]:
    """Places a location tail may name: the configured strip list plus every
    city the aggregator has put on a row (a fact, not a guess)."""
    out = set(merchant_dedup.cities_for(conn))
    for r in conn.execute(
            "SELECT location_city AS c, count(*) AS n FROM transactions "
            "WHERE removed = 0 AND location_city IS NOT NULL "
            "GROUP BY 1 HAVING count(*) >= 2").fetchall():
        out.add(r["c"])
    return sorted(out)


def load_rehearsal(conn) -> list[dict]:
    """The ledger as a household with NO alias would see it: rows grouped by
    the string clean of their raw key. Each candidate keeps `labels` — the
    live merchant ids its rows carry today — as the truth to score against."""
    cities = list(merchant_dedup.cities_for(conn))
    rows = conn.execute(_ROWS_SQL.format(
        key="COALESCE(t.merchant_outlet, t.merchant_name, t.name)")).fetchall()
    groups: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    for r in rows:
        canon = merchant_dedup.canonical_merchant(r["key"], cities) or r["key"]
        groups[canon].append(r)
        names[canon] = canon
    return _build(groups, names, {})


def rehearse(conn) -> dict:
    """Run the proposer over the alias-free ledger and score it against the
    aliases the household actually has (LLM map + a person's renames)."""
    cities = cities_known(conn)
    cands = load_rehearsal(conn)
    props = propose(cands, cities)

    def truth(c):
        return c["labels"].most_common(1)[0][0] if c["labels"] else None

    tp, fp = [], []
    for p in props:
        ta, tb = truth(p["from"]), truth(p["into"])
        (tp if ta and ta == tb else fp).append(p)
    # recall over families: live merchants the clean split into several
    # candidates; how many of the extra pieces did a proposal reconnect?
    fam: dict[str, list] = defaultdict(list)
    for c in cands:
        t = truth(c)
        if t:
            fam[t].append(c)
    families = {t: cs for t, cs in fam.items() if len(cs) > 1}
    joined = set()
    for p in tp:
        joined.add(p["from"]["id"]); joined.add(p["into"]["id"])
    pieces = sum(len(cs) - 1 for cs in families.values())
    found = sum(sum(1 for c in cs if c["id"] in joined) - 1 for cs in families.values()
                if any(c["id"] in joined for c in cs))
    found = max(found, 0)
    return {"candidates": len(cands), "proposed": len(props), "true": len(tp),
            "false": len(fp),
            "precision": round(len(tp) / len(props), 3) if props else None,
            "families": len(families), "pieces": pieces,
            "recall": round(found / pieces, 3) if pieces else None,
            "false_pairs": [(p["from"]["name"], p["into"]["name"], p["signals"]) for p in fp],
            "true_pairs": [(p["from"]["name"], p["into"]["name"]) for p in tp],
            "missed": [[c["name"] for c in cs] for t, cs in families.items()
                       if not any(c["id"] in joined for c in cs)][:40]}


# ---- storage ---------------------------------------------------------------

def _sig(a: str, b: str) -> str:
    return "mm:" + hashlib.sha1("|".join(sorted((a, b))).encode()).hexdigest()[:16]


def run(conn, *, limit: int = 25, only_ids: list[str] | None = None) -> dict:
    """Write new proposals for the live merchants (bounded per run). A pair
    already decided keeps its decision (deterministic id, DO NOTHING).

    `only_ids` — the ingest path, right after a sync minted merchants — is
    a real fast path, not a filter applied to a full scan: only those
    merchants' FAMILIES are loaded (`_neighbourhood` reads names, not
    rows), so an hourly sync or a CSV upload does not pay for a
    whole-ledger dedup pass. The offers it writes are the ones the nightly full run
    would write for those merchants.

    The nightly full run also SWEEPS: a pending offer needs two live
    merchants to be an offer at all."""
    cities = cities_known(conn)
    stats = {"pairs": 0, "inserted": 0}
    if only_ids is not None:
        want = set(only_ids)
        names = _live_names(conn)
        # counted over every merchant, because that is what the count
        # means — see propose()
        lead_counts = Counter(lead_token(n) for n in {norm(r["name"]) for r in names})
        near = _neighbourhood(names, want)
        cands = load_live(conn, ids=near) if near else []
        props = propose(cands, cities, lead_counts)
        props = [p for p in props if p["from"]["id"] in want or p["into"]["id"] in want]
    else:
        # A merge made on the Merchants page, a twin fold, or an approve
        # that absorbed another offer's `from` leaves offers standing whose
        # merchants are no longer two merchants. They are unreachable —
        # pending() hides them behind the live join — so without this they
        # sit pending forever and the alert counts a queue that is empty.
        # 'stale', not deleted: the pair's id is deterministic, so the row
        # is also the record that this pair was already dealt with.
        stats["stale"] = conn.execute(
            """UPDATE merchant_merge_proposals p
                  SET status = 'stale', decided_at = now()
                WHERE p.status = 'pending'
                  AND NOT EXISTS (
                      SELECT 1 FROM merchants f, merchants i
                       WHERE f.id = p.from_merchant_id AND f.merged_into IS NULL
                         AND i.id = p.into_merchant_id AND i.merged_into IS NULL)
            """).rowcount
        cands = load_live(conn)
        props = propose(cands, cities)
    stats["pairs"] = len(props)
    for p in props[:limit]:
        cur = conn.execute(
            """INSERT INTO merchant_merge_proposals
                   (id, from_merchant_id, into_merchant_id, evidence, status)
               VALUES (%s, %s, %s, %s, 'pending')
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (_sig(p["from"]["id"], p["into"]["id"]), p["from"]["id"], p["into"]["id"],
             jsonb({"signals": p["signals"], "supports": p["supports"],
                    **({"rename_to": p["rename_to"]} if p.get("rename_to") else {}),
                    "from": {"name": p["from"]["name"], "rows": p["from"]["rows"],
                             "samples": p["from"]["samples"]},
                    "into": {"name": p["into"]["name"], "rows": p["into"]["rows"],
                             "samples": p["into"]["samples"]}})))
        stats["inserted"] += cur.rowcount
    return stats


def pending(conn) -> list[dict]:
    """Pending proposals whose merchants are both still live, with names."""
    out = []
    for r in conn.execute(
            """SELECT p.id, p.evidence, p.created_at,
                      f.name AS from_name, f.logo_url AS from_logo, f.id::text AS from_id,
                      i.name AS into_name, i.logo_url AS into_logo, i.id::text AS into_id
                 FROM merchant_merge_proposals p
                 JOIN merchants f ON f.id = p.from_merchant_id AND f.merged_into IS NULL
                 JOIN merchants i ON i.id = p.into_merchant_id AND i.merged_into IS NULL
                WHERE p.status = 'pending'
                ORDER BY p.created_at, p.id""").fetchall():
        ev = as_dict(r["evidence"])
        # a chain offer names the brand, which no member wears yet
        out.append({"id": r["id"], "from": r["from_name"],
                    "into": ev.get("rename_to") or r["into_name"],
                    "from_id": r["from_id"], "into_id": r["into_id"],
                    "from_logo": r["from_logo"], "into_logo": r["into_logo"],
                    "from_rows": (ev.get("from") or {}).get("rows"),
                    "into_rows": (ev.get("into") or {}).get("rows"),
                    "from_samples": (ev.get("from") or {}).get("samples") or [],
                    "into_samples": (ev.get("into") or {}).get("samples") or [],
                    "signals": ev.get("signals") or [],
                    "supports": ev.get("supports") or []})
    return out


def notice(conn) -> dict | None:
    """What the alert strip says while offers wait, or None when nothing
    does. The figures are those of the LATEST batch (its day and how many
    pairs it found, decided or not), never the live pending count: an
    alert's identity is its words, so a count that shrank with every
    decision would come back un-dismissed each time the person worked
    through the queue. New offers are news; the queue going down is not.

    The pending count is the one pending() would SHOW — both merchants
    still live. A bare status count says "3 pairs look like one business"
    over a queue the person opens to find empty, every night, because a
    merge made on the Merchants page took one of the two away."""
    r = conn.execute(
        """SELECT (SELECT COUNT(*) FROM merchant_merge_proposals p
                     JOIN merchants f ON f.id = p.from_merchant_id
                                     AND f.merged_into IS NULL
                     JOIN merchants i ON i.id = p.into_merchant_id
                                     AND i.merged_into IS NULL
                    WHERE p.status = 'pending') AS pending,
                  MAX(created_at)::date AS found
             FROM merchant_merge_proposals""").fetchone()
    if not r or not r["pending"]:
        return None
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM merchant_merge_proposals "
        "WHERE created_at::date = %s", (r["found"],)).fetchone()["n"]
    return {"pending": r["pending"], "batch": n, "found": r["found"].isoformat()}


def _others_named(conn, name: str | None, pair: tuple[str, str]) -> list[str]:
    """Live merchants wearing `name` that are not the pair being decided.
    Matched case-insensitively, because merchant_dedup.rename resolves an
    existing casing before it merges."""
    if not name:
        return []
    return [r["name"] for r in conn.execute(
        "SELECT name FROM merchants WHERE lower(name) = lower(%s) "
        "AND merged_into IS NULL AND id::text <> ALL(%s) ORDER BY name",
        (name, list(pair))).fetchall()]


def decide(conn, pid: str, action: str) -> dict:
    """approve = merge through the journalled rename door (undo on the
    Merchants page works unchanged); reject = never offered again."""
    with conn.transaction():
        # the household identity lock FIRST — the clash check below is a
        # read, and rename() takes this same lock later; taken here it
        # cannot see a merchant that lands between the check and the act
        from . import merchant_identity
        merchant_identity._lock_identity(conn)
        p = conn.execute(
            "SELECT * FROM merchant_merge_proposals WHERE id=%s FOR UPDATE",
            (pid,)).fetchone()
        if not p or p["status"] != "pending":
            return {"error": "unknown or already-decided proposal"}
        if action == "reject":
            conn.execute("UPDATE merchant_merge_proposals SET status='rejected', "
                         "decided_at=now() WHERE id=%s", (pid,))
            return {"rejected": pid}
        if action != "approve":
            return {"error": f"unknown action {action!r}"}
        names = {r["id"]: r["name"] for r in conn.execute(
            "SELECT id::text AS id, name FROM merchants WHERE id IN (%s, %s) "
            "AND merged_into IS NULL",
            (p["from_merchant_id"], p["into_merchant_id"])).fetchall()}
        frm = names.get(str(p["from_merchant_id"]))
        into = names.get(str(p["into_merchant_id"]))
        if not frm or not into:
            conn.execute("UPDATE merchant_merge_proposals SET status='stale', "
                         "decided_at=now() WHERE id=%s", (pid,))
            return {"error": "one of the merchants is gone"}
        # merchant_dedup.rename works by DISPLAY NAME, and a rename onto a
        # name that already exists is a MERGE. So both names this approve
        # is about have to name one merchant each, or approving performs a
        # merge the person was never shown: a third live merchant already
        # wearing the chain brand would absorb the survivor, and a twin of
        # `frm` would be dragged along with it. Refuse and name the other
        # merchant — merging into it is a thing the person can choose on
        # the Merchants page, with both names in front of them. (The offer
        # stays pending; rejecting it is still one click.)
        brand = as_dict(p["evidence"]).get("rename_to")
        pair = (str(p["from_merchant_id"]), str(p["into_merchant_id"]))
        clash = _others_named(conn, frm, pair) or (
            _others_named(conn, brand, pair) if brand and brand != into else [])
        if clash:
            return {"error": f'"{clash[0]}" is already another merchant here — '
                             f"merge into it from the Merchants page if that "
                             f"is what you mean"}
        if brand and brand != into:
            # the survivor takes the brand first, so the merge lands on it
            merchant_dedup.rename(conn, into, brand)
            into = brand
        moved = merchant_dedup.rename(conn, frm, into)
        conn.execute("UPDATE merchant_merge_proposals SET status='approved', "
                     "decided_at=now() WHERE id=%s", (pid,))
        # the survivor's row may have been folded into another (a rename
        # onto an existing name is a merge): the family's other offers
        # follow it, or they would vanish behind the live-merchant join
        live = conn.execute(
            "SELECT id::text AS id FROM merchants WHERE name=%s AND merged_into IS NULL "
            "ORDER BY id LIMIT 1", (into,)).fetchone()
        if live and live["id"] != str(p["into_merchant_id"]):
            conn.execute(
                "UPDATE merchant_merge_proposals SET into_merchant_id=%s "
                "WHERE status='pending' AND into_merchant_id=%s",
                (live["id"], p["into_merchant_id"]))
        return {"approved": pid, "from": frm, "into": into, **moved}
