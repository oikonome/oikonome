"""The control that changed a request stays on screen when it fails.

Web Bills returned before its month stepper when a past month failed; a
range that failed took its range picker with it on Bill history, Cash
Flow's overview and income and Spending (both clients); the phone hid the
Overview / Spending / Income tabs when the overview failed, so the Income
tab, a read of its own, could not be reached; web Debt replaced the whole
page, form included, when a what-if was refused; web Rules swapped the
page for a skeleton on every keystroke of a search (the box lost focus)
and a failed search took the box with it; the web Month and Year lenses
blanked to a skeleton on every step, where the phone dims the old period.
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


class ControlsStay(unittest.TestCase):
    def test_web_bills_keeps_its_month_stepper(self):
        src = _src(self, _WEB / "pages" / "Bills.tsx")
        self.assertNotIn("Couldn't load bills: {String(q.error)}", src)
        fail = src[src.index("  if (q.data === undefined)\n    return (<>{title}{stepper}"):]
        self.assertIn("<FailedLine q={q}", fail[:300])
        self.assertIn("return (<>{title}{stepper}\n      <div className=\"centered\">",
                      src)

    def test_a_failed_range_keeps_its_picker(self):
        for p in (_WEB / "pages" / "BillsHistory.tsx",
                  _MOB / "app" / "bill-history.tsx"):
            src = _src(self, p)
            self.assertIn("setLastGood(", src, p)
            self.assertIn("this bill's transactions (", src, p)
        cf = _src(self, _WEB / "pages" / "CashFlow.tsx")
        self.assertNotIn("Couldn't load: {String(iq.error)}", cf)
        self.assertNotIn("Couldn't load flow —", cf)
        for p in (_WEB / "pages" / "CashFlow.tsx", _MOB / "app" / "cashflow.tsx"):
            src = _src(self, p)
            self.assertIn("what={`income (${rangeCaption(range)})`}", src, p)
            self.assertIn("what={`the flow (${", src, p)
        for p in (_WEB / "pages" / "Spending.tsx", _MOB / "app" / "spending.tsx"):
            self.assertIn("what={`spending (${rangeCaption(range)})`}",
                          _src(self, p), p)

    def test_a_failed_page_read_keeps_the_pages_name(self):
        """Net Worth and Accounts answered a failed first read with the
        failed line alone, on a page with no title."""
        nw = _src(self, _WEB / "pages" / "NetWorth.tsx")
        self.assertTrue('return <><h1>Net Worth</h1><FailedLine q={q} '
                        'what="net worth" /></>;' in nw)
        acc = _src(self, _WEB / "pages" / "Accounts.tsx")
        fail = acc[acc.index("if (accounts.isLoadingError)"):]
        self.assertTrue("{!embedded && <h1>Accounts</h1>}" in fail[:260])
        self.assertTrue('<FailedLine q={accounts} what="accounts" />'
                        in fail[:400])

    def test_phone_cash_flow_tabs_survive_a_failed_overview(self):
        src = _src(self, _MOB / "app" / "cashflow.tsx")
        tabs = src.index('<SegmentedControl label="Cash Flow view"')
        # the strip stands while the overview loads and when it fails, as
        # the web's does: nothing above it waits on the overview's read
        before = src[tabs - 300:tabs]
        self.assertNotIn("{d && (", before)
        self.assertNotIn("query.isError", before)
        self.assertGreater(src.index('<FailedLine q={query} what="cash flow" />'),
                           tabs)

    def test_web_debt_keeps_the_last_plan_and_its_form(self):
        src = _src(self, _WEB / "pages" / "Debt.tsx")
        self.assertNotIn("Couldn't load: {String(q.error)}", src)
        self.assertIn("if (q.data && q.data !== lastGood) setLastGood(q.data);",
                      src)
        self.assertIn("{whatIfErr && <Note tone=\"bad\"", src)

    def test_web_rules_search_keeps_the_page(self):
        src = _src(self, _WEB / "pages" / "Rules.tsx")
        self.assertNotIn("Couldn't load: {String(userQ.error)}", src)
        self.assertNotIn("Couldn't load: {String(rq.error)}", src)
        head = src[src.index('const userQ = useQuery({ queryKey: ["rules", "user"'):]
        self.assertIn("placeholderData: (prev) => prev", head[:300])
        self.assertIn('what={q ? `rules matching “${q}”` : "rules"}', src)
        self.assertIn('what={q ? `rules matching “${q}”` : "rules"}',
                      _src(self, _MOB / "app" / "rules.tsx"))

    def test_web_lenses_dim_the_old_period(self):
        for name, what in (("MonthLens.tsx", "the month"),
                           ("YearLens.tsx", "the year")):
            src = _src(self, _WEB / "pages" / name)
            self.assertIn("placeholderData: (prev) => prev", src, name)
            self.assertIn("q.isPlaceholderData ? { opacity: 0.5", src, name)
            self.assertIn(f'<FailedLine q={{q}} what="{what}" />', src, name)


if __name__ == "__main__":
    unittest.main()
