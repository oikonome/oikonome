"""No screen goes blank or spins for good when a read fails.

Every screen that reads data (Bills, Net worth, Line items) shows a
failed-read line in place of its body when the first read fails, and
each Budget section says a failed read in its own place on both clients.
A bill or bill-history screen opened with no bill named shows an
empty-state line instead of a spinner, since its read never starts. The
wizard's proposals and the member's history choice end in the failed
line. Activity keeps its loaded pages across a filter change or a pull
and never shows an empty-log message while entries exist.
(specs/screen-states.md)
"""

import pathlib
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


class ScreensNeverBlank(unittest.TestCase):
    def test_first_load_failures_say_so(self):
        bills = _src(self, _MOB / "app" / "(tabs)" / "bills.tsx")
        self.assertIn("{d === undefined && query.isError && (\n"
                      "        <FailedLine q={query}", bills)
        for p in (_MOB / "app" / "networth.tsx",
                  _WEB / "pages" / "NetWorth.tsx"):
            self.assertIn('<FailedLine q={', _src(self, p))
            self.assertIn('what="net worth" />', _src(self, p), p)
        items = _src(self, _WEB / "pages" / "Items.tsx")
        self.assertIn('<FailedLine q={query} what="line items" />', items)
        self.assertIn('<FailedLine q={query} what="line items" />',
                      _src(self, _MOB / "app" / "items.tsx"))

    def test_budget_sections_say_a_failed_read_in_their_place(self):
        for p in (_WEB / "pages" / "Budget.tsx", _MOB / "app" / "budget.tsx"):
            src = _src(self, p)
            self.assertIn('what="this month\'s plan" />', src, p)
            self.assertIn('what="the budget settings" />', src, p)

    def test_no_bill_named_is_a_line_not_a_spinner(self):
        for p in (_MOB / "app" / "bill.tsx", _MOB / "app" / "bill-history.tsx",
                  _WEB / "pages" / "BillsHistory.tsx"):
            src = _src(self, p)
            self.assertIn('<EmptyState title="Pick a bill on Bills & Income."',
                          src, p)
        for p in (_MOB / "app" / "bill.tsx", _MOB / "app" / "bill-history.tsx"):
            src = _src(self, p)
            self.assertNotIn("tap to retry", src, p)

    def test_wizard_reads_end_in_the_failed_line(self):
        wb = _src(self, _MOB / "components" / "wizard-bills.tsx")
        self.assertIn('return q.isError ? <FailedLine q={q} what="bills" />',
                      wb)
        for p in (_WEB / "components" / "HistoryChoice.tsx",
                  _MOB / "components" / "wizard-history.tsx"):
            self.assertIn('what="the history setting" />', _src(self, p), p)

    def test_activity_keeps_its_pages_until_the_answer(self):
        src = _src(self, _MOB / "app" / "activity.tsx")
        self.assertNotIn("setPages([])", src)
        self.assertIn("pagesAfter(prev, filterKey, cursor, page)", src)
        self.assertIn('<FailedLine q={query} what="the activity log" />', src)


if __name__ == "__main__":
    unittest.main()
