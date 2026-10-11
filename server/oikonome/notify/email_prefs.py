"""Who in the household receives which kind of email.

The recipient list says who is on the household's mail and the mute list
says who asked for none of it. This is the finer switch between the two:
per address, per kind — the daily verdict, each report cadence, and the
alert mail (bank connections, savings goals, collector alerts).

Stored in tenant config under `email_prefs` as the addresses that turned
a kind OFF, by kind: `{"monthly": ["partner@example.com"]}`. Stored that
way round so that absence means what it always meant — everyone on the
list receives everything — and a missing or unreadable value can only
ever fall back to that, never to silence. The keys are fixed kind names,
so nothing that walks config by key name (the export scrub) can mistake
an address for something else and drop it.

A mute still wins: a muted address receives nothing, whatever this says.
The settings door and the worker both read the kinds from here.
"""

from __future__ import annotations

KEY = "email_prefs"
# the four scheduled cadences, then everything sent when something happens
# (a stored "weekly" list from an older version is simply never read)
KINDS = ("daily", "monthly", "yearly", "alerts")

# The note a scheduled cadence's heartbeat carries when every address that
# would have been mailed turned that kind off: the day's decision was made
# (nobody wants it), not work still pending, and Doctor says so instead of
# blaming the mail relay.
NOBODY_OPTED_IN = "nobody opted in"


def _low(address) -> str:
    return str(address or "").strip().lower()


def off_sets(cfg: dict) -> dict[str, set[str]]:
    """Each kind's opted-out addresses, lowercased. Any stored shape is
    tolerated (a restored or hand-edited config): what cannot be read is
    read as nobody opted out."""
    raw = cfg.get(KEY)
    out: dict[str, set[str]] = {k: set() for k in KINDS}
    if not isinstance(raw, dict):
        return out
    for kind in KINDS:
        v = raw.get(kind)
        if isinstance(v, list):
            out[kind] = {_low(a) for a in v if isinstance(a, str) and a.strip()}
    return out


def allows(cfg: dict, address, kind: str) -> bool:
    return _low(address) not in off_sets(cfg).get(kind, set())


def keep(cfg: dict, addresses: list[str], kind: str | None) -> list[str]:
    """`addresses` minus those that turned `kind` off. No kind = no
    filter, for the sends that are not one of these kinds."""
    if kind is None:
        return list(addresses)
    off = off_sets(cfg).get(kind, set())
    return [a for a in addresses if _low(a) not in off]


def row(cfg: dict, address) -> dict[str, bool]:
    sets = off_sets(cfg)
    a = _low(address)
    return {k: a not in sets[k] for k in KINDS}


def _store(cfg: dict, sets: dict[str, set[str]]) -> None:
    clean = {k: sorted(v) for k, v in sets.items() if v}
    if clean:
        cfg[KEY] = clean
    else:
        cfg.pop(KEY, None)


def set_pref(cfg: dict, address, kind: str, on: bool, cap: int) -> None:
    """Turn one kind on or off for one address, on the stored document
    (the caller holds the config lock). `cap` bounds each kind's list the
    way the mute list is bounded."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    a = _low(address)
    sets = off_sets(cfg)
    if on:
        sets[kind].discard(a)
    else:
        sets[kind].add(a)
        if len(sets[kind]) > cap:
            raise OverflowError(f"the limit is {cap} addresses")
    _store(cfg, sets)


def without(cfg: dict, address) -> None:
    """Forget an address everywhere — the person left the household."""
    a = _low(address)
    sets = off_sets(cfg)
    if any(a in v for v in sets.values()):
        for v in sets.values():
            v.discard(a)
        _store(cfg, sets)


def move(cfg: dict, old, new) -> None:
    """A person's address changed: their choices are theirs and follow
    them, like their mute does."""
    o, n = _low(old), _low(new)
    if not o or o == n:
        return
    sets = off_sets(cfg)
    if not any(o in v for v in sets.values()):
        return
    for v in sets.values():
        if o in v:
            v.discard(o)
            v.add(n)
    _store(cfg, sets)
