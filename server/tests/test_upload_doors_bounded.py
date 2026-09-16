"""Upload doors must cost the server a bounded amount of work.

Two invariants, both about a shared instance where one tenant's upload
must never take the process down for everyone else:

1. The bulk-import sniff reads a FIXED head of a file. It looks at the
   first rows' numbers to guess the amount-sign convention, so its cost
   must not grow with the size of the upload — parsing a whole 50 MB CSV
   into Python objects costs gigabytes and the container's memory limit
   kills the process before Python raises anything.
2. Every route that accepts a file upload carries a rate limit. Without
   one, an authenticated caller can repeat the most expensive request in
   the product as fast as it completes.
"""

import inspect
import tracemalloc
import unittest

from oikonome.web import bulk_import
from oikonome.web.app import app

from .util import mutating_routes, route_key


def _rate_limited(route) -> bool:
    """True when the route carries the per-IP sliding-window dependency.

    `security.limit(...)` returns a closure named `dep`, so the qualified
    name is what identifies it wherever it was attached.
    """
    seen = []

    def walk(dependant):
        for d in dependant.dependencies:
            seen.append(getattr(d.call, "__qualname__", ""))
            walk(d)

    walk(route.dependant)
    return any(n.startswith("limit.") for n in seen)


def _takes_upload(route) -> bool:
    """True when the handler binds an UploadFile (single or list)."""
    try:
        params = inspect.signature(route.endpoint).parameters
    except (TypeError, ValueError):
        return False
    return any("UploadFile" in str(p.annotation) for p in params.values())


class SignGuessReadsOnlyTheHeadTests(unittest.TestCase):
    """The sniff's memory and its answer both come from the first rows."""

    def test_cost_does_not_grow_with_the_size_of_the_file(self):
        row = b"2021-03-01,COFFEE SHOP,-4.50\n"
        body = b"Date,Description,Amount\n" + row * 300_000   # ~8 MB
        tracemalloc.start()
        try:
            # the file itself was allocated before tracing started, so the
            # peak below is what the SNIFF costs on top of holding it
            tracemalloc.reset_peak()
            guess = bulk_import._csv_sign_guess(body)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(guess, ("bank", True))
        self.assertLess(
            peak, len(body) // 4,
            f"the sniff allocated {peak:,} bytes for a {len(body):,}-byte "
            "file — it must read a fixed head, not the whole upload")

    def test_rows_past_the_byte_window_do_not_change_the_guess(self):
        """Rows the sniff never reaches cannot vote on the convention.

        Wide rows fill the byte window long before the row cap, so this
        file's later rows — which would swing the answer to the ambiguous
        'plaid' reading if they were counted — sit outside it.
        """
        pad = b"X" * 2000
        row_len = len(b"2021-03-01," + pad + b",-4.50\n")
        in_window = bulk_import.SNIFF_BYTES // row_len
        negatives = in_window // 2          # a clear majority of the window
        total = bulk_import.SNIFF_ROWS      # …but a minority of the rows

        self.assertLess(in_window, total, "the window must bind first")
        self.assertGreaterEqual(negatives / in_window, 0.4)
        self.assertLess(negatives / total, 0.4)

        lines = [b"Date,Description,Amount"]
        for i in range(total):
            amount = b"-4.50" if i < negatives else b"4.50"
            lines.append(b"2021-03-01," + pad + b"," + amount)
        body = b"\n".join(lines) + b"\n"

        self.assertEqual(bulk_import._csv_sign_guess(body), ("bank", True))

    def test_a_torn_final_row_is_dropped_rather_than_half_read(self):
        """A cut cell must not become a number the file never contained.

        Every row in the window is positive except a final one whose
        amount the byte window slices through. Reading that fragment as a
        value would be inventing data; dropping it leaves the honest
        all-positive answer.
        """
        pad = b"X" * 2000
        row = b"2021-03-01," + pad + b",4.50\n"
        head = row * (bulk_import.SNIFF_BYTES // len(row))
        body = (b"Date,Description,Amount\n" + head
                + b"2021-03-01," + pad + b",-999.99\n")
        self.assertGreater(len(body), bulk_import.SNIFF_BYTES)
        self.assertEqual(bulk_import._csv_sign_guess(body), ("plaid", False))


class UploadDoorsAreRateLimitedTests(unittest.TestCase):
    """An upload route buffers and parses megabytes; repeating it must
    cost the caller a budget."""

    def test_every_upload_route_carries_a_rate_limit(self):
        unlimited = sorted(route_key(r) for r in mutating_routes(app)
                           if _takes_upload(r) and not _rate_limited(r))
        self.assertEqual(unlimited, [],
                         "these upload doors accept a file with no per-IP "
                         "budget behind it")

    def test_the_bulk_analyze_door_is_covered(self):
        # the most expensive upload in the product: many files, the whole
        # tenant staging budget, and a parse of every one of them
        route = next(r for r in mutating_routes(app)
                     if r.path.endswith("/import/bulk/analyze"))
        self.assertTrue(_rate_limited(route))
