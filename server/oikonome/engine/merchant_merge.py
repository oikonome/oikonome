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
import weakref
from collections import Counter, defaultdict

from . import merchant_dedup, merchant_sql
from .compat import as_date, as_dict, jsonb
from .categories import display_label

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
        return "cut short"
    if len(short) in TERMINAL_CUTS:
        return (f"cut short at {len(short)} characters, a card "
                f"terminal's limit")
    # a three-word name cut at a word boundary is a cut, not a sibling
    # ("riverton family health" / "riverton family health care"); a
    # two-word one is a sibling ("amazon prime" / "amazon prime video")
    if len(short.split()) >= 3 and len(short) >= 14:
        return "cut short"
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
            # the city itself cut short by the terminal ("maplecrof")
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


# The two sentences _desc_family writes. They say the rows look alike, not
# that the names do, so they are judged against the names (one_word_only).
_MCC_LINE = re.compile(r"^same registered line of business \(MCC [^)]*\) in ")
_BANK_LINE = re.compile(r'^both carry the bank line "(.*)…"$')
# The longest signal the proposer writes: a sentence around two merchant
# names, each at most a descriptor long. A stored signal past it came from
# somewhere else (a restored archive's cell can be megabytes) and is not
# read at all: every Today load judges each pending offer's signals in the
# shared web process.
MAX_SIGNAL = 2 * 600 + 64      # 600 = sync.base.MAX_DESCRIPTOR (a test holds them)

# what a family offer or a chain adds to its evidence: a pointer, not proof
_POINTER_HEAD, _POINTER_JOIN = 'and "', '" is one merchant with "'
_OUTLETS_HEAD = 'all are outlets of "'


def _is_pointer(s: str) -> bool:
    """'and "A" is one merchant with "B"' or 'all are outlets of "C"', read
    by their fixed words in one pass (a pattern with two open spans
    backtracks on a crafted signal); a line break is never inside one."""
    if "\n" in s or not s.endswith('"'):
        return False
    if s.startswith(_POINTER_HEAD):
        return _POINTER_JOIN in s[len(_POINTER_HEAD):-1]
    return s.startswith(_OUTLETS_HEAD) and len(s) > len(_OUTLETS_HEAD)
_STOP = frozenset({"the", "and", "for", "of"})


def _name_set(name: str | None, cities: list[str]) -> set[str]:
    return {w for w in _minus_city(norm(name), cities).split()
            if len(w) >= 3 and w not in _STOP}


def _abbreviates(short: str, long: str) -> bool:
    """A bank feed's abbreviation of a word: its first letter, then only
    consonants, each in order in the word ("hlth" / "health")."""
    if len(short) < 3 or len(short) >= len(long) or short[0] != long[0]:
        return False
    if any(c in "aeiou" for c in short[1:]):
        return False
    it = iter(long[1:])
    return all(c in it for c in short[1:])


def _shared_words(a: set[str], b: set[str]) -> int:
    """How many words of a are in b, spelled out or abbreviated."""
    return sum(1 for w in a if any(
        w == x or _abbreviates(w, x) or _abbreviates(x, w) for x in b))


def one_word_only(signals: list[str], a_name: str | None, b_name: str | None,
                  cities: list[str]) -> bool:
    """THE rule that an offer rests on one shared word and is no offer: its
    only evidence is that the rows look alike — one registered line of
    business in one town, or a bank-line stem — while the names agree on
    a single ordinary word. Two bars called Juniper in one town are two
    bars. The registered line counts when the names share two words (a
    feed's abbreviation of a word is that word: "hlth" is "health"); the
    stem when it is more than a word both names already carry (a
    run-together spelling is the bank line, not a shared word). Every
    other signal is name evidence and stands. Read by propose() for a new
    pair and by every door that shows or approves a stored one."""
    proof = [s for s in signals
             if len(s) <= MAX_SIGNAL and not _is_pointer(s)]
    if not proof:
        return False
    wa, wb = _name_set(a_name, cities), _name_set(b_name, cities)
    shared = wa & wb
    for s in proof:
        if _MCC_LINE.match(s):
            if _shared_words(wa, wb) >= 2:
                return False
            continue
        m = _BANK_LINE.match(s)
        if m:
            stem = norm(m.group(1)).split()
            if not (len(stem) <= 1 and set(stem) <= shared):
                return False
            continue
        return False
    return True


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
        if _minus_kind(na) != na and _minus_kind(nb) != nb:
            out.append("the same name with a different word for the kind "
                       "of business")
        else:
            longer = na if len(na) > len(nb) else nb
            out.append(f'"{longer}" is the same name with the kind of '
                       "business added")
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
                out.append(f'"{short}" looks like "{long}" {why}')
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
        out.append(f"filed under different categories "
                   f"({display_label(a['category'])} and "
                   f"{display_label(b['category'])})")
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
    """A month as people say it ("Nov 2026"): MM/YY reads as a day."""
    return f"{d:%b %Y}"


def hand_over(a: dict, b: dict) -> str | None:
    """The date reason, or None: two spellings that never overlap in time
    are one terminal re-labelled — the old descriptor stops the month the
    new one starts. Spellings that interleave say nothing either way — a
    cut varies per terminal and two outlets of one chain interleave too —
    so only the clean hand-over is cited. Each side is a name and its
    first and last day (dates or ISO text)."""
    fa, la, fb, lb = (as_date(a.get("first")), as_date(a.get("last")),
                      as_date(b.get("first")), as_date(b.get("last")))
    if not (fa and la and fb and lb and (la < fb or lb < fa)):
        return None
    (old, ol), (new, nf) = (((a, la), (b, fb)) if la < fb
                            else ((b, lb), (a, fa)))
    return (f'"{old["name"]}" stops in {_mmyy(ol)}, '
            f'"{new["name"]}" starts in {_mmyy(nf)}')


# a stored hand-over, in this wording or the older "stops 11/19" one
_HAND_OVER_RE = re.compile(r'^".*" stops (?:in )?.+, ".*" starts (?:in )?.+$')


def supports(a: dict, b: dict) -> list[str]:
    out = []
    if a["category"] and a["category"] == b["category"]:
        out.append(f"both filed under {display_label(a['category'])}")
    why = hand_over(a, b)
    if why:
        out.append(why)
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
    """Who survives a merge: the merchant with more rows; with equal rows,
    the one whose first charge is older. A merge renames one side's rows
    after the other, and an offer must never ask a person to rename a long
    history after a merchant that appeared this week, whatever backs the
    newcomer's name.

    Only then the name's own quality: Plaid-backed, else a name the bank
    did not prefix, else a name that is more than a kind of business
    ("Bakery" alone names nothing), else one carrying a real share of the
    family's rows (`top` = the family's largest count), else the fuller
    name. (A name that is another member's cut short never survives at
    all: `propose` takes those out before ranking.)"""
    n = norm(m["name"])
    prefixed = bool(_BANK_PREFIX.match(m["name"].lower()))
    bare = all(w in _KIND_WORDS for w in n.split()) or lead_token(n) in GENERIC_LEAD
    first = m.get("first")
    # no dated row at all ranks as the newest
    age = -first.toordinal() if first is not None else float("-inf")
    return (m["rows"], age, m["entity"] is not None, not prefixed, not bare,
            m["rows"] * 3 > top, len(n))


# What an offer that may not go with the others says before its reason
SHARE_A_WORD = "share a word — check the address"


def _name_words(name: str | None) -> list[str]:
    """A display name's words for the cut test: lower case, apostrophes
    closed up, and a lone letter dropped — a terminal that cuts "Port
    d'Orvane" writes "Port D Orvane" or "Port Orvane"."""
    s = (name or "").lower().replace("'", " ").replace("’", " ")
    return [w for w in re.split(r"[^a-z0-9]+", s) if len(w) > 1]


def cut_of(short: str | None, long: str | None) -> bool:
    """Is `short` the name `long` cut short by a terminal? Every word but
    the last is the long name's, and the last is either a word cut
    mid-way (three letters at least) or, for a name of three words or
    more, a whole word with more after it. A short name that merely
    shares a leading word ("Ember" / "Ember Restaurant") is not a cut."""
    s, lw = _name_words(short), _name_words(long)
    if not s or len(s) > len(lw) or s == lw:
        return False
    if s[:-1] != lw[:len(s) - 1]:
        return False
    last, whole = s[-1], lw[len(s) - 1]
    if last == whole:
        return len(s) >= 3 and len(s) < len(lw)
    return len(last) >= 3 and whole.startswith(last)


def offer_shape(frm: str | None, into: str | None) -> dict:
    """How an offer may be taken (specs/settings-status-lines.md):
    `bulk` — one name is the other cut short, or the two are one name
    spelled differently, so "Merge all" may take it; `caution` — what any
    other offer says before its reason, for a person to check it one at a
    time; `turn` — the offer would keep the cut name, so it is turned
    around and the full name survives."""
    turn = cut_of(into, frm)
    bulk = (cut_of(frm, into) or turn
            or _same_name(norm(frm), norm(into)))
    return {"bulk": bulk, "caution": None if bulk else SHARE_A_WORD,
            "turn": turn}


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
                if not s or one_word_only(s, a["name"], b["name"], cities):
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
        # a spelling that is another member cut short never survives,
        # whatever backs it: the merge keeps the full name
        whole = [by_id[m] for m in members
                 if not any(cut_of(by_id[m]["name"], by_id[o]["name"])
                            for o in members if o != m)]
        into = max(whole or (by_id[m] for m in members),
                   key=lambda c: _rank(c, top))
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

_ROWS_SQL = f"""
    SELECT {{key}} AS key, t.merchant_id::text AS merchant_id,
           {merchant_sql.RAW_KEY} AS raw_key,
           COALESCE(t.raw->>'original_description', t.name) AS descriptor,
           t.raw->>'merchant_entity_id' AS entity,
           REPLACE(COALESCE(t.category_override, t.category_primary, ''), '_', ' ') AS category,
           t.amount, t.mcc, t.location_city AS city, t.date
      FROM transactions t
     WHERE t.removed = 0 AND t.amount > 0
       AND {merchant_sql.RAW_KEY} IS NOT NULL
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
                             AND """ + merchant_sql.RAW_KEY + """
                                 IS NOT NULL)""").fetchall()


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


# one city list per connection: the ingest pass asks for it on every
# sync, and the GROUP BY behind it walks the whole ledger each time for an
# answer that changes only when a new city lands on two rows — the next
# connection (the next sync, the next request) reads it afresh
_CITIES_MEMO: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def cities_known(conn) -> list[str]:
    """Places a location tail may name: the configured strip list plus every
    city the aggregator has put on a row (a fact, not a guess)."""
    try:
        return list(_CITIES_MEMO[conn])
    except (KeyError, TypeError):
        pass
    out = set(merchant_dedup.cities_for(conn))
    for r in conn.execute(
            "SELECT location_city AS c, count(*) AS n FROM transactions "
            "WHERE removed = 0 AND location_city IS NOT NULL "
            "GROUP BY 1 HAVING count(*) >= 2").fetchall():
        out.add(r["c"])
    cities = sorted(out)
    try:
        _CITIES_MEMO[conn] = cities
    except TypeError:              # a connection that cannot be weakly referenced
        pass
    return list(cities)


def load_rehearsal(conn) -> list[dict]:
    """The ledger as a household with NO alias would see it: rows grouped by
    the string clean of their raw key. Each candidate keeps `labels` — the
    live merchant ids its rows carry today — as the truth to score against."""
    cities = list(merchant_dedup.cities_for(conn))
    rows = conn.execute(_ROWS_SQL.format(key=merchant_sql.RAW_KEY)).fetchall()
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


def follow_merged(conn, losers, survivor) -> int:
    """Carry the decided offers about merchants that were just merged into
    `survivor` across to the survivor's pair.

    A pair is keyed by its two ids, so a merchant moved onto another row —
    a twin fold, a re-clean retiring it — would leave the household's
    answer behind on the old id, and the next proposal run would offer the
    same two businesses again. The decision already on the survivor's pair
    wins; a pending offer there takes the person's answer, as a restore
    does. Called by every writer of merchants.merged_into, inside its
    locked transaction."""
    losers = [str(x) for x in losers]
    survivor = str(survivor)
    moved = 0
    for r in conn.execute(
            "SELECT from_merchant_id::text AS f, into_merchant_id::text AS i, "
            "       evidence, status, created_at, decided_at "
            "  FROM merchant_merge_proposals "
            " WHERE status IN ('rejected', 'approved') "
            "   AND (from_merchant_id = ANY(%s::uuid[]) "
            "        OR into_merchant_id = ANY(%s::uuid[]))",
            (losers, losers)).fetchall():
        f = survivor if r["f"] in losers else r["f"]
        i = survivor if r["i"] in losers else r["i"]
        if f == i:
            continue                     # the pair is one merchant now
        moved += conn.execute(
            """INSERT INTO merchant_merge_proposals (id, from_merchant_id,
                   into_merchant_id, evidence, status, created_at, decided_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, id) DO UPDATE
                  SET status = EXCLUDED.status, decided_at = EXCLUDED.decided_at
                WHERE merchant_merge_proposals.status = 'pending'""",
            (_sig(f, i), f, i, jsonb(r["evidence"] if isinstance(r["evidence"], dict) else {}), r["status"],
             r["created_at"], r["decided_at"])).rowcount
    return moved


def run(conn, *, limit: int = 25, only_ids: list[str] | None = None) -> dict:
    """Write new proposals for the live merchants (bounded per run). A pair
    already decided keeps its decision (deterministic id, DO NOTHING).

    `only_ids` — the ingest path, right after a sync minted merchants — is
    a real fast path, not a filter applied to a full scan: only those
    merchants' FAMILIES are loaded (`_neighbourhood` reads names, not
    rows), so an hourly sync or a CSV upload does not pay for a
    whole-ledger dedup pass. The offers it writes are the ones the nightly full run
    would write for those merchants.

    The nightly full run also SWEEPS: a pending offer whose merchant was
    merged away or deleted is retired. One whose side merely holds no
    counted row today stays pending, hidden until the rows return."""
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
        # A side that holds no counted row today is hidden by
        # live_pair_sql, never retired: its rows can come back.
        stats["stale"] = conn.execute(
            f"""UPDATE merchant_merge_proposals p
                  SET status = 'stale', decided_at = now()
                WHERE p.status = 'pending' AND NOT {pair_merchants_sql()}
            """).rowcount
        cands = load_live(conn)
        props = propose(cands, cities)
    stats["pairs"] = len(props)

    def _stored(p):
        return jsonb({"signals": p["signals"], "supports": p["supports"],
                      **({"rename_to": p["rename_to"]} if p.get("rename_to") else {}),
                      "from": {"name": p["from"]["name"], "rows": p["from"]["rows"],
                               "samples": p["from"]["samples"]},
                      "into": {"name": p["into"]["name"], "rows": p["into"]["rows"],
                               "samples": p["into"]["samples"]}})

    # An offer's id is its pair, whichever way it points, so one stored the
    # other way round (the ledger grew since, or it was ranked by an older
    # rule) would stand as stored for as long as it is pending. It is
    # turned in place: the survivor is the side `propose` ranks higher
    # NOW. Not counted as inserted — the alert announces new offers. A
    # decided pair is never touched. The offer keeps `turned`, so an
    # approve that does not name the survivor it was shown is refused
    # rather than carried out the other way round (decide).
    for p in props:
        stats["turned"] = stats.get("turned", 0) + conn.execute(
            """UPDATE merchant_merge_proposals
                  SET from_merchant_id = %s, into_merchant_id = %s,
                      evidence = %s::jsonb || '{"turned": true}'::jsonb
                WHERE id = %s AND status = 'pending'
                  AND from_merchant_id = %s AND into_merchant_id = %s""",
            (p["from"]["id"], p["into"]["id"], _stored(p),
             _sig(p["from"]["id"], p["into"]["id"]),
             p["into"]["id"], p["from"]["id"])).rowcount
    for p in props[:limit]:
        cur = conn.execute(
            """INSERT INTO merchant_merge_proposals
                   (id, from_merchant_id, into_merchant_id, evidence, status)
               VALUES (%s, %s, %s, %s, 'pending')
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (_sig(p["from"]["id"], p["into"]["id"]), p["from"]["id"],
             p["into"]["id"], _stored(p)))
        stats["inserted"] += cur.rowcount
    return stats


RECENT_SHOWN = 3


def side_facts(conn, ids: list[str]) -> dict[str, dict]:
    """What the ledger holds under each merchant today, so a person can
    tell the two sides of an offer apart before joining them: how many
    rows and how much — every row, refunds and payouts included, because
    approving moves every one — over which dates, the usual charge, where,
    through which accounts, and the latest bank lines. Read live — the
    evidence stored with the offer is the ledger as it was the night the
    offer was made."""
    if not ids:
        return {}
    # a linked account's non-primary sources and hidden accounts count for
    # nothing, as in every other total: a dual-sourced card would otherwise
    # show each side's spend twice
    from . import links
    shadows = links.shadow_ids(conn)
    facts: dict[str, dict] = {}
    for r in conn.execute(
            """SELECT t.merchant_id::text AS id, count(*) AS n,
                      SUM(t.amount) AS total, MIN(t.date) AS first, MAX(t.date) AS last,
                      PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.amount)
                          FILTER (WHERE t.amount > 0) AS typical,
                      MODE() WITHIN GROUP (ORDER BY REPLACE(COALESCE(
                          t.category_override, t.category_primary, ''), '_', ' '))
                          FILTER (WHERE t.amount > 0) AS category,
                      MODE() WITHIN GROUP (ORDER BY t.location_city)
                          FILTER (WHERE t.amount > 0) AS city,
                      ARRAY_AGG(DISTINCT COALESCE(a.display_name, a.name))
                          FILTER (WHERE COALESCE(a.display_name, a.name) IS NOT NULL)
                          AS accounts
                 FROM transactions t
                 LEFT JOIN accounts a ON a.id = t.account_id
                WHERE t.removed = 0
                  AND t.merchant_id = ANY(%s::uuid[])
                  AND NOT (t.account_id = ANY(%s))
                GROUP BY 1""", (list(ids), shadows)).fetchall():
        facts[r["id"]] = {
            "rows": r["n"], "total": round(float(r["total"] or 0), 2),
            "first": str(as_date(r["first"])) if r["first"] else None,
            "last": str(as_date(r["last"])) if r["last"] else None,
            "typical": round(float(r["typical"]), 2) if r["typical"] is not None else None,
            "category": r["category"] or None, "city": r["city"] or None,
            "accounts": list(r["accounts"] or []), "recent": []}
    for r in conn.execute(
            """SELECT * FROM (
                 SELECT t.merchant_id::text AS id, t.date, t.amount,
                        COALESCE(t.raw->>'original_description', t.name) AS line,
                        COALESCE(a.display_name, a.name) AS account,
                        ROW_NUMBER() OVER (PARTITION BY t.merchant_id
                                           ORDER BY t.date DESC, t.id) AS k
                   FROM transactions t
                   LEFT JOIN accounts a ON a.id = t.account_id
                  WHERE t.removed = 0 AND t.amount > 0
                    AND t.merchant_id = ANY(%s::uuid[])
                    AND NOT (t.account_id = ANY(%s))) x
               WHERE k <= %s ORDER BY id, k""",
            (list(ids), shadows, RECENT_SHOWN)).fetchall():
        # The totals and the lines are two statements, so a charge ingested
        # between them puts a merchant in this answer that the first read
        # never saw. Its totals would be a year out of date anyway, so the
        # side is left factless for this load rather than half-stated —
        # and the page loads instead of raising.
        side = facts.get(r["id"])
        if side is None:
            continue
        side["recent"].append({
            "date": str(as_date(r["date"])), "amount": round(float(r["amount"]), 2),
            "line": (r["line"] or "")[:60], "account": r["account"]})
    return facts


def _evidence(raw) -> dict:
    """An offer's stored evidence read by type, the one read every door
    uses: a side that is not a map reads as a side with no facts, a list
    that is not a list as empty, a brand that is not text as none. A
    restored or hand-edited row of another shape is less evidence, never
    a Merchants screen that cannot load or an approve that raises."""
    ev = as_dict(raw)

    def side(k):
        s = ev.get(k)
        s = s if isinstance(s, dict) else {}
        rows = s.get("rows")
        return {"rows": rows if isinstance(rows, int)
                and not isinstance(rows, bool) else None,
                "samples": [x for x in s.get("samples") if isinstance(x, str)]
                if isinstance(s.get("samples"), list) else []}

    def listed(k):
        v = ev.get(k)
        # strings only: the clients render each item as text, and an
        # object inside a restored list would take the whole screen down
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    brand = ev.get("rename_to")
    return {"from": side("from"), "into": side("into"),
            "signals": listed("signals"), "supports": listed("supports"),
            "rename_to": brand if isinstance(brand, str) and brand else None}


# An offer is two merchants that each still hold a row — a live merchant
# and one the side facts can describe (removed rows, and a linked
# account's second feed or a hidden account, count for nothing there).
# Without a row a side has an empty box and no "After merge", and the
# merge it offers moves nothing. pending() shows only these and notice()
# counts only these. Holding no row is a state of today, not a verdict:
# a feed withdraws and re-posts rows, an account is hidden for a while,
# a failover swaps which feed of a card is primary. So the nightly never
# retires an offer for it — only for a merchant merged away or deleted
# (pair_merchants_sql), which does not come back. Two placeholders: the
# shadow accounts, twice.
def _has_rows(m: str) -> str:
    return (f"""EXISTS (SELECT 1 FROM transactions t
                         WHERE t.merchant_id = {m}.id AND t.removed = 0
                           AND NOT (t.account_id = ANY(%s)))""")


def pair_merchants_sql(p: str = "p", extra: str = "") -> str:
    """Whether proposal `p`'s two merchants both still exist unmerged —
    the only lasting reason an offer stops being one (see above)."""
    return f"""EXISTS (SELECT 1 FROM merchants f, merchants i
                        WHERE f.id = {p}.from_merchant_id AND f.merged_into IS NULL
                          AND i.id = {p}.into_merchant_id AND i.merged_into IS NULL
                          {extra})"""


def live_pair_sql(p: str = "p") -> str:
    """THE test that proposal `p` is an offer to show today (see above)."""
    return pair_merchants_sql(
        p, f'AND {_has_rows("f")} AND {_has_rows("i")}')


def _live_pair_args(conn) -> tuple:
    from . import links
    shadows = links.shadow_ids(conn)
    return (shadows, shadows)


def live_supports(stored: list[str], a: dict, b: dict) -> list[str]:
    """An offer's reasons as shown: every one worked out again by
    supports(), the function that wrote them, from the two sides as the
    ledger holds them now (`a`/`b`: a name and its live facts — the
    boxes beside the reasons — from side_facts). A stored reason is the
    ledger of the night the offer was made: a category one side has
    since left, charges that no longer look alike, or a side said to stop
    before its box's last charge would each contradict the box, and one
    stored in an older wording keeps it. A side the ledger has no facts
    for (a row ingested mid-read) keeps the stored reasons, the date
    reason left out: there is no box to contradict and no date to read."""
    if a.get("rows") and b.get("rows"):
        return supports(*(_live_side(x) for x in (a, b)))
    return [x for x in stored if not _HAND_OVER_RE.match(x)]


def _live_side(side: dict) -> dict:
    """A side's live facts in the shape supports() reads: its category
    as the box prints it, and its typical charge as the median."""
    return {"name": side.get("name"), "first": side.get("first"),
            "last": side.get("last"), "category": side.get("category") or "",
            "median": side.get("typical")}


def pending(conn) -> list[dict]:
    """Pending proposals whose merchants are both still live and still
    hold rows, with names and each side's facts."""
    out = []
    cities = cities_known(conn)
    for r in conn.execute(
            f"""SELECT p.id, p.evidence, p.created_at,
                      f.name AS from_name, f.logo_url AS from_logo, f.id::text AS from_id,
                      i.name AS into_name, i.logo_url AS into_logo, i.id::text AS into_id
                 FROM merchant_merge_proposals p
                 JOIN merchants f ON f.id = p.from_merchant_id
                 JOIN merchants i ON i.id = p.into_merchant_id
                WHERE p.status = 'pending' AND {live_pair_sql()}
                ORDER BY p.created_at, p.id""",
            _live_pair_args(conn)).fetchall():
        ev = _evidence(r["evidence"])
        # stored before the rule, or by an older one: not an offer today
        if one_word_only(ev["signals"], r["from_name"], r["into_name"], cities):
            continue
        shape = offer_shape(r["from_name"], r["into_name"])
        f, i = "from", "into"
        if shape["turn"] and not ev["rename_to"]:
            # stored onto the cut spelling before the full name was made
            # the survivor: shown — and approved — the other way round
            f, i = "into", "from"
        # a chain offer names the brand, which no member wears yet
        out.append({"id": r["id"], "from": r[f"{f}_name"],
                    "into": ev["rename_to"] or r[f"{i}_name"],
                    "into_name": r[f"{i}_name"],
                    "from_id": r[f"{f}_id"], "into_id": r[f"{i}_id"],
                    "from_logo": r[f"{f}_logo"], "into_logo": r[f"{i}_logo"],
                    "from_rows": ev[f]["rows"],
                    "into_rows": ev[i]["rows"],
                    "from_samples": ev[f]["samples"],
                    "into_samples": ev[i]["samples"],
                    "signals": ev["signals"],
                    "supports": ev["supports"],
                    "bulk": shape["bulk"], "caution": shape["caution"]})
    facts = side_facts(conn, [i for o in out for i in (o["from_id"], o["into_id"])])
    for o in out:
        o["from_facts"] = facts.get(o["from_id"])
        o["into_facts"] = facts.get(o["into_id"])
        o["merged"] = merged_facts(o["from_facts"], o["into_facts"])
        o["supports"] = live_supports(
            o["supports"], {"name": o["from"], **(o["from_facts"] or {})},
            {"name": o["into_name"], **(o["into_facts"] or {})})
    return out


def _signed_whole(d: int) -> str:
    """A change in whole dollars with its sign, as both clients print it
    beside a figure: "+$1,250", "−$50"; nothing moved is "+$0"."""
    return f"{'−' if d < 0 else '+'}${abs(d):,}"


def merged_facts(f: dict | None, i: dict | None) -> dict | None:
    """What approving an offer makes, for the "After merge" box: both
    sides' rows and total together, the dates they span, and what the
    folded side adds — the row count and the dollars, signed, in the
    words printed. None unless both sides have facts: half a sum is a
    figure nobody can check against the two boxes above it.

    The dollar change is the difference of the two totals AS PRINTED
    (whole dollars, halves to even, as the clients' money() rounds), so
    the three figures on the box agree with each other to the dollar."""
    if not f or not i:
        return None
    from .reporting import whole_dollars
    dates = sorted(d for d in (f.get("first"), f.get("last"),
                               i.get("first"), i.get("last")) if d)
    total = round(f["total"] + i["total"], 2)
    return {"rows": f["rows"] + i["rows"], "total": total,
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "rows_delta": f"+{f['rows']:,}",
            "total_delta": _signed_whole(
                whole_dollars(total) - whole_dollars(i["total"]))}


# how long a household's first offers sit on the Merchants page before
# the alert strip mentions them
NOTICE_HOLD_DAYS = 3


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
    merge made on the Merchants page took one of the two away.

    A household's FIRST offers wait NOTICE_HOLD_DAYS before the strip
    names them. They are found on the first night's ledger, so without
    the hold a household that has just finished setting up opens Today
    to a chore about merchant spellings before it has read its own
    numbers. The offers themselves are on the Merchants page from the
    start; only the notice waits."""
    r = conn.execute(
        f"""SELECT (SELECT COUNT(*) FROM merchant_merge_proposals p
                    WHERE p.status = 'pending' AND {live_pair_sql()})
                      AS pending,
                  MAX(created_at)::date AS found,
                  MIN(created_at) <= now() - make_interval(days => %s)
                      AS settled
             FROM merchant_merge_proposals""",
        (*_live_pair_args(conn), NOTICE_HOLD_DAYS)).fetchone()
    if not r or not r["pending"] or not r["settled"]:
        return None
    # the count is of the offers pending() shows: one resting on a single
    # shared word is left off the list, so it is left out of the count
    pending_n = r["pending"] - _one_word_count(conn)
    if not pending_n:
        return None
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM merchant_merge_proposals "
        "WHERE created_at::date = %s", (r["found"],)).fetchone()["n"]
    return {"pending": pending_n, "batch": n, "found": r["found"].isoformat()}


def _one_word_rows(conn, pid: str | None = None) -> list[dict]:
    """The live pending offers (or the one named) that rest on one shared
    word — the ones pending() leaves off the list."""
    cities = cities_known(conn)
    rows = conn.execute(
        f"""SELECT p.id, p.evidence, f.name AS from_name, i.name AS into_name
              FROM merchant_merge_proposals p
              JOIN merchants f ON f.id = p.from_merchant_id
              JOIN merchants i ON i.id = p.into_merchant_id
             WHERE p.status = 'pending' AND {live_pair_sql()}
               {"AND p.id = %s" if pid is not None else ""}""",
        (*_live_pair_args(conn), *((pid,) if pid is not None else ()))).fetchall()
    return [r for r in rows if one_word_only(
        _evidence(r["evidence"])["signals"], r["from_name"], r["into_name"], cities)]


def _one_word_count(conn) -> int:
    return len(_one_word_rows(conn))


def settle_alert(conn) -> bool:
    """Retire the "merges" alert the moment the queue it names is empty —
    after the decision that empties it. Without this the row stays active
    until the next Today build rewrites the snapshot, and the person who
    just worked the queue from the alert's own link comes back to find it
    still lit. A merge made by hand on the Merchants page does not empty
    the queue: its offer stays listed until the nightly prunes the emptied
    merchant, and that same nightly rewrites the snapshot. Returns whether
    anything was retired."""
    if notice(conn) is not None:
        return False
    from . import alerts
    return alerts.retire(conn, "merges") > 0


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


def _listed_survivor(conn, pid: str) -> tuple[str, str, object] | None:
    """(survivor id, merged-away id, evidence) of a stored offer, the way
    pending() lists it and decide() carries it out: an offer stored onto
    the cut spelling keeps the full name. Read off the merchant rows
    whether or not they are still live."""
    r = conn.execute(
        "SELECT p.from_merchant_id::text AS f, p.into_merchant_id::text AS i,"
        "       p.evidence, fm.name AS fn, im.name AS iname"
        "  FROM merchant_merge_proposals p"
        "  JOIN merchants fm ON fm.id = p.from_merchant_id"
        "  JOIN merchants im ON im.id = p.into_merchant_id"
        " WHERE p.id = %s", (pid,)).fetchone()
    if not r:
        return None
    if not _evidence(r["evidence"])["rename_to"] and offer_shape(
            r["fn"], r["iname"])["turn"]:
        return r["f"], r["i"], r["evidence"]
    return r["i"], r["f"], r["evidence"]


def _shown_is_this_merge(conn, evidence, shown, into_id, gone_id) -> bool:
    """Whether the survivor an approve was listed with (`shown`) still
    names the merge this offer now stands for. It does when it is the
    survivor; when it has since been folded into the survivor; and when an
    earlier approve folded the offer's OTHER side into the survivor (the
    offer recorded that fold as `followed`) — the pair the person approved,
    one step on, which leaves `shown` the side that goes. A survivor that
    swapped sides with no fold between (a sync turning the offer) is none
    of these."""
    from .merchant_identity import _survivor
    shown, into_id = str(shown), str(into_id)
    if shown == into_id or str(_survivor(conn, shown)) == into_id:
        return True
    if shown != str(gone_id):
        return False
    followed = as_dict(evidence).get("followed")
    for f in followed if isinstance(followed, list) else ():
        if (isinstance(f, list) and len(f) == 2
                and str(_survivor(conn, f[1]) or f[1]) == into_id):
            return True
    return False


def decide(conn, pid: str, action: str, *, bulk: bool = False,
           shown_into: str | None = None) -> dict:
    """approve = merge through the journalled rename door (undo on the
    Merchants page works unchanged); reject = never offered again.

    `bulk` is "Merge all" approving an offer it listed as a cut-off pair.
    An earlier approve in the same batch may have settled this offer or
    re-pointed it onto the survivor, and a re-pointed pair may now only
    share a word: such an offer comes back `conflict` (the client skips it
    and refetches) and is never merged unseen.

    `shown_into` is the survivor's id as `pending` listed it. A sync may
    turn a pending offer in place between that listing and this approve;
    when the survivor is no longer the one shown, the approve is a
    `conflict` and nothing merges, so it is never carried out the other
    way round. An offer that followed an earlier approve's fold is still
    the merge it was shown as (_shown_is_this_merge)."""
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
            return {"error": "unknown or already-decided proposal",
                    "conflict": bulk}
        if action == "reject":
            conn.execute("UPDATE merchant_merge_proposals SET status='rejected', "
                         "decided_at=now() WHERE id=%s", (pid,))
            # rejecting the last offer empties the queue as surely as a merge
            settle_alert(conn)
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
            return {"error": "one of the merchants is gone", "conflict": bulk}
        # a side with no counted row today is left off the list (pending()
        # shows only live_pair_sql offers): one approved from a tab that
        # listed it earlier would merge a side nobody can see, so it is
        # the conflict a turned offer is — the client lists them again
        if not conn.execute(
                f"SELECT {live_pair_sql()} AS live "
                f"FROM merchant_merge_proposals p WHERE p.id = %s",
                (*_live_pair_args(conn), pid)).fetchone()["live"]:
            return {"error": "this offer changed since it was listed — "
                             "check it again", "conflict": True}
        # an offer pending() no longer lists, approved from a tab that did
        if _one_word_rows(conn, pid):
            return {"error": "this offer changed since it was listed — "
                             "check it again", "conflict": True}
        if bulk and not offer_shape(frm, into)["bulk"]:
            return {"error": "this pair changed since it was listed — "
                             "check it on its own", "conflict": True}
        # merchant_dedup.rename works by DISPLAY NAME, and a rename onto a
        # name that already exists is a MERGE. So both names this approve
        # is about have to name one merchant each, or approving performs a
        # merge the person was never shown: a third live merchant already
        # wearing the chain brand would absorb the survivor, and a twin of
        # `frm` would be dragged along with it. Refuse and name the other
        # merchant — merging into it is a thing the person can choose on
        # the Merchants page, with both names in front of them. (The offer
        # stays pending; rejecting it is still one click.)
        brand = _evidence(p["evidence"])["rename_to"]
        pair = (str(p["from_merchant_id"]), str(p["into_merchant_id"]))
        # the survivor's merchant row, which the family's other offers
        # follow below
        into_id = p["into_merchant_id"]
        gone_id = p["from_merchant_id"]
        if not brand and offer_shape(frm, into)["turn"]:
            # an offer stored onto the cut spelling keeps the full name
            frm, into = into, frm
            into_id, gone_id = p["from_merchant_id"], p["into_merchant_id"]
        if shown_into is not None and not _shown_is_this_merge(
                conn, p["evidence"], shown_into, into_id, gone_id):
            return {"error": "this offer changed since it was listed — "
                             "check it again", "conflict": True}
        if shown_into is None and as_dict(p["evidence"]).get("turned"):
            # an approve that does not say which survivor it was shown (an
            # older app) cannot be told from one listed before the offer
            # was turned: refused, never merged the other way round
            return {"error": "this offer was turned around since it was "
                             "first offered — update the app to approve it, "
                             "or open the merchant and use Merge into",
                    "conflict": True}
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
        survivor = live["id"] if live else str(into_id)
        # every pending offer naming a merchant this approve folded away —
        # the survivor's old row, and the merged-away spelling (on a turned
        # offer that is the row the family's other offers were stored
        # onto) — now names the survivor, on whichever side it stood, so
        # the next "Merge all" approve finds both its merchants live
        folded = list({str(gone_id), str(into_id)} - {survivor})
        before = {r["id"]: _listed_survivor(conn, r["id"]) for r in conn.execute(
            "SELECT id FROM merchant_merge_proposals WHERE status='pending' "
            "AND (from_merchant_id::text = ANY(%s) "
            "     OR into_merchant_id::text = ANY(%s))",
            (folded, folded)).fetchall()}
        for old_id in folded:
            for col in ("into_merchant_id", "from_merchant_id"):
                # the offer records the fold it followed, so the survivor it
                # was listed with still answers for it (_shown_is_this_merge)
                conn.execute(
                    f"UPDATE merchant_merge_proposals SET {col}=%s, "
                    f"evidence = CASE WHEN evidence IS NULL "
                    f"  OR jsonb_typeof(evidence) = 'object' THEN "
                    f"  COALESCE(evidence, '{{}}'::jsonb) || jsonb_build_object("
                    f"    'followed', COALESCE(CASE WHEN jsonb_typeof("
                    f"      evidence->'followed') = 'array' THEN "
                    f"      evidence->'followed' END, '[]'::jsonb)"
                    f"    || jsonb_build_array(jsonb_build_array("
                    f"      %s::text, %s::text)))"
                    f"  ELSE evidence END "
                    f"WHERE status='pending' AND {col}::text=%s",
                    (survivor, old_id, survivor, old_id))
        # an offer whose survivor, as it was listed before this fold, no
        # longer names the merge it now stands for was turned by it
        for opid, was in before.items():
            now = _listed_survivor(conn, opid)
            if was and now and not _shown_is_this_merge(
                    conn, now[2], was[0], now[0], now[1]):
                conn.execute(
                    "UPDATE merchant_merge_proposals SET evidence = "
                    "COALESCE(evidence, '{}'::jsonb) || '{\"turned\": true}'::jsonb "
                    "WHERE id = %s AND jsonb_typeof(COALESCE(evidence, "
                    "'{}'::jsonb)) = 'object'", (opid,))
        # an offer that now pairs the survivor with itself has nothing left
        # to decide
        conn.execute(
            "UPDATE merchant_merge_proposals SET status='stale', "
            "decided_at=now() WHERE status='pending' "
            "AND from_merchant_id = into_merchant_id")
        # now that the offer is decided the queue may be empty
        settle_alert(conn)
        return {"approved": pid, "from": frm, "into": into, **moved}
