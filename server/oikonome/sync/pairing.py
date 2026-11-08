"""One-to-one pairing of incoming rows with rows already held.

Several doors decide which held row an incoming one is: the reconnect
guard (is this backfill row a re-delivery of a restored one, and which),
the merge of a restored account onto its live twin, the settlement of a
retired hold, a waiting receipt meeting its charge. Each is a bipartite
match — every incoming row takes at most one held row and every held row
answers for at most one incoming — and each went wrong the same way when
it was written as a walk over the incoming rows in the order they
arrived, every row taking the best partner still free: the order decided
the answer. A feed that sends tomorrow's coffee before today's re-delivery
lets the new charge take today's restored row, and the re-delivery, its
partner gone, is kept as a second charge on the wrong day. A daily
same-price run then shifts by a day all along its length, across a month
end as easily as across a week.

`pair` makes the decision once, over every candidate edge together:
same-day edges before any off-day one, then off-day edges nearest day
first, each tier ordered by the caller's own preference, and every tie
broken by stable keys of the rows themselves — never by their position in
a list. The same rows in any order therefore pair the same way.
"""
from __future__ import annotations

from typing import Callable, Hashable, Iterable


def pair(edges: Iterable[tuple[Hashable, Hashable, int, tuple]], *,
         in_key: Callable[[Hashable], tuple],
         cand_key: Callable[[Hashable], tuple]) -> dict:
    """{incoming: candidate} for a one-to-one pairing of the edges.

    Each edge is (incoming, candidate, days, rank): `days` is how far
    apart the two rows are dated (0 = the same day, never negative), and
    `rank` is the caller's preference among edges the same distance apart
    (smaller is better — a shared name token before mere absence of a
    contradiction, say). An edge that should never be made is simply not
    offered.

    Edges are taken best first by (days, rank, in_key, cand_key), and an
    edge whose incoming or candidate is already paired is passed over. So
    every same-day pair is made before any off-day one, and a row is never
    beaten to its own day's partner by a neighbour one day off; with
    in_key and cand_key derived from the rows' content (date, id) rather
    than from where they sit in a list, the result does not depend on the
    order either side was delivered in."""
    ordered = sorted(edges, key=lambda e: (e[2], e[3], in_key(e[0]),
                                           cand_key(e[1])))
    out: dict = {}
    taken: set = set()
    for inc, cand, _days, _rank in ordered:
        if inc in out or cand in taken:
            continue
        out[inc] = cand
        taken.add(cand)
    return out


def edge_key(days: int, rank: tuple) -> tuple:
    """The order `pair` prefers edges in, for a caller that needs to ask
    whether one edge beats another (the receipt matcher does)."""
    return (days, rank)
