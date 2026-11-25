"""An empty screen says one short line, the same words on both clients,
with at most one action.

A filter that matched nothing was told to "connect a bank or import a
file" on the web and "No transactions." on the phone; a new household saw
"$0" net worth, "Nothing owed", "$0 came in" and "$0 spent" with no way to
link an account; a narrow bill-history range said no transaction matched
the payee; the Merchants catalog said "No merchants match that." with no
search; the web hid Recent imports when there were none; the phone's
Month lens dropped its empty cards; Reimburse, the Assistant, Alerts, Line
items and Bills used different words (or no action) on each client, and
Bills told people to press a button named something else.
(specs/screen-states.md)

The words are one table (EMPTY_LINES, in the screen-states twin block both
clients carry character for character); "Connect or import" is offered
only when an accounts read answered with none (noAccountsYet).
"""

import pathlib
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"

_HEAD = "// ---- screen states: twin block ----"
_TAIL = "// ---- end of the screen-states twin block ----"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


# screen → the empty lines it says, on both clients
_SCREENS = [
    ("pages/Transactions.tsx", "app/(tabs)/transactions.tsx",
     ("EMPTY_LINES.filtered", "EMPTY_LINES.monthNoTransactions",
      "<NoAccountsLine />")),
    ("pages/BillsHistory.tsx", "app/bill-history.tsx", ("EMPTY_LINES.range",)),
    ("pages/MonthLens.tsx", "app/month.tsx",
     ("EMPTY_LINES.noSpending}", "EMPTY_LINES.noSpendingPosted",
      "<NoAccountsLine />")),
    ("pages/NetWorth.tsx", "app/networth.tsx", ("<NoAccountsLine />",)),
    ("pages/Debt.tsx", "app/debt.tsx", ("<NoAccountsLine />",)),
    ("pages/CashFlow.tsx", "app/cashflow.tsx", ("EMPTY_LINES.incomeRange",)),
    ("pages/Spending.tsx", "app/spending.tsx",
     ("EMPTY_LINES.spendingRange", "<NoAccountsLine />")),
    ("pages/Business.tsx", "app/business.tsx", ("EMPTY_LINES.businessBooks",)),
    ("pages/Merchants.tsx", "app/merchants.tsx",
     ("EMPTY_LINES.filtered", "EMPTY_LINES.merchants")),
    ("pages/Settings.tsx", "app/activity.tsx", ("EMPTY_LINES.filtered",)),
    ("pages/Import.tsx", "app/imports.tsx", ("EMPTY_LINES.imports",)),
    ("pages/Reimburse.tsx", "app/reimburse.tsx",
     ("EMPTY_LINES.reimburse}", "EMPTY_LINES.reimburseAction")),
    ("pages/Assistant.tsx", "app/assistant.tsx",
     ("EMPTY_LINES.assistantOff", "EMPTY_LINES.assistantAction")),
    ("pages/Alerts.tsx", "app/alerts.tsx", ("EMPTY_LINES.filtered",)),
    ("pages/Items.tsx", "app/items.tsx",
     ("EMPTY_LINES.filtered", "EMPTY_LINES.lineItems}",
      "EMPTY_LINES.lineItemsAction")),
    ("pages/Bills.tsx", "app/(tabs)/bills.tsx",
     ("EMPTY_LINES.bills}", "EMPTY_LINES.billsAction")),
]

# the per-client words these replaced
_GONE = [
    ("pages/Transactions.tsx", "No matches anywhere"),
    ("pages/Transactions.tsx", "connect a bank or import a file"),
    ("app/(tabs)/transactions.tsx", '"No transactions."'),
    ("pages/Bills.tsx", "Find recurring bills"),
    ("app/(tabs)/bills.tsx", "Find recurring bills"),
    ("app/alerts.tsx", "Nothing under this filter."),
    ("app/reimburse.tsx", "Nothing awaiting payback"),
    ("app/assistant.tsx", "enabled on this server"),
    ("pages/Spending.tsx", "Nothing to chart yet."),
    ("app/items.tsx", "every line item from parsed"),
    ("pages/Items.tsx", "No line items matching"),
    ("pages/Merchants.tsx", "No merchants match that.</p>"),
    ("app/merchants.tsx", "No merchants match that.</Text>"),
]


class EmptyStatesSayOneLine(unittest.TestCase):
    def test_the_words_are_one_table_in_the_twin_block(self):
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            src = _src(self, p)
            block = src[src.index(_HEAD):src.index(_TAIL)]
            self.assertIn("export const EMPTY_LINES = {", block, p)
            self.assertIn("export function noAccountsYet(", block, p)

    def test_connect_is_offered_only_when_nothing_is_linked(self):
        for p, to in ((_WEB / "components" / "Loaded.tsx", '"/accounts"'),
                      (_MOB / "components" / "loaded.tsx", '"/connect-hub"')):
            src = _src(self, p)
            self.assertIn("export function useNoAccounts(): boolean", src, p)
            self.assertIn("return noAccountsYet(q);", src, p)
            self.assertIn(f"<EmptyState title={{EMPTY_LINES.noAccounts}} to={to}",
                          src, p)
            self.assertIn("action={EMPTY_LINES.connect}", src, p)

    def test_each_screen_says_its_line_on_both_clients(self):
        for web, mob, needles in _SCREENS:
            for p in (_WEB / web, _MOB / mob):
                src = _src(self, p)
                for n in needles:
                    self.assertIn(n, src, f"{p}: {n}")

    def test_the_old_words_are_gone(self):
        for rel, needle in _GONE:
            p = (_WEB if rel.startswith("pages/") else _MOB) / rel
            self.assertNotIn(needle, _src(self, p), f"{p}: {needle}")

    def test_the_bills_action_is_only_for_those_who_may_run_it(self):
        web = _src(self, _WEB / "pages" / "Bills.tsx")
        self.assertIn("action={mayEdit && !detect.isPending ? "
                      "EMPTY_LINES.billsAction : undefined}", web)
        mob = _src(self, _MOB / "app" / "(tabs)" / "bills.tsx")
        self.assertIn("action={!viewer && !detect.isPending && !offline\n"
                      "            ? EMPTY_LINES.billsAction : undefined}", mob)

    def test_the_phone_feedback_says_a_failed_read(self):
        src = _src(self, _MOB / "app" / "feedback.tsx")
        self.assertIn('<FailedLine q={testing} what="the feedback page" />', src)


if __name__ == "__main__":
    unittest.main()
