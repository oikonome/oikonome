"""A merchant row on the Spending tab is a figure for one window; the
history it links to lists rows by window too. The link names the window, and
the history page opens on it — otherwise the page that answers "what made
this figure" lists a different stretch of the ledger than the figure read.
"""
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WEB = _ROOT / "webapp" / "src"
_MOB = _ROOT / "mobile" / "src"


class MerchantDrillKeepsItsWindow(unittest.TestCase):
    def test_the_spending_merchant_link_names_its_window(self):
        src = (_WEB / "pages" / "Spending.tsx").read_text()
        self.assertIn(
            "`/bills/history?payee=${encodeURIComponent(payee)}"
            "&window=${range}`", src)

    def test_the_history_page_opens_on_the_window_a_link_names(self):
        web = (_WEB / "pages" / "BillsHistory.tsx").read_text()
        self.assertIn('sp.get("window")', web)
        self.assertIn('RANGES.includes(linked) ? linked : "all"', web)
        mob = (_MOB / "app" / "bill-history.tsx").read_text()
        self.assertIn("window: linked", mob)
        self.assertIn('? linked as TrendRange : "all"', mob)


if __name__ == "__main__":
    unittest.main()
