#!/usr/bin/env python3
"""Regenerate server/oikonome/engine/merchant_seed_data.py from the
Name Suggestion Index.

NSI (github.com/osmlab/name-suggestion-index, BSD 3-Clause) is the
OpenStreetMap community's canonical brand database — thousands of chains
with machine-readable categories, actively maintained. This script maps
its OSM tag families onto Oikonome's spend categories and emits a
generated python table, so the seed data needs zero manual curation:
re-run this script to pick up their latest release.

Usage:
 python3 scripts/refresh-merchant-seed.py [path/to/nsi.min.json [version]]

Without an argument it downloads the latest release from the npm registry
(the repo's dist/ is only published there and on their site). Given a local
file, pass the release string as a second argument to reproduce a specific
generated file byte for byte.
"""
from __future__ import annotations

import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] \
    / "server/oikonome/engine/merchant_seed_data.py"

# NSI tag-family → Oikonome category, in PRIORITY order: the first family
# that claims a brand name wins (fuel before convenience puts Circle K in
# TRANSPORTATION, not FOOD_AND_DRINK). Families not listed and not covered
# by the shop/* default are skipped.
#
# Deliberately EXCLUDED: banks/ATMs/money transfer (those merchant strings
# ride transfers and loan payments — categorizing them as spend would fight
# the flow-guard), car dealers (loan-sized flows), schools/government
# operators, and physical infrastructure (power poles, pipelines...).
FAMILY_MAP: list[tuple[str, str]] = [
    ("brands/amenity/fuel", "TRANSPORTATION"),
    ("brands/amenity/charging_station", "TRANSPORTATION"),
    ("brands/amenity/car_rental", "TRANSPORTATION"),
    ("brands/amenity/car_sharing", "TRANSPORTATION"),
    ("brands/amenity/taxi", "TRANSPORTATION"),
    ("brands/amenity/parking", "TRANSPORTATION"),

    ("brands/amenity/fast_food", "FOOD_AND_DRINK"),
    ("brands/amenity/restaurant", "FOOD_AND_DRINK"),
    ("brands/amenity/cafe", "FOOD_AND_DRINK"),
    ("brands/amenity/bar", "FOOD_AND_DRINK"),
    ("brands/amenity/pub", "FOOD_AND_DRINK"),
    ("brands/amenity/ice_cream", "FOOD_AND_DRINK"),
    ("brands/amenity/food_court", "FOOD_AND_DRINK"),
    ("brands/shop/supermarket", "FOOD_AND_DRINK"),
    ("brands/shop/convenience", "FOOD_AND_DRINK"),
    ("brands/shop/greengrocer", "FOOD_AND_DRINK"),
    ("brands/shop/bakery", "FOOD_AND_DRINK"),
    ("brands/shop/butcher", "FOOD_AND_DRINK"),
    ("brands/shop/deli", "FOOD_AND_DRINK"),
    ("brands/shop/alcohol", "FOOD_AND_DRINK"),
    ("brands/shop/beverages", "FOOD_AND_DRINK"),
    ("brands/shop/coffee", "FOOD_AND_DRINK"),
    ("brands/shop/confectionery", "FOOD_AND_DRINK"),

    ("brands/amenity/pharmacy", "MEDICAL"),
    ("brands/shop/chemist", "MEDICAL"),
    ("brands/amenity/dentist", "MEDICAL"),
    ("brands/amenity/doctors", "MEDICAL"),
    ("brands/amenity/clinic", "MEDICAL"),
    ("brands/amenity/hospital", "MEDICAL"),
    ("brands/amenity/veterinary", "MEDICAL"),
    ("brands/shop/optician", "MEDICAL"),
    ("brands/shop/hearing_aids", "MEDICAL"),
    ("brands/healthcare/counselling", "MEDICAL"),
    ("brands/healthcare/physiotherapist", "MEDICAL"),
    ("brands/healthcare/laboratory", "MEDICAL"),

    ("brands/shop/doityourself", "HOME_IMPROVEMENT"),
    ("brands/shop/hardware", "HOME_IMPROVEMENT"),
    ("brands/shop/furniture", "HOME_IMPROVEMENT"),
    ("brands/shop/garden_centre", "HOME_IMPROVEMENT"),
    ("brands/shop/paint", "HOME_IMPROVEMENT"),
    ("brands/shop/flooring", "HOME_IMPROVEMENT"),
    ("brands/shop/bed", "HOME_IMPROVEMENT"),
    ("brands/shop/interior_decoration", "HOME_IMPROVEMENT"),
    ("brands/shop/houseware", "HOME_IMPROVEMENT"),
    ("brands/shop/bathroom_furnishing", "HOME_IMPROVEMENT"),
    ("brands/shop/kitchen", "HOME_IMPROVEMENT"),
    ("brands/shop/storage_rental", "HOME_IMPROVEMENT"),
    ("brands/shop/trade", "HOME_IMPROVEMENT"),

    ("brands/shop/car_repair", "GENERAL_SERVICES"),
    ("brands/shop/car_parts", "GENERAL_SERVICES"),
    ("brands/shop/tyres", "GENERAL_SERVICES"),
    ("brands/office/insurance", "GENERAL_SERVICES"),
    ("brands/shop/laundry", "GENERAL_SERVICES"),
    ("brands/shop/dry_cleaning", "GENERAL_SERVICES"),
    ("brands/shop/copyshop", "GENERAL_SERVICES"),
    ("brands/amenity/post_office", "GENERAL_SERVICES"),
    ("brands/shop/tailor", "GENERAL_SERVICES"),
    ("brands/amenity/driving_school", "GENERAL_SERVICES"),
    ("brands/craft/plumber", "GENERAL_SERVICES"),
    ("brands/craft/electrician", "GENERAL_SERVICES"),
    ("brands/craft/hvac", "GENERAL_SERVICES"),
    ("brands/shop/funeral_directors", "GENERAL_SERVICES"),
    ("brands/shop/pest_control", "GENERAL_SERVICES"),

    ("brands/shop/hairdresser", "PERSONAL_CARE"),
    ("brands/shop/beauty", "PERSONAL_CARE"),
    ("brands/shop/massage", "PERSONAL_CARE"),
    ("brands/shop/nails", "PERSONAL_CARE"),
    ("brands/shop/tattoo", "PERSONAL_CARE"),
    ("brands/leisure/fitness_centre", "PERSONAL_CARE"),
    ("brands/leisure/sports_centre", "PERSONAL_CARE"),

    ("brands/amenity/cinema", "ENTERTAINMENT"),
    ("brands/amenity/theatre", "ENTERTAINMENT"),
    ("brands/amenity/nightclub", "ENTERTAINMENT"),
    ("brands/amenity/amusement_arcade", "ENTERTAINMENT"),
    ("brands/leisure/bowling_alley", "ENTERTAINMENT"),
    ("brands/leisure/trampoline_park", "ENTERTAINMENT"),
    ("brands/leisure/water_park", "ENTERTAINMENT"),
    ("brands/leisure/escape_game", "ENTERTAINMENT"),
    ("brands/leisure/miniature_golf", "ENTERTAINMENT"),
    ("brands/leisure/amusement_ride", "ENTERTAINMENT"),
    ("brands/tourism/theme_park", "ENTERTAINMENT"),
    ("brands/tourism/zoo", "ENTERTAINMENT"),
    ("brands/tourism/aquarium", "ENTERTAINMENT"),
    ("brands/tourism/museum", "ENTERTAINMENT"),

    ("brands/tourism/hotel", "TRAVEL"),
    ("brands/tourism/motel", "TRAVEL"),
    ("brands/tourism/hostel", "TRAVEL"),
    ("brands/tourism/guest_house", "TRAVEL"),
    ("brands/tourism/camp_site", "TRAVEL"),
    ("brands/shop/travel_agency", "TRAVEL"),

    ("brands/shop/mobile_phone", "RENT_AND_UTILITIES"),
    ("brands/office/telecommunication", "RENT_AND_UTILITIES"),
]
# any remaining brands/shop/* family → retail
SHOP_DEFAULT = "GENERAL_MERCHANDISE"
SKIP_FAMILIES = {
    "brands/amenity/bank", "brands/amenity/atm",
    "brands/amenity/money_transfer", "brands/amenity/bureau_de_change",
    "brands/shop/money_lender", "brands/shop/pawnbroker",
    "brands/shop/car",                     # dealers: loan-sized flows
    "brands/amenity/vending_machine", "brands/advertising/totem",
}
# brands active in these locations (or worldwide "001") are kept
KEEP_LOCATIONS = {"us", "ca", "001"}

MIN_LEN = 4          # "76" or "BP" would word-match everywhere
GENERIC = {           # single words far too common in merchant strings
    "MARKET", "EXPRESS", "PLUS", "CITY", "SHOP", "STORE", "FRESH", "FOOD",
    "FOODS", "CAFE", "PIZZA", "SUSHI", "GRILL", "DINER", "HOTEL", "MOTEL",
    "SALON", "SPORT", "SPORTS", "AUTO", "GAS", "FUEL", "BANK", "PARK",
    "CENTER", "CENTRE", "HOUSE", "HOME", "SUPER", "MEGA", "MINI", "LOCAL",
    "FAMILY", "GOLD", "STAR", "SUPERMARKET", "PHARMACY", "COFFEE", "TACO",
    "BURGER", "CHICKEN", "DONUTS", "BAKERY", "LIQUOR", "WINE", "SMOKE",
    "MOBILE", "PHONE", "DENTAL", "CLINIC", "OPTICAL", "TIRE", "TIRES",
    "STORAGE", "CLEANERS", "NAILS", "FITNESS", "CINEMA", "TRAVEL", "TOURS",
}
# Brand names whose bare form is too greedy: the word alone also starts
# unrelated local business names, which would then be filed under the
# national brand's category. Longer forms in EXTRA still catch the brand.
# These belong here, not in the generated file — a hand edit to generated
# output is silently reverted by the next refresh.
DROP = {
    "ROGERS",           # bare form also begins unrelated local businesses
}
# Statement spellings the dataset does not carry. Applied after the tag
# families, so a name dropped above can be restored in an unambiguous form.
EXTRA: dict[str, str] = {
    "ROGERS WIRELESS": "RENT_AND_UTILITIES",
}


def load(path: str | None) -> dict:
    if path:
        return json.load(open(path))
    meta = json.load(urllib.request.urlopen(
        "https://registry.npmjs.org/name-suggestion-index/latest"))
    print(f"downloading NSI {meta['version']} from npm…")
    blob = urllib.request.urlopen(meta["dist"]["tarball"]).read()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        f = tf.extractfile("package/dist/json/nsi.min.json")
        assert f is not None
        data = json.load(f)
    data["_version"] = meta["version"]
    return data


def wanted_here(item: dict) -> bool:
    inc = (item.get("locationSet") or {}).get("include") or ["001"]
    return any(str(x).lower() in KEEP_LOCATIONS
               for x in inc if isinstance(x, str))


def names_of(item: dict) -> set[str]:
    tags = item.get("tags") or {}
    raw = {item.get("displayName"), tags.get("brand"), tags.get("name"),
           *(item.get("matchNames") or [])}
    out = set()
    for n in raw:
        if not n:
            continue
        up = n.upper().strip()
        if (len(up) < MIN_LEN or up in GENERIC or up in DROP
                or not re.search(r"[A-Z].*[A-Z]", up)):
            continue
        out.add(up)
    return out


def main() -> None:
    data = load(sys.argv[1] if len(sys.argv) > 1 else None)
    nsi = data["nsi"]
    version = data.get("_version") or (sys.argv[2] if len(sys.argv) > 2
                                       else "?")
    fam_order = dict(FAMILY_MAP)
    claimed: dict[str, str] = {}

    def take(family: str, cat: str) -> None:
        tree = nsi.get(family)
        if not tree:
            return
        for item in tree["items"]:
            if not wanted_here(item):
                continue
            for name in names_of(item):
                claimed.setdefault(name, cat)   # priority order wins

    for family, cat in FAMILY_MAP:
        take(family, cat)
    for family in nsi:
        if (family.startswith("brands/shop/") and family not in fam_order
                and family not in SKIP_FAMILIES):
            take(family, SHOP_DEFAULT)
    for name, cat in EXTRA.items():
        claimed.setdefault(name, cat)

    by_cat: dict[str, list[str]] = {}
    for name, cat in claimed.items():
        by_cat.setdefault(cat, []).append(name)

    with open(OUT, "w") as f:
        f.write('"""GENERATED by scripts/refresh-merchant-seed.py — do not '
                'edit by hand.\n\nBrand → category data derived from the '
                'Name Suggestion Index\n(github.com/osmlab/name-suggestion-'
                'index, BSD 3-Clause), filtered to\nUS/CA/worldwide brands. '
                f'Source release: {version}.\n"""\n\n'
                f'NSI_VERSION = "{version}"\n\nNSI_RULES = {{\n')
        for cat in sorted(by_cat):
            f.write(f'    "{cat}": (\n')
            for name in sorted(by_cat[cat]):
                f.write(f"        {name!r},\n")
            f.write("    ),\n")
        f.write("}\n")
    total = sum(len(v) for v in by_cat.values())
    print(f"wrote {OUT.name}: {total} names in {len(by_cat)} categories "
          f"(NSI {version})")
    for cat in sorted(by_cat):
        print(f"  {len(by_cat[cat]):5}  {cat}")


if __name__ == "__main__":
    main()
