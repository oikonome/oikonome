"""Business Overview: every money aggregate excludes shadow-linked rows.

An account linked through two aggregators leaves two rows sharing one link
group, and both can carry entity_id, so summing every matching row counts one
real balance twice. The "Cash in the business" tile must apply the same
shadow-exclusion rule as the tax set-aside; otherwise one checking account
linked via two aggregators reads as double its balance. Both are decided on
the server (books.entity_cash, selfemploy.tax_set_aside).
"""

import pathlib
import re
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class BusinessShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Business.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_next_deadline_tile_shows_the_installment_due_on_it(self):
        """From January 1 to 15 the next 1040-ES deadline collects last
        year's fourth installment, which a quarter of this year's
        projection misstates; the estimate says which amount is due, and
        both clients' tile reads it, falling back to `quarterly` for a
        server that does not send it."""
        root = pathlib.Path(oikonome.__file__).parent.parent.parent
        for rel in ("webapp/src/pages/Business.tsx",
                    "mobile/src/app/business.tsx"):
            src = (root / rel).read_text()
            tile = re.search(r'"Next deadline".*?est\. quarterly', src, re.S)
            self.assertIsNotNone(tile, rel)
            self.assertRegex(tile.group(0),
                             r"next_payment\s*\?\?\s*\S*quarterly", rel)

    def test_cash_tile_excludes_shadow_linked_rows(self):
        # The Overview's cash and its "N accounts" are the server's
        # (books.entity_cash on the P&L reply), which leaves a linked
        # backup and a hidden account out; the page sums no balance.
        import inspect

        from oikonome.engine import books
        self.assertIn("links.shadow_ids", inspect.getsource(books.entity_cash))
        self.assertIn("pnl.data?.cash", self.src)
        label = re.search(r"[^\n]*sub=\{cash \? `[^\n]*account", self.src)
        self.assertIsNotNone(label, "accounts sub-label not found")
        self.assertIn("cash.accounts", label.group(0))

    def test_tax_set_aside_reads_the_servers_figures(self):
        block = self.src[self.src.index("function TaxSetAside"):]
        block = block[:block.index("\nfunction ")]
        self.assertIn("tax.data.set_aside", block)
        self.assertNotIn("balance_current", block)

    def test_form_dates_default_to_the_local_day(self):
        """toISOString() is the UTC day: west of Greenwich an evening trip
        or owner contribution defaulted to tomorrow — on Dec 31, into the
        next tax year."""
        self.assertNotIn("toISOString().slice(0, 10)", self.src)
        # the household's day (useHouseholdDay), never the browser's raw clock
        self.assertIn("date: localYmd(useHouseholdDay())", self.src)



class BusinessToastShapeTests(unittest.TestCase):
    def test_toasts_go_through_the_shared_helper(self):
        """client.ts toast() owns the event contract (plain text, or the
        {text, to} link form); hand-built dispatches drift from it."""
        try:
            src = _src("pages/Business.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertNotIn('"oiko-toast"', src)
        self.assertNotIn("const toast =", src)
        self.assertIn("const toastErr = (e: unknown) => toast(errText(e))", src)
