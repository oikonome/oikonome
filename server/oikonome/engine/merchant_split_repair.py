"""Converge merchants that one payee was split across.

The resolver decides identity from COALESCE(merchant_outlet, merchant_name,
name), so a payee whose rows an aggregator enriched UNEVENLY keys two ways:
the tidy enrichment string on the rows that got one, the raw bank
descriptor on the rows that did not. Two keys are two independent
decisions, so the household ends up with two merchants and two alias rows
for one payee, its history cut in half.

The resolver no longer CREATES that split — an unenriched group now adopts
the merchant its descriptor's enriched siblings were given. It cannot heal
one that already exists, and the reasons are structural rather than
oversights: descriptor adoption runs only where nothing has spoken for the
string, and a split household HAS an alias row for the raw key; and a row
that already carries a merchant_id is never revisited. Healing therefore
means retraction — repoint the alias, move the rows, retire the twin —
which is precisely what a person's rename does.

So this sweep merges through `merchant_dedup.rename`. Every merge it makes
is journalled to `merchant_renames` and reversible with
`merchant_dedup.undo`, exactly like a rename someone typed; nothing here
writes merchant state by a private route. That has a consequence worth
naming: a merge lands as method='manual', which is the weight a person's
answer carries, so an undone merge stays undone — the sweep will not
re-make a merge a person reversed.

A wrong merge is worse than a missed one. Two payees fused into one cannot
be told apart again from the data, while a split payee is visible,
annoying, and still repairable next run. Every rule below is therefore a
reason to REFUSE, and the sweep refuses a whole descriptor rather than
guess at part of it.
"""
from __future__ import annotations

import logging

from . import merchant_dedup, merchant_identity

log = logging.getLogger(__name__)

# The app builds its own display name for a counterparty rail ("Zelle —
# Casey Example"); the resolver keeps those separate on purpose, and the
# dash is the marker.
_COUNTERPARTY_MARK = " — "

# How authoritative a merchant's name is. A Plaid-named merchant carries an
# entity id and a logo the others cannot get; an LLM merge was reviewed; a
# layer-1 name is a string clean and nothing more. 'manual' is absent
# because a person's answer is never a candidate at all.
_AUTHORITY = {"plaid": 3, "llm": 2, "layer1": 1}

# A descriptor pointing at more merchants than a split can plausibly make
# is not describing one payee. Uneven enrichment forks a payee two ways,
# three at the outside (an enrichment string, the raw descriptor, and a
# variant of it from a re-clean); beyond that the likelier reading is a
# bank constant that many payees share, and those must never collapse.
_MAX_WAYS = 3

# What one nightly run may do. The scan is one aggregate over the live
# ledger; the merges are not — each rewrites every row of a merchant and
# re-resolves them, so an unbounded first run on a large ledger would hold
# the household's identity lock for minutes inside the nightly. Splits are
# a finite backlog, so a cap converges the household over a few nights and
# costs nothing once it has.
MAX_MERGES = 25
# Descriptor groups examined per run. Only genuinely split descriptors
# reach this, so the honest number is small; the cap exists so a household
# in a pathological state cannot make the scan itself the problem.
SCAN_LIMIT = 1000


def _abbrev_distance(descriptor: str, name: str) -> int:
    """How many words of `name` the descriptor only ABBREVIATES.

    Zero means the descriptor spells the whole name out. Used to break a
    tie between two names of equal authority: the one the descriptor
    matches word for word is the one it names most tightly."""
    dwords = merchant_identity._words(descriptor)
    loose = 0
    for m in merchant_identity._words(name):
        if m in dwords:
            continue
        loose += 1
    return loose


def _survivor_key(descriptor: str, cand: dict) -> tuple:
    """Sort key that picks the merchant a split should converge ON.

    By AUTHORITY first, never by row count: the tidy Plaid-named merchant
    is routinely the one with fewer rows (it appears only where enrichment
    reached), and choosing by weight of history would throw away the entity
    id and the logo that make it the better name. Plaid outranks a reviewed
    LLM merge, which outranks the layer-1 string clean. Ties break on the
    name the descriptor names most tightly — spelled out beats abbreviated
    — then on the shorter name (a location tail is noise, not identity),
    then on the name and id so a run is reproducible."""
    return (-_AUTHORITY.get(cand["name_source"], 0),
            _abbrev_distance(descriptor, cand["name"]),
            len(cand["name"]),
            cand["name"].lower(),
            str(cand["id"]))


def _related(a: str, b: str) -> bool:
    """Are these two merchant names two spellings of one payee?

    One name has to be inside the other, by the same test that asks whether
    a descriptor names a payee: "Example Telecom" and "Example Telecom New
    York Usa" are one payee written twice, while "Example Telecom" and
    "Corner Market" are two payees that merely appear under one string. The
    second shape is exactly what a bank constant produces, and it is the
    shape that must never merge."""
    return (merchant_identity._descriptor_names(a, b)
            or merchant_identity._descriptor_names(b, a))


def _decide(descriptor: str, cands: list[dict]) -> tuple[dict | None, list[dict], str]:
    """Which of the merchants under one descriptor should merge, if any.

    Returns (survivor, losers, reason). A survivor of None means refused,
    and `reason` says why — the dry run prints it, so a refusal is
    inspectable rather than silent."""
    # A person's answer outranks this sweep in both directions: never merge
    # a hand-named merchant away, and never merge anything INTO one on the
    # sweep's own initiative — that would be the sweep enlarging a decision
    # someone made about a different set of rows.
    # A hand-named merchant under the descriptor means a person has already
    # said what rows under it are; merging the others around that answer
    # would be the sweep second-guessing it, so the whole group is left.
    if any(c["name_source"] == "manual" for c in cands):
        return None, [], "hand-named"
    if len(cands) < 2:
        return None, [], "nothing-to-merge"
    if len(cands) > _MAX_WAYS:
        return None, [], "descriptor-names-many-merchants"
    for c in cands:
        # An outlet and its brand are deliberately two merchants — the fuel
        # arm has its own prices and its own category — and they share the
        # brand's descriptor by design. A merchant with a parent is one half
        # of such a pair whether or not the other half is in this group.
        if c["parent_id"] is not None:
            return None, [], "outlet-of-a-brand"
        # A rail's counterparty name IS the identity, and two people paid
        # over one rail share a descriptor without being one payee.
        if _COUNTERPARTY_MARK in c["name"] or c["counterparty_key"]:
            return None, [], "counterparty-name"
    # Two merchants of the same name are not a descriptor split, and a
    # rename could not tell them apart anyway — it moves rows by the name
    # they display.
    if len({c["name"].lower() for c in cands}) != len(cands):
        return None, [], "duplicate-names"
    # The descriptor must NAME every merchant it points at. A descriptor
    # that names none of them ("POS DEBIT PURCHASE", a bare processor
    # prefix) is a bank's constant, not a payee, and the rows under it are
    # unrelated purchases. One it names only in part is no better: the
    # merchant it does not name is evidence the string is shared.
    for c in cands:
        if not merchant_identity._descriptor_names(descriptor, c["name"]):
            return None, [], "descriptor-does-not-name-them-all"
    survivor = min(cands, key=lambda c: _survivor_key(descriptor, c))
    losers = [c for c in cands if c["id"] != survivor["id"]]
    # and the names have to be two spellings of one payee, not two payees
    # that happen to sit under a string naming both
    for lo in losers:
        if not _related(lo["name"], survivor["name"]):
            return None, [], "names-unrelated"
    return survivor, losers, "split"


def _candidates(conn, scan_limit: int) -> list[dict]:
    """Descriptors whose live rows resolve to more than one merchant.

    Grouped on the bank's own descriptor because that is the one string
    both halves of a split share — the decision key is exactly the string
    that differs. Removed rows are ignored: they are not displayed, and a
    retired lineage would otherwise keep proposing a merge forever."""
    rows = conn.execute(
        """WITH split AS (
               SELECT t.name AS descriptor
                 FROM transactions t
                WHERE t.removed = 0 AND t.merchant_id IS NOT NULL
                  AND coalesce(t.name, '') <> ''
                GROUP BY t.name
               HAVING count(DISTINCT t.merchant_id) > 1
                ORDER BY t.name
                LIMIT %s)
           SELECT t.name AS descriptor, t.merchant_id AS mid,
                  count(*) AS rows,
                  bool_or(COALESCE(t.merchant_outlet, t.merchant_name,
                                   t.name) LIKE %s) AS counterparty_key
             FROM transactions t
             JOIN split s ON s.descriptor = t.name
            WHERE t.removed = 0 AND t.merchant_id IS NOT NULL
            GROUP BY t.name, t.merchant_id
            ORDER BY t.name""",
        (scan_limit, f"%{_COUNTERPARTY_MARK}%")).fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["descriptor"], []).append(r)
    # A merged-away merchant still named by a row is not a second payee, so
    # compare the SURVIVORS. This is also what makes the sweep idempotent:
    # once a merge has moved the rows, the descriptor names one survivor
    # and stops being a candidate.
    surv: dict = {}
    out = []
    for descriptor in sorted(groups):
        merged: dict = {}
        for r in groups[descriptor]:
            mid = r["mid"]
            if mid not in surv:
                surv[mid] = merchant_identity._survivor(conn, mid)
            live = surv[mid]
            if live is None:
                continue
            cur = merged.setdefault(live, {"id": live, "rows": 0,
                                           "counterparty_key": False})
            cur["rows"] += r["rows"]
            cur["counterparty_key"] |= bool(r["counterparty_key"])
        if len(merged) < 2:
            continue
        facts = {r["id"]: r for r in conn.execute(
            "SELECT id, name, name_source, plaid_entity_id, parent_id "
            "  FROM merchants WHERE id = ANY(%s)",
            (list(merged),)).fetchall()}
        cands = []
        for mid, c in merged.items():
            f = facts.get(mid)
            if f is None:
                continue
            cands.append({**c, "name": f["name"],
                          "name_source": f["name_source"],
                          "plaid_entity_id": f["plaid_entity_id"],
                          "parent_id": f["parent_id"]})
        if len(cands) > 1:
            out.append({"descriptor": descriptor, "merchants": cands})
    return out


def _descriptors_of(conn, mid) -> set[str]:
    """Every bank descriptor with a live row on this merchant — rows still
    pointing at a merchant that was merged into it included, since the
    rename moves those too."""
    return {r["name"] for r in conn.execute(
        """WITH RECURSIVE chain AS (
               SELECT id FROM merchants WHERE id = %s
               UNION
               SELECT m.id FROM merchants m JOIN chain c ON m.merged_into = c.id)
           SELECT DISTINCT t.name FROM transactions t
             JOIN chain c ON c.id = t.merchant_id
            WHERE t.removed = 0 AND coalesce(t.name, '') <> ''""",
        (mid,)).fetchall()}


def plan(conn, *, scan_limit: int = SCAN_LIMIT, limit: int = MAX_MERGES) -> dict:
    """What the sweep WOULD merge, changing nothing.

    Returns {"merges": [...], "refused": [...], "descriptors": n} where each
    merge names the descriptor, the survivor, and the merchants that would
    fold into it. Refusals carry their reason so an operator can see which
    rule stopped a merge rather than wondering why nothing happened."""
    merges, refused = [], []
    deferred = 0
    groups = _candidates(conn, scan_limit)
    for g in groups:
        survivor, losers, reason = _decide(g["descriptor"], g["merchants"])
        if survivor is not None:
            # The evidence is one descriptor; the rename moves EVERY row the
            # loser displays. A loser that also has rows under a descriptor
            # the survivor never appears under is a payee in its own right
            # (a membership beside the store it belongs to), and folding
            # it would carry those rows along on no evidence at all.
            covered = _descriptors_of(conn, survivor["id"])
            for lo in losers:
                if _descriptors_of(conn, lo["id"]) - covered:
                    survivor, losers = None, []
                    reason = "loser-rows-under-other-descriptors"
                    break
        if survivor is None:
            refused.append({"descriptor": g["descriptor"], "reason": reason,
                            "merchants": sorted(c["name"]
                                                for c in g["merchants"])})
            continue
        if len(merges) >= limit:
            # over the per-run cap: left for the next run, which finds it
            # in exactly the same state
            deferred += 1
            continue
        merges.append({
            "descriptor": g["descriptor"],
            "survivor": survivor["name"],
            "survivor_source": survivor["name_source"],
            "merge": [lo["name"] for lo in losers],
            "rows": sum(lo["rows"] for lo in losers),
            "_survivor": survivor, "_losers": losers})
    return {"merges": merges, "refused": refused, "descriptors": len(groups),
            "deferred": deferred}


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def repair(conn, *, apply: bool = False, scan_limit: int = SCAN_LIMIT,
           limit: int = MAX_MERGES) -> dict:
    """Find split payees and, with apply=True, converge them.

    Dry run by default — the operator door and the nightly both read the
    same plan, so what the nightly does is what a dry run printed.

    The household identity lock is taken FIRST, before the per-name locks
    the merges take underneath, because that is the order this module's
    neighbours use and the only order in which the two kinds of lock cannot
    form a cycle."""
    with conn.transaction():
        merchant_identity._lock_identity(conn)
        p = plan(conn, scan_limit=scan_limit, limit=limit)
        done = []
        if apply:
            for rec in p["merges"]:
                survivor = rec["_survivor"]
                changes: list[int] = []
                rows = 0
                for lo in rec["_losers"]:
                    before = conn.execute(
                        "SELECT coalesce(max(id), 0) AS n "
                        "  FROM merchant_renames").fetchone()["n"]
                    moved = merchant_dedup.rename(conn, lo["name"],
                                                  survivor["name"])
                    rows += moved["rows"]
                    changes += [r["id"] for r in conn.execute(
                        "SELECT id FROM merchant_renames WHERE id > %s "
                        " ORDER BY id", (before,)).fetchall()]
                done.append({**_public(rec), "rows": rows,
                             "change_ids": changes})
                log.info("merchant split repaired: %s <- %s (%d rows)",
                         rec["survivor"], ", ".join(rec["merge"]), rows)
    return {"applied": bool(apply),
            "merges": done if apply else [_public(r) for r in p["merges"]],
            "refused": p["refused"],
            "descriptors": p["descriptors"],
            "deferred": p["deferred"]}
