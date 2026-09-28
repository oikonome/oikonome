"""The "today" tick in the email draws as one straight line.

Mail clients have no absolute positioning, so the web's overlay tick
becomes three stacked tables: a stub above the bar, a 2px cell inside it,
a stub below. That only reads as a line if all three put the cell in the
same column — and with `table-layout:fixed` the percentage columns divide
whatever is left after the FIXED ones, so a stub of [pace%][2px][rest] and
a bar carrying BOTH a budget divider and the tick are measuring their
percentages against different widths, which renders the marker with a
one-pixel kink in it on every row that is over budget.

The fix is structural: the stubs render the bar's own cell list,
colourless except the tick, so the columns are identical by construction.
This test states that invariant the same way — the widths of a stub's
columns must equal the bar's, cell for cell — because it is the property
that makes the line straight, and comparing rendered pixels would be a
much worse test of the same thing.
"""

import datetime as dt
import re
import unittest

from oikonome.web import report

from .util import TODAY, add_bill, add_txn, make_db, write_config

# one <td> → (height px, width as written). Both are read off the inlined
# style attribute, which is what the mail client actually lays out.
CELL = re.compile(r"<td style=\"([^\"]*)\"")


def _tables(html: str) -> list[tuple[int, list[str]]]:
    """Every fixed-height table row in the mail, as (height, [widths…]).

    Split per <table> rather than by scanning cells, because two stubs
    that happen to be adjacent — one row's lower stub and the next row's
    upper one — are the same height and would otherwise read as one row.
    """
    out: list[tuple[int, list[str]]] = []
    for chunk in html.split("<table")[1:]:
        chunk = chunk.split("</table")[0]
        heights, widths = set(), []
        for m in CELL.finditer(chunk):
            style = m.group(1)
            h = re.search(r"height:(\d+)px", style)
            if not h:
                continue
            w = re.search(r"width:([\d.]+(?:%|px))", style)
            heights.add(int(h.group(1)))
            widths.append(w.group(1) if w else "auto")
        if len(heights) == 1 and widths:
            out.append((heights.pop(), widths))
    return out


class PaceTickAlignmentTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        # DETAIL face: the money map with its per-category bars is what
        # carries the tick, and only that face renders it
        write_config(self.conn, today_view="detail",
                     food_monthly=400, other_monthly=400)
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        # over budget in one category and under in the other, so the map
        # renders a bar WITH a budget divider beside a bar without one —
        # the divider is the second fixed-width cell that broke the stubs
        add_txn(self.conn, TODAY, 900.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 60.0, "TARGET")
        self.d = report.gather(self.conn, TODAY)
        _, _, self.html = report.build(self.d)

    def tearDown(self):
        self.conn.close()

    def _trios(self, bar_h: int):
        runs = _tables(self.html)
        return [(a, b, c) for a, b, c in zip(runs, runs[1:], runs[2:])
                if (a[0], b[0], c[0]) == (3, bar_h, 3)]

    def test_the_money_map_stubs_use_the_bars_own_columns(self):
        trios = self._trios(14)
        self.assertTrue(trios, "no money-map bar rendered with its stubs")
        for above, bar, below in trios:
            self.assertEqual(above[1], bar[1],
                             "the stub above the bar has different columns "
                             "from the bar — the tick will not line up")
            self.assertEqual(below[1], bar[1],
                             "the stub below the bar has different columns "
                             "from the bar — the tick will not line up")

    def test_the_case_that_broke_is_actually_rendered(self):
        """A bar with a budget divider AND a tick — two fixed-width cells.
        Without one in the fixture the test above would pass on a template
        that still gets the hard case wrong."""
        divided = [bar for _, bar, _ in self._trios(14)
                   if bar[1].count("2px") >= 2]
        self.assertTrue(divided,
                        "fixture no longer renders an over-budget row; the "
                        "alignment case this test exists for is untested")

    def test_the_tile_meters_are_in_the_same_set(self):
        """The hero tiles' meters build their columns differently — a
        fill, then the gap to today, then the tick — and stack the same
        three tables at the same height, so they are covered by the test
        above only while the fixture actually renders one. A bar with a
        single 2px cell is one of them; the over-budget rows have two."""
        plain = [bar for _, bar, _ in self._trios(14)
                 if bar[1].count("2px") == 1]
        self.assertTrue(plain,
                        "no tick-only bar rendered — the tile meters' own "
                        "column shape is going untested")


if __name__ == "__main__":
    unittest.main()
