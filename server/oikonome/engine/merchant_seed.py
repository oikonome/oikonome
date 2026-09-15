"""Curated merchant → category seed rules.

A few hundred national chains and payment-processor patterns cover the
bulk of any US ledger — categorizing them deterministically is instant,
free, and never wrong the way a small local model can be. The LLM only
sees what these rules don't claim.

Matching is word-boundary based on the upper-cased merchant string, so
"SHELL" hits "SHELL OIL 5723" but not "SHELLY'S CAFE". PREFIXES handle
POS processors whose prefix itself implies the business type (TST* =
Toast → restaurants). Deliberately NOT here: anything ambiguous
(Amazon spans every category and has its own pipeline; SQ*/PAYPAL*
merchants could be anything), and any flow category (transfers, income,
loan payments) — those stay the aggregator/importer's call.
"""
from __future__ import annotations

import re

# processor prefixes that imply the business type
PREFIXES: list[tuple[str, str]] = [
    ("TST*", "FOOD_AND_DRINK"),        # Toast POS — restaurants
    ("TST *", "FOOD_AND_DRINK"),
    ("DD *DOORDASH", "FOOD_AND_DRINK"),
    ("DOORDASH", "FOOD_AND_DRINK"),
    ("GRUBHUB", "FOOD_AND_DRINK"),
    ("UBER EATS", "FOOD_AND_DRINK"),
    ("UBEREATS", "FOOD_AND_DRINK"),
]

RULES: dict[str, tuple[str, ...]] = {
    "FOOD_AND_DRINK": (
        # groceries / supermarkets
        "TRADER JOE", "TRADER JOES", "WHOLE FOODS", "SAFEWAY", "KROGER",
        "ALBERTSONS",
        "PUBLIX", "WEGMANS", "ALDI", "WINCO", "FRED MEYER", "SPROUTS",
        "PIGGLY WIGGLY", "FOOD LION", "GIANT EAGLE", "HARRIS TEETER",
        "MEIJER", "H-E-B", "HEB GROCERY", "STOP & SHOP", "VONS", "RALPHS",
        "SMITHS", "KING SOOPERS", "HY-VEE", "SHOPRITE", "GROCERY OUTLET",
        "LIDL", "INSTACART", "MARKET BASKET",
        # restaurants / fast food / coffee
        "MCDONALD", "BURGER KING", "WENDY", "TACO BELL", "CHIPOTLE",
        "CHICK-FIL-A", "CHICKFILA", "KFC", "POPEYES", "SUBWAY",
        "JIMMY JOHN", "JERSEY MIKE", "FIVE GUYS", "IN-N-OUT", "IN N OUT",
        "SHAKE SHACK", "PANERA", "PANDA EXPRESS", "OLIVE GARDEN",
        "APPLEBEE", "CHILIS", "CHILI'S", "OUTBACK", "RED ROBIN", "DENNYS",
        "DENNY'S", "IHOP", "WAFFLE HOUSE", "CRACKER BARREL", "PIZZA HUT",
        "DOMINO", "PAPA JOHN", "LITTLE CAESARS", "PAPA MURPHY",
        "STARBUCKS", "DUTCH BROS", "DUNKIN", "PEETS", "PEET'S",
        "TIM HORTONS", "JAMBA", "SMOOTHIE KING", "BASKIN", "DAIRY QUEEN",
        "COLD STONE", "KRISPY KREME", "CINNABON", "WINGSTOP",
        "BUFFALO WILD WINGS", "SONIC DRIVE", "ARBYS", "ARBY'S", "CARL'S JR",
        "CARLS JR", "HARDEES", "HARDEE'S", "JACK IN THE BOX", "WHATABURGER",
        "QDOBA", "DEL TACO", "EL POLLO LOCO", "RAISING CANE", "ZAXBY",
        "BOJANGLES", "CULVER", "FIREHOUSE SUBS", "POTBELLY", "SWEETGREEN",
        "CAVA", "NOODLES & COMPANY", "PF CHANG", "CHEESECAKE FACTORY",
        "TEXAS ROADHOUSE", "LONGHORN STEAKHOUSE", "RED LOBSTER",
    ),
    "GENERAL_MERCHANDISE": (
        "WALMART", "WAL-MART", "TARGET", "COSTCO", "SAMS CLUB",
        "SAM'S CLUB", "BJS WHOLESALE", "DOLLAR TREE", "DOLLAR GENERAL",
        "FIVE BELOW", "BIG LOTS", "ROSS ", "ROSS DRESS", "TJ MAXX",
        "TJMAXX", "MARSHALLS", "BURLINGTON", "NORDSTROM", "MACYS",
        "MACY'S", "KOHLS", "KOHL'S", "JCPENNEY", "DILLARDS", "BELK",
        "OLD NAVY", "GAP ", "BANANA REPUBLIC", "H&M", "ZARA", "UNIQLO",
        "FOREVER 21", "AMERICAN EAGLE", "HOLLISTER", "ABERCROMBIE",
        "VICTORIAS SECRET", "VICTORIA'S SECRET", "NIKE", "ADIDAS",
        "FOOT LOCKER", "DICKS SPORTING", "DICK'S SPORTING", "ACADEMY SPORTS",
        "BASS PRO", "CABELAS", "CABELA'S", "REI ", "REI.COM", "BEST BUY",
        "GAMESTOP", "MICRO CENTER", "B&H PHOTO", "APPLE STORE",
        "APPLE.COM/BILL"[:12], "BARNES & NOBLE", "BOOKS-A-MILLION",
        "MICHAELS", "HOBBY LOBBY", "JOANN", "PETSMART", "PETCO",
        "CHEWY", "ETSY", "EBAY", "WAYFAIR", "OVERSTOCK", "TEMU", "SHEIN",
        "ALIEXPRESS", "WISH.COM", "WALGREENS PHOTO", "PARTY CITY",
        "SPIRIT HALLOWEEN", "ULTA", "SEPHORA", "BATH & BODY WORKS",
        "BATH AND BODY WORKS", "CARTERS", "CARTER'S", "OSHKOSH",
        "BUY BUY BABY", "TOYS R US", "LEGO",
    ),
    "GENERAL_SERVICES": (
        "JIFFY LUBE", "VALVOLINE", "MIDAS", "FIRESTONE", "GOODYEAR",
        "DISCOUNT TIRE", "LES SCHWAB", "PEP BOYS", "AUTOZONE",
        "O'REILLY", "OREILLY", "ADVANCE AUTO", "NAPA AUTO", "CARQUEST",
        "MEINEKE", "AAMCO", "SAFELITE", "GEICO", "PROGRESSIVE",
        "STATE FARM", "ALLSTATE", "FARMERS INS", "LIBERTY MUTUAL",
        "NATIONWIDE INS", "USAA INSURANCE", "H&R BLOCK", "TURBOTAX",
        "INTUIT", "JACKSON HEWITT", "UPS STORE", "FEDEX OFFICE", "USPS",
        "STAMPS.COM", "LEGALZOOM", "ADOBE", "MICROSOFT 365", "MSFT",
        "GOOGLE STORAGE", "GOOGLE ONE", "DROPBOX", "ICLOUD", "NORDVPN",
        "EXPRESSVPN", "1PASSWORD", "LASTPASS", "GODADDY", "NAMECHEAP",
        "SQUARESPACE", "WIX.COM", "MAILCHIMP", "ZOOM.US", "LINKEDIN",
        "COURSERA", "UDEMY", "SKILLSHARE", "CHATGPT", "OPENAI", "GITHUB",
    ),
    "TRANSPORTATION": (
        "SHELL", "CHEVRON", "EXXON", "MOBIL", "TEXACO", "ARCO", "BP ",
        "BP#", "CONOCO", "PHILLIPS 66", "SINCLAIR", "VALERO", "SUNOCO",
        "MARATHON PETRO", "CIRCLE K", "7-ELEVEN", "SPEEDWAY", "WAWA",
        "QUIKTRIP", "RACETRAC", "SHEETZ", "CASEYS", "CASEY'S", "MAVERIK",
        "PILOT ", "FLYING J", "LOVES TRAVEL", "LOVE'S TRAVEL", "COSTCO GAS",
        "SAMS FUEL", "KWIK TRIP", "UBER TRIP", "UBER *TRIP", "LYFT",
        "AMTRAK", "GREYHOUND", "BART", "MTA ", "METRO TRANSIT", "E-ZPASS",
        "EZPASS", "FASTRAK", "GOOD TO GO", "TOLL", "PARKMOBILE",
        "SPOTHERO", "PARKWHIZ", "IMPARK", "LAZ PARKING", "HERTZ", "AVIS",
        "ENTERPRISE RENT", "BUDGET RENT", "NATIONAL CAR RENTAL", "TURO",
        "ZIPCAR", "U-HAUL", "UHAUL",
    ),
    "MEDICAL": (
        "CVS", "WALGREENS", "RITE AID", "KAISER", "QUEST DIAGNOSTICS",
        "LABCORP", "URGENT CARE", "DENTAL", "ORTHODONT", "PEDIATRIC",
        "DERMATOLOGY", "OPTOMETR", "VISION CENTER", "LENSCRAFTERS",
        "AMERICAS BEST", "WARBY PARKER", "PHARMACY", "CLINIC", "HOSPITAL",
        "MEDICAL CENTER", "PHYSICAL THERAPY", "CHIROPRACT", "VETERINAR",
        "VCA ANIMAL", "BANFIELD", "GOODRX", "TELADOC", "ONE MEDICAL",
    ),
    "HOME_IMPROVEMENT": (
        "HOME DEPOT", "LOWES", "LOWE'S", "MENARDS", "ACE HARDWARE",
        "TRUE VALUE", "HARBOR FREIGHT", "TRACTOR SUPPLY", "SHERWIN",
        "BENJAMIN MOORE", "FLOOR & DECOR", "IKEA", "ASHLEY FURNITURE",
        "ASHLEY HOMESTORE", "LA-Z-BOY", "ROOMS TO GO", "MATTRESS FIRM",
        "SLEEP NUMBER", "POTTERY BARN", "WEST ELM", "CRATE & BARREL",
        "CRATE AND BARREL", "RESTORATION HARDWARE", "CONTAINER STORE",
        "BED BATH", "HOMEGOODS", "AT HOME", "KIRKLANDS", "PUBLIC STORAGE",
        "EXTRA SPACE", "CUBESMART", "LIFE STORAGE", "TERMINIX", "ORKIN",
        "TRUGREEN", "ADT SECURITY", "RING.COM", "SIMPLISAFE",
    ),
    "RENT_AND_UTILITIES": (
        "COMCAST", "XFINITY", "SPECTRUM", "COX COMM", "CENTURYLINK",
        "LUMEN", "FRONTIER COMM", "AT&T", "ATT*BILL", "VERIZON",
        "T-MOBILE", "TMOBILE", "MINT MOBILE", "CRICKET",
        "BOOST MOBILE", "VISIBLE", "GOOGLE FI", "STARLINK", "DISH NETWORK",
        "DIRECTV", "PG&E", "PACIFIC GAS", "SOCAL EDISON", "SO CAL EDISON",
        "SDG&E", "DUKE ENERGY", "GEORGIA POWER", "FLORIDA POWER", "FPL",
        "CON EDISON", "CONEDISON", "NATIONAL GRID", "XCEL ENERGY",
        "PUGET SOUND ENERGY", "ROCKY MOUNTAIN POWER",
        "AVISTA", "DOMINION ENERGY", "CENTERPOINT", "AMEREN", "PSEG",
        "WASTE MANAGEMENT", "REPUBLIC SERVICES", "RECOLOGY",
        "CITY UTILITIES", "WATER DISTRICT", "SEWER",
    ),
    "ENTERTAINMENT": (
        "NETFLIX", "HULU", "DISNEY PLUS", "DISNEY+", "DISNEYPLUS", "MAX ",
        "HBO", "PARAMOUNT+", "PARAMOUNT PLUS", "PEACOCK", "APPLE TV",
        "APPLETV", "YOUTUBE PREMIUM", "YOUTUBE TV", "YOUTUBEPREMIUM",
        "SPOTIFY", "PANDORA", "SIRIUSXM", "AUDIBLE", "KINDLE", "AMC ",
        "AMC THEATRES", "REGAL CINEMAS", "CINEMARK", "FANDANGO",
        "TICKETMASTER", "STUBHUB", "AXS.COM", "LIVE NATION", "EVENTBRITE",
        "STEAM GAMES", "STEAMGAMES", "PLAYSTATION", "XBOX", "NINTENDO",
        "EPIC GAMES", "RIOT GAMES", "BLIZZARD", "TWITCH", "PATREON",
        "DAVE & BUSTER", "DAVE AND BUSTER", "TOPGOLF", "BOWLERO",
        "SIX FLAGS", "DISNEYLAND", "DISNEY WORLD", "UNIVERSAL STUDIOS",
        "SEAWORLD", "LEGOLAND", "MUSEUM", "ZOO ",
    ),
    "TRAVEL": (
        "MARRIOTT", "HILTON", "HYATT", "SHERATON", "WESTIN", "HOLIDAY INN",
        "HAMPTON INN", "COMFORT INN", "BEST WESTERN", "LA QUINTA",
        "MOTEL 6", "SUPER 8", "DAYS INN", "EMBASSY SUITES", "FAIRFIELD INN",
        "COURTYARD BY MARRIOTT", "RESIDENCE INN", "AIRBNB", "VRBO",
        "BOOKING.COM", "EXPEDIA", "HOTELS.COM", "PRICELINE", "KAYAK",
        "TRAVELOCITY", "ORBITZ", "AGODA", "UNITED AIRLINES", "UNITED AIR",
        "DELTA AIR", "AMERICAN AIRLINES", "SOUTHWEST AIR", "ALASKA AIR",
        "JETBLUE", "SPIRIT AIRLINES", "FRONTIER AIRLINES", "ALLEGIANT",
        "HAWAIIAN AIR", "BRITISH AIRWAYS", "LUFTHANSA", "AIR FRANCE",
        "AIR CANADA", "CARNIVAL CRUISE", "ROYAL CARIBBEAN", "NORWEGIAN CRUISE",
        "PRINCESS CRUISES",
    ),
    "PERSONAL_CARE": (
        "GREAT CLIPS", "SUPERCUTS", "SPORT CLIPS", "FANTASTIC SAMS",
        "BARBER", "SALON", "NAIL SPA", "NAILS ", "MASSAGE ENVY",
        "EUROPEAN WAX", "PLANET FITNESS", "LA FITNESS", "24 HOUR FITNESS",
        "ANYTIME FITNESS", "GOLDS GYM", "GOLD'S GYM", "EQUINOX",
        "ORANGETHEORY", "CRUNCH FITNESS", "YMCA", "CROSSFIT", "PELOTON",
        "CLASSPASS", "SPA ",
    ),
    "GOVERNMENT_AND_NON_PROFIT": (
        "DMV", "DEPT OF MOTOR", "IRS ", "IRS.GOV", "US TREASURY",
        "STATE TAX", "FRANCHISE TAX", "COUNTY TAX", "CITY OF ", "COUNTY OF ",
        "CLERK OF COURT", "MUNICIPAL COURT", "RED CROSS", "SALVATION ARMY",
        "GOODWILL", "UNITED WAY", "ST JUDE", "WOUNDED WARRIOR", "GOFUNDME",
        "DONORBOX", "CHURCH", "MINISTR", "TITHE",
    ),
}


# Plaid-convention tie-breaks over NSI: OSM files big-box stores under
# shop/supermarket (Walmart → groceries), but the aggregator convention
# this product is built on puts them in GENERAL_MERCHANDISE — and the food
# budget seed would silently inflate otherwise. This is taxonomy alignment
# (a dozen entries, stable), not merchant curation.
OVERRIDES: dict[str, str] = {
    "WALMART": "GENERAL_MERCHANDISE",
    "WAL-MART": "GENERAL_MERCHANDISE",
    "TARGET": "GENERAL_MERCHANDISE",
    "COSTCO": "GENERAL_MERCHANDISE",
    "SAM'S CLUB": "GENERAL_MERCHANDISE",
    "SAMS CLUB": "GENERAL_MERCHANDISE",
    "MEIJER": "GENERAL_MERCHANDISE",
    "FRED MEYER": "GENERAL_MERCHANDISE",
    # in-store restaurants (IKEA Restaurant, Nordstrom Cafe) put the parent
    # brand under food in NSI — the card statement says the store
    "IKEA": "HOME_IMPROVEMENT",
    "NORDSTROM": "GENERAL_MERCHANDISE",
    # a local model tends to guess food; bare Amazon charges are retail (the
    # amazon-match pipeline owns real per-order item categories)
    "AMAZON": "GENERAL_MERCHANDISE",
}


def _compile(rules: dict[str, tuple[str, ...]],
             chunk: int = 800) -> list[tuple[re.Pattern, str]]:
    """Chunked alternations per category — one search over thousands of
    names instead of thousands of searches. Longest-first within a chunk
    so 'HOME DEPOT' beats 'HOME'."""
    out = []
    for cat, tokens in rules.items():
        parts = sorted({re.escape(t.strip()) for t in tokens if t.strip()},
                       key=len, reverse=True)
        for i in range(0, len(parts), chunk):
            pat = "|".join(parts[i:i + chunk])
            out.append((re.compile(rf"(?<![A-Z0-9])(?:{pat})(?![A-Z0-9])"),
                        cat))
    return out


_layers: list[list[tuple[re.Pattern, str]]] | None = None


def _compiled_layers() -> list[list[tuple[re.Pattern, str]]]:
    global _layers
    if _layers is None:
        from .merchant_seed_data import NSI_RULES
        over = {}
        for name, cat in OVERRIDES.items():
            over.setdefault(cat, []).append(name)
        _layers = [
            _compile({c: tuple(v) for c, v in over.items()}),
            _compile(NSI_RULES),   # the maintained public dataset — breadth
            _compile(RULES),       # curated fallback: online-only merchants
                                   # (streaming/SaaS) OSM never maps, plus
                                   # US chains NSI tags differently
        ]
    return _layers


def match(merchant: str | None) -> str | None:
    """Category for a merchant string, or None when no rule claims it.
    Layering: processor prefixes → Plaid-convention overrides → NSI
    (generated, see scripts/refresh-merchant-seed.py) → hand fallback."""
    if not merchant:
        return None
    up = merchant.upper()
    for prefix, cat in PREFIXES:
        if up.startswith(prefix):
            return cat
    for layer in _compiled_layers():
        for pat, cat in layer:
            if pat.search(up):
                return cat
    return None


# ---- bank mechanics ----------------------------------------------
# On category-less sources (SimpleFIN, bare CSV) the payment/transfer rows
# arrive looking like merchants — and an LLM will happily file "Chase
# Credit Card" under spend, double-counting every card payment. These
# HIGH-PRECISION patterns route the obvious mechanics to flow categories
# BEFORE any categorizer runs. Deliberately conservative: a false flow
# classification HIDES real spend, which is worse than a category miss —
# so checks and bill-pay (which are usually real spend) are only shielded
# from the LLM, never flow-classified.

_CARD_PAYMENT_RE = re.compile(
    r"\b(CREDIT CARD|CREDIT CRD|CARD PAYMENT|CARDMEMBER SERV\w*|"
    r"PAYMENT THANK YOU|E-?PAYMENT)\b")
_TRANSFER_RE = re.compile(
    r"\b(TRANSFER|XFER|WITHDRAWAL|ATM WITHDRAWAL|CASH WITHDRAWAL)\b")
# strings that are ONLY mechanics (no merchant to categorize) — never sent
# to the LLM, left uncategorized for the user; fullmatch keeps
# "BILL PAY VERIZON" (a real, classifiable biller) out of this list
_SKIP_LLM_RE = re.compile(
    r"^(ONLINE )?(BILL ?PAY(MENT)?|PAYMENT|ACH (DEBIT|CREDIT|PAYMENT))$"
    r"|^CHECK( PAID)?\s*#?\s*\d*$")


def flow_match(merchant: str | None) -> tuple[str, str | None] | None:
    """(category_primary, category_detailed) for bank-mechanics strings —
    Plaid's exact convention, so the spend exclusions treat them like any
    aggregator-marked payment/transfer. None for everything else."""
    if not merchant:
        return None
    up = merchant.upper()
    if _CARD_PAYMENT_RE.search(up):
        return ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    if _TRANSFER_RE.search(up):
        return ("TRANSFER_OUT", None)
    return None


def skip_llm(merchant: str | None) -> bool:
    """Mechanics-only strings with no categorizable merchant behind them."""
    if not merchant:
        return True
    return bool(_SKIP_LLM_RE.match(merchant.upper().strip()))
