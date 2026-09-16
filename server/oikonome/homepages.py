"""The closed vocabulary of pages a client may open on.

A household picks where the web app and the phone app open (Settings ->
Home page). The clients redirect there blind at launch, so an arbitrary
route would strand the app on a 404 every open — the set is closed, and
every door that writes the setting checks against THIS list: the Settings
save, and the restore of an export ZIP (which writes the config row
wholesale and could otherwise plant a route the client cannot serve).

The clients carry their own copies (webapp/src/api/client.ts WEB_HOMES /
MOBILE_HOMES, mobile/src/app/settings.tsx MOBILE_HOMES and the launch
redirect in mobile/src/app/(tabs)/index.tsx); a source test holds all of
them equal to these.
"""

from __future__ import annotations

HOME_WEB: tuple[str, ...] = (
    "/", "/transactions", "/bills", "/accounts", "/networth", "/budget",
    "/cashflow", "/reimburse", "/business")
HOME_MOBILE: tuple[str, ...] = (
    "index", "transactions", "bills", "accounts", "more")

# the value that means "absent = Today" per client
DEFAULTS = {"home_web": "/", "home_mobile": "index"}
VOCAB = {"home_web": HOME_WEB, "home_mobile": HOME_MOBILE}


def scrub(cfg: dict) -> list[str]:
    """Drop a home-page key whose value the clients cannot open. Mutates
    `cfg`; returns a note per key dropped (empty when all survived)."""
    notes: list[str] = []
    for key, allowed in VOCAB.items():
        v = cfg.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or v not in allowed:
            cfg.pop(key, None)
            notes.append(f"the {key} setting was dropped — not a page "
                         f"this app can open on")
        elif v == DEFAULTS[key]:
            cfg.pop(key, None)                  # absent = Today
    return notes
