"""Secondary cards say a failed read in their own place, never "nothing".

A failed this-month read put "Nothing to chart yet" on Spending; failed
investment-fee and interest-and-fee reads hid their cards as if nothing
was ever charged; failed merchant searches read "No merchant matches
that."; a failed matched-pairs read hid the web card with its search box
and read "No matched pair names …" on the phone; failed categories read
"no custom categories yet"; failed import batches and stored W-2s hid
the undo; an unread nightly-finder setting showed as on; failed accounts
showed a goal as "no account (plan only)"; failed onboarding hid Today's
setup card and told the wizard to "connect at least one source"; the
wizard's summaries counted "0 bills" from reads that never answered.
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


class ReportCardsWait(unittest.TestCase):
    def assertBoth(self, web: str, mob: str, needle: str):
        self.assertIn(needle, _src(self, _WEB / web), web)
        self.assertIn(needle, _src(self, _MOB / mob), mob)

    def test_spending_month_and_fee_cards(self):
        sp = _src(self, _WEB / "pages" / "Spending.tsx")
        self.assertLess(sp.index('<FailedLine q={curQ} what="this month\'s spending" />'),
                        sp.index("<EmptyState title={EMPTY_LINES.spendingRange} />"))
        nw = _src(self, _WEB / "pages" / "NetWorth.tsx")
        self.assertNotIn("if (q.isPending || q.isLoadingError) return null;\n"
                         "  const fe: FeesReport", nw)
        self.assertBoth("pages/NetWorth.tsx", "app/networth.tsx",
                        'what="the investment fees" />')
        self.assertBoth("pages/CashFlow.tsx", "app/cashflow.tsx",
                        'what="the interest and fees paid" />')

    def test_merge_pickers_and_merchants_list(self):
        for web, mob in (("pages/BillsHistory.tsx", "app/bill-history.tsx"),
                         ("pages/Merchants.tsx", "app/merchant.tsx")):
            for p in (_WEB / web, _MOB / mob):
                src = _src(self, p)
                self.assertNotIn("!cands.isPending && options.length === 0",
                                 src, p)
                self.assertIn('<FailedLine q={cands} what="the merchants" />',
                              src, p)
        web = _src(self, _WEB / "pages" / "Merchants.tsx")
        self.assertNotIn("Couldn't load: {String(list.error)}", web)
        self.assertIn("{rows.length === 0 && list.data !== undefined && (", web)
        mob = _src(self, _MOB / "app" / "merchants.tsx")
        self.assertNotIn("Couldn't load merchants:", mob)
        self.assertIn('<FailedLine q={query} what="merchants" />', mob)

    def test_matched_pairs_keep_the_card_and_the_search(self):
        web = _src(self, _WEB / "pages" / "Reimburse.tsx")
        self.assertNotIn("if (!q.data || (pairs.length === 0", web)
        self.assertIn('<FailedLine q={q} what="the matched pairs" />', web)
        mob = _src(self, _MOB / "app" / "reimburse.tsx")
        self.assertIn('<FailedLine q={pairs} what="the matched pairs" />', mob)
        self.assertIn("{pairRows.length === 0 && pairs.data !== undefined && (",
                      mob)

    def test_categories_imports_and_w2s(self):
        web = _src(self, _WEB / "pages" / "Rules.tsx")
        self.assertIn(': cats.data === undefined ? "Loading…"', web)
        self.assertBoth("pages/Rules.tsx", "app/rules.tsx",
                        '<FailedLine q={cats} what="the categories" />')
        mob = _src(self, _MOB / "app" / "rules.tsx")
        self.assertNotIn("Couldn't load categories — tap to retry.", mob)
        self.assertBoth("pages/Import.tsx", "app/imports.tsx",
                        '<FailedLine q={batches} what="the recent imports" />')
        self.assertBoth("pages/Import.tsx", "app/imports.tsx",
                        '<FailedLine q={list} what="the stored documents" />')

    def test_settings_that_read_as_a_default(self):
        web = _src(self, _WEB / "pages" / "Bills.tsx")
        self.assertNotIn("const on = s.data ? s.data.bill_detection_auto "
                         "!== false : true;", web)
        self.assertIn('<FailedLine q={s} what="the nightly search setting" />',
                      web)
        mob = _src(self, _MOB / "app" / "(tabs)" / "bills.tsx")
        self.assertIn('<FailedLine q={settings} what="the nightly search '
                      'setting" />', mob)
        self.assertBoth("components/SavingsGoalsCard.tsx", "app/budget.tsx",
                        'what="the accounts" />')

    def test_today_setup_and_the_wizard(self):
        self.assertBoth("pages/Today.tsx", "app/(tabs)/index.tsx",
                        '<FailedLine q={ob} what="the setup progress" />')
        web = _src(self, _WEB / "pages" / "Welcome.tsx")
        self.assertIn('failLine(ob.error, "the setup progress", "web").text', web)
        self.assertIn('["Bills & income", bills.data', web)
        self.assertIn('["Today checklist", d ? checklistSummary(d) : '
                      'UNKNOWN_FIGURE]', web)
        mob = _src(self, _MOB / "app" / "welcome.tsx")
        self.assertIn('failLine(ob.error, "the setup progress", "phone").text',
                      mob)
        self.assertIn('["Bills & income", bills.data', mob)
        self.assertIn('["Today checklist", d ? checklistSummary(d) : '
                      'UNKNOWN_FIGURE]', mob)
        wb = _src(self, _WEB / "components" / "WizardBills.tsx")
        # "found nothing" only from a scan that answered
        self.assertIn("{detect.isSuccess && proposals.length === 0", wb)
        self.assertNotIn("Couldn't load bills: {errText(q.error)}", wb)
        self.assertBoth("components/WizardBills.tsx",
                        "components/wizard-bills.tsx",
                        '{q.data === undefined && q.isError && '
                        '<FailedLine q={q} what="bills" />}')


if __name__ == "__main__":
    unittest.main()
